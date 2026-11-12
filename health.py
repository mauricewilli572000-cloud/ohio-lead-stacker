"""Daily health check for every scraper repo.

For each repo listed in health.json it reads the repo's GitHub Actions history (public API) and,
per scheduled workflow, reports a problem when:
  * the latest finished run FAILED (or was cancelled / timed out), or
  * the last SUCCESSFUL run is overdue: older than 2x that workflow's usual gap between runs + 6 hours
    (so daily scrapers are flagged after ~2 days, weekly ones after ~2 weeks).
Test/recon workflows and GitHub Pages builds are ignored. It also flags stacker feeds that could not
be read today (the stacker fell back to a cached copy).

Writes docs/health.json (shown on docs/health.html). With --issue, opens one GitHub issue listing the
problems (GitHub emails you about it), updates it while problems remain, and closes it once all is well.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
IGNORE_WORDS = ("recon", "pages build", "pages-build", "copilot", "dependabot")
ISSUE_TITLE = "Scraper health check: problems found"


def gh(path, method="GET", body=None):
    req = urllib.request.Request(API + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "null")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours(a, b):
    return (a - b).total_seconds() / 3600


def check_repo(owner, repo, now, runs=None):
    """List of workflow summaries + problems for one repo."""
    if runs is None:
        runs = gh(f"/repos/{owner}/{repo}/actions/runs?per_page=100").get("workflow_runs", [])
    by_wf = {}
    for r in runs:
        name = r.get("name") or ""
        if any(w in name.lower() for w in IGNORE_WORDS):
            continue
        by_wf.setdefault(name, []).append(r)
    out = []
    for name, rs in sorted(by_wf.items()):
        sched = sorted((ts(r["created_at"]) for r in rs if r.get("event") == "schedule"), reverse=True)
        if not sched:
            continue                       # not a scheduled scraper workflow (manual-only, push-only)
        gaps = [hours(a, b) for a, b in zip(sched, sched[1:]) if hours(a, b) > 0.5]
        usual = statistics.median(gaps) if gaps else 24.0
        done = [r for r in rs if r.get("status") == "completed"]
        last = max(done, key=lambda r: r["created_at"]) if done else None
        ok = [r for r in done if r.get("conclusion") == "success"]
        last_ok = max(ok, key=lambda r: r["created_at"]) if ok else None
        since_ok = hours(now, ts(last_ok["created_at"])) if last_ok else None
        limit = 2 * usual + 6
        problems = []
        if last and last.get("conclusion") not in ("success", "skipped", "neutral"):
            problems.append(f"latest run {last.get('conclusion')} ({last['created_at'][:16].replace('T', ' ')} UTC)")
        if since_ok is None:
            problems.append("no successful run on record")
        elif since_ok > limit:
            problems.append(f"no successful run for {since_ok / 24:.1f} days (usually runs every {usual:.0f}h)")
        out.append({
            "repo": repo, "workflow": name,
            "last_run": last["created_at"] if last else "", "last_result": last.get("conclusion") if last else "",
            "last_success": last_ok["created_at"] if last_ok else "",
            "usual_gap_hours": round(usual, 1),
            "url": (last or {}).get("html_url", f"https://github.com/{owner}/{repo}/actions"),
            "problems": problems,
        })
    return out


def feed_problems():
    """Stacker feeds that fell back to a cached copy in today's stacked.json."""
    try:
        with open(os.path.join(ROOT, "docs", "stacked.json"), encoding="utf-8") as f:
            srcs = json.load(f).get("sources", [])
    except Exception:  # noqa: BLE001
        return []
    return [{"repo": s.get("id", ""), "workflow": "feed read by the stacker", "problems": [s.get("status", "")],
             "url": ""} for s in srcs if str(s.get("status", "ok")) != "ok"]


def issue_body(problems, generated):
    lines = [f"Health check at {generated} UTC found {len(problems)} problem(s):", ""]
    for p in problems:
        link = f" ([run]({p['url']}))" if p.get("url") else ""
        lines.append(f"- **{p['repo']}** / {p['workflow']}: {'; '.join(p['problems'])}{link}")
    lines += ["", "This issue updates every day and closes itself once everything is healthy.",
              "Full status: https://mauricewilli572000-cloud.github.io/ohio-lead-stacker/health.html"]
    return "\n".join(lines)


def sync_issue(owner, repo, problems, generated):
    issues = gh(f"/repos/{owner}/{repo}/issues?state=open&per_page=100")
    mine = [i for i in issues if i.get("title") == ISSUE_TITLE and "pull_request" not in i]
    if problems:
        body = issue_body(problems, generated)
        if mine:
            gh(f"/repos/{owner}/{repo}/issues/{mine[0]['number']}", "PATCH", {"body": body})
            # a comment makes GitHub notify again when the list of problems changes
            if mine[0].get("body", "").split("found", 1)[-1] != body.split("found", 1)[-1]:
                gh(f"/repos/{owner}/{repo}/issues/{mine[0]['number']}/comments", "POST",
                   {"body": "Problems changed:\n\n" + body})
        else:
            gh(f"/repos/{owner}/{repo}/issues", "POST", {"title": ISSUE_TITLE, "body": body})
    else:
        for i in mine:
            gh(f"/repos/{owner}/{repo}/issues/{i['number']}/comments", "POST",
               {"body": f"All scrapers healthy as of {generated} UTC. Closing."})
            gh(f"/repos/{owner}/{repo}/issues/{i['number']}", "PATCH", {"state": "closed"})


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", action="store_true", help="open/update/close the GitHub health issue")
    args = ap.parse_args(argv)
    with open(os.path.join(ROOT, "health.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    owner = cfg["owner"]
    now = datetime.now(timezone.utc)
    generated = now.strftime("%Y-%m-%d %H:%M")
    workflows, problems = [], []
    for repo in cfg["repos"]:
        try:
            rows = check_repo(owner, repo, now)
            if not rows:
                rows = [{"repo": repo, "workflow": "(no scheduled workflow found)", "problems": [
                    "no scheduled runs in recent history"], "url": f"https://github.com/{owner}/{repo}/actions"}]
        except urllib.error.HTTPError as e:
            rows = [{"repo": repo, "workflow": "-", "problems": [f"could not read Actions history (HTTP {e.code})"],
                     "url": f"https://github.com/{owner}/{repo}"}]
        workflows += rows
        problems += [r for r in rows if r["problems"]]
    feeds = feed_problems()
    problems += feeds
    out = {"generated_at": generated, "problem_count": len(problems), "workflows": workflows, "feeds": feeds}
    with open(os.path.join(ROOT, "docs", "health.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(f"{len(workflows)} workflows checked, {len(problems)} problem(s)")
    for p in problems:
        print(f"  PROBLEM {p['repo']} / {p['workflow']}: {'; '.join(p['problems'])}")
    if args.issue:
        sync_issue(owner, cfg.get("issue_repo", "ohio-lead-stacker"), problems, generated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
