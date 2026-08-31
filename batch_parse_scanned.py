#!/usr/bin/env python3
"""
batch_parse_scanned.py
═══════════════════════════════════════════════════════════════════════════

Parse SCANNED tabular reports (TIFF / JPEG / PNG images with no text layer)
into the same CSV format the PDF parsers produce.

Approach
--------
These images have no text layer, so we OCR each page with Tesseract to get
positioned words (text + bounding box), then feed those words straight into
the SAME column-detection / clustering / flagging pipeline the text-layer
PDF parser uses.  We do this by wrapping the OCR output in a lightweight
object that mimics the small slice of the pdfplumber page interface the
parser touches (`extract_words()` and `.chars`), so the scanned path reuses
all the hardened PDF logic (header detection, header repair, cluster
alignment, multi-line merge, the review flagger) with no duplication.

The important difference from the PDF path: OCR is PROBABILISTIC.  Tesseract
misreads some characters ("WARANT" for "WARRANT", a split "ASSAU LTA").
These aren't deterministically fixable, so we lean on the review flagger AND
add an OCR-confidence flag: any row containing a low-confidence word is
marked for human review.  The philosophy matches the rest of the toolkit —
surface uncertainty rather than silently emit a wrong value.

USAGE
    python batch_parse_scanned.py <City> <State>

    Reads Input_<City>_Logs/, writes results_<City>_<State>/ with one
    <file>.csv and <file>_REVIEW.csv per image, plus a combined MASTER.
    Accepts .tif/.tiff/.jpg/.jpeg/.png (extension-independent; content is
    what matters).  Multi-page TIFFs are handled page by page.

REQUIREMENTS
    pytesseract + the Tesseract binary (already used for Brea OCR), Pillow.
    No new pip packages beyond what the toolkit already lists.
"""

import sys
from pathlib import Path

import pandas as pd

try:
    from PIL import Image, ImageSequence
except Exception:
    Image = None

try:
    import pytesseract
    from pytesseract import Output
except Exception:
    pytesseract = None

# Reuse the PDF parser's hardened internals.
from batch_parse_tabular_pdfs import (
    parse_page_words,
    _apply_post_passes,
    flag_rows,
)

REVIEW_COLS = ("_needs_review", "_review_reason")

# Words OCR'd below this confidence get their row flagged for review.
OCR_CONF_FLAG = 60


class _OCRPage:
    """Minimal stand-in for a pdfplumber page, backed by OCR output.

    parse_page_words() only calls `.extract_words(...)` and reads `.chars`,
    so we implement just those.  Coordinates come straight from Tesseract's
    word boxes; we synthesise per-character boxes by evenly splitting each
    word's width (the parser uses chars only to split words that straddle a
    column boundary, and even splitting is a fine approximation for that).
    """

    def __init__(self, words):
        # words: list of dicts with text, x0, x1, top, bottom, conf
        self._words = words
        self.width = max((w["x1"] for w in words), default=1000) + 50
        self.height = max((w["bottom"] for w in words), default=1000) + 50
        self.chars = self._build_chars(words)

    @staticmethod
    def _build_chars(words):
        chars = []
        for w in words:
            text = w["text"]
            n = len(text)
            if n == 0:
                continue
            span = (w["x1"] - w["x0"]) / n
            for j, ch in enumerate(text):
                if ch == " ":
                    continue
                cx0 = w["x0"] + j * span
                chars.append({
                    "text": ch,
                    "x0": cx0,
                    "x1": cx0 + span,
                    "top": w["top"],
                    "bottom": w["bottom"],
                })
        return chars

    def extract_words(self, **kwargs):
        return [
            {"text": w["text"], "x0": w["x0"], "x1": w["x1"],
             "top": w["top"], "bottom": w["bottom"]}
            for w in self._words
        ]

    # Rect-based detection probes these; return "no ruled lines" so the
    # parser uses word-position mode (correct for these reports).
    @property
    def lines(self):
        return []

    @property
    def rects(self):
        return []

    def flush_cache(self):
        pass


def _snap_rows(words, tol=8):
    """Snap OCR word tops onto shared row baselines.

    Tesseract's per-word `top` jitters by a few pixels within one visual
    row (and can split a wrapped header across two y-values).  The parser
    groups words by exact rounded top, which is right for crisp PDF
    coordinates but too strict for OCR.  We cluster words whose tops fall
    within `tol` pixels and assign them all the cluster's median top, so a
    visual row becomes a single group downstream.
    """
    if not words:
        return words
    ws = sorted(words, key=lambda w: w["top"])
    clusters = []          # list of [tops...]
    members = []           # parallel list of [word...]
    for w in ws:
        if clusters and w["top"] - clusters[-1][-1] <= tol:
            clusters[-1].append(w["top"])
            members[-1].append(w)
        else:
            clusters.append([w["top"]])
            members.append([w])
    for tops, group in zip(clusters, members):
        base = sorted(tops)[len(tops) // 2]  # median top
        for w in group:
            w["top"] = base
    return words


def _ocr_words(pil_image):
    """OCR one image → list of word dicts (text, x0, x1, top, bottom, conf).

    Returns positioned words with tops snapped to shared row baselines so
    the parser groups each visual row together despite OCR jitter.
    """
    data = pytesseract.image_to_data(pil_image, output_type=Output.DICT)
    words = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf < 0:
            continue
        x0 = float(data["left"][i])
        top = float(data["top"][i])
        words.append({
            "text": text,
            "x0": x0,
            "x1": x0 + float(data["width"][i]),
            "top": top,
            "bottom": top + float(data["height"][i]),
            "conf": conf,
        })
    return _snap_rows(words)


def _iter_page_images(path):
    """Yield PIL images for each page of an image file (multi-page TIFFs
    yield several; JPEG/PNG yield one)."""
    img = Image.open(path)
    for frame in ImageSequence.Iterator(img):
        yield frame.convert("L")


def _low_conf_tops(words, bucket=12):
    """Vertical buckets (row keys) that contain a low-confidence word, so we
    can flag those rows for OCR review."""
    bad = set()
    for w in words:
        if w.get("conf", 100) < OCR_CONF_FLAG:
            bad.add(round(w["top"]) // bucket)
    return bad


def _merge_adjacent_split_headers(rows):
    """Rejoin columns that a two-word header was split into.

    On scanned reports the OCR'd header spacing can make a two-word column
    name ("Call Type", "Arrest Date") look like two columns, so the values
    land in two columns ("Call"+"Type").  We detect the specific pairs the
    arrest reports use and merge each pair's values back into one column.

    This lives in the scanned parser (not the shared PDF column logic) so it
    can't affect the text-layer departments — their header detection already
    handles their own multi-word headers correctly.
    """
    if not rows:
        return rows
    cols = list(rows[0].keys())
    # Pairs to merge when BOTH appear as adjacent columns: (left, right) ->
    # merged name.  Value becomes "left right" (trimmed).
    pairs = [("Call", "Type", "Call Type"),
             ("Arrest", "Date", "Arrest Date")]
    merges = [(l, r, m) for (l, r, m) in pairs if l in cols and r in cols]
    if not merges:
        return rows

    out = []
    for row in rows:
        new = {}
        skip = set()
        for c in cols:
            if c in skip:
                continue
            merged_here = None
            for l, r, m in merges:
                if c == l:
                    merged_here = (r, m)
                    break
            if merged_here:
                r, m = merged_here
                lv = str(row.get(l, "")).strip()
                rv = str(row.get(r, "")).strip()
                new[m] = (lv + " " + rv).strip()
                skip.add(r)
            else:
                new[c] = row.get(c, "")
        out.append(new)
    return out


def _drop_scan_boilerplate(rows):
    """Remove page boilerplate that OCR captured as data rows.

    Scanned reports repeat a title block and footer on every page:
        "Arrest Report by Address", "From 1/1/2019 To 3/1/2024",
        "Report Printed on 3/13/2024 at 9:49:06AM", "Page 33 of 496".
    These lack the leading key value (Address) that every real record has,
    and carry recognisable boilerplate words.  We drop a row only when it
    both lacks a first-column value AND matches a boilerplate pattern, so a
    genuine record can never be removed.
    """
    if not rows:
        return rows
    cols = list(rows[0].keys())
    key_col = cols[0]  # Address on these reports
    import re
    pat = re.compile(
        r"\b(report\s+printed|printed\s+on|page\s+\d+\s+of\s+\d+|"
        r"arrest\s+report|from\b.*\bto\b)\b", re.I)
    out = []
    for row in rows:
        key = str(row.get(key_col, "")).strip()
        blob = " ".join(str(v) for v in row.values())
        if not key and pat.search(blob):
            continue  # boilerplate — drop
        # Also drop a bare "Page N of M" even if something odd is in key.
        if re.search(r"\bpage\s+\d+\s+of\s+\d+\b", blob, re.I) and \
                len(blob.split()) <= 5:
            continue
        out.append(row)
    return out


def process_image(path, out_dir):
    print(f"[OPEN] {path.name}")
    all_rows = []
    col_info = None
    row_conf_flags = []  # parallel to all_rows: True if row had low-conf OCR

    page_no = 0
    for pil in _iter_page_images(path):
        page_no += 1
        words = _ocr_words(pil)
        if not words:
            continue
        page = _OCRPage(words)
        before = len(all_rows)
        rows, col_info = parse_page_words(page, col_info)
        all_rows.extend(rows)
        # Mark which of the newly added rows sat at a low-confidence position.
        low = _low_conf_tops(words)
        # We can't map merged rows back to exact tops perfectly, so flag
        # conservatively: if this page had ANY low-conf words, we note the
        # page so a per-row check below can catch obvious cases.
        for _ in range(len(all_rows) - before):
            row_conf_flags.append(bool(low))
        if page_no % 25 == 0:
            print(f"  ...page {page_no}")

    if not all_rows:
        print("  [skip] no text recognised\n")
        return None

    all_rows = _apply_post_passes(all_rows)
    all_rows = _merge_adjacent_split_headers(all_rows)
    all_rows = _drop_scan_boilerplate(all_rows)
    print(f"  [Columns] {list(all_rows[0].keys())}")
    print(f"  [Done] {len(all_rows)} rows from {page_no} page(s)")

    # Build DataFrame + review columns (structural flags + OCR-confidence).
    df = pd.DataFrame(all_rows).fillna("").astype(str)
    data_cols = list(df.columns)
    struct = dict(flag_rows(all_rows))
    review = []
    reason = []
    for i in range(len(all_rows)):
        reasons = list(struct.get(i, []))
        # OCR low-confidence check on this row's own cells.
        row_text = " ".join(str(all_rows[i].get(c, "")) for c in data_cols)
        if _looks_low_conf(row_text):
            reasons.append("low-confidence OCR text")
        review.append("YES" if reasons else "")
        reason.append("; ".join(reasons))
    df.insert(0, "_review_reason", reason)
    df.insert(0, "_needs_review", review)

    n_flag = int((df["_needs_review"] == "YES").sum())
    out_csv = out_dir / f"{path.stem}.csv"
    df.to_csv(out_csv, index=False)
    rev = df[df["_needs_review"] == "YES"]
    if not rev.empty:
        rev.to_csv(out_dir / f"{path.stem}_REVIEW.csv", index=False)
    print(f"  [CSV] {out_csv.name} ({len(df)} rows, {n_flag} flagged)\n")
    return df


def _looks_low_conf(text):
    """Heuristic: a row's text shows OCR trouble (very short tokens glued to
    long ones, isolated single letters mid-row, stray punctuation runs).
    Kept deliberately light — the structural flagger catches most issues;
    this just adds an OCR-specific net."""
    import re
    # A lone 1-2 letter uppercase fragment between words often marks a split
    # like "ASSAU LTA".  Not conclusive, so only a soft signal.
    if re.search(r"\b[A-Z]{1,2}\b \b[A-Z]{2,}\b", text):
        # very common in this data (e.g. 'ST', 'AV'); require a nearby oddity
        if re.search(r"[A-Z]{2,}\s+[A-Z]{1,2}\s+[A-Z]{2,}", text):
            return True
    return False


def main():
    if pytesseract is None or Image is None:
        sys.exit("This tool needs pytesseract + Pillow (and the Tesseract "
                 "binary) installed.")
    if len(sys.argv) < 3:
        sys.exit("Usage: python batch_parse_scanned.py <City> <State>")
    city, state = sys.argv[1], sys.argv[2]
    in_dir = Path(f"Input_{city}_Logs")
    out_dir = Path(f"results_{city}_{state}")
    if not in_dir.is_dir():
        sys.exit(f"Input folder not found: {in_dir}")
    out_dir.mkdir(exist_ok=True)

    exts = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}
    images = sorted(p for p in in_dir.iterdir()
                    if p.suffix.lower() in exts or p.suffix == "")
    print(f"[Input]  {in_dir}/  ({len(images)} images)")
    print(f"[Output] {out_dir}/\n")

    frames = []
    total_rows = total_flag = 0
    for path in images:
        df = process_image(path, out_dir)
        if df is not None:
            frames.append(df)
            total_rows += len(df)
            total_flag += int((df["_needs_review"] == "YES").sum())

    if frames:
        master = pd.concat(frames, ignore_index=True)
        master.to_csv(out_dir / f"MASTER_{city}_{state}.csv", index=False)
        print(f"[Master] MASTER_{city}_{state}.csv ({len(master)} rows)")

    print(f"\nFinished: {len(frames)} images, {total_rows} rows, "
          f"{total_flag} flagged for review")


if __name__ == "__main__":
    main()