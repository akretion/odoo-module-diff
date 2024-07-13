import math
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List

import git
import typer
from slugify import slugify

LINE_CHANGE_THRESHOLD = 30
LINE_CHANGE_FEAT_THRESHOLD = 200
LINE_MESSAGE_FEAT_THRESHOLD = 45
NON_TRIVIAL_FIELD_ATTRS = (
    "company_dependent=",
    "store=",
    "compute=",
    "recursive=",
    # "inverse=",
)
ADDON_PREFIX_FILTER = ["l10n_", "website_", "test"]


def find_end_commit_by_serie(repo: git.Repo, target_serie: int):
    """
    Find the most recent commit with a specific message.
    Return the more recent commit if no match is found.
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
    for commit in repo.iter_commits():
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
    matches_add: float,
    matches_rem: float,
    matches_feat: float,
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
        matches_rem += 1

    elif (
        " _inherits =" in line
        or " _inherits =" in prev_line
        and prev_line.endswith("[")
        or " _inherits =" in prev_prev_line
        and prev_prev_line.endswith("[")
    ) and "AbstractModel" not in (line + prev_line + prev_prev_line):
        reset_scanning_buffer = True
        matches.append(line)
        matches_rem += 1

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
            matches_rem += 0.4  # wheights less because just an attr change
        else:
            matches_rem += 1
        if "2many(" in line:  # relations removal weights more
            matches_rem += 1

    return (
        line,
        matches_add,
        matches_rem,
        matches_feat,
        matches,
        prev_line,
        prev_prev_line,
        reset_scanning_buffer,
    )


def scan_diff_line_addition(
    line: str,
    matches_add: float,
    matches_rem: float,
    matches_feat: float,
    matches: List[str],
    prev_line: str,
    prev_prev_line: str,
    reset_scanning_buffer: bool,
):
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
                match[1:].split("(")[0]
                == line[1:].split("(")[0]  # same field name and type
            ):
                removed_match = match
                break

        if removed_match:
            # new we try to detect trivial field attrs changes:
            non_trivial_prev = set()
            for key in NON_TRIVIAL_FIELD_ATTRS:
                if key in removed_match:
                    if key == "compute=":
                        # we don't want to track the exact compute method
                        value = "some_method"
                    else:
                        value = removed_match.split(key)[-1]
                        value = value.split(",")[0].split(")")[0]
                    non_trivial_prev.add(f"{key}{value}")

            non_trivial_line = set()
            for key in NON_TRIVIAL_FIELD_ATTRS:
                if key in line:
                    if key == "compute=":
                        # we don't want to track the exact compute method
                        value = "some_method"
                    else:
                        value = line.split(key)[-1]
                        value = value.split(",")[0].split(")")[0]
                    non_trivial_line.add(f"{key}{value}")

            if non_trivial_prev == non_trivial_line:
                # print(
                #    "  NOT COUNTING trivial field change:",
                # )
                # print("  " + removed_match)
                # print("  " + line)
                matches_rem -= 1  # cancel our previous match
                if "2many(" in line:  # relations removal weights more
                    matches_rem -= 1

                matches.remove(removed_match)
            else:
                matches_rem -= 0.6  # field isn't removed but some attr changed
                if "2many(" in line:  # relations removal weights more
                    matches_rem -= 1
                matches.append(line)  # we help diff visualization

        else:
            if "2many(" in line:  # adding relations weights more
                matches_add += 1
                matches.append(line)
            else:  # non relational field addition give no migration work
                matches_feat += 1
    else:
        matches_add += 0.2  # weights less because only a trivial attr additive change

    return (
        line,
        matches_add,
        matches_rem,
        matches_feat,
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
    matches_rem = 0
    matches_add = 0
    matches_feat = 0
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

            for line in diff_item_string.splitlines():
                line = line.split(" #")[0].strip().replace("\t", " ")
                reset_scanning_buffer = False

                if line.startswith("-    ") and not line.startswith("-        "):
                    (
                        line,
                        matches_add,
                        matches_rem,
                        matches_feat,
                        matches,
                        prev_line,
                        prev_prev_line,
                        reset_scanning_buffer,
                    ) = scan_diff_line_removal(
                        line,
                        matches_add,
                        matches_rem,
                        matches_feat,
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
                        matches_add,
                        matches_rem,
                        matches_feat,
                        matches,
                        prev_line,
                        prev_prev_line,
                        reset_scanning_buffer,
                    ) = scan_diff_line_addition(
                        line,
                        matches_add,
                        matches_rem,
                        matches_feat,
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

        if matches_rem + matches_add + matches_feat > 0:
            diff_items.append(diff_string)

    return diff_items, matches_rem, matches_add, matches_feat, matches


def scan_addon_commits(
    repo: git.Repo,
    addon: str,
    start_commit: git.Commit,
    end_commit: git.Commit,
    output_module_dir: str,
    keep_noise: bool = False,
):
    if addon == "base":
        module_path = "odoo/addons/base/models/"
    else:
        module_path = f"addons/{addon}/models/"

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

    for commit in commits:
        message = commit.message.strip()
        summary = message.splitlines()[0]
        if "forwardport" in summary.lower().replace(" ", "").replace("-", ""):
            # such ports may present structural changes in the diff
            # but we assume they aren't introducing new changes
            # since previous serie.
            # such false positives were common before version 13.
            continue

        if addon == "base":  # logging progress because base can be very slow...
            print(f"  scanning {commit.hexsha} {summary} ...")

        total_changes = 0
        for file in commit.stats.files:
            if str(file).startswith(module_path):
                total_changes += commit.stats.files[file]["lines"]

        migration_diffs, matches_rem, matches_add, matches_feat, matches = scan_commit(
            module_path, commit
        )
        if matches_rem or matches_add or matches_feat:
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
                matches_rem == 1
                and total_changes > LINE_CHANGE_THRESHOLD
                and len(message.splitlines()) > 20
                or matches_rem == 2
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
                and len(message.splitlines()) < 10
            ):
                # medium change without too much removal and very little explanation can be skipped
                print(f"SKIPPING NOISY COMMIT FROM PR {pr}", message)
                is_noise = True

            if (
                is_noise
                and not "FIX" in summary
                and total_changes > LINE_CHANGE_FEAT_THRESHOLD
                and len(message.splitlines()) > LINE_MESSAGE_FEAT_THRESHOLD
            ) or (
                is_noise
                and not "FIX" in summary
                and matches_add + matches_feat > 5
                and len(message.splitlines()) > LINE_MESSAGE_FEAT_THRESHOLD
            ):
                is_noise = False
                is_big_feature = True

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
    for idx, item in enumerate(result):
        # print(f"Commit SHA: {item['commit_sha']}")
        print(f"\nTotal Changes: {item['total_changes']}")
        print(
            f"Non trivial structural Changes: {item['matches_rem'] + item['matches_add']}"
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

        filename = f"{output_module_dir}/{prefix}{str(idx).zfill(3)}{heat}_{item['pr'].split('/')[-1]}_{slugify(item['summary'])[:70]}.patch"
        print(filename)

        with open(filename, "w") as f:
            f.write(f"PR: {item['pr']}")
            f.write(f"\n\nFrom: {item['commit_sha']}")
            f.write(f"\nFrom: {item['author']}")
            f.write(f"\nDate: {item['date']}")
            f.write(
                f"\n\nBreaking data model changes score: {item['matches_rem'] + item['matches_add']}, change matches:"
            )
            for match in item["matches"]:
                f.write("\n" + match)
            f.write(f"\n\nTotal Changes: {item['total_changes']}")
            f.write("\n\n" + re.sub(r"^-", "*", item["message"], flags=re.MULTILINE))
            f.write("\n\n" + "=" * 33 + " pseudo patch: " + "=" * 33 + "\n")
            for diffs in item["diffs"]:
                for diff_item in diffs:
                    f.write(diff_item)


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


def scan(
    repo_path: str,
    target_serie: int,
    output_dir: str,
    addon: str = "",
    dump_dependencies: bool = False,
    keep_noise: bool = False,
    commit: str = "",
):
    # Initialize local repo object
    repo = git.Repo(repo_path)

    print(f"git checkout {target_serie}.0 ...")
    repo.git.checkout(f"{target_serie}.0")

    if addon:
        addons = [addon]
    else:
        addons = list_addons(
            repo_path,
            excludes=ADDON_PREFIX_FILTER,
        )
    print(f"Will scan {len(addons)} addons. (applied filter {ADDON_PREFIX_FILTER})")

    if commit:
        start_commit = repo.commit(commit).parents[0]
    else:
        # Get the commits for the branches
        print(f"Getting the merge base with previous serie {target_serie - 1}.0 ...")
        target_serie_commit = repo.commit(f"{target_serie}.0")
        prev_serie_commit = repo.commit(f"{target_serie - 1}.0")
        merge_base = repo.merge_base(target_serie_commit, prev_serie_commit)
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
        end_commit, end_found = find_end_commit_by_serie(repo, target_serie)
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

    if end_found:
        serie = f"{target_serie}.0"
    else:
        serie = f"{target_serie - 1}.0"

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
                    "odoo/src/addons",
                    f"--odoo-series={serie}",
                    "--select",
                    addon,
                    "tree",
                ],
                capture_output=True,
                text=True,
            )
            manifestoo_output = result.stdout
            with open(f"{output_module_dir}/dependencies.txt", "w") as f:
                f.write(manifestoo_output)

        scan_addon_commits(
            repo, addon, start_commit, end_commit, output_module_dir, keep_noise
        )


def create_serie_readme(target_serie: int, output_dir: str):
    result = subprocess.run(
        ["find", ".", "-type", "f", "-name", "*.patch"],
        capture_output=True,
        cwd=output_dir,
        text=True,
    )
    commits = len(result.stdout.splitlines())

    commits_size = subprocess.run(
        ["du", "-sh", "."],
        capture_output=True,
        cwd=output_dir,
        text=True,
    ).stdout

    command = 'du -sh -- */ | sort -rh | head -n 30 | awk \'{sub(/\\/$/, "", $2); print NR ". " $2 " - " $1}\''
    result = subprocess.run(
        command, shell=True, capture_output=True, cwd=output_dir, text=True
    )
    table = result.stdout

    readme = f"""# How crazy it is to migrate to Odoo {target_serie}.0?

There are {commits} non trivial commits impacting the database structure to migrate
from Odoo {target_serie -1}.0 to {target_serie}.0
Together theses commits weight {commits_size}.

The addons that changed the most are listed below with their relative migration commit sizes:
    """

    with open(f"{output_module_dir}/README.md", "w") as f:
        f.write(readme)


app = typer.Typer()


@app.command()
def main(
    repo_path: str,
    target_serie: float,
    addon: str = "",
    output_dir: str = "module_diff_analysis",
    wrap_serie_dir: bool = True,
    dump_dependencies: bool = False,
    keep_noise: bool = False,
    commit: str = "",
):
    target_serie = int(target_serie)  # (float this allows .0)
    if wrap_serie_dir and str(target_serie) not in output_dir:
        output_dir += f"/{target_serie}.0"
    scan(
        repo_path=repo_path,
        target_serie=target_serie,
        addon=addon,
        output_dir=output_dir,
        dump_dependencies=dump_dependencies,
        keep_noise=keep_noise,
        commit=commit,
    )


if __name__ == "__main__":
    app().run(main)
