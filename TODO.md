# odoo-module-diff — TODO

User feedback and future work items (2026-09, after the external addon
milestone analysis work).

## Hard / later

### 1. Parallel evolution of OCA modules across series

When migrating a module depending on an OCA addon from serie n to n+1,
the relevant change set is NOT the full aggregation of the big breaking
changes collected on the target serie branch. OCA backports the same
kind of changes to previous serie branches in parallel (fast moving
repos like OCA/l10n-brazil do this constantly). So the milestone context
of the target serie branch overstates what the migration will actually
face.

Idea to explore later: diff the milestone change sets of serie n and
serie n+1 branches and keep only what is NEW in n+1 (or what was
backported differently), possibly by matching PR numbers/titles across
branches.

## Quick fixes (from the same review session)

### 2. External addon context should contain the real pseudo diffs

The external milestone context currently reads like a `git status`
overview (diffstat + commit lists + PR descriptions). It should include
the actual pseudo patches (the structural `-/+` field matches, method
signature deltas...) like the core addon `--dump-context` output does
(e.g. the 562933 bytes / 10k+ lines account context from
`--from-serie 18.0 --to-serie 19.0`).

Implementation sketch: after the milestone scan, run the regular
`scan_addon_commits` between the serie boundary commits in the external
repo (module_prefix="") and concatenate its pseudo patches + the
milestone/PR narratives, or convert the milestone commit diffs into
pseudo patch files on the fly with the same structural regexes.

### 3. Core addon passed with --odoo-cfg should not error

`odoo-module-diff --addon account --odoo-cfg ~/DEV/odoo19/odoo.cfg 19
--dump-context` currently errors with "account not found in the addons
paths (or it is a core addon...)" instead of transparently falling back
to the core scan path. Expected: auto-detect that the addon is core and
continue with the normal serie scan (the message is only acceptable as
a NOTE, not an Error).

### 4. Serie arguments consistency in context dump mode

`--from-serie 18.0 --to-serie 19.0` should not be required for the
context dump: the target serie should be found the same way as without
`--dump-context` (positional / analysis dir name), and the from-serie
should default to `target_serie - 1`. (Also the option values are
declared as floats but the docs show `18.0` style floats — keep a
single canonical integer form.)
