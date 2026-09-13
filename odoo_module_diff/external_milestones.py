"""Milestone-based on-the-fly analysis of external (OCA / custom) addons.

OCA and custom addons rarely carry explicit Odoo-SA-style commit
messages: the change explanations live in the Pull Requests, and OCA
branches are history-rewritten on merge (squash or bot post-merge
commits), so the branch commits are not always the ground truth — the
PRs are.

Instead of scanning every commit touching the addon (the shared analysis
repo approach, reserved for core addons), this module detects the
MILESTONE commits of an addon between two serie branches:

- manifest version bumps: a change of the serie part (``A.B`` of
  ``A.B.C.D.E``) is a MIGRATION, a change of the minor (``C``) is a
  FEATURE batch; patch bumps carry no information;
- commits touching ``<addon>/migrations/`` — the OpenUpgrade scripts.

Every commit that touched the addon between two consecutive milestones
is attached to the newer milestone: with the OCA conventions those are
exactly the merged PR commits (squash convention: one commit = one PR;
merge convention: the PR commits carry their own messages).

The PR of a milestone commit (or of each change commit) is resolved
through the GitHub API (``commit -> pulls``): the PR title and
description become the migration context.

Everything runs on the fly from the local clone: nothing is written to
the shared analysis repo.
"""

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import git

GITHUB_API = "https://api.github.com"
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\d+)\.(\d+)")
_SKIP_PREFIXES = ("[BOT]", "[UPD]", "Update translation", "[I18N]")
_SEPARATOR = "=" * 78


# --- GitHub token resolution -------------------------------------------------


def _github_token() -> str:
    """Token for the GitHub API: $GITHUB_TOKEN, then a token file
    (~/tmp.txt, Akretion convention), then the gh CLI config
    (hosts.yml, last: its stored tokens may be expired)."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    for candidate in (Path.home() / "tmp" / "tmp.txt", Path.home() / "tmp.txt"):
        if candidate.is_file():
            match = re.search(
                r"github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+",
                candidate.read_text(errors="ignore"),
            )
            if match:
                return match.group(0)
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if hosts.is_file():
        match = re.search(r"oauth_token:\s*(\S+)", hosts.read_text(errors="ignore"))
        if match:
            return match.group(1)
    return ""


def _api_get(url: str) -> Optional[list | dict]:
    token = _github_token()
    headers = {
        "Accept": "application/vnd.github.groot-preview+json",
        "User-Agent": "odoo-module-diff",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as err:
        print(f"WARNING! GitHub API call failed ({url}): {err}")
        return None


def _github_repo_slug(repo: git.Repo) -> Optional[str]:
    """'OCA/bank-payment' style slug from the origin remote URL."""
    try:
        url = repo.remote("origin").url
    except ValueError:
        return None
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def _pr_for_commit(slug: str, sha: str) -> Optional[Dict]:
    """The merged PR of a commit (GitHub commit->pulls endpoint)."""
    data = _api_get(f"{GITHUB_API}/repos/{slug}/commits/{sha}/pulls")
    if not data:
        return None
    for pr in data:
        if pr.get("state") != "open":
            return {
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["html_url"],
                "body": pr.get("body") or "",
            }
    return None


# --- version helpers ---------------------------------------------------------


def _manifest_version(repo: git.Repo, rev: str, addon: str) -> Optional[str]:
    try:
        blob = repo.commit(rev).tree / addon / "__manifest__.py"
    except KeyError:
        return None
    content = blob.data_stream.read().decode("utf-8", errors="ignore")
    match = re.search(r"""["']version["']\s*:\s*["']([^"']+)["']""", content)
    return match.group(1) if match else None


def _version_kind(old: str, new: str) -> Optional[str]:
    """'major' when the serie (A.B) changed, 'minor' when C increased,
    else None (patch bumps and version decreases — branch recreation
    noise — carry no information)."""
    old_m = _SEMVER.search(old)
    new_m = _SEMVER.search(new)
    if not old_m or not new_m:
        return None
    old_v, new_v = old_m.groups(), new_m.groups()
    if old_v[:2] != new_v[:2]:
        return "major"
    if new_v[2] > old_v[2]:
        return "minor"
    return None


# --- milestone detection -----------------------------------------------------


def _change_commits(
    repo: git.Repo, addon: str, since_sha: str, until_sha: str
) -> List[Dict]:
    """Commits that touched the addon between two manifest commits,
    excluding the bot/translation noise. When the base commit sits on an
    unrelated history (recreated OCA branches), the range degrades to the
    whole ancestry of until_sha."""
    base = None
    try:
        bases = repo.merge_base(repo.commit(since_sha), repo.commit(until_sha))
        base = bases[0] if bases else None
    except Exception:
        base = None
    rev_range = f"{base.hexsha}..{until_sha}" if base else until_sha
    commits = []
    log = repo.git.log("--format=%H%x1f%s", rev_range, "--", addon)
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, subject = line.split("\x1f", 1)
        if subject.startswith(_SKIP_PREFIXES):
            continue
        commits.append({"sha": sha, "subject": subject})
    commits.reverse()  # chronological
    return commits


def milestone_commits(
    repo: git.Repo,
    addon: str,
    start_rev: str,
    end_rev: str,
    target_serie: int = 0,
) -> List[Dict]:
    """Milestones of <addon> between start_rev and end_rev, oldest
    first: manifest version major/minor bumps and migration-script
    commits. Each milestone carries the addon commits accumulated since
    the previous milestone (the merged PR work)."""
    # walk the manifest commits chronologically with their version
    log = repo.git.log(
        "--format=%H%x00%s", f"{start_rev}..{end_rev}",
        "--reverse", "--", f"{addon}/__manifest__.py",
    )
    version_events: List[Tuple[str, str, Optional[str]]] = []  # (sha, subject, kind)
    prev_version = _manifest_version(repo, start_rev, addon)
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, subject = line.split("\x00", 1)
        new_version = _manifest_version(repo, sha, addon)
        kind = (
            _version_class_of(prev_version, new_version)
            if prev_version and new_version
            else None
        )
        if kind:
            version_events.append((sha, subject, kind))
        if new_version:
            prev_version = new_version

    migration_shas = {
        line.split("\x00", 1)[0]
        for line in repo.git.log(
            "--format=%H%x00%s", f"{start_rev}..{end_rev}", "--reverse",
            "--", f"{addon}/migrations/",
        ).splitlines()
        if line.strip()
    }

    # merge the two milestone sources chronologically
    milestone_shas = sorted(
        {sha for sha, _, _ in version_events} | migration_shas,
        key=lambda sha: repo.commit(sha).committed_date,
    )
    kind_by_sha = {sha: kind for sha, _, kind in version_events}
    if target_serie:
        # drop the older-series history (recreated branches carry the
        # whole module history): start at the first commit whose version
        # serie is the target serie (or whose subject marks the
        # migration to it)
        serie_prefix = f"{target_serie}."
        for idx, sha in enumerate(milestone_shas):
            new_version = _manifest_version(repo, sha, addon) or ""
            commit_msg = str(repo.commit(sha).message).lower()
            marker = (
                f"migration to {target_serie}.0" in commit_msg
                or "[mig]" in commit_msg
                and serie_prefix in new_version
            )
            if marker or new_version.startswith(serie_prefix):
                milestone_shas = milestone_shas[idx:]
                break
        else:
            milestone_shas = []
    milestones: List[Dict] = []
    prev_milestone_sha = start_rev
    prev_version = _manifest_version(repo, start_rev, addon) or "?"
    for sha in milestone_shas:
        is_migration = sha in migration_shas
        kind = "major" if is_migration else kind_by_sha.get(sha, "minor")
        new_version = _manifest_version(repo, sha, addon)
        milestone = {
            "sha": sha,
            "subject": repo.commit(sha).message.splitlines()[0],
            "kind": kind,
            "migrations": is_migration,
        }
        if new_version:
            milestone["version"] = f"{prev_version} -> {new_version}"
            prev_version = new_version
        milestone["change_commits"] = _change_commits(
            repo, addon, prev_milestone_sha, sha
        )
        milestones.append(milestone)
        prev_milestone_sha = sha
    return milestones


def _version_class_of(old: str, new: str) -> Optional[str]:
    """Alias kept for readability: see _version_kind."""
    return _version_kind(old, new)


# --- context building --------------------------------------------------------


def build_milestone_context(
    addon: str,
    addon_path: Path,
    repo: git.Repo,
    start_rev: str,
    end_rev: str,
    target_serie: int = 0,
) -> str:
    """Markdown migration context of an external addon, from its
    milestone commits and their Pull Requests (on the fly)."""
    slug = _github_repo_slug(repo)
    if not slug:
        print(
            "WARNING! No github.com origin remote on"
            f" {repo.working_tree_dir or repo.git_dir}: PR resolution"
            " disabled, using the commit messages only."
        )
    milestones = milestone_commits(
        repo, addon, start_rev, end_rev, target_serie=target_serie
    )
    if not milestones:
        return (
            f"# {addon} migration context ({start_rev} -> {end_rev})\n\n"
            "No version bump or migration-script commit found between"
            f" {start_rev} and {end_rev}: the addon saw no major change"
            " in this range."
        )

    lines = [
        f"# {addon} migration context ({start_rev} -> {end_rev})",
        "",
        "- Source: on-the-fly milestone analysis of the local clone",
        f"- Addon path: {addon_path}",
        f"- Addon repository: https://github.com/{slug}" if slug else "",
        "",
        "OCA conventions: the addon minor version is bumped by the OCA",
        "bot after merging PRs, so each milestone below aggregates the",
        "PRs merged since the previous milestone. For the serie",
        "migrations (major) the PR description documents the breaking",
        "changes; custom code should be checked against them.",
        "",
    ]
    for milestone in milestones:
        sha = milestone["sha"]
        kind = milestone["kind"]
        title = "MIGRATION" if kind == "major" else "FEATURES"
        lines += [
            _SEPARATOR,
            f"## {title}: {milestone['subject']}",
            _SEPARATOR,
            "",
            f"- Milestone commit: {sha[:10]}",
        ]
        if milestone.get("version"):
            lines.append(f"- Version bump: {milestone['version']}")
        if milestone.get("migrations"):
            lines.append(
                f"- Touches {addon}/migrations/ (OpenUpgrade scripts"
                " present: check them for the authoritative rename list)"
            )
        changes = milestone["change_commits"]
        lines.append(f"- Addon commits since previous milestone: {len(changes)}")
        lines.append("")

        # PR resolution: for migrations always; for feature batches only
        # when there is a single change commit (cheap and precise)
        milestone_pr = None
        if slug and milestone.get("migrations"):
            milestone_pr = _pr_for_commit(slug, sha)
        if milestone_pr:
            lines += [
                f"### Pull Request #{milestone_pr['number']}: {milestone_pr['title']}",
                f"URL: {milestone_pr['url']}",
                "",
                "PR description:",
                "",
                milestone_pr["body"].strip() or "(empty)",
                "",
            ]
        # the milestone commit itself often IS the change (squashed MIG):
        # give its diffstat for the addon
        stat = repo.git.show(
            "--stat", "--format=", sha, "--", addon
        ).strip()
        if stat:
            lines += ["### Milestone commit diffstat", "", "```", stat, "```", ""]
        if changes:
            shown = changes[:30]
            lines += [
                f"### Change commits ({len(changes)} total,"
                f" showing {len(shown)})",
                "",
            ]
            for change in shown:
                line = f"- {change['sha'][:10]} {change['subject']}"
                if slug and (kind == "major" or len(changes) <= 5):
                    pr = _pr_for_commit(slug, change["sha"])
                    if pr and pr["number"] != (milestone_pr or {}).get("number"):
                        line += f" (PR #{pr['number']}: {pr['title']})"
                lines.append(line)
            if len(changes) > len(shown):
                lines.append(f"- ... and {len(changes) - len(shown)} more")
            lines.append("")
    return "\n".join(lines)


def dump_external_context(
    addon: str,
    odoo_cfg: str = "",
    addons_path_str: str = "",
    target_serie: int = 0,
    output_file: str = "",
    stdout: bool = False,
) -> None:
    """CLI entry point: locate the external addon, detect the serie
    branches around the target serie and dump the milestone context."""
    from odoo_module_diff.external_locate import locate_external_addon

    found = locate_external_addon(addon, odoo_cfg, addons_path_str)
    if not found:
        print(
            f"Error! External addon {addon} not found in the addons paths"
            " (pass --odoo-cfg or --addons-path)."
        )
        raise SystemExit(1)
    addon_path, repo_path = found
    repo = git.Repo(str(repo_path))

    # scan boundary: the target serie branch tip vs the previous serie
    # branch (merge base, or previous serie tip on recreated branches)
    serie = target_serie
    if not serie:
        # infer the serie from the addon version on the current branch
        version = _manifest_version(repo, "HEAD", addon) or ""
        match = _SEMVER.search(version)
        serie = int(match.group(1)) if match else 0
    if not serie:
        print(
            f"Error! Cannot infer the serie for {addon}: pass the serie"
            " positionally."
        )
        raise SystemExit(1)
    target_rev = f"{serie}.0"
    prev_rev = f"{serie - 1}.0"
    try:
        end_commit = repo.commit(target_rev)
    except git.BadName:
        print(
            f"Error! Branch {target_rev} not found in {repo_path}: fetch"
            f" it first (git -C {repo_path} fetch origin {target_rev})."
        )
        raise SystemExit(1) from None
    try:
        prev_commit = repo.commit(prev_rev)
    except git.BadName:
        prev_commit = None
    if prev_commit:
        merge_base = repo.merge_base(prev_commit, end_commit)
        # on recreated branches there is no merge base: fall back to the
        # previous serie tip; the serie filter in milestone_commits drops
        # the older-series history it carries
        start_commit = merge_base[0] if merge_base else prev_commit
    else:
        print(
            f"WARNING! Branch {prev_rev} not fetched in {repo_path}:"
            f" analysing the {target_rev} branch history (serie filter"
            " will drop the older series)."
        )
        start_commit = next(repo.iter_commits(target_rev), end_commit)
    start_rev = start_commit.hexsha
    end_rev = end_commit.hexsha

    print(
        f"Analysing {addon} in {addon_path}"
        f" ({start_rev[:10]}..{end_rev[:10]})"
    )
    context = build_milestone_context(
        addon, addon_path, repo, start_rev, end_rev, target_serie=serie
    )
    if stdout:
        print(context)
        return
    if not output_file:
        output_file = f"{addon}_migration_context_{serie}.0.md"
    Path(output_file).write_text(context)
    print(f"Migration context written to {output_file} ({len(context)} chars)")
