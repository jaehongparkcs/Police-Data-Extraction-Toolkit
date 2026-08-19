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

    rows = []
    for y in sorted(row_ys.keys()):
        ws = row_ys[y]
        row = {}
        for col_name, cx_start, cx_end in col_ranges:
            col_words = sorted(
                [w for w in ws if w["x0"] >= cx_start and w["x0"] < cx_end],
                key=lambda w: w["x0"],
            )
            row[col_name] = " ".join(w["text"] for w in col_words)

        values = [v for v in row.values() if v.strip()]
        if values and not any(re.match(r"^Page\s+\d+", v) for v in values):
            rows.append(row)

    return rows, col_info


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
        description="Parse tabular police PDFs into CSVs (no OCR needed)."
    )
    ap.add_argument("input_dir", help="Directory containing PDF files")
    ap.add_argument("--out", default=".", help="Output directory for CSVs")
    ap.add_argument("--pages", type=int, default=0,
                    help="Process only first N pages per PDF (0 = all)")
    ap.add_argument("--no-master", action="store_true",
                    help="Skip generating the combined MASTER CSV")
    args = ap.parse_args()

    in_dir = Path(args.input_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    succeeded = []
    failed = []

    for pdf_path in sorted(in_dir.glob("*.pdf")):
        try:
            rows = process_pdf(pdf_path, max_pages=args.pages)
        except Exception as e:
            print(f"  [ERR] {pdf_path.name}: {e}")
            failed.append(pdf_path.name)
            continue

        if rows:
            df = pd.DataFrame(rows)
            csv_name = f"{pdf_path.stem}.csv"
            df.to_csv(out_dir / csv_name, index=False)
            print(f"  [CSV] {csv_name} ({len(rows)} rows, "
                  f"{len(df.columns)} cols)\n")
            succeeded.append((pdf_path.name, len(rows)))
            if not args.no_master:
                all_rows.extend(rows)

    print(f"{'=' * 60}")
    print(f"Finished: {len(succeeded)} files, "
          f"{sum(n for _, n in succeeded)} total rows")
    for name, n in succeeded:
        print(f"  ✓ {name} → {n} rows")
    if failed:
        print(f"\nFailed ({len(failed)}):")
        for name in failed:
            print(f"  ✗ {name}")

    if all_rows and not args.no_master:
        df_all = pd.DataFrame(all_rows)
        df_all.to_csv(out_dir / "MASTER_combined.csv", index=False)
        print(f"\nMASTER: {out_dir / 'MASTER_combined.csv'} "
              f"({len(df_all)} rows)")


if __name__ == "__main__":
    main()
