"""Get stacked leads ready for outreach: skip-trace files and the GoHighLevel push.

Skip tracing
    docs/skiptrace_ready.csv      people, best leads first, in the column layout skip-trace services
                                  (BatchSkipTracing, PropStream, REISift, ...) import directly
    docs/skiptrace_companies.csv  LLC / corporate / trust owners: look up the registered agent
                                  (Ohio Secretary of State business search) before tracing
    When a property is in probate the owner has died, so the row is the estate contact
    (executor / heir from the probate case), not the deceased owner.

GoHighLevel
    New leads at or above GHL_MIN_SCORE (and every Hot lead) become GHL contacts.
    Preferred: the GHL API (secrets GHL_API_TOKEN + GHL_LOCATION_ID, a Private Integration with
    contacts read/write) - creates the contact, adds a note with the property and its signals, and
    remembers the contact ID so a lead that gains a new lead type updates the same contact.
    Fallback: an Inbound Webhook workflow (GHL_WEBHOOK_URL) - note GHL bills premium triggers after
    the first 100. Sent leads live in data/ghl_sent.json. With neither secret set this does nothing.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.error
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


GHL_API = "https://services.leadconnectorhq.com"
GHL_API_DELAY = 0.25       # API burst limit is 100 requests / 10 s; each lead is 2-3 requests


class GhlApi:
    def __init__(self, token: str, location: str):
        self.token, self.location = token, location

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(GHL_API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Version": "2021-07-28",
                                              "Content-Type": "application/json", "Accept": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    txt = r.read().decode() or "{}"
                    time.sleep(GHL_API_DELAY)
                    return json.loads(txt)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(12)
                    continue
                detail = e.read().decode(errors="replace")[:300]
                raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        raise RuntimeError("rate limited")


def contact_fields(s: dict, labels: dict) -> dict:
    t = target(s)
    street, city, state, zip_ = split_mailing(t["mailing"])
    if not street:     # no separate mailing address on file: use the property
        street, city, state, zip_ = s["address"], s.get("city", ""), "OH", s.get("zip", "")
    body = {"firstName": t["first"], "lastName": t["last"], "address1": street, "city": city.title(),
            "state": state, "postalCode": zip_, "source": "ohio-lead-stacker",
            "tags": ["stacked", s["county"], (s.get("tier") or "").lower()] + list(s["lead_types"])}
    if t["company"]:
        body.update({"companyName": t["company"], "name": t["company"]})
    return {k: v for k, v in body.items() if v not in ("", None)}


def contact_note(s: dict, labels: dict) -> str:
    t = target(s)
    lines = [f"Stacked lead - {s.get('tier', '')} - seller score {s['score']}",
             f"Property: {s['address']}, {s.get('city', '')} OH {s.get('zip', '')} ({s['county'].title()} County)",
             f"Parcel: {s.get('parcel', '') or 'n/a'}",
             f"Owner on record: {s.get('owner', '')}",
             f"Contact is: {t['role']}",
             f"Lists: {', '.join(labels.get(x, x) for x in s['lead_types'])}"]
    if s.get("owned_since"):
        lines.append(f"Owned since: {s['owned_since']}")
    if s.get("absentee"):
        lines.append("Absentee owner (mails elsewhere)")
    lines.append("")
    lines += [f"- {x['source_label']}: {x['detail']}" + (f" ({x['date']})" if x.get("date") else "")
              for x in s.get("signals", [])[:10]]
    return "\n".join(lines)


def push_ghl_api(stacks: list[dict], sent: dict, api: GhlApi, labels: dict, log=print) -> tuple[int, int]:
    todo = [s for s in stacks
            if (s.get("tier") == "Hot" or s["score"] >= GHL_MIN_SCORE) and _types(sent.get(s["id"])) != sorted(s["lead_types"])]
    todo.sort(key=lambda s: -s["score"])
    ok = failed = in_a_row = 0
    for s in todo[:GHL_MAX_PER_RUN]:
        prev = sent.get(s["id"])
        cid = prev.get("ghl_id") if isinstance(prev, dict) else None
        if prev is not None and not cid:
            # sent earlier through the webhook (no contact ID kept): don't create a duplicate
            sent[s["id"]] = {"types": sorted(s["lead_types"]), "ghl_id": None}
            continue
        try:
            fields = contact_fields(s, labels)
            if cid:
                api.call("PUT", f"/contacts/{cid}", {k: v for k, v in fields.items() if k != "tags"})
                api.call("POST", f"/contacts/{cid}/tags", {"tags": fields.get("tags", [])})
            else:
                res = api.call("POST", "/contacts/", {**fields, "locationId": api.location})
                cid = (res.get("contact") or {}).get("id") or res.get("id")
            try:
                api.call("POST", f"/contacts/{cid}/notes", {"body": contact_note(s, labels)})
            except Exception as e:  # noqa: BLE001 - a missing note shouldn't lose the contact
                log(f"GHL API: note for {s['address']} failed ({e})")
            sent[s["id"]] = {"types": sorted(s["lead_types"]), "ghl_id": cid}
            ok += 1
            in_a_row = 0
        except Exception as e:  # noqa: BLE001
            failed += 1
            in_a_row += 1
            if failed <= 3:
                log(f"GHL API: {s['address']} failed ({e})")
            if in_a_row >= 5:
                log("GHL API: 5 failures in a row - stopping; check the token's scopes and the location ID")
                break
    left = max(0, len(todo) - GHL_MAX_PER_RUN)
    log(f"GHL API: {ok} contacts created/updated, {failed} failed" + (f", {left} more next run" if left else ""))
    return ok, failed


def _types(v):
    """ghl_sent.json entries: old webhook format is a list of types; API format is {types, ghl_id}."""
    return v.get("types") if isinstance(v, dict) else v


def _post(url: str, payload: dict) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status


def push_ghl(stacks: list[dict], sent_path: str, labels: dict, log=print, post=_post, api=None) -> int:
    try:
        with open(sent_path, encoding="utf-8") as f:
            sent = json.load(f)
    except Exception:  # noqa: BLE001
        sent = {}
    token, location = os.environ.get("GHL_API_TOKEN", "").strip(), os.environ.get("GHL_LOCATION_ID", "").strip()
    if api is None and token and location:
        api = GhlApi(token, location)
    if api is not None:
        ok, _ = push_ghl_api(stacks, sent, api, labels, log)
        os.makedirs(os.path.dirname(sent_path), exist_ok=True)
        with open(sent_path, "w", encoding="utf-8") as f:
            json.dump(sent, f, indent=0, sort_keys=True)
        return ok
    url = os.environ.get("GHL_WEBHOOK_URL", "").strip()
    if not url:
        log("GHL push: no GHL_API_TOKEN/GHL_LOCATION_ID or GHL_WEBHOOK_URL secret set - skipped")
        return 0
    todo = [s for s in stacks
            if (s.get("tier") == "Hot" or s["score"] >= GHL_MIN_SCORE)
            and sorted(s["lead_types"]) != _types(sent.get(s["id"]))]
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
