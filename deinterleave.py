#!/usr/bin/env python3
"""
deinterleave.py
═══════════════════════════════════════════════════════════════════════════

Deterministic repair for the most common tabular-PDF defect in this project:
the address/date pixel collision.

THE DEFECT
    On long addresses, the street suffix (AVE, RD, PKWY, DR, ...) is printed
    in the SAME pixels as the incident date, so the text layer interleaves
    them character-by-character into garble, e.g.:

        "2817 COMMUNITY COLLEGE 3A/V6E/2022 at 20:25"
                                ^^^^^^^^^^^  = "AVE" woven into "3/6/2022"

    Because letters belong to the suffix and digits/slashes to the date, the
    two can be separated deterministically — no model, no guessing.

SAFETY POLICY (important)
    The DATE is almost always cleanly recoverable (digits + slashes fall into
    canonical M/D/YYYY order).  The ADDRESS is only recoverable when the fused
    suffix is a short known street type with the rest of the address intact;
    on deeply-tangled cases (e.g. "MARTIN LUTHER KING JR DR") the word spaces
    are lost in the collision and the address CANNOT be safely rebuilt.

    So this module recovers each field independently and only reports success
    for a field when the result is verifiably clean:
      - a recovered date must match M/D/YYYY (real calendar-shaped value)
      - a recovered address must end in a known suffix with spaces preserved

    A caller clears a row's review flag only for the field(s) actually fixed;
    a row whose date is recovered but whose address stays tangled keeps its
    address flag for a human (or the LLM residue pass).  Nothing garbled is
    ever written.

These functions are imported by both:
  - batch_parse_tabular_pdfs.py  (as a post-pass, so future PDFs come out
    clean in one run), and
  - fix_flagged_rows.py          (to repair CSVs already produced).
"""

import re

# Known short street suffixes we will trust when rebuilding an address.
_STREET_SUFFIXES = {
    "AVE", "RD", "ST", "DR", "BLVD", "PKWY", "LN", "CT", "WAY", "PL",
    "TER", "HWY", "EXT", "CIR", "SQ", "ROW", "PT", "PLZ", "TRL", "CV",
}

_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2})\b")
_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")


def looks_like_address_date_collision(address, date):
    """True if this row shows the address/date interleave signature.

    Signature: the Address value contains a fused digit-bearing token
    followed by an 'at HH:MM' time, OR the date bled such that Address holds
    date-like digits with a time.  Cheap pre-check before attempting repair.
    """
    a = str(address)
    # A time appears inside the Address field, and there is a digit-bearing
    # non-date token before it (the fused suffix+date).
    if re.search(r"\d.*\bat\b.*\d{1,2}:\d{2}", a):
        return True
    if re.search(r"[A-Za-z]/[A-Za-z]|\d[A-Za-z]\d", a):  # interleave marks
        return True
    return False


def recover_date(address, date):
    """Recover just the incident date from a collision. Returns
    'M/D/YYYY at HH:MM', or None if a valid date can't be formed.

    Only digits from the FUSED token (the one adjacent to the time) are
    used — not digits from the street number earlier in the address, which
    would otherwise contaminate the date (e.g. the "7" in "2817").
    """
    combined = (str(address) + " " + str(date)).strip()
    tm = _TIME_RE.search(combined)
    if not tm:
        return None
    time_str = tm.group(1)
    pre = combined[: tm.start()].rstrip()
    # Drop a trailing garbled 'at' token if present (e.g. "aXt", "a t").
    pre = re.sub(r"\s+a\S{0,2}t?\s*$", "", pre).rstrip()
    # The fused suffix+date is the LAST whitespace-delimited token (it may
    # itself contain no spaces because the collision destroyed them).
    if not pre:
        return None
    last_tok = pre.split()[-1]
    ds = "".join(c for c in last_tok if c.isdigit() or c == "/")
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", ds)
    if not m:
        return None
    # Reject if extra leading/trailing digits remain around the date — that
    # means the token held more than one date's worth of digits and we
    # can't trust the split.
    if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", ds):
        return None
    return f"{m.group(1)} at {time_str}"


def recover_address(address, date):
    """Recover the address ONLY when it comes out clean (known short suffix,
    spaces preserved).  Returns the address string, or None to leave it
    flagged (deeply-tangled addresses whose word spacing was destroyed).
    """
    a = str(address)
    m = re.match(r"^(.*?)\s+([A-Za-z0-9/]+)\s+at\s+\d", a)
    if not m:
        return None
    head, fused = m.group(1), m.group(2)
    letters = "".join(c for c in fused if c.isalpha())
    digits = "".join(c for c in fused if not c.isalpha())
    if letters.upper() in _STREET_SUFFIXES and \
            re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}", digits):
        return (head + " " + letters).strip()
    return None


def fix_row(row, addr_col="Address", date_col="Date & Time"):
    """Attempt a deterministic repair of one row (a dict).

    Returns a dict describing what was fixed:
        {"date": <new date or None>, "address": <new address or None>}
    with None meaning "leave that field as-is / still flagged".  Does not
    mutate the input.
    """
    addr = row.get(addr_col, "")
    date = row.get(date_col, "")
    if not looks_like_address_date_collision(addr, date):
        return {"date": None, "address": None}
    return {
        "date": recover_date(addr, date),
        "address": recover_address(addr, date),
    }
