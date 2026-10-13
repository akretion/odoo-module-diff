"""Migration planning mode (--plan): the machine-readable contract
between odoo-module-diff and the migration agent (odoo-migration skill).

For a target module and a from->to serie step, emit a JSON plan:

- the dependency closure, resolved from the ORIGIN serie addons path
  (works even when the target module is not migrated yet),
  topologically sorted (migration order);
- per dependency: its classification (core-ce / core-ee / oca /
  private), its repo, its migration status in the target serie, the
  diff directory (when a diff could be produced), and for unmigrated
  OCA addons the candidate migration PRs (credible / promising /
  partial / stale / merged).

The agent orchestrating the migration consumes this JSON to schedule
the recursive sub-migrations (dependencies first); PR creation and
branch management stay the agent's job.
"""

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

import git

from odoo_module_diff.classify import (
    KIND_CORE_CE,
    KIND_CORE_EE,
    KIND_CORE_REMOVED,
    KIND_OCA,
    classify_addon,
)
from odoo_module_diff.dependencies import resolve_dependencies
from odoo_module_diff.external_locate import (
    build_addons_paths,
    find_addon_in_any_env,
)
from odoo_module_diff.external_treediff import scan_external_treediff
from odoo_module_diff.oca_prs import find_migration_prs


def _locate_in_paths(
    addon: str, addons_paths: List[Path]
) -> "tuple[Optional[Path], Optional[Path]]":
    """(addon_path, repo_path) of an addon in the given addons paths,
    skipping the core odoo paths (core addons are classified without a
    filesystem lookup)."""
    from odoo_module_diff.external_locate import _git_root, _is_core_path

    for base in addons_paths:
        if _is_core_path(base):
            continue
        manifest = base / addon / "__manifest__.py"
        if manifest.is_file():
            addon_path = manifest.parent.resolve()
            return addon_path, _git_root(addon_path)
    return None, None


def resolve_closure(
    addon: str,
    from_serie: int,
    addons_paths: List[Path],
) -> "tuple[List[str], List[Path], str]":
    """The transitive dependency closure of an addon, tolerant to the
    target module not being migrated yet: when the addon is not in the
    given (origin serie) addons paths, its clone from another serie env
    on disk is added to the addons path so manifestoo can still walk the
    manifests. Returns (closure, addons_paths_used, source_note)."""
    _addon_path, _repo = _locate_in_paths(addon, addons_paths)
    extra: List[Path] = []
    note = ""
    if _addon_path is None:
        other_path, other_repo = find_addon_in_any_env(
            addon, near_serie=from_serie
        )
        if other_path is not None and other_repo is not None:
            extra = [Path(other_repo)]
            note = (
                f"{addon} is not in the {from_serie}.0 addons paths: its"
                f" manifests were read from another serie env clone"
                f" ({other_repo}) as a proxy."
            )
            print(f"NOTE! {note}")
    paths_used = list(addons_paths) + extra
    closure = resolve_dependencies(
        from_serie, [str(p) for p in paths_used], addon
    )
    return closure, paths_used, note


def _plan_dependency(
    dep: str,
    from_serie: int,
    to_serie: int,
    addons_paths: List[Path],
    analysis_dir: Path,
    origin_addons_path: str = "",
    check_prs: bool = True,
) -> dict:
    """Plan entry of one dependency (or of the target module itself)."""
    entry: Dict = {"addon": dep}
    addon_path, repo_path = _locate_in_paths(dep, addons_paths)
    if addon_path is None:
        # not in the origin serie addons paths (typically the target
        # module itself, not migrated yet): fall back to any serie env
        # clone on disk to still resolve its repo and classification
        other_path, other_repo = find_addon_in_any_env(dep)
        if other_path is not None:
            addon_path = other_path
            repo_path = other_repo

    info = classify_addon(
        dep,
        to_serie,
        addon_path=addon_path,
        repo_path=str(repo_path) if repo_path else "",
        from_serie=from_serie,
    )
    entry.update(
        {
            "kind": info["kind"],
            "repo": str(repo_path) if repo_path else "",
            "github_repo": info["github_repo"],
        }
    )

    if info["kind"] == KIND_CORE_REMOVED:
        # was core in an earlier serie, gone from the target one
        upto = info.get("core_upto_serie") or from_serie
        entry["target_status"] = "removed-in-target"
        entry["diff_dir"] = ""
        entry["hint"] = (
            f"{dep} is a core addon up to the {upto}.0 serie but"
            f" no longer exists as a core addon in {to_serie}.0: the"
            " manifest dependency must be dropped or replaced (renamed"
            " or merged core addon — check the release notes)."
        )
        return entry

    if info["kind"] in (KIND_CORE_CE, KIND_CORE_EE):
        # core addons are always migrated; the diff comes from the
        # shared analysis cache when present
        cache_dir = analysis_dir / f"{to_serie}.0" / dep
        entry["target_status"] = "migrated"
        entry["diff_dir"] = (
            str(cache_dir)
            if cache_dir.is_dir() and any(cache_dir.glob("*.patch"))
            else ""
        )
        return entry

    if not repo_path:
        entry["target_status"] = "not-found"
        entry["diff_dir"] = ""
        return entry

    # external addon: tree diff between the two serie tips (or from the
    # origin addons path for the BEFORE code)
    result = scan_external_treediff(
        dep,
        from_serie,
        to_serie,
        str(analysis_dir / f"{to_serie}.0"),
        repo_path=str(repo_path),
        origin_addons_path=origin_addons_path,
    )
    if result is None:
        entry["target_status"] = "error"
        entry["diff_dir"] = ""
        return entry

    entry["target_status"] = result["target_status"]
    entry["diff_dir"] = result["output_dir"]

    if (
        entry["target_status"] == "unmigrated"
        and info["kind"] == KIND_OCA
        and info["oca_repo"]
        and check_prs
    ):
        prs = find_migration_prs(info["oca_repo"], dep, to_serie)
        entry["oca_prs"] = prs if prs is not None else "unavailable"
    return entry


def build_plan(
    addon: str,
    from_serie: int,
    to_serie: int,
    odoo_cfg: str = "",
    addons_path_str: str = "",
    origin_addons_path: str = "",
    analysis_root: str = "",
    check_prs: bool = True,
) -> dict:
    """The migration plan of one serie step (from_serie -> to_serie)."""
    from odoo_module_diff.main import analysis_home  # late import

    addons_paths = build_addons_paths(odoo_cfg, addons_path_str)
    if not addons_paths:
        raise SystemExit(
            "Error! No addons paths resolved: pass --odoo-cfg or"
            " --addons-path (the ORIGIN serie environment)."
        )

    closure, addons_paths, closure_note = resolve_closure(
        addon, from_serie, addons_paths
    )

    analysis_dir = Path(analysis_root or analysis_home())
    entries = [
        _plan_dependency(
            dep,
            from_serie,
            to_serie,
            addons_paths,
            analysis_dir,
            origin_addons_path=origin_addons_path,
            check_prs=check_prs,
        )
        for dep in closure
    ]

    to_migrate = [
        e["addon"] for e in entries if e["target_status"] == "unmigrated"
    ]
    plan: Dict = {
        "addon": addon,
        "from_serie": f"{from_serie}.0",
        "to_serie": f"{to_serie}.0",
        "migration_order": [e["addon"] for e in entries],
        "to_migrate": to_migrate,
        "dependencies": entries,
    }
    if closure_note:
        plan["closure_source_note"] = closure_note
    return plan


def dump_plan(
    addon: str,
    from_serie: int,
    to_serie: int,
    odoo_cfg: str = "",
    addons_path_str: str = "",
    origin_addons_path: str = "",
    analysis_root: str = "",
    output_file: str = "",
    stdout: bool = False,
    check_prs: bool = True,
) -> Optional[str]:
    """CLI entry point of --plan: build the plan and dump it as JSON.

    Returns the JSON payload when emitted on stdout (the caller prints
    it, so the payload stays out of the progress logs), None when it was
    written to a file."""
    plan = build_plan(
        addon,
        from_serie,
        to_serie,
        odoo_cfg=odoo_cfg,
        addons_path_str=addons_path_str,
        origin_addons_path=origin_addons_path,
        analysis_root=analysis_root,
        check_prs=check_prs,
    )
    payload = json.dumps(plan, indent=1)
    if stdout or not output_file:
        return payload
    Path(output_file).write_text(payload + "\n")
    print(f"Migration plan written to {output_file} ({len(payload)} chars)")
    return None
