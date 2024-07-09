# Dude, what did they do to my Odoo at version X? Find out with odoo-module-diff!

<!--- shortdesc-begin -->

A CLI tool to extract key commits impacting database migration between [Odoo](https://odoo.com) series.

<!--- shortdesc-end -->

## Installation

<!--- install-begin -->

```console
pip install odoo-module-diff
```

## Features

<!--- features-begin -->

`odoo-module-diff` provides the following features:

* Extracting the relevant key commits impacting database migration out of the bugfix and gimmick commit noise. Indeed less than 1 commit in 50 actually impacts anything for the database migration.
* Scanming all the repo addons or only a specific addon.
* Listing the key commits, addon by addon and with a 'heat' in the name (+/-/#) to explicit how much the commit added lines with `= fields.`  `_inherit = ` or `_inherits = `, removed such lines or how large is the diff in general so you can see at a glance what are the most impacting commits for a given addon migration.
* The idea is to help people doing the OCA/OpenUpgrade scripts, and eventually integrate the odoo-module-diff analysis files with the standard OpenUpgrade analysis files. But it will help you to migrate your modules in general or to help you find out what are the benefits and pitfalls to migrate to version X for module Y.
* Eventually it could complete the existing OCA/OpenUpgrade analysis files to provide a more complete learning dataset to train LLM models to write OpenUprade migration scripts (I don't expect AI to write more than the half of the easiest scripts, but that could still be a win).
