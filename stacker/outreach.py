"""Get stacked leads ready for outreach: skip-trace files and the GoHighLevel push.

Skip tracing
    docs/skiptrace_ready.csv      people, best leads first, in the column layout skip-trace services
                                  (BatchSkipTracing, PropStream, REISift, ...) import directly
    docs/skiptrace_companies.csv  LLC / corporate / trust owners: look up the registered agent
                                  (Ohio Secretary of State business search) before tracing
    When a property is in probate the owner has died, so the row is the estate contact
    (executor / heir from the probate case), not the deceased owner.

GoHighLevel
    New leads at or above GHL_MIN_SCORE (and every Hot lead) are sent to an Inbound Webhook
    workflow; a lead is sent again only when it gains a new lead type. Sent IDs live in
    data/ghl_sent.json. With no GHL_WEBHOOK_URL set this does nothing.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.request

GHL_MIN_SCORE = 60
GHL_MAX_PER_RUN = 500
GHL_DELAY = 0.75           # seconds between webhook calls (GHL rejects with 422 after ~100 quick calls)
GHL_BACKOFF = 65           # on a reject, wait this long once and retry

ENTITY_WORDS = {"LLC", "L.L.C.", "INC", "INC.", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LP", "LLP",
                "TRUST", "TRUSTEE", "TRUSTEES", "TR", "TRS", "BANK", "PROPERTIES", "PROPERTY", "HOLDINGS",
                "INVESTMENTS", "INVESTMENT", "GROUP", "PARTNERS", "ASSOCIATES", "REALTY", "ENTERPRISES",
                "CHURCH", "MINISTRIES", "FOUNDATION", "ASSOCIATION", "HOUSING", "DEVELOPMENT", "CAPITAL",
                "VENTURES", "MANAGEMENT", "RENTALS", "HOMES", "ESTATES", "FUND", "AUTHORITY"}
SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


def is_entity(name: str) -> bool:
    words = set(re.sub(r"[^A-Z0-9. ]", " ", (name or "").upper()).split())
    return bool(words & ENTITY_WORDS) or bool(re.match(r"^\d", (name or "").strip()))


def person_name(raw: str, order: str = "auto") -> tuple[str, str] | None:
    """Owner / contact -> (first, last) for the first person named; None for companies.

    'SMITH, JOHN A' -> John A / Smith        'SMITH JOHN A & MARY' -> John A / Smith  (county 'LAST FIRST')
    order='first_last': 'Michelle S. Swain' (court contacts) -> Michelle S. / Swain
    """
    n = re.sub(r"\s+", " ", (raw or "").strip())
    if not n or is_entity(n):
        return None
    n = re.split(r"\s*&\s*|\s+AND\s+", n, flags=re.I)[0]
    n = re.sub(r"\s*-\s*(TRS?|ET AL|ETAL)\b.*$", "", n, flags=re.I).strip(" ,.")
    if "," in n:
        last, first = [x.strip() for x in n.split(",", 1)]
    else:
        parts = [p for p in n.split() if p.upper().strip(".") not in SUFFIXES]
        if len(parts) < 2:
            return None
        if order == "first_last":
            first, last = " ".join(parts[:-1]), parts[-1]
        else:
            last, first = parts[0], " ".join(parts[1:])
    first = " ".join(p for p in first.split() if p.upper().strip(".") not in SUFFIXES)
    if not first or not last:
        return None
    return first.title(), last.title()


def split_mailing(m: str) -> tuple[str, str, str, str]:
    """'123 Main St, Columbus OH 43215' -> street, city, state, zip (best effort)."""
    m = (m or "").strip()
    zm = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", m)
    zip_ = zm.group(1) if zm else ""
    rest = m[:zm.start()].strip(" ,") if zm else m
    sm = re.search(r"[ ,]([A-Z]{2})$", rest)
    state = sm.group(1) if sm else ""
    rest = rest[:sm.start()].strip(" ,") if sm else rest
    street, city = ([x.strip() for x in rest.rsplit(",", 1)] if "," in rest else (rest, ""))
    return street, city, state, zip_


def target(stack: dict) -> dict:
    """Who to skip trace / contact for this property, and where they get mail."""
    probate = "probate" in stack.get("lead_types", [])
    if probate and stack.get("contact_name"):
        c = stack["contact_name"]
        name = person_name(c) if "," in c else person_name(c, "first_last")    # court names are 'First Last'

        if name:
            return {"first": name[0], "last": name[1], "role": "estate contact (owner deceased)",
                    "company": "", "mailing": ""}      # executor's own address isn't known: trace by name + property
    owner = stack.get("owner", "")
    name = person_name(owner)
    return {"first": name[0] if name else "", "last": name[1] if name else "", "company": "" if name else owner,
            "role": "owner", "mailing": stack.get("mailing", "")}


SKIP_COLS = ["First Name", "Last Name", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
             "Property Address", "Property City", "Property State", "Property Zip",
             "Who", "Owner On Record", "County", "Parcel", "Lead Types", "Tier", "Score", "Owned Since", "Stack ID"]
COMPANY_COLS = ["Company", "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
                "Property Address", "Property City", "Property State", "Property Zip",
                "County", "Parcel", "Lead Types", "Tier", "Score", "Stack ID"]


def write_skiptrace(stacks: list[dict], people_path: str, company_path: str, labels: dict) -> tuple[int, int]:
    people, companies, seen = [], [], set()
    for s in sorted(stacks, key=lambda s: -s["score"]):
        t = target(s)
        street, city, state, zip_ = split_mailing(t["mailing"])
        if not street:                         # no mailing on file: the property is the best address to trace
            street, city, state, zip_ = s["address"], s.get("city", ""), "OH", s.get("zip", "")
        types = " + ".join(labels.get(x, x) for x in s["lead_types"])
        if t["first"]:
            key = (t["first"].upper(), t["last"].upper(), street.upper())
            if key in seen:
                continue
            seen.add(key)
            people.append([t["first"], t["last"], street, city, state, zip_,
                           s["address"], s.get("city", ""), "OH", s.get("zip", ""),
                           t["role"], s.get("owner", ""), s["county"].title(), s.get("parcel", ""), types,
                           s.get("tier", ""), s["score"], s.get("owned_since", ""), s["id"]])
        elif t["company"]:
            companies.append([t["company"], street, city, state, zip_,
                              s["address"], s.get("city", ""), "OH", s.get("zip", ""),
                              s["county"].title(), s.get("parcel", ""), types, s.get("tier", ""), s["score"], s["id"]])
    for path, cols, rows in ((people_path, SKIP_COLS, people), (company_path, COMPANY_COLS, companies)):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows(rows)
    return len(people), len(companies)


# ------------------------------------------------------------------ GoHighLevel
def ghl_payload(s: dict, labels: dict) -> dict:
    t = target(s)
    street, city, state, zip_ = split_mailing(t["mailing"])
    types = [labels.get(x, x) for x in s["lead_types"]]
    return {
        "firstName": t["first"], "lastName": t["last"], "companyName": t["company"],
        "address1": street, "city": city, "state": state, "postalCode": zip_,
        "propertyAddress": s["address"], "propertyCity": s.get("city", ""), "propertyState": "OH",
        "propertyZip": s.get("zip", ""), "county": s["county"].title(), "parcelNumber": s.get("parcel", ""),
        "leadTypes": ", ".join(types), "stackTier": s.get("tier", ""), "sellerScore": s["score"],
        "ownedSince": s.get("owned_since", ""), "absenteeOwner": "yes" if s.get("absentee") else "no",
        "contactRole": t["role"], "ownerOnRecord": s.get("owner", ""), "stackId": s["id"],
        "lastActivity": s.get("last_date", ""),
        "notes": " | ".join(f"{x['source_label']}: {x['detail']}" for x in s.get("signals", [])[:6]),
        "tags": ["stacked", s["county"], (s.get("tier") or "").lower()] + list(s["lead_types"]),
        "source": "ohio-lead-stacker",
    }


def _post(url: str, payload: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def push_ghl(stacks: list[dict], sent_path: str, labels: dict, log=print, post=_post) -> int:
    url = os.environ.get("GHL_WEBHOOK_URL", "").strip()
    if not url:
        log("GHL push: no GHL_WEBHOOK_URL secret set - skipped")
        return 0
    try:
        with open(sent_path, encoding="utf-8") as f:
            sent = json.load(f)
    except Exception:  # noqa: BLE001
        sent = {}
    todo = [s for s in stacks
            if (s.get("tier") == "Hot" or s["score"] >= GHL_MIN_SCORE)
            and sorted(s["lead_types"]) != sent.get(s["id"])]
    todo.sort(key=lambda s: -s["score"])
    ok = failed = in_a_row = 0
    for s in todo[:GHL_MAX_PER_RUN]:
        payload = ghl_payload(s, labels)
        for attempt in range(2):
            try:
                status = post(url, payload)
            except Exception as e:  # noqa: BLE001 - HTTPError 422/429 when GHL is rate limiting
                status, err = 0, e
            if 200 <= status < 300:
                break
            if attempt == 0:
                time.sleep(GHL_BACKOFF)       # GHL rate limit: wait it out, then retry once
        if 200 <= status < 300:
            sent[s["id"]] = sorted(s["lead_types"])
            ok += 1
            in_a_row = 0
        else:
            failed += 1
            in_a_row += 1
            if failed <= 3:
                log(f"GHL push: {s['address']} failed ({err if status == 0 else status})")
            if in_a_row >= 5:
                log("GHL push: 5 rejects in a row even after waiting - stopping; the rest go next run")
                break
        time.sleep(GHL_DELAY)
    os.makedirs(os.path.dirname(sent_path), exist_ok=True)
    with open(sent_path, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=0, sort_keys=True)
    left = max(0, len(todo) - GHL_MAX_PER_RUN)
    log(f"GHL push: {ok} sent, {failed} failed" + (f", {left} more next run" if left else ""))
    return ok
