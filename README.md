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

## Environment variables (the two homes)

`odoo-module-diff` relies on two distinct directories; do not confuse
them:

* **`ODOO_MODULE_DIFF_HOME`** — a local clone of
  [odoo-module-diff-analysis](https://github.com/akretion/odoo-module-diff-analysis):
  the shared cache of the **core addon** pseudo patches
  (`<home>/<serie>.0/<addon>/`). It is the place to find the core
  changes and to write new core scans. Without it the tool falls back to
  `~/DEV/odoo-module-diff-analysis` and warns; scanning the Odoo commits
  from scratch is extremely slow, so clone the repository once and point
  this variable at the clone. `--from-analysis <dir>` overrides it per
  invocation.
* **`ODOO_PARENT_HOME`** — the directory holding your per serie Odoo
  envs (`odoo16/`, `odoo18/`, `odoo19/`, ... each with its `odoo/`
  subdir, its `odoo.cfg` and its `external-src/` OCA clones). It is how
  the tool locates the OCA/custom addons, their git repositories, the
  core addons dirs and the `odoo.cfg` used to resolve dependencies.
  Default `~/DEV`; there is **no CLI option** for it (only the
  environment variable). A warning is printed when the variable points
  nowhere, or when it is unset and no `odoo<serie>/` env can be found at
  the default location — in both cases the OCA clones and the env
  configs cannot be located.

`--odoo-cfg <file>` may be omitted: the env holding the requested addon
is auto-detected under `ODOO_PARENT_HOME` (nearest serie wins).

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
summary when one was generated, the pseudo patches of the breaking
commits (biggest first) and the method signature deltas with the sha1 of
each contributing commit (a `# Method Signatures Delta: <addon>`
section). With `--with-dependencies`, the whole transitive closure is
appended to the SAME document, dependencies first and target addon last.
Missing addons (or addons without significant changes) are marked
`[MISSING]` rather than being scanned: dumping a context never scans the
core repository.

The analysis home is resolved from `--from-analysis <dir>`, then
`$ODOO_MODULE_DIFF_HOME` (legacy alias `$ODOO_MODULE_DIFF_ANALYSIS`),
then the default `~/DEV/odoo-module-diff-analysis` — with a warning
suggesting the clone when you rely on the default. The serie is passed
positionally (`19`) or with `--to-serie`; the from serie defaults to
`serie - 1` (`--from-serie 12` starts a multi-serie span).

Non core (OCA / custom project) addons are analysed **on the fly** from
their local clone as soon as the requested addon is not a core addon of
the core addons dirs: pass `--odoo-cfg <config>` or
`--addons-path <comma,separated,list>`, or let the env be auto-detected
from the addon location under `$ODOO_PARENT_HOME`. The clone is located
with manifestoo (the same mechanism as akaidoo) and nothing is written
to the analysis repo. The analysis combines:

* a **tree diff** of the model/wizard code (`models/`, `wizard/`,
  `wizards/`) between the tips of the two serie branches
  (`origin/<from>.0` and `origin/<to>.0`), read directly from the git
  refs — the clones are never checked out. Unlike a commit scan, a
  change backported to BOTH series simply cancels out of the diff, and
  OCA branch rewrites (squash merges, bot commits) do not matter. The
  diff is then sliced: only the hunks carrying field declarations
  (` = fields.`), class-level method definitions (4-space indented
  `def `) and model anchors (`class ...(Model)`, `_inherit` changes)
  are kept, so it stays small enough for an LLM context. A
  `method_signatures.patch` style delta (before/after `def` lines, no
  commit annotations) completes it;
* the **milestone commits** (serie migrations, minor version bumps,
  `<addon>/migrations/` scripts) resolved to their GitHub Pull Requests
  — kept as "elements to investigate", since the PR descriptions are
  where OCA addons document the breaking changes. The change commits
  listed are the ones AFTER the `[MIG]` commit of the target serie
  (the older-series history is dropped), plus the post-milestone work
  at the serie tip. Set `$GITHUB_TOKEN` for a higher API rate limit.

With `--from-serie <n>`, the external addon analysis becomes a
**multi-serie span**: one section per serie step (e.g. `--from-serie 16`
with target serie `18` yields the 16->17 and 17->18 steps), each with
its own milestones, tree diff and signature delta. The steps read the
`origin/<serie>.0` branches of the addon repository (fetched when
needed; the checkout is never moved). A step whose `<serie>.0` branch is
missing from the addon repo is skipped with a warning (fetch the branch
to enable it).

The milestone commits themselves are also documented: the `[MIG]`
commits with their diffstats and the per-commit PR references — that is
the ported migration work an agent adapting custom code should mirror
(their code changes are covered by the tree diff above, so no separate
MIG-commit diff is dumped).

When the addon does not exist at `origin/<to>.0` (not migrated upstream
yet), the AFTER side of the tree diff falls back to the local working
tree, but only when the local clone is checked out on that very serie
(migration in progress). If the checkout sits on another serie, the tool
reports "not migrated to `<serie>.0` yet" rather than diffing against
code that belongs to a different serie.

## Planning a migration (--plan)

`--plan` emits a machine-readable (JSON) migration plan for ONE serie
step, designed to be consumed by a migration agent (see the
`odoo-migration` agent skill):

```console
odoo-module-diff --addon l10n_br_account --odoo-cfg ~/DEV/odoo18/odoo.cfg 19 --plan -o plan.json
```

The JSON root carries `addon`, `from_serie`, `to_serie`,
`migration_order` (the closure in migration order) and `to_migrate`
(the subset still to migrate for this step, i.e. the sub-migration
tasks); `dependencies` holds one entry per closure addon. The closure is
resolved from the ORIGIN serie environment (so it works even when the
addon is not migrated yet) and sorted topologically (migration order).
Per dependency:

* `kind`: `core-ce` / `core-ee` / `oca` / `private`, plus
  `core-removed` for a dependency that was a core addon up to an earlier
  serie but no longer exists in the target one (a renamed or merged core
  addon, e.g. `web_editor` in 19.0 — the manifest dependency must be
  dropped or replaced; `target_status: removed-in-target` with a `hint`);
* `target_status`: `migrated` (present in the target serie branch) or
  `unmigrated`;
* `diff_dir`: the diff context — the shared analysis cache for core
  addons, an on-the-fly tree diff (written to the analysis dir) for
  OCA/custom addons;
* `oca_prs`: for unmigrated OCA addons, the candidate migration PRs
  found in the OCA repo (matched on the `mod:<addon>` + `series:<X>.0`
  labels, the `[MIG]` title or the `<X>.0-mig-<addon>` branch), each
  enriched with its CI status (coverage-only failures tolerated), its
  approval count and a verdict: `credible` (green CI, 2+ approvals),
  `promising` (green CI), `partial` (CI red beyond coverage — a
  starting point only), `stale` (closed unmerged), `merged` (the module
  is already available in the target serie branch or via pip: the PR
  branch is not needed). Without a GitHub token the lookup is reported
  as `unavailable` and never fails the plan (`--no-pr-check` skips it).

Options: `--from-serie <n>` overrides the origin serie (default: target
- 1); `--origin-addons-path <dir>` takes the BEFORE code of external
addons from a directory as-is (no branch fetch) — for modules that
moved repos or when an older version than the origin serie tip is the
reference.

`--odoo-cfg` may be omitted: the env holding the addon is auto-detected
under `$ODOO_PARENT_HOME` (nearest serie wins), falling back to the
env of the origin serie for core addons — so a plan can be run without
knowing which env to point at (the chosen config is echoed as a NOTE).

When the target module is not in the origin serie addons path yet (its
migration has not landed), the closure falls back to the clone of the
addon found in another serie env on disk (the nearest one wins) and the
plan carries a `closure_source_note` recalling it — so a plan is always
possible, even for the second serie step of a two-step migration.

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
usage section above (`--odoo-cfg` / `--addons-path`, or the env
auto-detected under `$ODOO_PARENT_HOME`).

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
* Analysing **non core (OCA / custom) addons** from their local clone: a
  read-only tree diff of `models/` + `wizard(s)/` between the two serie
  branch tips (no checkout needed), a method signatures delta, and the
  milestone commits resolved to their GitHub Pull Requests. No
  commit-by-commit scanning, so a change backported to several series
  cancels out of the diff and OCA branch rewrites do not matter.
* Classifying every addon of the dependency closure (`core-ce`,
  `core-ee`, `core-removed`, `oca`, `private`) from manifestoo plus the
  git origin, and discovering the existing OCA migration Pull Requests
  of the unmigrated dependencies (labels, `[MIG]` titles, CI status,
  approvals) with a credibility verdict.
* Emitting a machine-readable migration plan (`--plan`, JSON) for one
  serie step: the topologically sorted dependency closure with, per
  addon, its kind, migration status, diff context and candidate PRs —
  the orchestration data an agent (see the `odoo-migration` skill) uses
  to plan the recursive sub-migrations.
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
* `--with-dependencies`: also dump the migration contexts of the
  transitive dependency closure. For a **non core** addon, one file per
  closure addon is written into a `<output>_deps/` directory next to the
  `-o` file, named `<addon>_migration_context.md`, in topological order
  (dependencies first, target addon last); the non core addons are
  analysed on the fly, the core ones are skipped with a NOTE pointing at
  their analysis cache directory. The single aggregated document is then
  NOT written — the `-o` name only sets the directory base (the console
  log says so). For a **core** addon the closure is appended to the
  single `-o` document instead (`## ADDON: <name>` sections,
  dependencies first), its size bounded by `--max-bytes`. One file per
  addon keeps every document small enough for an LLM agent to load
  individually, in migration order.
* `--with-external`: also analyse the external (OCA/custom) addons of
  the dependency chain in their own git repositories (e.g.
  `external-src/l10n-brazil`), instead of reporting them as missing.
* `--from-serie <n>`: build a multi-serie span (one context section per
  serie step, e.g. `--from-serie 12 --to-serie 19` yields the 12->13,
  ..., 18->19 steps). Steps are read from the analysis home; missing
  serie steps are reported as missing (never scanned in context mode).
  `--max-bytes-per-step` bounds each step independently.

## Example

[Here is a systematic commit analysis between the different Odoo series using odoo-module-diff](https://github.com/akretion/odoo-module-diff-analysis)
