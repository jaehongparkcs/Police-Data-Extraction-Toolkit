import re
from datetime import datetime
from pathlib import Path
import pandas as pd
import pdfplumber


def calculate_age(birth_date_str, arrest_date_str):
    """Calculates age at the time of arrest given MM/DD/YY strings."""
    try:
        b_date = datetime.strptime(birth_date_str.strip(), "%m/%d/%y")
        a_date = datetime.strptime(
            arrest_date_str.strip().split()[1], "%m/%d/%y"
        )

        if b_date > a_date:
            b_date = b_date.replace(year=b_date.year - 100)

        age = (
            a_date.year
            - b_date.year
            - ((a_date.month, a_date.day) < (b_date.month, b_date.day))
        )
        return age
    except (ValueError, IndexError, AttributeError):
        return "N/A"


def parse_single_pdf(pdf_path, department_name):
    """Parses a single PDF and returns a list of dictionaries (one per charge)."""
    all_rows = []

    # Regex patterns for header metadata
    re_arrest_time = re.compile(
        r"Arrest Time/Date:\s*(\d{2}:\d{2}:\d{2}\s+\d{2}/\d{2}/\d{2})"
    )
    re_inmate_name = re.compile(r"Inmate Name:\s*(.+?)(?:\s+Birth Date:|$)")
    re_birth_date = re.compile(r"Birth Date:\s*(\d{2}/\d{2}/\d{2})")
    re_address = re.compile(r"Address:\s*(.*)")
    re_location = re.compile(
        r"Arrest Location:\s*(.*?)(?:\s+Arrest Number:|$)"
    )
    re_arrest_num = re.compile(r"Arrest Number:\s*(\S+)")
    re_incidents = re.compile(r"Related Incidents:\s*(.*)")

    # Regex pattern for charge table rows
    re_charge_start = re.compile(
        r"^(\d{2}:\d{2}:\d{2}\s+\d{2}/\d{2}/\d{2})\s+(.*?)\s+(SEC\s*\d+|\S+)\s+(.*?)\s+([A-Z]{4})\s+([A-Z])$"
    )

    with pdfplumber.open(pdf_path) as pdf:
        current_arrest = {}
        current_charge = None

        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue

            lines = text.split("\n")
            for line in lines:
                line_clean = line.strip()

                # Skip page headers/footers
                if any(
                    skip in line_clean
                    for skip in [
                        "POLICE DEPARTMENT",
                        "Media Arrest Summary",
                        "REDACTED COPY",
                        "PER GOVT CODE",
                        "Page ",
                    ]
                ):
                    continue

                # 1. Detect start of a new arrest record
                if "Arrest Time/Date:" in line_clean:
                    current_arrest = {
                        "Arrest ID Number": "N/A",
                        "Arrestee Name": "N/A",
                        "Race": "N/A",
                        "Ethnicity": "N/A",
                        "Gender": "N/A",
                        "Age": "N/A",
                        "Home Address": "N/A",
                        "Time and Date of Arrest": "N/A",
                        "Location of Arrest (Address and/or Geographic Coordinates)": "N/A",
                        "Crime Incident Report ID Number (if available)": "N/A",
                        "Police Division": "N/A",
                        "Police Beat": "N/A",
                        "Police Department": department_name,
                        "Source PDF File": pdf_path.name,
                    }
                    match = re_arrest_time.search(line_clean)
                    if match:
                        current_arrest["Time and Date of Arrest"] = match.group(
                            1
                        )
                    continue

                # 2. Extract arrest header attributes
                if "Inmate Name:" in line_clean:
                    match = re_inmate_name.search(line_clean)
                    if match:
                        current_arrest["Arrestee Name"] = match.group(1).strip()
                    match_bd = re_birth_date.search(line_clean)
                    if match_bd:
                        current_arrest["Age"] = calculate_age(
                            match_bd.group(1),
                            current_arrest.get("Time and Date of Arrest", ""),
                        )
                    continue

                if "Address:" in line_clean:
                    match = re_address.search(line_clean)
                    if match and match.group(1).strip():
                        current_arrest["Home Address"] = match.group(1).strip()
                    continue

                if "Arrest Location:" in line_clean:
                    match_loc = re_location.search(line_clean)
                    if match_loc:
                        current_arrest[
                            "Location of Arrest (Address and/or Geographic Coordinates)"
                        ] = match_loc.group(1).strip()
                    match_num = re_arrest_num.search(line_clean)
                    if match_num:
                        current_arrest["Arrest ID Number"] = match_num.group(
                            1
                        ).strip()
                    continue

                if "Related Incidents:" in line_clean:
                    match = re_incidents.search(line_clean)
                    if match and match.group(1).strip():
                        current_arrest[
                            "Crime Incident Report ID Number (if available)"
                        ] = match.group(1).strip()
                    continue

                # 3. Detect charge rows
                charge_match = re_charge_start.match(line_clean)
                if charge_match:
                    if current_charge:
                        all_rows.append(current_charge)

                    (
                        c_time,
                        offense,
                        area,
                        statute,
                        court,
                        c_class,
                    ) = charge_match.groups()
                    current_charge = {
                        **current_arrest,
                        "Offense and Charge Description": f"{offense.strip()} ({statute.strip()})",
                        "Charge Level (Misdemeanor/Felony Class)": c_class.strip(),
                        "Police Division": area.strip(),
                    }
                    continue

                # 4. Handle wrapped multi-line charge descriptions
                if (
                    current_charge
                    and "Time/Date" not in line_clean
                    and "Booking Number:" not in line_clean
                ):
                    current_charge[
                        "Offense and Charge Description"
                    ] += f" {line_clean}"

        if current_charge:
            all_rows.append(current_charge)

    return all_rows


def categorize_and_process_directory(input_dir="."):
    input_path = Path(input_dir)

    # Output directory paths explicitly named for Porterville, CA and Torrance, CA
    porterville_dir = input_path / "results_Porterville_CA"
    torrance_dir = input_path / "results_Torrance_CA"
    porterville_dir.mkdir(exist_ok=True)
    torrance_dir.mkdir(exist_ok=True)

    # Regex patterns for the two different file conventions
    # Porterville, CA: MMDDYY TO MMDDYY ARREST LOG_Redacted.pdf
    pattern_porterville = re.compile(
        r"^\d{6}\s+TO\s+\d{6}.*\.pdf$", re.IGNORECASE
    )
    # Torrance, CA: Arrest_Log_-_YYYY-MM_MONTH_Redacted.pdf
    pattern_torrance = re.compile(
        r"^Arrest_Log_-_20\d{2}-\d{2}.*\.pdf$", re.IGNORECASE
    )

    columns_order = [
        "Arrest ID Number",
        "Arrestee Name",
        "Race",
        "Ethnicity",
        "Gender",
        "Age",
        "Home Address",
        "Offense and Charge Description",
        "Charge Level (Misdemeanor/Felony Class)",
        "Time and Date of Arrest",
        "Location of Arrest (Address and/or Geographic Coordinates)",
        "Crime Incident Report ID Number (if available)",
        "Police Division",
        "Police Beat",
        "Police Department",
        "Source PDF File",
    ]

    all_porterville_rows = []
    all_torrance_rows = []

    # Recursively find all PDFs in subfolders using rglob
    all_pdfs = list(input_path.rglob("*.pdf"))

    # Filter out any PDFs that happen to be inside our output folders
    valid_pdfs = [
        p
        for p in all_pdfs
        if "results_Porterville_CA" not in p.parts
        and "results_Torrance_CA" not in p.parts
    ]

    print(f"Found {len(valid_pdfs)} PDF file(s) across subfolders.\n")

    for pdf_file in valid_pdfs:
        filename = pdf_file.name

        # Identify Porterville, CA logs
        if pattern_porterville.match(filename):
            dept_name = "Porterville Police Department, CA"
            print(f"[Porterville, CA] Processing: {filename}")
            rows = parse_single_pdf(pdf_file, dept_name)
            all_porterville_rows.extend(rows)

            # Save individual file CSV
            df_single = pd.DataFrame(rows)
            for col in columns_order:
                if col not in df_single.columns:
                    df_single[col] = "N/A"
            df_single[columns_order].to_csv(
                porterville_dir / f"{pdf_file.stem}.csv", index=False
            )

        # Identify Torrance, CA logs
        elif pattern_torrance.match(filename):
            dept_name = "Torrance Police Department, CA"
            print(f"[Torrance, CA] Processing: {filename}")
            rows = parse_single_pdf(pdf_file, dept_name)
            all_torrance_rows.extend(rows)

            # Save individual file CSV
            df_single = pd.DataFrame(rows)
            for col in columns_order:
                if col not in df_single.columns:
                    df_single[col] = "N/A"
            df_single[columns_order].to_csv(
                torrance_dir / f"{pdf_file.stem}.csv", index=False
            )

        else:
            print(
                f"[Uncategorized] Skipped '{filename}' (Does not match either naming convention)."
            )

    # Export combined master CSVs for each city
    if all_porterville_rows:
        df_porterville = pd.DataFrame(all_porterville_rows)
        for col in columns_order:
            if col not in df_porterville.columns:
                df_porterville[col] = "N/A"
        master_port_path = (
            porterville_dir / "MASTER_Porterville_CA_Arrest_Charges.csv"
        )
        df_porterville[columns_order].to_csv(master_port_path, index=False)
        print(
            f"\n--> Porterville Master CSV saved: {master_port_path} ({len(df_porterville)} total charges)"
        )

    if all_torrance_rows:
        df_torrance = pd.DataFrame(all_torrance_rows)
        for col in columns_order:
            if col not in df_torrance.columns:
                df_torrance[col] = "N/A"
        master_torr_path = (
            torrance_dir / "MASTER_Torrance_CA_Arrest_Charges.csv"
        )
        df_torrance[columns_order].to_csv(master_torr_path, index=False)
        print(
            f"--> Torrance Master CSV saved: {master_torr_path} ({len(df_torrance)} total charges)"
        )


if __name__ == "__main__":
    # "." tells the script to recursively scan the current directory and all its subfolders.
    categorize_and_process_directory(".")