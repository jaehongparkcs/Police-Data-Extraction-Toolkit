# Police Data Extraction Toolkit

Automated Python tools for parsing police department PDF logs into structured CSV datasets. Includes two parsers:

1. **Arrest Log Parser** — extracts individual arrest charge records from Porterville, CA and Torrance, CA media arrest summaries.
2. **Dispatch Log Parser** — extracts calls-for-service / dispatch records from Marlborough, MA and Brea, CA press logs.

---

## Project Structure

```text
arrest-data-conversion/
│
├── batch_parse_arrest_logs.py          # Arrest log extraction script
├── batch_parse_dispatch_logs.py        # Dispatch / calls-for-service extraction script
├── requirements.txt                    # Python package dependencies
├── README.md
│
├── Input_Porterville_Logs/             # Porterville arrest PDFs
├── Input_Torrance_Logs/                # Torrance arrest PDFs
├── Input_Marlborough_Logs/             # Marlborough dispatch/press log PDFs
├── Input_Brea_Logs/                    # Brea calls-for-service PDFs
│
├── results_Porterville_CA/             # Generated Porterville arrest CSVs
├── results_Torrance_CA/                # Generated Torrance arrest CSVs
├── results_Marlborough_MA/             # Generated Marlborough dispatch CSVs
└── results_Brea_CA/                    # Generated Brea dispatch CSVs
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

Tesseract is only needed if processing scanned PDFs (Brea calls-for-service). Marlborough, Porterville, and Torrance PDFs have text layers and do not require it.

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
```

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

## Troubleshooting

* **`ModuleNotFoundError: No module named 'pdfplumber'`** — Activate the conda environment first: `conda activate arrest_parser`
* **`FileNotFoundError: No such file or directory: 'pdftotext'`** — Install poppler (`brew install poppler`) or ignore this; the script falls back to pdfplumber automatically.
* **`TesseractNotFoundError`** — Install Tesseract (`brew install tesseract`). Only needed for Brea PDFs.
* **Arrest parser skips a PDF** — Verify the filename matches an expected pattern (see table above).
* **Dispatch parser detects wrong layout** — Brea is only detected for files named `CALLS*FOR*SERVICE*`. All other PDFs default to Marlborough.
* **Large file crashes / out of memory** — Use `--no-master` to skip the combined CSV, or `--pages N` to limit processing. Each file still gets its own CSV even if another file crashes.
* **OCR address errors in Brea CSV** — Scanned PDFs produce OCR noise, especially on redacted (blacked-out) fields. The parser filters most garbage but some garbled addresses are expected. Entries with unreadable timestamps/dates are dropped rather than concatenated into adjacent rows.
* **Output folders are safe to re-run** — Both parsers ignore PDFs already inside `results_*` directories.