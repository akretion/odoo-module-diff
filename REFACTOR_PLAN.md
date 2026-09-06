# Refactoring & Feature Expansion Plan: `odoo-module-diff`

> **Note for AI Agents & Developers**: This document outlines the background, user goals, detailed specifications (Points A–F), architectural refactoring, and step-by-step implementation plan for extending `odoo-module-diff` to support advanced automated Odoo module migrations for AI LLMs.

---

## 1. User Prompt & Goal Summary

### Objective
`odoo-module-diff` extracts key database structure changes between Odoo series into patch files per addon. The objective is to expand `odoo-module-diff` into a complete **LLM Dataset & Migration Context Generator** for AI agents (and human developers) migrating custom/OCA Odoo modules across Odoo series.

### Key Capabilities Required:
1. **Method Signatures Delta (`method_signatures.patch`)**: Extract method signature additions, deletions, and modifications between series.
   - Grouped by Odoo technical model ID (`_name` or `_inherit`, e.g., `[account.move]`, `[res.company]`).
   - Move-invariant: Methods moved across lines or files without signature changes are ignored.
   - Annotated with commit SHA1s, PR URLs, authors, and commit summaries (supporting multi-commit history).
2. **Single LLM Context Export**: Option to aggregate all pseudo patch files (structural patches + `method_signatures.patch`) and dependency graphs into a single file or stream to `stdout`.
3. **Dependency Expansion via Manifestoo (`--with-dependencies`)**: Automatically resolve and include pseudo-patches for all module dependencies.
4. **Core Analysis Caching**: Reuse pre-computed analysis files in `module_diff_analysis/<serie>/<addon>/` to prevent redundant git log scanning of large core repos.
5. **External / OCA Addon Support**: Locate non-core addons in `$ODOO_PARENT_HOME/odoo<ver>/odoo/external-src/` or `local-src/`, detect their Git repositories, and perform on-the-fly commit/AST analysis.
6. **Multi-Serie Version Spans**: Support migration spans across multiple series (e.g., Odoo 12.0 to 18.0) by processing sequential series pairs ($12\rightarrow13\rightarrow\dots\rightarrow18$) and concatenating the migration path.
7. **Zero Breaking Changes**: Retain 100% backward compatibility for standard CLI invocations (`odoo-module-diff <repo_path> <target_serie>`).

---

## 2. Detailed Specifications (Points A – F)

### Point A: Single LLM Context Aggregation (`--dump-context`, `--output-file`, `--stdout`)
- **Problem**: Individual `.patch` files are useful for browsing, but an AI agent migrating a module needs a single concatenated context prompt.
- **Specification**:
  - Add `--dump-context` flag.
  - Add `--output-file <path>` (e.g. `migration_context.md`) and `--stdout` options.
  - Aggregates dependency tree metadata, method signature patches, and structural patch files into a structured markdown document optimized for LLM prompts.

### Point B: Dependency Expansion (`--with-dependencies` via Manifestoo)
- **Problem**: Migrating a custom module (e.g., `account_financial_report`) requires knowing breaking changes in `account`, `base`, `account_accountant`, etc.
- **Specification**:
  - Add `--with-dependencies` flag.
  - Runs `manifestoo --addons-path <path> --odoo-series <serie> --select <addon> tree`.
  - Topologically sorts dependencies (e.g. `[base, account, account_financial_report]`).
  - Processes each dependency in order and appends its diff dataset to the context.

### Point C: Core Analysis Caching (`cache_manager`)
- **Problem**: Scanning 1,000+ commits on core Odoo repositories (`odoo.git`) for large addons like `account` or `base` takes time.
- **Specification**:
  - Implement an analysis cache manager.
  - If `module_diff_analysis/<serie>/<addon>/` exists and contains valid `.patch` files, load them directly from disk.
  - Bypasses git commit iteration for core addons when cached datasets are present.

### Point D: Non-Core / External & OCA Addons Support (`addon_locator`)
- **Problem**: Custom or OCA addons live outside `odoo.git` in directories like `$ODOO_PARENT_HOME/odoo<ver>/odoo/external-src/<repo>/<addon>`.
- **Specification**:
  - Implement an addon locator module.
  - Resolves addon paths across `addons/`, `odoo/addons/base/`, `external-src/*/<addon>`, and `local-src/<addon>`.
  - Detects underlying Git repositories (`git.Repo(..., search_parent_directories=True)`).
  - Determines available target branches/tags (e.g. `16.0..18.0`) and runs diff/AST analysis on the fly.

### Point E: Multi-Serie Span Analysis (`--from-serie`, `--to-serie`)
- **Problem**: Legacy modules are often migrated across multiple versions at once (e.g. Odoo 12.0 to 18.0).
- **Specification**:
  - Add `--from-serie <ver>` and `--to-serie <ver>` flags.
  - Breaks the range into step pairs:
    $$\text{Span} = \{(12.0, 13.0), (13.0, 14.0), (14.0, 15.0), (15.0, 16.0), (16.0, 17.0), (17.0, 18.0)\}$$
  - For each step pair, executes the diff scan (checking cache first) and concatenates the step results chronologically.

### Point F: Backward Compatibility Enforcement
- **Specification**:
  - The default CLI command remains:
    ```bash
    odoo-module-diff ~/DEV/odoo.git 19.0 --addon account
    ```
  - All new options (`--dump-context`, `--with-dependencies`, `--from-serie`, `--stdout`) are strictly optional flags.

---

## 3. Package Architecture & Directory Structure

Refactor `odoo-module-diff` into clean, decoupled Python modules:

```
odoo_module_diff/
├── __init__.py
├── main.py                  # Typer CLI entrypoint & subcommands
├── core/
│   ├── git_helper.py        # Git repo, worktree, and branch/tag resolution
│   ├── commit_scanner.py    # Structural diff scanner & commit scoring heuristics
│   └── method_diff.py       # AST method signature extraction & model ID grouping
├── dependency/
│   └── resolver.py          # Manifestoo tree resolution & topological dependency ordering
├── repository/
│   └── addon_locator.py     # Discovers Core vs OCA/External module git paths
├── exporter/
│   ├── cache_manager.py     # Reuses pre-computed module_diff_analysis files
│   └── llm_context.py       # Formats markdown context prompts for LLMs
└── span/
    └── span_scanner.py      # Multi-version range scanner (12.0 -> 18.0)
```

---

## 4. Detailed Component Specifications

### 4.1 `odoo_module_diff/core/method_diff.py` (Method Signatures Delta Engine)
- **Model Identifier Extraction**: AST visitor inspects Python class bodies for `_name = '...'` or `_inherit = '...'` (falling back to `class_name`).
- **Signature Formatting**: `format_signature()` converts AST function nodes to normalized `def method_name(...)` strings.
- **Move Invariance**: Compares method dictionaries keyed by `(ModelID, MethodName)`. Identical signatures are omitted even if relocated across lines/files.
- **Multi-Commit Tracking**: `build_commit_map_from_repo()` scans commits in `start_commit..end_commit` for the addon path, mapping method names to all commit SHAs, PR URLs, authors, and summaries.

### 4.2 `odoo_module_diff/dependency/resolver.py` (Manifestoo Dependency Resolver)
- Executes `manifestoo` to extract the dependency tree.
- Returns topologically sorted list of dependencies.

### 4.3 `odoo_module_diff/repository/addon_locator.py` (Addon & Repo Locator)
- Checks:
  1. `odoo/addons/base/models`
  2. `addons/<addon>/models`
  3. `$ODOO_PARENT_HOME/odoo<ver>/odoo/external-src/*/<addon>`
  4. `$ODOO_PARENT_HOME/odoo<ver>/odoo/local-src/<addon>`
- Returns physical filesystem path, parent `git.Repo` instance, and category (`core` vs `external`).

### 4.4 `odoo_module_diff/exporter/cache_manager.py` (Analysis Cache Manager)
- Checks `module_diff_analysis/<serie>/<addon>/`.
- If `.patch` files exist, parses and loads them into memory structure without re-scanning Git history.

### 4.5 `odoo_module_diff/exporter/llm_context.py` (LLM Prompt Exporter)
- Formats single LLM context document:

```markdown
# ODOO MODULE MIGRATION CONTEXT

## Target Addon: account_financial_report
## Migration Span: 16.0 -> 18.0
## Dependency Tree: base -> account -> account_financial_report

================================================================================
### DEPENDENCY: base (16.0 -> 17.0) [CACHED]
================================================================================
[method_signatures.patch & structural patches]

================================================================================
### DEPENDENCY: account (16.0 -> 17.0) [CACHED]
================================================================================
[method_signatures.patch & structural patches]

================================================================================
### TARGET ADDON: account_financial_report (16.0 -> 18.0)
================================================================================
[method_signatures.patch & structural patches]
```

---

## 5. CLI Extensions & Command Examples

```bash
# 1. Standard usage (unchanged)
odoo-module-diff ~/DEV/odoo.git 19.0 --addon account

# 2. Fast prototyping for method signatures patch only
odoo-module-diff ~/DEV/odoo.git 19.0 --addon account --bypass-structural-scan

# 3. Export single LLM context file for an addon
odoo-module-diff ~/DEV/odoo.git 19.0 --addon account --dump-context --output-file account_19_context.md

# 4. Stream LLM context to stdout
odoo-module-diff ~/DEV/odoo.git 19.0 --addon account --dump-context --stdout

# 5. Include all module dependencies (via Manifestoo)
odoo-module-diff ~/DEV/odoo.git 19.0 --addon account_financial_report --with-dependencies --dump-context

# 6. Multi-serie version span (e.g. Odoo 12.0 to 18.0)
odoo-module-diff ~/DEV/odoo.git 18.0 --addon account --from-serie 12.0 --to-serie 18.0 --dump-context
```

---

## 6. Phased Implementation Roadmap

- [x] **Phase 0: Prototyping & Core Method Delta Engine** (Completed)
  - Implemented AST method extraction with `_name` / `_inherit` model ID grouping.
  - Implemented move-invariance logic and multi-commit SHA1 annotations.
  - Verified on `account` (18.0 $\rightarrow$ 19.0) producing `method_signatures.patch`.

- [x] **Phase 1b: `--dump-context` aggregation (Chunk 1)** (Implemented — NOT the Phase 1 package split below)
  - New `odoo_module_diff/context.py`: aggregates `method_signatures.patch`,
    structural `*.patch` (noise ranked last), `dependencies.txt` and missing
    addon notices into a single markdown `migration_context.md`.
  - New CLI options: `--dump-context`, `--from-analysis` (cache mode, no git),
    `--output-file`, `--stdout`, `--max-bytes` (size budget with a skip list).
  - Serie inferred from the analysis dir name in cache mode; `--stdout`
    keeps logs on stderr. Backward compatible CLI (positional args kept).
  - NOTE: the original "Phase 1: Package Refactoring" below was rejected —
    the current 2-module layout is kept on purpose (see session discussion).

- [x] **Phase 3b: `--with-dependencies` (Chunk 2)** (Implemented)
  - New `odoo_module_diff/dependencies.py`: manifestoo
    `list-depends --transitive --include-selected` then `list --sort
    topological` over the closure (dependencies first, addon last).
  - addons-path auto-detection: `$ODOO_PARENT_HOME/odoo<serie>/odoo/`
    layout (core `src/addons` + `src/odoo/addons` + every `external-src/*`
    repo holding addons), any worktree, or the matching worktree of a
    shared repository (serie > 19 falls back to the master worktree).
  - Aggregated contexts list dependencies in migration order; deps without
    analysis files (e.g. OCA/external addons, or core addons with no
    structural change in the cache) appear as `[MISSING]` notices.
  - Works in both `--from-analysis` cache mode and fresh scan mode.

- [x] **Phase 3c: `--with-external` external/OCA addons (Chunk 3)** (Implemented)
  - New `odoo_module_diff/external_addons.py`: locates external addons in
    the `external-src` dirs of the serie env, resolves the scan boundary
    from the merge base of the two serie version branches (with a
    recreated-branch fallback to the previous serie tip, e.g. OCA 19.0),
    and reuses `scan_addon_commits` with a per-repo `module_prefix`
    (external repos have `addon/models/` at their root, no `addons/` prefix).
  - `--with-external` scans the external addons of the `--with-dependencies`
    chain on the fly (the target addon itself is scanned externally too when
    it is not a core addon) and feeds their analysis into the context.
  - `context.py` accepts an `addons_dirs` override so external addon files
    aggregate seamlessly into the single migration context.

- [ ] **Phase 1: Package Refactoring**
  - Extract helper modules (`core/git_helper.py`, `core/commit_scanner.py`, `core/method_diff.py`).
  - Maintain `main.py` entrypoint.

- [ ] **Phase 2: LLM Context Exporter & Cache Manager**
  - Implement `exporter/cache_manager.py` and `exporter/llm_context.py`.
  - Add `--dump-context`, `--output-file`, and `--stdout` CLI options.

- [ ] **Phase 3: Dependency Expansion & External Addon Support**
  - Implement `dependency/resolver.py` (manifestoo) and `repository/addon_locator.py`.
  - Add `--with-dependencies` CLI flag.

- [ ] **Phase 4: Multi-Serie Span Scanner**
  - Implement `span/span_scanner.py`.
  - Add `--from-serie` and `--to-serie` CLI options.
