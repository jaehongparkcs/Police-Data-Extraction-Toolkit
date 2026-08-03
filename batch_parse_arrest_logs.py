#!/usr/bin/env python3
"""
parse_arrest_logs.py
=====================
Converts scanned "Media Arrest Summary, by Name" arrest-log PDFs into a single
tidy CSV, one row per CHARGE (not per arrestee).

Both sample PDFs supplied are 100% scanned images (no usable text layer), so
this script OCRs every page with Tesseract and then parses the OCR text with
a label-anchored regex chain. Two department layouts are auto-detected and
supported out of the box:

  * Torrance PD   -- "Name#:", "Birthday:", "Arrest #", "Booking #:", etc.
  * Porterville PD -- "Inmate Name:", "Name Number:", "Arrest Time/Date:", etc.

Usage:
    python3 parse_arrest_logs.py /path/to/uploads_dir --out /path/to/out_dir

Requirements:
    pip install pdfplumber pytesseract pillow pandas --break-system-packages
    Tesseract binary must be installed and on PATH.
"""

import argparse
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import pytesseract

# --------------------------------------------------------------------------
# Output schema (as requested)
# --------------------------------------------------------------------------
COLUMNS_ORDER = [
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
    "Court (if available)",
    "Source PDF File",
    "Source Page",
]

WS = re.compile(r"\s+")


def norm(s: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces and strip."""
    return WS.sub(" ", s or "").strip(" -:\u2014")


BOILERPLATE_ANYWHERE = re.compile(
    r"Torrance\s+Police\s+Department|"
    r"Media\s+Arrest\s+Summary|"
    r"rpjmasr[\W_]*x7a?\S*|"
    r"rpjlasr[\W_]*x7\S*|"
    r"Page\s+\d+\s+of\s+\d+|"
    r"REDACTIONS?\s+PURSUANT|"
    r"CA\s+Penal\s+Code\s+851\.6|"
    r"REDACTED\s+COPY|"
    r"PER\s+GOVT\s+CODE|"
    r"AND\s+MAY\s+NOT\s+BE\s+DUPLICATED|"
    r"REVEALED\s+TO\s+ANY\s+UNAUTHORIZED|"
    r"PORTERVILLE\s+POLICE\s+DEPARTMENT|"
    r"Time\s*/\s*Date\s+Offense\s+Area\s+Statute\s+Court\s+Crime\s+Class",
    re.IGNORECASE,
)

# Footer date stamps (e.g. "09-30-2025", "07/22/25") -- these must be
# matched as the *entire* line, since a bare date could legitimately be
# part of real data elsewhere.
BOILERPLATE_DATE_ONLY = re.compile(
    r"^\s*(?:\d{2}-\d{2}-\d{4}|\d{2}/\d{2}/\d{2})\s*$"
)

# Report-summary footer (Porterville layout): "Total Arrests Reported: N",
# the juvenile-naming "Note:", and the "Report Includes:" parameter dump
# that follows it. This appears exactly once, at the very end of the
# report -- so it's safe to cut everything from its start onward, rather
# than matching line-by-line like the boilerplate above.
#
# This needs special handling because it isn't ordinary per-line
# boilerplate: for the LAST arrest record in the file there's no next
# "Arrest Time/Date:" to bound that record's block, so this footer text
# otherwise gets swallowed whole into the final record's charges text.
# Worse, the "Report Includes:" sentence itself contains two "time date"
# pairs (the report's date-range filter, e.g. "00:00:00 01/01/25" ...
# "23:59:59 02/28/25"), which split_charges_porterville mistakes for two
# additional bogus charge rows on top of leaking the summary text into
# the real charge's description.
REPORT_FOOTER_RE = re.compile(
    r"Total\s+Arrests\s+Reported\s*:.*", re.IGNORECASE | re.DOTALL
)


def strip_boilerplate(text: str) -> str:
    """Drops letterhead/legal-notice/footer lines from OCR text.

    Phrase-based boilerplate (letterhead, "Media Arrest Summary", legal
    notices) is matched anywhere in the line and the whole line is
    dropped -- these lines sometimes have stray OCR garbage (from a logo
    or decorative rule) mixed in around the recognizable phrase, so
    requiring the *entire* line to be nothing but boilerplate text let
    those slip through and pollute the next charge description.
    """
    text = REPORT_FOOTER_RE.sub("", text)
    lines = text.split("\n")
    return "\n".join(
        l for l in lines
        if not BOILERPLATE_ANYWHERE.search(l)
        and not BOILERPLATE_DATE_ONLY.match(l)
        and not is_noise_line(l)
    )


def ocr_pdf_pages(pdf_path: Path, resolution: int = 250):
    """Yields (page_number, ocr_text) for every page of a scanned PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        for idx, page in enumerate(pdf.pages):
            # Try a real text layer first (cheap); fall back to OCR.
            text = page.extract_text() or ""
            useful = norm(text)
            if len(useful) < 200 or "Name #" in useful or "Inmate Name" in useful:
                pass  # still attempt OCR below unless text layer is clearly rich
            if len(useful) < 200:
                img = page.to_image(resolution=resolution).original
                text = pytesseract.image_to_string(img, config="--psm 6")
            yield idx + 1, strip_boilerplate(text)


def calculate_age(dob_str: str, arrest_date_str: str):
    fmts = ["%m/%d/%Y", "%m/%d/%y"]
    b_date = a_date = None
    for f in fmts:
        try:
            b_date = datetime.strptime(dob_str.strip(), f)
            break
        except ValueError:
            continue
    for f in fmts:
        try:
            a_date = datetime.strptime(arrest_date_str.strip(), f)
            break
        except ValueError:
            continue
    if not b_date or not a_date:
        return "N/A"
    if b_date > a_date:
        b_date = b_date.replace(year=b_date.year - 100)
    return a_date.year - b_date.year - (
        (a_date.month, a_date.day) < (b_date.month, b_date.day)
    )



COURT_CODES = re.compile(r"\b(SCJC|SCIC|SCIJC|SCIIC|SUPR|MUNI|JUVI|JUV)\b\s*$", re.IGNORECASE)
COURT_CODES_ANY = re.compile(r"\b(SCJC|SCIC|SCIJC|SCIIC|SUPR|MUNI|JUVI|JUV)\b", re.IGNORECASE)
AREA_CODE = re.compile(r"\bSEC\.?\s*(\d+)\b", re.IGNORECASE)
STATUTE_CODE = re.compile(
    r"(?:"
    # number-then-code, e.g. "11377(A)HS", "594(B)(1)PC" (Porterville style)
    r"\d{2,6}(?:\.\d+)?(?:\s*\([A-Za-z0-9]{1,4}\))*\s*(?:PC|HS|VC|WI|H&S|MC|PENAL\s*CODE)"
    r"\.?[A-Z]?\b(?:\s+[MFI]\b)?"
    r"|"
    # code-then-number, e.g. "PC 530.5(A)F", "HS 11377(a)M", "HS 11364 (a)"
    # (Torrance style). Note the \s* before the paren groups -- OCR often
    # inserts a space there, and requiring them to be flush left the
    # subsection stranded in the description (e.g. "POSSESSION (a) OF DRUG").
    r"(?:PC|HS|VC|WI|H&S|MC|PENAL\s*CODE)\.?\s*\d{2,6}(?:\.\d+)?"
    r"(?:\s*\([A-Za-z0-9]{1,4}\))*(?:\s*[MFI]\b)?"
    r")",
    re.IGNORECASE,
)

# Leftover orphan subsection markers, e.g. a stray "(a)" whose statute
# number was mangled or split across an OCR line break.
ORPHAN_SUBSECTION = re.compile(r"\(\s*[A-Za-z0-9]{1,3}\s*\)")

# Characters that essentially never appear in real offense text but show up
# constantly when Tesseract tries to read decorative rules, dotted
# separators, or the department badge/logo.
OCR_NOISE_CHARS = re.compile(r"[^\x00-\x7F]|[~«»©®™€|\\^_`{}\[\]<>=+*#@$%]")


def scrub_noise_chars(text: str) -> str:
    """Light scrub: removes only OCR noise *characters*, keeping every
    token that has real content.

    Used for address text, where the full token-dropping scrub is too
    aggressive -- street numbers ("1546") and short directionals ("W")
    are legitimate content that the description-oriented scrubber would
    discard.
    """
    # "&" is legitimate in intersection addresses ("Yukon & Artesia"), so
    # it is preserved here even though it's absent from offense text.
    cleaned = re.sub(r"[^\x00-\x7F]|[~«»©®™€|\\^_`{}\[\]<>=+*#@$%]", " ", text or "")
    # Drop tokens that are now nothing but stray punctuation -- but keep a
    # bare "&", which is meaningful in intersection addresses
    # ("Yukon & Artesia").
    toks = [
        t for t in cleaned.split()
        if re.search(r"[A-Za-z0-9]", t) or t == "&"
    ]
    return " ".join(toks)


def scrub_ocr_garbage(text: str) -> str:
    """Removes OCR noise tokens produced by non-text page elements.

    Tesseract renders dotted separator rules, table borders, and the
    department badge/logo as dense runs of symbol junk (e.g.
    "~=«GetCreditfetc. = (isti('<'eiP SOAP 0(c)6(c)6(c)!"). Rather than
    enumerating specific junk strings, this drops tokens by *shape*: any
    whitespace-separated token that contains noise characters, or that is
    mostly non-letters, is discarded. Tokens that look like ordinary words
    or real codes are kept.
    """
    out = []
    for tok in (text or "").split():
        # Strip noise chars and see what real content is left.
        cleaned = OCR_NOISE_CHARS.sub("", tok)
        if not cleaned.strip(" .,:;-/()'\""):
            continue  # nothing but punctuation/noise
        letters = sum(c.isalpha() for c in cleaned)
        digits = sum(c.isdigit() for c in cleaned)
        other = len(cleaned) - letters - digits
        # If the token was materially altered by noise removal AND what
        # remains is short//junky, drop it entirely.
        if tok != cleaned and letters < 3 and digits < 2:
            continue
        # Drop tokens that are mostly punctuation/symbols.
        if other > letters + digits:
            continue
        # Drop tokens that survived character scrubbing but still look like
        # OCR nonsense (erratic casing, no vowels, punctuation-riddled).
        if _is_garbled_token(cleaned):
            continue
        out.append(cleaned)
    return " ".join(out)


def _is_garbled_token(tok: str) -> bool:
    """True if a token looks like OCR nonsense rather than a real word.

    Deliberately conservative: it is much worse to delete a real offense
    word than to leave some junk in, so this only fires on tokens that are
    clearly unreadable. Anything that is a plain word, an all-caps word,
    or a recognizable code is kept.
    """
    letters = re.sub(r"[^A-Za-z]", "", tok)
    if not letters:
        return True
    # Keep anything that's a clean word or clean all-caps word -- this
    # covers the overwhelming majority of real offense text.
    if re.fullmatch(r"[A-Za-z][a-z]*", tok) or re.fullmatch(r"[A-Z]+", tok):
        return False
    # Keep normal word forms with internal punctuation that legitimately
    # occurs in offense text: "Dirk/Dagger", "Credit/etc.", "others'".
    if re.fullmatch(r"[A-Za-z]+(?:[/\-'][A-Za-z]+)*\.?", tok):
        return False
    # Beyond this point the token has unusual structure. Flag it only if
    # punctuation is dense enough that it can't be a readable word.
    punct = len(re.sub(r"[A-Za-z0-9]", "", tok))
    if punct >= 2 and punct >= len(letters):
        return True
    # Erratic internal case-switching combined with punctuation is the
    # signature of unreadable OCR garble (e.g. "(isti(''eiP"). Require
    # BOTH signals so ordinary words and all-caps text are never touched.
    if punct >= 1 and not letters.isupper():
        switches = sum(
            1 for a, b in zip(letters, letters[1:]) if a.islower() and b.isupper()
        )
        if switches >= 1:
            return True
    return False


# A line is likely pure logo/badge/rule noise (not real data) if it has
# almost no recognizable word content. Used to drop such lines wholesale
# before parsing, since they otherwise get absorbed into whichever charge
# description precedes them.
def is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    # Never drop a line that carries real structured data.
    if re.search(r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}", stripped):
        return False
    if re.search(r"[A-Za-z]{2,}\s*[#:]", stripped):
        return False
    noise_hits = len(OCR_NOISE_CHARS.findall(stripped))
    alpha = sum(c.isalpha() for c in stripped)
    if alpha == 0:
        return True
    # Dense symbol junk relative to letters.
    if noise_hits >= 3 and noise_hits * 2 >= alpha:
        return True
    # Mostly 1-2 char fragments ("Aol aes, GY, «" / "fed ie He fey Ls" /
    # "SOE Bn") -- real offense text has multi-letter words. Two-word
    # lines count too, since badge/logo junk often lands that short.
    words = [w for w in re.split(r"\s+", stripped) if w]
    if len(words) >= 2:
        short = sum(1 for w in words if len(re.sub(r"[^A-Za-z]", "", w)) <= 3)
        if short >= len(words) * 0.75 and not re.search(r"[A-Z]{4,}", stripped):
            return True
    return False


NUMERIC_PAREN = re.compile(r"\(\s*\d{3,7}\s*\)")


# Fuzzy statute matcher for OCR-mangled codes. Tesseract confuses 5<->S,
# 0<->O, 1<->I/l inside statute numbers and often loses the space after the
# code prefix, producing things like "PCS5S305" for "PC 530.5". Requiring
# clean digits misses these entirely and leaves the junk in the offense
# text, so this accepts the confusable letters as digit stand-ins. It
# demands the code prefix plus a run of >=3 digit-or-confusable chars
# containing at least 2 real digits, which keeps it from eating words.
STATUTE_CODE_FUZZY = re.compile(
    r"\b(?:PC|HS|VC|WI|H&S|MC)"
    r"[\s.]*"
    r"(?=[0-9SOIl.()]*[0-9][0-9SOIl.()]*[0-9])"
    r"[0-9SOIl]{1,3}[0-9SOIl.()]{2,}"
    r"[A-Z]?\b",
    re.IGNORECASE,
)


def _is_wrap_garbage(word: str) -> bool:
    """True if a tail token is badge/logo OCR noise rather than wrapped
    offense text.

    Applied only to the post-statute tail, where the prior probability of
    noise is high, so it can be more aggressive than the general token
    scrubber -- but it still keeps anything that reads as a plain word,
    since dropping real offense text is the worse error.
    """
    # Compound offense words join real words with / or - ("Dirk/Dagger",
    # "Credit/etc."). Judge each part on its own, or the join point looks
    # like an internal case flip.
    parts = [p for p in re.split(r"[/\-']", word) if p]
    if len(parts) > 1:
        return any(_is_wrap_garbage(p) for p in parts)

    letters = re.sub(r"[^A-Za-z]", "", word)
    if not letters:
        return True
    # All-caps words are wrapped offense text ("SUBSTANCE", "DRUG").
    if letters.isupper() and len(letters) >= 2:
        return False
    # Three or more identical letters in a row ("errr", "reeer").
    if re.search(r"(.)\1{2,}", letters, re.IGNORECASE):
        return True
    # No vowels in a 3+ letter token.
    if len(letters) >= 3 and not re.search(r"[aeiouAEIOU]", letters):
        return True
    # Case flips inside the token ("SQe", "eiP") -- real words don't.
    if re.search(r"[a-z][A-Z]", letters):
        return True
    # Two or more capitals followed by lowercase ("SQe", "MFq"). Real
    # Title-case words have exactly one leading capital.
    if re.match(r"^[A-Z]{2,}[a-z]", letters):
        return True
    # Very short lowercase fragments are logo speckle, not offense words.
    if len(letters) <= 2 and letters.lower() not in {"of", "id"}:
        return True
    return False


def parse_charge_body(body: str, layout: str = "porterville"):
    """
    Splits a single charge row (everything after the leading time/date)
    into (description, charge_level, beat, court). Shared by both the
    Porterville and Torrance parsers.

    Table columns are roughly: Offense | Area/Location | Statute | Court |
    Crime Class. OCR flattens these onto one/two lines with the
    statute/court code often landing in the *middle* of the wrapped
    offense text (e.g. "POSSESS CONTROLLED 11377(A)HS M SCIC SUBSTANCE
    (35353)"), so rather than anchoring only to the start/end of the
    string we search for and strip the statute-code, court-code, and
    class-letter patterns wherever they occur.
    """
    body = body.strip(" -\n")
    body = re.sub(
        r"Time\s*/\s*Date\s+Offense\s+Area\s+Statute\s+Court\s+Crime\s+Class",
        " ",
        body,
        flags=re.IGNORECASE,
    )

    # TORRANCE column order is:
    #   Time/Date | Offense | Location | Statute | Court | CC
    # so text after the statute code is usually not offense text -- but a
    # tall wrapped Offense cell CAN continue after it (e.g. "POSSESSION
    # ... HS 11364 (a) M ... OF DRUG PARAPHERNALIA"). So rather than
    # hard-cutting at the statute, split there and later re-attach only
    # the word-like tokens from the tail, dropping badge/logo OCR garbage
    # (e.g. "Jones. SQe Wat Caen errr reer") that landed on the same line.
    tail = ""
    if layout == "torrance":
        cut = None
        for pat in (STATUTE_CODE, STATUTE_CODE_FUZZY):
            m_cut = pat.search(body)
            if m_cut and (cut is None or m_cut.start() < cut):
                cut = m_cut.start()
        if cut is not None:
            tail = body[cut:]
            body = body[:cut]

    # Crime Class: last standalone M/F/I token. Usually at the very end of
    # the row, but on warrant-type rows (blank Area/Court columns) it can
    # land mid-string once OCR flattens the table, so search the whole body
    # and take the last hit rather than anchoring strictly to the end.
    # For Torrance the class letter and court code live in the tail we cut
    # off above; for Porterville they're still inside body.
    cls_source = tail if layout == "torrance" and tail else body
    cls_hits = list(re.finditer(r"(?<![A-Za-z0-9])([MFI])(?![A-Za-z0-9])", cls_source))
    level_letter = cls_hits[-1].group(1) if cls_hits else None
    if cls_hits and cls_source is body:
        last = cls_hits[-1]
        body = (body[: last.start()] + " " + body[last.end():]).strip()

    # Court code (e.g. SCJC, SCIC) -- can appear mid-string, not just at the end
    court = "N/A"
    court_source = tail if layout == "torrance" and tail else body
    court_hits = list(COURT_CODES_ANY.finditer(court_source))
    if court_hits:
        court = court_hits[-1].group(1).upper()
    if court_source is body:
        body = COURT_CODES_ANY.sub(" ", body)

    # Area / Police Beat ("SEC 3" etc.) can appear anywhere mid-string
    beat = "N/A"
    area_m = AREA_CODE.search(body)
    if area_m:
        beat = f"SEC {area_m.group(1)}"
        body = (body[: area_m.start()] + " " + body[area_m.end():]).strip()

    # Strip statute codes (e.g. "11377(A)HS", "594(B)(1)PC") and any stray
    # bare numeric code in parens (e.g. "(35353)") wherever they occur --
    # these belong to the Statute column, not the offense description.
    # Re-attach any wrapped offense text from the tail (Torrance only).
    #
    # A tall Offense cell continues after the statute (e.g. "Poss of" ...
    # statute ... "Controlled Substances"), but badge/logo OCR garbage can
    # land there too. Rather than filtering by case -- which wrongly threw
    # away every Title-case continuation -- walk the tail in order and
    # keep real words until the first clearly-garbled token, then stop.
    # Wrapped text is contiguous and comes first; garbage runs to the end
    # of the line, so cutting at the transition keeps the real words.
    if layout == "torrance" and tail:
        salvaged = []
        # The tail begins with the statute code itself (plus court/class
        # columns). Strip those out first so the garbage walk below starts
        # at the wrapped-text boundary -- otherwise the statute token's own
        # digit/letter mix trips the garbage test and aborts salvage.
        tail_clean = STATUTE_CODE.sub(" ", tail)
        tail_clean = STATUTE_CODE_FUZZY.sub(" ", tail_clean)
        tail_clean = NUMERIC_PAREN.sub(" ", tail_clean)
        tail_clean = COURT_CODES_ANY.sub(" ", tail_clean)
        hit_garbage = False
        for tok in tail_clean.split():
            word = re.sub(r"[^A-Za-z/'\-]", "", tok)
            if not word:
                continue  # leftover digits/punctuation from stripped columns
            if word.upper() in {"PC", "HS", "VC", "WI", "MC"}:
                continue
            if len(word) == 1 and word.upper() in {"M", "F", "I"}:
                continue  # crime-class column letter
            if _is_wrap_garbage(word):
                hit_garbage = True
                break  # transition to badge/logo noise -- stop salvaging
            salvaged.append(word)
        # If the walk stopped early, the ONE Title-case token sitting right
        # at that boundary is usually the first word of the same noise run
        # (e.g. "Jones." leading "SQe Wat Caen"), not offense text. Only
        # that single token is dropped -- stripping a whole trailing run
        # would eat legitimate multi-word wraps like "Controlled
        # Substances". All-caps tokens are always kept, since those are
        # genuine wrapped offense continuations.
        if hit_garbage and salvaged and re.fullmatch(r"[A-Z][a-z]+", salvaged[-1]):
            salvaged.pop()
        if salvaged:
            body = (body + " " + " ".join(salvaged)).strip()

    body = STATUTE_CODE.sub(" ", body)

    # Strip OCR-mangled statute codes that the strict pattern missed
    # (e.g. "PCS5S305" for "PC 530.5").
    body = STATUTE_CODE_FUZZY.sub(" ", body)
    body = NUMERIC_PAREN.sub(" ", body)
    # A stray "(a)"/"(4)" can survive when OCR mangled or line-broke the
    # statute number it belonged to; it's never part of the offense name.
    body = ORPHAN_SUBSECTION.sub(" ", body)

    # Scrub leftover OCR noise LAST -- doing it earlier would strip the
    # punctuation that the statute/court/class patterns above rely on
    # (e.g. the parens in "PC 530.5(A)F"), causing those codes to survive
    # into the description instead of being recognized and removed.
    body = scrub_ocr_garbage(body)

    desc = re.sub(r"\s+", " ", body).strip(" -")
    # Drop an orphan single letter left dangling at either end -- these are
    # redaction-edge speckle or a mangled column value, never part of an
    # offense name (e.g. "Robbery i"). Two-letter words like "OF"/"ID" and
    # single letters inside the text are preserved.
    desc = re.sub(r"^[A-Za-z]\s+|\s+[A-Za-z]$", "", desc).strip(" -")
    level = {"M": "Misdemeanor (M)", "F": "Felony (F)", "I": "Infraction (I)"}.get(
        level_letter, "N/A"
    )
    return desc, level, beat, court


def split_charges_porterville(charges_blob: str):
    """Splits the Porterville charges section into individual charge rows."""
    date_re = re.compile(r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}")
    time_re = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")

    dt_re = re.compile(rf"({time_re.pattern})\s+({date_re.pattern})")
    matches = list(dt_re.finditer(charges_blob))
    out = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(charges_blob)
        chunk = charges_blob[start:end]
        desc, level, beat, court = parse_charge_body(chunk)
        if desc:
            out.append({
                "time": m.group(1), "date": m.group(2),
                "desc": desc, "level": level, "beat": beat, "court": court,
            })
    return out


def split_charges(charges_blob: str):
    """
    Splits a normalized charges section into individual charge strings.
    Each charge begins with a date (M/D/YYYY or M/D/YY) followed by a time.
    Returns list of (date, time, description, level, court).

    Previously this only stripped a trailing M/F/I letter anchored to the
    very end of the chunk and left statute codes (e.g. "HS 11377(a)M",
    "PC 530.5(A)F") embedded in the description, which also meant the
    class letter attached directly to a statute code (no space before it)
    was never recognized as the level -- it showed up as text in the
    description and the level came back "N/A". Now it reuses the same
    statute/court/class-letter stripping as the Porterville parser, which
    finds these tokens wherever they land in the OCR'd text rather than
    only at a fixed position.
    """
    date_re = re.compile(r"\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}")
    time_re = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?\s*[APap]\.?[Mm]?\.?")

    matches = list(date_re.finditer(charges_blob))
    out = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(charges_blob)
        chunk = charges_blob[start:end]

        time_m = time_re.search(chunk)
        time_val = time_m.group(0) if time_m else ""
        body = chunk[: time_m.start()] + chunk[time_m.end():] if time_m else chunk
        body = body.strip(" -\n")

        desc, level, _beat, court = parse_charge_body(body, layout="torrance")
        if desc:
            out.append((m.group(0), time_val, desc, level, court))
    return out


# --------------------------------------------------------------------------
# Sequential field extractor
# --------------------------------------------------------------------------
# NOTE: The old approach used a single regex per layout chaining many lazy
# `.*?` groups together under re.DOTALL (e.g. "Label1: (?P<a>.*?) Label2:
# (?P<b>.*?) Label3: ..."). That is catastrophic-backtracking-prone: when a
# block doesn't cleanly match end-to-end (garbled OCR, a missing label, or a
# block that runs unusually long because the next record wasn't detected),
# the engine can spend exponential time trying every way to redistribute
# text across those gaps. That is what caused the hang.
#
# This helper instead walks the block with a cursor, doing one small bounded
# search per field and slicing between hits. Every step is O(n) and the
# whole extraction is O(n * num_steps) -- no backtracking blowup possible,
# regardless of block size or how messy the OCR text is.
#
# Step kinds:
#   ('label', pattern)          - consume a label, nothing captured
#   ('value', name, pattern)    - capture a short token right after the
#                                 cursor (skipping leading whitespace)
#   ('until', name, pattern)    - capture everything from the cursor up to
#                                 (not including) the next label match, then
#                                 consume that label; fails if not found
#   ('until_opt', name, pattern)- same as 'until' but doesn't fail if the
#                                 label is missing (captures "" and leaves
#                                 cursor in place)
def extract_sequential(block, steps):
    """Returns (fields_dict, final_cursor_pos), or (None, None) on failure."""
    pos = 0
    gd = {}
    for step in steps:
        kind = step[0]
        if kind == "label":
            pattern = step[1]
            m = re.compile(pattern, re.IGNORECASE).search(block, pos)
            if not m:
                return None, None
            pos = m.end()
        elif kind == "value":
            _, name, pattern = step
            m = re.compile(r"\s*(" + pattern + ")", re.IGNORECASE).match(block, pos)
            if not m:
                return None, None
            gd[name] = m.group(1)
            pos = m.end()
        elif kind == "until":
            _, name, pattern = step
            m = re.compile(pattern, re.IGNORECASE).search(block, pos)
            if not m:
                return None, None
            gd[name] = block[pos:m.start()]
            pos = m.end()
        elif kind == "until_opt":
            _, name, pattern = step
            m = re.compile(pattern, re.IGNORECASE).search(block, pos)
            if m:
                gd[name] = block[pos:m.start()]
                pos = m.end()
            else:
                gd[name] = ""
        elif kind == "until_before":
            # Like 'until', but stops right before the match and does NOT
            # consume it -- use when the boundary text itself is data you
            # need intact for a later step (e.g. a timestamp that starts
            # the next section).
            _, name, pattern = step
            m = re.compile(pattern, re.IGNORECASE).search(block, pos)
            if not m:
                return None, None
            gd[name] = block[pos:m.start()]
            pos = m.start()
        else:
            raise ValueError(f"unknown step kind: {kind}")
    return gd, pos


# --------------------------------------------------------------------------
# Torrance PD layout parser
# --------------------------------------------------------------------------
TORRANCE_RECORD_START = re.compile(
    r"Name\s*#\s*:\s*(?P<namenum>\d+)\s*Birthday\s*:\s*(?P<dob>\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)

# Step list for extract_sequential(), replacing the old catastrophic
# chained regex. Mirrors the same fields in the same order.
TORRANCE_STEPS = [
    ("label", r"Name\s*#\s*[:;]"),
    ("value", "namenum", r"\d+"),
    ("label", r"Birthday\s*[:;]"),
    ("value", "dob", r"\d{1,2}/\d{1,2}/\d{2,4}"),
    ("label", r"Eye\s*Color\s*[:;]"),
    ("value", "eye", r"\S+"),
    ("until", "name2", r"Hair\s*Color\s*[:;]"),
    ("value", "hair", r"\S+"),
    ("label", r"Sex\s*[:;]"),
    ("value", "sex", r"[MF]"),
    ("label", r"Height\s*[:;]"),
    ("value", "height", r"\S+"),
    ("label", r"Weight\s*[:;]"),
    ("value", "weight", r"\S+"),
    ("label", r"Address\s*[:;]"),
    ("until", "address", r"Job\s*Desc\s*[:;]"),
    ("until", "job", r"Arrest\s*#"),
    ("value", "arrestnum", r"\S+"),
    ("until", "_skip1", r"Arrest\s*Date"),
    ("value", "adate", r"\d{1,2}/\d{1,2}/\d{2,4}"),
    ("label", r"Arrest\s*Type\s*[:;]"),
    ("until", "atype_time", r"Agency\s*[:;]"),
    ("value", "agency", r"\S+"),
    ("until", "_skip2", r"Arrest\s*Loc\s*[:;]"),
    ("until", "loc", r"Booking\s*#\s*[:;]"),
    ("value", "booknum", r"\S+"),
    ("until", "loc_wrap", r"Booking\s*Date\s*[:;]"),
    ("value", "bdate", r"\d{1,2}/\d{1,2}/\d{2,4}"),
    ("until", "bail", r"Release\s*Date\s*[:;]"),
    ("value", "rdate", r"\d{1,2}/\d{1,2}/\d{2,4}"),
    ("until", "rtype", r"Time\s*/\s*Date\s+Offense\s+Location\s+Statute\s+Court\s+CC\s*"),
]

TIME_TOKEN = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?\s*[APap]\.?[Mm]\.?")


def _name_prefix(full_text: str, pos: int) -> str:
    """Grabs the arrestee-name text on the line(s) immediately before a
    'Name#:' match, ignoring boilerplate/header noise from earlier lines.

    Names sometimes wrap across two lines (e.g. "BAKER, ROBERT" /
    "LONNIE"), so this looks at up to the last 2 non-empty lines and keeps
    whichever trailing ones look like name text.

    Torrance prints arrestee names in ALL CAPS, which is the key signal
    used to reject OCR garbage: these pages have large black redaction
    blocks whose edges Tesseract renders as mixed-case junk (e.g.
    "RANGE. ay") on the line right above the name. Requiring all-caps
    name shape keeps that junk out of the name field.
    """
    window = full_text[max(0, pos - 80):pos]
    lines = [l.strip() for l in window.split("\n") if l.strip()]
    if not lines:
        return ""

    def is_name_line(l: str) -> bool:
        # All-caps words, optionally separated by spaces/commas/hyphens/
        # apostrophes. Must contain at least one 2+ letter word.
        if not re.fullmatch(r"[A-Z][A-Z,.'\- ]*", l):
            return False
        return bool(re.search(r"[A-Z]{2,}", l))

    tail = []
    for l in reversed(lines[-2:]):
        if is_name_line(l):
            tail.insert(0, l)
        else:
            break
    if not tail:
        # Fall back to salvaging just the all-caps portion at the end of
        # the last line (handles "<redaction junk> GUZMAN, MIGUEL").
        m = re.search(r"([A-Z]{2,}[A-Z,.'\- ]*)$", lines[-1])
        return m.group(1).strip(" ,.-'") if m else ""
    combined = " ".join(tail)
    return combined.strip(" ,.-'")


def parse_torrance(full_text: str, source_name: str):
    rows = []
    starts = list(TORRANCE_RECORD_START.finditer(full_text))
    name_prefixes = [_name_prefix(full_text, m.start()) for m in starts]

    for i, m in enumerate(starts):
        block_start = m.start()
        name1 = name_prefixes[i]

        if i + 1 < len(starts):
            block_end = starts[i + 1].start() - len(name_prefixes[i + 1])
        else:
            block_end = len(full_text)
        block_end = max(block_end, block_start)
        block = full_text[block_start:block_end]

        gd, pos = extract_sequential(block, TORRANCE_STEPS)
        if gd is None:
            continue

        # Trailing part: charges table, then an optional "Related DRs:"
        # section. Charges runs up to "Related DRs:" if present, else to
        # the end of the block -- one bounded search, no backtracking.
        rel_m = re.compile(r"Related\s*DRs\s*[:;]", re.IGNORECASE).search(block, pos)
        if rel_m:
            gd["charges"] = block[pos:rel_m.start()]
            gd["drs"] = block[rel_m.end():]
        else:
            gd["charges"] = block[pos:]
            gd["drs"] = ""

        full_name = norm(f"{name1} {gd.get('name2','')}").strip(", ")

        # Continuation pages reprint the arrestee's name as a margin label
        # (e.g. "BAKER, ROBERT" / "LONNIE" wrapped across two lines) even
        # though it's not part of any charge row. Column interleaving in the
        # OCR stream can separate those wrapped lines from each other, so
        # match each line-fragment of the name independently rather than
        # requiring the whole name to appear as one contiguous run.
        raw_charges = gd.get("charges") or ""
        if full_name:
            # Longest fragments first so "BAKER, ROBERT" is consumed before
            # the bare "BAKER" would partially match it.
            fragments = set()
            words = [w for w in re.split(r"\s+", full_name) if w]
            for size in range(len(words), 0, -1):
                for i in range(len(words) - size + 1):
                    frag = words[i:i + size]
                    # Only strip multi-word fragments, or single words long
                    # enough to be a real surname -- stripping short single
                    # tokens risks eating legitimate offense text.
                    if size >= 2 or len(re.sub(r"[^A-Za-z]", "", frag[0])) >= 4:
                        fragments.add(tuple(frag))
            for frag in sorted(fragments, key=len, reverse=True):
                frag_re = re.compile(
                    r"(?<![A-Za-z])" + r"\s+".join(re.escape(w) for w in frag)
                    + r"(?![A-Za-z])",
                    re.IGNORECASE,
                )
                raw_charges = frag_re.sub(" ", raw_charges)

        arrest_time_m = TIME_TOKEN.search(gd.get("atype_time") or "")
        arrest_time = arrest_time_m.group(0) if arrest_time_m else ""
        arrest_dt = norm(f"{gd['adate']} {arrest_time}")

        # loc_wrap holds a continuation line of the "Arrest Loc" cell that
        # OCR emitted after the "Booking #:" label instead of before it
        # (common when a wrapped table cell spans two lines but the
        # neighboring cells on the same row don't) -- stitch it back on
        # rather than discarding it, or the location gets truncated (e.g.
        # "Yukon &" instead of "Yukon & Artesia").
        # Scrub noise chars ("~", stray symbols) that redaction-block edges
        # inject into the address text, e.g. "1546 W MLK ~ BLVD". Uses the
        # light char-only scrub so street numbers and directionals survive.
        location = scrub_noise_chars(norm(f"{gd.get('loc', '')} {gd.get('loc_wrap', '')}"))
        drs_raw = norm(gd.get("drs", ""))
        drs_m = re.match(r"[\d][\d,\s]*", drs_raw)
        related_drs = drs_m.group(0).strip() if drs_m else "N/A"
        related_drs = related_drs or "N/A"

        age = calculate_age(gd["dob"], gd["adate"])

        charges = split_charges(raw_charges)
        if not charges:
            charges = [("", "", "N/A", "N/A", "N/A")]

        for _, _, desc, level, court in charges:
            rows.append({
                "Arrest ID Number": gd["arrestnum"],
                "Arrestee Name": full_name,
                "Race": "N/A",
                "Ethnicity": "N/A",
                "Gender": gd.get("sex", "N/A"),
                "Age": age,
                "Home Address": "N/A (redacted in source)",
                "Offense and Charge Description": desc,
                "Charge Level (Misdemeanor/Felony Class)": level,
                "Time and Date of Arrest": arrest_dt,
                "Location of Arrest (Address and/or Geographic Coordinates)": location,
                "Crime Incident Report ID Number (if available)": related_drs,
                "Police Division": norm(gd.get("agency", "")) or "TPD",
                "Police Beat": "N/A",
                "Court (if available)": court,
                "Source PDF File": source_name,
                "Source Page": "",
            })
    return rows


# --------------------------------------------------------------------------
# Porterville PD layout parser
# --------------------------------------------------------------------------
PORT_RECORD_START = re.compile(r"Arrest\s*Time\s*/\s*Date\s*[:;]?\s*\d{1,2}:\d{2}:\d{2}", re.IGNORECASE)

# Step list for extract_sequential(), replacing the old catastrophic
# chained regex. Mirrors the same fields in the same order.
PORT_STEPS = [
    ("label", r"Arrest\s*Time\s*/\s*Date\s*[:;]?"),
    ("value", "atime", r"\d{1,2}:\d{2}:\d{2}"),
    ("value", "adate", r"\d{1,2}/\d{1,2}/\d{2,4}"),
    ("label", r"Booking\s*Number\s*[:;]?"),
    ("value", "booknum", r"\S+"),
    ("label", r"Inmate\s*Name\s*[:;]?"),
    ("until", "name", r"Name\s*Number\s*[:;]?"),
    ("value", "namenum", r"\S+"),
    ("label", r"Birth\s*Date\s*[:;]?"),
    ("value", "dob", r"\d{1,2}/\d{1,2}/\d{2,4}"),
    ("label", r"Address\s*[:;]?"),
    ("until", "address", r"Arrest\s*Type\s*[:;]?"),
    ("until", "atype", r"Arrested\s*By\s*[:;]?"),
    ("until", "arrestedby", r"Agency\s*[:;]?"),
    ("value", "agency", r"\S+"),
    ("label", r"Arrest\s*Location[s]?\s*[:;]?"),
    ("until", "loc", r"Arrest\s*Number\s*[:;]?"),
    ("value", "arrestnum", r"\S+"),
    ("until_opt", "loc_wrap", r"Related\s*Incidents?\s*[:;]?"),
    # Bound the incidents value on the first charge row's own time/date
    # stamp, not on the literal column-header text ("Time/Date Offense
    # Area Statute Court Crime Class"). That header is underlined in the
    # source and OCRs unreliably (e.g. "Time/D Off S C Crime Cl"), so
    # matching on it was letting the search run past the whole charges
    # table looking for a literal match, swallowing the charges into the
    # incidents field. A charge row's leading "hh:mm:ss mm/dd/yyyy" is
    # pure digits/punctuation and OCRs far more reliably.
    ("until_before", "incidents_raw", r"\d{1,2}:\d{2}(?::\d{2})?\s+\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{2,4}"),
]


INCIDENT_ID_TOKEN = re.compile(r"\b(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}\b")


def clean_incident_ids(raw: str) -> str:
    """Extracts likely incident-ID tokens (alphanumeric, contains a digit,
    4+ chars -- e.g. '25P06849') from raw text, dropping any leftover
    header noise (e.g. OCR debris like 'Time/D Off S C Crime Cl', which
    is pure letters and won't match). Falls back to 'N/A' if nothing
    recognizable is found."""
    tokens = INCIDENT_ID_TOKEN.findall(raw or "")
    return ", ".join(tokens) if tokens else "N/A"


def parse_porterville(full_text: str, source_name: str):
    rows = []
    starts = list(PORT_RECORD_START.finditer(full_text))
    for i, m in enumerate(starts):
        block_end = starts[i + 1].start() if i + 1 < len(starts) else len(full_text)
        block = full_text[m.start():block_end]
        gd, pos = extract_sequential(block, PORT_STEPS)
        if gd is None:
            continue
        gd["charges"] = block[pos:]

        arrest_dt = norm(f"{gd['adate']} {gd['atime']}")
        age = calculate_age(gd["dob"], gd["adate"])
        related = clean_incident_ids(gd.get("incidents_raw", ""))

        loc_wrap = norm(gd.get("loc_wrap", ""))
        # Guard: loc_wrap should only be short leftover wrap text (e.g. "VETERANS
        # PARK"), never the charges table header/rows if Related Incidents was
        # itself blank and the lazy match over-ran.
        if re.search(r"Time\s*/\s*Date|Offense|Statute|Crime\s*Class", loc_wrap, re.IGNORECASE):
            loc_wrap = ""
        full_loc = norm(f"{gd.get('loc','')} {loc_wrap}")

        charges = split_charges_porterville(gd.get("charges") or "")
        if not charges:
            charges = [{"desc": "N/A", "level": "N/A", "beat": "N/A", "court": "N/A"}]

        for c in charges:
            rows.append({
                "Arrest ID Number": gd["arrestnum"],
                "Arrestee Name": norm(gd["name"]),
                "Race": "N/A",
                "Ethnicity": "N/A",
                "Gender": "N/A",
                "Age": age,
                "Home Address": norm(gd.get("address", "")) or "N/A (redacted in source)",
                "Offense and Charge Description": c["desc"],
                "Charge Level (Misdemeanor/Felony Class)": c["level"],
                "Time and Date of Arrest": arrest_dt,
                "Location of Arrest (Address and/or Geographic Coordinates)": full_loc,
                "Crime Incident Report ID Number (if available)": related,
                "Police Division": norm(gd.get("agency", "")) or "PPD",
                "Police Beat": c["beat"],
                "Court (if available)": c["court"],
                "Source PDF File": source_name,
                "Source Page": "",
            })
    return rows


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def detect_layout(sample_text: str) -> str:
    if re.search(r"Inmate\s*Name\s*:", sample_text, re.IGNORECASE):
        return "porterville"
    if re.search(r"Name\s*#\s*:", sample_text, re.IGNORECASE):
        return "torrance"
    return "unknown"


def process_pdf(pdf_path: Path, resolution: int = 250, dump_text_to: Path = None):
    print(f"[OCR] {pdf_path.name} ...")
    page_texts = []
    for page_no, text in ocr_pdf_pages(pdf_path, resolution=resolution):
        page_texts.append(text)
        if page_no % 20 == 0:
            print(f"   ...page {page_no}")
    full_text = "\n".join(page_texts)

    if dump_text_to is not None:
        dump_text_to.write_text(full_text, encoding="utf-8")
        print(f"[Dump] raw OCR text -> {dump_text_to}")

    layout = detect_layout(full_text)
    print(f"[Layout] {pdf_path.name} -> {layout}")

    if layout == "torrance":
        rows = parse_torrance(full_text, pdf_path.name)
    elif layout == "porterville":
        rows = parse_porterville(full_text, pdf_path.name)
    else:
        print(f"[WARN] Could not detect layout for {pdf_path.name}; skipping.")
        rows = []
    print(f"[Done] {pdf_path.name}: {len(rows)} charge rows")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Convert scanned arrest-log PDFs to CSV.")
    ap.add_argument("input_dir", help="Directory containing PDF files")
    ap.add_argument("--out", default=".", help="Output directory for CSV files")
    ap.add_argument("--resolution", type=int, default=400, help="OCR render DPI")
    ap.add_argument(
        "--dump-text",
        action="store_true",
        help="Write the raw post-boilerplate-strip OCR text for each PDF to "
             "<out>/<stem>.ocr.txt. Use this to diagnose parsing problems: it "
             "shows exactly what Tesseract produced, which is what the parser "
             "actually sees.",
    )
    args = ap.parse_args()

    in_dir = Path(args.input_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    for pdf_path in sorted(in_dir.glob("*.pdf")):
        rows = process_pdf(
            pdf_path,
            resolution=args.resolution,
            dump_text_to=(out_dir / f"{pdf_path.stem}.ocr.txt") if args.dump_text else None,
        )
        all_rows.extend(rows)
        if rows:
            df = pd.DataFrame(rows)
            for c in COLUMNS_ORDER:
                if c not in df.columns:
                    df[c] = "N/A"
            df[COLUMNS_ORDER].to_csv(out_dir / f"{pdf_path.stem}.csv", index=False)

    if all_rows:
        df_all = pd.DataFrame(all_rows)
        for c in COLUMNS_ORDER:
            if c not in df_all.columns:
                df_all[c] = "N/A"
        df_all[COLUMNS_ORDER].to_csv(out_dir / "MASTER_arrest_charges.csv", index=False)
        print(f"\nMASTER CSV: {out_dir / 'MASTER_arrest_charges.csv'} ({len(df_all)} rows)")
    else:
        print("\nNo rows extracted from any PDF.")


if __name__ == "__main__":
    main()