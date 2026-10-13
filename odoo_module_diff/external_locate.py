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
    existing directories in order, plus the core addons dirs of the env
    inferred from the config location: newer envs (odoo19+) do not list
    the core in addons_path anymore (the editable install resolves it),
    while manifestoo needs them explicitly to walk the dependencies."""
    from manifestoo.addons_path import AddonsPath

    addons_path = AddonsPath()
    if odoo_cfg:
        cfg = Path(odoo_cfg).expanduser()
        if not cfg.is_file():
            raise FileNotFoundError(f"Odoo config not found: {cfg}")
        addons_path.extend_from_odoo_cfg(cfg)

    paths: List[Path] = []
    env_root = None
    if odoo_cfg:
        cfg_dir = Path(odoo_cfg).expanduser().resolve().parent
        for candidate in (cfg_dir / "odoo", cfg_dir):
            if (candidate / "src").is_dir() or (candidate / "lib").is_dir():
                env_root = candidate
                break
    if env_root:
        # <env>/src/addons + <env>/src/odoo/addons (Odoo git checkout)
        for core in (env_root / "src" / "odoo" / "addons", env_root / "src" / "addons"):
            if core.is_dir():
                paths.append(core)
    for p in addons_path:
        p = Path(p)
        if not p.is_dir():
            continue
        paths.append(p)
        # editable/pip install layout: <site-packages>/odoo/addons
        nested = p / "odoo" / "addons"
        if p.name == "site-packages" and nested.is_dir():
            paths.append(nested)

    # dedupe, keep order
    seen = set()
    result = []
    for p in paths:
        key = str(p)
        if key not in seen:
            seen.add(key)
            result.append(p)
    if addons_path_str:
        for raw in addons_path_str.split(","):
            p = Path(raw.strip())
            if p.is_dir() and str(p) not in seen:
                seen.add(str(p))
                result.append(p)
    return result


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


_PARENT_HOME_WARNED = False


def odoo_parent_home() -> Path:
    """The directory holding the per serie envs (odoo16/, odoo18/...).

    $ODOO_PARENT_HOME wins; the default is ~/DEV. A warning is emitted
    (once per run) when the variable is set but points nowhere, or when
    it is unset AND no odoo<serie>/ env can be found at the default
    location — in both cases the OCA clones / odoo.cfg files cannot be
    located, so the user must fix the variable."""
    global _PARENT_HOME_WARNED
    import os
    import sys

    configured = os.environ.get("ODOO_PARENT_HOME", "").strip()
    if configured:
        home = Path(configured).expanduser()
        if not home.is_dir() and not _PARENT_HOME_WARNED:
            _PARENT_HOME_WARNED = True
            print(
                f"WARNING! $ODOO_PARENT_HOME points at {home} which does"
                " not exist: the per serie Odoo envs (odoo18/, odoo19/,"
                " ...), their odoo.cfg and the OCA clones will not be"
                " found. Fix the variable (it must hold the odoo<serie>/"
                " directories, e.g. export ODOO_PARENT_HOME=~/DEV).",
                file=sys.stderr,
            )
        return home

    home = Path.home() / "DEV"
    if not _PARENT_HOME_WARNED and not any(home.glob("odoo*/odoo")):
        _PARENT_HOME_WARNED = True
        print(
            "WARNING! $ODOO_PARENT_HOME is not set and no odoo<serie>/"
            f" env was found under its default {home}: the per serie"
            " Odoo envs, their odoo.cfg and the OCA clones cannot be"
            " located. Point ODOO_PARENT_HOME at the directory holding"
            " your envs (e.g. export ODOO_PARENT_HOME=~/DEV)."
            " (Note: this is NOT the analysis cache, which is"
            " $ODOO_MODULE_DIFF_HOME.)",
            file=sys.stderr,
        )
    return home


def _env_serie(env_root: Path) -> int:
    import re

    match = re.fullmatch(r"odoo(\d+)", env_root.name)
    return int(match.group(1)) if match else 0


def iter_env_roots() -> List[Path]:
    """The serie env roots (each holding an odoo/ subdir), closest to the
    requested serie first when one is given by the caller."""
    home = odoo_parent_home()
    envs = [
        p
        for p in home.glob("odoo*")
        if p.is_dir() and (p / "odoo").is_dir()
    ]
    envs.sort(key=lambda e: (-_env_serie(e), str(e)))
    return envs


def find_addon_in_any_env(
    addon: str, near_serie: int = 0
) -> Tuple[Optional[Path], Optional[Path]]:
    """(addon_path, repo_path) of an addon found in ANY serie env on
    disk (<env>/odoo/{external-src,local-src,links}/*/<addon>).

    The fallback for a module that is not migrated to the from_serie yet:
    its clone from another env still gives the repo and (as a proxy) the
    dependency manifests of another version — far better than an empty
    closure. Envs are tried by proximity to ``near_serie``, so the
    closest clone wins."""
    envs = iter_env_roots()
    if near_serie:
        envs.sort(key=lambda e: (abs(_env_serie(e) - near_serie), -_env_serie(e)))
    for env_root in envs:
        env = env_root / "odoo"
        for root_name in ("external-src", "local-src", "links"):
            root = env / root_name
            if not root.is_dir():
                continue
            for manifest in root.glob(f"*/{addon}/__manifest__.py"):
                addon_path = manifest.parent.resolve()
                return addon_path, _git_root(addon_path)
    return None, None


def find_env_cfg_for_addon(
    addon: str, near_serie: int = 0
) -> Optional[Path]:
    """The odoo.cfg of the serie env holding the addon, so the caller
    does not have to know which env to point the tool at. The closest
    serie to ``near_serie`` wins; envs without a cfg are skipped."""
    addon_path, _repo = find_addon_in_any_env(addon, near_serie=near_serie)
    if addon_path is None:
        return None
    # walk up the located clone: <env_root>/odoo/{external-src,...}/<repo>/<addon>
    for parent in addon_path.parents:
        cfg = parent / "odoo.cfg"
        if cfg.is_file():
            return cfg
    return None
