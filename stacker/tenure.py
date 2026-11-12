"""Owner tenure and owner-occupancy for stacked properties.

For each stacked property with a parcel ID, look up when it last sold, then adjust the score:
    owned 20+ years  +8      owned 10-19 years  +4
(Long-held homes tend to have more equity and owners more open to selling.)
The homestead flag is deliberately NOT used: in Ohio it marks the senior/disability exemption,
and leads are not ranked by an owner's age or disability.

Sources (all public parcel data, cached between runs):
    franklin   Auditor monthly Parcel.txt (loaded by franklin_parcels.py)
    hamilton   CAGIS quarterly parcel CSV (SALDAT) - rebuilt only when a new quarter is posted
    cuyahoga   Fiscal Officer countywide appraisal parcels (GIS query for just the stacked parcels)
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.parse
from datetime import date, datetime, timezone

from .franklin_parcels import _get, _session, sale_date
from .normalize import normalize_parcel

csv.field_size_limit(10_000_000)

CAGIS_FOLDER = "https://cagis.org/Opendata/Quarterly_GIS_Data/"
CAGIS_NAME_RE = re.compile(r"Hamilton[ _]*County[ _]*Parcels[ _]*([A-Za-z]+)[ _]*(\d{4})\.csv", re.I)
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
CUY_BASE = "https://gis.cuyahogacounty.gov/server/rest/services/CCFO/Current_Year_Appraisal_Parcels/MapServer"
CUY_LAYERS = [2, 0]
CUY_REFRESH_DAYS = 30


def parse_date(v) -> str:
    """Many formats -> 'YYYY-MM-DD' ('' if none): epoch ms, YYYYMMDD, M/D/YYYY, ISO, DD-MON-YY(YY)."""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)) and v > 10_000_000_000:          # epoch milliseconds
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date().isoformat()
    t = str(v).strip()
    if re.fullmatch(r"\d{8}", t):
        return f"{t[:4]}-{t[4:6]}-{t[6:]}" if 1800 < int(t[:4]) < 2100 else ""
    if re.fullmatch(r"-?\d{11,}", t):
        return parse_date(int(t))
    d = sale_date(t)
    if d:
        return d
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(t[:11].title(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def years_since(iso: str, today: date) -> float | None:
    try:
        return (today - date.fromisoformat(iso)).days / 365.25
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ Hamilton (CAGIS)
def _cagis_date(name: str) -> tuple[int, int]:
    m = CAGIS_NAME_RE.search(urllib.parse.unquote(name))
    if not m or m.group(1).lower() not in MONTHS:
        return (0, 0)
    return (int(m.group(2)), MONTHS.index(m.group(1).lower()) + 1)


def hamilton_parcel_keys(row) -> list[str]:
    g = lambda k: re.sub(r"\s+", "", str(row.get(k) or ""))
    keys = []
    for raw in (g("PROPTYID"), g("AUDPTYID"),
                (g("BOOK").zfill(3) + g("PAGE").zfill(4) + g("PARCEL").zfill(4) + "00") if g("BOOK") else ""):
        k = normalize_parcel(raw, "hamilton")
        if k and k not in keys:
            keys.append(k)
    return keys


def build_hamilton_info(rows) -> dict:
    info = {}
    for r in rows:
        sold = parse_date(r.get("SALDAT"))
        if not sold:
            continue
        for k in hamilton_parcel_keys(r):
            info[k] = sold
    return info


def load_hamilton(cache_dir: str, log=print) -> dict:
    path = os.path.join(cache_dir, "hamilton_parcel_info.json")
    cached = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:  # noqa: BLE001
            cached = None
    s = _session()
    if s is None:
        return (cached or {}).get("info", {})
    try:
        page = _get(s, CAGIS_FOLDER, 60).text
        names = [h for h in re.findall(r'href=["\']([^"\']+\.csv)["\']', page, re.I)
                 if CAGIS_NAME_RE.search(urllib.parse.unquote(h))]
        if not names:
            raise RuntimeError("no parcel CSV listed")
        newest = max(names, key=_cagis_date)
    except Exception as e:  # noqa: BLE001
        log(f"Hamilton tenure: CAGIS folder unavailable ({e}); using cached copy")
        return (cached or {}).get("info", {})
    if cached and cached.get("file") == newest:
        return cached["info"]
    try:
        data = _get(s, urllib.parse.urljoin(CAGIS_FOLDER, newest), 900).content
        rows = csv.DictReader(io.StringIO(data.decode("latin-1")))
        info = build_hamilton_info(rows)
    except Exception as e:  # noqa: BLE001
        log(f"Hamilton tenure: CAGIS file unusable ({e}); using cached copy")
        return (cached or {}).get("info", {})
    log(f"Hamilton tenure: built from {urllib.parse.unquote(newest)} ({len(info)} parcel keys with a sale date)")
    os.makedirs(cache_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"file": newest, "info": info}, f, separators=(",", ":"))
    return info


# ------------------------------------------------------------------ Cuyahoga (county GIS)
def load_cuyahoga(cache_dir: str, parcels: set[str], today: date, log=print) -> dict:
    """{parcel: last_sale} for the given parcels; queried in small batches, cached 30 days."""
    path = os.path.join(cache_dir, "cuyahoga_parcel_info.json")
    cache = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001
            cache = {}
    def age_days(v):
        y = years_since(v[1] if isinstance(v, list) and len(v) > 1 else "", today)
        return y * 365.25 if y is not None else 9999
    fresh = {p for p, v in cache.items() if age_days(v) < CUY_REFRESH_DAYS}
    todo = sorted(p for p in parcels if p not in fresh)
    s = _session() if todo else None
    got = 0
    for i in range(0, len(todo), 50):
        if s is None:
            break
        batch = todo[i:i + 50]
        where = "PARCELPIN IN (" + ",".join(f"'{p}'" for p in batch) + ")"
        for layer in CUY_LAYERS:
            try:
                r = s.get(f"{CUY_BASE}/{layer}/query", params={
                    "where": where, "outFields": "PARCELPIN,last_transfer_date",
                    "returnGeometry": "false", "f": "json"}, timeout=90)
                for f in (r.json().get("features") or []):
                    a = f.get("attributes") or {}
                    p = normalize_parcel(a.get("PARCELPIN"), "cuyahoga")
                    if p:
                        cache[p] = [parse_date(a.get("last_transfer_date")), today.isoformat()]
                        got += 1
            except Exception as e:  # noqa: BLE001
                log(f"Cuyahoga tenure: query failed ({e})")
            time.sleep(1)
    if todo:
        log(f"Cuyahoga tenure: looked up {len(todo)} parcels, {got} found")
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
    return {p: v[0] for p, v in cache.items() if isinstance(v, list) and v[0]}


# ------------------------------------------------------------------ apply
def apply(stacks: list[dict], sold_by_county: dict, today: date) -> int:
    """Add owned_since / years_owned to stacks and adjust scores. Returns how many got a sale date."""
    n = 0
    for s in stacks:
        sold = (sold_by_county.get(s["county"]) or {}).get(s.get("parcel") or "")
        yrs = years_since(sold, today) if sold else None
        if yrs is None or yrs < 0:
            continue
        s["owned_since"] = sold[:4]
        s["years_owned"] = round(yrs, 1)
        n += 1
        if yrs >= 20:
            s["score"] += 8
            s["score_reasons"].append(f"Owned {int(yrs)} years (since {sold[:4]}) +8")
        elif yrs >= 10:
            s["score"] += 4
            s["score_reasons"].append(f"Owned {int(yrs)} years (since {sold[:4]}) +4")
        s["score"] = max(0, min(100, s["score"]))
    stacks.sort(key=lambda s: (-s["score"], -s["type_count"], s["address"]))
    return n
