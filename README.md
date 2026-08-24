# Police Data Extraction Toolkit

Automated Python tools for parsing police department PDF logs into structured CSV datasets. Includes three parsers plus a two-stage correction pipeline for the flagged rows:

1. **Arrest Log Parser** — extracts individual arrest charge records from Porterville, CA and Torrance, CA media arrest summaries.
2. **Dispatch Log Parser** — extracts calls-for-service / dispatch records from Marlborough, MA and Brea, CA press logs.
3. **Universal Tabular Parser** — extracts rows from any text-layer tabular PDF (no OCR), auto-detecting columns from the document itself. Tested on Cleveland, OH (incident), Oceanside, CA (arrest), Bainbridge Island, WA (incident), and Alameda, CA (arrest). Includes an automatic review-flagging system that marks suspicious rows for manual checking, and a built-in deterministic repair for the most common PDF defect (see *Correcting Flagged Rows* below).
4. **Correction pipeline** — for rows the parser flags, a deterministic fixer repairs the mechanical defects safely and fast, and an optional LLM pass attempts the irregular residue. Deterministic first, LLM only for what's left.

---

## Project Structure

```text
arrest-data-conversion/
│
├── batch_parse_arrest_logs.py          # Arrest log extraction script
├── batch_parse_dispatch_logs.py        # Dispatch / calls-for-service extraction script
├── batch_parse_tabular_pdfs.py         # Universal tabular PDF extraction script
├── deinterleave.py                     # Shared deterministic de-interleave engine
├── fix_flagged_rows.py                 # Deterministic corrector for existing CSVs
├── llm_fix_flagged_rows.py             # Optional LLM corrector for the irregular residue
├── requirements.txt                    # Python package dependencies
├── README.md
│
├── Input_Porterville_Logs/             # Porterville arrest PDFs
├── Input_Torrance_Logs/                # Torrance arrest PDFs
├── Input_Marlborough_Logs/             # Marlborough dispatch/press log PDFs
├── Input_Brea_Logs/                    # Brea calls-for-service PDFs
├── Input_Cleveland_Logs/               # Cleveland incident PDFs (tabular parser)
├── Input_Oceanside_Logs/               # Oceanside arrest PDFs (tabular parser)
├── Input_Bainbridge_Island_Logs/       # Bainbridge Island incident PDFs (tabular parser)
│
├── results_Porterville_CA/             # Generated Porterville arrest CSVs
├── results_Torrance_CA/                # Generated Torrance arrest CSVs
├── results_Marlborough_MA/             # Generated Marlborough dispatch CSVs
├── results_Brea_CA/                    # Generated Brea dispatch CSVs
├── results_Cleveland_OH/               # Generated Cleveland CSVs
├── results_Oceanside_CA/               # Generated Oceanside CSVs
└── results_Bainbridge_Island_WA/       # Generated Bainbridge Island CSVs
```

---

## Requirements & Environment Setup

### 1. Prerequisites

* [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or [Anaconda](https://www.anaconda.com/)
* [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) (only required for scanned PDFs like Brea)

### 2. Conda Environment Setup

```bash
# Create and activate environment
conda create -n arrest_parser python=3.10 -y
conda activate arrest_parser

# Install Python dependencies
pip install -r requirements.txt
```

### 3. Install Tesseract (for Brea OCR)

Tesseract is only needed if processing scanned PDFs (Brea calls-for-service). All other departments have text layers and do not require it.

```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt-get install tesseract-ocr

# Windows — download installer from https://github.com/tesseract-ocr/tesseract
```

### 4. Optional: Install poppler (faster Marlborough processing)

```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils
```

If poppler is not installed, the dispatch parser falls back to pdfplumber automatically.

### 5. `requirements.txt`

```text
pdfplumber>=0.10.0
pandas>=2.0.0
pytesseract>=0.3.10
Pillow>=10.0.0
wordfreq>=3.1.1
```

`wordfreq` powers the review-flagging system in the tabular parser (dictionary check for garbled text). It pulls in `ftfy`, `langcodes`, `regex`, `msgpack`, and `wcwidth` automatically. If `wordfreq` is not installed, the tabular parser still runs — it just skips the dictionary-based garbled-text check and relies on the other structural checks.

The correction tools (`fix_flagged_rows.py`, `deinterleave.py`, `llm_fix_flagged_rows.py`) add **no new pip dependencies** — they use only `pandas`, `pdfplumber`, and the Python standard library. The LLM corrector talks to a local Ollama server over HTTP; Ollama and its models are installed separately (see *Local LLM setup*), not via pip.

---

## Arrest Log Parser

### Supported Departments

| Department | Filename Pattern | Example |
| --- | --- | --- |
| Porterville, CA | `MMDDYY TO MMDDYY*.pdf` | `010125 TO 022825 ARREST LOG_Redacted.pdf` |
| Torrance, CA | `Arrest_Log_-_YYYY-MM*.pdf` | `Arrest_Log_-_2021-01_JAN_Redacted.pdf` |

### Usage

```bash
conda activate arrest_parser
python batch_parse_arrest_logs.py
```

The script recursively scans the project folder, identifies which department each PDF belongs to, and outputs CSVs to `results_Porterville_CA/` and `results_Torrance_CA/`.

### Extracted Fields

| Field | Description |
| --- | --- |
| Arrest ID Number | Agency booking/arrest ID |
| Arrestee Name | Full name |
| Race / Ethnicity / Gender | Demographic fields (default: N/A if unlisted) |
| Age | Calculated from birth date and arrest date |
| Home Address | Street address (often redacted) |
| Offense and Charge Description | Combined offense description and statutory code |
| Charge Level | Misdemeanor (M), Felony (F), or Infraction (I) |
| Time and Date of Arrest | Timestamp in HH:MM:SS MM/DD/YY format |
| Location of Arrest | Address or coordinates of arrest |
| Crime Incident Report ID Number | Related incident/case number |
| Police Division / Beat | Agency sector/area code |
| Police Department | Standardized agency name |
| Source PDF File | Original PDF filename |

---

## Dispatch Log Parser

### Supported Departments

| Department | PDF Type | Format |
| --- | --- | --- |
| Marlborough, MA | Text-layer press logs | Dispatch Log / Selective Search (2008–2026) |
| Brea, CA | Scanned incident summary | Law Incident Summary Report, by Incident Number |

The parser auto-detects the department and format. Marlborough PDFs include three known sub-formats (varying header text across years) that are all handled identically. Brea PDFs require OCR via Tesseract.

### Usage

```bash
conda activate arrest_parser

# Marlborough
python batch_parse_dispatch_logs.py ./Input_Marlborough_Logs --out ./results_Marlborough_MA

# Brea (requires Tesseract — will take ~1 sec/page for OCR)
python batch_parse_dispatch_logs.py ./Input_Brea_Logs --out ./results_Brea_CA
```

### Options

| Flag | Description |
| --- | --- |
| `--out <dir>` | Output directory for CSV files (default: current directory) |
| `--skip-ocr` | Skip scanned (Brea) PDFs, only process text-layer PDFs |
| `--pages N` | Process only the first N pages of each PDF (useful for testing) |
| `--no-master` | Skip generating the combined MASTER CSV (saves memory on large runs) |
| `--resolution N` | OCR render DPI for scanned PDFs (default: 300) |

### Extracted Fields

| Field | Description |
| --- | --- |
| Dispatch Call ID Number | Incident number (Brea only; Marlborough logs do not include these) |
| Dispatch Code | Nature/call type code (Brea only) |
| Call Description | Call reason or nature description |
| Priority Level of Call | Priority level (if available) |
| Call Disposition | Action taken / disposition |
| Crime Incident Report Written | Yes/No based on disposition or arrest reference |
| Crime Incident ID | Arrest reference ID (e.g. 26-3-AR) when present |
| Address Street | Street address of the call |
| Address City | City (Marlborough or Brea) |
| Address Zip | Zip code (if available) |
| Geographic Coordinates | Coordinates (if available) |
| Police Division | Department name |
| Police Beat | Beat/location code (Brea only) |
| Time of Call | Time the call was logged |
| Time of Dispatch | Dispatch time (if available) |
| Time of Arrival | Arrival time (if available) |
| Date of Call | Date of the call |
| Source PDF File | Original PDF filename |
| Source Page | Page number in source PDF |

---

## Universal Tabular Parser

A general-purpose parser for any **text-layer tabular PDF** (no OCR required). Instead of hard-coded per-department rules, it detects the table structure from each document — column names, boundaries, and orientation — so it works across departments with minimal setup. It handles overflowing/wrapping text, right-aligned values, and multi-word headers, and flags rows it is unsure about for manual review.

### Supported Departments (tested)

| Department | Type | Table Structure |
| --- | --- | --- |
| Cleveland, OH | Incident | Borderless — columns inferred from header + word positions |
| Oceanside, CA | Arrest | Bordered — cell grid read directly from table lines |
| Bainbridge Island, WA | Incident | Borderless — tightly-packed columns |
| Alameda, CA | Arrest details | Borderless — multi-line records (each arrest wraps across 2–4 lines) |

Any other clean, text-layer tabular PDF should also work; see *Reliability & Review Flags* below.

### Two-Tier Extraction Strategy

1. **Rect-based** — if the PDF has real table cell borders (lines/rectangles), those are used as exact column and row boundaries. Pixel-perfect. (Oceanside.)
2. **Word-position** — if there are no borders, columns are detected from header word positions using adaptive gap analysis, with a space-glyph signal to keep overflowing/wrapping text in the correct cell. (Cleveland, Bainbridge, Alameda.)

The strategy is chosen automatically per file.

### Single-line vs. Multi-line Records

Most tabular PDFs put one record per physical line. Some — like Alameda's arrest-details export — wrap a single record across several lines, where only the first line carries the record ID and the following lines hold overflow text (a long status, a multi-word summary, an officer's surname) with the ID column left blank:

```text
2022-902  Mar-17  FIELD RELEASE -  MINOR IN         1618 - BOUCHET  GONZALEZ, JOEL...
                  CITE / OR        POSSESSION OF    LLOYD
                  (WARRANT)        OPEN CONTAINER
```

All three lines above are one arrest. The parser detects this pattern automatically — it merges continuation lines (blank ID column) back into the record above them, joining each field's text — and does so across page breaks. The detection is conservative: it only activates when a meaningful share of rows have a blank first column *and* the populated first-column values look like record keys (IDs). Single-line formats (Cleveland, Oceanside, Bainbridge) are recognized as such and pass through unchanged.

### Two-line Headers

Some headers wrap onto a second physical line (e.g. an "Arrest Number" column whose header prints "Arrest" over "Number"). The parser folds the second header line into the header rather than emitting it as a spurious data row, and it correctly recovers the first data row on continuation pages that repeat no header.

### Usage

The tabular parser uses a `City State` argument convention, reading from `Input_<City>_Logs/` and writing to `results_<City>_<State>/`.

```bash
conda activate arrest_parser

# Reads Input_Cleveland_Logs/, writes results_Cleveland_OH/
python batch_parse_tabular_pdfs.py Cleveland OH

# Reads Input_Oceanside_Logs/, writes results_Oceanside_CA/
python batch_parse_tabular_pdfs.py Oceanside CA

# Multi-word city names go in quotes
python batch_parse_tabular_pdfs.py "Bainbridge Island" WA --pages 5
```

### Options

| Flag | Description |
| --- | --- |
| `--pages N` | Process only the first N pages of each PDF (useful for testing) |
| `--no-master` | Skip generating the combined MASTER CSV (saves memory on large runs) |

### Output Files

For each input PDF, the parser writes to `results_<City>_<State>/`:

* **`<filename>.csv`** — every extracted row. The first two columns are `_needs_review` (`YES` for flagged rows) and `_review_reason` (why it was flagged), followed by the columns detected from the PDF header.
* **`<filename>_REVIEW.csv`** — only the flagged rows, each with its original row number and reasons, so you can review a short list instead of scanning the whole file.
* **`MASTER_<City>_<State>.csv`** — all rows from all PDFs combined (unless `--no-master` is set).

Column names come directly from the PDF header — the parser preserves whatever the source document uses.

### Reliability & Review Flags

The parser handles clean tabular PDFs well, but real-world PDFs sometimes contain defects the extractor cannot fully recover — most often when long text visually wraps and its characters physically overlap another column in the PDF's text layer. Rather than silently emit a bad row, the parser runs a validation pass and flags anything structurally suspicious.

A row is flagged for review when any of these are detected:

* A nearly-empty row in a multi-column table
* A value far longer than that column's normal range (text bleeding in from a neighbor)
* A mostly-numeric column holding mostly-letters (or vice versa)
* A date column whose value doesn't look like a date, or a non-date column containing an embedded date/time
* Interleaving artifacts from overlapping text (e.g. `3A/V6E/2022`)
* A symbol wedged between non-words (e.g. `FIREA*RCMHSA`)
* Garbled / scrambled text — checked against an English dictionary via `wordfreq` (e.g. `ORRR CANOTN SEUNST`)
* Clusters of tiny non-word fragments paired with non-dictionary content

Flag thresholds are relative to what each column normally contains in the same file, so the system adapts to new departments rather than assuming a fixed format. Columns that hold **proper names** (people, places) are detected automatically and exempted from the dictionary-based garble check, since names are legitimately not dictionary words — this avoids false-flagging every unusual surname in a name column.

**Built-in repair.** Before flagging, the parser runs a deterministic de-interleave pass that repairs the single most common defect — the address/date pixel collision, where a street suffix is printed in the same pixels as the date and interleaves into garble like `3A/V6E/2022` (= `AVE` + `3/6/2022`). This is fixed in place when the recovery is verifiably clean, so on fresh PDFs many of these rows never get flagged at all. Older CSVs produced before this pass existed can be repaired with `fix_flagged_rows.py` (see *Correcting Flagged Rows*).

**Recommended workflow:** run the parser, open `<filename>_REVIEW.csv`, and verify or fix only those rows (or filter `_needs_review = YES` in the main CSV). On the tested samples this reduces manual review to a handful of rows: 3 of 460 for Cleveland, 0 for Oceanside, 0 for Bainbridge, and 5 of 901 for Alameda — and those flagged rows are mostly genuine overlap artifacts in the source PDF.

**Caveat:** the flagger catches *structural* anomalies (misplaced, garbled, or overlapping text). It cannot catch an error where a wrong value still looks plausible and lands in a reasonable-looking column. For a brand-new department, the first run still deserves a manual spot-check beyond just the flagged rows.

### Extracted Fields

The tabular parser does not impose a fixed schema — it outputs whatever columns the PDF's header defines, plus the two review columns. For the tested departments the detected columns are:

| Department | Columns |
| --- | --- |
| Cleveland, OH | Case Number, Address, Date & Time, Offense Description, Case Disposition, PD District |
| Oceanside, CA | Incident Type, Incident Number, Incident Date, Violation Type, Violation Section, Violation Description, Location |
| Bainbridge Island, WA | call_id, actdate, acttime, streetnbr, street, geox, geoy, naturecode |
| Alameda, CA | Arrest Number, Arrest Date, Status, Summary, Arrest Officer, Arrestee, DOB, Current Cell, Arrest Reason |

---

## Correcting Flagged Rows

The parser flags rows it cannot fully trust (see *Reliability & Review Flags*). Those flags are a feature — they mark exactly what needs attention rather than silently emitting bad data. Two tools help clear them, and they are meant to run **in order**: deterministic first, LLM only for what remains.

### Why this order

Analysis of a full 485k-row Cleveland file (13,506 flagged rows) showed the flags are dominated by **one mechanical defect repeated thousands of times**: the address/date pixel collision. About 65% of all flags are this single pattern. It reverses deterministically with a strict validity gate — fast, correct, and safe. An LLM, by contrast, cleared only a small fraction of flagged rows on the same data and occasionally produced *silent date corruption* (e.g. writing `28/2022`, a date with no month, that still passed a structural check). So the deterministic pass does the bulk of the work, and the LLM is reserved for the irregular long tail where its flexibility is genuinely worth the risk.

### Stage 1 — Deterministic corrector (`fix_flagged_rows.py`)

Repairs the address/date collision on an already-parsed CSV. It recovers each field independently and only writes verifiably-clean values:

* the **date** is written only when the recovered digits form a real `M/D/YYYY`;
* the **address** is written only when the fused suffix is a known short street type (AVE, RD, PKWY, DR, …) with the rest of the address intact.

Deeply-tangled cases (e.g. `MARTIN LUTHER KING JR DR`, where the collision destroyed the word spacing) get their **date** back but keep their **address** flag — nothing garbled is ever written.

```bash
conda activate arrest_parser

python fix_flagged_rows.py --csv results_Cleveland_OH/<file>.csv
# writes results_Cleveland_OH/<file>_fixed.csv
```

On a 50,000-row slice of the real Cleveland data this took the flag count from 1,065 down to 709 (356 rows fully cleared), with **zero invalid dates produced**. Fixed rows are tagged in a `_corrected_by` column (`deterministic:address-date`) so every machine-written value stays auditable.

Options:

| Option | Description |
| --- | --- |
| `--csv PATH` | The parser-produced CSV to repair (required). |
| `--address-col NAME` | Address column name (default `Address`). |
| `--date-col NAME` | Date column name (default `Date & Time`). |

This same de-interleave is built into `batch_parse_tabular_pdfs.py`, so **PDFs parsed from now on come out clean in one pass** — you only need `fix_flagged_rows.py` for CSVs produced before it existed.

### Stage 2 — LLM corrector (`llm_fix_flagged_rows.py`, optional)

For the irregular rows no deterministic pattern matches, an LLM can attempt a repair by re-reading the original PDF region for each flagged row. It is optional and best pointed at the *residue* left after Stage 1.

**How it works:** for each still-flagged row it locates that record in the PDF (by ID), shows the model the raw positioned words plus the column schema, asks for a corrected row as JSON, then **re-validates** the result with the parser's own flagger — accepting only outputs that pass. It needs both the CSV (which rows are flagged) and the PDF (the raw word positions the CSV has already lost).

```bash
# Point it at the Stage-1 output so it only sees the residue
python llm_fix_flagged_rows.py \
    --csv results_Cleveland_OH/<file>_fixed.csv \
    --pdf Input_Cleveland_Logs/<file>.pdf \
    --backend ollama \
    --fast-model llama3:latest \
    --escalate-model qwen3-coder:30b-a3b-q4_K_M \
    --limit 25 --verbose
```

**Always run `--limit 25` first and read the results.** The LLM can produce confident, plausible-looking but wrong values, so judge quality on a sample before committing to a full run. Every LLM-written value is tagged in `_corrected_by` (e.g. `llm:ollama:llama3:latest`) so it can be audited or filtered out later — important for research or legal use.

Key options:

| Option | Description |
| --- | --- |
| `--csv` / `--pdf` | The flagged CSV and its source PDF (both required). |
| `--fast-model` / `--escalate-model` | Two-tier chain: the fast model tries every row first; only rows it can't fix escalate to the stronger model. |
| `--model NAME` | Use a single model instead of the two-tier chain. |
| `--concurrency N` \| `auto` | How many requests to fire at once. `auto` (default) picks a safe value per tier from RAM and model size. Match your server's `OLLAMA_NUM_PARALLEL`. |
| `--limit N` | Process only the first N flagged rows (for a quality check). |
| `--resume` | Continue a prior run, skipping rows already fixed. |
| `--save-every N` | Write progress every N rows so a crash loses at most that much work (default 200). |
| `--verbose` | Print a line per non-fixed row (default: just a progress bar). |

### Local LLM setup (Ollama)

The LLM corrector talks to a local model server over HTTP — it needs **no extra pip packages**. Install Ollama separately and pull the models:

```bash
brew install ollama

# Start the server with parallel slots (48GB Macs handle 8 comfortably);
# MAX_LOADED_MODELS=2 keeps both tiers resident for the escalation chain.
OLLAMA_NUM_PARALLEL=8 OLLAMA_MAX_LOADED_MODELS=2 OLLAMA_FLASH_ATTENTION=1 ollama serve

# In another terminal, pull models (use tags you actually have / want):
ollama pull llama3:latest
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

Leave `ollama serve` running in its own terminal while the corrector runs. If it's installed as a menu-bar app, that app supervises the server; set the variables with `launchctl setenv OLLAMA_NUM_PARALLEL 8` (etc.) and relaunch the app instead of running `serve` by hand.

### Recommended workflow for a flagged file

1. Run **`fix_flagged_rows.py`** — clears the bulk (the mechanical address/date defect) deterministically, in seconds, with no corruption risk.
2. Open the `_fixed.csv` and check the remaining flag count and reasons.
3. Optionally run **`llm_fix_flagged_rows.py --limit 25`** on the residue and read the corrections. If the quality is good, run it without `--limit`; if not, hand the residue to a human using the `_REVIEW` list.
4. Filter or audit by the `_corrected_by` column at any time to see which values came from the parser, the deterministic fixer, or the LLM.


### Troubleshooting
* **`ModuleNotFoundError: No module named 'pdfplumber'`** — Activate the conda environment first: `conda activate arrest_parser`
* **`FileNotFoundError: No such file or directory: 'pdftotext'`** — Install poppler (`brew install poppler`) or ignore this; the dispatch parser falls back to pdfplumber automatically.
* **`TesseractNotFoundError`** — Install Tesseract (`brew install tesseract`). Only needed for Brea PDFs.
* **Tabular parser: garbled-text rows not flagged** — Install `wordfreq` (`pip install wordfreq`). Without it, the dictionary-based check is skipped and some scrambled-text rows may not be caught.
* **Tabular parser: `Input folder not found`** — The folder must be named exactly `Input_<City>_Logs` (spaces in the city name become underscores). E.g. `python batch_parse_tabular_pdfs.py "Bainbridge Island" WA` reads `Input_Bainbridge_Island_Logs/`.
* **Arrest parser skips a PDF** — Verify the filename matches an expected pattern (see table above).
* **Dispatch parser detects wrong layout** — Brea is only detected for files named `CALLS*FOR*SERVICE*`. All other PDFs default to Marlborough.
* **Large file crashes / out of memory** — Use `--no-master` to skip the combined CSV, or `--pages N` to limit processing. Each file still gets its own CSV even if another file crashes.
* **OCR address errors in Brea CSV** — Scanned PDFs produce OCR noise, especially on redacted (blacked-out) fields. The parser filters most garbage but some garbled addresses are expected. Entries with unreadable timestamps/dates are dropped rather than concatenated into adjacent rows.
* **Too many / too few review flags on a new department** — Flag thresholds adapt to each file's own data. If a legitimate value type is being flagged (e.g. an unusual abbreviation), it can be added to the common-words set in `flag_rows()`; if real errors are being missed, the first-run manual spot-check is the safety net.
* **Tabular parser: record count looks doubled, or rows have a blank first column** — This is a multi-line-record PDF where each record wraps across several lines. The parser normally merges these automatically; if it doesn't, the first-column values may not look enough like record IDs for auto-detection to trigger. Check that the ID column is consistently populated on the first line of each record.
* **`fix_flagged_rows.py` leaves many rows still flagged** — Expected. It only repairs the address/date collision, and only when the result is verifiably clean; deeply-tangled addresses keep their flag by design (their date is still recovered). The remainder is for the LLM pass or a human. If your columns aren't named `Address` / `Date & Time`, pass `--address-col` / `--date-col`.
* **LLM corrector: `pull model manifest: file does not exist`** — The model tag doesn't exist in Ollama's library. List installed models with `ollama list` and the registry at ollama.com/library; pass a real tag via `--model` / `--fast-model` / `--escalate-model`.
* **LLM corrector: `address already in use` on `ollama serve`** — Ollama is already running (often the menu-bar app supervising it). Either use the running server as-is, or quit the app and set the parallel variables with `launchctl setenv` before relaunching. The corrector works against the already-running server regardless.
* **LLM corrector is slow / requests seem to queue** — Ollama serializes requests unless started with `OLLAMA_NUM_PARALLEL`. Set it to at least your `--concurrency` value. For single-digit flag counts the difference is negligible; it matters only on large batches.
* **LLM correction wrote a wrong-looking value** — Every LLM-written cell is tagged in `_corrected_by`. Filter on it to audit or revert. Always sample with `--limit` before a full run; the deterministic pass should handle the bulk first.
* **Output folders are safe to re-run** — All parsers ignore PDFs already inside `results_*` directories.