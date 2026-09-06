"""Dependency resolution for the migration context (REFACTOR_PLAN Point B).

Uses manifestoo to get the transitive dependencies of an addon, topologically
sorted so that dependencies come first (migration order).
"""

import os
import subprocess
from pathlib import Path
from typing import List

import git

# manifestoo only knows released series (last supported one):
MAX_MANIFESTOO_SERIE = 19


def _find_worktree_by_branch(repo_path: str, branch: str):
    """Return the path of the worktree that has the given branch checked
    out, or None (same parsing as main.find_worktree_by_branch, kept local
    to avoid a circular import)."""
    try:
        porcelain = git.Repo(repo_path).git.worktree("list", "--porcelain")
    except Exception:
        return None
    for block in porcelain.split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        for line in lines[1:]:
            if line.startswith("branch "):
                if line[len("branch refs/heads/"):] == branch:
                    return lines[0][len("worktree "):]
                break
    return None


def find_addons_paths(target_serie: int, fs_dir: str = "") -> List[str]:
    """Find candidate addons-path dirs: the Odoo core addons plus every
    external-src repo holding at least one addon (Akretion layout:
    $ODOO_PARENT_HOME/odoo<serie>/odoo/{src/addons,external-src})."""
    if not fs_dir:
        parent_home = Path(
            os.environ.get("ODOO_PARENT_HOME", str(Path.home() / "DEV"))
        ).expanduser()
        fs_dir = str(parent_home / f"odoo{target_serie}" / "odoo" / "src")
    env_dir = Path(fs_dir)
    if not (env_dir / "addons").is_dir():
        # shared repo case: look in the worktree of the target serie (or
        # master for an unreleased serie) instead
        serie_rev = f"{target_serie}.0"
        worktree = _find_worktree_by_branch(fs_dir or os.getcwd(), serie_rev)
        if not worktree and target_serie > MAX_MANIFESTOO_SERIE:
            worktree = _find_worktree_by_branch(fs_dir or os.getcwd(), "master")
        if worktree:
            env_dir = Path(worktree)

    core = env_dir / "addons"
    external_root = env_dir.parent / "external-src"

    paths = []
    if core.is_dir():
        # in the odoo repo, `base` and few others live in odoo/addons/
        odoo_addons = core.parent / "odoo" / "addons"
        if odoo_addons.is_dir():
            paths.append(str(odoo_addons))
        paths.append(str(core))
    if external_root.is_dir():
        for child in sorted(external_root.iterdir()):
            if child.is_dir() and any(child.glob("*/__manifest__.py")):
                paths.append(str(child))
    return paths


def resolve_dependencies(
    target_serie: int, addons_path: List[str], addon: str
) -> List[str]:
    """Return the addon dependencies (transitive), topologically sorted
    (dependencies first, the addon itself last). Fallback to [addon]."""
    manifestoo_serie = (
        f"{target_serie}.0"
        if target_serie <= MAX_MANIFESTOO_SERIE
        else f"{target_serie - 1}.0"
    )
    result = subprocess.run(
        [
            "manifestoo",
            "--addons-path",
            ",".join(addons_path),
            f"--odoo-series={manifestoo_serie}",
            "--select",
            addon,
            "list-depends",
            "--transitive",
            "--include-selected",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            "WARNING! manifestoo failed, aggregating the addon alone:"
            f" {result.stderr.strip().splitlines()[-1:]}"
        )
        return [addon]
    closure = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if addon not in closure:
        closure.append(addon)
    # now get the closure topologically sorted (dependencies first)
    result = subprocess.run(
        [
            "manifestoo",
            "--addons-path",
            ",".join(addons_path),
            f"--odoo-series={manifestoo_serie}",
            "--select",
            ",".join(closure),
            "list",
            "--sort",
            "topological",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(
            "WARNING! manifestoo topo sort failed, keeping closure order:"
            f" {result.stderr.strip().splitlines()[-1:]}"
        )
        return closure
    deps = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if addon not in deps:
        deps.append(addon)
    print(f"Dependency chain (migration order): {' -> '.join(deps)}")
    return deps
