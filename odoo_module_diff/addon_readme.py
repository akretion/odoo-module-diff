"""Per-addon README.md summaries for a serie analysis.

For the addons with the most changes (same ranking as the per-serie
README), generate <serie>.0/<addon>/README.md: a customer-facing summary
of what changed between <prev>.0 and <serie>.0. The summary is written by
an LLM (litellm loose dependency, DeepSeek by default) from two sources:

- the addon pseudo patches (ground truth for the technical changes);
- the official Odoo release notes, downloaded from
  https://www.odoo.com/odoo-<serie>-release-notes (series 16 and up) and
  cached as <serie>.0/RELEASE_NOTE.md. Release notes are fuzzy and cover
  Enterprise features Akretion does not ship: only the parts matching the
  addon are pre-selected locally, and the LLM is instructed to ignore the
  rest.

Without litellm (or without an API key) the generation is skipped with a
warning and the pseudo patches only are left untouched.
"""

import html as html_mod
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

from odoo_module_diff.context import build_context

ADDON_README_LIMIT = 30
ADDON_README_MAX_CHARS = 4800
# prompt budget for the pseudo patches (methods first, then by heat,
# mirroring the migration context ordering)
ADDON_README_PATCH_BUDGET = 120_000
# max chars of release-note sections fed to the LLM per addon
ADDON_README_NOTES_BUDGET = 80_000
RELEASE_NOTES_FILENAME = "RELEASE_NOTE.md"
RELEASE_NOTES_URL = "https://www.odoo.com/odoo-{serie}-release-notes"
DEFAULT_LLM_MODEL = "deepseek/deepseek-flash"
LLM_MODEL_ENV = "ODOO_MODULE_DIFF_LLM_MODEL"
FALLBACK_NOTE = (
    "(no release note extract matched this addon: rely on the"
    " pseudo patches only)"
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _load_llm_api_key(provider: str) -> str:
    """API key for the LLM provider from the environment, with a
    ~/.hermes/.env fallback (same convention as akaidoo)."""
    var = f"{provider.upper()}_API_KEY"
    api_key = os.environ.get(var)
    if api_key:
        return api_key
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.is_file():
        with open(env_path) as f:
            for line in f:
                if line.startswith(f"{var}="):
                    return line.strip().split("=", 1)[1]
    return ""


def _strip_tag_by_class(html: str, class_hint: str) -> str:
    """Remove whole elements whose class contains class_hint (non nested
    safe enough for the odoo.com release notes markup)."""
    pattern = re.compile(
        r"<(\w+)[^>]*class=\"[^\"]*" + class_hint + r"[^\"]*\"[^>]*>.*?</\1>",
        re.S,
    )
    return pattern.sub(" ", html)


def html_to_markdown(html: str) -> str:
    """Convert the odoo.com release notes page to markdown-ish text,
    keeping the per-app h2 sections and the feature bullet lists."""
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    html = _strip_tag_by_class(html, "table_of_content")
    html = _strip_tag_by_class(html, "o_header_standard")
    html = _strip_tag_by_class(html, "o_footer")
    # headings and list items before the generic tag strip
    html = re.sub(r"<h1[^>]*>", "\n\n# ", html)
    html = re.sub(r"<h2[^>]*>", "\n\n## ", html)
    html = re.sub(r"<h3[^>]*>", "\n\n### ", html)
    html = re.sub(r"<h4[^>]*>", "\n\n#### ", html)
    html = re.sub(r"<li[^>]*>", "\n- ", html)
    html = re.sub(r"<(p|div|section|tr|br)[^>]*>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    text = html_mod.unescape(text)
    # emoji/flag characters trip DeepSeek's "Content Exists Risk" filter,
    # and they carry no information anyway: drop everything outside
    # reasonable printable text
    text = re.sub(
        r"[^\w\s.,:;()\[\]/'\"&+%€$#@*=<>\-!?~\n]", "", text
    )
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+\n", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # the odoo.com page embeds its navigation as long empty "- " bullet
    # lists before the content: cut everything before the h1 title
    title = text.find("\n# ")
    if title >= 0:
        text = text[title + 1 :]
    return text.strip() + "\n"


def ensure_release_notes(serie: int, serie_dir: str) -> Optional[Path]:
    """Download the official release notes for the serie into
    <serie_dir>/RELEASE_NOTE.md unless already present. Returns None when
    unavailable (no release notes published for that serie, e.g. 20, or
    offline)."""
    path = Path(serie_dir) / RELEASE_NOTES_FILENAME
    if path.is_file() and path.stat().st_size > 0:
        return path
    url = RELEASE_NOTES_URL.format(serie=serie)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as err:  # noqa: B902
        print(
            f"WARNING! No release notes fetched from {url} ({err}):"
            " the addon summaries will rely on the pseudo patches only."
        )
        return None
    if "<h2" not in raw:
        # 404 pages come back as 200 sometimes: sanity check the markup
        print(f"WARNING! {url} does not look like a release notes page.")
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_to_markdown(raw))
    print(f"Release notes saved to {path} ({path.stat().st_size} bytes)")
    return path


def load_release_note_sections(path: Path) -> List[Tuple[str, str]]:
    """Split the RELEASE_NOTE.md into (section title, section text)
    pairs, one per '## ' heading; returns [(release notes, whole text)]
    when no heading is found."""
    text = path.read_text(errors="ignore")
    parts = re.split(r"\n## ", "\n" + text)
    sections = []
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        sections.append((title.strip(), body.strip()))
    if not sections:
        return [("Release notes", text)]
    return sections


def _addon_keywords(addon: str, patch_text: str) -> List[str]:
    """Case-insensitive keywords used to score release-note sections for
    an addon: its own name, its underscore parts (long enough to be
    meaningful) and the Odoo model technical names mentioned in the
    patches (e.g. account.move -> also its 'account' prefix via the addon
    parts)."""
    keywords = {addon.lower()}
    for part in addon.lower().split("_"):
        if len(part) >= 4:
            keywords.add(part)
    # dotted model names from the patch (res.partner, account.move.line...)
    for match in re.findall(r"\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+\b", patch_text):
        keywords.add(match)
    return sorted(keywords)


def select_release_note_sections(
    sections: List[Tuple[str, str]], addon: str, patch_text: str
) -> str:
    """Keep the release-note sections plausibly related to the addon,
    scored by keyword hits, within ADDON_README_NOTES_BUDGET chars."""
    keywords = _addon_keywords(addon, patch_text)
    scored = []
    for title, body in sections:
        haystack = f"{title}\n{body}".lower()
        # count every keyword occurrence, longer keywords weigh more
        score = sum(haystack.count(k) * min(len(k), 12) for k in keywords)
        if score > 0:
            scored.append((score, title, body))
    scored.sort(reverse=True)
    kept = []
    used = 0
    for _score, title, body in scored:
        chunk = f"### Release notes section: {title}\n\n{body}\n"
        if used + len(chunk) > ADDON_README_NOTES_BUDGET:
            continue
        kept.append(chunk)
        used += len(chunk)
    if not kept:
        return ""
    return "\n".join(kept)


# provider -> OpenAI-compatible base URL, same trick Hermes uses: every
# provider speaks the OpenAI chat completions dialect
PROVIDER_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai": "https://api.openai.com/v1",
    # z.ai (Zhipu) serves an OpenAI compatible endpoint too
    "zai": "https://api.z.ai/api/paas/v4",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
}


def _parse_model(model: str) -> Tuple[str, str]:
    """Split 'provider/model' (default provider: deepseek)."""
    if "/" in model:
        provider, model_name = model.split("/", 1)
    else:
        provider, model_name = "deepseek", model
    return provider.lower(), model_name


def _call_llm_openai(provider: str, model_name: str, system: str, user: str) -> str:
    """Call the provider through the openai SDK (the same package Hermes
    uses for all its LLM backends, pure Python)."""
    from openai import OpenAI

    client = OpenAI(
        base_url=PROVIDER_BASE_URLS.get(provider, "https://api.deepseek.com/v1"),
        api_key=_load_llm_api_key(provider),
        timeout=600,
    )
    # deepseek-flash is a reasoning model: its thinking tokens count
    # against max_tokens, so budget generously for the final answer
    resp = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=8000,
    )
    return resp.choices[0].message.content or ""


def _call_llm_urllib(provider: str, model_name: str, system: str, user: str) -> str:
    """Fallback when the openai package is missing: same request with the
    stdlib only (same approach as akaidoo)."""
    base = PROVIDER_BASE_URLS.get(provider, "https://api.deepseek.com/v1")
    url = base + "/chat/completions"
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 8000,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_load_llm_api_key(provider)}",
        },
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _call_llm(model: str, system: str, user: str) -> str:
    """Call the LLM through the openai SDK when available (loose
    dependency, same one Hermes relies on), else the stdlib fallback.
    Raises on any failure so the caller can degrade to a warning."""
    provider, model_name = _parse_model(model)
    try:
        import openai  # noqa: F401

        return _call_llm_openai(provider, model_name, system, user)
    except ImportError:
        print(
            "NOTE! openai package not installed: using the stdlib"
            " fallback for the LLM call."
        )
        return _call_llm_urllib(provider, model_name, system, user)


def generate_addon_readme(
    serie: int,
    serie_dir: str,
    addon: str,
    model: Optional[str] = None,
    notes_path: Optional[Path] = None,
) -> bool:
    """Write <serie_dir>/<addon>/README.md, the LLM summary of the addon
    changes. Returns False when skipped (no patches, missing API key or
    LLM error): the analysis itself stays valid."""
    addon_dir = Path(serie_dir) / addon
    patch_files = [
        f for f in addon_dir.glob("*.patch") if f.name != "method_signatures.patch"
    ]
    if not patch_files:
        return False

    model = model or os.environ.get(LLM_MODEL_ENV) or DEFAULT_LLM_MODEL
    provider = model.split("/", 1)[0] if "/" in model else "deepseek"
    api_key = _load_llm_api_key(provider)
    if api_key:
        os.environ.setdefault(f"{provider.upper()}_API_KEY", api_key)

    patches_context = build_context(
        addons=[addon],
        addons_dirs={addon: str(addon_dir)},
        from_label=f"{serie - 1}.0",
        to_label=f"{serie}.0",
        max_bytes=ADDON_README_PATCH_BUDGET,
        header=False,
    )

    notes_context = ""
    if notes_path:
        sections = load_release_note_sections(notes_path)
        notes_context = select_release_note_sections(sections, addon, patches_context)

    prev_serie = serie - 1
    system = (
        "You are an Odoo migration expert writing customer-facing"
        " documentation for Akretion, an Odoo integrator shipping the"
        " Community edition (OCA style deployments)."
    )
    user = f"""Write a README.md summary (STRICT MAX {ADDON_README_MAX_CHARS} \
characters) for the Odoo addon "{addon}" describing what changed between \
Odoo {prev_serie}.0 and Odoo {serie}.0.

Audience: Odoo functional users and a customer considering a migration from \
{prev_serie}.0 to {serie}.0. Favor clear, user focused explanations over raw \
technical detail, but stay factual.

SOURCE 1 - PSEUDO PATCHES (ground truth: the data model impacting commits \
found by odoo-module-diff, plus method signature deltas):

{patches_context}

SOURCE 2 - EXTRACT OF THE OFFICIAL ODOO RELEASE NOTES (pre-selected \
sections):

{notes_context or FALLBACK_NOTE}

Write the README.md with exactly these markdown sections:
# {addon} migration guide ({prev_serie}.0 -> {serie}.0)
## What's new for users
## Technical data model changes
## How your habits should change
## What you gain by migrating

Rules:
- "What's new for users" and "How your habits should change" come from the \
release notes extract: use ONLY the parts that actually relate to this \
addon. The release notes are fuzzy and cover Enterprise features: NEVER \
present an Enterprise-only feature as included in the Community edition; \
when unsure, drop it.
- "Technical data model changes" comes from the pseudo patches only: \
added/removed models and fields, changed field behaviors, method signature \
changes.
- If the release notes say nothing relevant for this addon, say it plainly \
and rely on the pseudo patches.
- "What you gain by migrating" summarizes the concrete selling points for a \
customer still on {prev_serie}.0.
- Output ONLY the markdown content (no code fences), at most \
{ADDON_README_MAX_CHARS} characters."""
    try:
        content = _call_llm(model, system, user)
    except Exception as err:
        print(
            f"WARNING! LLM call failed for {addon} ({err}): skipping its"
            " README generation."
        )
        return False

    content = content.strip()
    content = re.sub(r"^```(?:markdown)?\n|```$", "", content).strip()
    if len(content) > ADDON_README_MAX_CHARS:
        # hard limit: cut at the last line boundary that fits
        cut = content.rfind("\n", 0, ADDON_README_MAX_CHARS + 1)
        content = content[: cut if cut > 0 else ADDON_README_MAX_CHARS]
    out = addon_dir / "README.md"
    out.write_text(content + "\n")
    print(f"Generated {out} ({len(content)} chars)")
    return True


def generate_addon_readmes(
    serie: int,
    serie_dir: str,
    addon: Optional[str] = None,
    model: Optional[str] = None,
    github_base: str = "https://github.com/akretion/odoo-module-diff-analysis/blob/main",
) -> None:
    """Generate the README.md of the given addon, or of the
    ADDON_README_LIMIT addons with the most patch bytes (same ranking as
    the per-serie README) when addon is None."""
    from odoo_module_diff.main import addons_patch_stats

    stats = addons_patch_stats(Path(serie_dir))
    if addon:
        targets = [addon]
    else:
        targets = [name for name, _count, _size in stats[:ADDON_README_LIMIT]]
    if not targets:
        print(f"No addon analysis found in {serie_dir}: nothing to summarize.")
        return

    notes_path = ensure_release_notes(serie, serie_dir)
    done = 0
    for target in targets:
        addon_dir = Path(serie_dir) / target
        patch_files = [
            f
            for f in addon_dir.glob("*.patch")
            if f.name != "method_signatures.patch"
        ]
        if not patch_files:
            print(f"Skipping {target}: no pseudo patch in its analysis dir.")
            continue
        if generate_addon_readme(
            serie, serie_dir, target, model=model, notes_path=notes_path
        ):
            done += 1
    print(f"Per addon README generation: {done}/{len(targets)} addon(s) written.")
    if notes_path:
        print(
            f"(release notes: {notes_path}; ranking base:"
            f" {github_base}/{serie}.0/README.md)"
        )
