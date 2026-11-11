"""Address, parcel and owner-name normalization shared by every adapter.

Matching only works if every source is reduced to the same form, so all
normalization lives here and nowhere else.
"""
import re

SUFFIXES = {
    "AVENUE": "AVE", "AV": "AVE", "AVN": "AVE", "STREET": "ST", "STR": "ST",
    "ROAD": "RD", "DRIVE": "DR", "DRV": "DR", "BOULEVARD": "BLVD", "BLV": "BLVD",
    "LANE": "LN", "COURT": "CT", "CRT": "CT", "PLACE": "PL", "CIRCLE": "CIR",
    "CIRC": "CIR", "TERRACE": "TER", "TERR": "TER", "PARKWAY": "PKWY", "PKY": "PKWY",
    "HIGHWAY": "HWY", "TRAIL": "TRL", "SQUARE": "SQ", "ALLEY": "ALY",
    "CROSSING": "XING", "EXPRESSWAY": "EXPY", "FREEWAY": "FWY", "PIKE": "PIKE",
    "RUN": "RUN", "WAY": "WAY", "COVE": "CV", "LOOP": "LOOP", "PATH": "PATH",
    "POINTE": "PT", "GROVE": "GRV", "HEIGHTS": "HTS", "MANOR": "MNR",
}
DIRECTIONS = {
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
    "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW",
}
ORDINALS = {
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH",
    "FIFTH": "5TH", "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH",
    "NINTH": "9TH", "TENTH": "10TH", "ELEVENTH": "11TH", "TWELFTH": "12TH",
}
# Everything from a unit marker onward is dropped, so units match the building.
UNIT_RE = re.compile(
    r"\s(?:APT|APARTMENT|UNIT|STE|SUITE|LOT|BLDG|BUILDING|RM|ROOM|FL|FLOOR|SPC|SPACE|TRLR)\b.*$"
    r"|\s#.*$"
)
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")


def extract_zip(*values):
    """First 5-digit ZIP found at the end of any of the given strings."""
    for v in values:
        if not v:
            continue
        m = ZIP_RE.search(str(v).strip())
        if m:
            return m.group(1)
    return ""


def normalize_address(raw):
    """Street address -> canonical form like '3355 E 5TH AVE', or None.

    Drops the city/state/ZIP part (anything after the first comma), units,
    and punctuation, and standardizes suffixes, directionals and ordinals.
    Returns None when the address doesn't start with a house number
    (PO boxes, blanks, 'UNKNOWN', etc.) because those can't be matched safely.
    """
    if not raw:
        return None
    s = str(raw).upper()
    s = re.split(r"\bINCLUDING\b|\bAND\s+\d", s)[0]   # '27 S BURGESS AVE INCLUDING 25D ...'
    s = s.split(",")[0]
    s = re.sub(r"^\s*(\d+)\s*-\s*\d+\b", r"\1", s)   # '872 - 874 SUMMIT ST' -> '872 SUMMIT ST'
    s = s.replace(".", " ").replace("'", "")
    s = re.sub(r"\s+", " ", " " + s).rstrip()
    s = UNIT_RE.sub("", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    toks = s.split()
    if not toks:
        return None
    # House number: '1487-1489' -> '1487', '25D' stays '25D'
    m = re.match(r"^(\d+[A-Z]?)(?:-\d+[A-Z]?)?$", toks[0])
    if not m:
        return None
    toks[0] = m.group(1)
    if len(toks) < 2:
        return None
    # A trailing 5-digit token is a ZIP that slipped through without a comma
    if len(toks) > 2 and re.fullmatch(r"\d{5}", toks[-1]):
        toks = toks[:-1]
    out = [toks[0]]
    street = toks[1:]
    for i, t in enumerate(street):
        last = i == len(street) - 1
        first = i == 0
        t = ORDINALS.get(t, t)                       # 'FIFTH' -> '5TH'
        if re.fullmatch(r"\d+(?:ST|ND|RD|TH)", t):   # 'E 97TH ST' == 'E 97 ST'
            t = re.sub(r"\D", "", t)
        elif t in DIRECTIONS and (first or last) and len(street) > 1:
            t = DIRECTIONS[t]
        elif not first and t in SUFFIXES:
            t = SUFFIXES[t]
        out.append(t)
    return " ".join(out)


def normalize_parcel(raw, county):
    """Parcel ID -> digits only, with county-specific fixes. None if unusable."""
    if not raw:
        return None
    d = re.sub(r"\D", "", str(raw))
    if not d or set(d) == {"0"}:
        return None
    if county == "franklin":
        # Franklin IDs are 3+6 digits; some sources add a '-00' split suffix.
        if len(d) == 11 and d.endswith("00"):
            d = d[:9]
        if len(d) != 9:
            return None
    elif county == "cuyahoga":
        if len(d) == 7:
            d = "0" + d
        if len(d) != 8:
            return None
    return d


OWNER_STOP = {
    "LLC", "INC", "TRUST", "TRUSTEE", "TRUSTEES", "TR", "ESTATE", "EST", "OF", "THE",
    "AND", "ETAL", "ET", "AL", "JR", "SR", "II", "III", "LIVING", "REVOCABLE",
    "REV", "FAMILY", "CO", "CORP", "CORPORATION", "COMPANY", "LTD", "LP", "LLP",
    "LIMITED", "PARTNERSHIP", "HOLDINGS", "PROPERTIES", "PROPERTY", "INVESTMENTS",
    "GROUP", "REAL", "LIFE", "DECEASED", "DEC", "UNKNOWN", "HEIRS", "SPOUSE",
    "MR", "MRS", "MS", "AKA", "FKA", "NKA", "DBA", "TOD",
}


def owner_tokens(name):
    """Distinctive name words (3+ letters), used to sanity-check address matches."""
    if not name:
        return set()
    toks = re.findall(r"[A-Z]{3,}", str(name).upper())
    return {t for t in toks if t not in OWNER_STOP}


def county_key(raw):
    """'Cuyahoga County' / 'CUYAHOGA' -> 'cuyahoga'."""
    return re.sub(r"\s*county\s*$", "", str(raw or "").strip().lower()).strip()


def iso_date(raw):
    """'10/06/2026', '2026-10-06T00:00:00' -> '2026-10-06'. '' if unparseable."""
    if not raw:
        return ""
    s = str(raw).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""
