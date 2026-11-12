#!/usr/bin/env python3
"""Ohio lead stacker.

Reads the JSON feeds your scraper repos already publish (read-only), matches
records that point at the same property, and writes:

    docs/stacked.json        properties with 2+ different lead types (dashboard)
    docs/stacked_ghl.csv     one row per property for GoHighLevel, tags per signal
    data/history.json        first date each stacked property was seen

Usage:
    python run_stacker.py                       # pull feeds from GitHub
    python run_stacker.py --local ../repos      # read from local clones instead
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone

from stacker.adapters import ADAPTERS
from stacker.franklin_parcels import fill_parcels as fill_franklin_parcels, load_index as load_franklin_index
from stacker import tenure
from stacker.stack import LABELS, build_stacks

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "data", "cache")


def _records(payload):
    if isinstance(payload, list):
        return payload
    for k in ("records", "leads", "items", "data", "rows"):
        if isinstance(payload.get(k), list):
            return payload[k]
    raise ValueError("no record list found in feed")


def fetch(src, cfg, local_root):
    """Return (records, status). Falls back to the last good cached copy."""
    cache_path = os.path.join(CACHE, f"{src['id']}.json")
    try:
        if local_root:
            with open(os.path.join(local_root, src["repo"], src["path"]), encoding="utf-8-sig") as f:
                payload = json.load(f)
        else:
            url = (f"https://raw.githubusercontent.com/{cfg['owner']}/{src['repo']}/"
                   f"{cfg.get('branch', 'main')}/{src['path']}")
            with urllib.request.urlopen(url, timeout=60) as r:
                payload = json.loads(r.read().decode("utf-8-sig"))
        recs = _records(payload)
        os.makedirs(CACHE, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(recs, f)
        return recs, "ok"
    except Exception as e:  # noqa: BLE001 - one bad feed must not stop the run
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f), f"cached copy used ({e})"
        return [], f"failed ({e})"


def load_history():
    p = os.path.join(ROOT, "data", "history.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1 if path.endswith("history.json") else None,
                  ensure_ascii=False)


def split_name(owner):
    """'SMITH, JOHN A' -> ('John A', 'Smith'); entities go entirely in last name."""
    o = (owner or "").strip()
    if not o:
        return "", ""
    if any(w in o.upper().split() for w in ("LLC", "INC", "TRUST", "BANK", "CORP", "LTD", "LP", "CO")):
        return "", o
    o = o.split("&")[0].strip()
    if "," in o:
        last, first = [x.strip() for x in o.split(",", 1)]
    else:
        parts = o.split()
        last, first = (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (o, "")
    return first.title(), last.title()


def split_mailing(m):
    """'123 Main St, Columbus OH 43215' -> street, city, state, zip (best effort)."""
    import re
    m = (m or "").strip()
    zm = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", m)
    zip_ = zm.group(1) if zm else ""
    rest = m[:zm.start()].strip(" ,") if zm else m
    sm = re.search(r"[ ,]([A-Z]{2})$", rest)
    state = sm.group(1) if sm else ""
    rest = rest[:sm.start()].strip(" ,") if sm else rest
    if "," in rest:
        street, city = [x.strip() for x in rest.rsplit(",", 1)]
    else:
        street, city = rest, ""
    return street, city, state, zip_


def write_ghl(path, stacks):
    cols = ["First Name", "Last Name", "Phone", "Mailing Address", "Mailing City",
            "Mailing State", "Mailing Zip", "Property Address", "Property City",
            "Property State", "Property Zip", "Lead Type", "Stack Tier", "Signal Count",
            "Seller Score", "Tags", "Contact Name", "Last Activity", "Owned Since", "Notes"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in stacks:
            first, last = split_name(s["owner"])
            street, city, state, zip_ = split_mailing(s["mailing"])
            types = [LABELS.get(t, t) for t in s["lead_types"]]
            w.writerow([
                first, last, s["phone"], street, city, state, zip_,
                s["address"], s["city"], "OH", s["zip"],
                "Stacked: " + " + ".join(types), s["tier"], s["type_count"], s["score"],
                ",".join(["stacked"] + [t for t in s["lead_types"]] + [s["county"]]),
                s["contact_name"], s["last_date"], s.get("owned_since", ""),
                " | ".join(f"{x['source_label']}: {x['detail']}" for x in s["signals"][:6]),
            ])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="folder containing local clones of the scraper repos")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    args = ap.parse_args(argv)

    with open(os.path.join(ROOT, "sources.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    today = date.fromisoformat(args.today) if args.today else date.today()

    signals, source_report = [], []
    for src in cfg["sources"]:
        recs, status = fetch(src, cfg, args.local)
        sigs = list(ADAPTERS[src["adapter"]](recs, src))
        matchable = sum(1 for s in sigs if s["address_key"] or s["parcel"])
        signals.extend(sigs)
        source_report.append({"id": src["id"], "label": src["label"],
                              "lead_type": src["lead_type"], "county": src["county"],
                              "records": len(recs), "signals": len(sigs),
                              "matchable": matchable, "status": status})
        print(f"{src['label']:<30} {len(recs):>6} records  {len(sigs):>6} signals  "
              f"{matchable:>6} matchable  {status}")

    # Franklin records mostly lack parcel IDs: fill them in from the Auditor's address list.
    franklin_data = None
    if not args.local and any(x["county"] == "franklin" for x in signals):
        franklin_data = load_franklin_index(CACHE)
    franklin_idx = franklin_data and franklin_data.get("index")
    if franklin_idx:
        import copy
        before = len(build_stacks(copy.deepcopy(signals), today))
        filled = fill_franklin_parcels(signals, franklin_idx)
        stacks = build_stacks(signals, today)
        print(f"Franklin parcel lookup: parcel IDs added to {filled} records; "
              f"stacked properties {before} -> {len(stacks)}")
    else:
        stacks = build_stacks(signals, today)

    # How long the owner has held each stacked property (long-held homes = more equity).
    if not args.local:
        sold = {"franklin": (franklin_data or {}).get("info") or {}}
        if any(s["county"] == "hamilton" and s["parcel"] for s in stacks):
            sold["hamilton"] = tenure.load_hamilton(CACHE)
        cuy = {s["parcel"] for s in stacks if s["county"] == "cuyahoga" and s["parcel"]}
        if cuy:
            sold["cuyahoga"] = tenure.load_cuyahoga(CACHE, cuy, today)
        n = tenure.apply(stacks, sold, today)
        print(f"Owner tenure: sale dates found for {n} of {len(stacks)} stacked properties; "
              f"{sum(1 for s in stacks if (s.get('years_owned') or 0) >= 20)} owned 20+ years")

    history = load_history()
    run_day = today.isoformat()
    for s in stacks:
        s["added_date"] = history.setdefault(s["id"], run_day)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": source_report,
        "counts": {
            "signals": len(signals),
            "stacked": len(stacks),
            "hot": sum(1 for s in stacks if s["tier"] == "Hot"),
            "warm": sum(1 for s in stacks if s["tier"] == "Warm"),
        },
        "stacks": stacks,
    }
    save_json(os.path.join(ROOT, "docs", "stacked.json"), out)
    save_json(os.path.join(ROOT, "data", "history.json"), history)
    write_ghl(os.path.join(ROOT, "docs", "stacked_ghl.csv"), stacks)

    print(f"\n{len(signals)} signals -> {len(stacks)} stacked properties "
          f"({out['counts']['hot']} hot, {out['counts']['warm']} warm)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
