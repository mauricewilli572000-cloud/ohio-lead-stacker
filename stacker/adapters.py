"""One adapter per published scraper feed.

Each adapter reads the JSON a scraper repo already publishes and turns every
record into one or more "signals" in a single shared format. The stacker only
ever reads these feeds; it never writes back to a scraper repo.

Signal fields:
    source_id, source_label, lead_type, county, record_id,
    parcel, address, address_key, city, zip,
    owner, mailing, phone, contact_name,
    date, amount, detail, url
"""
from .normalize import county_key, iso_date, extract_zip, normalize_address, normalize_parcel


def _join(*parts):
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def _mailing(street, city="", state="", zip_=""):
    street = (street or "").strip()
    if not street:
        return ""
    if extract_zip(street):          # already a full one-line address
        return street
    tail = _join(city, state, zip_)
    return f"{street}, {tail}" if tail else street


def make_signal(src, *, record_id, address, county=None, parcel=None, city="",
                zip_="", owner="", mailing="", phone="", contact_name="",
                date="", amount=None, detail="", url=""):
    county = county_key(county) or src["county"]
    zip_ = (str(zip_ or "").strip()[:5]) or extract_zip(address)
    return {
        "source_id": src["id"],
        "source_label": src["label"],
        "lead_type": src["lead_type"],
        "county": county,
        "record_id": str(record_id),
        "parcel": normalize_parcel(parcel, county),
        "address": (str(address or "").split(",")[0]).strip(),
        "address_key": normalize_address(address),
        "city": (city or "").strip().title(),
        "zip": zip_ if len(zip_) == 5 else "",
        "owner": (owner or "").strip(),
        "mailing": (mailing or "").strip(),
        "phone": (phone or "").strip(),
        "contact_name": (contact_name or "").strip(),
        "date": iso_date(date),
        "amount": amount if isinstance(amount, (int, float)) else None,
        "detail": detail,
        "url": url or "",
    }


# --- foreclosures -------------------------------------------------------

def franklin_foreclosure(records, src):
    for r in records:
        rate = r.get("interest_rate")
        bits = [f"Case {r['case_number']}" if r.get("case_number") else "",
                r.get("loan_type") or "", f"{rate}% rate" if rate else ""]
        yield make_signal(
            src, record_id=r.get("doc_num") or r.get("case_number"),
            address=r.get("prop_address"), parcel=r.get("parcel_number"),
            city=r.get("prop_city"), zip_=r.get("prop_zip"), owner=r.get("owner"),
            mailing=_mailing(r.get("mail_address"), r.get("mail_city"),
                             r.get("mail_state"), r.get("mail_zip")),
            date=r.get("filed"), amount=r.get("amount"),
            detail=" · ".join(b for b in bits if b), url=r.get("clerk_url"))


def clerk_foreclosure(records, src):
    """Shared shape used by the Cuyahoga and Summit foreclosure scrapers."""
    for r in records:
        rate = r.get("interest_rate")
        kind = r.get("lead_type") or "Foreclosure"
        bits = [kind if kind != "Foreclosure" else "",
                f"Case {r['case_number']}" if r.get("case_number") else "",
                r.get("loan_type") or "", f"{rate}% rate" if rate else ""]
        yield make_signal(
            src, record_id=r.get("case_number") or r.get("doc_number"),
            address=r.get("property_address"), parcel=r.get("parcel_number"),
            city=r.get("property_city"), zip_=r.get("property_zip"),
            owner=r.get("owner_name") or r.get("defendant"),
            mailing=r.get("mailing_address") or "",
            date=r.get("filing_date"), amount=r.get("amount_owed"),
            detail=" · ".join(b for b in bits if b), url=r.get("source_url"))


# --- code enforcement ---------------------------------------------------

def columbus_code(records, src):
    for r in records:
        status = (r.get("record_status") or "")
        if "active" not in status.lower() and "court" not in status.lower():
            continue
        rtype = (r.get("record_type") or "").replace("Enforcement/", "")
        yield make_signal(
            src, record_id=r.get("case_number"), address=r.get("property_address"),
            city=r.get("property_city"), zip_=r.get("property_zip"),
            owner=r.get("owner_name"),
            mailing=_mailing(r.get("mailing_address"), r.get("mailing_city"),
                             r.get("mailing_state"), r.get("mailing_zip")),
            date=r.get("filing_date"),
            detail=f"{rtype} · {status}".strip(" ·"), url=r.get("source_url"))


def cleveland_code(records, src):
    for r in records:
        if not (r.get("open_records") or 0):
            continue
        types = r.get("lead_types") or []
        yield make_signal(
            src, record_id=r.get("lead_id"), address=r.get("property_address"),
            parcel=r.get("parcel"), city=r.get("city") or "Cleveland",
            zip_=r.get("zip"), owner=r.get("owner_name"),
            mailing=r.get("mailing_address") or "", date=r.get("latest_date"),
            amount=r.get("total_fines"),
            detail=f"{r.get('open_records')} open: " + ", ".join(types))


# --- evictions (one signal per rental property of the landlord) -------

def franklin_evictions(records, src):
    for r in records:
        mailing = _mailing(r.get("mailing_street"), r.get("mailing_city"),
                           r.get("mailing_state"), r.get("mailing_zip"))
        seen = set()
        for i, addr in enumerate(r.get("property_addresses") or []):
            key = normalize_address(addr)
            if key in seen:        # several units of one building
                continue
            seen.add(key)
            yield make_signal(
                src, record_id=f"{r.get('lead_id')}:{i}", address=addr,
                zip_=extract_zip(addr), owner=r.get("owner_name"), mailing=mailing,
                date=r.get("last_filing"),
                detail=(f"Landlord: {r.get('filings_12m', 0)} filings in 12 mo, "
                        f"{r.get('property_count', 0)} rentals"
                        + (f", tier {r['tier']}" if r.get("tier") else "")))


# --- probate (decedent's property, fiduciary is the contact) ----------

def probate(records, src):
    for r in records:
        fid = r.get("fiduciary") or {}
        if not isinstance(fid, dict):
            fid = {"name": str(fid)}
        estate = _join(r.get("decedent_first"), r.get("decedent_last")).title()
        props = [r.get("property")] + list(r.get("other_properties") or [])
        for i, p in enumerate(x for x in props if x):
            yield make_signal(
                src, record_id=f"{r.get('case_key') or r.get('case_number')}:{i}",
                county=r.get("county") or src["county"],
                address=p.get("property_address"), parcel=p.get("parcel_id"),
                city=p.get("property_city"), zip_=p.get("property_zip"),
                owner=p.get("auditor_owner"),
                mailing=_mailing(p.get("owner_mail_street"), p.get("owner_mail_city"),
                                 p.get("owner_mail_state"), p.get("owner_mail_zip")),
                phone=fid.get("phone", ""), contact_name=fid.get("name", ""),
                date=r.get("filing_date"),
                detail=(f"Estate of {estate}" if estate else "Estate")
                       + (f" · fiduciary {fid['name'].title()}" if fid.get("name") else ""),
                url=r.get("court_url"))


# --- tax delinquent ----------------------------------------------------

def hamilton_tax(records, src):
    for r in records:
        if (r.get("status") or "active") != "active":
            continue
        amt = r.get("delinquent_amount") or r.get("amount_owed")
        yrs = r.get("advertised_years") or []
        yield make_signal(
            src, record_id=r.get("doc_num"), address=r.get("property_address"),
            parcel=r.get("doc_num"), city=r.get("property_city"),
            zip_=r.get("property_zip"), owner=r.get("owner_name"),
            mailing=_mailing(r.get("mailing_address"), r.get("mailing_city"),
                             r.get("mailing_state"), r.get("mailing_zip")),
            date=r.get("date_added"), amount=amt,
            detail=(f"Delinquent since {r['tax_year']}" if r.get("tax_year") else "Delinquent")
                   + (f" · advertised {', '.join(yrs)}" if yrs else ""),
            url=r.get("auditor_url"))


ADAPTERS = {
    "franklin_foreclosure": franklin_foreclosure,
    "clerk_foreclosure": clerk_foreclosure,
    "columbus_code": columbus_code,
    "cleveland_code": cleveland_code,
    "franklin_evictions": franklin_evictions,
    "probate": probate,
    "hamilton_tax": hamilton_tax,
}
