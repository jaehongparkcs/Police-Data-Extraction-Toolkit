#!/usr/bin/env python3
"""
llm_fix_flagged_rows.py
═══════════════════════════════════════════════════════════════════════════

Post-processing pass that uses a lightweight LLM to correct the rows that
`batch_parse_tabular_pdfs.py` flagged for review.

WHY A SECOND PASS
    The deterministic parser is right on the overwhelming majority of rows
    and flags the handful it is unsure about (misplaced/wrapped/garbled
    text).  This tool re-reads the ORIGINAL PDF region for each flagged row
    and asks an LLM to reconstruct the correct field values, then re-runs
    the parser's own validator on the result.  Only outputs that pass
    validation are accepted; everything else stays flagged for a human.

WHY IT NEEDS THE PDF (not just the CSV)
    The CSV holds the already-corrupted output — for interleaved text like
    "3A/V6E/2022" the original "AVE" + "3/6/2022" is fused and unrecoverable
    from the CSV alone.  The PDF's text layer still has the individual words
    and their positions, which is the signal the LLM needs.  So we hand the
    model the RAW positioned words for that row's region plus the known
    column schema — it fills known slots rather than inventing structure.

DESIGN
    - One LLM call per flagged row, ~200-400 tokens of context.  A small
      local model (7-8B) is sufficient for the misplaced/intact cases.
    - Strict JSON output constrained to the known column names.
    - Every correction is re-validated with flag_rows(); failures are
      rejected and the original (flagged) row is kept.
    - Corrected rows are tagged in a `_corrected_by` column so a human can
      audit exactly which values came from the model vs. the parser.  This
      matters: an LLM can produce a confident, plausible, WRONG value, so
      machine-corrected cells must remain auditable.

MODEL BACKENDS (pick one via --backend)
    - "ollama"  : local Ollama server (default; e.g. `ollama run qwen2.5:7b`)
    - "openai"  : any OpenAI-compatible endpoint (llama.cpp server, vLLM,
                  LM Studio, etc.) via --base-url
    - "echo"    : no model — a dry-run stub that returns the row unchanged,
                  for testing the harness plumbing without a GPU.

USAGE
    Single model:
        python llm_fix_flagged_rows.py \
            --csv results_Cleveland_OH/<file>.csv \
            --pdf Input_Cleveland_Logs/<file>.pdf \
            --backend ollama --model qwen3.5:32b

    Two-tier escalation in one command (fast model first, only the rows it
    can't fix escalate to the stronger model):
        python llm_fix_flagged_rows.py \
            --csv results_Cleveland_OH/<file>.csv \
            --pdf Input_Cleveland_Logs/<file>.pdf \
            --backend ollama \
            --fast-model qwen3.5:7b --escalate-model qwen3.5:32b

    With no model flags, it defaults to the qwen3.5:7b → qwen3.5:32b chain.

    Concurrency is "auto" by default: each tier picks a safe number of
    parallel requests from your RAM and the model's size (capped at the
    number of rows it will process).  Pass --concurrency N to fix it.
    To actually run them in parallel, start the server with a matching
    OLLAMA_NUM_PARALLEL, e.g.:
        OLLAMA_NUM_PARALLEL=4 OLLAMA_MAX_LOADED_MODELS=2 ollama serve

    Writes <file>_corrected.csv next to the input CSV.  Each fixed row is
    tagged in a `_corrected_by` column with the tier that fixed it; rows no
    tier could fix stay flagged for a human.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import pandas as pd
import pdfplumber

# Reuse the parser's own validator and column helpers so the correction
# loop is checked against the exact same rules that flagged the rows.
from batch_parse_tabular_pdfs import (
    flag_rows,
    _profile_columns,
    _find_header_y,
    _detect_columns,
)

try:
    from wordfreq import zipf_frequency as _WORDFREQ
except Exception:  # wordfreq optional
    _WORDFREQ = None

REVIEW_COLS = ("_needs_review", "_review_reason")


# ══════════════════════════════════════════════════════════════════════════
#  LOCATING A FLAGGED ROW IN THE PDF
# ══════════════════════════════════════════════════════════════════════════

def _row_key(row, key_col):
    """The value used to locate this row back in the PDF (its ID)."""
    return str(row.get(key_col, "")).strip()


def _extract_region_words(pdf, key_value, key_col_idx):
    """Find the PDF line whose first-column token matches key_value.

    Returns (page_number, [words_on_that_line_and_continuations]) or None.
    Words are dicts with text/x0/x1/top so the LLM sees raw positions.
    Continuation lines (blank first column, for multi-line formats) that
    follow the keyed line are included up to the next keyed line.
    """
    if not key_value:
        return None

    for pnum, page in enumerate(pdf.pages, start=1):
        words = page.extract_words(x_tolerance=1)
        if not words:
            continue
        # Does this page contain the key value as a standalone token?
        if not any(w["text"] == key_value for w in words):
            continue

        # Group words by rounded Y (one physical line each)
        from collections import defaultdict
        lines = defaultdict(list)
        for w in words:
            lines[round(w["top"])].append(w)
        ordered_ys = sorted(lines.keys())

        # Find the line whose leftmost word is the key value
        for li, y in enumerate(ordered_ys):
            row_words = sorted(lines[y], key=lambda w: w["x0"])
            if not row_words:
                continue
            if row_words[0]["text"] != key_value:
                continue

            # Collect this line + following continuation lines (until the
            # next line that itself starts with a key-like token at the
            # far left — i.e. a new record).
            region = list(row_words)
            first_x = row_words[0]["x0"]
            for y2 in ordered_ys[li + 1:]:
                nxt = sorted(lines[y2], key=lambda w: w["x0"])
                if not nxt:
                    continue
                # A new record starts at roughly the same left x as the key
                # and looks like an ID; stop before it.
                starts_new = (
                    abs(nxt[0]["x0"] - first_x) < 5
                    and re.search(r"\d", nxt[0]["text"])
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_/]*",
                                     nxt[0]["text"])
                )
                if starts_new:
                    break
                region.extend(nxt)
            return pnum, region

    return None


def _region_to_text(region):
    """Render the raw region words with positions for the LLM prompt."""
    lines = {}
    from collections import defaultdict
    grouped = defaultdict(list)
    for w in region:
        grouped[round(w["top"])].append(w)
    out = []
    for y in sorted(grouped.keys()):
        ws = sorted(grouped[y], key=lambda w: w["x0"])
        parts = [f'"{w["text"]}"@{int(round(w["x0"]))}' for w in ws]
        out.append("  line: " + " ".join(parts))
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════
#  LLM BACKENDS
# ══════════════════════════════════════════════════════════════════════════

def _build_prompt(columns, col_x_starts, region_text, current_row, reasons):
    """Construct the correction prompt for one flagged row."""
    schema = ", ".join(columns)
    col_hints = "\n".join(
        f'  - "{name}" starts near x={int(x)}'
        for name, x in zip(columns, col_x_starts)
    )
    current = json.dumps(
        {c: str(current_row.get(c, "")) for c in columns}, ensure_ascii=False
    )
    return f"""You correct one row of data extracted from a government PDF table.

The table columns, in order, are:
{schema}

Approximate x-position where each column begins (words are placed left to
right; a word belongs to the column whose start x it is nearest to at or
after):
{col_hints}

RAW words for this record straight from the PDF, shown as "text"@x-position,
one physical line per row (records may wrap across several lines):
{region_text}

The parser's current (possibly wrong) output for this record is:
{current}

It was flagged because: {reasons}

Your job: using ONLY the raw words above and their x-positions, decide which
word belongs in which column and return the corrected record. If two values
were fused by overlapping text, split them by their x-positions. Do NOT
invent values that are not present in the raw words. If a field is genuinely
absent, use an empty string.

Return ONLY a JSON object with exactly these keys: {schema}
No explanation, no markdown, just the JSON object."""


def _call_ollama(prompt, model, host):
    import urllib.request
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(
        f"{host}/api/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
    return resp.get("response", "")


def _call_openai(prompt, model, base_url, api_key):
    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'not-needed'}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read())
    return resp["choices"][0]["message"]["content"]


class Tier:
    """One model in the escalation chain (backend + model + endpoint)."""

    def __init__(self, backend, model, host=None, base_url=None, api_key=None):
        self.backend = backend
        self.model = model
        self.host = host or "http://localhost:11434"
        self.base_url = base_url or "http://localhost:8000/v1"
        self.api_key = api_key

    def __str__(self):
        return f"{self.backend}:{self.model}" if self.model else self.backend


def _call_llm(prompt, tier):
    if tier.backend == "echo":
        # Dry-run stub: return empty so the harness keeps the original row.
        return ""
    if tier.backend == "ollama":
        return _call_ollama(prompt, tier.model, tier.host)
    if tier.backend == "openai":
        return _call_openai(prompt, tier.model, tier.base_url, tier.api_key)
    raise ValueError(f"Unknown backend: {tier.backend}")


def _total_ram_gb():
    """Total system RAM in GB, or None if it can't be determined."""
    # Portable-ish: try os.sysconf first (Linux/macOS), then sysctl (macOS).
    import os
    try:
        return (os.sysconf("SC_PAGE_SIZE") *
                os.sysconf("SC_PHYS_PAGES")) / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        import subprocess
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"],
                                      timeout=5)
        return int(out.strip()) / (1024 ** 3)
    except Exception:
        return None


def _ollama_model_size_gb(model, host):
    """On-disk size of an Ollama model in GB (proxy for its memory use)."""
    import urllib.request
    try:
        body = json.dumps({"name": model}).encode()
        req = urllib.request.Request(
            f"{host}/api/show", data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            info = json.loads(r.read())
        # Newer Ollama returns size_vram / details; fall back to tag list.
        size = info.get("size") or info.get("size_vram")
        if size:
            return int(size) / (1024 ** 3)
    except Exception:
        pass
    # Fallback: list models and match by name.
    try:
        with urllib.request.urlopen(f"{host}/api/tags", timeout=15) as r:
            tags = json.loads(r.read())
        for m in tags.get("models", []):
            if m.get("name") == model or m.get("model") == model:
                return int(m.get("size", 0)) / (1024 ** 3)
    except Exception:
        pass
    return None


def _auto_concurrency(tier, n_rows, hard_cap=8):
    """Choose a safe number of parallel LLM requests automatically.

    Heuristic (deliberately conservative — it does NOT try to find the
    true bandwidth ceiling, which needs benchmarking):
      - never more than the number of rows we're about to process
      - never more than `hard_cap`
      - leave the OS headroom, then divide the remaining memory budget by
        (model footprint + a per-slot KV-cache allowance)
    Falls back to a small value if sizes can't be probed.  Only meaningful
    for real server backends; echo/unknown default to 1.
    """
    if tier.backend not in ("ollama", "openai"):
        return 1
    if n_rows <= 1:
        return 1

    ram = _total_ram_gb()
    model_gb = None
    if tier.backend == "ollama":
        model_gb = _ollama_model_size_gb(tier.model, tier.host)

    # If we can't probe memory or model size, be conservative.
    if not ram or not model_gb:
        return min(2, n_rows, hard_cap)

    # Reserve headroom for the OS and other apps: 8 GB or 25%, whichever
    # is larger.  The model weights load once; each parallel slot adds a
    # KV-cache slice — our prompts are short, so ~15% of the model size
    # per slot is a safe allowance.
    reserved = max(8.0, ram * 0.25)
    budget = ram - reserved - model_gb
    if budget <= 0:
        return 1
    per_slot = max(0.5, model_gb * 0.15)
    slots = 1 + int(budget / per_slot)
    return max(1, min(slots, n_rows, hard_cap))


def _parse_json_row(text, columns):
    """Extract a JSON object with the expected columns from model output."""
    if not text or not text.strip():
        return None
    # Strip code fences if present
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                  flags=re.MULTILINE).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Try to find the first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    # Keep only known columns; fill missing with empty string
    return {c: str(obj.get(c, "")).strip() for c in columns}


# ══════════════════════════════════════════════════════════════════════════
#  MAIN CORRECTION LOOP
# ══════════════════════════════════════════════════════════════════════════

def _fmt_eta(seconds):
    """Human-readable ETA."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _progress(done, total, accepted, t0, width=28):
    """Render a single-line progress bar with counts and ETA."""
    frac = done / total if total else 1.0
    filled = int(width * frac)
    bar = "█" * filled + "·" * (width - filled)
    elapsed = time.time() - t0
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    sys.stdout.write(
        f"\r  [{bar}] {done}/{total} "
        f"({frac * 100:4.1f}%) fixed={accepted} "
        f"{rate:4.1f} row/s ETA {_fmt_eta(eta)}   "
    )
    sys.stdout.flush()


def _attempt_rows(df, data_cols, col_x_starts, pdf, tier, targets, key_col,
                  concurrency=1, profile=None, save_cb=None,
                  save_every=200, verbose=False):
    """Run one tier over the given target row indices.

    Mutates df in place for accepted corrections.  Returns
    (accepted_indices, still_flagged_indices).

    - LLM calls are fired concurrently (up to `concurrency`).
    - Rows are processed in batches of `concurrency` so a progress bar can
      advance and progress can be saved incrementally (via `save_cb`)
      rather than only at the very end.
    - Each correction is validated against `profile` (the FULL-table
      column profile) so validation is O(1) per row, not O(table).
    """
    accepted = []
    still = []

    # Phase 1: build prompts sequentially (pdfplumber is not thread-safe).
    jobs = []  # (i, page_no, prompt)
    for i in targets:
        row = {c: df.iloc[i][c] for c in data_cols}
        key = _row_key(row, key_col)
        reasons = str(df.iloc[i].get("_review_reason", "")).strip()

        located = _extract_region_words(pdf, key, 0)
        if located is None:
            if verbose:
                print(f"  row {i + 1} (key {key!r}): region not found — skip")
            still.append(i)
            continue
        page_no, region = located
        region_text = _region_to_text(region)
        prompt = _build_prompt(
            data_cols, col_x_starts, region_text, row, reasons
        )
        jobs.append((i, page_no, prompt))

    if not jobs:
        return accepted, still

    from concurrent.futures import ThreadPoolExecutor

    total = len(jobs)
    done = 0
    t0 = time.time()
    since_save = 0

    def handle_result(i, page_no, raw):
        """Validate one LLM result and write it if it passes. Returns bool."""
        if isinstance(raw, Exception):
            if verbose:
                print(f"\n  row {i + 1}: LLM call failed ({raw})")
            return False
        corrected = _parse_json_row(raw, data_cols)
        if corrected is None:
            if verbose:
                print(f"\n  row {i + 1}: no valid JSON")
            return False
        # Validate the single corrected row against the FULL-table profile.
        row_flags = flag_rows([corrected], profile=profile)
        if row_flags:  # 0 -> clean, non-empty -> still flagged
            if verbose:
                print(f"\n  row {i + 1} (p{page_no}): still flagged "
                      f"({'; '.join(row_flags[0][1])})")
            return False
        # Strict output gate: reject residual garble / invalid dates that
        # the structural check misses (e.g. "AE DM OF TEOLRO VNEYHICLE").
        ok, why = _output_is_clean(corrected, data_cols)
        if not ok:
            if verbose:
                print(f"\n  row {i + 1} (p{page_no}): rejected by output "
                      f"gate — {why}")
            return False
        for c in data_cols:
            df.at[i, c] = corrected[c]
        df.at[i, "_needs_review"] = ""
        df.at[i, "_review_reason"] = ""
        df.at[i, "_corrected_by"] = f"llm:{tier}"
        return True

    # Phase 2+3: process in batches of `concurrency`.
    step = max(1, concurrency)
    for start in range(0, total, step):
        batch = jobs[start:start + step]

        if concurrency <= 1:
            results = []
            for i, page_no, prompt in batch:
                try:
                    results.append((i, page_no, _call_llm(prompt, tier)))
                except Exception as e:
                    results.append((i, page_no, e))
        else:
            results = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                fut_map = {
                    pool.submit(_call_llm, prompt, tier): (i, page_no)
                    for i, page_no, prompt in batch
                }
                for fut in fut_map:
                    i, page_no = fut_map[fut]
                    try:
                        results.append((i, page_no, fut.result()))
                    except Exception as e:
                        results.append((i, page_no, e))

        # Validate + write (deterministic order)
        for i, page_no, raw in sorted(results, key=lambda r: r[0]):
            ok = handle_result(i, page_no, raw)
            (accepted if ok else still).append(i)
            done += 1
            since_save += 1

        _progress(done, total, len(accepted), t0)

        if save_cb and since_save >= save_every:
            save_cb()
            since_save = 0

    sys.stdout.write("\n")
    if save_cb:
        save_cb()  # flush at end of tier
    return accepted, still


def _field_still_garbled(value):
    """True if a text value still contains scrambled word-salad.

    This is the check the structural validator misses: a value like
    "AE DM OF TEOLRO VNEYHICLE" has the right character types and length,
    so flag_rows accepts it — but its words aren't real words.  We run the
    same dictionary test the parser's flagger uses, but directly on the
    LLM's OUTPUT and with a stricter threshold (this is a final gate on a
    value we're about to WRITE, so we'd rather reject a borderline case and
    leave the row flagged than persist garbage).
    """
    if _WORDFREQ is None:
        return False  # can't check without the dictionary
    v = str(value)
    # Embedded interleave symbol between letters (e.g. "P*RCIHVAILREGGEED")
    if re.search(r"[A-Za-z]\*[A-Za-z]", v):
        return True
    # Dictionary check on multi-letter tokens.  Use a 2-token floor (not 3),
    # so short garbled dispositions like "TEOLRO VNEYHICLE" are caught.
    toks = [re.sub(r"[^A-Za-z]", "", t) for t in v.split()]
    toks = [t for t in toks if len(t) >= 3]
    if len(toks) >= 2:
        nonwords = sum(1 for t in toks if _WORDFREQ(t.lower(), "en") == 0.0)
        if nonwords >= len(toks) * 0.6:
            return True
    return False


def _looks_like_date_col(col_name):
    cl = col_name.lower()
    return any(k in cl for k in ("date", "time", "occurred"))


def _output_is_clean(corrected, data_cols):
    """Strict gate on an LLM correction, beyond structural validation.

    Rejects a correction if ANY field:
      - is a date column whose value isn't a real M/D/YYYY date, or
      - still contains scrambled/garbled word-salad.
    Returns (ok: bool, reason: str).  This is what stops confident-but-wrong
    LLM output (corrupted dates, un-descrambled text) from being written.
    """
    for col in data_cols:
        val = str(corrected.get(col, "")).strip()
        if not val:
            continue
        if _looks_like_date_col(col):
            # Allow an optional 'at HH:MM'; the date part must be real.
            if not re.fullmatch(
                    r"\d{1,2}/\d{1,2}/\d{4}(\s+at\s+\d{1,2}:\d{2})?", val):
                return False, f"{col!r} not a valid date ({val!r})"
        else:
            if _field_still_garbled(val):
                return False, f"{col!r} still garbled ({val[:30]!r})"
    return True, ""


def correct_csv(csv_path, pdf_path, tiers, concurrency=1, limit=0,
                resume=False, save_every=200, verbose=False):
    """Run an escalation chain of model tiers over the flagged rows.

    `tiers` is a list of Tier objects, tried in order.  Each tier only
    attempts the rows that are still flagged after the previous tier, so
    a cheap fast model clears the easy rows and only the residue reaches
    the larger model.

    `concurrency` is how many LLM requests to fire at once (int, or the
    string "auto" for per-tier auto-sizing).
    `limit` > 0 processes only the first N flagged rows (for a quick
    quality check before committing to a full run).
    `resume` continues a prior run: it loads the existing
    <file>_corrected.csv if present and skips rows already corrected.
    `save_every` writes the output CSV every N processed rows so a crash
    loses at most that many rows of work.
    """
    out_path = csv_path.with_name(csv_path.stem + "_corrected.csv")

    # Resume: prefer the partially-corrected file if it exists.
    src = csv_path
    if resume and out_path.exists():
        src = out_path
        print(f"Resuming from {out_path.name}")
    df = pd.read_csv(src, dtype=str, keep_default_na=False)
    data_cols = [c for c in df.columns if c not in REVIEW_COLS
                 and c != "_corrected_by"]

    # Column x-starts (for the LLM hint) from the PDF header.
    col_x_starts = []
    with pdfplumber.open(pdf_path) as probe:
        words = probe.pages[0].extract_words(x_tolerance=1)
        hy = _find_header_y(words)
        if hy is not None:
            detected = _detect_columns(words, hy)
            starts = {name: x for name, x, _ in detected}
            col_x_starts = [starts.get(c, 0) for c in data_cols]
    if not col_x_starts:
        col_x_starts = [0] * len(data_cols)

    key_col = data_cols[0]  # first column is the record key

    if "_corrected_by" not in df.columns:
        df["_corrected_by"] = ""

    # Precompute the column profile ONCE from the full table, so each
    # corrected row is validated against whole-table norms in O(1).
    all_rows = [
        {c: df.iloc[j][c] for c in data_cols} for j in range(len(df))
    ]
    profile = _profile_columns(all_rows)

    remaining = [
        i for i in range(len(df))
        if str(df.iloc[i].get("_needs_review", "")).strip().upper() == "YES"
    ]
    if limit and limit > 0:
        remaining = remaining[:limit]
    if not remaining:
        print("No flagged rows to correct.")
        return None

    auto = isinstance(concurrency, str) and concurrency.lower() == "auto"
    print(f"{len(remaining)} flagged rows to attempt across "
          f"{len(tiers)} tier(s): {', '.join(str(t) for t in tiers)}")
    if limit:
        print(f"(--limit {limit}: sampling the first {len(remaining)} "
              f"flagged rows)")
    if auto:
        print("Concurrency: auto (chosen per tier from RAM + model size).")
    elif isinstance(concurrency, int) and concurrency > 1:
        print(f"Concurrency: up to {concurrency} requests in flight "
              f"(match to OLLAMA_NUM_PARALLEL).")
    print(f"Saving progress every {save_every} rows → {out_path.name}\n")

    def save():
        df.to_csv(out_path, index=False)

    total_accepted = 0
    with pdfplumber.open(pdf_path) as pdf:
        for ti, tier in enumerate(tiers, start=1):
            if not remaining:
                break
            if auto:
                tier_conc = _auto_concurrency(tier, len(remaining))
                note = f", auto-concurrency {tier_conc}"
            else:
                tier_conc = max(1, int(concurrency))
                note = f", concurrency {tier_conc}" if tier_conc > 1 else ""
            print(f"── Tier {ti}: {tier}  "
                  f"({len(remaining)} row(s) to try{note}) ──")
            accepted, still = _attempt_rows(
                df, data_cols, col_x_starts, pdf, tier, remaining, key_col,
                concurrency=tier_conc, profile=profile, save_cb=save,
                save_every=save_every, verbose=verbose,
            )
            total_accepted += len(accepted)
            print(f"   tier {ti} fixed {len(accepted)}, "
                  f"{len(still)} remain\n")
            remaining = still

    save()
    print(f"Done. Fixed {total_accepted} row(s); "
          f"{len(remaining)} still flagged for human review.")
    print(f"Wrote {out_path}")
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--csv", required=True, type=Path,
                    help="The <file>.csv produced by the parser (with review cols).")
    ap.add_argument("--pdf", required=True, type=Path,
                    help="The original PDF the CSV was extracted from.")
    ap.add_argument("--backend", default="ollama",
                    choices=["ollama", "openai", "echo"],
                    help="LLM backend for all tiers (default: ollama).")

    # Single-tier (backwards compatible) OR two-tier escalation.
    ap.add_argument("--model", default=None,
                    help="Single model to use (e.g. qwen3.5:32b). Use this "
                         "OR the --fast-model/--escalate-model pair.")
    ap.add_argument("--fast-model", default=None,
                    help="Cheap model tried FIRST on every flagged row "
                         "(e.g. qwen3.5:7b).")
    ap.add_argument("--escalate-model", default=None,
                    help="Stronger model tried on rows the fast model "
                         "couldn't fix (e.g. qwen3.5:32b).")

    ap.add_argument("--host", default="http://localhost:11434",
                    help="Ollama host URL.")
    ap.add_argument("--base-url", default="http://localhost:8000/v1",
                    help="OpenAI-compatible base URL (for --backend openai).")
    ap.add_argument("--api-key", default=None,
                    help="API key if the OpenAI-compatible endpoint needs one.")
    ap.add_argument("--concurrency", default="auto",
                    help="How many LLM requests to fire at once. Use an "
                         "integer to set it fixed, or 'auto' (default) to "
                         "choose a safe value per tier from your RAM and the "
                         "model size. Set the server's OLLAMA_NUM_PARALLEL to "
                         "at least this value to actually run them in "
                         "parallel.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process the first N flagged rows. Use this to "
                         "check correction quality on a small sample before "
                         "committing to a full run. 0 = all (default).")
    ap.add_argument("--resume", action="store_true",
                    help="Continue a prior run: load the existing "
                         "<file>_corrected.csv and skip rows already fixed.")
    ap.add_argument("--save-every", type=int, default=200,
                    help="Write the output CSV every N processed rows so a "
                         "crash loses at most that much work (default 200).")
    ap.add_argument("--verbose", action="store_true",
                    help="Print a line for every non-fixed row (default: just "
                         "the progress bar).")
    args = ap.parse_args()

    if not args.csv.exists():
        sys.exit(f"CSV not found: {args.csv}")
    if not args.pdf.exists():
        sys.exit(f"PDF not found: {args.pdf}")

    # Build the tier chain.
    def mk(model):
        return Tier(args.backend, model, host=args.host,
                    base_url=args.base_url, api_key=args.api_key)

    tiers = []
    if args.fast_model or args.escalate_model:
        # Two-tier escalation (either or both may be set).
        if args.fast_model:
            tiers.append(mk(args.fast_model))
        if args.escalate_model:
            tiers.append(mk(args.escalate_model))
        if args.model:
            print("Note: --model is ignored when --fast-model/"
                  "--escalate-model are given.")
    elif args.model:
        tiers.append(mk(args.model))
    elif args.backend == "echo":
        tiers.append(mk(None))
    else:
        # Default chain for a 48GB Mac using REAL Ollama tags.
        tiers.append(mk("qwen3.5:9b"))
        tiers.append(mk("qwen3.5:27b"))
        print("No model specified — using default escalation chain "
              "qwen3.5:9b → qwen3.5:27b.\n")

    # Concurrency: "auto" or an integer.
    conc = args.concurrency
    if isinstance(conc, str) and conc.lower() != "auto":
        try:
            conc = int(conc)
        except ValueError:
            sys.exit(f"--concurrency must be 'auto' or an integer, got {conc!r}")

    correct_csv(args.csv, args.pdf, tiers, concurrency=conc,
                limit=args.limit, resume=args.resume,
                save_every=args.save_every, verbose=args.verbose)


if __name__ == "__main__":
    main()