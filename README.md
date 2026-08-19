# Police Data Extraction Toolkit

Automated Python tools for parsing police department PDF logs into structured CSV datasets. Includes three parsers:

1. **Arrest Log Parser** — extracts individual arrest charge records from Porterville, CA and Torrance, CA media arrest summaries.
2. **Dispatch Log Parser** — extracts calls-for-service / dispatch records from Marlborough, MA and Brea, CA press logs.
3. **Universal Tabular Parser** — extracts rows from any text-layer tabular PDF (no OCR), auto-detecting columns from the document itself. Tested on Cleveland, OH (incident), Oceanside, CA (arrest), and Bainbridge Island, WA (incident). Includes an automatic review-flagging system that marks suspicious rows for manual checking.

---

## Project Structure

```text
arrest-data-conversion/
│
├── batch_parse_arrest_logs.py          # Arrest log extraction script
├── batch_parse_dispatch_logs.py        # Dispatch / calls-for-service extraction script
├── batch_parse_tabular_pdfs.py         # Universal tabular PDF extraction script
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

Any other clean, text-layer tabular PDF should also work; see *Reliability & Review Flags* below.

### Two-Tier Extraction Strategy

1. **Rect-based** — if the PDF has real table cell borders (lines/rectangles), those are used as exact column and row boundaries. Pixel-perfect. (Oceanside.)
2. **Word-position** — if there are no borders, columns are detected from header word positions using adaptive gap analysis, with a space-glyph signal to keep overflowing/wrapping text in the correct cell. (Cleveland, Bainbridge.)

The strategy is chosen automatically per file.

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

Flag thresholds are relative to what each column normally contains in the same file, so the system adapts to new departments rather than assuming a fixed format.

**Recommended workflow:** run the parser, open `<filename>_REVIEW.csv`, and verify or fix only those rows (or filter `_needs_review = YES` in the main CSV). On the tested samples this reduced manual review to a handful of rows: 2 of 370 for Cleveland, 0 for Oceanside, 0 for Bainbridge.

**Caveat:** the flagger catches *structural* anomalies (misplaced, garbled, or overlapping text). It cannot catch an error where a wrong value still looks plausible and lands in a reasonable-looking column. For a brand-new department, the first run still deserves a manual spot-check beyond just the flagged rows.

### Extracted Fields

The tabular parser does not impose a fixed schema — it outputs whatever columns the PDF's header defines, plus the two review columns. For the tested departments the detected columns are:

| Department | Columns |
| --- | --- |
| Cleveland, OH | Case Number, Address, Date & Time, Offense Description, Case Disposition, PD District |
| Oceanside, CA | Incident Type, Incident Number, Incident Date, Violation Type, Violation Section, Violation Description, Location |
| Bainbridge Island, WA | call_id, actdate, acttime, streetnbr, street, geox, geoy, naturecode |

---

## Troubleshooting

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
* **Output folders are safe to re-run** — All parsers ignore PDFs already inside `results_*` directories.