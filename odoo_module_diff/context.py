"""Aggregate per-addon analysis files into a single LLM-friendly context.

This is the "Point A" of REFACTOR_PLAN.md: AI agents migrating a module need
one concatenated migration context (structural pseudo patches, method
signature deltas and the dependency tree) instead of browsing many files.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List

CONTEXT_FILENAME = "migration_context.md"
METHODS_FILENAME = "method_signatures.patch"
DEPS_FILENAME = "dependencies.txt"
_SEPARATOR = "=" * 78


def discover_addons(analysis_dir: str) -> List[str]:
    """Return the addon subdirs of analysis_dir that contain patch files."""
    addons = []
    for entry in sorted(Path(analysis_dir).iterdir()):
        if entry.is_dir() and any(entry.glob("*.patch")):
            addons.append(entry.name)
    return addons


def _rank(filepath: Path) -> int:
    """method signatures first, structural patches then, noise last."""
    if filepath.name == METHODS_FILENAME:
        return 0
    if filepath.name.startswith("__"):
        return 2
    return 1


def _addon_files(addon_dir: Path) -> List[Path]:
    files = sorted(addon_dir.glob("*.patch"), key=lambda p: (_rank(p), p.name))
    deps = addon_dir / DEPS_FILENAME
    if deps.exists():
        files.append(deps)
    return files


def build_context(
    addons: List[str],
    addons_dirs: Dict[str, str],
    from_label: str,
    to_label: str,
    max_bytes: int = 0,
    header: bool = True,
) -> str:
    """Build the aggregated markdown context for the given addons."""
    lines = []
    if header:
        lines += [
            "# ODOO MODULE MIGRATION CONTEXT",
            "",
            f"- Migration span: {from_label} -> {to_label}",
            f"- Generated: {datetime.now().isoformat(timespec='seconds')}",
            "- Source: odoo-module-diff pseudo patches (structural commits,"
            " method signature deltas, dependency trees)",
            "",
            "## Contents",
            "",
        ]

    total_bytes = 0
    for addon in addons:
        addon_dir = Path(addons_dirs[addon])
        files = _addon_files(addon_dir)
        if not files:
            lines.append(f"- **{addon}**: NO ANALYSIS FOUND (missing or not scanned)")
            continue
        addon_bytes = sum(f.stat().st_size for f in files)
        total_bytes += addon_bytes
        has_methods = (addon_dir / METHODS_FILENAME).exists()
        lines.append(
            f"- **{addon}**: {len(files)} files, {addon_bytes} bytes"
            f" (method_signatures: {'yes' if has_methods else 'NO'})"
        )
    lines += [
        "",
        f"Total: ~{total_bytes // 1024} KB for {len(addons)} addon(s).",
        "",
    ]

    used = 0
    skipped: List[str] = []
    for addon in addons:
        addon_dir = Path(addons_dirs[addon])
        files = _addon_files(addon_dir)
        if not files:
            lines += [
                _SEPARATOR,
                f"## ADDON: {addon} ({from_label} -> {to_label})",
                _SEPARATOR,
                "",
                "[MISSING: no analysis files found for this addon. Run"
                " odoo-module-diff on it first or check the addon name.]",
                "",
            ]
            continue
        lines += [
            _SEPARATOR,
            f"## ADDON: {addon} ({from_label} -> {to_label})",
            _SEPARATOR,
            "",
        ]
        for filepath in files:
            size = filepath.stat().st_size
            if max_bytes and used + size > max_bytes:
                skipped.append(f"{addon}/{filepath.name} ({size} bytes)")
                continue
            content = filepath.read_text(errors="ignore")
            used += size
            lines += [
                "-" * 78,
                f"### {addon}/{filepath.name} ({size} bytes)",
                "-" * 78,
                "",
                content,
                "",
            ]

    if skipped:
        lines += ["## Skipped files (--max-bytes budget reached)", ""]
        lines += [f"- {item}" for item in skipped]
        lines.append("")
    return "\n".join(lines)


def dump_context(
    analysis_dir: str,
    addon: str = "",
    addons: List[str] | None = None,
    addons_dirs: Dict[str, str] | None = None,
    from_label: str = "",
    to_label: str = "",
    output_file: str = "",
    stdout: bool = False,
    max_bytes: int = 0,
):
    """Entry point used by the CLI: aggregate and write or print the context.
    `addons` overrides `addon` with an explicit ordered list (e.g. the
    dependency chain, dependencies first). `addons_dirs` optionally maps
    addon names to the dir holding their analysis files (e.g. external
    addons scanned in their own repo), defaulting to analysis_dir/<addon>."""
    if addons is None:
        addons = [addon] if addon else discover_addons(analysis_dir)
    if not addons:
        print(f"Error! No addon analysis found in {analysis_dir}")
        raise SystemExit(1)

    if addons_dirs is None:
        addons_dirs = {}
    addons_dirs = {
        **{a: str(Path(analysis_dir) / a) for a in addons},
        **addons_dirs,
    }
    context = build_context(
        addons, addons_dirs, from_label, to_label, max_bytes=max_bytes
    )

    if stdout:
        print(context)
        return
    if not output_file:
        output_file = os.path.join(analysis_dir, CONTEXT_FILENAME)
    with open(output_file, "w") as f:
        f.write(context)
    print(f"Migration context written to {output_file} ({len(context)} bytes)")
