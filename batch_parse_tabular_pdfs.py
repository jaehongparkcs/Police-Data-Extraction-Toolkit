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
from collections import defaultdict
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
    else:
        header_y, columns = col_info
        # Check for repeated header on this page
        y_groups = defaultdict(list)
        for w in words:
            y_groups[round(w["top"])].append(w)
        first_word = columns[0][0].split()[0]
        last_word = columns[-1][0].split()[-1]
        for y in sorted(y_groups.keys())[:5]:
            texts = {w["text"] for w in y_groups[y]}
            if first_word in texts and last_word in texts:
                header_y = y
                new_cols = _detect_columns(words, header_y)
                if new_cols:
                    columns = new_cols
                    col_info = (header_y, columns)
                break

    # Build column ranges: each column starts at its header's x_start
    # and ends where the next column's header starts.
    col_ranges = []
    for i, (name, x_start, x_end) in enumerate(columns):
        x_left = x_start - 3  # small left buffer
        if i + 1 < len(columns):
            x_right = columns[i + 1][1] - 1  # just before next header
        else:
            x_right = page.width + 50
        col_ranges.append((name, x_left, x_right))

    # Extract data
    data_words = [w for w in words if w["top"] > header_y + 3]
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
        if c.get("text") == " " and c["top"] > header_y + 3:
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

            if has_space and gap < 40:
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

        # Post-fix for wrapped offense text that landed in the last
        # column: if the last column (typically PD District) holds a
        # lone digit followed by alphabetic words (e.g. "4 PREMISES"),
        # the digit is the real value and the words are wrapped text
        # from the widest text column — move them back there.
        if len(col_ranges) >= 2:
            last_name = col_ranges[-1][0]
            last_val = final.get(last_name, "")
            m = re.match(r"^(\d)\s+([A-Za-z].*)$", last_val)
            if m:
                final[last_name] = m.group(1)
                # Append wrapped words to the longest text column (the
                # one most likely to wrap — usually the description).
                text_cols = sorted(
                    col_ranges[:-1],
                    key=lambda c: len(final.get(c[0], "")),
                    reverse=True,
                )
                if text_cols:
                    tgt = text_cols[0][0]
                    final[tgt] = (final[tgt] + " " + m.group(2)).strip()
            elif (last_val and not any(ch.isdigit() for ch in last_val)
                  and len(col_ranges) >= 3):
                # The last column (PD District) holds only text — that's
                # a disposition code (e.g. "CMP") that landed in the
                # wrong column because PD is empty.  Move it to the
                # second-to-last column (Case Disposition) if empty.
                disp_name = col_ranges[-2][0]
                if not final.get(disp_name, "").strip():
                    final[disp_name] = last_val
                    final[last_name] = ""

        values = [v for v in final.values() if v.strip()]
        if values and not any(re.match(r"^Page\s+\d+", v) for v in values):
            rows.append(final)

    return rows, col_info


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
        profile[col] = {
            "lengths": lengths,
            "median_len": lengths[len(lengths) // 2],
            "max_len": lengths[-1],
            "numeric_frac": numeric / len(nonempty),
            "fill_frac": len(nonempty) / len(vals),
        }
    return profile


def flag_rows(rows):
    """Return a list of (row_index, [reasons]) for suspicious rows.

    Uses the column profile learned from the data plus a few structural
    checks.  Nothing here is department-specific — thresholds are
    relative to what each column normally contains.
    """
    if not rows:
        return []

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
        for col in cols:
            if "date" in col.lower() or "time" in col.lower():
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

            # Signal B: dictionary check on multi-letter tokens
            letter_toks = [re.sub(r"[^A-Za-z]", "", t) for t in val.split()]
            letter_toks = [t for t in letter_toks if len(t) >= 3]
            if len(letter_toks) >= 3 and _WORDFREQ is not None:
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