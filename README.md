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

## The shared analysis home (read this first!)

Scanning all the Odoo commits of a serie from scratch is **extremely slow**
(hours per serie on a laptop). The pseudo patches of every serie and every
addon are therefore shared through the
[odoo-module-diff-analysis](https://github.com/akretion/odoo-module-diff-analysis)
repository, and all the cache modes of this tool (`--from-analysis`,
`--addon-readmes`, `--from-serie/--to-serie`) read from that local clone.

Recommended setup for anyone using the tool:

```console
git clone https://github.com/akretion/odoo-module-diff-analysis
export ODOO_MODULE_DIFF_HOME=/path/to/odoo-module-diff-analysis
```

Resolution order of the analysis home (the dir holding one `<serie>.0/`
analysis dir per serie):

1. the explicit `--from-analysis <dir>` option when given;
2. `$ODOO_MODULE_DIFF_HOME` (recommended), falling back to the legacy
   `$ODOO_MODULE_DIFF_ANALYSIS` alias;
3. the default `~/DEV/odoo-module-diff-analysis`.

When no analysis home is configured (3), the tool prints a warning
suggesting the clone: rescan from git only when you really mean it (a
fresh serie scan is what maintains the shared repository).

## Features

<!--- features-begin -->

`odoo-module-diff` provides the following features:

* Extracting the relevant key commits impacting database migration out of the bugfix and gimmick commit noise. Indeed less than 1 commit in 50 actually impacts anything for the database migration.
* Scanning all the repo addons or only a specific addon.
* Listing the key commits, addon by addon and with a 'heat' in the name (+/-/#) to explicit how much the commit added lines with `= fields.|_inherit = |_inherits = `, removed such lines or how large is the diff in general so you can see at a glance what are the most impacting commits for a given addon migration.
* Generating a per-serie `README.md` ranking the most impacted addons (a
  "Dude, what did they do to my Odoo" digest, regenerated on every clean
  whole-serie scan) and, per addon, an LLM written `README.md` migration
  guide for the 30 most impacted addons (see `--addon-readmes`).
* The idea is to help people doing the OCA/OpenUpgrade scripts, and eventually integrate the odoo-module-diff analysis files with the standard OpenUpgrade analysis files. But it will help you to migrate your modules in general or help you find out what are the benefits and pitfalls to migrate to version X for module Y.
* Aggregating the per-addon analysis files into one AI-agent-sized
  migration context (`--dump-context`, `--with-dependencies`,
  `--from-serie/--to-serie`, `--max-bytes`), and generating customer
  facing per-addon migration READMEs from the pseudo patches plus the
  official Odoo release notes (`--addon-readme`/`--addon-readmes`).
* What about xml_id changes? Well these would be harder to track in commit diffs. However, they are very well detected by the standard OCA/OpenUpgrade analysis tool and it's usually easy to accomodate for xml_id changes. If not then, it's likely the change will be part of the larger commit tracked by odoo-module-diff.
* Eventually it could complete the existing OCA/OpenUpgrade analysis files to provide a more complete learning dataset to train LLM models to write OpenUprade migration scripts (I don't expect AI to write more than the half of the easiest scripts, but that could still be a win, in the future I mean).


## Usage

```console
odoo-module-diff <path_to_odoo_repo> <target_serie> [options]
```

The scan writes one pseudo patch file per key commit into
`<output-dir>/<addon>/`, plus a `method_signatures.patch` per addon (the
method signature deltas, annotated with commit SHAs/PRs). The tool
compares the target serie against the previous one (e.g. serie `19`
compares the merge base of `19.0` up to the `[REL] 19.0` release commit).

### Options by workflow

**Scan mode (git scan; the slow path - see the shared analysis home
above):**

* `--addon <addon>`: scan a single addon instead of all of them.
* `--output-dir <dir>`: where to write the analysis files (default
  `module_diff_analysis/<serie>.0`).
* `--commit <sha>`: only analyse a single commit (and its diff from its
  first parent) instead of the whole serie range.
* `--dump-dependencies`: also write a manifestoo dependency tree per addon.
* `--keep-noise`: keep the commits detected as noise (grey results) too.
* `--continue`: incremental re-scan of an already analysed serie: resume
  after the newest pseudo commit already present (a `From:` SHA of the
  existing patches) instead of rescanning everything. Fetches the serie
  branch first (`--no-fetch` to skip). Never combine with `--addon` (the
  resume point is global to the serie).

**Cache mode (no git scan; the fast path):**

* `--from-analysis <dir>`: aggregate the existing analysis files of
  `<dir>/<serie>.0` (or of the given serie dir itself) into one migration
  context: a ready-to-use prompt for AI agents. Implies `--dump-context`.
  Combine with `--addon`, `--output-file`, `--stdout`, `--max-bytes` and
  `--with-dependencies`. When omitted, the analysis home is resolved as
  described above.
* `--from-serie <n> --to-serie <m>`: with `--addon`, build a multi-serie
  migration context decomposed into one section per serie step (e.g.
  12.0 -> 18.0 yields the 12->13, 13->14, ..., 17->18 steps). Steps are
  read from the analysis home; missing serie steps are scanned on demand
  into it (incremental cache fill) using the shared Odoo repository
  (`$ODOO_MODULE_DIFF_REPO`, default `~/DEV/odoo.git`).
* `--max-bytes-per-step <n>`: size budget per serie step in span mode.
* `--addon-readme <addon>`: write the LLM migration README of one addon
  from its cached analysis (no scan).
* `--addon-readmes`: same, for the 30 addons with the most changes (same
  ranking as the per-serie README).

**Context/README output tuning (both modes):**

* `--output-file <path>`: where to write the aggregated context (default:
  `<analysis_dir>/migration_context.md`).
* `--stdout`: stream the aggregated context to stdout (logs go to stderr).
* `--max-bytes <n>`: size budget for the aggregated context; the method
  signatures patch and the structural patches are included first (noise
  last) and skipped files are listed at the end of the document.
* `--with-dependencies`: with `--addon`, resolve the transitive
  dependencies with manifestoo and aggregate them first, in topological
  (migration) order: core addons come from the analysis files (missing
  ones are marked `[MISSING]`), while OCA/external dependencies are not
  scanned yet (they will appear as `[MISSING]` placeholders). The
  addons-path is auto-detected from `$ODOO_PARENT_HOME/odoo<serie>/odoo/`
  (core `src/addons`, `src/odoo/addons` and every `external-src/*` repo),
  from the given repo worktree, or from the matching worktree of a shared
  repository.
* `--with-external`: with `--with-dependencies`, additionally scan the
  external (OCA/custom) addons of the dependency chain on the fly, in
  their own git repositories (e.g. `external-src/l10n-brazil`), instead
  of reporting them as missing. External addons are located in the
  `external-src` dirs of the serie environment and scanned with the merge
  base of the two serie version branches as start boundary; when the
  target serie branch was recreated with unrelated history (e.g. the OCA
  19.0 branch recreation), the previous serie branch tip is used instead.

**Advanced/rarely needed:**

* `--dump-context`: force the context aggregation after a fresh scan
  (implied by `--from-analysis`).
* `--dump-methods / --no-dump-methods`: disable the method signatures
  patch generation (on by default).
* `--bypass-structural-scan`: skip the structural commit detection
  (used internally to produce the method signature delta of an addon
  without structural changes).
* `--no-wrap-serie-dir`: do not append `<serie>.0` to `--output-dir`.
* `--skip-addon-readmes`: on a whole-serie scan, skip the per-addon
  README generation (they are LLM calls, so this also saves API cost).

Examples:

```console
# aggregate the cached 19.0 analysis of the account addon into one context file
odoo-module-diff --from-analysis ~/DEV/odoo-module-diff-analysis/19.0 --addon account

# scan the future 20.0 serie and stream a context under 100 KB for an agent prompt
odoo-module-diff ~/DEV/odoo.git 20 --addon account --dump-context --stdout --max-bytes 100000 > context.md

# (re)generate the LLM migration READMEs of the top 30 addons of serie 19.0, no scan
odoo-module-diff --addon-readmes --from-analysis ~/DEV/odoo-module-diff-analysis/19.0

# incremental re-scan of the moving 20.0/master serie after a first full scan
odoo-module-diff --continue ~/DEV/odoo.git 20
```

### LLM configuration (per addon READMEs)

The per-addon README summaries call an LLM through the `openai` SDK (loose
dependency, `pip install odoo-module-diff[llm]`; a stdlib fallback is used
when it is missing):

* model: `deepseek/deepseek-flash` by default, override with
  `$ODOO_MODULE_DIFF_LLM_MODEL` (format `provider/model`).
* API key: `$<PROVIDER>_API_KEY` (e.g. `DEEPSEEK_API_KEY`), with a
  `~/.hermes/.env` fallback.
* The official Odoo release notes (`https://www.odoo.com/odoo-<serie>-release-notes`,
  series 16 and up) are downloaded once per serie as
  `<serie>.0/RELEASE_NOTE.md` and used to make the summaries more user
  focused; only the parts actually matching the addon are used (the
  release notes cover Enterprise features Akretion does not ship).
  Without release notes (serie 20 and below 16, or offline), the pseudo
  patches only are used.

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
