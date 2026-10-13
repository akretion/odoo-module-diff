"""Classify an addon as core (CE/EE), OCA, or private/custom.

Core detection reuses manifestoo_core's bundled per-serie addon lists
(``manifestoo_core.core_addons``). OCA detection is a two-signal
heuristic:

- primary: the git origin URL of the addon's repository points at the
  ``github.com/OCA`` organization (also yields the org/repo slug, which
  the OCA PR discovery needs);
- fallback: the manifest ``author`` key contains the canonical
  ``Odoo Community Association (OCA)`` string (used when the repo has
  no usable remote, e.g. an offline copy).

Anything that is neither core nor OCA is reported as ``private`` (the
customer-project case).
"""

import re
from pathlib import Path
from typing import Optional, Tuple

# manifestoo only knows released series (last supported one):
MAX_MANIFESTOO_SERIE = 19

OCA_AUTHOR_MARK = "Odoo Community Association (OCA)"

GITHUB_URL_RE = re.compile(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$")

KIND_CORE_CE = "core-ce"
KIND_CORE_EE = "core-ee"
KIND_CORE_REMOVED = "core-removed"
KIND_OCA = "oca"
KIND_PRIVATE = "private"


def manifestoo_serie(target_serie: int) -> str:
    """The serie label manifestoo knows about: fall back to the last
    supported one for unreleased series (same convention as
    dependencies.py)."""
    serie = target_serie if target_serie <= MAX_MANIFESTOO_SERIE else MAX_MANIFESTOO_SERIE
    return f"{serie}.0"


def is_core_addon(addon: str, target_serie: int) -> Tuple[bool, str]:
    """Is the addon part of the Odoo core for the serie? Returns
    (is_core, kind) with kind in {core-ce, core-ee}."""
    from manifestoo_core.core_addons import is_core_ce_addon, is_core_ee_addon
    from manifestoo_core.odoo_series import OdooSeries

    serie = OdooSeries(manifestoo_serie(target_serie))
    if is_core_ce_addon(addon, serie):
        return True, KIND_CORE_CE
    if is_core_ee_addon(addon, serie):
        return True, KIND_CORE_EE
    return False, ""


def oca_slug_from_url(url: str) -> Optional[str]:
    """'OCA/l10n-brazil' slug when the URL points at the OCA GitHub
    organization, else None."""
    match = GITHUB_URL_RE.search(url or "")
    if not match:
        return None
    slug = match.group("slug")
    org = slug.split("/", 1)[0]
    return slug if org.lower() == "oca" else None


def github_slug_from_url(url: str) -> Optional[str]:
    """'org/repo' slug of any GitHub URL, else None."""
    match = GITHUB_URL_RE.search(url or "")
    return match.group("slug") if match else None


def repo_origin_url(repo_path: str) -> str:
    """The origin remote URL of a git repo ("" when none)."""
    import git

    try:
        return git.Repo(repo_path).remote("origin").url
    except Exception:
        return ""


def manifest_author(addon_path: Path) -> str:
    """The manifest author key, without importing Odoo."""
    manifest = addon_path / "__manifest__.py"
    if not manifest.is_file():
        return ""
    content = manifest.read_text(errors="ignore")
    match = re.search(
        r"""["']author["']\s*:\s*["']([^"']+)["']""", content
    )
    return match.group(1) if match else ""


def classify_addon(
    addon: str,
    target_serie: int,
    addon_path: Optional[Path] = None,
    repo_path: Optional[str] = None,
    from_serie: int = 0,
) -> dict:
    """Classify an addon. Returns::

        {
            "addon": "l10n_br_fiscal",
            "kind": "core-ce" | "core-ee" | "core-removed" | "oca"
                    | "private",
            "oca_repo": "OCA/l10n-brazil" | None,   # GitHub slug when OCA
            "github_repo": "org/repo" | None,        # any GitHub slug
            "detection": "core-list" | "core-list-from-serie"
                         | "git-origin" | "manifest-author" | "",
        }

    ``core-removed`` is reported for an addon that was part of the core
    up to ``from_serie`` but no longer exists in ``target_serie`` (a
    renamed or merged core addon, e.g. ``web_editor`` in 19.0): the
    manifest dependency must be dropped or replaced.

    ``addon_path``/``repo_path`` are only needed for non-core addons:
    without them the OCA/private distinction cannot be made and the
    kind is reported as ``private`` with an empty detection.
    """
    core, core_kind = is_core_addon(addon, target_serie)
    if core:
        return {
            "addon": addon,
            "kind": core_kind,
            "oca_repo": None,
            "github_repo": None,
            "detection": "core-list",
        }

    if from_serie and from_serie != target_serie:
        was_core, _was_kind = is_core_addon(addon, from_serie)
        if was_core:
            return {
                "addon": addon,
                "kind": KIND_CORE_REMOVED,
                "oca_repo": None,
                "github_repo": None,
                "detection": "core-list-from-serie",
                "core_upto_serie": from_serie,
            }

    if addon_path is None:
        # not on disk: check whether it was a core addon of a not so
        # ancient serie (a manifest may still depend on it long after
        # it was merged into another core addon, e.g. web_editor into
        # html_editor in 19.0)
        for serie in range(target_serie - 1, max(target_serie - 6, 8), -1):
            was_core, _was_kind = is_core_addon(addon, serie)
            if was_core:
                return {
                    "addon": addon,
                    "kind": KIND_CORE_REMOVED,
                    "oca_repo": None,
                    "github_repo": None,
                    "detection": "core-list-from-serie",
                    "core_upto_serie": serie,
                }

    origin = repo_origin_url(repo_path) if repo_path else ""
    slug = github_slug_from_url(origin)
    oca_slug = oca_slug_from_url(origin)
    if oca_slug:
        return {
            "addon": addon,
            "kind": KIND_OCA,
            "oca_repo": oca_slug,
            "github_repo": slug,
            "detection": "git-origin",
        }
    if addon_path and OCA_AUTHOR_MARK in manifest_author(Path(addon_path)):
        return {
            "addon": addon,
            "kind": KIND_OCA,
            "oca_repo": slug,  # unknown org layout; keep whatever slug we have
            "github_repo": slug,
            "detection": "manifest-author",
        }
    return {
        "addon": addon,
        "kind": KIND_PRIVATE,
        "oca_repo": None,
        "github_repo": slug,
        "detection": "git-origin" if slug else "",
    }
