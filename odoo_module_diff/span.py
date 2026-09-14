"""Multi-serie migration span aggregation (REFACTOR_PLAN Point E).

A long span migration (e.g. 12.0 -> 18.0, the typical customer case) is
decomposed into one step per serie pair (12->13, 13->14, ... 17->18). Each
step reuses the aggregated per-step context builder over the analysis cache
(`odoo-module-diff-analysis/<serie>.0/<addon>/`), and absent serie analyses
can be scanned on demand exactly like `--with-external` does for the
target serie.
"""

from pathlib import Path
from typing import List, Optional

from odoo_module_diff.context import build_context


def serie_range(from_serie: int, to_serie: int):
    """Iterate the serie pairs of a span: 12->18 yields 13, 14, ..., 18."""
    return range(from_serie + 1, to_serie + 1)


def build_span_context(
    addon: str,
    from_serie: int,
    to_serie: int,
    analysis_root: str,
    max_bytes_per_step: int = 0,
    max_bytes: int = 0,
    header: bool = True,
    chain_addons: List[str] | None = None,
    allow_ondemand_scan: bool = True,
) -> str:
    """Build the multi-serie context: one section per serie step.
    `analysis_root` holds one `<serie>.0/` subdir per serie.
    `chain_addons` optionally extends the context to the dependency
    chain (dependencies first, target addon last)."""
    from odoo_module_diff.main import scan_serie_addon  # late import

    addons = chain_addons or [addon]
    missing_scans = []
    if allow_ondemand_scan:
        for target_serie in serie_range(from_serie, to_serie):
            analysis_dir = Path(analysis_root) / f"{target_serie}.0"
            for chain_addon in addons:
                addon_dir = analysis_dir / chain_addon
                if not addon_dir.is_dir() or not any(
                    addon_dir.glob("*.patch")
                ):
                    scanned = scan_serie_addon(
                        target_serie, chain_addon, str(analysis_dir)
                    )
                    if not scanned:
                        missing_scans.append((target_serie, chain_addon))
    else:
        # cache-only mode: missing core addon analyses are not scanned
        # (scanning the whole odoo.git serie is extremely slow); they are
        # simply reported as missing in the context
        for target_serie in serie_range(from_serie, to_serie):
            analysis_dir = Path(analysis_root) / f"{target_serie}.0"
            for chain_addon in addons:
                addon_dir = analysis_dir / chain_addon
                if not addon_dir.is_dir() or not any(
                    addon_dir.glob("*.patch")
                ):
                    missing_scans.append((target_serie, chain_addon))

    lines = []
    if header:
        lines += [
            "# ODOO MULTI-SERIE MIGRATION CONTEXT",
            "",
            f"- Migration span: {from_serie}.0 -> {to_serie}.0"
            f" ({to_serie - from_serie} step(s))",
            f"- Addon: {addon}"
            + (
                f" (with dependencies: {', '.join(addons[:-1])})"
                if len(addons) > 1
                else ""
            ),
            "",
            "> **NOTICE:** this is an extraction of the main breaking",
            "> changes pseudo commits done between the 2 Odoo series. But",
            "> these commits only show the intent of each change. They",
            "> should be used to detect possible migration issues. Once",
            "> migration issues are detected, the real Odoo code base",
            "> should be used as the real code reference to ensure the",
            "> correctness of the changes: such a migration pseudo commit",
            "> might be followed by many small fix commits that change the",
            "> final diff between the Odoo series.",
            "",
            "## Contents",
            "",
        ]
    for target_serie in serie_range(from_serie, to_serie):
        analysis_dir = Path(analysis_root) / f"{target_serie}.0"
        lines += [
            "=" * 78,
            f"## STEP {target_serie - 1}.0 -> {target_serie}.0",
            "=" * 78,
            "",
        ]
        addons_dirs = {}
        chain = []
        for chain_addon in addons:
            addon_dir = analysis_dir / chain_addon
            if addon_dir.is_dir() and any(addon_dir.glob("*.patch")):
                addons_dirs[chain_addon] = str(addon_dir)
                chain.append(chain_addon)
            else:
                lines += [
                    f"[MISSING: no analysis files for {chain_addon} in"
                    f" serie {target_serie}.0. Run odoo-module-diff on it"
                    " first.]",
                    "",
                ]
        if chain:
            step = build_context(
                chain,
                addons_dirs,
                from_label=f"{target_serie - 1}.0",
                to_label=f"{target_serie}.0",
                max_bytes=max_bytes_per_step,
                header=False,
            )
            lines += [step, ""]
    if missing_scans:
        lines += [
            "## Serie steps that could not be scanned on demand",
            "",
        ]
        lines += [f"- {s}.0/{a}" for s, a in missing_scans]
        lines.append("")
    if max_bytes and len("\n".join(lines).encode()) > max_bytes:
        lines += [
            "WARNING! Span context exceeds the --max-bytes budget:"
            " pass a lower --max-bytes-per-step or aggregate single steps.",
            "",
        ]
    return "\n".join(lines)


def dump_span_context(
    addon: str,
    from_serie: int,
    to_serie: int,
    analysis_root: str,
    output_file: str = "",
    stdout: bool = False,
    max_bytes_per_step: int = 0,
    max_bytes: int = 0,
    chain_addons: Optional[List[str]] = None,
    allow_ondemand_scan: bool = True,
):
    """Entry point used by the CLI: build and write or print the span."""
    context = build_span_context(
        addon,
        from_serie,
        to_serie,
        analysis_root,
        max_bytes_per_step=max_bytes_per_step,
        max_bytes=max_bytes,
        chain_addons=chain_addons,
        allow_ondemand_scan=allow_ondemand_scan,
    )
    if stdout:
        print(context)
        return
    if not output_file:
        output_file = str(
            Path(analysis_root)
            / f"migration_context_{addon}_{from_serie}.0_to_{to_serie}.0.md"
        )
    with open(output_file, "w") as f:
        f.write(context)
    print(f"Migration context written to {output_file} ({len(context)} bytes)")
