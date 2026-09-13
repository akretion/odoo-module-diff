"""Locate an external (OCA / custom project) addon on the filesystem.

Core Odoo addons live in the big odoo.git repository; OCA and custom
project addons live in their own git clones, referenced from an Odoo
config file (``addons_path`` option) or directly through an addons-path
list. manifestoo already knows how to parse Odoo configs and resolve
addons paths, so we reuse it as the single source of truth (same
approach as akaidoo).
"""

from pathlib import Path
from typing import List, Optional, Tuple

CORE_PATH_MARKERS = ("src/addons", "src/odoo/addons", "odoo/addons")


def build_addons_paths(
    odoo_cfg: str = "",
    addons_path_str: str = "",
) -> List[Path]:
    """Resolve the addons-path entries from an Odoo config file and/or a
    comma separated addons-path list, using manifestoo. Returns the
    existing directories in order."""
    from manifestoo.addons_path import AddonsPath

    addons_path = AddonsPath()
    if odoo_cfg:
        cfg = Path(odoo_cfg).expanduser()
        if not cfg.is_file():
            raise FileNotFoundError(f"Odoo config not found: {cfg}")
        addons_path.extend_from_odoo_cfg(cfg)
    if addons_path_str:
        addons_path.extend_from_addons_path(addons_path_str)
    return [Path(p) for p in addons_path if Path(p).is_dir()]


def _is_core_path(path: Path) -> bool:
    """The core addons of odoo.git (src/addons, src/odoo/addons or the
    bare repo layouts): those are handled by the regular git scan, not by
    the external on-the-fly mode."""
    posix = path.as_posix()
    return any(marker in posix for marker in CORE_PATH_MARKERS)


def locate_external_addon(
    addon: str,
    odoo_cfg: str = "",
    addons_path_str: str = "",
) -> Optional[Tuple[Path, Path]]:
    """Locate the local clone of an external addon: walk the resolved
    addons paths looking for <addon>/__manifest__.py, skipping the core
    Odoo addons paths. Returns (addon_path, repo_path) — repo_path being
    the git repository root containing addon_path — or None when the
    addon is not found or is a core addon."""
    for base in build_addons_paths(odoo_cfg, addons_path_str):
        manifest = base / addon / "__manifest__.py"
        if not _is_core_path(base) and manifest.is_file():
            addon_path = (manifest.parent).resolve()
            repo = _git_root(addon_path)
            if repo:
                return addon_path, repo
            print(
                f"WARNING! {addon} found in {addon_path} but it is not"
                " inside a git clone: cannot analyse its history."
            )
            return None
    return None


def _git_root(path: Path) -> Optional[Path]:
    import git

    try:
        repo = git.Repo(str(path), search_parent_directories=True)
    except git.InvalidGitRepositoryError:
        return None
    work_tree = repo.working_tree_dir
    return Path(work_tree) if work_tree else None
