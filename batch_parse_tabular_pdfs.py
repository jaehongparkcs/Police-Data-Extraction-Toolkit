#!/usr/bin/env python3
"""
batch_parse_tabular_pdfs.py
============================
Universal parser for tabular police PDFs with text layers (no OCR).

Two-tier extraction strategy:
  1. RECT-BASED — if the PDF has table cell borders (rects/lines),
     use them as explicit column+row boundaries → pixel-perfect.
  2. WORD-POSITION — if no table borders exist, detect columns from
     header word positions with adaptive gap analysis.

Usage:
    python batch_parse_tabular_pdfs.py <input_dir> --out <output_dir>
    python batch_parse_tabular_pdfs.py <input_dir> --out <output_dir> --pages 5

Requirements:
    pip install pdfplumber pandas
"""

import argparse
import re
from collections import defaultdict, Counter
from pathlib import Path

import pandas as pd
import pdfplumber

# Optional: wordfreq powers the garbled-text detector in flag_rows.
# If it isn't installed, that specific check is skipped (everything
# else still works).  Install with: pip install wordfreq
try:
    from wordfreq import zipf_frequency as _WORDFREQ
except ImportError:
    _WORDFREQ = None


# ══════════════════════════════════════════════════════════════════════════
#  TIER 1: RECT-BASED TABLE EXTRACTION
# ══════════════════════════════════════════════════════════════════════════

def _get_table_lines(page):
    """Extract vertical and horizontal table separator lines from rects.

    Returns (v_lines, h_lines) as sorted lists of x / y positions,
    or (None, None) if no table grid is found.
    """
    if not page.rects:
        return None, None

    # Vertical separators: tall, thin rects
    v_rects = [r for r in page.rects if r["height"] > 5 and r["width"] < 3]
    # Horizontal separators: wide, thin rects
    h_rects = [r for r in page.rects if r["width"] > 50 and r["height"] < 3]

    if len(v_rects) < 3:  # need at least 3 vertical lines for 2 columns
        return None, None

    v_lines = sorted(set(round(r["x0"], 1) for r in v_rects))
    h_lines = sorted(set(round(r["top"], 1) for r in h_rects))

    return v_lines, h_lines


def parse_page_rects(page, v_lines, h_lines):
    """Extract a table using explicit rect-based boundaries."""
    settings = {
        "vertical_strategy": "explicit",
        "explicit_vertical_lines": v_lines,
    }
    if h_lines:
        settings["horizontal_strategy"] = "explicit"
        settings["explicit_horizontal_lines"] = h_lines
    else:
        settings["horizontal_strategy"] = "text"

    table = page.extract_table(settings)
    return table if table else []


# ══════════════════════════════════════════════════════════════════════════
#  TIER 2: WORD-POSITION TABLE EXTRACTION
# ══════════════════════════════════════════════════════════════════════════

def _find_header_y(words):
    """Find the Y position of the header row."""
    y_groups = defaultdict(list)
    for w in words:
        y_groups[round(w["top"])].append(w)

    sorted_ys = sorted(y_groups.keys())

    for idx, y in enumerate(sorted_ys):
        ws = y_groups[y]
        texts = " ".join(w["text"] for w in ws)

        if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", texts):
            continue
        if re.search(r"\d{4}-\d{5,}", texts):
            continue
        if re.match(r"^[\d\s,.:]+$", texts):
            continue

        alpha = sum(1 for w in ws if re.search(r"[A-Za-z]", w["text"])
                    and not re.match(r"^\d+$", w["text"]))
        if alpha < 5:
            continue

        # Verify data follows
        for next_y in sorted_ys[idx + 1: idx + 4]:
            next_texts = " ".join(w["text"] for w in y_groups[next_y])
            if re.search(r"\d", next_texts):
                return y

    return None


def _detect_columns(words, header_y):
    """Detect columns from header words using adaptive gap analysis."""
    header_words = sorted(
        [w for w in words if round(w["top"]) == header_y],
        key=lambda w: w["x0"],
    )
    if not header_words:
        return []

    # Repair shattered headers: some PDFs render a column name with spurious
    # spaces inside it ("C ALL _ NO" for "CALL_NO", "U NIT" for "UNIT"), so
    # the header arrives as many fragments.  Adjacent fragments that touch or
    # overlap (gap <= ~2px) are pieces of one word — glue them back together
    # before analysing column gaps.  Clean headers have no such near-zero
    # gaps between separate words, so they're unaffected.
    joined = []
    cur = dict(header_words[0])
    for nxt in header_words[1:]:
        gap = nxt["x0"] - cur["x1"]
        if gap <= 2:
            cur = {
                "text": cur["text"] + nxt["text"],
                "x0": cur["x0"],
                "x1": nxt["x1"],
                "top": cur.get("top", header_y),
            }
        else:
            joined.append(cur)
            cur = dict(nxt)
    joined.append(cur)
    header_words = joined

    gaps = []
    for i in range(len(header_words) - 1):
        gaps.append(header_words[i + 1]["x0"] - header_words[i]["x1"])

    if not gaps:
        w = header_words[0]
        return [(w["text"], w["x0"], w["x1"])]

    min_gap = min(gaps)
    if min_gap < 5:
        sorted_gaps = sorted(gaps)
        threshold = min_gap * 2
        for i in range(len(sorted_gaps) - 1):
            if sorted_gaps[i + 1] > min_gap * 2:
                threshold = (sorted_gaps[i] + sorted_gaps[i + 1]) / 2
                break
    else:
        threshold = min_gap * 0.5

    columns = []
    i = 0
    while i < len(header_words):
        w = header_words[i]
        name_parts = [w["text"]]
        x_start = w["x0"]
        x_end = w["x1"]
        while i < len(header_words) - 1:
            gap = header_words[i + 1]["x0"] - header_words[i]["x1"]
            if gap < threshold:
                i += 1
                name_parts.append(header_words[i]["text"])
                x_end = header_words[i]["x1"]
            else:
                break
        columns.append((" ".join(name_parts), x_start, x_end))
        i += 1

    return columns


def _refine_column_starts(columns, words, header_y, snap_tol=22, min_frac=0.4):
    """Refine each column's start x by snapping it to where the DATA
    actually begins, not just where the header sits.

    Some PDFs position headers offset from their left-aligned data (e.g.
    a header centered a few px right of the values below it).  Using the
    header x as the column edge then misassigns words.  We find the
    x-positions where many data rows have a word starting — those recurring
    "start stacks" are the true column left-edges — and snap each header
    column to the nearest such cluster.

    Safety: this only REFINES; it never adds or removes columns.  A column
    snaps only when a strong cluster sits near its header AND within the
    midpoint to its neighbours (so a snap can't cross into an adjacent
    column).  Columns with no nearby cluster keep their header position, so
    formats where headers already align with data (or where data doesn't
    stack cleanly, like rect-based tables) are unchanged.
    """
    yg = defaultdict(list)
    for w in words:
        if w["top"] > header_y + 3:
            yg[round(w["top"])].append(w)
    n_rows = len(yg)
    if n_rows < 5 or len(columns) < 2:
        return columns  # not enough data to cluster reliably

    # Count, per 2px bucket, how many rows have a word starting there.
    starts = Counter()
    for ws in yg.values():
        seen = set()
        for w in ws:
            b = round(w["x0"] / 2) * 2
            if b not in seen:
                starts[b] += 1
                seen.add(b)
    thresh = max(3, n_rows * min_frac)
    strong = {x: c for x, c in starts.items() if c >= thresh}
    if not strong:
        return columns  # no clear stacks (e.g. rect-based/packed tables)

    hdr_starts = [xs for _, xs, _ in columns]
    refined = []
    prev_start = None
    for i, (name, xs, xe) in enumerate(columns):
        # A valid cluster for this column sits to the RIGHT of the previous
        # column's chosen start (plus a small margin so columns stay
        # ordered) and to the LEFT of the next column's header start (so a
        # snap can't jump into the next column).  Using the previous
        # *refined* start — not the header midpoint — lets a whole row of
        # columns shift left together when the data is offset from the
        # headers, which the midpoint rule would wrongly block.
        lo = (prev_start + 4) if prev_start is not None else xs - snap_tol
        hi = hdr_starts[i + 1] - 4 if i + 1 < len(columns) else xs + snap_tol
        candidates = [
            (abs(cx - xs), cx) for cx in strong
            if abs(cx - xs) <= snap_tol and lo <= cx <= hi
        ]
        if candidates:
            candidates.sort()
            chosen = float(candidates[0][1])
        else:
            chosen = xs
        refined.append((name, chosen, xe))
        prev_start = chosen
    return refined


def _cluster_column_starts(words, top_cut, min_frac=0.4):
    """Recurring word-start x-positions across data rows = column edges.

    Counts, per 2px bucket, how many rows start a word there; keeps buckets
    recurring in >= min_frac of rows.  Robust to tight vs wide spacing
    because it counts start-stacks, not gaps.  Returns (edges, n_rows).
    """
    yg = defaultdict(list)
    for w in words:
        if w["top"] > top_cut:
            yg[round(w["top"])].append(w)
    n = len(yg)
    if n < 5:
        return [], n
    starts = Counter()
    for ws in yg.values():
        seen = set()
        for w in ws:
            b = round(w["x0"] / 2) * 2
            if b not in seen:
                starts[b] += 1
                seen.add(b)
    thresh = max(3, n * min_frac)
    strong = sorted(x for x, c in starts.items() if c >= thresh)
    merged = []
    for x in strong:
        if merged and x - merged[-1] <= 4:
            continue
        merged.append(x)
    return merged, n


def _cluster_is_date_like(words, top_cut, x, tol=6):
    """True if the words starting near x are mostly dates (M/D/YY[YY])."""
    vals = []
    yg = defaultdict(list)
    for w in words:
        if w["top"] > top_cut:
            yg[round(w["top"])].append(w)
    for ws in yg.values():
        for w in ws:
            if abs(w["x0"] - x) <= tol:
                vals.append(w["text"])
                break
    if len(vals) < 3:
        return False
    hits = sum(1 for v in vals
               if re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", v))
    return hits >= len(vals) * 0.6


def _align_columns_by_order(columns, words, header_y, data_cutoff):
    """Align the canonical column schema to THIS page's data clusters by
    ORDER, not absolute position.

    Every page of a given report has its columns in the same left-to-right
    order, but the absolute x-positions can shift page to page (different
    render scale/offset).  Matching by order is therefore reliable where
    matching by position is not.

    Handled shapes:
      - exactly len(schema) clusters      → 1:1 by order.
      - len(schema)+1 clusters AND the surplus stack (position 2) is
        date-like → the time and date are two stacks of one When-Reported-
        style column; fold them together.
    Any other shape (surplus not a date, wrong count, no clusters) returns
    None so the caller falls back to header-based refinement — this is what
    keeps single-layout departments (whose surplus stacks are wrapped text,
    not dates) from being mis-aligned.
    """
    top_cut = header_y if data_cutoff >= 0 else (
        min((w["top"] for w in words), default=header_y) - 1)
    clusters, n = _cluster_column_starts(words, top_cut)
    schema = [c for c in columns]
    nC, nS = len(clusters), len(schema)
    if nC < 2 or nS < 2:
        return None

    if nC == nS:
        # Sanity check: 1:1 by order is only trustworthy if each cluster
        # lands reasonably near the header column it maps to.  When a
        # multi-word value (e.g. a 3-word address) splits into several
        # clusters, the count can coincidentally still equal nS but the
        # mapping is wrong — a cluster ends up far from its header.  If the
        # median mapping error is large, refuse (fall back to header-based
        # refinement) rather than emit a scrambled mapping.
        hdr_starts = [c[1] for c in schema]
        errors = sorted(abs(clusters[i] - hdr_starts[i]) for i in range(nS))
        median_err = errors[nS // 2]
        # Allow a global offset (whole table shifted) but not per-column
        # chaos: compare against the SHIFTED headers too.
        offset = clusters[0] - hdr_starts[0]
        shifted_err = sorted(
            abs(clusters[i] - (hdr_starts[i] + offset)) for i in range(nS))
        median_shifted = shifted_err[nS // 2]
        if min(median_err, median_shifted) > 60:
            return None
        return [(schema[i][0], float(clusters[i]), float(clusters[i]))
                for i in range(nS)]

    if nC == nS + 1:
        # Only accept the surplus if it's the date splitting off the time:
        # the 3rd cluster (index 2) must be date-like, and the 2nd column
        # of the schema is the one that carries dates/times.
        second_name = schema[1][0].lower()
        second_is_datey = any(k in second_name
                              for k in ("date", "time", "reported",
                                        "occurred", "received"))
        if second_is_datey and _cluster_is_date_like(words, top_cut,
                                                      clusters[2]):
            # time (clusters[1]) and date (clusters[2]) both -> column 2;
            # the rest map in order.
            cols = [(schema[0][0], float(clusters[0]), float(clusters[0])),
                    (schema[1][0], float(clusters[1]), float(clusters[1]))]
            for name, cx in zip([s[0] for s in schema[2:]], clusters[3:]):
                cols.append((name, float(cx), float(cx)))
            return cols
        return None

    return None


def parse_page_words(page, col_info=None):
    """Parse a page using word positions.

    Returns (rows_as_dicts, col_info_for_next_page).
    """
    # Use tight x_tolerance to prevent concatenation of adjacent values
    words = page.extract_words(x_tolerance=1)
    if not words:
        return [], col_info

    # Attach the constituent characters to each word (extract_words
    # doesn't include them).  We match non-space chars whose center
    # falls within the word's bounding box.  This lets us later split
    # words that span column boundaries due to overlapping content.
    chars_by_line = defaultdict(list)
    for c in page.chars:
        if c.get("text") != " ":
            chars_by_line[round(c["top"])].append(c)
    for w in words:
        wy = round(w["top"])
        wchars = [
            c for c in chars_by_line.get(wy, [])
            if c["x0"] >= w["x0"] - 0.5 and c["x1"] <= w["x1"] + 0.5
        ]
        w["chars"] = sorted(wchars, key=lambda c: c["x0"])

    if col_info is None:
        header_y = _find_header_y(words)
        if header_y is None:
            return [], None
        columns = _detect_columns(words, header_y)
        if not columns:
            return [], None
        col_info = (header_y, columns)
        data_cutoff = header_y + 3
    else:
        header_y, columns = col_info
        # Check for repeated header on this page.  If found, filter rows
        # below it; if NOT found, this is a continuation page whose data
        # starts at the very top — filter nothing (cutoff = -1), so the
        # first data row (often at the same Y a header would occupy on
        # page 1) is not mistakenly dropped.
        y_groups = defaultdict(list)
        for w in words:
            y_groups[round(w["top"])].append(w)
        first_word = columns[0][0].split()[0]
        last_word = columns[-1][0].split()[-1]
        data_cutoff = -1  # no header on this page by default
        for y in sorted(y_groups.keys())[:5]:
            texts = {w["text"] for w in y_groups[y]}
            if first_word in texts and last_word in texts:
                header_y = y
                data_cutoff = header_y + 3
                new_cols = _detect_columns(words, header_y)
                if new_cols:
                    columns = new_cols
                    col_info = (header_y, columns)
                break

    # Determine this page's column boundaries.  First try order-based
    # alignment to the page's own data clusters — this adapts to layouts
    # whose absolute x-positions drift page-to-page (or gain/lose the date
    # column) while keeping the columns matched by their left-to-right
    # order.  It self-guards: it only returns columns when the cluster
    # count matches the schema (or is schema+1 with a genuine date surplus),
    # otherwise None.  On None we fall back to header positions + light
    # refinement, so single-layout departments are unchanged.
    aligned = _align_columns_by_order(columns, words, header_y, data_cutoff)
    if aligned is not None:
        columns = aligned
    else:
        columns = _refine_column_starts(columns, words, header_y)

    # Build column ranges: each column starts at its (refined) x_start
    # and ends where the next column's start begins.
    col_ranges = []
    for i, (name, x_start, x_end) in enumerate(columns):
        x_left = x_start - 3  # small left buffer
        if i + 1 < len(columns):
            x_right = columns[i + 1][1] - 1  # just before next column start
        else:
            x_right = page.width + 50
        col_ranges.append((name, x_left, x_right))

    # Header tokens (individual words from every column name).  A header
    # can span two physical lines (e.g. "Arrest\nNumber"), so the second
    # line leaks in as a spurious data row.  We use this set to drop any
    # data row whose entire content is made of header words.
    header_tokens = set()
    for name, _, _ in columns:
        for tok in name.split():
            header_tokens.add(tok.lower())

    # Detect a second header line: some headers wrap (e.g. "Arrest" on
    # line 1, "Number" on line 2).  The continuation sits a small gap
    # below the header and contains only header-like words (no dates,
    # case numbers, or many values).  If present, fold its words into
    # the header token set so it isn't mistaken for a data row.  We look
    # only just below the header, well above where data begins.
    if data_cutoff > 0:
        rows_below = defaultdict(list)
        for w in words:
            # Strictly below the header row (guard against sub-pixel Y of
            # the header line itself), within a small band above data.
            if round(w["top"]) > header_y + 3 and w["top"] <= header_y + 20:
                rows_below[round(w["top"])].append(w)
        for y in sorted(rows_below.keys()):
            ws_line = rows_below[y]
            line_text = " ".join(w["text"] for w in ws_line)
            # A data row has a date, a case-number-like token, or many
            # words; a header continuation is a few plain alpha words.
            looks_like_data = (
                re.search(r"\d{1,2}[/\-]\d", line_text)
                or re.search(r"\b\d{4,}\b", line_text)
                or len(ws_line) > 4
            )
            if looks_like_data:
                break  # reached real data; stop
            for w in ws_line:
                for tok in w["text"].split():
                    header_tokens.add(tok.lower())
            data_cutoff = y + 3  # push cutoff past this header line

    # Extract data
    data_words = [w for w in words if w["top"] > data_cutoff]
    row_ys = defaultdict(list)
    for w in data_words:
        row_ys[round(w["top"])].append(w)

    # Build a map of space-glyph positions per row.  The key structural
    # signal: overflow prose has an actual space character between words
    # ("ALCOHOL AND"), while adjacent-but-distinct column values have
    # only positional whitespace with NO space glyph ("0:00" | "0913").
    # We record the right edge (x1) of every space char, grouped by Y.
    space_x_by_y = defaultdict(list)
    for c in page.chars:
        if c.get("text") == " " and c["top"] > data_cutoff:
            space_x_by_y[round(c["top"])].append((c["x0"], c["x1"]))

    def _has_space_between(y, prev_x1, next_x0):
        """True if a space glyph directly connects the two words.

        A connecting space starts right after the previous word and ends
        right before the next word (a single space char, ~2-4px wide).
        A trailing space in a wide empty gap between columns does NOT
        connect — the next word starts far past the space's end.
        """
        for sx0, sx1 in space_x_by_y.get(y, []):
            # Space must begin at the previous word's right edge AND the
            # next word must begin right where the space ends.
            if abs(sx0 - prev_x1) <= 2 and abs(next_x0 - sx1) <= 3:
                return True
        return False

    def _word_col(x0):
        """Return which column index a word at x0 falls into by position."""
        for i, (_, cx_start, cx_end) in enumerate(col_ranges):
            if x0 >= cx_start and x0 < cx_end:
                return i
        if col_ranges and x0 >= col_ranges[-1][1]:
            return len(col_ranges) - 1
        return 0

    # Column header start positions for alignment detection
    col_x_starts = [xs for _, xs, _ in columns]

    def _starts_at_col(x0, tol=3):
        """Return column index if x0 aligns with a column's header
        start (within tol), else None."""
        for i, xs in enumerate(col_x_starts):
            if abs(x0 - xs) <= tol:
                return i
        return None

    rows = []
    for y in sorted(row_ys.keys()):
        ws = sorted(row_ys[y], key=lambda w: w["x0"])
        if not ws:
            continue

        # Narrow fix for physically-overlapping content: a word like
        # "PROHIBITED4" is wrapped offense text ("PROHIBITED") printed
        # over the PD digit ("4").  Split ONLY when a word is letters
        # followed by a single trailing digit, the letters and digit
        # sit in different columns, and there's no space between them
        # (space-connected words are handled normally).
        split_ws = []
        for w in ws:
            m = re.match(r"^([A-Za-z]{3,})(\d)$", w["text"])
            chars = w.get("chars")
            if m and chars and len(chars) == len(w["text"]):
                digit_char = chars[-1]
                letters_char0 = chars[0]
                if _word_col(digit_char["x0"]) > _word_col(letters_char0["x0"]):
                    # Split into letters (current col) + digit (its col)
                    split_ws.append({
                        "text": m.group(1),
                        "x0": letters_char0["x0"],
                        "x1": chars[-2]["x1"],
                        "top": w["top"],
                    })
                    split_ws.append({
                        "text": m.group(2),
                        "x0": digit_char["x0"],
                        "x1": digit_char["x1"],
                        "top": w["top"],
                    })
                    continue
            split_ws.append(w)
        ws = sorted(split_ws, key=lambda w: w["x0"])

        row = {name: [] for name, _, _ in col_ranges}

        current_col = _word_col(ws[0]["x0"])
        row[col_ranges[current_col][0]].append(ws[0]["text"])

        for i in range(1, len(ws)):
            w = ws[i]
            prev = ws[i - 1]
            pos_col = _word_col(w["x0"])
            y = round(w["top"])
            has_space = _has_space_between(y, prev["x1"], w["x0"])
            gap = w["x0"] - prev["x1"]

            # HIGHEST PRIORITY: hard x-alignment.  In a real table each
            # column's data starts at exactly its header's x-position, so a
            # word beginning right under a header start (within tol) belongs
            # to THAT column.  We apply this only when the word is NOT joined
            # to the previous word by a real space glyph — a connecting
            # space means genuine wrapped/overflow text that should stay in
            # the current cell (e.g. "*NO PAPERS (INSUFFICIENT ...)").  This
            # targets the fusion case (a later-column value with its own
            # start position butting against the previous value) without
            # disturbing wrapped text.
            aligned = _starts_at_col(w["x0"], tol=3)
            if (aligned is not None and aligned != current_col
                    and not has_space):
                current_col = aligned
                row[col_ranges[current_col][0]].append(w["text"])
            elif has_space and gap < 40:
                # A real space glyph joins this word to the previous one
                # with normal spacing → same cell's overflow text.  Keep
                # it in the current column regardless of X drift.
                row[col_ranges[current_col][0]].append(w["text"])
            elif pos_col > current_col:
                # Word sits in a later column (by position) and is NOT a
                # space-connected continuation → a distinct new value.
                current_col = pos_col
                row[col_ranges[current_col][0]].append(w["text"])
            elif not has_space and gap >= 40 and current_col < len(col_ranges) - 1:
                # Large empty gap with no connecting space, but the word
                # still falls in the current column's range (its value
                # starts just before the next column boundary, e.g. a
                # disposition like "*NO PAPERS" beginning a few px before
                # the header).  Advance to the next column.
                current_col += 1
                row[col_ranges[current_col][0]].append(w["text"])
            else:
                # No space, same/earlier column position → continuation.
                row[col_ranges[current_col][0]].append(w["text"])

        final = {name: " ".join(parts) for name, parts in row.items()}

        values = [v for v in final.values() if v.strip()]
        # Skip page-number footers.
        if not values or any(re.match(r"^Page\s+\d+", v) for v in values):
            continue
        # Skip multi-line header fragments: a row whose every word is a
        # header token (e.g. the second line "Number" of an
        # "Arrest\nNumber" header) is not real data.
        all_words = " ".join(values).split()
        if all_words and all(w.lower() in header_tokens for w in all_words):
            continue
        rows.append(final)

    # Last-column repair pass (for tables where the final column is a
    # short numeric/code field, e.g. Cleveland's "PD District").  Long
    # wrapped text can overflow into it, or a text disposition can land
    # there when the numeric value is blank.  These repairs are applied
    # ONLY when the last column is genuinely numeric-dominant across the
    # file, so tables whose last column is normally text (e.g. an arrest
    # log's "Arrest Reason" = MISDEMEANOR/FELONY) are left untouched.
    if len(col_ranges) >= 3:
        last_name = col_ranges[-1][0]
        nonempty = [str(r.get(last_name, "")).strip() for r in rows]
        nonempty = [v for v in nonempty if v]
        if nonempty:
            numeric_frac = sum(
                1 for v in nonempty if re.fullmatch(r"\d{1,3}", v)
            ) / len(nonempty)
            if numeric_frac >= 0.6:
                for final in rows:
                    last_val = final.get(last_name, "")
                    m = re.match(r"^(\d)\s+([A-Za-z].*)$", last_val)
                    if m:
                        # "4 PREMISES" → digit stays, wrapped words go to
                        # the longest text column (usually description).
                        final[last_name] = m.group(1)
                        text_cols = sorted(
                            col_ranges[:-1],
                            key=lambda c: len(final.get(c[0], "")),
                            reverse=True,
                        )
                        if text_cols:
                            tgt = text_cols[0][0]
                            final[tgt] = (final[tgt] + " " + m.group(2)).strip()
                    elif last_val and not any(ch.isdigit() for ch in last_val):
                        # Text-only value in a numeric column (e.g. "CMP")
                        # → move to the second-to-last column if it's empty.
                        disp_name = col_ranges[-2][0]
                        if not final.get(disp_name, "").strip():
                            final[disp_name] = last_val
                            final[last_name] = ""

    # Note: multi-line record merging is done globally in process_pdf
    # (after all pages are collected) so records that span a page break
    # are merged correctly.  We return the raw per-line rows here.
    return rows, col_info


# ══════════════════════════════════════════════════════════════════════════
#  MULTI-LINE RECORD MERGING
# ══════════════════════════════════════════════════════════════════════════

def _deinterleave_pass(rows):
    """Repair address/date pixel collisions across the whole table.

    Auto-detects which column holds addresses and which holds dates (by
    header name, falling back to content), then applies the shared
    deterministic de-interleave.  Only verifiably-clean recoveries are
    written; anything tangled is left for the review flagger.  If the
    de-interleave module isn't importable, rows pass through unchanged.
    """
    if not rows:
        return rows
    try:
        from deinterleave import fix_row
    except Exception:
        return rows

    cols = list(rows[0].keys())

    # Find the address column and date column by header name.
    def _find(patterns):
        for c in cols:
            cl = c.lower()
            if any(p in cl for p in patterns):
                return c
        return None

    addr_col = _find(["address", "location", "street"])
    date_col = _find(["date", "time", "occurred"])
    if not addr_col or not date_col:
        return rows  # not an address/date table; nothing to do

    for row in rows:
        fx = fix_row(row, addr_col=addr_col, date_col=date_col)
        if fx["date"]:
            row[date_col] = fx["date"]
        if fx["address"]:
            row[addr_col] = fx["address"]
    return rows


def _split_priority_location_pass(rows):
    """Split a fused priority+location column into its two fields.

    In some calls-for-service formats the single-digit Priority column
    (values 1-4) is printed with no gap before the Location, so pdfplumber
    emits one fused token per row and the whole location lands in the
    priority column with the location column left empty, e.g.:

        P = "41359 W SIXTH ST"      Location = ""
            ^ priority 4, location "1359 W SIXTH ST"

    When we can see a priority column (short header, its values are a
    single digit 1-4 fused to more text) directly followed by an EMPTY
    location column, we peel the leading digit into priority and move the
    rest into location.

    Strongly guarded so it only fires on this specific shape:
      - a column whose header is a short priority-ish name (P / Pri / Prio
        / Priority), immediately followed by a location/address column;
      - most of the priority column's values look like "<1-4><more>";
      - the following location column is empty on those rows.
    Any of these failing → the table is left untouched.
    """
    if not rows:
        return rows
    cols = list(rows[0].keys())
    data_cols = [c for c in cols if not c.startswith("_")]

    # Find a priority column immediately followed by a location column.
    pri_col = loc_col = None
    for i, c in enumerate(data_cols[:-1]):
        cl = c.strip().lower()
        if cl in ("p", "pri", "prio", "priority", "pty", "prty"):
            nxt = data_cols[i + 1]
            if any(k in nxt.lower() for k in ("location", "address", "street")):
                pri_col, loc_col = c, nxt
                break
    if pri_col is None:
        return rows

    # Confirm the fusion shape on the data before touching anything:
    # most priority values are "<digit 1-4><more text>", and the location
    # column is empty on those rows.
    fused_re = re.compile(r"^([1-4])(\S.*)$")
    n_checked = n_fused = 0
    for r in rows:
        pv = str(r.get(pri_col, "")).strip()
        if not pv:
            continue
        n_checked += 1
        if fused_re.match(pv) and not str(r.get(loc_col, "")).strip():
            n_fused += 1
    if n_checked == 0 or n_fused < n_checked * 0.6:
        return rows  # not this fusion pattern — leave it alone

    # Apply the split only on rows matching the fused shape with empty loc.
    for r in rows:
        pv = str(r.get(pri_col, "")).strip()
        if not pv or str(r.get(loc_col, "")).strip():
            continue
        m = fused_re.match(pv)
        if not m:
            continue
        r[pri_col] = m.group(1)
        r[loc_col] = m.group(2).strip()
    return rows


def _split_merged_datetime_code_column(rows):
    """Split a column whose HEADER merged a datetime column with a trailing
    short-code column (e.g. header "When Reported Typ" holding values like
    "00:01:57 l 09/01/19").

    When two headers are printed with tight, even spacing, header detection
    can merge them into one column — the geometry gives no boundary to split
    on (see notes in _detect_columns).  But the merged column's NAME still
    lists both, and its VALUES follow "<time> <code> <date>".  We detect
    that shape and split it into the datetime column (time + date) and a new
    code column, inserted right after, named from the trailing word(s) of
    the merged header.

    Guarded: fires only when the column name contains a datetime word
    followed by a short trailing word, AND most values match
    "<time> <code> <date>" with a consistent small code vocabulary.
    """
    if not rows:
        return rows
    cols = [c for c in rows[0].keys() if not c.startswith("_")]

    # Find a merged "datetime ... <code-name>" column by its header name.
    target = None
    for c in cols:
        parts = c.split()
        if len(parts) < 2:
            continue
        has_dt = any(k in c.lower() for k in ("when", "date", "time",
                                              "reported", "occurred"))
        # trailing word short & alphabetic (a code header like "Typ")
        if has_dt and parts[-1].isalpha() and len(parts[-1]) <= 4:
            target = c
            break
    if target is None:
        return rows

    pat = re.compile(
        r"^(\d{1,2}:\d{2}(?::\d{2})?)\s+([A-Za-z]{1,3})\s+"
        r"(\d{1,2}/\d{1,2}/\d{2,4})$"
    )
    # Verify most non-empty values fit the time-code-date shape.
    nonempty = [str(r.get(target, "")).strip() for r in rows
                if str(r.get(target, "")).strip()]
    if not nonempty:
        return rows
    matches = [pat.match(v) for v in nonempty]
    if sum(1 for m in matches if m) < len(nonempty) * 0.6:
        return rows

    # Build the two replacement column names.
    name_parts = target.split()
    code_name = name_parts[-1]
    dt_name = " ".join(name_parts[:-1])

    # Rebuild each row's dict, replacing the merged column with two.
    new_rows = []
    for r in rows:
        raw = str(r.get(target, "")).strip()
        m = pat.match(raw)
        if m:
            dt_val = f"{m.group(1)} {m.group(3)}"
            code_val = m.group(2)
        else:
            # leave unmatched values in the datetime column, code empty
            dt_val = raw
            code_val = ""
        new = {}
        for k, v in r.items():
            if k == target:
                new[dt_name] = dt_val
                new[code_name] = code_val
            else:
                new[k] = v
        new_rows.append(new)
    return new_rows


def _extract_code_stranded_in_datetime(rows):
    """Recover a short column value that got captured inside a datetime.

    On some rows a narrow single-token column (e.g. a Typ code "l"/"f"/"lf")
    is rendered between the time and the date and ends up swept into the
    date/time column, producing values like:

        When Reported = "16:16:26 f 09/01/19"   Typ = ""
                                  ^ Typ code stranded between time and date

    When we can see a datetime-style column immediately followed by a short
    code column, and a row shows "<time> <code> <date>" in the datetime
    field with the code column empty, we pull the code back into its column
    and leave a clean "<time> <date>" behind.

    Guarded tightly: fires only when the middle token is one of the code
    column's OWN observed values (learned from the rows where that column
    is populated), so it can't misfire on unrelated text.
    """
    if not rows:
        return rows
    cols = [c for c in rows[0].keys() if not c.startswith("_")]

    # Find a datetime-ish column immediately followed by a short-code column.
    dt_col = code_col = None
    for i, c in enumerate(cols[:-1]):
        cl = c.lower()
        if any(k in cl for k in ("when", "date", "time", "reported",
                                 "occurred", "received")):
            dt_col, code_col = c, cols[i + 1]
            break
    if dt_col is None:
        return rows

    # Learn the code column's vocabulary from rows where it's populated and
    # short (a code, not free text).
    vocab = Counter()
    for r in rows:
        v = str(r.get(code_col, "")).strip()
        if 0 < len(v) <= 3 and v.isalpha():
            vocab[v.lower()] += 1
    if not vocab:
        return rows
    # Any code the column genuinely uses is valid vocabulary.  The
    # "<time> <code> <date>" pattern is already highly specific, so a
    # single confirmed occurrence is enough to trust the code.
    known = set(vocab)

    pat = re.compile(
        r"^(\d{1,2}:\d{2}(?::\d{2})?)\s+([A-Za-z]{1,3})\s+"
        r"(\d{1,2}/\d{1,2}/\d{2,4})$"
    )
    for r in rows:
        if str(r.get(code_col, "")).strip():
            continue  # code column already has a value — don't touch
        m = pat.match(str(r.get(dt_col, "")).strip())
        if not m:
            continue
        if m.group(2).lower() not in known:
            continue
        r[dt_col] = f"{m.group(1)} {m.group(3)}"
        r[code_col] = m.group(2)
    return rows


def _merge_multiline_records(rows, first_col=None):
    """Merge continuation lines into their parent record.

    Some tabular PDFs wrap a single logical record across several
    physical lines: only the first line carries the record's key (an ID
    in the first column), and following lines hold overflow text in the
    other columns with the first column empty.  Example (arrest log):

        2022-902  Mar-17  FIELD RELEASE -  MINOR IN         1618 - BOUCHET
                          CITE / OR        POSSESSION OF    LLOYD
                          (WARRANT)        OPEN CONTAINER

    All three physical lines are one record.  This function detects that
    pattern and merges continuation lines (empty first column) upward
    into the preceding keyed line, appending each field's text.

    Auto-detection: the merge only runs when BOTH
      (a) the first column is normally populated with a consistent
          key-like value (an ID pattern), AND
      (b) a meaningful fraction of rows have an EMPTY first column
          (the continuation lines).
    Single-line formats (Cleveland, Oceanside, Bainbridge) have a
    populated first column on every row, so (b) is false and the rows
    pass through unchanged.
    """
    if not rows:
        return rows

    if first_col is None:
        first_col = list(rows[0].keys())[0]

    # Count rows whose first column is empty vs populated
    total = len(rows)
    empty_first = sum(1 for r in rows if not str(r.get(first_col, "")).strip())

    # If almost no rows have an empty first column, this is a normal
    # single-line table — leave it completely untouched.
    if empty_first < max(3, total * 0.08):
        return rows

    # Confirm the first column looks like a record key on the populated
    # rows (mostly a consistent ID-ish token, not free text).  This
    # guards against accidentally merging a table that merely has some
    # blank cells in its first column.
    populated = [str(r.get(first_col, "")).strip()
                 for r in rows if str(r.get(first_col, "")).strip()]
    if not populated:
        return rows
    # Key-like = alphanumeric with digits, no spaces (e.g. "2022-903",
    # "P170001769", "AR-1234").  Require most populated first-col values
    # to match, so free-text first columns don't trigger merging.
    keyish = sum(1 for v in populated
                 if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_/]*", v)
                 and re.search(r"\d", v))
    if keyish < len(populated) * 0.7:
        return rows

    # Merge: walk rows, starting a new record on each populated-first-col
    # row and appending continuation lines' fields to it.
    merged = []
    current = None
    for r in rows:
        key = str(r.get(first_col, "")).strip()
        if key:
            # New record
            if current is not None:
                merged.append(current)
            current = dict(r)
        else:
            # Continuation line — append each non-empty field to the
            # current record, joined with a space.
            if current is None:
                # Orphan continuation with no parent — keep as its own
                # row rather than dropping data.
                current = dict(r)
                continue
            for col, val in r.items():
                val = str(val).strip()
                if not val:
                    continue
                existing = str(current.get(col, "")).strip()
                current[col] = (existing + " " + val).strip() if existing else val
    if current is not None:
        merged.append(current)

    return merged


# ══════════════════════════════════════════════════════════════════════════
#  VALIDATION / FLAGGING
# ══════════════════════════════════════════════════════════════════════════

def _profile_columns(rows):
    """Learn what 'normal' looks like for each column from the data.

    Returns a dict per column with typical length range and whether the
    column is predominantly numeric, so we can flag outliers without
    hard-coding any department's specific format.
    """
    if not rows:
        return {}

    cols = list(rows[0].keys())
    profile = {}
    for col in cols:
        vals = [str(r.get(col, "")).strip() for r in rows]
        nonempty = [v for v in vals if v]
        if not nonempty:
            profile[col] = {"lengths": [], "numeric_frac": 0.0,
                            "fill_frac": 0.0, "median_len": 0}
            continue

        lengths = sorted(len(v) for v in nonempty)
        numeric = sum(1 for v in nonempty
                      if re.fullmatch(r"[\d\s,./:\-]+", v))

        # Detect "name-like" columns: ones containing proper nouns
        # (people's names), which are legitimately non-dictionary words.
        # Even though many names ARE dictionary words (common surnames /
        # first names), a column of names reliably carries a meaningful
        # share of non-dictionary tokens, whereas a clean prose/code
        # column carries almost none.  So a modest non-word share marks
        # the column as name-like and exempts it from the garble check.
        name_like = False
        if _WORDFREQ is not None:
            sample_toks = []
            for v in nonempty[:80]:
                for t in re.findall(r"[A-Za-z]{3,}", v):
                    sample_toks.append(t)
            if len(sample_toks) >= 10:
                nonword = sum(1 for t in sample_toks
                              if _WORDFREQ(t.lower(), "en") == 0.0)
                if nonword >= len(sample_toks) * 0.08:
                    name_like = True

        # Detect "date-like" columns by CONTENT, not just header name.
        # A column whose values are mostly dates and/or times legitimately
        # holds timestamps, so a date/time appearing in it is NOT a leak.
        # This covers headers like "When Reported", "Occurred", "Received"
        # that don't contain the literal words "date"/"time".
        date_like = False
        date_hits = 0
        for v in nonempty[:100]:
            if re.search(r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}", v) or \
               re.search(r"\b\d{1,2}:\d{2}(:\d{2})?\b", v):
                date_hits += 1
        checked = min(len(nonempty), 100)
        if checked and date_hits >= checked * 0.7:
            date_like = True

        profile[col] = {
            "lengths": lengths,
            "median_len": lengths[len(lengths) // 2],
            "max_len": lengths[-1],
            "numeric_frac": numeric / len(nonempty),
            "fill_frac": len(nonempty) / len(vals),
            "name_like": name_like,
            "date_like": date_like,
        }
    return profile


def flag_rows(rows, profile=None):
    """Return a list of (row_index, [reasons]) for suspicious rows.

    Uses the column profile learned from the data plus a few structural
    checks.  Nothing here is department-specific — thresholds are
    relative to what each column normally contains.

    `profile` may be a precomputed column profile (from _profile_columns
    over the FULL table).  Passing it lets callers validate a small slice
    of rows against the whole-table norms without recomputing the profile
    each call — essential when checking corrected rows one at a time over
    a large file.  When None (default), the profile is computed from
    `rows`, preserving the original behaviour.
    """
    if not rows:
        return []

    if profile is None:
        profile = _profile_columns(rows)
    cols = list(rows[0].keys())
    flags = []

    for idx, row in enumerate(rows):
        reasons = []
        filled = [c for c in cols if str(row.get(c, "")).strip()]

        # 1. Almost-empty row (only 1 field filled in a multi-col table)
        if len(cols) >= 4 and len(filled) <= 1:
            reasons.append("nearly empty row")

        for col in cols:
            val = str(row.get(col, "")).strip()
            if not val:
                continue
            p = profile.get(col, {})
            med = p.get("median_len", 0)
            mx = p.get("max_len", 0)

            # 2. A value far longer than anything normal for this column
            #    (a sign another column's text bled in).
            if med and len(val) > max(mx, med * 3) + 5:
                reasons.append(f"{col!r} unusually long")

            # 3. A predominantly-numeric column holding a value that is
            #    MOSTLY letters (a real leak), or a predominantly-text
            #    column holding a bare number.  We require the value to
            #    be dominated by letters, so legitimate mixed tokens like
            #    "9000-BLK" or "12A" don't trip the flag.
            numfrac = p.get("numeric_frac", 0)
            if numfrac >= 0.9:
                letters = sum(c.isalpha() for c in val)
                digits = sum(c.isdigit() for c in val)
                if letters > digits and letters >= 3:
                    reasons.append(f"{col!r} expected numeric, has text")
            if numfrac <= 0.1 and re.fullmatch(r"\d{1,3}", val) \
                    and p.get("fill_frac", 0) > 0.5:
                reasons.append(f"{col!r} expected text, has number")

        # 4. A date-like column that doesn't look like a date
        for col in cols:
            val = str(row.get(col, "")).strip()
            if "date" in col.lower() and val:
                if not re.search(r"\d{1,2}[/\-]\d{1,2}", val) \
                        and not re.search(r"\d{4}", val):
                    reasons.append(f"{col!r} doesn't look like a date")

        # 5. Interleaving artifact: a token mixing letters and digits
        #    in an unusual way (e.g. "3A/V6E/2022" from overlapping
        #    text).  Allow separators like / : - between the alternating
        #    letters and digits.
        for col in cols:
            val = str(row.get(col, "")).strip()
            for tok in val.split():
                if len(tok) < 5:
                    continue
                # letter and digit alternating with optional separators
                alt = re.search(r"\d[/:.\-]?[A-Za-z][/:.\-]?\d", tok) and \
                      re.search(r"[A-Za-z][/:.\-]?\d[/:.\-]?[A-Za-z]", tok)
                if alt:
                    reasons.append(f"{col!r} possible overlapping text")
                    break

        # 6. Cross-column leak: a non-date column that contains an
        #    embedded date+time pattern (another column's value bled in).
        #    Skip columns that legitimately hold dates/times — either by
        #    header name OR by content (e.g. "When Reported"), since a
        #    timestamp there is expected data, not a leak.
        for col in cols:
            if "date" in col.lower() or "time" in col.lower():
                continue
            if profile.get(col, {}).get("date_like"):
                continue
            val = str(row.get(col, "")).strip()
            if re.search(r"\bat\s+\d{1,2}:\d{2}", val) or \
               re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", val):
                reasons.append(f"{col!r} contains a date/time (leak?)")

        # 7. Garbled / interleaved text: characters from two overlapping
        #    text objects woven together produce word-salad (e.g.
        #    "ORRR CANOTN S- SEUNST PAEGCRTE").  We check tokens against
        #    an English word list — real offense/disposition text is
        #    dictionary words; scrambled text is not.
        for col in cols:
            val = str(row.get(col, "")).strip()

            # Signal A: a symbol wedged between letters with no spaces
            #   where it clearly breaks a word (overlap artifact like
            #   "FIREA*RCMHSA").  We only flag when the symbol sits
            #   inside what would otherwise be one alphabetic run AND the
            #   surrounding letters don't form real words — legitimate
            #   text like "ENTER/REMAIN" or "LAW*CHARGED" has real words
            #   on both sides.
            star_mid = False
            for t in val.split():
                m = re.search(r"([A-Za-z]{2,})[*/]([A-Za-z]{2,})", t)
                if m and _WORDFREQ is not None:
                    left_ok = _WORDFREQ(m.group(1).lower(), "en") > 0
                    right_ok = _WORDFREQ(m.group(2).lower(), "en") > 0
                    # Both sides real words → legitimate (ENTER/REMAIN).
                    # Neither side a word → garbled (FIREA/RCMHSA).
                    if not left_ok and not right_ok:
                        star_mid = True
                        break
            if star_mid:
                reasons.append(f"{col!r} garbled text (embedded symbol)")
                continue

            # Signal B: dictionary check on multi-letter tokens.  Skip
            #   columns that hold proper names (people, places) — those
            #   are legitimately non-dictionary and would false-positive
            #   on every row.
            letter_toks = [re.sub(r"[^A-Za-z]", "", t) for t in val.split()]
            letter_toks = [t for t in letter_toks if len(t) >= 3]
            if (len(letter_toks) >= 3 and _WORDFREQ is not None
                    and not profile.get(col, {}).get("name_like")):
                nonwords = sum(
                    1 for t in letter_toks
                    if _WORDFREQ(t.lower(), "en") == 0.0
                )
                # Most tokens aren't real words → scrambled/garbled
                if nonwords >= len(letter_toks) * 0.6:
                    reasons.append(f"{col!r} garbled/scrambled text")
                    continue

            # Signal C: cluster of tiny non-word fragments (overlap
            #    artifact like "AE DM O/ FTEOLRO")
            # Signal C: cluster of tiny non-word fragments (overlap
            #    artifact like "AE DM O/ FTEOLRO").  To avoid flagging
            #    legitimate abbreviation-heavy text (addresses like
            #    "INTERSTATE 5 NB & STATE ROUTE 76 WB"), require that the
            #    value ALSO contains non-dictionary longer tokens — a
            #    real scramble has both.
            common = {"a", "or", "of", "on", "to", "in", "at", "is",
                      "no", "s", "e", "w", "n", "dr", "do", "so", "wo",
                      "by", "up", "if", "as", "an", "me", "my", "he",
                      "we", "us", "am", "pm", "id", "st", "rd", "av",
                      "nb", "sb", "eb", "wb", "ne", "nw", "se", "sw",
                      "de", "la", "el", "ln", "ct", "pl", "hw", "us"}
            frags = sum(1 for t in val.split()
                        if 0 < len(re.sub(r"[^A-Za-z]", "", t)) <= 2
                        and re.sub(r"[^A-Za-z]", "", t).lower() not in common)
            if frags >= 3 and _WORDFREQ is not None:
                longer = [re.sub(r"[^A-Za-z]", "", t) for t in val.split()]
                longer = [t for t in longer if len(t) >= 4]
                nonword_long = sum(
                    1 for t in longer if _WORDFREQ(t.lower(), "en") == 0.0
                )
                if longer and nonword_long >= len(longer) * 0.5:
                    reasons.append(f"{col!r} fragmented text (overlap?)")
            elif frags >= 3 and _WORDFREQ is None:
                # No dictionary available — fall back to the raw count
                reasons.append(f"{col!r} fragmented text (overlap?)")

        if reasons:
            # De-duplicate while preserving order
            seen = set()
            uniq = [r for r in reasons if not (r in seen or seen.add(r))]
            flags.append((idx, uniq))

    return flags


# ══════════════════════════════════════════════════════════════════════════
#  DRIVER
# ══════════════════════════════════════════════════════════════════════════

def _apply_post_passes(all_rows):
    """Run the deterministic clean-up passes over a batch of parsed rows.

    Shared by the whole-document path (process_pdf) and the chunked path
    (process_pdf_streaming) so both produce identical output.  Each pass is
    a no-op on formats it doesn't apply to.
    """
    all_rows = _merge_multiline_records(all_rows)
    all_rows = _split_merged_datetime_code_column(all_rows)
    all_rows = _split_priority_location_pass(all_rows)
    all_rows = _extract_code_stranded_in_datetime(all_rows)
    all_rows = _deinterleave_pass(all_rows)
    return all_rows


def process_pdf_streaming(pdf_path, out_dir, chunk_size=10000):
    """Memory-bounded processing for very large PDFs.

    Parses the document in page-batches of `chunk_size`, running the post-
    passes and writing to the CSV incrementally, so peak memory stays flat
    regardless of page count.  A small overlap of trailing rows is carried
    between batches so a multi-line record wrapping across a batch boundary
    still merges correctly.

    Only used for documents above the page threshold; smaller ones go
    through process_pdf() unchanged.  Returns (csv_path, total_rows,
    total_flagged).
    """
    import csv as _csv

    print(f"[OPEN] {pdf_path.name} ({pdf_path.stat().st_size / 1e6:.1f} MB)")
    csv_path = out_dir / f"{pdf_path.stem}.csv"
    review_path = out_dir / f"{pdf_path.stem}_REVIEW.csv"
    OVERLAP = 12  # trailing rows carried between chunks for cross-boundary merge

    with pdfplumber.open(str(pdf_path)) as pdf:
        n_pages = len(pdf.pages)
        v_lines, h_lines = _get_table_lines(pdf.pages[0])
        use_rects = v_lines is not None
        print(f"  [Strategy] {'rect-based' if use_rects else 'word-position'}"
              f"  [Streaming] {n_pages} pages in {chunk_size}-page chunks")

        col_info = None
        header_names = None
        csv_f = review_f = None
        writer = review_writer = None
        total_rows = total_flagged = 0
        carry = []           # overlap rows from previous chunk (not yet final)

        for start in range(0, n_pages, chunk_size):
            end = min(start + chunk_size, n_pages)
            batch_rows = []
            for page_idx in range(start, end):
                page = pdf.pages[page_idx]
                try:
                    if use_rects:
                        table = parse_page_rects(page, v_lines, h_lines)
                        if table:
                            if header_names is None:
                                header_names = table[0]
                                data = table[1:]
                            else:
                                data = table
                                if data and data[0] == header_names:
                                    data = data[1:]
                            for row_vals in data:
                                if any(v for v in row_vals if v and v.strip()):
                                    batch_rows.append(
                                        dict(zip(header_names, row_vals)))
                    else:
                        rows, col_info = parse_page_words(page, col_info)
                        batch_rows.extend(rows)
                except Exception as e:
                    print(f"  [ERR] Page {page_idx + 1}: {e}")
                try:
                    page.flush_cache()          # release cached layout
                    page.get_textmap.cache_clear()
                except Exception:
                    pass
                if (page_idx + 1) % 500 == 0:
                    print(f"  ...page {page_idx + 1}/{n_pages}")

            # Run passes on carry + this batch so a record split across the
            # boundary merges.  Then hold back the last OVERLAP rows as the
            # next carry (they might still gain a continuation line).
            processed = _apply_post_passes(carry + batch_rows)
            is_last = end >= n_pages
            if is_last:
                emit, carry = processed, []
            elif len(processed) > OVERLAP:
                emit, carry = processed[:-OVERLAP], processed[-OVERLAP:]
            else:
                emit, carry = [], processed

            if emit:
                data_cols = [c for c in emit[0].keys()
                             if not c.startswith("_")]
                if writer is None:
                    csv_f = open(csv_path, "w", newline="", encoding="utf-8")
                    writer = _csv.DictWriter(
                        csv_f,
                        fieldnames=["_needs_review", "_review_reason"] + data_cols,
                        extrasaction="ignore")
                    writer.writeheader()
                flags = dict(flag_rows(emit))
                for i, row in enumerate(emit):
                    reasons = flags.get(i)
                    out = dict(row)
                    out["_needs_review"] = "YES" if reasons else ""
                    out["_review_reason"] = "; ".join(reasons) if reasons else ""
                    writer.writerow(out)
                    total_rows += 1
                    if reasons:
                        if review_writer is None:
                            review_f = open(review_path, "w", newline="",
                                            encoding="utf-8")
                            review_writer = _csv.DictWriter(
                                review_f,
                                fieldnames=["row_number", "reasons"] + data_cols,
                                extrasaction="ignore")
                            review_writer.writeheader()
                        rr = {"row_number": total_rows, "reasons": out["_review_reason"]}
                        rr.update(row)
                        review_writer.writerow(rr)
                        total_flagged += 1
                csv_f.flush()
            print(f"  ...chunk {start // chunk_size + 1}: pages "
                  f"{start + 1}-{end}, {total_rows} rows so far")

        if csv_f:
            csv_f.close()
        if review_f:
            review_f.close()

    print(f"  [Done] {total_rows} rows (streamed), {total_flagged} flagged")
    return csv_path, total_rows, total_flagged


def process_pdf(pdf_path: Path, max_pages: int = 0):
    """Process a single tabular PDF."""
    print(f"[OPEN] {pdf_path.name} ({pdf_path.stat().st_size / 1e6:.1f} MB)")

    with pdfplumber.open(str(pdf_path)) as pdf:
        if not pdf.pages:
            return []

        # Detect strategy from page 1
        v_lines, h_lines = _get_table_lines(pdf.pages[0])
        use_rects = v_lines is not None

        if use_rects:
            print(f"  [Strategy] rect-based ({len(v_lines)} cols, "
                  f"{len(h_lines) if h_lines else '?'} rows)")
        else:
            print(f"  [Strategy] word-position")

        all_rows = []
        col_info = None  # for word-position mode
        header_names = None
        limit = max_pages if max_pages > 0 else len(pdf.pages)

        for page_idx, page in enumerate(pdf.pages[:limit]):
            try:
                if use_rects:
                    table = parse_page_rects(page, v_lines, h_lines)
                    if table:
                        if header_names is None:
                            header_names = table[0]
                            data = table[1:]
                        else:
                            data = table
                            # Skip repeated header rows
                            if data and data[0] == header_names:
                                data = data[1:]

                        for row_vals in data:
                            if any(v for v in row_vals if v and v.strip()):
                                row = dict(zip(header_names, row_vals))
                                all_rows.append(row)
                else:
                    rows, col_info = parse_page_words(page, col_info)
                    all_rows.extend(rows)

                if (page_idx + 1) % 500 == 0:
                    print(f"  ...page {page_idx + 1}/{len(pdf.pages)}")
            except Exception as e:
                print(f"  [ERR] Page {page_idx + 1}: {e}")
                continue

        if use_rects and header_names:
            print(f"  [Columns] {header_names}")
        elif col_info:
            print(f"  [Columns] {[c[0] for c in col_info[1]]}")

    # Deterministic clean-up passes (each a no-op on formats it doesn't
    # apply to).  Shared with the streaming path so output is identical.
    all_rows = _apply_post_passes(all_rows)

    print(f"  [Done] {len(all_rows)} rows")
    return all_rows


def main():
    ap = argparse.ArgumentParser(
        description="Parse tabular police PDFs into CSVs (no OCR needed).",
        epilog="Examples:\n"
               "  python batch_parse_tabular_pdfs.py Oceanside CA\n"
               "  python batch_parse_tabular_pdfs.py Cleveland OH\n"
               "  python batch_parse_tabular_pdfs.py \"Bainbridge Island\" WA --pages 5\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("city", help="City name (must match Input_<City>_Logs folder)")
    ap.add_argument("state", help="Two-letter state code (e.g. CA, OH, WA)")
    ap.add_argument("--pages", type=int, default=0,
                    help="Process only first N pages per PDF (0 = all)")
    ap.add_argument("--chunk-size", type=int, default=10000,
                    help="Documents with more than this many pages are "
                         "streamed to CSV in page-chunks of this size to "
                         "bound memory (default 10000). Smaller documents "
                         "are unaffected.")
    ap.add_argument("--no-master", action="store_true",
                    help="Skip generating the combined MASTER CSV")
    args = ap.parse_args()

    city = args.city.replace(" ", "_")
    state = args.state.upper()

    in_dir = Path(f"Input_{city}_Logs")
    out_dir = Path(f"results_{city}_{state}")

    if not in_dir.exists():
        print(f"[ERR] Input folder not found: {in_dir}/")
        print(f"      Create it and place your PDFs inside.")
        return

    pdfs = sorted(in_dir.glob("*.pdf"))
    if not pdfs:
        print(f"[ERR] No PDF files found in {in_dir}/")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[Input]  {in_dir}/  ({len(pdfs)} PDFs)")
    print(f"[Output] {out_dir}/\n")

    all_rows = []
    succeeded = []
    failed = []
    total_flagged = 0

    for pdf_path in pdfs:
        # For very large documents, stream in page-chunks to bound memory.
        # Small documents (the vast majority) take the unchanged in-memory
        # path and produce byte-for-byte identical output.
        try:
            with pdfplumber.open(str(pdf_path)) as _probe:
                n_pages = len(_probe.pages)
        except Exception as e:
            print(f"  [ERR] {pdf_path.name}: {e}")
            failed.append(pdf_path.name)
            continue

        if args.pages == 0 and n_pages > args.chunk_size:
            try:
                csv_path, n_rows, n_flagged = process_pdf_streaming(
                    pdf_path, out_dir, chunk_size=args.chunk_size)
            except Exception as e:
                print(f"  [ERR] {pdf_path.name}: {e}")
                failed.append(pdf_path.name)
                continue
            total_flagged += n_flagged
            flag_note = f", {n_flagged} flagged" if n_flagged else ""
            print(f"  [CSV] {csv_path.name} ({n_rows} rows, streamed"
                  f"{flag_note})\n")
            succeeded.append((pdf_path.name, n_rows, n_flagged))
            # Streamed files are too large to fold into an in-memory MASTER;
            # note it and skip.
            if not args.no_master:
                print("  [note] streamed file omitted from MASTER "
                      "(too large to combine in memory)\n")
            continue

        try:
            rows = process_pdf(pdf_path, max_pages=args.pages)
        except Exception as e:
            print(f"  [ERR] {pdf_path.name}: {e}")
            failed.append(pdf_path.name)
            continue

        if rows:
            # Flag suspicious rows for manual review
            flags = flag_rows(rows)
            flag_map = {idx: reasons for idx, reasons in flags}

            df = pd.DataFrame(rows)
            # Add review columns so flagged rows are easy to filter in
            # any spreadsheet tool.
            df.insert(0, "_needs_review",
                      ["YES" if i in flag_map else "" for i in range(len(rows))])
            df.insert(1, "_review_reason",
                      ["; ".join(flag_map.get(i, [])) for i in range(len(rows))])

            csv_name = f"{pdf_path.stem}.csv"
            df.to_csv(out_dir / csv_name, index=False)

            n_flagged = len(flags)
            total_flagged += n_flagged
            flag_note = f", {n_flagged} flagged" if n_flagged else ""
            print(f"  [CSV] {csv_name} ({len(rows)} rows, "
                  f"{len(df.columns) - 2} cols{flag_note})")

            # Write a separate review file with only the flagged rows,
            # including their original row number for easy lookup.
            if flags:
                review_rows = []
                for idx, reasons in flags:
                    rr = {"row_number": idx + 1,
                          "reasons": "; ".join(reasons)}
                    rr.update(rows[idx])
                    review_rows.append(rr)
                review_name = f"{pdf_path.stem}_REVIEW.csv"
                pd.DataFrame(review_rows).to_csv(
                    out_dir / review_name, index=False)
                print(f"  [REVIEW] {review_name} — "
                      f"{n_flagged} rows to check manually")
            print()

            succeeded.append((pdf_path.name, len(rows), n_flagged))
            if not args.no_master:
                all_rows.extend(rows)

    print(f"{'=' * 60}")
    print(f"Finished: {len(succeeded)} files, "
          f"{sum(n for _, n, _ in succeeded)} total rows, "
          f"{total_flagged} flagged for review")
    for name, n, nf in succeeded:
        note = f"  ({nf} flagged)" if nf else ""
        print(f"  ✓ {name} → {n} rows{note}")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for name in failed:
            print(f"  ✗ {name}")

    if total_flagged:
        print(f"\n⚠  {total_flagged} rows flagged for manual review — "
              f"see *_REVIEW.csv files, or filter _needs_review=YES in "
              f"the main CSVs.")

    if all_rows and not args.no_master:
        df_all = pd.DataFrame(all_rows)
        master_name = f"MASTER_{city}_{state}.csv"
        df_all.to_csv(out_dir / master_name, index=False)
        print(f"\nMASTER: {out_dir / master_name} ({len(df_all)} rows)")


if __name__ == "__main__":
    main()