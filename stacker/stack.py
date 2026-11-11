"""Group signals into properties and score each property's stack."""
import hashlib
from collections import defaultdict
from datetime import date

from .normalize import normalize_address, owner_tokens

WEIGHTS = {
    "foreclosure": 30,
    "probate": 30,
    "tax_delinquent": 25,
    "code_violation": 20,
    "eviction": 15,
}
LABELS = {
    "foreclosure": "Foreclosure",
    "probate": "Probate",
    "tax_delinquent": "Tax delinquent",
    "code_violation": "Code violation",
    "eviction": "Eviction",
}
# Which source's owner/mailing to trust first. Auditor-enriched sources first.
OWNER_PRIORITY = ["probate", "code_violation", "tax_delinquent", "foreclosure", "eviction"]


class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def group_signals(signals):
    """Return lists of signal indexes that refer to the same property.

    Two signals are the same property when, within one county, they share a
    parcel ID, or they share a normalized street address and their ZIPs don't
    disagree. A signal without a ZIP joins an address only if every signal at
    that address has the same ZIP (otherwise it's ambiguous and stays alone).
    """
    uf = _UF(len(signals))
    by_parcel = defaultdict(list)
    by_addr = defaultdict(list)
    for i, s in enumerate(signals):
        if s["parcel"]:
            by_parcel[(s["county"], s["parcel"])].append(i)
        if s["address_key"]:
            by_addr[(s["county"], s["address_key"])].append(i)

    for idx in by_parcel.values():
        for j in idx[1:]:
            uf.union(idx[0], j)

    for idx in by_addr.values():
        by_zip = defaultdict(list)
        no_zip = []
        for i in idx:
            (by_zip[signals[i]["zip"]] if signals[i]["zip"] else no_zip).append(i)
        for members in by_zip.values():
            for j in members[1:]:
                uf.union(members[0], j)
        # Different ZIPs at the same street address: only merge when the owner
        # names agree (catches ZIP typos without merging two different towns).
        zgroups = list(by_zip.values())
        for a in range(len(zgroups)):
            for b in range(a + 1, len(zgroups)):
                ta = set().union(*(owner_tokens(signals[i]["owner"]) for i in zgroups[a]))
                tb = set().union(*(owner_tokens(signals[i]["owner"]) for i in zgroups[b]))
                if ta & tb:
                    uf.union(zgroups[a][0], zgroups[b][0])
        if len(by_zip) == 1:
            anchor = next(iter(by_zip.values()))[0]
            for j in no_zip:
                uf.union(anchor, j)
        elif not by_zip:
            for j in no_zip[1:]:
                uf.union(no_zip[0], j)

    groups = defaultdict(list)
    for i in range(len(signals)):
        groups[uf.find(i)].append(i)
    return list(groups.values())


def _days_ago(d, today):
    try:
        return (today - date.fromisoformat(d)).days
    except (TypeError, ValueError):
        return None


def _owner_check(sigs):
    """'match' if owner names overlap, 'conflict' if two owners share no words."""
    sets = [owner_tokens(s["owner"]) | owner_tokens(s["contact_name"]) for s in sigs]
    sets = [t for t in sets if t]
    if len(sets) < 2:
        return "unknown"
    for a in range(len(sets)):
        for b in range(a + 1, len(sets)):
            if not (sets[a] & sets[b]):
                return "conflict"
    return "match"


def _is_absentee(mailing, prop_key):
    """Mailing address is somewhere other than the property itself."""
    if not mailing or not prop_key:
        return False
    m = normalize_address(mailing)
    if not m:                      # PO box etc.
        return True
    # Mailing strings often carry the city without a comma: '12 MAIN ST AKRON OH'
    return not (m == prop_key or m.startswith(prop_key + " "))


def stack_id(sigs):
    s = next((x for x in sigs if x["parcel"]), None)
    if s:
        key = f"{s['county']}|p|{s['parcel']}"
    else:
        s = next((x for x in sigs if x["address_key"]), sigs[0])
        key = f"{s['county']}|a|{s['address_key']}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def build_stack(sigs, today):
    types = sorted({s["lead_type"] for s in sigs}, key=lambda t: -WEIGHTS.get(t, 0))
    ordered = sorted(sigs, key=lambda s: (OWNER_PRIORITY.index(s["lead_type"])
                                          if s["lead_type"] in OWNER_PRIORITY else 99))
    owner_src = next((s for s in ordered if s["owner"]), {})
    mail_src = next((s for s in ordered if s["mailing"]), {})
    addr_src = next((s for s in ordered if s["address_key"] and s["zip"]), None) \
        or next((s for s in ordered if s["address_key"]), ordered[0])
    contact = next((s for s in ordered if s["phone"] or s["contact_name"]), None)
    dates = sorted(s["date"] for s in sigs if s["date"])
    newest = _days_ago(dates[-1], today) if dates else None
    owner_check = _owner_check(sigs)
    absentee = _is_absentee(mail_src.get("mailing"), addr_src["address_key"])

    score = sum(WEIGHTS.get(t, 10) for t in types)
    reasons = [f"{LABELS.get(t, t)} +{WEIGHTS.get(t, 10)}" for t in types]
    extra = len(sigs) - len(types)
    if extra > 0:
        bonus = min(10, 3 * extra)
        score += bonus
        reasons.append(f"{extra} more record{'s' if extra > 1 else ''} +{bonus}")
    if newest is not None and newest <= 30:
        score += 10
        reasons.append("Activity in last 30 days +10")
    elif newest is not None and newest <= 90:
        score += 5
        reasons.append("Activity in last 90 days +5")
    if absentee:
        score += 5
        reasons.append("Absentee owner +5")
    if owner_check == "conflict":
        score -= 15
        reasons.append("Owner names differ -15")
    score = max(0, min(100, score))

    return {
        "id": stack_id(sigs),
        "county": addr_src["county"],
        "address": addr_src["address"],
        "city": addr_src["city"] or next((s["city"] for s in sigs if s["city"]), ""),
        "zip": addr_src["zip"] or next((s["zip"] for s in sigs if s["zip"]), ""),
        "parcel": next((s["parcel"] for s in sigs if s["parcel"]), ""),
        "owner": owner_src.get("owner", ""),
        "mailing": mail_src.get("mailing", ""),
        "absentee": absentee,
        "contact_name": contact["contact_name"] if contact else "",
        "phone": contact["phone"] if contact else "",
        "lead_types": types,
        "type_count": len(types),
        "record_count": len(sigs),
        "tier": "Hot" if len(types) >= 3 else ("Warm" if len(types) == 2 else ""),
        "score": score,
        "score_reasons": reasons,
        "owner_check": owner_check,
        "first_date": dates[0] if dates else "",
        "last_date": dates[-1] if dates else "",
        "signals": [
            {k: s[k] for k in ("source_id", "source_label", "lead_type", "record_id",
                               "date", "owner", "detail", "url", "amount")}
            for s in sorted(sigs, key=lambda s: s["date"] or "", reverse=True)
        ],
    }


def build_stacks(signals, today=None, min_types=2):
    today = today or date.today()
    stacks = []
    for idx in group_signals(signals):
        sigs = [signals[i] for i in idx]
        if len({s["lead_type"] for s in sigs}) < min_types:
            continue
        stacks.append(build_stack(sigs, today))
    stacks.sort(key=lambda s: (-s["score"], -s["type_count"], s["address"]))
    return stacks
