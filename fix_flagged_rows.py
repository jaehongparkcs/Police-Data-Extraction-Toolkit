#!/usr/bin/env python3
"""
fix_flagged_rows.py
═══════════════════════════════════════════════════════════════════════════

Deterministic correction pass for an already-parsed CSV.  Runs the shared
de-interleave repairs (see deinterleave.py) over the rows the parser flagged,
recovers what can be recovered VERIFIABLY, and clears each row's review flag
only for the field(s) actually fixed.  Nothing garbled is ever written.

This is the "fix what we already have" tool.  For future PDFs, the same
repairs run inside batch_parse_tabular_pdfs.py so fresh output comes out
clean in one pass — you don't need this script for those.

WHY DETERMINISTIC, NOT LLM
    The dominant defect (address/date pixel collision) is mechanical and
    repeats thousands of times.  It reverses deterministically with a strict
    validity gate, so it's fast, correct, and safe — unlike an LLM, which on
    this data produced silent date corruption.  Use llm_fix_flagged_rows.py
    only for the irregular residue this pass can't handle.

USAGE
    python fix_flagged_rows.py --csv results_Cleveland_OH/<file>.csv

    Writes <file>_fixed.csv.  Fixed rows are tagged in a `_corrected_by`
    column (e.g. "deterministic:address-date") for auditing.  Rows it can't
    fully fix keep their remaining review flags for a human or the LLM pass.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from deinterleave import fix_row
from batch_parse_tabular_pdfs import flag_rows, _profile_columns

REVIEW_COLS = ("_needs_review", "_review_reason")


def correct_csv(csv_path, addr_col="Address", date_col="Date & Time"):
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    data_cols = [c for c in df.columns
                 if c not in REVIEW_COLS and c != "_corrected_by"]
    if "_corrected_by" not in df.columns:
        df["_corrected_by"] = ""

    # Precompute the full-table column profile once, so each repaired row is
    # re-validated against whole-table norms in O(1).
    all_rows = [{c: df.iloc[j][c] for c in data_cols} for j in range(len(df))]
    profile = _profile_columns(all_rows)

    flagged_idx = [
        i for i in range(len(df))
        if str(df.iloc[i].get("_needs_review", "")).strip().upper() == "YES"
    ]
    if not flagged_idx:
        print("No flagged rows.")
        return None

    print(f"{len(flagged_idx)} flagged rows; attempting deterministic "
          f"de-interleave repairs...")

    date_fixes = addr_fixes = cleared = 0
    have_addr = addr_col in df.columns
    have_date = date_col in df.columns

    for i in flagged_idx:
        row = {c: df.iloc[i][c] for c in data_cols}
        fx = fix_row(row, addr_col=addr_col, date_col=date_col)
        changed = False

        if fx["date"] and have_date:
            df.at[i, date_col] = fx["date"]
            date_fixes += 1
            changed = True
        if fx["address"] and have_addr:
            df.at[i, addr_col] = fx["address"]
            addr_fixes += 1
            changed = True

        if not changed:
            continue

        # Re-validate the repaired row against whole-table norms.  If it now
        # passes cleanly, clear its review flag; otherwise keep it flagged
        # (we improved it but it still has a residual issue).
        repaired = {c: df.iloc[i][c] for c in data_cols}
        row_flags = flag_rows([repaired], profile=profile)
        prior = str(df.at[i, "_corrected_by"]).strip()
        tag = "deterministic:address-date"
        df.at[i, "_corrected_by"] = f"{prior};{tag}".strip(";") if prior else tag
        if not row_flags:
            df.at[i, "_needs_review"] = ""
            df.at[i, "_review_reason"] = ""
            cleared += 1
        else:
            # Keep it flagged, but update the reason to what REMAINS.
            df.at[i, "_review_reason"] = "; ".join(row_flags[0][1])

    out_path = csv_path.with_name(csv_path.stem + "_fixed.csv")
    df.to_csv(out_path, index=False)

    still = int((df["_needs_review"] == "YES").sum())
    print(f"\nDeterministic pass complete:")
    print(f"  dates recovered:     {date_fixes}")
    print(f"  addresses recovered: {addr_fixes}")
    print(f"  rows fully cleared:  {cleared}")
    print(f"  still flagged:       {still} "
          f"(down from {len(flagged_idx)})")
    print(f"\nWrote {out_path}")
    print("Run llm_fix_flagged_rows.py on this output to attempt the "
          "irregular residue.")
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--csv", required=True, type=Path,
                    help="The <file>.csv produced by the parser.")
    ap.add_argument("--address-col", default="Address",
                    help="Name of the address column (default: Address).")
    ap.add_argument("--date-col", default="Date & Time",
                    help="Name of the date column (default: 'Date & Time').")
    args = ap.parse_args()
    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")
    correct_csv(args.csv, addr_col=args.address_col, date_col=args.date_col)


if __name__ == "__main__":
    main()
