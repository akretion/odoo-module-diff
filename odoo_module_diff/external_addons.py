"""External (OCA / custom) addon support (REFACTOR_PLAN Point D).

Core Odoo addons live in one big repository (odoo.git) where the analysis
boundaries come from the [REL] x.0 release commits or from the merge base
with the previous serie. External addons (OCA repos, customer modules) live
in their own git repositories (e.g. external-src/l10n-brazil), where each
 serie has a clean version branch (18.0, 19.0, ...) that forks from the
previous serie branch: the merge base of the two version branches is the
natural scan boundary and there is no [REL] commit to look for.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import git

# manifestoo only knows released series (last supported one):
MAX_MANIFESTOO_SERIE = 19


def find_external_repo(
    target_serie: int,
    addon: str,
    fs_dir: str = "",
) -> Optional[Tuple[str, str]]:
    """Find the git repo holding the given external addon for the serie.
    Returns (repo_path, repo_kind) with repo_kind in {"env", "given"},
    or None if not found. Mirrors dependencies.find_addons_paths layout
    detection ($ODOO_PARENT_HOME/odoo<serie>/odoo/external-src/*)."""
    candidates: List[Path] = []
    if fs_dir and (Path(fs_dir) / "addons").is_dir():
        # an odoo src worktree was given: its sibling external-src
        candidates.append(Path(fs_dir).parent / "external-src")
    else:
        repo_dir = Path(fs_dir) if fs_dir else Path.cwd()
        if (repo_dir / ".git").exists() or (repo_dir / "git-dir").exists():
            # the given dir is a git repo itself but not an odoo src dir:
            # fall through to the env lookup below
            pass
        parent_home = Path(
            os.environ.get("ODOO_PARENT_HOME", str(Path.home() / "DEV"))
        ).expanduser()
        env_src = parent_home / f"odoo{target_serie}" / "odoo" / "src"
        if (env_src / "addons").is_dir():
            candidates.append(env_src.parent / "external-src")

    for external_root in candidates:
        if not external_root.is_dir():
            continue
        for child in sorted(external_root.iterdir()):
            manifest = child / addon / "__manifest__.py"
            if child.is_dir() and manifest.exists():
                return str(child), "env"
    return None


def resolve_scan_boundary(
    repo: git.Repo, target_serie: int
) -> Tuple[Optional[git.Commit], Optional[git.Commit]]:
    """Scan boundary for an external repo: the merge base of the previous
    serie version branch with the target serie version branch (start),
    and the tip of the target serie branch (end). Returns (None, None) if
    the refs are missing."""
    prev_rev = f"{target_serie - 1}.0"
    target_rev = f"{target_serie}.0"
    try:
        prev_commit = repo.commit(prev_rev)
        target_commit = repo.commit(target_rev)
    except git.BadName:
        where = repo.working_tree_dir or repo.git_dir
        print(
            f"WARNING! Branches {prev_rev} / {target_rev} not fully fetched"
            f" in {where}. Fetch them first, e.g.:"
        )
        print(
            f"  git -C {where} fetch origin {prev_rev} {target_rev}"
        )
        return None, None
    merge_base = repo.merge_base(prev_commit, target_commit)
    if merge_base:
        return merge_base[0], target_commit
    # recreated target branch (unrelated history, e.g. OCA branch
    # recreation): compare the previous serie tip with the target serie tip.
    # The method signature delta stays exact (tree comparison) while the
    # commit scan honestly covers the whole recreated branch (including the
    # squashed port commits, which is what the migration agent wants).
    print(
        f"WARNING! No merge base between {prev_rev} and {target_rev}:"
        " the target serie branch was likely recreated with unrelated"
        " history (e.g. OCA branch recreation)."
    )
    print(
        f"Falling back to the {prev_rev} tip as start commit:"
        f" {prev_commit.hexsha[:10]}..{target_commit.hexsha[:10]}"
    )
    return prev_commit, target_commit


def scan_external_addon(
    addon: str,
    target_serie: int,
    output_dir: str,
    keep_noise: bool = False,
    dump_methods: bool = True,
    fs_dir: str = "",
    addons_dirs: Optional[Dict[str, str]] = None,
):
    """Scan one external addon and write its pseudo patches into
    output_dir/<addon>/. Returns the (repo_path, start, end) triple or
    None. When `addons_dirs` is given (context aggregation mode), record
    the output dir there instead of the plain output dir."""
    from odoo_module_diff.main import scan_addon_commits  # late import

    found = find_external_repo(target_serie, addon, fs_dir=fs_dir)
    if not found:
        # silent: the aggregation marks missing addons itself
        return None
    repo_path, _kind = found

    # honor an existing per addon dir (e.g. an in-progress migration branch
    # in the env clone) only if it has the serie branches we need
    repo = git.Repo(repo_path)

    start_commit, end_commit = resolve_scan_boundary(repo, target_serie)
    if not start_commit or not end_commit:
        return None

    # when the addon does not exist in the target serie branch (e.g. an
    # in-progress migration branch checked out in the clone), compare with
    # the current HEAD instead: this captures the migration work in progress
    target_rev = f"{target_serie}.0"
    try:
        end_commit.tree / f"{addon}/__manifest__.py"
    except KeyError:
        head = repo.head.commit
        try:
            head_label = str(repo.active_branch)
        except TypeError:
            head_label = "detached HEAD"
        print(
            f"NOTE! {addon} is not in branch {target_rev}: using the current"
            f" HEAD ({head.hexsha[:10]}, {head_label}) as end commit instead."
        )
        end_commit = head

    target_out = str(Path(output_dir) / addon)
    if addons_dirs is not None:
        addons_dirs[addon] = target_out
    existing = []
    if Path(target_out).is_dir():
        existing = sorted(Path(target_out).glob("*.patch"))
    if existing:
        print(
            f"Keeping existing analysis of {addon} in {target_out}"
            f" ({len(existing)} patch files; delete the dir to rescan)."
        )
        return repo_path, start_commit, end_commit
    os.makedirs(target_out, exist_ok=True)
    print(
        f"Scanning external addon {addon} in {repo_path}"
        f" ({start_commit.hexsha[:10]}..{end_commit.hexsha[:10]})"
    )
    scan_addon_commits(
        repo,
        addon,
        start_commit,
        end_commit,
        target_out,
        keep_noise=keep_noise,
        dump_methods=dump_methods,
        module_prefix="",
    )
    return repo_path, start_commit, end_commit
