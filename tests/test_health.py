import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health  # noqa: E402

NOW = datetime(2026, 11, 12, 16, 0, tzinfo=timezone.utc)


def run(name, hours_ago, conclusion="success", event="schedule"):
    t = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"name": name, "event": event, "status": "completed", "conclusion": conclusion,
            "created_at": t, "html_url": "https://x"}


def test_healthy_daily():
    rows = health.check_repo("o", "r", NOW, [run("Scrape", h) for h in (3, 27, 51, 75)])
    assert rows[0]["problems"] == [] and rows[0]["usual_gap_hours"] == 24


def test_latest_failed_and_overdue():
    rows = health.check_repo("o", "r", NOW, [run("Scrape", 3, "failure")] + [run("Scrape", h) for h in (75, 99, 123)])
    probs = rows[0]["problems"]
    assert any("failure" in p for p in probs) and any("no successful run for 3.1 days" in p for p in probs)


def test_weekly_not_flagged_early_and_recon_ignored():
    runs = [run("Weekly", h) for h in (100, 268, 436)] + [run("recon", 1, "failure", "push")]
    rows = health.check_repo("o", "r", NOW, runs)
    assert [r["workflow"] for r in rows] == ["Weekly"] and rows[0]["problems"] == []


def test_push_only_workflows_skipped():
    assert health.check_repo("o", "r", NOW, [run("Deploy", 2, "failure", "push")]) == []


def test_new_workflow_run_by_hand_counts():
    rows = health.check_repo("o", "r", NOW, [run("Divorce leads", 5, "success", "workflow_dispatch")])
    assert len(rows) == 1 and rows[0]["problems"] == []
