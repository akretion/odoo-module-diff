"""Discover existing OCA migration PRs for an addon.

When an OCA addon is not migrated to the target serie yet, someone may
already have a migration PR open. This module searches the addon's OCA
repository for candidate migration PRs and ranks them:

Matching signals (strongest first):

- labels ``mod:<addon>`` AND ``series:<target>.0`` (l10n-brazil
  convention; a PR can carry several ``mod:`` labels);
- ``[MIG]`` in the title (with the addon name in the title);
- ``-mig-`` in the head branch name.

Enrichment per candidate:

- CI: check-runs on the head SHA — failures other than coverage
  (codecov/coverage jobs) make the PR ``partial``; coverage-only
  failures are tolerated;
- reviews: number of approving reviewers and the ``approved`` label
  (OCA sets it after 2 approvals).

Verdicts: ``credible`` (green CI + 2+ approvals), ``promising`` (green
CI, fewer approvals), ``partial`` (CI red beyond coverage, or pending —
usable as a starting point for hard migrations only), ``stale``
(closed unmerged). Without a GitHub token the discovery reports
``unavailable`` and never fails the plan.
"""

import re
from typing import Dict, List, Optional

from odoo_module_diff.external_milestones import GITHUB_API, _api_get, _github_token

_COVERAGE_RE = re.compile(r"coverage|codecov", re.IGNORECASE)

VERDICT_RANK = {"credible": 0, "promising": 1, "partial": 2, "stale": 3, "merged": 4}


def _search_prs(slug: str, query: str) -> List[dict]:
    data = _api_get(f"{GITHUB_API}/search/issues?q={query}&per_page=20")
    if not data or not isinstance(data, dict):
        return []
    return data.get("items", [])


def _check_runs_status(slug: str, sha: str) -> str:
    """'green' | 'coverage-only' | 'red' | 'pending' | 'unknown'."""
    data = _api_get(f"{GITHUB_API}/repos/{slug}/commits/{sha}/check-runs?per_page=50")
    if not data or not isinstance(data, dict):
        return "unknown"
    runs = data.get("check_runs", [])
    if not runs:
        return "unknown"
    failures = []
    pending = False
    for run in runs:
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status != "completed":
            pending = True
            continue
        if conclusion in ("success", "neutral", "skipped"):
            continue
        if _COVERAGE_RE.search(run.get("name") or ""):
            continue
        failures.append(run.get("name") or "?")
    if failures:
        return "red"
    if pending:
        return "pending"
    return "green"


def _approval_count(slug: str, number: int) -> int:
    data = _api_get(f"{GITHUB_API}/repos/{slug}/pulls/{number}/reviews?per_page=100")
    if not data or not isinstance(data, list):
        return 0
    # latest review state per reviewer counts
    latest: Dict[str, str] = {}
    for review in data:
        user = (review.get("user") or {}).get("login")
        state = review.get("state") or ""
        if user and state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            latest[user] = state
    return sum(1 for state in latest.values() if state == "APPROVED")


def _matches_addon(pr: dict, addon: str, target_serie: int) -> int:
    """Match strength: 3 = mod+series labels, 2 = [MIG]+addon+serie in
    title, 1 = <serie>.0-mig- branch, 0 = no match. The target serie
    evidence is always required: migration PRs of OTHER series are not
    candidates."""
    labels = {l.get("name", "") for l in pr.get("labels", [])}
    title = pr.get("title") or ""
    head_ref = ((pr.get("head") or {}).get("ref")) or ""
    serie = f"{target_serie}.0"
    is_migration = "[mig]" in title.lower() or "-mig-" in head_ref
    if not is_migration:
        return 0  # [FIX]/[FW]/[UPD] PRs of the module are not migrations
    if f"mod:{addon}" in labels and f"series:{serie}" in labels:
        return 3
    if "[mig]" in title.lower() and addon in title and serie in title:
        return 2
    if head_ref.startswith(f"{serie}-mig-") and addon in head_ref:
        return 1
    return 0


def _verdict(pr: dict, ci: str, approvals: int, labels: set) -> str:
    if pr.get("state") == "closed":
        # a merged migration PR means the addon IS migrated upstream —
        # valuable info, but not a candidate to build upon
        return "merged" if pr.get("merged_at") else "stale"
    approved = "approved" in labels or approvals >= 2
    if ci == "red":
        return "partial"
    if ci in ("green", "coverage-only"):
        return "credible" if approved else "promising"
    return "partial"  # pending / unknown CI


def find_migration_prs(
    oca_slug: str, addon: str, target_serie: int, enrich: bool = True
) -> Optional[List[dict]]:
    """Candidate migration PRs of ``addon`` in ``OCA/<repo>`` for
    ``<target_serie>.0``, best first. Returns None when the GitHub API
    is not usable (no token / rate limited) — the caller reports the
    discovery as 'unavailable' instead of failing."""
    if not _github_token():
        print("WARNING! No GitHub token found: OCA PR discovery unavailable.")
        return None

    queries = [
        # label convention (l10n-brazil & repos using mod:/series: labels)
        f"repo:{oca_slug}+is:pr+label:%22mod:{addon}%22"
        f"+label:%22series:{target_serie}.0%22",
        # title convention
        f"repo:{oca_slug}+is:pr+%22[MIG]%22+{addon}+in:title",
        # branch convention (search matches the head branch name too)
        f"repo:{oca_slug}+is:pr+%22{target_serie}.0-mig-{addon}%22",
    ]
    seen: Dict[int, dict] = {}
    for query in queries:
        for item in _search_prs(oca_slug, query):
            seen[item["number"]] = item

    if not seen:
        return []

    candidates: List[dict] = []
    for number, item in seen.items():
        # the search payload lacks labels details on some items: fetch
        # the full PR when we enrich
        full = item
        if enrich:
            fetched = _api_get(f"{GITHUB_API}/repos/{oca_slug}/pulls/{number}")
            if fetched and isinstance(fetched, dict):
                full = fetched
        labels = {l.get("name", "") for l in full.get("labels", [])}
        strength = _matches_addon(full, addon, target_serie)
        if strength == 0:
            continue
        head_sha = ((full.get("head") or {}).get("sha")) or ""
        ci = _check_runs_status(oca_slug, head_sha) if enrich and head_sha else "unknown"
        approvals = _approval_count(oca_slug, number) if enrich else 0
        verdict = _verdict(full, ci, approvals, labels)
        candidates.append(
            {
                "number": number,
                "title": full.get("title") or "",
                "url": full.get("html_url")
                or f"https://github.com/{oca_slug}/pull/{number}",
                "state": full.get("state") or "",
                "merged": bool(full.get("merged_at")),
                "head_ref": ((full.get("head") or {}).get("ref")) or "",
                "head_sha": head_sha[:10],
                "match": strength,
                "ci": ci,
                "approvals": approvals,
                "verdict": verdict,
            }
        )

    candidates.sort(
        key=lambda c: (VERDICT_RANK.get(c["verdict"], 9), -c["match"], c["number"])
    )
    return candidates
