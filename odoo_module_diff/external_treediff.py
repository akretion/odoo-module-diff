"""Before/after tree diff of external (OCA / custom) addons.

Unlike core addons — where the analysis scans the Odoo-SA commits of
the serie — external addons are diffed DIRECTLY between the tips of
the two serie branches (or between a directory on disk and the target
serie branch tip). This sidesteps the two flaws of commit scanning on
OCA repos:

- OCA branches are history-rewritten on merge (squash / bot commits):
  the commit list is not the ground truth;
- a change backported to BOTH series would show up as a "major change
  commit" of the target serie while being a no-op for the migration:
  in a tree diff it simply cancels out.

The resulting diff is then sliced: only the hunks carrying structural
information are kept — field declarations (`` = fields.``), class-level
method definitions (4-space indented ``def ``) and model anchors
(``class ...(Model)`` / ``_inherit`` changes) — so the pseudo patch
stays small enough for an LLM migration context.

Everything is read-only on the user clones: blobs are read from git
refs (origin/<serie>.0) through GitPython trees, never checked out; a
fetch of the needed refs is the only repository mutation.
"""

import difflib
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import git

from odoo_module_diff.method_diff import (
    addon_model_paths,
    diff_method_models,
    extract_methods_from_blobs,
    get_py_blobs,
    write_method_signatures_patch,
)

# addon subdirs holding the python business code worth diffing
MODEL_SUBDIRS = ("models", "wizard", "wizards")


# --- code map extraction -----------------------------------------------------


def ref_py_files(
    repo: git.Repo, rev: str, addon: str, module_prefix: str = ""
) -> Optional[Dict[str, str]]:
    """{repo-relative path: content} of the addon python files under the
    model subdirs at the given ref. None when the addon does not exist
    at that ref (e.g. not migrated yet); possibly empty when it exists
    but has no python model code (pure data/view addons)."""
    try:
        root = repo.commit(rev).tree
    except git.BadName:
        return None
    try:
        root / f"{module_prefix}{addon}/__manifest__.py"
    except KeyError:
        return None
    blobs: Dict[str, str] = {}
    for path in addon_model_paths(addon, module_prefix, include_wizards=True):
        try:
            tree = root
            for part in path.split("/"):
                tree = tree[part]
        except KeyError:
            continue
        blobs.update(get_py_blobs(tree))
    return blobs


def disk_py_files(addon_dir: Path) -> Dict[str, str]:
    """{addon-relative path: content} of the python files under the
    model subdirs of an addon directory on disk."""
    blobs: Dict[str, str] = {}
    for subdir in MODEL_SUBDIRS:
        base = addon_dir / subdir
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            try:
                blobs[str(py.relative_to(addon_dir.parent))] = py.read_text(
                    errors="ignore"
                )
            except OSError:
                pass
    return blobs


# --- hunk slicing ------------------------------------------------------------


def _is_interesting_line(line: str) -> bool:
    """A +/- diff line worth keeping a hunk for: field declarations and
    model anchors (_inherit/_inherits, model class headers). Method
    definitions are NOT kept here — they have their dedicated, more
    compact METHOD SIGNATURES DELTA section. Mirrors the structural
    scan of the core commits (scan_commit in main.py)."""
    if not line or line[0] not in "+-":
        return False
    body = line[1:]
    if " = fields." in body:
        return True
    if " _inherit =" in body or " _inherits =" in body:
        return True
    if body.startswith("class ") and "Model" in body:
        return True
    return False


def slice_unified_diff(diff_text: str, max_context: int = 2) -> str:
    """Drop the hunks of a unified diff that carry no structural line,
    and trim the context lines of the kept hunks (at most max_context
    lines before/after the interesting +/- lines) so file rewrites don't
    drag whole file bodies into the context."""
    out: List[str] = []
    header: List[str] = []
    hunk: List[str] = []
    hunk_interesting = False

    def flush_hunk():
        if not (hunk and hunk_interesting):
            return
        kept: List[str] = []
        # keep the @@ header, trim the context lines around +/- lines
        interesting_idx = [
            i
            for i, l in enumerate(hunk)
            if _is_interesting_line(l)
        ]
        keep = set()
        for i in interesting_idx:
            keep.update(range(max(1, i - max_context), min(len(hunk), i + max_context + 1)))
        last_ctx = None
        for i, line in enumerate(hunk):
            if i == 0 or i in keep or _is_interesting_line(line):
                kept.append(line)
                if line.startswith((" ", "-", "+")):
                    last_ctx = len(kept)
            elif last_ctx is not None and last_ctx >= len(kept):
                # contiguous trailing context right after kept lines
                pass
        # drop trailing pure-context runs (keep at most max_context)
        while kept:
            tail_line = kept[-1]
            if tail_line.startswith((" ",)) and len(kept) > 1:
                # count consecutive trailing context lines
                n = 0
                while n < len(kept) and kept[len(kept) - 1 - n].startswith(" "):
                    n += 1
                if n > max_context:
                    kept.pop()
                else:
                    break
            else:
                break
        out.extend(kept)

    for line in diff_text.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            flush_hunk()
            hunk = []
            hunk_interesting = False
            header.append(line)
        elif line.startswith("@@ "):
            flush_hunk()
            hunk = [line]
            hunk_interesting = False
        else:
            if hunk:
                hunk.append(line)
                if _is_interesting_line(line):
                    hunk_interesting = True
            else:
                # file-level lines outside any hunk (rare)
                header.append(line)
    flush_hunk()
    if not out:
        return ""
    return "\n".join(header + out) + "\n"


def build_tree_diff(
    addon: str,
    before_files: Dict[str, str],
    after_files: Dict[str, str],
    from_label: str,
    to_label: str,
) -> str:
    """Sliced unified diff of the two code maps (paths must use the same
    base, e.g. both repo-relative or both addon-parent-relative)."""
    patches: List[str] = []
    for path in sorted(set(before_files) | set(after_files)):
        before = before_files.get(path, "")
        after = after_files.get(path, "")
        if before == after:
            continue
        diff = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
        sliced = slice_unified_diff("\n".join(diff))
        if sliced:
            patches.append(sliced)
    if not patches:
        return ""
    header = (
        f"# Tree diff: {addon} ({from_label} -> {to_label})\n"
        "# Sliced: only the hunks with field declarations (_inherit /\n"
        "# _inherits / model class anchors included) are kept. Method\n"
        "# changes are in the method signatures delta instead.\n\n"
    )
    return header + "\n".join(patches)


def mig_commits_code_diff(
    repo: git.Repo,
    addon: str,
    rev_range: str,
    slice_lines: bool = True,
) -> str:
    """The models/wizards diff of the [MIG] commits in the rev range,
    sliced like the tree diff (field lines only). This is the ported
    migration work itself — the part of the serie branch history an
    agent adapting custom code cares about most."""
    log = repo.git.log(
        "--format=%H%x00%s", rev_range, "--", f"{addon}/"
    )
    mig_shas = [
        line.split("\x00", 1)[0]
        for line in log.splitlines()
        if line.strip() and "[mig]" in line.split("\x00", 1)[1].lower()
    ]
    if not mig_shas:
        return ""
    chunks: List[str] = []
    for sha in mig_shas:
        commit = repo.commit(sha)
        if not commit.parents:
            continue
        diffs = commit.parents[0].diff(
            commit,
            paths=[f"{addon}/models/", f"{addon}/wizard/", f"{addon}/wizards/"],
            create_patch=True,
        )
        text = ""
        for d in diffs:
            raw = d.diff or b""
            if isinstance(raw, str):
                raw = raw.encode()
            text += (
                f"--- a/{d.a_path}\n+++ b/{d.b_path}\n"
                + raw.decode("utf-8", errors="ignore")
            )
        if not text:
            continue
        if slice_lines:
            text = slice_unified_diff(text)
            if not text:
                continue
        chunks.append(f"### [MIG] commit {sha[:10]} {commit.summary}\n\n```diff\n{text}```\n")
    return "\n".join(chunks)


# --- scan entry point --------------------------------------------------------


def _fetch_ref(repo: git.Repo, ref: str) -> bool:
    """Make a ref available locally: already-resolvable refs (local
    branches, SHAs, tags) pass through; a branch name is fetched from
    origin when ``origin/<branch>`` is missing. Returns True when the
    ref can be resolved afterwards."""
    for candidate in (ref, f"origin/{ref}"):
        try:
            repo.commit(candidate)
            return True
        except git.BadName:
            continue
    try:
        repo.git.fetch("origin", ref)
    except git.GitCommandError as err:
        print(f"WARNING! Could not fetch {ref}: {err}")
    for candidate in (f"origin/{ref}", ref):
        try:
            repo.commit(candidate)
            return True
        except git.BadName:
            continue
    return False


def _resolve_rev(repo: git.Repo, ref: str) -> str:
    """The locally resolvable rev for a branch name / ref: prefer
    origin/<branch>, then the ref as given."""
    for candidate in (f"origin/{ref}", ref):
        try:
            repo.commit(candidate)
            return candidate
        except git.BadName:
            continue
    return ref


def scan_external_treediff(
    addon: str,
    from_serie: int,
    to_serie: int,
    output_dir: str,
    repo_path: str = "",
    origin_addons_path: str = "",
    dump_methods: bool = True,
    from_ref: str = "",
    to_ref: str = "",
) -> Optional[dict]:
    """Tree-diff an external addon between two code states.

    The BEFORE code comes from ``from_ref`` (default
    ``origin/<from_serie>.0``) of the addon repo (fetched if needed),
    or from ``origin_addons_path/<addon>`` on disk when given
    (repo-moved or pinned-version cases). The AFTER code comes from
    ``to_ref`` (default ``origin/<to_serie>.0``); when the addon does
    not exist there (not migrated yet), no diff is produced and the
    result reports ``target_status: unmigrated``.

    The from/to override pairs are the extension point for the
    same-serie production-update workflow (installed A.B.C.E.F version
    vs newer version: pass two tags/SHAs, or a disk path holding the
    installed version) — the diff and method-signature machinery is
    source-agnostic and needs no change for it.

    Writes ``tree_diff.patch`` and ``method_signatures.patch`` into
    ``output_dir/<addon>/`` (the shared analysis layout, so the context
    aggregation picks them up). Returns a small status dict or None
    when the addon cannot be located at all.
    """
    from odoo_module_diff.external_addons import find_external_repo

    if not repo_path:
        found = find_external_repo(to_serie, addon)
        if not found:
            print(f"WARNING! No repo found for external addon {addon}.")
            return None
        repo_path, _kind = found
    repo = git.Repo(repo_path)

    # BEFORE code map
    from_branch = from_ref or f"{from_serie}.0"
    from_label = ""
    before_missing = False
    if origin_addons_path:
        before_dir = Path(origin_addons_path).expanduser() / addon
        if not (before_dir / "__manifest__.py").is_file():
            print(
                f"WARNING! {before_dir} has no __manifest__.py:"
                " --origin-addons-path ignored for this addon."
            )
            before_files: Dict[str, str] = {}
        else:
            before_files = disk_py_files(before_dir)
            from_label = str(before_dir)
    else:
        before_files = {}
    if not before_files:
        if not _fetch_ref(repo, from_branch):
            print(
                f"WARNING! Ref {from_branch} not available in"
                f" {repo_path}: no BEFORE code for {addon}."
            )
            return None
        rev = _resolve_rev(repo, from_branch)
        ref_files = ref_py_files(repo, rev, addon)
        before_missing = False
        if ref_files is None:
            # absent at the origin ref: either a genuinely new addon in
            # the target serie, or (worse) an addon never migrated to
            # the target serie — decided once the AFTER map is known
            before_missing = True
            ref_files = {}
        before_files = ref_files
        from_label = rev

    # AFTER code map
    to_branch = to_ref or f"{to_serie}.0"
    if not _fetch_ref(repo, to_branch):
        print(
            f"WARNING! Ref {to_branch} not available in {repo_path}:"
            f" no AFTER code for {addon}."
        )
        return None
    to_rev = _resolve_rev(repo, to_branch)
    to_label = to_rev
    after_files = ref_py_files(repo, to_rev, addon)
    if after_files is None:
        if before_missing:
            print(
                f"NOTE! {addon} exists in neither {from_label} nor"
                f" {to_label}: it is NOT migrated to the {to_serie}.0"
                " serie yet (no diff to produce)."
            )
        else:
            # absent from the target ref while present in the origin
            # one: with the OCA conventions this means NOT MIGRATED YET
            # (a module leaving the repo gets its manifest dropped, but
            # that is rare and cannot be told apart from the branch
            # content alone) — this is exactly the --plan use case
            print(
                f"NOTE! {addon} is absent from {to_label}: not migrated"
                f" to the {to_serie}.0 serie yet (nothing to diff)."
            )
        return {
            "addon": addon,
            "repo_path": repo_path,
            "from_label": from_label,
            "to_label": to_label,
            "target_status": "unmigrated",
            "output_dir": "",
        }
    if before_missing:
        print(
            f"NOTE! {addon} not found at {from_label} in {repo_path}:"
            " new addon in the target serie (full diff)."
        )

    target_out = str(Path(output_dir) / addon)
    os.makedirs(target_out, exist_ok=True)
    patch = build_tree_diff(addon, before_files, after_files, from_label, to_label)
    if patch:
        tree_patch = Path(target_out) / "tree_diff.patch"
        tree_patch.write_text(patch)
        print(f"Generated {tree_patch} ({len(patch)} chars)")
    else:
        print(f"No structural tree diff for {addon} ({from_label} -> {to_label}).")

    if dump_methods:
        methods_start = extract_methods_from_blobs(before_files)
        methods_end = extract_methods_from_blobs(after_files)
        diff_data, added, removed, changed = diff_method_models(
            methods_start, methods_end
        )
        if diff_data:
            write_method_signatures_patch(
                addon,
                diff_data,
                (added, removed, changed),
                target_out,
                skipped_annotation_note=True,
            )
            print(
                f"Generated {target_out}/method_signatures.patch"
                f" ({changed} changed, {added} added, {removed} removed)"
            )

    return {
        "addon": addon,
        "repo_path": repo_path,
        "from_label": from_label,
        "to_label": to_label,
        "target_status": "migrated",
        "output_dir": target_out,
    }
