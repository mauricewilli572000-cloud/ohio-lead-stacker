import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stacker.adapters import make_signal  # noqa: E402
from stacker.normalize import iso_date, normalize_address, normalize_parcel  # noqa: E402
from stacker.stack import build_stacks  # noqa: E402


def test_address_forms_match():
    same = [
        "1487 ELAINE ROAD APT D, COLUMBUS, OH 43227",
        "1487 Elaine Rd.",
        "1487 ELAINE RD #B",
    ]
    assert {normalize_address(a) for a in same} == {"1487 ELAINE RD"}
    assert normalize_address("33 SOUTH BURGESS AVENUE") == normalize_address("33 S Burgess Ave")
    assert normalize_address("3355 E FIFTH AVE") == normalize_address("3355 East 5th Avenue")
    assert normalize_address("1109 E 97TH ST") == normalize_address("1109 E 97 ST")
    assert normalize_address("27 S BURGESS AVE INCLUDING 25D AND 29") == "27 S BURGESS AVE"
    assert normalize_address("872 - 874 SUMMIT ST") == normalize_address("872 Summit St")


def test_unmatchable_addresses():
    for a in ("", None, "PO BOX 2861", "UNKNOWN", "MAIN ST"):
        assert normalize_address(a) is None


def test_different_houses_stay_apart():
    assert normalize_address("100 MAIN ST") != normalize_address("102 MAIN ST")
    assert normalize_address("100 N MAIN ST") != normalize_address("100 S MAIN ST")


def test_parcels():
    assert normalize_parcel("010-075397-00", "franklin") == "010075397"
    assert normalize_parcel("010-075397", "franklin") == "010075397"
    assert normalize_parcel("762-02-090", "cuyahoga") == normalize_parcel("76202090", "cuyahoga")
    assert normalize_parcel("000-000000", "franklin") is None
    assert normalize_parcel("01790074033100", "hamilton") == normalize_parcel("179-0074-0331-00", "hamilton")


def test_dates():
    assert iso_date("10/06/2026") == "2026-10-06"
    assert iso_date("2026-10-06T12:00:00") == "2026-10-06"
    assert iso_date("soon") == ""


def _src(lead_type, county="franklin"):
    return {"id": lead_type, "label": lead_type, "lead_type": lead_type, "county": county}


def test_stacking_by_address_and_zip_rules():
    sigs = [
        make_signal(_src("foreclosure"), record_id=1, address="12 OAK ST", zip_="43215", owner="SMITH JOHN"),
        make_signal(_src("code_violation"), record_id=2, address="12 Oak Street", zip_="43215", owner="JOHN SMITH"),
        # same street address in another town, different owner -> must not merge
        make_signal(_src("probate"), record_id=3, address="12 OAK ST", zip_="43081", owner="DOE JANE"),
        # same address, different county -> must not merge
        make_signal(_src("eviction", "cuyahoga"), record_id=4, address="12 OAK ST", zip_="43215"),
    ]
    stacks = build_stacks(sigs, date(2026, 11, 10))
    assert len(stacks) == 1
    assert stacks[0]["lead_types"] == ["foreclosure", "code_violation"]
    assert stacks[0]["owner_check"] == "match"


def test_stacking_by_parcel_even_when_address_differs():
    sigs = [
        make_signal(_src("probate"), record_id=1, address="5 ELM", parcel="010-000763"),
        make_signal(_src("foreclosure"), record_id=2, address="5 ELM AVE", parcel="010-000763-00"),
    ]
    assert len(build_stacks(sigs, date(2026, 11, 10))) == 1


def test_single_list_properties_are_not_stacked():
    sigs = [
        make_signal(_src("code_violation"), record_id=1, address="9 PINE ST", zip_="43215"),
        make_signal(_src("code_violation"), record_id=2, address="9 PINE ST", zip_="43215"),
    ]
    assert build_stacks(sigs, date(2026, 11, 10)) == []


def test_divorce_adapter():
    from stacker.adapters import ADAPTERS
    src = {"id": "hamilton-divorce", "label": "Hamilton divorce", "lead_type": "divorce", "county": "hamilton"}
    rec = {"case_number": "DR2601559", "case_label": "Dissolution", "filed_date": "2026-11-10",
           "case_url": "https://x", "spouses": [{"name": "Kenneth Easterling"}, {"name": "Ellen Isaac"}],
           "properties": [{"address": "4512 RAVENWOOD CT", "parcel": "0010001000100", "city": "Cincinnati",
                           "zip": "45244", "owner_name": "EASTERLING KENNETH C", "mailing_address": "",
                           "match": "home at spouse's address"}]}
    sigs = list(ADAPTERS["divorce"]([rec], src))
    assert len(sigs) == 1 and sigs[0]["lead_type"] == "divorce"
    assert sigs[0]["address_key"] and sigs[0]["date"] == "2026-11-10"
    assert "Kenneth Easterling" in sigs[0]["contact_name"]


def test_franklin_parcel_lookup():
    from stacker.franklin_parcels import build_index, fill_parcels, lookup, site_keys
    from stacker.normalize import normalize_address
    rows = [
        {"PARCEL ID": "010-075397-00", "SiteAddress": "872 - 874 SUMMIT ST, COLUMBUS, OH 43215"},
        {"PARCEL ID": "010-000001-00", "SiteAddress": "100 MAIN ST, COLUMBUS, OH 43215"},
        {"PARCEL ID": "100-000002-00", "SiteAddress": "100 MAIN ST, GROVEPORT, OH 43125"},
        {"PARCEL ID": "010-000003-00", "SiteAddress": "5 CONDO WAY UNIT A, COLUMBUS, OH 43215"},
        {"PARCEL ID": "010-000004-00", "SiteAddress": "5 CONDO WAY UNIT B, COLUMBUS, OH 43215"},
    ]
    assert len(site_keys("872 - 874 SUMMIT ST")) == 2
    idx = build_index(rows)
    assert lookup(idx, normalize_address("874 SUMMIT STREET")) == "010075397"
    assert lookup(idx, normalize_address("100 MAIN ST"), "43125") == "100000002"
    assert lookup(idx, normalize_address("100 MAIN ST")) is None          # two cities, no ZIP: ambiguous
    assert lookup(idx, normalize_address("5 CONDO WAY"), "43215") is None  # several units: ambiguous
    sigs = [{"county": "franklin", "parcel": None, "address_key": normalize_address("874 SUMMIT ST"), "zip": ""},
            {"county": "hamilton", "parcel": None, "address_key": normalize_address("874 SUMMIT ST"), "zip": ""}]
    assert fill_parcels(sigs, idx) == 1 and sigs[0]["parcel"] == "010075397" and sigs[1]["parcel"] is None
