#!/usr/bin/env python3
"""
batch_parse_dispatch_logs.py
============================
Converts police dispatch / press-log PDFs from **Brea PD** and
**Marlborough PD** into a single tidy CSV, one row per dispatch call.

Two department layouts are auto-detected:

  * Brea PD         -- scanned "Law Incident Summary Report, by Incident Number"
                       Columns: Number | Time and Date | Nature | Address | Location | Dsp
                       (requires OCR -- Tesseract must be installed)

  * Marlborough PD  -- text-layer "PRESS LOG" / "Dispatch Log"
                       Columns: Time | Call Reason | Action | Location/Address
                       Three known sub-formats (2016, 2025, 2026) all share the
                       same layout and are handled identically.

Usage:
    python3 batch_parse_dispatch_logs.py /path/to/pdf_dir --out /path/to/out_dir

Requirements:
    pip install pdfplumber pytesseract pillow pandas --break-system-packages
    Tesseract binary must be installed and on PATH (only needed for Brea PDFs).
"""

import argparse
import re
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------
# Output schema  (matches the user's requested CSV columns)
# --------------------------------------------------------------------------
COLUMNS_ORDER = [
    "Dispatch Call ID Number",
    "Dispatch Code",
    "Call Description",
    "Priority Level of Call",
    "Call Disposition",
    "Crime Incident Report Written",
    "Crime Incident ID",
    "Address Street",
    "Address City",
    "Address Zip",
    "Geographic Coordinates",
    "Police Division",
    "Police Beat",
    "Time of Call",
    "Time of Dispatch",
    "Time of Arrival",
    "Date of Call",
    "Source PDF File",
    "Source Page",
]

WS = re.compile(r"\s+")


def norm(s: str) -> str:
    """Collapse whitespace to single spaces and strip."""
    return WS.sub(" ", s or "").strip()


# ══════════════════════════════════════════════════════════════════════════
#  MARLBOROUGH  PD  PARSER  (text-layer PDFs)
# ══════════════════════════════════════════════════════════════════════════

# Header printed at the top of each page
MAR_PAGE_HEADER = re.compile(
    r"Marlborough\s+Police\s+Department\s+PRESS\s+LOG\s+Page:\s*\d+",
    re.IGNORECASE,
)
MAR_SUB_HEADER = re.compile(
    r"(?:Dispatch|Selective)\s+(?:Log|Search)\s+From:",
    re.IGNORECASE,
)
MAR_COL_HEADER = re.compile(r"^\s*Time\s+Call\s+Reason\s+Action\s*$", re.IGNORECASE)

# "For Date: 01/01/2026 - Thursday"
MAR_DATE_LINE = re.compile(
    r"For\s+Date:\s*(\d{2}/\d{2}/\d{4})\s*-\s*\w+", re.IGNORECASE
)

# A new call entry: optional leading whitespace then a 4-digit time (HHMM)
MAR_ENTRY_TIME = re.compile(r"^\s*(\d{4})\s+(.+)$")

# Location line
MAR_LOCATION = re.compile(
    r"^\s*(?:Location/Address|Vicinity\s+of)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE
)

# Arrest / P/C block lines
MAR_REFER_TO = re.compile(
    r"^\s*Refer\s+To\s+(?:Arrest|P/C)\s*:\s*(.+)$", re.IGNORECASE
)
MAR_ARREST_NAME = re.compile(
    r"^\s*(?:Arrest|P/C)\s*:\s*(.+)$", re.IGNORECASE
)

# Dispositions that indicate a report was written
REPORT_DISPOSITIONS = {
    "report", "criminal complaint", "arrest(s) made",
}




# Known Marlborough dispositions / actions (matched case-insensitively
# against the end of the combined time-line).  Ordered longest-first so
# we don't accidentally match a suffix of a longer action.
_MAR_ACTIONS_RAW = [
    "Building Checked/Secured",
    "Services Rendered- PD",
    "Services Rendered- FD",
    "Transported to Hospital",
    "Medical Assistance Rendered",
    "Arrest(s) Made",
    "CRIMINAL COMPLAINT",
    "Citation/Warning Issued",
    "No Action Required",
    "False Alarm, Accidental",
    "Taken/Referred to Other Agency",
    "Could Not Locate",
    "Vehicle Towed",
    "System malfunction",
    "Field Interrogation",
    "Peace Restored",
    "Extinguished",
    "Investigated",
    "Private tow",
    "Unfounded",
    "Report",
]
# Build a compiled regex that matches any of them at the end of a string
_MAR_ACTION_PATTERN = re.compile(
    r"\s+(" + "|".join(re.escape(a) for a in _MAR_ACTIONS_RAW) + r")\s*$",
    re.IGNORECASE,
)


def _split_reason_action(text: str):
    """Split a combined 'CALL_REASON  ...  Action' line.

    Two strategies (tried in order):
      1. If there is a wide gap (4+ spaces), split on the last one.
      2. Fall back to matching known disposition strings at line end.
    """
    # Strategy 1: layout-preserved text from pdftotext
    gaps = list(re.finditer(r" {4,}", text))
    if gaps:
        best = gaps[-1]
        reason = text[: best.start()].strip()
        action = text[best.end():].strip()
        if action:
            return reason, action

    # Strategy 2: known dispositions at end of line
    m = _MAR_ACTION_PATTERN.search(text)
    if m:
        reason = text[: m.start()].strip()
        action = m.group(1).strip()
        return reason, action

    return text.strip(), ""


def _parse_mar_address(raw: str):
    """Parse a Marlborough location string into street and place-name parts.

    Examples:
        '[MAR 786] OFFICE BUILDING - 181 CEDAR HILL ST'
        '13 VIOLETWOOD CIR'
        '643 LINCOLN ST @ 169 LAKESIDE AVE'
        '[MAR 871] ROYAL CREST APARTMENTS - 14 ROYAL CREST DR Apt. #6'
    """
    # Strip the [MAR xxx] tag and place name before the dash
    addr = raw.strip()
    m = re.match(r"\[MAR\s+\d+\]\s*[^-]*-\s*(.+)", addr)
    if m:
        addr = m.group(1).strip()
    return addr


def parse_marlborough_text(full_text: str, source_name: str):
    """Parse the full text of a Marlborough press-log PDF into rows."""
    rows = []
    current_date = ""
    current_entry = None
    current_page = 1

    lines = full_text.split("\n")

    def _flush(entry):
        """Finalise and emit one entry."""
        if not entry:
            return
        reason = entry.get("reason", "")
        action = entry.get("action", "")
        addr_raw = entry.get("address", "")
        addr_street = _parse_mar_address(addr_raw) if addr_raw else ""
        refer_ids = entry.get("refer_ids", [])

        action_lower = action.strip().lower()
        has_report = "Yes" if action_lower in REPORT_DISPOSITIONS else "No"
        crime_id = "; ".join(refer_ids) if refer_ids else ""
        if refer_ids:
            has_report = "Yes"

        rows.append({
            "Dispatch Call ID Number": "",  # Marlborough logs don't have incident numbers
            "Dispatch Code": "",            # no numeric codes
            "Call Description": reason,
            "Priority Level of Call": "",
            "Call Disposition": action,
            "Crime Incident Report Written": has_report,
            "Crime Incident ID": crime_id,
            "Address Street": addr_street,
            "Address City": "Marlborough",
            "Address Zip": "",
            "Geographic Coordinates": "",
            "Police Division": "Marlborough PD",
            "Police Beat": "",
            "Time of Call": entry.get("time", ""),
            "Time of Dispatch": "",
            "Time of Arrival": "",
            "Date of Call": entry.get("date", ""),
            "Source PDF File": source_name,
            "Source Page": entry.get("page", ""),
        })

    for line in lines:
        # Track page breaks (form feed)
        if "\f" in line:
            current_page += line.count("\f")
            line = line.replace("\f", "")

        # Skip boilerplate headers
        if MAR_PAGE_HEADER.search(line):
            continue
        if MAR_SUB_HEADER.search(line):
            continue
        if MAR_COL_HEADER.match(line):
            continue
        if not line.strip():
            continue

        # Date header
        dm = MAR_DATE_LINE.search(line)
        if dm:
            _flush(current_entry)
            current_entry = None
            current_date = dm.group(1)
            continue

        # New call entry (time + reason/action on same line)
        em = MAR_ENTRY_TIME.match(line)
        if em:
            _flush(current_entry)
            time_str = em.group(1)
            rest = em.group(2)
            reason, action = _split_reason_action(rest)
            current_entry = {
                "time": f"{time_str[:2]}:{time_str[2:]}",
                "date": current_date,
                "reason": reason,
                "action": action,
                "address": "",
                "refer_ids": [],
                "page": current_page,
            }
            continue

        # Location/Address line
        lm = MAR_LOCATION.match(line)
        if lm and current_entry is not None:
            current_entry["address"] = lm.group(1).strip()
            continue

        # Refer To Arrest / P/C
        rm = MAR_REFER_TO.match(line)
        if rm and current_entry is not None:
            current_entry["refer_ids"].append(rm.group(1).strip())
            continue

        # Arrest name, address, age, charges lines -- skip (these are
        # arrest details; the dispatch row already captures the refer ID)
        if current_entry is not None:
            if MAR_ARREST_NAME.match(line):
                continue
            if re.match(r"^\s+Address:", line, re.IGNORECASE):
                continue
            if re.match(r"^\s+Age:", line, re.IGNORECASE):
                continue
            if re.match(r"^\s+Charges:", line, re.IGNORECASE):
                continue
            # Continuation charge lines (indented text after Charges:)
            if re.match(r"^\s{20,}\S", line):
                continue

    _flush(current_entry)
    return rows


def process_marlborough_pdf(pdf_path: Path, max_pages: int = 0):
    """Extract text from a Marlborough press-log PDF and parse it.

    Tries three strategies in order:
      1. pdftotext -layout  (fastest, preserves column gaps)
      2. pdfplumber text extraction  (no system dependency)
      3. OCR via Tesseract  (for scanned Marlborough PDFs)
    """
    import shutil
    import subprocess

    print(f"[TEXT] {pdf_path.name} ({pdf_path.stat().st_size / 1e6:.1f} MB) ...")

    full_text = ""

    # ── Strategy 1: pdftotext (fast, preserves layout gaps) ──────────
    if shutil.which("pdftotext"):
        cmd = ["pdftotext", "-layout"]
        if max_pages > 0:
            cmd += ["-l", str(max_pages)]
        cmd += [str(pdf_path), "-"]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and result.stdout.strip():
            full_text = result.stdout

    # ── Strategy 2: pdfplumber text extraction ───────────────────────
    if not full_text.strip():
        import pdfplumber

        print(f"  (trying pdfplumber text extraction ...)")
        texts = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            total = len(pdf.pages)
            limit = max_pages if max_pages > 0 else total
            for idx, page in enumerate(pdf.pages[:limit]):
                text = page.extract_text() or ""
                texts.append(text)
                if (idx + 1) % 200 == 0:
                    print(f"   ...page {idx + 1}/{total}")
        full_text = "\n\f\n".join(texts)

    # ── Strategy 3: OCR (for scanned Marlborough PDFs) ───────────────
    if not full_text.strip():
        try:
            import pdfplumber
            import pytesseract

            print(f"  (no text layer found — OCR'ing with Tesseract ...)")
            texts = []
            with pdfplumber.open(str(pdf_path)) as pdf:
                total = len(pdf.pages)
                limit = max_pages if max_pages > 0 else total
                for idx, page in enumerate(pdf.pages[:limit]):
                    img = page.to_image(resolution=300).original
                    text = pytesseract.image_to_string(img, config="--psm 6")
                    texts.append(text)
                    if (idx + 1) % 50 == 0:
                        print(f"   ...OCR page {idx + 1}/{total}")
            full_text = "\n\f\n".join(texts)
        except ImportError:
            print(f"[ERR] {pdf_path.name} is scanned but pytesseract is not "
                  f"installed. Install with: pip install pytesseract")
            return []
        except Exception as e:
            print(f"[ERR] OCR failed for {pdf_path.name}: {e}")
            return []

    rows = parse_marlborough_text(full_text, pdf_path.name)
    print(f"[Done] {pdf_path.name}: {len(rows)} dispatch rows")
    return rows


# ══════════════════════════════════════════════════════════════════════════
#  BREA  PD  PARSER  (scanned / OCR PDFs)
# ══════════════════════════════════════════════════════════════════════════

# Brea dispatch codes -> descriptions (common ones)
BREA_NATURE_CODES = {
    "T": "Traffic Stop",
    "Pc": "Penal Code",
    "Ped": "Pedestrian Check",
    "Homeless": "Homeless Related",
    "Outrch": "Outreach",
    "Flag": "Flag Down / Walk-In",
    "Csar": "Community Service Area Request",
    "Lostr": "Lost Report",
    "Vacck": "Vacation Check",
    "Boveh": "Bolo Vehicle",
    "Welfck": "Welfare Check",
    "Hs": "Health & Safety",
}

# Brea dispositions
BREA_DSP_CODES = {
    "ACT": "Active / Handled",
    "UNF": "Unfounded",
    "RTF": "Report to Follow",
    "CIT": "Citation Issued",
    "ARR": "Arrest",
    "GOA": "Gone on Arrival",
    "ADV": "Advised",
    "UTL": "Unable to Locate",
    "CAN": "Cancelled",
}

# Regex to find the start of a Brea incident line.
# OCR may insert noise chars (=, |, ~, ,) around the incident number.
# Format: 2604-1116  00:23:07 04/16/26  586  <address>  <location>  ACT
#
# The time and date regexes are intentionally lenient because Tesseract
# frequently garbles digits/punctuation:
#   time:  14:333112, WBTAG, 18:52:54  →  \d{1,2}[:\.]?\d{2}[:\.]?\d{0,4}
#   date:  W/LL/25, 1V11A5, LI/LM/25  →  \S{4,10}  (just "some token")
BREA_LINE_START = re.compile(
    r"[,|=~]?\s*(\d{4}[-~—]?\d{3,5})\s+[=|]?\s*"  # incident number
    r"(\d{1,2}:\d{2}:\d{2})\s+"                  # time HH:MM:SS (strict)
    r"(\d{2}/\d{2}/\d{2,4})\s+"                  # date MM/DD/YY (strict)
    r"(\S+)\s+"                                   # nature code
    r"(.+)$"                                       # rest of line
)

# Lenient variant: accepts OCR-mangled times and dates so we can at
# least detect the entry boundary (even if time/date are garbage).
BREA_LINE_START_LENIENT = re.compile(
    r"[,|=~]?\s*(\d{4}[-~—]?\d{3,5})\s+[=|:.]?\s*"  # incident number
    r"(\S{5,8})\s+"                                # time-ish token
    r"(\S{4,10})\s+"                               # date-ish token
    r"(\S+)\s+"                                    # nature code
    r"(.+)$"                                        # rest of line
)

# Pattern to detect an incident number anywhere in a line — used to
# guard the continuation handler against swallowing garbled entries.
# OCR sometimes renders the dash as ~, —, period, or even a space.
BREA_INCIDENT_EMBEDDED = re.compile(r"\d{4}[-~—.]\d{3,5}")

# More reliable guard: a time (HH:MM:SS) followed by a date-ish token
# (MM/DD/YY). Every Brea entry has this, and OCR almost always gets the
# colons right even when it garbles the digits around them.  This
# catches entries whose incident *number* is garbled beyond recognition
# (e.g. "QSi11-1277", "FC a —") but whose time+date are still readable.
BREA_TIME_DATE_EMBEDDED = re.compile(
    r"\d{1,2}:\d{2}:\d{2}\s+\d{1,2}/\d{1,2}/\d{2,4}"
)

def _is_ocr_garbage(line: str) -> bool:
    """Return True if a line is mostly OCR noise from redacted blocks.

    Redacted (blacked-out) areas produce two kinds of noise:
      1. Long runs of random characters (e.g. "iiemedniaadanad...")
      2. Clusters of short nonsense tokens (e.g. "eer eee nen mi ee Re")

    Real address continuations are short (1-5 tokens, mostly
    recognizable street words).
    """
    if len(line) < 30:
        return False
    tokens = line.split()
    if not tokens:
        return False

    # Check 1: any single very long nonsense token
    if any(len(t) > 40 for t in tokens):
        return True

    # Check 2: many tokens and most are short nonsense
    if len(tokens) >= 8:
        # Common street abbreviations that ARE legitimate short tokens
        legit_short = {
            "st", "ave", "dr", "rd", "ln", "ct", "pl", "blvd", "hwy",
            "cir", "way", "n", "s", "e", "w", "ne", "nw", "se", "sw",
            "apt", "ste", "ca", "and", "at", "the", "of", "in", "or",
        }
        nonsense = sum(
            1 for t in tokens
            if len(t) <= 4 and t.lower() not in legit_short
            and not t.isdigit()
        )
        if nonsense > len(tokens) * 0.5:
            return True

    # Check 3: many long gibberish tokens (> 15 chars, no real words)
    garbage_long = sum(
        1 for t in tokens
        if len(t) > 15 and not re.search(r"[A-Z]{2,}", t)
    )
    if garbage_long > len(tokens) * 0.3:
        return True

    return False

# Brea location-beat codes look like: 13, GB13, DTW13, CH12, BM13,
# BLS12, EH12, LF12, CC13, NH11, etc.
BREA_BEAT_RE = re.compile(r"^[A-Z]{0,4}\d{1,2}$", re.IGNORECASE)

# Brea dispositions
BREA_DSP_VALUES = {"ACT", "UNF", "RTF", "CIT", "ARR", "GOA", "ADV",
                   "UTL", "CAN", "CLR", "ARA", "REF", "DUP", "INF"}
BREA_DSP_RE = re.compile(r"^(?:" + "|".join(BREA_DSP_VALUES) + r")$", re.IGNORECASE)


def _fix_ocr_beat(beat: str) -> str:
    """Fix common OCR misreads of Brea beat codes.
    
    Tesseract often reads '1' as 'I' in alphanumeric codes.
    """
    if not beat:
        return beat
    # Common fixes: CHI2->CH12, DTWI3->DTW13, EHI2->EH12, etc.
    fixed = re.sub(r"I(?=\d)", "1", beat)  # I before digit -> 1
    fixed = re.sub(r"(?<=\d)I", "1", fixed)  # digit then I -> 1
    return fixed


def _clean_ocr_address(addr: str) -> str:
    """Clean OCR noise from Brea addresses."""
    # Remove leading/trailing pipe chars (from redacted fields)
    addr = re.sub(r"^\s*[|]\s*", "", addr)
    addr = re.sub(r"\s*[|]\s*$", "", addr)
    # Remove stray single-char OCR noise at start
    addr = re.sub(r"^[|~=;,]\s+", "", addr)
    return addr.strip()


def _clean_ocr_line(line: str) -> str:
    """Remove common OCR artefacts from a Brea line."""
    # Strip leading OCR junk (|, ~, ;, commas from badge/logo)
    line = re.sub(r"^[\s|~;,=]+", "", line)
    # Remove stray = that OCR inserts between fields
    line = re.sub(r"\s+=\s+", " ", line)
    # Remove isolated pipe/bar chars
    line = re.sub(r"\s+\|\s+", " ", line)
    return line.strip()


def _extract_brea_city_zip(address: str):
    """Split 'street, BREA, CA' into (street, city, zip)."""
    m = re.search(
        r",\s*(BREA|DBR|LA HABRA|FULLERTON|PLACENTIA|YORBA LINDA|"
        r"DIAMOND BAR|ROWLAND HEIGHTS|WHITTIER)\s*,?\s*CA\s*$",
        address, re.IGNORECASE,
    )
    if m:
        street = address[: m.start()].strip().rstrip(",")
        city = m.group(1).strip()
        return street, city, ""

    m2 = re.search(r",\s*CA\s*$", address, re.IGNORECASE)
    if m2:
        parts = address[: m2.start()].rsplit(",", 1)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip(), ""
        return address[: m2.start()].strip(), "Brea", ""

    return address, "Brea", ""


def _parse_brea_tail(tail: str):
    """Parse the tail of a Brea incident line: address + beat + dsp.

    Working from right to left: the last token is the disposition (ACT,
    UNF, etc.), the second-to-last is the beat/location code (13, DTW13,
    etc.), and everything before that is the address.
    """
    tokens = tail.split()
    dsp = ""
    beat = ""

    # Pop disposition from the right
    if tokens and BREA_DSP_RE.match(tokens[-1]):
        dsp = tokens.pop().upper()

    # Pop beat code from the right
    if tokens and BREA_BEAT_RE.match(tokens[-1]):
        beat = tokens.pop()

    address = " ".join(tokens)
    return address, beat, dsp


def ocr_brea_pdf(pdf_path: Path, resolution: int = 300):
    """OCR a scanned Brea PDF and yield (page_number, ocr_text)."""
    import pdfplumber
    import pytesseract

    print(f"[OCR] {pdf_path.name} ({pdf_path.stat().st_size / 1e6:.1f} MB) ...")
    with pdfplumber.open(str(pdf_path)) as pdf:
        total = len(pdf.pages)
        for idx, page in enumerate(pdf.pages):
            img = page.to_image(resolution=resolution).original
            text = pytesseract.image_to_string(img, config="--psm 6")
            yield idx + 1, text
            if (idx + 1) % 50 == 0:
                print(f"   ...page {idx + 1}/{total}")


def parse_brea_ocr_text(full_text: str, source_name: str):
    """Parse OCR'd text from a Brea PDF into rows.

    Strategy: scan line by line. When we see a line that starts with an
    incident number (NNNN-NNNN), parse it. Continuation lines (wrapped
    addresses) are appended to the previous entry — but only if they
    don't contain another incident number (which would indicate a
    garbled entry the strict regex couldn't parse).
    """
    rows = []
    pending = None  # partially built entry awaiting address continuation

    for raw_line in full_text.split("\n"):
        line = _clean_ocr_line(raw_line)
        if not line:
            continue

        # ── Try strict regex first ───────────────────────────────────
        m = BREA_LINE_START.match(line)

        # ── Lenient fallback: catches OCR-garbled times/dates ────────
        if not m:
            m = BREA_LINE_START_LENIENT.match(line)
            if m:
                # Validate: the time-ish token should contain at least
                # one colon and digits; the date-ish token at least one
                # slash and digits. Reject pure garbage.
                time_tok = m.group(2)
                date_tok = m.group(3)
                has_time_shape = bool(re.search(r"\d", time_tok) and
                                     re.search(r"[:\.]", time_tok))
                has_date_shape = bool(re.search(r"\d", date_tok) and
                                     re.search(r"[/\-]", date_tok))
                if not (has_time_shape or has_date_shape):
                    m = None  # reject — not plausibly a time/date

        if m:
            # Flush any pending entry
            if pending:
                rows.append(pending)
                pending = None

            inc_num = m.group(1)
            # Normalise incident number: fix OCR dash variants, add
            # dash if missing entirely
            inc_num = re.sub(r"[~—]", "-", inc_num)
            if "-" not in inc_num and len(inc_num) >= 7:
                inc_num = inc_num[:4] + "-" + inc_num[4:]

            time_str = m.group(2)
            date_str = m.group(3)
            nature = m.group(4)
            rest = m.group(5).strip()

            # If the rest-of-line contains additional entries (OCR
            # merged multiple lines), truncate at the first sign of
            # another entry.  Check both incident numbers and time+date
            # patterns since OCR may garble one but not the other.
            cut = None
            em1 = BREA_INCIDENT_EMBEDDED.search(rest)
            em2 = BREA_TIME_DATE_EMBEDDED.search(rest)
            if em1:
                cut = em1.start()
            if em2:
                # Back up a few chars to catch the garbled incident
                # number that precedes the time+date.
                td_cut = max(0, em2.start() - 20)
                if cut is None or td_cut < cut:
                    cut = td_cut
            if cut is not None:
                rest = rest[:cut].strip()

            address, beat, dsp = _parse_brea_tail(rest)
            beat = _fix_ocr_beat(beat)
            address = _clean_ocr_address(address)
            street, city, zipcode = _extract_brea_city_zip(address)
            desc = BREA_NATURE_CODES.get(nature, nature)
            dsp_long = BREA_DSP_CODES.get(dsp, dsp) if dsp else ""

            has_report = "Yes" if dsp in ("RTF", "ARR") else ""

            pending = {
                "Dispatch Call ID Number": inc_num,
                "Dispatch Code": nature,
                "Call Description": desc,
                "Priority Level of Call": "",
                "Call Disposition": f"{dsp} ({dsp_long})" if dsp_long and dsp_long != dsp else dsp,
                "Crime Incident Report Written": has_report,
                "Crime Incident ID": inc_num if has_report == "Yes" else "",
                "Address Street": street,
                "Address City": city,
                "Address Zip": zipcode,
                "Geographic Coordinates": "",
                "Police Division": "Brea PD",
                "Police Beat": beat,
                "Time of Call": time_str,
                "Time of Dispatch": "",
                "Time of Arrival": "",
                "Date of Call": date_str,
                "Source PDF File": source_name,
                "Source Page": "",
            }
        elif pending:
            # Continuation line (wrapped address text).

            # ── Guard: if this line contains another entry (detected
            # by incident number OR time+date pattern), or is OCR
            # garbage from a redacted block, flush pending and skip. ──
            if BREA_INCIDENT_EMBEDDED.search(line):
                rows.append(pending)
                pending = None
                continue
            if BREA_TIME_DATE_EMBEDDED.search(line):
                rows.append(pending)
                pending = None
                continue
            if _is_ocr_garbage(line):
                rows.append(pending)
                pending = None
                continue

            # Skip boilerplate / header / footer lines.
            if re.search(r"Law\s+Incident|BREA\s+POLICE|rplwisr|Page\s+\d+\s+of",
                         line, re.IGNORECASE):
                continue
            if re.match(r"^(?:Number|Agency:)", line, re.IGNORECASE):
                continue
            if re.search(r"Total\s+Incidents|Total\s+reported|Report\s+Includes",
                         line, re.IGNORECASE):
                continue
            if re.search(r"All\s+(?:dates|agencies|officers|dispositions|natures|locations|cities)",
                         line, re.IGNORECASE):
                continue
            if re.search(r"clearance\s+codes|offense\s+codes|circumstance\s+codes",
                         line, re.IGNORECASE):
                continue
            # Append to address (stripping trailing beat/dsp if present)
            extra, extra_beat, extra_dsp = _parse_brea_tail(line)
            if extra:
                old_street = pending["Address Street"]
                combined = (old_street + " " + extra).strip()
                street, city, zipcode = _extract_brea_city_zip(combined)
                pending["Address Street"] = street
                pending["Address City"] = city
            if extra_beat and not pending["Police Beat"]:
                pending["Police Beat"] = extra_beat
            if extra_dsp and not pending["Call Disposition"]:
                dsp_long = BREA_DSP_CODES.get(extra_dsp, extra_dsp)
                pending["Call Disposition"] = (
                    f"{extra_dsp} ({dsp_long})" if dsp_long != extra_dsp else extra_dsp
                )

    # Flush last entry
    if pending:
        rows.append(pending)

    return rows


def process_brea_pdf(pdf_path: Path, resolution: int = 300, max_pages: int = 0):
    """OCR and parse a scanned Brea dispatch PDF."""
    page_texts = []
    for page_no, text in ocr_brea_pdf(pdf_path, resolution=resolution):
        page_texts.append(text)
        if max_pages > 0 and page_no >= max_pages:
            break
    full_text = "\n".join(page_texts)
    rows = parse_brea_ocr_text(full_text, pdf_path.name)
    print(f"[Done] {pdf_path.name}: {len(rows)} dispatch rows")
    return rows


# ══════════════════════════════════════════════════════════════════════════
#  LAYOUT  DETECTION  &  DRIVER
# ══════════════════════════════════════════════════════════════════════════

def detect_layout(pdf_path: Path) -> str:
    """Detect whether a PDF is Brea (scanned) or Marlborough (text layer).

    Strategy:
      1. If the filename contains Brea markers -> brea
      2. If we can extract text and it has Marlborough markers -> marlborough
      3. If there's no text layer, OCR page 1 and check for Brea markers
      4. Only classify as Brea if we positively see Brea markers;
         otherwise assume marlborough (the more common format)
    """
    import pdfplumber

    name_lower = pdf_path.name.lower()

    # Filename-based hints
    if "calls_for_service" in name_lower:
        return "brea"

    with pdfplumber.open(str(pdf_path)) as pdf:
        if not pdf.pages:
            return "unknown"

        # Sample first 3 pages for text
        sample = ""
        for page in pdf.pages[:3]:
            t = page.extract_text() or ""
            sample += t + "\n"

    # Check for Marlborough markers in text layer
    if re.search(r"Marlborough\s+Police\s+Department", sample, re.IGNORECASE):
        return "marlborough"
    if re.search(r"PRESS\s+LOG", sample, re.IGNORECASE):
        return "marlborough"
    if re.search(r"(?:Dispatch|Selective)\s+(?:Log|Search)", sample, re.IGNORECASE):
        return "marlborough"
    # Common Marlborough line patterns
    if re.search(r"Location/Address:", sample):
        return "marlborough"
    if re.search(r"Call\s+Reason\s+Action", sample):
        return "marlborough"

    # No text layer — try OCR on page 1 to look for Brea markers
    if len(sample.strip()) < 100:
        try:
            import pytesseract
            with pdfplumber.open(str(pdf_path)) as pdf:
                img = pdf.pages[0].to_image(resolution=150).original
                ocr_text = pytesseract.image_to_string(img, config="--psm 6")
                if re.search(r"BREA\s+POLICE", ocr_text, re.IGNORECASE):
                    return "brea"
                if re.search(r"Law\s+Incident\s+Summary", ocr_text, re.IGNORECASE):
                    return "brea"
                # Brea incident numbers are NNNN-NNNN
                if re.search(r"\d{4}-\d{4}", ocr_text):
                    return "brea"
                # If OCR shows Marlborough markers
                if re.search(r"Marlborough|PRESS\s+LOG|Location/Address",
                             ocr_text, re.IGNORECASE):
                    return "marlborough"
        except Exception:
            pass

        # Default: if it's scanned but no Brea markers found, assume
        # Marlborough (OCR the text and try the Marlborough parser)
        return "marlborough"

    return "unknown"


def process_pdf(pdf_path: Path, resolution: int = 300, max_pages: int = 0):
    """Auto-detect layout and parse a single PDF."""
    layout = detect_layout(pdf_path)
    print(f"[Layout] {pdf_path.name} -> {layout}")

    if layout == "marlborough":
        return process_marlborough_pdf(pdf_path, max_pages=max_pages)
    elif layout == "brea":
        return process_brea_pdf(pdf_path, resolution=resolution,
                                max_pages=max_pages)
    else:
        print(f"[WARN] Unknown layout for {pdf_path.name}; "
              f"attempting Marlborough text parse.")
        return process_marlborough_pdf(pdf_path, max_pages=max_pages)


def main():
    ap = argparse.ArgumentParser(
        description="Convert police dispatch/press-log PDFs to CSV."
    )
    ap.add_argument("input_dir", help="Directory containing PDF files")
    ap.add_argument("--out", default=".", help="Output directory for CSV files")
    ap.add_argument(
        "--resolution", type=int, default=300,
        help="OCR render DPI for scanned PDFs (default: 300)"
    )
    ap.add_argument(
        "--skip-ocr", action="store_true",
        help="Skip scanned (Brea) PDFs and only process text-layer PDFs"
    )
    ap.add_argument(
        "--pages", type=int, default=0,
        help="Process only the first N pages of each PDF (0 = all)"
    )
    ap.add_argument(
        "--dump-text", action="store_true",
        help="Write raw extracted/OCR text for each PDF to <out>/<stem>.txt"
    )
    args = ap.parse_args()

    in_dir = Path(args.input_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    for pdf_path in sorted(in_dir.glob("*.pdf")):
        if args.skip_ocr:
            layout = detect_layout(pdf_path)
            if layout == "brea":
                print(f"[SKIP] {pdf_path.name} (scanned; --skip-ocr set)")
                continue

        rows = process_pdf(pdf_path, resolution=args.resolution,
                          max_pages=args.pages)
        all_rows.extend(rows)

        if rows:
            df = pd.DataFrame(rows)
            for c in COLUMNS_ORDER:
                if c not in df.columns:
                    df[c] = ""
            df[COLUMNS_ORDER].to_csv(
                out_dir / f"{pdf_path.stem}.csv", index=False
            )
            print(f"[CSV] {pdf_path.stem}.csv ({len(rows)} rows)")

    if all_rows:
        df_all = pd.DataFrame(all_rows)
        for c in COLUMNS_ORDER:
            if c not in df_all.columns:
                df_all[c] = ""
        df_all[COLUMNS_ORDER].to_csv(
            out_dir / "MASTER_dispatch_calls.csv", index=False
        )
        print(f"\nMASTER CSV: {out_dir / 'MASTER_dispatch_calls.csv'} "
              f"({len(df_all)} rows)")
    else:
        print("\nNo rows extracted from any PDF.")


if __name__ == "__main__":
    main()