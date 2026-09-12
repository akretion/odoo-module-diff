import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import git
import typer
from slugify import slugify

from odoo_module_diff.context import dump_context as dump_context_impl
from odoo_module_diff.dependencies import find_addons_paths, resolve_dependencies
from odoo_module_diff.external_addons import (
    find_external_repo,
    scan_external_addon,
)
from odoo_module_diff.method_diff import generate_method_signatures_diff
from odoo_module_diff.span import dump_span_context

LINE_CHANGE_THRESHOLD = 25
LINE_CHANGE_FEAT_THRESHOLD = 140
LINE_MESSAGE_FEAT_THRESHOLD = 40
NON_TRIVIAL_FIELD_ATTRS = (
    "company_dependent=",
    "store=",
    "compute=",
    "recursive=",
    # "inverse=",
)


def _norm_code(line: str) -> str:
    """Normalize a diff line for twin comparison: strip the -/+ marker,
    unify quotes and collapse all whitespace. Lint/black reformatting
    makes twin lines look different while being semantically identical
    (e.g. _inherit = 'x' -> _inherit = "x")."""
    body = line[1:] if line[:1] in ("-", "+") else line
    return re.sub(r"\s+", "", body.replace("'", '"'))


ADDON_PREFIX_FILTER = ["l10n_", "website_", "test"]

BLACKLISTS = [
    "adapt model class names to correspond to model names",
    "Restore the model `_name`",
]

# commit SHAs blacklisted after post-scan false positive audits: skipped
# when re-scanning (pure file moves/splits or reformatting commits whose
# structural matches are all quote/whitespace twins or multiline re-wraps)
BLACKLISTED_COMMITS = [
    # 16.0 sale_quotation_builder: split files (reformat only)
    "70b9b7a7722db52061a29343c149bddd12a8dd20",
    # 16.0 sale_quotation_builder: [MOV] split template py (move only)
    "e5e4ca6554f5781a620f357eba562ed566222aeb",
    # 16.0 hr_skills: [MOV] split model files (move only)
    "16ee5235e0a5805c65c71bab2c89a4603ed690b8",
    # 16.0 product: clean pricelist item file (reformat only)
    "753ac6b30a567a3532b4c68c052058e06a93a9a3",
    # 15.0 im_livechat: [MOV] split models related to mail (move only)
    "74dc9795f5b9c1712bb171e8ca2e965acd832165",
    # 15.0 hr_fleet: split model files (reformat only)
    "a81942cf7efc6ec75b0fb1d30e243fc7a5b6f529",
    # 15.0 mail: [MOV] reorganize channel code (move only)
    "11b8735d8a68bfe44ba4c3aaedada85983e1b337",
    # 15.0 calendar_sms: [REF] prepare alarm config (imports only)
    "f87a0eb763dc7aa0abe68747d59a302f32e66e8b",
    # 15.0 calendar: [MOV] reorganize event/attendee fields (move only)
    "1e9b1da0eb813e268bac261bce748a9d78cba79b",
    # 14.0 account: [REF] split big files (move only)
    "7076b4f4d0d933d93b24e8e4c7cf21ef0b0008e5",
    # 14.0 event[_sale]: [MOV] split main models (move only)
    "abcd6c15cf27d0e5da77b2e08a742f40e11c4e84",
    # 14.0 calendar: [MOV] split models in their own file (move only)
    "779912007441d005641daadbd8336029950cc7d0",
    # 14.0 stock: [MOV] orderpoint own file (move only)
    "d81d12daaada571fdb184b059ada34221fa70d3d",
    # 14.0 base_address_extended: [MOV] reorganize python code (move only)
    "d160997c9515dd6178da83fcb1daebad0086cb86",
    # 14.0 purchase_mrp: imports-only diff (move only)
    "b0d22b5752d483da0534dad00b7f35dd11612804",
    # 14.0 mail: [MOV] move language computation (move only)
    "2835e0ba38f67f76b7f43948290050dcc68cd9db",
    # 14.0 mail: placeholder mixin fields all survive 14.0 (move only)
    "72aa04984463db0b15770cbf26fd15dd9c2ccaee",
    # 14.0 mail: [REF] reorganize template/render mixin (move only)
    "aa31e67716f19ad84145ecb35b0cda07e2143b83",
    # 14.0 sales_team: [IMP] lint module (reformat only)
    "150095ebc7ef112a03f3e6a4704e06cde6e7c5f0",
    # 13.0 hr: [REF] split hr.py into model files (move only)
    "b2b26f1ff7e4dcf7d0c0ccc36b249045372329c7",
    # 13.0 hr_holidays: split hr.py into model files (move only)
    "7362c6040cd22a0f98277de8fcc004b6ab724a48",
    # 13.0 hr_skills_slides: fix with class moves only (move only)
    "f93064606f4b6dd0fde0750991ff517ae2925fac",
    # 13.0 link_tracker: [REF] reorganize python (move only)
    "37fcb1e6ff371ffbeb81a3825c24a4ed0afbf75f",
    # 13.0 lunch: [REF] split lunch.py into smaller files (move only)
    "ad99f09a062cd190c9863d49486e60899daed2a6",
    # 13.0 mail: delete tracking values (non structural, move only)
    "8d2d41068d364f67c4da3eb68eb1f3c95e3c479c",
    # 13.0 mass_mailing: small code fixes + move (move only)
    "f5b1105c9752ba5836d24e71f3780a611ec1801f",
    # 13.0 phone_validation: fix MRO issue (move only)
    "4ba77c52f5d3287e7233eca604f2a086e3f29f7d",
    # 17.0 hr_recruitment_survey: [MOV] split survey models (move only)
    "a166aafb36b43242e82a5b0a08704684c4ff2c8b",
    # 17.0 hr_skills_survey: report fields 100% twins (move only)
    "45ac2970b00b352b86b1fbe6f8b25c15909c6d04",
    # 17.0 mail: [IMP] lint and reorder alias code (fields survive)
    "22a6170a01e5818bab57a49606bf385bc2be034d",
    # 17.0 mail: [REF] lint reorder code bits (fields survive)
    "c53aae845d91b03ac30c55e14f2e4b4b54d0a39f",
    # 17.0 purchase_stock: [MOV] separate po/po line (move only)
    "aabcdd2bc6239fca4037d10c4801739d3697f902",
    # 19.0 base: [IMP] split models res.users and res.groups (move only;
    # same split happens in other addons but only base had pure twins)
    "0804aa5566cd65b3470bc5d5d34112ef3ad71ea0",
]

def latest_patch_index(addon_output_dir: str) -> int:
    """Highest pseudo patch index (the NNN in cNNN/featNNN/__noiseNNN)
    already present in the addon analysis dir. Structural and noise
    patches share one chronological counter, so the max index over all
    files is the next free slot minus one."""
    highest = -1
    path = Path(addon_output_dir)
    if not path.is_dir():
        return highest
    for filepath in path.glob("*.patch"):
        if filepath.name == "method_signatures.patch":
            continue
        match = re.match(r"^(?:c|feat|__noise)(\d{3})", filepath.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def resolve_continue_start(
    repo: git.Repo,
    output_dir: str,
    target_rev: str,
    end_commit: git.Commit,
    scan_addons: List[str],
) -> Optional[git.Commit]:
    """Resume commit for --continue: the newest kept pseudo commit SHA
    found in the existing analysis files. Candidates are collected from
    the `From:` line of every pseudo patch of the addons to scan, then
    the winner is the candidate that is an ancestor of the serie tip and
    has the highest topology-agnostic rank (commit date first, then
    committer order in `git rev-list` as a tie breaker). Commits not
    reachable from the serie tip (e.g. a stale SHA from a force push)
    are ignored with a warning. Returns None when there is nothing to
    continue from."""
    shas: set = set()
    for addon in scan_addons:
        addon_dir = Path(output_dir) / addon
        if not addon_dir.is_dir():
            continue
        for filepath in addon_dir.glob("*.patch"):
            if filepath.name == "method_signatures.patch":
                continue
            try:
                for line in filepath.read_text(errors="ignore").splitlines():
                    if line.startswith("From: "):
                        sha = line[len("From: "):].strip()
                        if re.fullmatch(r"[0-9a-f]{40}", sha):
                            shas.add(sha)
                        break  # only the first From: line matters
            except OSError as err:
                print(f"WARNING! Cannot read {filepath}: {err}")

    if not shas:
        print(
            "WARNING! --continue found no pseudo patch with a commit SHA"
            f" in {output_dir}: nothing to continue from, full scan instead."
        )
        return None

    # Ancestry filter: only candidates that are ancestors of the serie tip
    # (the target rev may have been force pushed since the last scan).
    valid = []
    for sha in shas:
        try:
            candidate = repo.commit(sha)
        except (git.BadName, ValueError):
            print(f"WARNING! --continue: SHA {sha[:10]} not in repo, ignored.")
            continue
        if repo.is_ancestor(candidate, end_commit):
            valid.append(candidate)
        else:
            print(
                f"WARNING! --continue: SHA {sha[:10]} is not an ancestor of"
                f" {target_rev}, ignored (force pushed serie branch?)."
            )

    if not valid:
        print(
            "WARNING! --continue: none of the patch SHAs is an ancestor of"
            f" {target_rev}: nothing to continue from, full scan instead."
        )
        return None

    # rank by commit date; tie-break with the rev-list position (default
    # rev-list order is newest first, so the LOWEST position is the
    # newest commit: batch merges give several commits the same second)
    topo_order = {
        sha: pos
        for pos, sha in enumerate(
            repo.git.rev_list(target_rev).splitlines()
        )
    }

    def rank(commit: git.Commit):
        return (
            commit.committed_date,
            -topo_order.get(commit.hexsha, len(topo_order)),
        )

    start = max(valid, key=rank)
    if start is None:  # unreachable: valid is non-empty here
        return None
    print(
        f"--continue: resuming from {start.hexsha[:10]} -"
        f" {datetime.fromtimestamp(start.committed_date).strftime('%Y-%m-%d %H:%M:%S')}:"
        f" {start.message.splitlines()[0].strip()}"
    )
    return start


def count_new_commits(
    repo: git.Repo,
    addon: str,
    start_commit: git.Commit,
    end_commit: git.Commit,
    module_prefix: str = "addons/",
) -> int:
    """Number of serie commits that touched the addon models since
    start_commit (cheap path-filtered walk, no diff computation)."""
    if addon == "base" and module_prefix == "addons/":
        module_path = "odoo/addons/base/models/"
    else:
        module_path = f"{module_prefix}{addon}/models/"
    return sum(
        1
        for _ in repo.iter_commits(
            f"{start_commit.hexsha}..{end_commit.hexsha}", paths=module_path
        )
    )


CONTINUE_STATE_FILENAME = ".continue_state.json"
README_ADDON_LIMIT = 60


def read_continue_state(output_dir: str) -> Optional[dict]:
    state_path = Path(output_dir) / CONTINUE_STATE_FILENAME
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text())
    except (OSError, ValueError) as err:
        print(
            f"WARNING! Unreadable {state_path} ({err}): ignoring it."
        )
        return None


def write_continue_state(
    output_dir: str, complete: bool, end_sha: str = ""
):
    state_path = Path(output_dir) / CONTINUE_STATE_FILENAME
    try:
        os.makedirs(output_dir, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {"complete": complete, "end_sha": end_sha},
            )
        )
    except OSError as err:
        print(f"WARNING! Cannot write {state_path}: {err}")


def find_end_commit_by_serie(repo: git.Repo, target_serie: int, rev: str):
    """
    Find the most recent commit with a specific message.
    Return the more recent commit if no match is found.
    The commit search is done in the given rev (serie branch or master)
    instead of HEAD so no checkout is required (worktrees friendly).
    """
    if target_serie == 16:
        message = "[REL] 16.0 FINAL"
    if target_serie == 10:  # Odoo I hate you so much
        # message = "[REL] 10.0 \o/"
        return repo.commit("780869879b00d5772985e7c11003ac8a94451a61"), True
    elif target_serie == 9:
        message = "[REL] Odoo 9"
    elif target_serie == 8:
        message = "[REL] Odoo 8.0"
    else:
        message = f"[REL] {target_serie}.0"

    last_commit = None
    for commit in repo.iter_commits(rev):
        if last_commit is None:
            last_commit = commit
        if (
            message in str(commit.message.splitlines()[0])
            and commit.message.splitlines()[0].replace(message, "").strip() == ""
        ):
            return commit, True
    print("WARNING LAST COMMIT BEFORE RELEASE NOT FOUND!")
    print("Using last commit instead...")
    return last_commit, False


def scan_diff_line_removal(
    line: str,
    score_add: float,
    score_del: float,
    score_feat: float,
    matches: List[str],
    prev_line: str,
    prev_prev_line: str,
    reset_scanning_buffer: bool,
):
    if (
        " _inherit =" in line
        or " _inherit =" in prev_line
        and prev_line.endswith("[")
        or " _inherit =" in prev_prev_line
        and prev_prev_line.endswith("[")
    ) and "AbstractModel" not in (line + prev_line + prev_prev_line):
        reset_scanning_buffer = True
        matches.append(line)
        score_del += 1

    elif (
        " _inherits =" in line
        or " _inherits =" in prev_line
        and prev_line.endswith("[")
        or " _inherits =" in prev_prev_line
        and prev_prev_line.endswith("[")
    ) and "AbstractModel" not in (line + prev_line + prev_prev_line):
        reset_scanning_buffer = True
        matches.append(line)
        score_del += 1

    elif (
        " = fields." in line
        or " = fields." in prev_line
        and prev_line.endswith("(")
        or " = fields." in prev_prev_line
        and prev_prev_line.endswith("(")
    ) and not (
        # ensure it is not only a trivial attr change
        line.count("=") == 1
        and " = fields." not in line
        and not any(key in line for key in NON_TRIVIAL_FIELD_ATTRS)
    ):
        reset_scanning_buffer = True
        matches.append(line)
        if " = fields." not in line:
            score_del += 0.4  # wheights less because just an important attr change
        else:
            score_del += 1
        if "2many(" in line:  # relations removal weights more
            score_del += 1

    return (
        line,
        score_add,
        score_del,
        score_feat,
        matches,
        prev_line,
        prev_prev_line,
        reset_scanning_buffer,
    )


def scan_diff_line_addition(
    line: str,
    score_add: float,
    score_del: float,
    score_feat: float,
    matches: List[str],
    prev_line: str,
    prev_prev_line: str,
    reset_scanning_buffer: bool,
):
    if " _inherit =" in line or " _inherits =" in line:
        # re-added _inherit/_inherits: cancel a removal twin that only
        # differs by quotes/whitespace (mass lint reformatting such as
        # the double-quotes [LINT] commits carry no structural change)
        norm_line = _norm_code(line)
        for match in matches:
            if not match.startswith("-") or " _inherit" not in match:
                continue
            if _norm_code(match) == norm_line:
                matches.remove(match)
                score_del -= 1  # cancel the removal score of the twin
                break
        reset_scanning_buffer = True
        return (
            line,
            score_add,
            score_del,
            score_feat,
            matches,
            prev_line,
            prev_prev_line,
            reset_scanning_buffer,
        )

    if " = fields." in line:
        reset_scanning_buffer = True

        # is it only a minor attr change to a field removed before?
        removed_match = None
        for match in matches:
            if (
                not match.startswith("-")
                # or not match.endswith(")")
                or " = fields." not in match
            ):
                continue

            if (
                (
                    match[1:].split("(")[0]
                    == line[1:].split("(")[0]  # same field name and type
                )
                or (
                    match[1:].split("(")[0].replace("fields.Char", "fields.Text")
                    == line[1:].split("(")[0]  # same field name and type
                )
                or (
                    match[1:].split("(")[0].replace("fields.Char", "fields.Html")
                    == line[1:].split("(")[0]  # same field name and type
                )
                or (
                    match[1:].split("(")[0].replace("fields.Text", "fields.Html")
                    == line[1:].split("(")[0]  # same field name and type
                )
                or (
                    match[1:].split("(")[0].replace("fields.Integer", "fields.Float")
                    == line[1:].split("(")[0]  # same field name and type
                )
            ):
                removed_match = match
                break

        if removed_match:
            # new we try to detect trivial field attrs changes:
            non_trivial_prev = set()
            for key in NON_TRIVIAL_FIELD_ATTRS:
                if key in _norm_code(removed_match):
                    if key == "compute=":
                        # we don't want to track the exact compute method
                        value = "some_method"
                    else:
                        value = _norm_code(removed_match).split(key)[-1]
                        value = value.split(",")[0].split(")")[0]
                    non_trivial_prev.add(f"{key}{value}")

            non_trivial_line = set()
            for key in NON_TRIVIAL_FIELD_ATTRS:
                if key in _norm_code(line):
                    if key == "compute=":
                        # we don't want to track the exact compute method
                        value = "some_method"
                    else:
                        value = _norm_code(line).split(key)[-1]
                        value = value.split(",")[0].split(")")[0]
                    non_trivial_line.add(f"{key}{value}")

            if non_trivial_prev == non_trivial_line:
                # som unimportant attr change, let's revert the removal score
                score_del -= 1  # cancel our previous match
                if "2many(" in line:  # relations removal weights more
                    score_del -= 1

                matches.remove(removed_match)
            else:
                score_del -= 0.6  # field isn't removed but some important attr changed
                if "2many(" in line:  # revert relations removal score
                    score_del -= 1
                matches.append(line)  # we help diff visualization

        else:
            # it's really a new field addition
            score_feat += 1
            if "2many(" in line:  # adding relations weights more
                score_add += 1
                matches.append(line)
    else:
        score_add += 0.4  # weights less because only an attr additive change

    return (
        line,
        score_add,
        score_del,
        score_feat,
        matches,
        prev_line,
        prev_prev_line,
        reset_scanning_buffer,
    )


def scan_commit(path: str, commit: git.Commit):
    """
    Check if the commit diff contains the specified strings.
    We count a " = fields." match only
    if it's inside a -/+ line or in the 2 lines before.
    """
    score_del = 0
    score_add = 0
    score_feat = 0
    matches = []
    diff_items = []
    for parent in commit.parents:
        diff = parent.diff(commit, paths=path, create_patch=True)
        diff_string = ""
        for diff_item in diff:
            diff_item_string = diff_item.diff.decode("utf-8", errors="ignore")
            diff_string += f"\n--- a/{diff_item.a_path}\n+++ b/{diff_item.b_path}\n{diff_item_string}"

            # line, prev_line and prev_prev_line is a kind of 3 lines scanning buffer
            prev_line = ""
            prev_prev_line = ""
            is_transient_model = False

            for line in diff_item_string.splitlines():
                line = line.split(" #")[0].strip().replace("\t", " ")
                reset_scanning_buffer = False
                if line.startswith("@@ ") or line[1:].startswith("class "):
                    if "TransienModel" in line:
                        is_transient_model = True
                    else:
                        is_transient_model = False

                if is_transient_model:
                    continue

                if line.startswith("-    ") and not line.startswith("-        "):
                    (
                        line,
                        score_add,
                        score_del,
                        score_feat,
                        matches,
                        prev_line,
                        prev_prev_line,
                        reset_scanning_buffer,
                    ) = scan_diff_line_removal(
                        line,
                        score_add,
                        score_del,
                        score_feat,
                        matches,
                        prev_line,
                        prev_prev_line,
                        reset_scanning_buffer,
                    )

                elif (
                    line.startswith("+    ")
                    and not line.startswith("+        ")
                    and (
                        " = fields." in line
                        or " = fields." in prev_line
                        and prev_line.endswith("(")
                        or " = fields." in prev_prev_line
                        and prev_prev_line.endswith("(")
                    )
                    and not (
                        # ensure it is not only a trivial attr change
                        line.count("=") == 1
                        and " = fields." not in line
                        and not any(key in line for key in NON_TRIVIAL_FIELD_ATTRS)
                    )
                ):
                    (
                        line,
                        score_add,
                        score_del,
                        score_feat,
                        matches,
                        prev_line,
                        prev_prev_line,
                        reset_scanning_buffer,
                    ) = scan_diff_line_addition(
                        line,
                        score_add,
                        score_del,
                        score_feat,
                        matches,
                        prev_line,
                        prev_prev_line,
                        reset_scanning_buffer,
                    )

                if reset_scanning_buffer:
                    prev_line = prev_prev_line = ""  # reset the scanning buffer
                else:
                    prev_prev_line = prev_line
                    prev_line = line

        if score_del + score_add + score_feat > 0:
            diff_items.append(diff_string)

    return diff_items, score_del, score_add, score_feat, matches


def scan_addon_commits(
    repo: git.Repo,
    addon: str,
    start_commit: git.Commit,
    end_commit: git.Commit,
    output_module_dir: str,
    keep_noise: bool = False,
    dump_methods: bool = True,
    bypass_structural_scan: bool = False,
    module_prefix: str = "addons/",
    start_index: int = -1,
    force_methods: Optional[bool] = None,
    continue_start: Optional[git.Commit] = None,
    serie_start_commit: Optional[git.Commit] = None,
):
    """Scan the addon commits and write the pseudo patches.
    `start_index`: 0-based index of the last pseudo patch already present
    in output_module_dir (--continue mode); new patches continue the
    numbering after it. `force_methods`: force the method signatures
    dump on (True) or off (False) even when no structural commit is
    kept; default None keeps the legacy behavior (dump only when some
    structural commit was kept or bypass mode). `continue_start`: the
    resume commit of a --continue run (start_commit then holds the
    resume commit for the structural scan). `serie_start_commit`: the
    serie start (merge base) used for the method signature delta so it
    keeps spanning the whole serie in --continue mode; defaults to
    start_commit."""
    if addon == "base" and module_prefix == "addons/":
        module_path = "odoo/addons/base/models/"
    else:
        module_path = f"{module_prefix}{addon}/models/"

    # Get the commits between the two found commits
    commits = list(
        repo.iter_commits(
            f"{start_commit.hexsha}..{end_commit.hexsha}", paths=module_path
        )
    )
    print(
        f"\n***** scanning {len(commits)} commits in addon: {addon}/models ".ljust(
            80, "*"
        )
    )

    result = []

    # shas of the commits already kept in this analysis dir (--continue
    # idempotency): a resume point that lands slightly too early (e.g.
    # batch merges with identical commit seconds) must not produce
    # duplicate patches for commits that were already kept
    existing_shas = set()
    if start_index >= 0:
        existing = Path(output_module_dir)
        if existing.is_dir():
            for filepath in existing.glob("*.patch"):
                if filepath.name == "method_signatures.patch":
                    continue
                try:
                    first = filepath.read_text(errors="ignore").splitlines()[:6]
                except OSError:
                    continue
                for line in first:
                    if line.startswith("From: "):
                        existing_shas.add(line[len("From: "):].strip())
                        break

    if not bypass_structural_scan:
        for commit in commits:
            if commit.hexsha in existing_shas:
                print(
                    f"SKIPPING already analysed commit {commit.hexsha[:10]}"
                    f" {commit.message.strip().splitlines()[0]}"
                )
                continue
            message = commit.message.strip()
            summary = message.splitlines()[0]
            if "forwardport" in summary.lower().replace(" ", "").replace("-", ""):
                # such ports may present structural changes in the diff
                # but we assume they aren't introducing new changes
                # since previous serie.
                # such false positives were common before version 13.
                continue

            summary_lower = summary.lower()
            if (
                "[lint]" in summary_lower
                or "make black" in summary_lower
                and "blacklist" not in summary_lower
            ):
                # mass reformatting commits (double quotes, black, ...)
                # produce huge quote-only twins that the text heuristic
                # would count as structural removals: skip them early.
                # (pure formatting commits never carry a data model change;
                # beware "make blacklist" which is not a black commit!)
                if addon == "base":
                    print(f"  skipping lint commit {commit.hexsha} {summary} ...")
                continue

            if commit.hexsha in BLACKLISTED_COMMITS:
                print(f"SKIPPING blacklisted commit {commit.hexsha[:10]} {summary}")
                continue

            if addon == "base":  # logging progress because base can be very slow...
                print(f"  scanning {commit.hexsha} {summary} ...")

            migration_diffs, matches_rem, matches_add, matches_feat, matches = scan_commit(
                module_path, commit
            )
            if matches_rem or matches_add or matches_feat:
                # only compute the stats of relevant commits (stats is a costly
                # git subprocess and total_changes is unused otherwise)
                total_changes = 0
                for file, stats in commit.stats.files.items():
                    if str(file).startswith(module_path):
                        total_changes += stats["lines"]

                pr = ""
                for line in message.splitlines():
                    if " odoo/odoo#" in str(line):
                        pr = str(line).split(" odoo/odoo#")[1].strip()

                # now some heuristics to keep only relevant commits.
                # commits removing fields are the most critical to keep.
                # commits removings or adding just a couple of fields with
                # a small diff are likely to be trivial and are not kept.
                is_noise = True
                is_big_feature = False
                if (
                    # is a change if many structural removals:
                    matches_rem >= 1
                    and total_changes > LINE_CHANGE_THRESHOLD
                    and len(message.splitlines()) > 20
                    or matches_rem >= 2
                    and total_changes > LINE_CHANGE_THRESHOLD
                    or matches_rem > 2
                    # is a change if some removals and many additions:
                    or matches_rem > 1
                    and matches_add > 3
                    and total_changes > LINE_CHANGE_THRESHOLD
                    # or matches_add > 3
                    # or matches_rem + matches_add > 4
                ):
                    is_noise = False

                if (
                    not is_noise
                    and matches_rem < 4
                    and matches_rem + matches_add < 5
                    and total_changes < 2 * LINE_CHANGE_THRESHOLD
                    and len(message.splitlines()) < 9
                ):
                    # medium change without too much removal and very little explanation can be skipped
                    print(f"SKIPPING NOISY COMMIT FROM PR {pr}", message)
                    is_noise = True

                elif (
                    is_noise
                    and "FIX" not in summary
                    and total_changes > LINE_CHANGE_FEAT_THRESHOLD
                    and len(message.splitlines()) > LINE_MESSAGE_FEAT_THRESHOLD
                ) or (
                    is_noise
                    and "FIX" not in summary
                    and matches_add + matches_feat > 5
                    and len(message.splitlines()) > LINE_MESSAGE_FEAT_THRESHOLD
                ):
                    is_noise = False
                    is_big_feature = True

                for blacklist in BLACKLISTS:
                    if blacklist in message:
                        is_noise = True
                        break

                # you may switch this test off to fine tune the is_noise computation
                if is_noise and not keep_noise:
                    continue

                result.append(
                    {
                        "is_noise": is_noise,
                        "is_big_feature": is_big_feature,
                        "commit_sha": commit.hexsha,
                        "total_changes": int(total_changes),
                        "author": commit.author.name,
                        "date": datetime.fromtimestamp(commit.committed_date).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        "summary": summary,
                        "message": message,
                        "pr": f"https://github.com/odoo/odoo/pull/{pr}",
                        "matches_rem": matches_rem,
                        "matches_add": matches_add,
                        "diffs": migration_diffs,
                        "matches": matches,
                    }
                )

    # Output the result
    if result:
        os.makedirs(output_module_dir, exist_ok=True)

    result.reverse()
    for item in result:
        start_index += 1
        # print(f"Commit SHA: {item['commit_sha']}")
        print(f"\nTotal Changes: {item['total_changes']}")
        print(
            f"Non trivial structural Changes: {item['matches_rem']} + {item['matches_add']}"
        )
        print(f"Date: {item['date']}")
        print(f"Summary: {item['summary']}")
        print(f"PR: {item['pr']}")

        heat_diff = 0
        if item["total_changes"] > 800:
            heat_diff = 4
        elif item["total_changes"] > 400:
            heat_diff = 3
        elif item["total_changes"] > 200:
            heat_diff = 2
        elif item["total_changes"] > 100:
            heat_diff = 1
        heat_struct_add = int(math.log2(item["matches_add"] + 1))
        heat_struct_rem = int(math.log2(item["matches_rem"] + 1))
        heat = f"{'+'*heat_struct_add}{'-'*heat_struct_rem}{'#'*heat_diff}".rjust(
            13, "_"
        )[: (9 if item["is_big_feature"] else 12)]

        if item["is_noise"]:
            prefix = "__noise"
        elif item["is_big_feature"]:
            prefix = "feat"
        else:
            prefix = "c"

        slug = slugify(item["summary"])[:70]
        filename = (
            f"{output_module_dir}/{prefix}{str(start_index).zfill(3)}"
            f"{heat}_{item['pr'].split('/')[-1]}_{slug}.patch"
        )
        print(filename)

        with open(filename, "w") as f:
            f.write(f"PR: {item['pr']}")
            f.write(f"\n\nFrom: {item['commit_sha']}")
            f.write(f"\nFrom: {item['author']}")
            f.write(f"\nDate: {item['date']}")
            if not item["is_big_feature"]:
                f.write(
                    f"\n\nBreaking data model changes scores: del:{item['matches_rem']} + add:{item['matches_add']}, change matches:"
                )
            for match in item["matches"]:
                f.write("\n" + match)
            f.write(f"\n\nTotal Changes: {item['total_changes']}")
            f.write("\n\n" + re.sub(r"^-", "*", item["message"], flags=re.MULTILINE))
            f.write("\n\n" + "=" * 33 + " pseudo patch: " + "=" * 33 + "\n")
            for diffs in item["diffs"]:
                for diff_item in diffs:
                    f.write(diff_item)

    if dump_methods and (
        bypass_structural_scan or force_methods is True or bool(result)
    ):
        generate_method_signatures_diff(
            repo,
            addon,
            start_commit,
            end_commit,
            output_module_dir,
            commit_items=result,
            module_prefix=module_prefix,
            # in --continue mode the regenerated delta spans the whole
            # serie (serie_start -> tip), so the commit annotations must
            # span it too: annotate whenever this run kept structural
            # commits OR the addon already has kept patches from a
            # previous scan (what the equivalent full scan would have
            # produced). Only addons without any kept patch at all keep
            # the cheap un-annotated dump.
            annotate_commits=bool(result)
            or (continue_start is not None and start_index >= 0),
            annotate_start=serie_start_commit or start_commit,
            serie_start=serie_start_commit or start_commit,
        )


def list_addons(repo_path: str, excludes: List[str]):
    directory = Path(f"{repo_path}/addons")
    subdirectories = ["base"]
    for d in directory.iterdir():
        if not d.is_dir():
            continue

        is_excluded = False
        for exclude in excludes:
            if d.name.startswith(exclude):
                is_excluded = True
                continue
        if is_excluded:
            continue

        subdirectories.append(d.name)
    return subdirectories


def find_worktree_by_branch(repo: git.Repo, branch: str):
    """Return the path of the worktree that has the given branch checked
    out, or None. Parses `git worktree list --porcelain` (GitPython has no
    worktree API). Works from any linked worktree or from the shared repo.
    """
    porcelain = repo.git.worktree("list", "--porcelain")
    for block in porcelain.split("\n\n"):
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        for line in lines[1:]:
            if line.startswith("branch "):
                checked = line[len("branch refs/heads/"):]
                if checked == branch:
                    return lines[0][len("worktree "):]
                break
    return None


def _resolve_rev(repo: git.Repo, rev: str) -> str:
    """Return the rev form that resolves in this repo: the bare branch
    name when a local ref exists (worktrees layout), else its
    origin/ counterpart (shared repo without local serie branches).
    Raises git.BadName when neither resolves."""
    try:
        repo.commit(rev)
        return rev
    except git.BadName:
        repo.commit(f"origin/{rev}")  # raise BadName if really missing
        return f"origin/{rev}"


def scan(
    repo_path: str,
    target_serie: int,
    output_dir: str,
    addon: str = "",
    dump_dependencies: bool = False,
    keep_noise: bool = False,
    commit: str = "",
    dump_methods: bool = True,
    bypass_structural_scan: bool = False,
    addons: Optional[List[str]] = None,
    continue_run: bool = False,
    skip_addon_readmes: bool = False,
) -> Optional[str]:
    """Scan the serie. Returns the end commit SHA when a full-serie
    (--addon-less) run completed cleanly, else None. The --continue
    logic uses that to maintain the .continue_state.json marker whose
    complete=true verdict is what unlocks the next incremental resume."""
    # Initialize local repo object.
    # In the shared repo + worktrees layout (~/DEV/odoo.git + per serie
    # worktrees) a git checkout is impossible: each serie branch is already
    # checked out in its own worktree (or the shared repo would be bare).
    # So we never checkout anything: revs are resolved repo wide and the
    # worktree paths are only used for filesystem lookups (addons listing).
    # `addons` is filled from the filesystem below, so remember now whether
    # the addon list was narrowed by the caller (dependency chain) or if
    # this is a whole-serie run: only the latter may mark the continue
    # state as complete at the end.
    # `addon` is reused as the loop variable below, so remember now
    # whether the CLI scoped the run to a single addon
    single_addon_mode = bool(addon)
    narrowed_addons = addons is not None
    repo = git.Repo(repo_path, search_parent_directories=True)
    target_rev = f"{target_serie}.0"
    prev_rev = f"{target_serie - 1}.0"

    print(f"Resolving rev {target_rev} ...")
    try:
        target_rev = _resolve_rev(repo, target_rev)
    except git.BadName:
        print(
            f"WARNING! serie {target_rev} not found, assuming master branch instead..."
        )
        target_rev = "master"
        repo.commit(target_rev)  # fail early if master is missing too

    # filesystem dir to list the addons from: prefer the worktree that has
    # the target branch checked out, else the repo main worktree if any
    fs_dir = None if repo.bare else Path(repo.working_tree_dir)
    worktree_dir = find_worktree_by_branch(repo, target_rev)
    if worktree_dir:
        fs_dir = worktree_dir
    if not addon and not fs_dir:
        print(
            "Error! Cannot find a worktree to list the addons from. "
            "Pass a specific --addon or add the serie worktree first: "
            "`git worktree add ~/DEV/odoo<serie>/odoo/src origin/<serie>.0`"
        )
        exit(1)

    if addons is None:
        if addon:
            addons = [addon]
        else:
            addons = list_addons(
                str(fs_dir),
                excludes=ADDON_PREFIX_FILTER,
            )
    print(f"Will scan {len(addons)} addons. (applied filter {ADDON_PREFIX_FILTER})")

    if commit:
        start_commit = repo.commit(commit).parents[0]
    else:
        # Get the commits for the branches
        print(f"Getting the merge base with previous serie {prev_rev} ...")
        target_serie_commit = repo.commit(target_rev)
        prev_serie_commit = repo.commit(_resolve_rev(repo, prev_rev))
        merge_base = repo.merge_base(target_serie_commit, prev_serie_commit)
        if not merge_base:
            print("Error! No merge base found between " f"{target_rev} and {prev_rev}!")
            print("Likely a shallow or partial clone (depth 1). Run in the repo:")
            print("  git fetch --unshallow origin")
            exit(1)
        start_commit = merge_base[0]

    start_date = datetime.fromtimestamp(start_commit.committed_date).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    print(
        f"Start commit {start_commit} - {start_date}: {start_commit.message.splitlines()[0].strip()}"
    )

    if commit:
        end_commit = repo.commit(commit)
        end_found = True
    else:
        # Find the end commit
        if target_rev == "master":
            # unreleased serie: there is no [REL] commit to find and
            # find_end_commit_by_serie would walk the whole history just to
            # fallback to the branch tip, so take the tip directly
            end_commit = repo.commit(target_rev)
            end_found = False
        else:
            end_commit, end_found = find_end_commit_by_serie(
                repo, target_serie, target_rev
            )
        end_date = datetime.fromtimestamp(end_commit.committed_date).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        print(
            f"End commit {end_commit} - {end_date}: {end_commit.message.splitlines()[0].strip()}"
        )

    # Ensure both commits are found
    if not start_commit or not end_commit:
        print(
            f"Could not find the required commits for versions {target_serie - 1} and {target_serie}"
        )
        exit(1)

    # Ensure both commits are different
    if start_commit == end_commit and not commit:
        print(
            f"Error! start_commit and end_commit are equal to {start_commit}! You may need to checkout the target serie branch or master first!"
        )
        exit(1)

    if end_found and target_rev != "master":
        serie = f"{target_serie}.0"
    else:
        # unreleased serie (master) or release commit not found:
        # manifestoo only knows released series, use the previous serie
        serie = f"{target_serie - 1}.0"

    continue_start: Optional[git.Commit] = None
    if continue_run:
        # a scoped or interrupted scan would leave the addons at uneven
        # frontiers: the next global resume would then skip the commits
        # of the addons left behind (corruption of the global invariant)
        if addon or (addons and len(addons) == 1):
            print(
                "Error! --continue cannot be combined with --addon:"
                " it resumes ALL addons from one global resume commit"
                " derived from the existing analysis; a single-addon run"
                " would advance only that addon and later full resumes"
                " would skip the commits of the others."
            )
            exit(1)

        state = read_continue_state(output_dir)
        if state and not state.get("complete", False):
            print(
                "Error! The previous scan of"
                f" {output_dir} did not complete (marker"
                f" {CONTINUE_STATE_FILENAME} present with complete=false)."
                " Its analysis dirs are at uneven frontiers: resuming from"
                " the newest kept commit would silently skip the commits"
                " of the addons that were not reached. Recovery options:"
            )
            print(
                "  - re-run the same scan without --continue to complete"
                " it (the existing patch files are kept), then --continue again"
            )
            print(
                "  - or delete the marker file"
                f" {Path(output_dir) / CONTINUE_STATE_FILENAME} to accept"
                " the risk"
            )
            exit(1)

        continue_addons = addons if addons else ([addon] if addon else [])
        continue_start = resolve_continue_start(
            repo, output_dir, target_rev, end_commit, continue_addons
        )
        if continue_start is None:
            print(
                "No usable --continue start found: falling back to a"
                " full serie scan."
            )
        elif continue_start == end_commit:
            print(
                "--continue: the serie tip is already the newest kept"
                " commit: nothing new to scan."
            )
            write_continue_state(
                output_dir, complete=True, end_sha=end_commit.hexsha
            )
            return end_commit.hexsha

        # guard the next resume: if this run is interrupted, the marker
        # tells the next --continue that the dirs are at uneven frontiers
        write_continue_state(output_dir, complete=False)

    for addon in addons:
        output_module_dir = (
            f"{output_dir}/{addon}"  # TODO we might add a version dir for OpenUpgrade
        )

        if dump_dependencies:  # TODO move to scan_addon_commits
            os.makedirs(output_module_dir, exist_ok=True)

            # expliciting all dependencies can help OpenUpgrade developpers or even improve AI migration training
            result = subprocess.run(
                [
                    "manifestoo",
                    "--addons-path",
                    str(Path(fs_dir) / "addons"),
                    f"--odoo-series={serie}",
                    "--select",
                    addon,
                    "tree",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(
                    "WARNING! manifestoo failed, dependencies.txt will be"
                    f" empty: {result.stderr.strip().splitlines()[-1:]}"
                )
            manifestoo_output = result.stdout
            with open(f"{output_module_dir}/dependencies.txt", "w") as f:
                f.write(manifestoo_output)

        addon_start = start_commit
        start_idx = -1
        force_methods: Optional[bool] = None
        if continue_run and continue_start is not None:
            addon_dir = Path(output_module_dir)
            has_patches = addon_dir.is_dir() and any(addon_dir.glob("*.patch"))
            if has_patches:
                # incremental: resume after the newest kept pseudo commit
                addon_start = continue_start
                start_idx = latest_patch_index(output_module_dir)
            else:
                # addon absent from the previous analysis: scan it fully
                # from the serie merge base like a first run would
                print(
                    f"NOTE! --continue: no existing analysis for {addon},"
                    " scanning it fully from the merge base."
                )
            # regenerate the method signatures only when the module
            # received at least one commit since the resume commit
            force_methods = (
                count_new_commits(
                    repo,
                    addon,
                    continue_start,
                    end_commit,
                )
                > 0
            )
            if not force_methods and has_patches:
                print(
                    f"--continue: no new commit in {addon} models since"
                    f" {continue_start.hexsha[:10]}:"
                    " keeping its method_signatures.patch as is."
                )

        scan_addon_commits(
            repo,
            addon,
            addon_start,
            end_commit,
            output_module_dir,
            keep_noise,
            dump_methods,
            bypass_structural_scan,
            start_index=start_idx,
            force_methods=force_methods,
            continue_start=continue_start,
            serie_start_commit=start_commit,
        )

    if continue_run:
        # clean exit: the analysis dirs share the same frontier again
        write_continue_state(
            output_dir, complete=True, end_sha=end_commit.hexsha
        )
    elif not single_addon_mode and not narrowed_addons:
        # full-serie run without --continue: on clean completion, mark
        # the dirs even so a later --continue is allowed (this is the
        # documented recovery for an interrupted previous run)
        write_continue_state(
            output_dir, complete=True, end_sha=end_commit.hexsha
        )
        create_serie_readme(
            target_serie,
            output_dir,
            prev_serie=target_serie - 1,
        )
        if not skip_addon_readmes:
            from odoo_module_diff.addon_readme import generate_addon_readmes

            generate_addon_readmes(target_serie, output_dir)
    return end_commit.hexsha


def scan_serie_addon(target_serie: int, addon: str, output_dir: str):
    """Scan a single core addon for the transition to the given serie,
    writing into output_dir/<addon>/ (Point E on-demand scan). Uses the
    default odoo.git shared repo (or the serie worktree) as entry point.
    Returns True when analysis files were produced or already present."""
    addon_dir = Path(output_dir) / addon
    if addon_dir.is_dir() and any(addon_dir.glob("*.patch")):
        return True
    repo_path = os.environ.get(
        "ODOO_MODULE_DIFF_REPO", str(Path.home() / "DEV" / "odoo.git")
    )
    scan(
        repo_path=repo_path,
        target_serie=target_serie,
        output_dir=str(output_dir),
        addon=addon,
        dump_dependencies=False,
    )
    if addon_dir.is_dir() and any(addon_dir.glob("*.patch")):
        return True
    # no structural commit kept for this step: still produce the method
    # signature delta so the step is not rendered as MISSING in spans
    scan(
        repo_path=repo_path,
        target_serie=target_serie,
        output_dir=str(output_dir),
        addon=addon,
        dump_dependencies=False,
        bypass_structural_scan=True,
    )
    return addon_dir.is_dir() and any(addon_dir.glob("*.patch"))


def human_size(num_bytes: int) -> str:
    """K-style human readable size, one decimal below 10 (e.g. 488K,
    1.1M) to stay close to the historical du-based README numbers."""
    value = float(num_bytes)
    for unit in ("", "K", "M", "G"):
        if value < 1024 or unit == "G":
            if unit and value < 10:
                return f"{value:.1f}{unit}"
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.0f}G"


def addons_patch_stats(root: Path) -> list:
    """Patch stats per addon dir: [(addon, patch_count, patch_bytes)]
    sorted by descending patch bytes, method_signatures.patch excluded.
    Shared by the per-serie README and the per-addon README summaries."""
    addons_stats = []
    for addon_dir in sorted(root.iterdir()):
        if not addon_dir.is_dir():
            continue
        patch_files = [
            f
            for f in addon_dir.glob("*.patch")
            if f.name != "method_signatures.patch"
        ]
        if not patch_files:
            continue
        # patch bytes only (method_signatures.patch excluded: it is a
        # per-addon constant, not the weight of the data model commits)
        addon_bytes = sum(f.stat().st_size for f in patch_files)
        addons_stats.append((addon_dir.name, len(patch_files), addon_bytes))
    addons_stats.sort(key=lambda item: item[2], reverse=True)
    return addons_stats


def create_serie_readme(
    target_serie: int,
    output_dir: str,
    prev_serie: int,
    github_base: str = "https://github.com/akretion/odoo-module-diff-analysis/blob/main",
) -> None:
    """Regenerate <output_dir>/README.md: a short, human browsable
    ranking of the most impacted addons (README_ADDON_LIMIT lines),
    each linking to the addon directory in the published analysis repo
    (github.com/akretion/odoo-module-diff-analysis)."""
    root = Path(output_dir)
    addons_stats = addons_patch_stats(root)
    total_patches = sum(count for _name, count, _size in addons_stats)
    total_bytes = sum(size for _name, _count, size in addons_stats)

    if not addons_stats:
        print(f"No patch found in {output_dir}: no README.md generated.")
        return

    addons_stats.sort(key=lambda item: item[2], reverse=True)
    shown = addons_stats[:README_ADDON_LIMIT]
    lines = [
        f"# Dude, what did they do to my Odoo at version {target_serie}.0?",
        "",
        "You can see below the Odoo addons that got the largest data model",
        f"changes between versions {prev_serie}.0 and {target_serie}.0:",
        "(this is just summing the size of the data model impacting commits",
        "addon per addon; method signature deltas are in each addon's",
        "method_signatures.patch)",
        "You can browse each directory to dig into the detail of these changes.",
        "",
    ]
    for rank, (name, count, size) in enumerate(shown, 1):
        link = f"{github_base}/{target_serie}.0/{name}"
        plural = "commit" if count == 1 else "commits"
        lines.append(
            f"{rank}. [{name}]({link}) - {human_size(size)} ({count} {plural})"
        )
    lines += [
        "",
        f"In total: {len(addons_stats)} addons, {total_patches} data model",
        f"impacting commits, {human_size(total_bytes)} of pseudo patches.",
        "",
        "Generated by [odoo-module-diff]"
        "(https://github.com/akretion/odoo-module-diff): these numbers are",
        "heuristic (see the repo README); the full pseudo patches are in each",
        "addon directory.",
    ]

    with open(root / "README.md", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Generated {root / 'README.md'} ({len(shown)} addons listed)")


app = typer.Typer()


@app.command()
def main(
    repo_path: str = typer.Argument(
        "", help="Path to the Odoo git repo (shared repo or any worktree)."
    ),
    target_serie: float = typer.Argument(
        0, help="Target serie, e.g. 20 for the 20.0/master serie."
    ),
    addon: str = "",
    output_dir: str = "module_diff_analysis",
    wrap_serie_dir: bool = True,
    dump_dependencies: bool = False,
    keep_noise: bool = False,
    commit: str = "",
    dump_methods: bool = True,
    bypass_structural_scan: bool = False,
    from_analysis: str = "",
    dump_context: bool = False,
    output_file: str = "",
    stdout: bool = False,
    max_bytes: int = 0,
    with_dependencies: bool = False,
    with_external: bool = False,
    from_serie: float = 0,
    to_serie: float = 0,
    max_bytes_per_step: int = 0,
    continue_run: bool = typer.Option(
        False,
        "--continue",
        help="Incremental scan: resume after the newest pseudo commit"
        " already present in the analysis output of the target serie"
        " (a `From:` SHA of the existing patches), instead of rescanning"
        " the whole serie. Fetches the serie branch first"
        " (--no-fetch to skip).",
    ),
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help="With --continue: do not fetch the serie branch before the"
        " incremental scan.",
    ),
    addon_readme: str = typer.Option(
        "",
        "--addon-readme",
        help="Generate the per addon README.md summary of the given addon"
        " (LLM based, needs litellm), reading the existing analysis of the"
        " serie. Without a value, limit it to the 30 addons with the most"
        " changes (same ranking as the per serie README). Add"
        " --from-analysis <dir> to point at the serie analysis dir.",
    ),
    skip_addon_readmes: bool = typer.Option(
        False,
        "--skip-addon-readmes",
        help="On a whole serie scan, do not generate the per addon"
        " README.md summaries of the top 30 addons.",
    ),
):
    target_serie = int(target_serie)  # (float this allows .0)

    if addon_readme != "" and addon_readme is not False:
        # standalone per addon README mode: aggregate from the analysis
        # cache, no git scan
        analysis_root = from_analysis or output_dir
        if wrap_serie_dir and not re.fullmatch(
            r"\d+\.0", Path(analysis_root).name
        ):
            analysis_root += f"/{target_serie}.0" if target_serie else ""
        if not target_serie:
            match = re.fullmatch(r"(\d+)\.0", Path(analysis_root).name)
            if not match:
                print(
                    "Error! --addon-readme needs the serie: pass it"
                    " positionally or use an analysis dir named like"
                    " <serie>.0"
                )
                exit(1)
            target_serie = int(match.group(1))
        from odoo_module_diff.addon_readme import generate_addon_readmes

        readme_addon = (
            addon_readme
            if addon_readme and addon_readme != "True" and addon_readme is not True
            else None
        )
        generate_addon_readmes(
            target_serie,
            analysis_root,
            addon=readme_addon,
        )
        return

    if continue_run:
        if commit:
            print(
                "Error! --continue and --commit are mutually exclusive:"
                " --commit analyses a single past commit while --continue"
                " extends an existing analysis up to the serie tip."
            )
            exit(1)
        if from_analysis or (from_serie and to_serie):
            print(
                "Error! --continue applies to a fresh git scan of the"
                " serie, not to the cache/span aggregation modes."
            )
            exit(1)

    if not target_serie and not (from_analysis or (from_serie and to_serie)):
        print(
            "Error! Pass the target serie positionally (e.g. 19), or"
            " --from-serie/--to-serie, or --from-analysis."
        )
        exit(1)

    if (from_serie or to_serie) and addon:
        # multi-serie span mode (Point E): one context section per serie step
        if not (from_serie and to_serie) or to_serie <= from_serie:
            print(
                "Error! Pass both --from-serie and --to-serie with"
                " --to-serie greater than --from-serie."
            )
            exit(1)
        analysis_root = from_analysis or os.environ.get(
            "ODOO_MODULE_DIFF_ANALYSIS",
            str(Path.home() / "DEV" / "odoo-module-diff-analysis"),
        )
        dump_span_context(
            addon,
            int(from_serie),
            int(to_serie),
            analysis_root,
            output_file=output_file,
            stdout=stdout,
            max_bytes_per_step=max_bytes_per_step,
            max_bytes=max_bytes,
        )
        return

    if from_analysis and not target_serie:
        # cache mode: infer the serie from the analysis dir name (e.g. .../19.0/)
        match = re.fullmatch(r"(\d+)\.0", Path(from_analysis).name)
        if match:
            target_serie = int(match.group(1))

    # dependency chain for --with-dependencies (both scan and cache modes)
    context_addons = None
    if with_dependencies and addon:
        if not target_serie:
            print(
                "Error! Pass the target serie positionally (e.g. 19) or use"
                " an analysis directory named like <serie>.0"
            )
            exit(1)
        deps_addons_path = find_addons_paths(target_serie, fs_dir=repo_path or "")
        context_addons = resolve_dependencies(
            target_serie, deps_addons_path, addon
        )
        if len(context_addons) > 1:
            print(
                "Including dependencies in the context: "
                f"{', '.join(context_addons)}"
            )
    elif with_dependencies and not addon:
        print(
            "WARNING! --with-dependencies requires --addon, ignoring it.",
            file=sys.stderr,
        )
    elif with_external and addon and context_addons is None:
        # external addon without deps resolved: still scan/aggregate it alone
        context_addons = [addon]

    # split the chain into core addons (odoo.git / analysis cache) and
    # external addons (scanned on the fly in their own repos)
    external_dirs: dict = {}
    core_chain = None
    if with_external and context_addons:
        core_chain = [
            chain_addon
            for chain_addon in context_addons
            if not find_external_repo(
                target_serie, chain_addon, fs_dir=repo_path or ""
            )
        ]

    if from_analysis:
        # cache mode: aggregate existing analysis files without any git scan
        if not target_serie:
            print(
                "Error! Pass the target serie positionally (e.g. 19) or use"
                " an analysis directory named like <serie>.0"
            )
            exit(1)
        if not dump_context:
            print("Tip: --from-analysis implies --dump-context.", file=sys.stderr)
        context_dir = from_analysis
        addon_name = addon
        # scan the external addons of the chain on the fly (Point D)
        if with_external and context_addons:
            print(
                f"External addons will be scanned into {Path(output_dir).resolve()}"
            )
            for chain_addon in context_addons:
                if chain_addon in (core_chain or []):
                    continue
                scan_external_addon(
                    chain_addon,
                    target_serie,
                    output_dir=output_dir,
                    keep_noise=keep_noise,
                    dump_methods=dump_methods,
                    fs_dir=repo_path or "",
                    addons_dirs=external_dirs,
                )
        dump_context_impl(
            analysis_dir=context_dir,
            addon=addon_name,
            addons=context_addons,
            addons_dirs=external_dirs or None,
            from_label=f"{target_serie - 1}.0",
            to_label=f"{target_serie}.0",
            output_file=output_file,
            stdout=stdout,
            max_bytes=max_bytes,
        )
        return

    if wrap_serie_dir and str(target_serie) not in output_dir:
        output_dir += f"/{target_serie}.0"

    # in with_external mode, external addons of the chain are scanned on the
    # fly in their own repo, not in odoo.git where they don't exist
    scan_addons = core_chain
    if continue_run and not no_fetch:
        # fetch once, before resolving the start/end commits: the whole
        # point of --continue is to catch the newest serie commits
        fetched_rev = f"{int(target_serie)}.0"  # bare branch name for the fetch
        try:
            scan_repo = git.Repo(
                repo_path or str(Path.cwd()), search_parent_directories=True
            )
            print(f"Fetching origin {fetched_rev} ...")
            scan_repo.git.fetch("origin", fetched_rev)
        except git.GitCommandError as err:
            print(
                f"WARNING! Fetch of origin {fetched_rev} failed"
                " (offline? branch not created yet?):"
                f" continuing with the local tip. ({err})"
            )
    scan(
        repo_path=repo_path,
        target_serie=target_serie,
        addon=addon if not scan_addons else "",
        output_dir=output_dir,
        dump_dependencies=dump_dependencies,
        keep_noise=keep_noise,
        commit=commit,
        dump_methods=dump_methods,
        bypass_structural_scan=bypass_structural_scan,
        addons=scan_addons,
        continue_run=continue_run,
        skip_addon_readmes=skip_addon_readmes,
    )
    if with_external:
        for chain_addon in context_addons or ([addon] if addon else []):
            if scan_addons and chain_addon in scan_addons:
                continue
            scan_external_addon(
                chain_addon,
                target_serie,
                output_dir=output_dir,
                keep_noise=keep_noise,
                dump_methods=dump_methods,
                fs_dir=repo_path or "",
                addons_dirs=external_dirs,
            )

    if dump_context:
        dump_context_impl(
            analysis_dir=output_dir,
            addon=addon,
            addons=context_addons,
            addons_dirs=external_dirs or None,
            from_label=f"{target_serie - 1}.0",
            to_label=f"{target_serie}.0",
            output_file=output_file,
            stdout=stdout,
            max_bytes=max_bytes,
        )


if __name__ == "__main__":
    app()
