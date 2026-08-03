# Police Department Media Arrest Summary Parser

An automated Python tool designed to parse, categorize, and extract arrest charge records from **Porterville, CA** and **Torrance, CA** Police Department Media Arrest Summary PDFs. 

The script recursively scans your project folders, identifies which police department convention each PDF belongs to, extracts every individual charge into structured observations, and outputs both individual CSVs and a master combined dataset for each jurisdiction.

---

## Project Structure

```text
your_project_folder/
│
├── batch_parse_arrest_logs.py      # Main extraction script
├── requirements.txt                # Python package dependencies
├── README.md                       # Project documentation
│
├── Input_Porterville_Logs/         # Place Porterville PDFs here (or in any subfolder)
│   └── 010125 TO 022825 ARREST LOG_Redacted.pdf
│
├── Input_Torrance_Logs/            # Place Torrance PDFs here (or in any subfolder)
│   └── Arrest_Log_-_2021-01_JAN_Redacted.pdf
│
├── results_Porterville_CA/         # Automatically generated Porterville CSVs
└── results_Torrance_CA/            # Automatically generated Torrance CSVs

```

---

## Requirements & Environment Setup

This project uses **Conda** to manage the Python environment and **pip** to install dependencies from `requirements.txt`.

### 1. Prerequisites

* [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or [Anaconda](https://www.anaconda.com/) installed on your system.

### 2. Create `requirements.txt`

Ensure you have a file named `requirements.txt` in your project root containing the following dependencies:

```text
pdfplumber>=0.10.0
pandas>=2.0.0

```

### 3. Conda Environment Setup Instructions

Open your terminal (or Anaconda Prompt on Windows), navigate to your project directory, and run the following commands:

```bash
# 1. Create a clean Conda environment with Python 3.10
conda create -n arrest_parser python=3.10 -y

# 2. Activate the environment
conda activate arrest_parser

# 3. Install required Python packages via pip inside Conda
pip install -r requirements.txt

```

> **Note:** We use `pip install -r requirements.txt` inside an active Conda environment because `pdfplumber` is most actively maintained on PyPI, ensuring you get the most compatible version without altering your system Python.

---

## How to Use

### 1. Add Your PDF Files

Place your `.pdf` arrest logs anywhere inside the project folder or within organized subfolders (e.g., `Input_Porterville_Logs/` or `Input_Torrance_Logs/`). The script recursively scans all subdirectories automatically.

### 2. Run the Parser

With your `arrest_parser` Conda environment activated, execute the script from the root directory:

```bash
python batch_parse_arrest_logs.py

```

### 3. Review the Results

Once processing is complete, the script will automatically create two output directories:

* **`results_Porterville_CA/`**: Contains individual CSV files for each processed Porterville log and a master file named **`MASTER_Porterville_CA_Arrest_Charges.csv`**.
* **`results_Torrance_CA/`**: Contains individual CSV files for each processed Torrance log and a master file named **`MASTER_Torrance_CA_Arrest_Charges.csv`**.

---

## Extracted Data Fields

Each generated CSV observation represents a **single arrest charge** and includes the following standardized columns:

| Field Name | Description |
| --- | --- |
| **Arrest ID Number** | Agency booking/arrest identification number |
| **Arrestee Name** | Full name of the individual arrested |
| **Race / Ethnicity / Gender** | Demographic fields (default: `N/A` if unlisted in summary) |
| **Age** | Automatically calculated from Birth Date and Arrest Date |
| **Home Address** | Street address of the arrestee (often redacted by agency) |
| **Offense and Charge Description** | Combined offense description and statutory code |
| **Charge Level** | Misdemeanor (`M`), Felony (`F`), or Infraction (`I`) classification |
| **Time and Date of Arrest** | Timestamp in `HH:MM:SS MM/DD/YY` format |
| **Location of Arrest** | Geographic address or coordinates where the arrest occurred |
| **Crime Incident Report ID Number** | Related police agency incident/case number |
| **Police Division / Beat** | Agency sector/area command code |
| **Police Department** | Standardized agency name (`Porterville Police Department, CA` or `Torrance Police Department, CA`) |
| **Source PDF File** | Original PDF filename for auditability and verification |

---

## Troubleshooting

* **`ModuleNotFoundError: No module named 'pdfplumber'`**: Ensure you activated your Conda environment (`conda activate arrest_parser`) before running the script.
* **Uncategorized Files Skipped**: If a PDF is skipped, verify that its filename matches one of the expected agency naming conventions:
* Porterville: `MMDDYY TO MMDDYY*.pdf` (e.g., `010125 TO 022825 ARREST LOG_Redacted.pdf`)
* Torrance: `Arrest_Log_-_YYYY-MM*.pdf` (e.g., `Arrest_Log_-_2021-01_JAN_Redacted.pdf`)


* **Output Folders Skipped Safely**: The script is designed to ignore any PDFs already sitting inside the `results_*` directories so that it will not re-process output files if run multiple times.
