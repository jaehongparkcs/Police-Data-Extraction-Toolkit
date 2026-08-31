#!/usr/bin/env python3
"""
batch_parse_html.py
═══════════════════════════════════════════════════════════════════════════

Parse tabular HTML report exports (Incidents, Crimes, etc.) into the same
CSV format the PDF parsers produce, including the shared review-flagging.

Unlike the PDF path, HTML tables carry explicit cell boundaries (<td>), so
there is no column-collision, de-interleave, or clustering to do — the hard
problems of the scanned/text-layer PDFs simply don't exist here.  The work
is: locate the real header row (these exports stack a title row or two on
top, and a print-timestamp footer at the bottom), take the data rows
between, and run them through the same flagger so the output is consistent
with the rest of the toolkit.

USAGE
    python batch_parse_html.py <City> <State>

    Reads Input_<City>_HTML/, writes results_<City>_<State>_HTML/ with one
    <file>.csv, <file>_REVIEW.csv per input, and a combined
    MASTER_<City>_<State>.csv.  HTML files already inside a results_*
    directory are ignored, so re-runs are safe.

    These exports are often UTF-16; the encoding is auto-detected.
"""

import sys
from pathlib import Path

import pandas as pd

try:
    from batch_parse_tabular_pdfs import flag_rows
except Exception:  # flagger optional — parser still works without it
    flag_rows = None

REVIEW_COLS = ("_needs_review", "_review_reason")


def _detect_encoding(path):
    """These reports are commonly UTF-16 (little-endian).  Sniff the BOM /
    first bytes, fall back to utf-8."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    # UTF-16LE ASCII text shows as 'X\x00Y\x00...'
    if len(head) >= 4 and head[1] == 0 and head[3] == 0:
        return "utf-16"
    return "utf-8"


def _row_all_same(vals):
    """True if every cell in the row holds the same text (title/footer)."""
    seen = [str(v).strip() for v in vals if str(v).strip()
            and str(v).strip().lower() != "nan"]
    return len(seen) > 1 and len(set(seen)) == 1


def _row_blank(vals):
    return all(str(v).strip().lower() in ("", "nan") for v in vals)


def _find_header_row(df):
    """The header row is the first non-title, non-blank row whose cells are
    all distinct and non-empty — the column names.  Title rows repeat one
    label across every cell; the header row does not."""
    for i in range(min(len(df), 15)):
        vals = df.iloc[i].tolist()
        if _row_blank(vals) or _row_all_same(vals):
            continue
        cells = [str(v).strip() for v in vals]
        if all(c and c.lower() != "nan" for c in cells) and \
                len(set(cells)) == len(cells):
            return i
    return None


def parse_html(path):
    """Return a cleaned DataFrame (named columns, data rows only) or None."""
    enc = _detect_encoding(path)
    try:
        tables = pd.read_html(path, encoding=enc)
    except Exception as e:
        print(f"  [ERR] {path.name}: could not read tables ({e})")
        return None
    if not tables:
        print(f"  [ERR] {path.name}: no tables found")
        return None
    # The report table is the largest one.
    raw = max(tables, key=lambda t: t.shape[0] * t.shape[1])

    hdr = _find_header_row(raw)
    if hdr is None:
        print(f"  [ERR] {path.name}: couldn't locate a header row")
        return None
    columns = [str(v).strip() for v in raw.iloc[hdr].tolist()]

    # Data rows are those after the header, minus trailing title/footer/blank.
    body = raw.iloc[hdr + 1:].reset_index(drop=True)
    keep = []
    for i in range(len(body)):
        vals = body.iloc[i].tolist()
        if _row_blank(vals) or _row_all_same(vals):
            continue  # blank separators, or the print-timestamp footer
        keep.append(i)
    body = body.iloc[keep].reset_index(drop=True)
    body.columns = columns
    # Normalise NaN to empty string for consistent CSV output.
    body = body.fillna("").astype(str)
    body = body.replace("nan", "", regex=False)
    return body


def _apply_flags(df):
    """Run the shared flagger and prepend the review columns."""
    rows = df.to_dict("records")
    review = [""] * len(rows)
    reason = [""] * len(rows)
    if flag_rows is not None and rows:
        for idx, reasons in flag_rows(rows):
            review[idx] = "YES"
            reason[idx] = "; ".join(reasons)
    out = df.copy()
    out.insert(0, "_review_reason", reason)
    out.insert(0, "_needs_review", review)
    return out


def process_file(path, out_dir):
    print(f"[OPEN] {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
    df = parse_html(path)
    if df is None or df.empty:
        print("  [skip] no data\n")
        return None
    print(f"  [Columns] {list(df.columns)}")
    df = _apply_flags(df)
    n_flag = int((df["_needs_review"] == "YES").sum())
    print(f"  [Done] {len(df)} rows, {n_flag} flagged")

    out_csv = out_dir / f"{path.stem}.csv"
    df.to_csv(out_csv, index=False)
    review = df[df["_needs_review"] == "YES"]
    if not review.empty:
        review.to_csv(out_dir / f"{path.stem}_REVIEW.csv", index=False)
    print(f"  [Wrote] {out_csv.name}\n")
    return df


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: python batch_parse_html.py <City> <State>")
    city, state = sys.argv[1], sys.argv[2]
    in_dir = Path(f"Input_{city}_Logs")
    out_dir = Path(f"results_{city}_{state}")
    if not in_dir.is_dir():
        sys.exit(f"Input folder not found: {in_dir}")
    out_dir.mkdir(exist_ok=True)

    html_files = sorted(p for p in in_dir.glob("*.htm*"))
    print(f"[Input]  {in_dir}/  ({len(html_files)} HTML files)")
    print(f"[Output] {out_dir}/\n")

    all_frames = []
    total_rows = total_flags = 0
    for path in html_files:
        df = process_file(path, out_dir)
        if df is not None:
            all_frames.append(df)
            total_rows += len(df)
            total_flags += int((df["_needs_review"] == "YES").sum())

    if all_frames:
        master = pd.concat(all_frames, ignore_index=True)
        master_path = out_dir / f"MASTER_{city}_{state}.csv"
        master.to_csv(master_path, index=False)
        print(f"[Master] {master_path.name} ({len(master)} rows)")

    print(f"\nFinished: {len(all_frames)} files, {total_rows} total rows, "
          f"{total_flags} flagged for review")


if __name__ == "__main__":
    main()
