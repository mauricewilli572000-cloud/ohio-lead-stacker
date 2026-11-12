"""Fill in Franklin County parcel IDs from the address, using the Auditor's monthly parcel file.

Franklin foreclosure, eviction and Columbus code-enforcement records mostly come without a
parcel ID. The Auditor publishes every parcel's site address each month (Parcel.txt inside
Tab-Delimited.zip). This builds an address -> parcel lookup from it, so a record at "874 SUMMIT ST"
can join the Auditor's "872 - 874 SUMMIT ST" parcel, and same-named streets are told apart by ZIP.

The lookup is cached (data/cache) and only rebuilt when the Auditor posts a new monthly drop.
A record only gets a parcel when the answer is unambiguous.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.parse
import zipfile

from .normalize import normalize_address, normalize_parcel

OUTSIDE_FILES = "https://apps.franklincountyauditor.com/Outside_User_Files/"
ZIP_NAME = "Tab-Delimited.zip"
MAX_RANGE = 12          # expand house-number ranges up to this many numbers

csv.field_size_limit(10_000_000)


def _session():
    try:
        from curl_cffi import requests as http
        return http.Session(impersonate="chrome")
    except Exception:  # noqa: BLE001 - fall back to plain requests/urllib
        try:
            import requests as http
            s = http.Session()
            s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"
            return s
        except Exception:  # noqa: BLE001
            return None


def _get(s, url, timeout=600):
    last = None
    for attempt in range(3):
        try:
            r = s.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def latest_drops(s) -> list[tuple[str, str]]:
    """[(folder name, url)] of the Auditor's 'Tax Accounting' drops, newest first."""
    page = _get(s, OUTSIDE_FILES, 60).text
    years = sorted(set(re.findall(r"/Outside_User_Files/(\d{4})/", page)), reverse=True)
    drops = []
    for y in years[:2]:
        url = f"{OUTSIDE_FILES}{y}/"
        for h in re.findall(r'href="([^"]+)"', _get(s, url, 60).text, re.I):
            h = urllib.parse.urljoin(url, h)
            name = urllib.parse.unquote(h.rstrip("/").rsplit("/", 1)[-1])
            if h.endswith("/") and "Tax Accounting" in name and re.match(r"\d{4}-\d{2}-\d{2}", name):
                drops.append((name, h))
    return sorted(drops, reverse=True)


RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s+(.+)$")


def site_keys(street: str) -> list[str]:
    """Address keys for one parcel. '872 - 874 SUMMIT ST' -> keys for 872 and 874 (and between)."""
    street = re.sub(r"\s+", " ", street or "").strip()
    m = RANGE_RE.match(street)
    if not m:
        k = normalize_address(street)
        return [k] if k else []
    a, b, rest = int(m.group(1)), int(m.group(2)), m.group(3)
    if b < a or b - a > 2 * MAX_RANGE:
        nums = [a, b]
    else:
        step = 2 if (b - a) % 2 == 0 else 1
        nums = list(range(a, b + 1, step))
    return [k for k in dict.fromkeys(normalize_address(f"{n} {rest}") for n in nums) if k]


def build_index(rows) -> dict[str, list[list[str]]]:
    """rows of Parcel.txt -> {address_key: [[zip, parcel9], ...]}."""
    idx: dict[str, set] = {}
    for r in rows:
        pid = normalize_parcel(r.get("PARCEL ID"), "franklin")
        site = r.get("SiteAddress") or ""
        if not pid or not site:
            continue
        parts = [p.strip() for p in site.split(",")]
        m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", site)
        zipc = m.group(1) if m else re.sub(r"\D", "", str(r.get("ZipCode") or ""))[:5]
        for k in site_keys(parts[0] if parts else ""):
            idx.setdefault(k, set()).add((zipc, pid))
    return {k: sorted(map(list, v)) for k, v in idx.items()}


def lookup(idx: dict, address_key: str, zipc: str = "") -> str | None:
    """Parcel for this address, or None when unknown or ambiguous."""
    cands = idx.get(address_key or "")
    if not cands:
        return None
    if zipc:
        same = {p for z, p in cands if z == zipc}
        if len(same) == 1:
            return same.pop()
        if len(same) > 1:
            return None
    parcels = {p for _, p in cands}
    return parcels.pop() if len(parcels) == 1 else None


def load_index(cache_dir: str, log=print) -> dict | None:
    """Cached lookup, rebuilt when the Auditor has a newer drop. None if unavailable."""
    path = os.path.join(cache_dir, "franklin_parcel_index.json")
    cached = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:  # noqa: BLE001
            cached = None
    s = _session()
    if s is None:
        return cached and cached["index"]
    try:
        drops = latest_drops(s)
    except Exception as e:  # noqa: BLE001
        log(f"Franklin parcel lookup: Auditor drop list unavailable ({e}); using cached copy")
        return cached and cached["index"]
    for name, url in drops[:4]:
        if cached and cached.get("drop") == name:
            return cached["index"]
        try:
            data = _get(s, url + urllib.parse.quote(ZIP_NAME)).content
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                member = next((i for i in z.infolist() if i.filename.rsplit("/", 1)[-1] == "Parcel.txt"), None)
                if member is None:
                    continue
                with z.open(member) as raw:
                    text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
                    idx = build_index(csv.DictReader(text, delimiter="\t"))
        except Exception as e:  # noqa: BLE001
            log(f"Franklin parcel lookup: drop {name} unusable ({e})")
            continue
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"drop": name, "index": idx}, f, separators=(",", ":"))
        log(f"Franklin parcel lookup: built from {name} ({len(idx)} addresses)")
        return idx
    return cached and cached["index"]


def fill_parcels(signals: list[dict], idx: dict) -> int:
    """Give Franklin signals without a parcel one from the address lookup. Returns how many were filled."""
    n = 0
    for s in signals:
        if s.get("county") != "franklin" or s.get("parcel") or not s.get("address_key"):
            continue
        p = lookup(idx, s["address_key"], s.get("zip") or "")
        if p:
            s["parcel"] = p
            s["parcel_from_address"] = True
            n += 1
    return n
