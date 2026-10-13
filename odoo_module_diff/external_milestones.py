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


def _is_ancestor(repo: git.Repo, maybe_ancestor_sha: str, descendant_sha: str) -> bool:
    """Is the first commit an ancestor of the second? (git
    merge-base --is-ancestor wrapper, False on any error)"""
    try:
        repo.git.merge_base("--is-ancestor", maybe_ancestor_sha, descendant_sha)
        return True
    except Exception:
        return False


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
    # walk the manifest commits chronologically with their version.
    # Range correction: when the serie branches were recreated with a
    # copied history (OCA branch rewrites), start_rev..end_rev includes
    # the whole ancient history of end_rev — restrict the walk to the
    # actual ancestry difference (same merge-base trick as
    # _change_commits).
    base = None
    try:
        bases = repo.merge_base(repo.commit(start_rev), repo.commit(end_rev))
        base = bases[0] if bases else None
    except Exception:
        base = None
    walk_range = (
        f"{base.hexsha}..{end_rev}" if base else f"{start_rev}..{end_rev}"
    )
    log = repo.git.log(
        "--format=%H%x00%s", walk_range,
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
            "--format=%H%x00%s", walk_range, "--reverse",
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
    mig_filtered = False
    if target_serie:
        # drop the older-series history (recreated branches carry the
        # whole module history): start at the first commit whose version
        # serie is the target serie, or whose subject marks the
        # migration to it
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
                mig_filtered = True
                break
        if not mig_filtered:
            # no target-serie version bump found (the [MIG] commit may
            # keep the old version until the bot bumps it): fall back to
            # the first [MIG] commit whatever its version
            for idx, sha in enumerate(milestone_shas):
                if "[mig]" in str(repo.commit(sha).message).lower():
                    milestone_shas = milestone_shas[idx:]
                    mig_filtered = True
                    break
        if not mig_filtered:
            milestone_shas = []
    if mig_filtered and milestone_shas:
        # drop the milestone candidates that are ANCESTORS of the
        # slicing ([MIG]) commit: with the batch-merge OCA histories
        # they can carry the same commit date while belonging to the
        # older-series side of the slice (version bumps like
        # 16.0.x after a "Migration to 17.0" subject are the symptom)
        slice_sha = milestone_shas[0]
        milestone_shas = [
            sha
            for sha in milestone_shas
            if sha == slice_sha
            or not _is_ancestor(repo, sha, slice_sha)
        ]
    milestones: List[Dict] = []
    # when the serie filter sliced the milestones, the relevant change
    # commits are the ones AFTER the first ([MIG]) milestone commit —
    # everything before it belongs to the older series
    prev_milestone_sha = (
        milestone_shas[0] if mig_filtered and milestone_shas else start_rev
    )
    prev_version = _manifest_version(repo, start_rev, addon) or "?"
    for sha in milestone_shas:
        is_migration = sha in migration_shas
        kind = "major" if is_migration else kind_by_sha.get(sha, "minor")
        new_version = _manifest_version(repo, sha, addon)
        bot_bump = (
            repo.commit(sha).message.splitlines()[0].startswith(_SKIP_PREFIXES)
            and not is_migration
        )
        milestone = {
            "sha": sha,
            "subject": repo.commit(sha).message.splitlines()[0],
            "kind": kind,
            "migrations": is_migration,
            # a [BOT] post-merge version bump carries no addon work
            # itself (its diffstat is README/manifest only): its
            # accumulated change commits belong to the previous real
            # milestone, so the builder folds them there
            "bot_bump": bot_bump,
        }
        if new_version:
            milestone["version"] = f"{prev_version} -> {new_version}"
            prev_version = new_version
        milestone["change_commits"] = _change_commits(
            repo, addon, prev_milestone_sha, sha
        )
        # plus the commits strictly after the last milestone (post-MIG
        # work at the serie tip is the most relevant to the migration)
        milestone["tail_commits"] = []
        if bot_bump and milestones and not milestones[-1]["bot_bump"]:
            # fold into the previous real milestone: it absorbs the
            # commits up to this bot bump, and the tail below then
            # covers everything after it (bot bumps are transparent)
            milestones[-1]["change_commits"] = _change_commits(
                repo, addon, milestones[-1]["sha"], sha
            )
            milestones[-1]["version"] = (
                milestones[-1]["version"].split(" -> ")[0]
                + " -> "
                + (new_version or milestones[-1]["version"].split(" -> ")[-1])
            )
            prev_milestone_sha = sha
            continue
        milestones.append(milestone)
        prev_milestone_sha = sha
    if milestones:
        # tail of the last milestone: continue from the LAST bot bump
        # if one folded after it (or from the milestone itself), up to
        # the serie tip
        tail_start = prev_milestone_sha if mig_filtered else milestones[-1]["sha"]
        milestones[-1]["tail_commits"] = _change_commits(
            repo, addon, tail_start, end_rev
        )
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
        changes = milestone["change_commits"]
        tail = milestone.get("tail_commits") or []
        if (
            not changes
            and not tail
            and not milestone.get("migrations")
            and milestone["subject"].startswith(_SKIP_PREFIXES)
        ):
            # [BOT] version-bump anchors with nothing ported since them:
            # rendering them is pure noise (they still served as
            # accumulation anchors above)
            continue
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
            shown = changes[:50]
            if len(changes) > len(shown):
                lines += [
                    f"### Change commits (showing {len(shown)} of"
                    f" {len(changes)})",
                    "",
                ]
            else:
                lines += ["### Change commits", ""]
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
        if tail:
            shown = tail[:50]
            if len(tail) > len(shown):
                lines += [
                    f"### Post-milestone commits (serie tip work:"
                    f" showing {len(shown)} of {len(tail)})",
                    "",
                ]
            else:
                lines += ["### Post-milestone commits (serie tip work)", ""]
            for change in shown:
                lines.append(f"- {change['sha'][:10]} {change['subject']}")
            if len(tail) > len(shown):
                lines.append(f"- ... and {len(tail) - len(shown)} more")
            lines.append("")
    return "\n".join(lines)


def _disk_manifest_serie(addon_path: Path) -> int:
    """The serie (A of A.B.C.D.E) of the manifest version on disk, 0 when
    unknown: tells whether the local checkout holds the target serie."""
    try:
        content = (addon_path / "__manifest__.py").read_text(errors="ignore")
    except OSError:
        return 0
    match = re.search(r"""["']version["']\s*:\s*["'](\d+)\.\d+""", content)
    return int(match.group(1)) if match else 0


def _serie_step_context(
    addon: str,
    addon_path: Path,
    repo: git.Repo,
    from_serie: int,
    to_serie: int,
) -> str:
    """One serie step (from_serie -> to_serie) of the external addon
    migration context: milestones + PRs, the [MIG] commits models diff,
    the sliced tree diff and the method signatures delta."""
    from odoo_module_diff.external_treediff import (
        build_tree_diff,
        mig_commits_code_diff,
        ref_py_files,
    )
    from odoo_module_diff.method_diff import (
        diff_method_models,
        extract_methods_from_blobs,
        method_signatures_text,
    )

    target_rev = f"{to_serie}.0"
    prev_rev = f"{from_serie}.0"
    # resolve the serie branches as origin/<serie>.0 (fetching them when
    # needed): a bare "<serie>.0" only resolves when the clone happens to
    # have a LOCAL branch of that name (common in long-lived clones, not
    # in fresh ones)
    from odoo_module_diff.external_treediff import _fetch_ref, _resolve_rev

    if not _fetch_ref(repo, target_rev):
        print(
            f"WARNING! Branch {target_rev} not available in"
            f" {repo.working_tree_dir or repo.git_dir}: skipping the"
            f" {from_serie}.0 -> {to_serie}.0 step"
            f" (git fetch origin {target_rev} to enable it)."
        )
        return ""
    end_commit = repo.commit(_resolve_rev(repo, target_rev))
    prev_commit = None
    if _fetch_ref(repo, prev_rev):
        prev_commit = repo.commit(_resolve_rev(repo, prev_rev))
    if prev_commit:
        merge_base = repo.merge_base(prev_commit, end_commit)
        # on recreated branches there is no merge base: fall back to the
        # previous serie tip; the serie filter in milestone_commits drops
        # the older-series history it carries
        start_commit = merge_base[0] if merge_base else prev_commit
    else:
        print(
            f"WARNING! Branch {prev_rev} not fetched in the addon repo:"
            f" analysing the whole {target_rev} history (the serie filter"
            " drops the older series)."
        )
        # the ROOT commit of the target branch: walking from the tip
        # would yield an empty range
        roots = repo.git.rev_list(
            "--max-parents=0", end_commit.hexsha
        ).split()
        start_commit = (
            repo.commit(roots[-1]) if roots else end_commit
        )
    start_rev = start_commit.hexsha
    end_rev = end_commit.hexsha

    print(
        f"Analysing {addon} ({from_serie}.0 -> {to_serie}.0:"
        f" {start_rev[:10]}..{end_rev[:10]})"
    )
    context = build_milestone_context(
        addon, addon_path, repo, start_rev, end_rev, target_serie=to_serie
    )

    # No MIGRATION COMMITS DIFF section: the tree diff below already
    # contains the whole models/wizards delta between the serie tips
    # (including everything the [MIG] commits did), so a per-[MIG]
    # commit diff would just bloat the context redundantly.

    # The tree diff of the model/wizard code between the two serie tips
    # is the ground truth of the migration (commits only show intent;
    # backported changes cancel out in a tree diff).
    before_files = (
        ref_py_files(repo, f"origin/{prev_rev}", addon) if prev_commit else None
    )
    after_files = ref_py_files(repo, f"origin/{target_rev}", addon)
    after_label = f"origin/{target_rev}"
    if after_files is None and (addon_path / "__manifest__.py").is_file():
        # not in the target serie branch, but present on disk: only a
        # migration IN PROGRESS of THIS serie makes the working tree the
        # right AFTER state (otherwise the local checkout belongs to
        # another serie and diffing against it would be misleading)
        wt_serie = _disk_manifest_serie(addon_path)
        if wt_serie == to_serie:
            from odoo_module_diff.external_treediff import disk_py_files

            after_files = disk_py_files(addon_path)
            after_label = f"working tree ({addon_path})"
            context += (
                f"\n\nNOTE! {addon} does not exist at origin/{target_rev}:"
                " it is NOT migrated upstream yet; the tree diff below"
                " uses the local working tree (migration in progress).\n"
            )
        else:
            context += (
                f"\n\nNOTE! {addon} is not migrated to {to_serie}.0 yet"
                f" (the local checkout is {wt_serie}.0): no tree diff"
                " for this serie step.\n"
            )
            after_files = None
    if before_files is not None and after_files is not None:
        tree_patch = build_tree_diff(
            addon,
            before_files,
            after_files,
            f"origin/{prev_rev}",
            after_label,
        )
        if tree_patch:
            context += (
                "\n\n"
                + _SEPARATOR
                + "\n## TREE DIFF (field definitions + _inherit changes)\n"
                + _SEPARATOR
                + "\n\nGround truth between the two serie tips (backported\n"
                "changes cancel out). Only the hunks with field\n"
                "definitions, _inherit/_inherits changes or model class\n"
                "anchors are kept; method changes are in the signatures\n"
                "delta below.\n\n```diff\n"
                + tree_patch
                + "```\n"
            )
        elif any(
            before_files.get(path) != after_files.get(path)
            for path in set(before_files) | set(after_files)
        ):
            context += (
                "\n\n"
                + _SEPARATOR
                + "\n## TREE DIFF (field definitions + _inherit changes)\n"
                + _SEPARATOR
                + "\n\nNO structural diff between the two serie tips: no\n"
                "field definition, _inherit/_inherits or model class\n"
                "changed — the model layer only saw method-level changes\n"
                "(see the signatures delta below).\n"
            )
        diff_data, added, removed, changed = diff_method_models(
            extract_methods_from_blobs(before_files),
            extract_methods_from_blobs(after_files),
        )
        if diff_data:
            context += (
                "\n\n"
                + _SEPARATOR
                + "\n## METHOD SIGNATURES DELTA\n"
                + _SEPARATOR
                + "\n\n```\n"
                + method_signatures_text(
                    addon, diff_data, (added, removed, changed)
                )
                + "```\n"
            )
    return context


def dump_external_context(
    addon: str,
    odoo_cfg: str = "",
    addons_path_str: str = "",
    target_serie: int = 0,
    output_file: str = "",
    stdout: bool = False,
    from_serie: int = 0,
    with_dependencies: bool = False,
) -> Optional[str]:
    """CLI entry point: locate the external addon and dump the
    migration context — one section per serie step between from_serie
    and target_serie (default: a single step, target_serie - 1 ->
    target_serie). With --with-dependencies, one file per addon of the
    transitive dependency closure (dependencies first, core addons skipped)
    in a `<output>_deps/` directory, so no single file bloats and an agent
    can load them one by one in migration order."""
    from odoo_module_diff.classify import is_core_addon
    from odoo_module_diff.dependencies import resolve_dependencies
    from odoo_module_diff.external_locate import (
        build_addons_paths,
        locate_external_addon,
    )
    from odoo_module_diff.main import analysis_home

    analysis_dir = Path(analysis_home())

    def _dump_one(dep: str, out: str) -> None:
        core, _kind = is_core_addon(dep, serie)
        if core:
            # core addon: its diff lives in the shared analysis cache
            # (ODOO_MODULE_DIFF_HOME); mention it once instead of
            # dumping anything
            cache_dir = analysis_dir / f"{serie}.0" / dep
            if cache_dir.is_dir() and any(cache_dir.glob("*.patch")):
                print(f"NOTE! Core addon {dep}: diff context in {cache_dir}")
            else:
                print(
                    f"NOTE! Core addon {dep}: no analysis in"
                    f" {analysis_dir / f'{serie}.0'} — assuming no"
                    " significant change (run the core scan to build it)."
                )
            return
        found_dep = locate_external_addon(dep, odoo_cfg, addons_path_str)
        if not found_dep:
            print(
                f"WARNING! Dependency {dep} not found in the addons paths"
                " (nor in the Odoo core): skipped."
            )
            return
        dep_addon_path, dep_repo_path = found_dep
        dep_repo = git.Repo(str(dep_repo_path))
        steps = []
        for step_to in range(from_serie + 1, serie + 1):
            step = _serie_step_context(
                dep, dep_addon_path, dep_repo, step_to - 1, step_to
            )
            if step:
                steps.append(step)
        if not steps:
            print(f"WARNING! No serie step analysable for {dep}: skipped.")
            return
        header = (
            f"# {dep} migration context ({from_serie}.0 -> {serie}.0,"
            f" {serie - from_serie} step(s))\n"
        )
        dep_context = header + "\n\n" + (
            "\n\n" + _SEPARATOR + "\n\n"
        ).join(steps)
        Path(out).write_text(dep_context)
        print(f"Migration context written to {out} ({len(dep_context)} chars)")

    found = locate_external_addon(addon, odoo_cfg, addons_path_str)
    if not found:
        print(
            f"Error! External addon {addon} not found in the addons paths"
            " (pass --odoo-cfg or --addons-path)."
        )
        raise SystemExit(1)
    addon_path, repo_path = found
    repo = git.Repo(str(repo_path))

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
    if not from_serie:
        from_serie = serie - 1

    out_path = Path(output_file).expanduser() if output_file else None

    # dependency closure (resolved from the target serie environment,
    # where the migrated versions live): one file per addon. Core
    # addons of the closure are intentionally skipped: their diff
    # context comes from the shared analysis cache (plain core
    # --dump-context mode).
    if with_dependencies:
        addons_paths = build_addons_paths(odoo_cfg, addons_path_str)
        closure = resolve_dependencies(
            serie, [str(p) for p in addons_paths], addon
        )
        if out_path:
            out_dir = out_path.with_suffix("").parent / (
                out_path.stem + "_deps"
            )
        else:
            out_dir = Path.cwd()
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"Dependency closure (migration order): {', '.join(closure)}")
        for dep in closure:  # topological: dependencies first
            _dump_one(dep, str(out_dir / f"{dep}_migration_context.md"))
        # the target module itself, last (its dependencies' contexts are
        # already dumped above)
        if not stdout:
            _dump_one(addon, str(out_dir / f"{addon}_migration_context.md"))
        written = sorted(p.name for p in out_dir.glob("*_migration_context.md"))
        print(
            f"{len(written)} context file(s) in {out_dir} — load them in"
            f" migration order (dependencies first, {addon} last); the"
            " aggregated single file is not written in this mode."
        )
        return

    steps = []
    for step_to in range(from_serie + 1, serie + 1):
        step = _serie_step_context(
            addon, addon_path, repo, step_to - 1, step_to
        )
        if step:
            steps.append(step)
    if not steps:
        steps = [
            f"# {addon} migration context ({from_serie}.0 -> {serie}.0)\n\n"
            "No serie step could be analysed: fetch the serie branches in"
            f" the addon repo ({repo.working_tree_dir or repo.git_dir})."
        ]
    header = (
        f"# {addon} migration context ({from_serie}.0 -> {serie}.0,"
        f" {serie - from_serie} step(s))\n"
    )
    context = header + "\n\n" + ("\n\n" + _SEPARATOR + "\n\n").join(steps)
    if stdout:
        # the caller emits the payload: keeping it out of the progress
        # logs lets `--stdout > file` produce a clean document
        return context
    if not output_file:
        output_file = f"{addon}_migration_context_{serie}.0.md"
    Path(output_file).write_text(context)
    print(f"Migration context written to {output_file} ({len(context)} chars)")
    return None
