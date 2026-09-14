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

## Usage: dump a migration context (the default use case)

You normally **don't scan anything**: the pseudo patches of every Odoo
serie and addon are shared through the
[odoo-module-diff-analysis](https://github.com/akretion/odoo-module-diff-analysis)
repository. Clone it once and point the tool at it:

```console
git clone https://github.com/akretion/odoo-module-diff-analysis
export ODOO_MODULE_DIFF_HOME=/path/to/odoo-module-diff-analysis
```

Then dump the migration context of any core addon (`-d` is the short form
of `--dump-context`, `-o` of `--output-file`):

```console
# account 18.0 -> 19.0, with the transitive dependency chain
odoo-module-diff --addon account 19 -d -o ~/tmp/account_ctx.md --with-dependencies

# same, multi-serie span (12 -> 19, one section per serie step)
odoo-module-diff --addon account 19 -d -o ~/tmp/account_span.md --from-serie 12

# OCA / custom addons are located through an Odoo config and analysed
# on the fly from their local clone (PR based, see below)
odoo-module-diff --addon l10n_br_fiscal --odoo-cfg ~/DEV/odoo18/odoo.cfg 18 -d -o ~/tmp/fiscal_ctx.md
```

The context aggregates, per serie step and per addon: the README.md
summary when one was generated, then the pseudo patches of the breaking
commits (biggest first), then the method signature deltas with the sha1
of each contributing commit, then the dependency trees. Missing addons
(or addons without significant changes) are marked `[MISSING]` rather
than being scanned: dumping a context never scans the core repository.

The analysis home is resolved from `--from-analysis <dir>`, then
`$ODOO_MODULE_DIFF_HOME` (legacy alias `$ODOO_MODULE_DIFF_ANALYSIS`),
then the default `~/DEV/odoo-module-diff-analysis` — with a warning
suggesting the clone when you rely on the default. The serie is passed
positionally (`19`) or with `--to-serie`; the from serie defaults to
`serie - 1` (`--from-serie 12` starts a multi-serie span).

Non core (OCA / custom project) addons are detected when you pass
`--odoo-cfg <config>` or `--addons-path <comma,separated,list>`: the
tool locates their local clone (using manifestoo, the same mechanism as
akaidoo) and analyses them **on the fly**, without writing anything to
the analysis repo. OCA branches are history-rewritten on merge (bot
version bumps, squash merges), so instead of trusting the branch
commits the tool detects the milestone commits — serie migrations
(`A.B` version bump of `A.B.C.D.E`), minor bumps and
`<addon>/migrations/` script commits — and resolves each of them to its
GitHub Pull Request: the PR descriptions are where OCA addons document
the breaking changes. Set `$GITHUB_TOKEN` for a higher API rate limit.

The LLM-generated per addon README summaries (present for the 30 most
impacted addons of each serie in the analysis repo) are regenerated
with `--addon-readmes` / `--addon-readme <addon>`: they call an LLM
through the `openai` package (loose dependency, `pip install
odoo-module-diff[llm]`) with a DeepSeek default model
(`deepseek/deepseek-flash`, override with `$ODOO_MODULE_DIFF_LLM_MODEL`,
key in `$DEEPSEEK_API_KEY`), combining the pseudo patches with the
official release notes (`https://www.odoo.com/odoo-<serie>-release-notes`,
cached as `<serie>.0/RELEASE_NOTE.md`; Enterprise-only features are
filtered out).

## Building the analysis files (maintainers)

The analysis repo is built by scanning the Odoo git history — this is
the slow path (hours per serie), reserved for the maintainers of
[odoo-module-diff-analysis](https://github.com/akretion/odoo-module-diff-analysis):

```console
# full serie scan into the analysis repo
odoo-module-diff ~/DEV/odoo.git 20

# single addon, or a single commit
odoo-module-diff ~/DEV/odoo.git 20 --addon account
odoo-module-diff ~/DEV/odoo.git 20 --commit <sha>
```

Useful scan options:

* `--output-dir <dir>`: where to write the analysis files (default
  `module_diff_analysis/<serie>.0`).
* `--continue`: incremental re-scan of an already analysed serie: resume
  after the newest pseudo commit already present (a `From:` SHA of the
  existing patches) instead of rescanning everything. Fetches the serie
  branch first (`--no-fetch` to skip). Never combine with `--addon`
  (the resume point is global to the serie).
* `--dump-dependencies`: also write a manifestoo dependency tree per addon.
* `--keep-noise`: keep the commits detected as noise (grey results) too.
* `--skip-addon-readmes`: skip the LLM README generation of the top 30
  addons (they are API calls).
* `--dump-methods / --no-dump-methods`: disable the method signatures
  patch generation (on by default).

**Non core (OCA / custom) addons are never part of that build phase**:
they are analysed on the fly in their local clone, as described in the
usage section above (`--odoo-cfg` / `--addons-path`).

The scan compares the target serie against the previous one (e.g. serie
`19` compares the merge base of `19.0` up to the `[REL] 19.0` release
commit). To analyse an unreleased serie, pass the future serie number:
if the `<serie>.0` branch does not exist, `master` is used instead
(manifestoo labels fall back to the previous serie).

If your Odoo clones are git worktrees of a shared repository (e.g. a bare
`~/DEV/odoo.git` with one worktree per serie such as `~/DEV/odoo19/odoo/src`
checked out on `19.0`), the tool detects it: it never performs any `git
checkout`, so you can pass indifferently the shared repository path or any of
the worktree paths, even while the serie branches are checked out in the
other worktrees. The worktree of the target serie is only used to list the
addons on the filesystem. The shared repository must not be a shallow clone
(otherwise there is no merge base between the branches: run `git fetch
--unshallow origin` in it once).

## Features

<!--- features-begin -->

`odoo-module-diff` provides the following features:

* Extracting the relevant key commits impacting database migration out of the bugfix and gimmick commit noise. Indeed less than 1 commit in 50 actually impacts anything for the database migration.
* Scanning all the repo addons or only a specific addon.
* Listing the key commits, addon by addon and with a 'heat' in the name (+/-/#) to explicit how much the commit added lines with `= fields.|_inherit = |_inherits = `, removed such lines or how large is the diff in general so you can see at a glance what are the most impacting commits for a given addon migration.
* Generating a per-serie `README.md` ranking the most impacted addons (a
  "Dude, what did they do to my Odoo" digest, regenerated on every clean
  whole-serie scan) and, per addon, an LLM written `README.md` migration
  guide for the 30 most impacted addons.
* Aggregating the per-addon analysis files into one AI-agent-sized
  migration context (`--dump-context`, `--with-dependencies`,
  `--from-serie/--to-serie`, `--max-bytes`), and generating customer
  facing per-addon migration READMEs from the pseudo patches plus the
  official Odoo release notes.
* The idea is to help people doing the OCA/OpenUpgrade scripts, and eventually integrate the odoo-module-diff analysis files with the standard OpenUpgrade analysis files. But it will help you to migrate your modules in general or help you find out what are the benefits and pitfalls to migrate to version X for module Y.
* What about xml_id changes? Well these would be harder to track in commit diffs. However, they are very well detected by the standard OCA/OpenUpgrade analysis tool and it's usually easy to accomodate for xml_id changes. If not then, it's likely the change will be part of the larger commit tracked by odoo-module-diff.
* Eventually it could complete the existing OCA/OpenUpgrade analysis files to provide a more complete learning dataset to train LLM models to write OpenUprade migration scripts (I don't expect AI to write more than the half of the easiest scripts, but that could still be a win, in the future I mean).

## Additional context options

* `--output-file <path>` (`-o`): where to write the aggregated context
  (default: `<analysis_dir>/migration_context.md`).
* `--stdout`: stream the aggregated context to stdout (logs go to stderr).
* `--max-bytes <n>`: size budget for the aggregated context; the biggest
  patches are included first (noise last) and skipped files are listed
  at the end of the document.
* `--with-dependencies`: resolve the transitive dependencies of `--addon`
  with manifestoo and aggregate them first, in topological (migration)
  order. The addons-path is auto-detected from
  `$ODOO_PARENT_HOME/odoo<serie>/odoo/`, from the given repo worktree, or
  from the matching worktree of a shared repository.
* `--with-external`: additionally scan the external (OCA/custom) addons
  of the dependency chain on the fly, in their own git repositories
  (e.g. `external-src/l10n-brazil`), instead of reporting them as
  missing.
* `--from-serie <n>`: build a multi-serie span (one context section per
  serie step, e.g. `--from-serie 12 --to-serie 19` yields the 12->13,
  ..., 18->19 steps). Steps are read from the analysis home; missing
  serie steps are reported as missing (never scanned in context mode).
  `--max-bytes-per-step` bounds each step independently.

## Example

[Here is a systematic commit analysis between the different Odoo series using odoo-module-diff](https://github.com/akretion/odoo-module-diff-analysis)
