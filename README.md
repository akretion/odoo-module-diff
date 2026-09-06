# Dude, what did they do to my Odoo at version X? Find out with odoo-module-diff!

<!--- shortdesc-begin -->

A CLI tool to extract key commits impacting database migration between [Odoo](https://odoo.com) series.

<!--- shortdesc-end -->

## Installation

<!--- install-begin -->

```console
git clone https://github.com/akretion/odoo-module-diff
cd odoo-module-diff
pip install .
```

## Features

<!--- features-begin -->

`odoo-module-diff` provides the following features:

* Extracting the relevant key commits impacting database migration out of the bugfix and gimmick commit noise. Indeed less than 1 commit in 50 actually impacts anything for the database migration.
* Scanning all the repo addons or only a specific addon.
* Listing the key commits, addon by addon and with a 'heat' in the name (+/-/#) to explicit how much the commit added lines with `= fields.|_inherit = |_inherits = `, removed such lines or how large is the diff in general so you can see at a glance what are the most impacting commits for a given addon migration.
* The idea is to help people doing the OCA/OpenUpgrade scripts, and eventually integrate the odoo-module-diff analysis files with the standard OpenUpgrade analysis files. But it will help you to migrate your modules in general or help you find out what are the benefits and pitfalls to migrate to version X for module Y.
* What about xml_id changes? Well these would be harder to track in commit diffs. However, they are very well detected by the standard OCA/OpenUpgrade analysis tool and it's usually easy to accomodate for xml_id changes. If not then, it's likely the change will be part of the larger commit tracked by odoo-module-diff.
* Eventually it could complete the existing OCA/OpenUpgrade analysis files to provide a more complete learning dataset to train LLM models to write OpenUprade migration scripts (I don't expect AI to write more than the half of the easiest scripts, but that could still be a win, in the future I mean).


## Usage

```console
python odoo_module_diff/main.py <path_to_odoo_repo> <target_serie>
```

or once installed:

```console
odoo-module-diff <path_to_odoo_repo> <target_serie> [--addon <addon>]
```

Useful options:

* `--addon <addon>`: scan a single addon instead of all of them.
* `--output-dir <dir>`: where to write the analysis files (default `module_diff_analysis/<serie>.0`).
* `--commit <sha>`: only analyse a single commit (and its diff from its first parent) instead of the whole serie range.
* `--dump-dependencies`: also write a manifestoo dependency tree per addon.
* `--keep-noise`: keep the commits detected as noise (grey results) too.
* `--dump-context`: after the scan, aggregate all the pseudo patch files of
  the target addon (or of every scanned addon) into a single
  `migration_context.md` file next to the analysis files: a ready-to-use
  migration context for AI agents (see the odoo-migration skill,
  investigation step 2).
* `--from-analysis <dir>`: aggregate existing analysis files (e.g. from
  [odoo-module-diff-analysis](https://github.com/akretion/odoo-module-diff-analysis)
  or a previous run) without scanning git at all. Combine with `--addon`,
  `--output-file`, `--stdout` and `--max-bytes`. The serie is inferred from
  the directory name (`.../19.0/`) or passed positionally.
* `--with-dependencies`: with `--addon`, resolve the transitive dependencies
  with manifestoo and aggregate them first, in topological (migration) order:
  core addons come from the analysis files (missing ones are marked
  `[MISSING]`), while OCA/external dependencies are not scanned yet (they
  will appear as `[MISSING]` placeholders). The addons-path is auto-detected
  from `$ODOO_PARENT_HOME/odoo<serie>/odoo/` (core `src/addons`,
  `src/odoo/addons` and every `external-src/*` repo), from the given repo
  worktree, or from the matching worktree of a shared repository.
* `--with-external`: with `--with-dependencies`, additionally scan the
  external (OCA/custom) addons of the dependency chain on the fly, in their
  own git repositories (e.g. `external-src/l10n-brazil`), instead of
  reporting them as missing. External addons are located in the
  `external-src` dirs of the serie environment and scanned with the merge
  base of the two serie version branches as start boundary; when the target
  serie branch was recreated with unrelated history (e.g. the OCA 19.0
  branch recreation), the previous serie branch tip is used instead.
* `--output-file <path>`: where to write the aggregated context (default:
  `<analysis_dir>/migration_context.md`).
* `--stdout`: stream the aggregated context to stdout (logs go to stderr).
* `--max-bytes <n>`: size budget for the aggregated context; the method
  signatures patch and the structural patches are included first (noise
  last) and skipped files are listed at the end of the document.

Examples:

```console
# aggregate the cached 19.0 analysis of the account addon into one context file
odoo-module-diff --from-analysis ~/DEV/odoo-module-diff-analysis/19.0 --addon account

# scan the future 20.0 serie and stream a context under 100 KB for an agent prompt
odoo-module-diff ~/DEV/odoo.git 20 --addon account --dump-context --stdout --max-bytes 100000 > context.md
```

The tool compares the target serie against the previous one (e.g. serie `19`
compares the merge base of `19.0` up to the `[REL] 19.0` release commit).

### Analysing an unreleased serie (master)

To analyse what will become the next serie (e.g. 20.0 while only the `master`
branch exists), just pass the future serie number: if the `<serie>.0` branch
does not exist, the `master` branch is used instead, and the scan then covers
the merge base between the previous serie branch and master up to the current
master tip. The serie label falls back to the previous serie for tools
depending on released series (such as manifestoo).

### Shared repository + worktrees layout

If your Odoo clones are git worktrees of a shared repository (e.g. a bare
`~/DEV/odoo.git` with one worktree per serie such as `~/DEV/odoo19/odoo/src`
checked out on `19.0`), the tool detects it: it never performs any `git
checkout`, so you can pass indifferently the shared repository path or any of
the worktree paths as `<path_to_odoo_repo>`, even while the serie branches are
checked out in the other worktrees. The worktree of the target serie (or of
master for an unreleased serie) is only used to list the addons on the
filesystem. Note that the shared repository must not be a shallow clone
(otherwise there is no merge base between the branches: run `git fetch
--unshallow origin` in it once).

## Example

[Here is a systematic commit analysis between the different Odoo series using odoo-module-diff](https://github.com/akretion/odoo-module-diff-analysis)
