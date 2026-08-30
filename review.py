#!/usr/bin/env python3
"""TransForge review stage — cloud-API second pass over the high-visibility
sections of translated siblings.

Readers mostly see only the top of a page, so this stage sends the frontmatter
summary fields (plain, you_setup, llm_does, llm_prompt, title, excerpt,
description) plus the body intro through the tl;dr callout to a configurable
OpenAI-compatible reviewer model ("the Flash API bot"), which corrects
mistranslations, meaning inversions and grammar slips. Everything else stays
local-model output.

NOTE: unlike translation (strictly local), this stage calls a CLOUD API by
explicit owner decision — public blog content only. It is OFF unless
[review].enabled = true in the config, and the endpoint/model/key all come
from config/env; nothing is baked in here.

Config (~/.config/transforge/config.toml):

    [review]
    enabled = true
    base_url = "https://api.deepseek.com"   # OpenAI-compatible root
    model = "deepseek-v4-flash"
    api_key_env = "DEEPSEEK_API_KEY"        # env var NAME, value never logged
    sections = ["title","excerpt","description","plain","you_setup",
                "llm_does","llm_prompt","intro_through_tldr"]
    timeout_s = 120
    max_tokens = 16384
    reasoning_effort = "none"   # only sent when non-empty; on a reasoning
                                # model the token budget covers the hidden
                                # reasoning, and a model that thinks past it
                                # returns an EMPTY answer

    [sites.<name>.review]                   # optional per-site override
    enabled = false

Behavior:
  - Only tracked, pipeline-produced siblings are reviewed (CURRENT/DRIFT).
    MISSING/STALE pages must be translated first; UNTRACKED must be accepted.
  - Hand-edited (EDITED) siblings are skipped unless --include-edited AND the
    hand-edited region provably lies outside every reviewed section.
  - A reply that fails to parse, changes structure, or injects glossary terms
    is discarded: the original text is kept and a WARN is printed. A failed
    review is loud but never destructive.
  - Idempotent: the manifest records a hash of the reviewed sections + model;
    unchanged sections are never re-sent.

Exit codes: 0 ok/disabled · 1 review failures · 2 usage/config ·
            7 API key env var not set
"""
import argparse
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request

import yaml

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import transforge as tfmod  # noqa: E402  (helpers: manifest, verify, config)

REVIEW_DEFAULT_SECTIONS = [
    "title", "excerpt", "description", "plain",
    "you_setup", "llm_does", "llm_prompt", "intro_through_tldr",
]

REVIEW_DEFAULTS = {
    "enabled": False,
    "base_url": "",          # must come from config — no endpoint baked in
    "model": "",             # must come from config
    "api_key_env": "DEEPSEEK_API_KEY",
    "sections": REVIEW_DEFAULT_SECTIONS,
    "timeout_s": 120,
    "max_tokens": 16384,
    "temperature": 0.2,
    # Sent only when non-empty, so an endpoint that does not know the
    # parameter is never handed it. On a reasoning model "none" is what keeps
    # the answer inside the budget (see reasoning-overflow note below).
    "reasoning_effort": "",
}

EXIT_NO_KEY = 7

BODY_SECTION = "intro_through_tldr"
CALLOUT_RE = re.compile(r'<div\s+class="callout"', re.I)
DIV_RE = re.compile(r"<div\b|</div>", re.I)


class ReviewConfig:
    def __init__(self, merged):
        for k, v in merged.items():
            setattr(self, k, v)

    def api_key(self):
        return os.environ.get(self.api_key_env, "")


def load_review_config(site_name):
    """[review] merged under [sites.<name>.review]; missing tables = defaults."""
    merged = dict(REVIEW_DEFAULTS)
    try:
        with open(tfmod.CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except OSError:
        cfg = {}
    for layer in (cfg.get("review") or {},
                  ((cfg.get("sites") or {}).get(site_name) or {}).get("review") or {}):
        for k, v in layer.items():
            if k in REVIEW_DEFAULTS:
                merged[k] = v
    return ReviewConfig(merged)


# ------------------------------------------------------------ extraction
def intro_span(body):
    """Char span of the body from its start through the close of the FIRST
    callout div; falls back to everything before the first '## ' heading.
    (0, 0) means no reviewable intro region."""
    m = CALLOUT_RE.search(body)
    if m:
        depth = 0
        for dm in DIV_RE.finditer(body, m.start()):
            depth += 1 if dm.group(0).lower().startswith("<div") else -1
            if depth == 0:
                return 0, dm.end()
        # unclosed callout: fail closed, review nothing from the body
        return 0, 0
    hm = re.search(r"^##\s", body, re.M)
    return (0, hm.start()) if hm else (0, 0)


def extract_sections(text, sections):
    """-> (parts, fm_dict, body). parts maps section name -> str | [str]."""
    fm_text, body = tfmod.split_frontmatter(text)
    fm = yaml.safe_load(fm_text) if fm_text else {}
    if not isinstance(fm, dict):
        fm = {}
    parts = {}
    for s in sections:
        if s == BODY_SECTION:
            a, b = intro_span(body)
            if b > a:
                parts[s] = body[a:b]
        else:
            v = fm.get(s)
            if isinstance(v, str) and v.strip():
                parts[s] = v
            elif isinstance(v, list) and v:
                parts[s] = v
    return parts, fm, body


def sections_sha(src_parts, out_parts, rc):
    basis = json.dumps(
        {"src": src_parts, "out": out_parts, "model": rc.model,
         "sections": rc.sections},
        sort_keys=True, ensure_ascii=False, default=str)
    return tfmod.sha256_text(basis)[:16]


# ------------------------------------------------------------ API client
class FlashClient:
    def __init__(self, rc):
        self.rc = rc
        self.url = rc.base_url.rstrip("/") + "/chat/completions"
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0,
                      "cache_hit_tokens": 0, "cache_miss_tokens": 0}

    def chat(self, system, user):
        """One reviewer turn, with reasoning-overflow recovery.

        On a reasoning model `max_tokens` covers the hidden reasoning too: a
        model that thinks past the budget returns finish_reason=length and an
        EMPTY content string. Retrying that identically just burns the budget
        again, so the retry turns reasoning off and doubles the ceiling.
        """
        out, finish = self._post(system, user, self.rc.max_tokens,
                                 self.rc.reasoning_effort)
        if not out and finish == "length":
            out, _ = self._post(system, user, self.rc.max_tokens * 2, "none")
            if not out:
                raise RuntimeError(
                    "empty reply: the model spent the whole token budget "
                    "reasoning (raise [review].max_tokens or set "
                    'reasoning_effort = "none")')
        return out

    def _post(self, system, user, max_tokens, reasoning_effort):
        payload = {
            "model": self.rc.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": self.rc.temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        req_body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + self.rc.api_key()}
        last = None
        for attempt in range(3):
            req = urllib.request.Request(self.url, data=req_body,
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.rc.timeout_s) as r:
                    data = json.loads(r.read().decode("utf-8"))
                u = data.get("usage") or {}
                self.usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                self.usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
                # DeepSeek reports cache hit/miss; other providers may not.
                self.usage["cache_hit_tokens"] += int(
                    u.get("prompt_cache_hit_tokens") or 0)
                self.usage["cache_miss_tokens"] += int(
                    u.get("prompt_cache_miss_tokens") or 0)
                self.usage["calls"] += 1
                choice = data["choices"][0]
                return ((choice["message"].get("content") or "").strip(),
                        choice.get("finish_reason"))
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read()[:300]!r}"
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(last)
            except Exception as e:  # timeouts, connection resets
                last = str(e)
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(last)
        raise RuntimeError(last)


def review_prompt(site, lang):
    name = site.lang_name(lang)
    terms = ", ".join(site.no_translate) if site.no_translate else ""
    p = (f"You are a meticulous bilingual reviewer of website translations from "
         f"English into {name}.\n"
         "You receive the SOURCE sections and the TRANSLATION sections as JSON "
         "objects with identical keys.\n"
         "Fix real defects in the TRANSLATION: mistranslations, meaning "
         "inversions, dropped or added clauses, unnatural phrasing, and "
         "grammar/gender/case errors, so each section faithfully and fluently "
         "renders its SOURCE. Leave correct text unchanged — do not restyle "
         "for taste.\n"
         "Rules:\n"
         "- Preserve all markup exactly: HTML tags and attributes, markdown, "
         "URLs, file paths and code fragments byte-for-byte.\n"
         "- Return a JSON object with EXACTLY the same keys as TRANSLATION.\n"
         "- A string value stays a string; an array stays an array with the "
         "SAME number of items, in the same order.\n")
    if terms:
        p += (f"- Keep these terms exactly as written where the source uses "
              f"them, and never introduce one where the source does not: "
              f"{terms}.\n")
    style = site.style.get(lang, "")
    if style:
        p += f"- Language conventions: {style}\n"
    p += "Respond with ONLY the JSON object."
    return p


def strip_fences(raw):
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()).strip()


def validate_reply(orig_parts, reply):
    """Fail-closed merge of a reviewer reply onto the original sections.
    -> (fixed_parts, changed_keys, kept_keys)."""
    fixed, changed, kept = {}, [], []
    for k, v in orig_parts.items():
        nv = reply.get(k)
        if isinstance(v, list):
            ok = (isinstance(nv, list) and len(nv) == len(v)
                  and all(isinstance(x, str) and x.strip() for x in nv))
        else:
            ok = isinstance(nv, str) and bool(nv.strip())
        if not ok:
            fixed[k] = v
            kept.append(k)
        else:
            fixed[k] = nv
            if nv != v:
                changed.append(k)
    return fixed, changed, kept


# ------------------------------------------------------------ application
FM_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*):(?:\s|$)")


def _fm_key_blocks(fm_text):
    """-> {key: (start_line, end_line)} for top-level frontmatter keys.
    A block runs to the line before the next column-0 key."""
    lines = fm_text.split("\n")
    starts = [(i, m.group(1)) for i, ln in enumerate(lines)
              if (m := FM_KEY_RE.match(ln))]
    blocks = {}
    for n, (i, key) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        blocks[key] = (i, end)
    return blocks


def splice_frontmatter(fm_text, fixed_fields):
    """Replace ONLY the given top-level fields in the raw frontmatter text.

    Re-dumping the whole mapping through yaml.safe_dump would silently
    reformat every untouched field (drop quotes, reflow lists) — on the live
    corpus that rewrote 242 of 252 siblings for a one-field correction. Every
    line we did not review is preserved byte-for-byte.
    """
    lines = fm_text.split("\n")
    blocks = _fm_key_blocks(fm_text)
    edits, appends = [], []
    for k, v in fixed_fields.items():
        chunk = yaml.safe_dump({k: v}, allow_unicode=True, sort_keys=False,
                               default_flow_style=False).rstrip("\n")
        if k in blocks:
            edits.append((blocks[k], chunk.split("\n")))
        else:
            appends.extend(chunk.split("\n"))
    out = list(lines)
    for (start, end), chunk in sorted(edits, key=lambda e: e[0][0],
                                      reverse=True):
        out[start:end] = chunk
    out.extend(appends)
    return "\n".join(out)


def apply_sections(sib_text, fixed_parts):
    """Splice corrected sections back into the sibling document.

    Everything outside the reviewed sections — untouched frontmatter fields
    and the whole body past the tl;dr — stays byte-for-byte identical.
    """
    fm_text, body = tfmod.split_frontmatter(sib_text)
    fm_fields = {k: v for k, v in fixed_parts.items() if k != BODY_SECTION}
    if BODY_SECTION in fixed_parts:
        a, b = intro_span(body)
        if b > a:
            body = body[:a] + fixed_parts[BODY_SECTION] + body[b:]
    if fm_text is None:
        # no frontmatter in the original: never fabricate one
        return body if body.endswith("\n") else body + "\n"
    if fm_fields:
        fm_text = splice_frontmatter(fm_text, fm_fields)
    out = "---\n" + fm_text.strip("\n") + "\n---\n" + body
    if not out.endswith("\n"):
        out += "\n"
    return out


def newest_backup(site, out_rel):
    root = os.path.join(tfmod.STATE_DIR, "backups", site.name)
    if not os.path.isdir(root):
        return None
    for stamp in sorted(os.listdir(root), reverse=True):
        p = os.path.join(root, stamp, out_rel)
        if os.path.isfile(p):
            return p
    return None


def edited_overlap(site, rc, out_rel, cur_text):
    """For an EDITED sibling: names of reviewed sections the hand-edit touched.
    None => no backup found, cannot prove safety (treat as overlap)."""
    bak = newest_backup(site, out_rel)
    if bak is None:
        return None
    bak_parts, _, _ = extract_sections(tfmod.read_text(bak), rc.sections)
    cur_parts, _, _ = extract_sections(cur_text, rc.sections)
    names = sorted(set(bak_parts) | set(cur_parts))
    return [n for n in names if bak_parts.get(n) != cur_parts.get(n)]


# ------------------------------------------------------------ command
def cmd_review(args, sites):
    site = tfmod.pick_site(sites, args.site)
    rc = load_review_config(site.name)
    if not rc.enabled:
        print("review disabled ([review].enabled = false) — nothing to do")
        return 0
    if not rc.base_url or not rc.model:
        print("review misconfigured: [review].base_url and model are required "
              "(no endpoint is baked into the code)", file=sys.stderr)
        return 2
    if not rc.api_key():
        print(f"review: API key env var {rc.api_key_env} is not set "
              f"(export it, or point [review].api_key_env at one that is)",
              file=sys.stderr)
        return EXIT_NO_KEY

    manifest = tfmod.Manifest(site.name)
    langs = args.langs or site.languages
    for lang in langs:
        if lang not in site.languages:
            print(f"language '{lang}' not configured for site {site.name}",
                  file=sys.stderr)
            return 2
    files = tfmod.normalize_files(site, args.files)
    sources = tfmod.discover_sources(site)
    if files:
        unknown = [f for f in files if f not in sources]
        if unknown:
            print("no such source page(s) under this site: "
                  + ", ".join(unknown), file=sys.stderr)
            return 2

    client = FlashClient(rc)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stats = {"reviewed": 0, "changed": 0, "skipped": 0, "fail": 0,
             "partial": 0, "edited": 0}
    exit_code = 0
    hit_limit = False

    for rel_src in sources:
        if hit_limit:
            break
        if files and rel_src not in files:
            continue
        src_abs = os.path.join(site.root, rel_src)
        src_text = tfmod.read_text(src_abs)
        src_parts, _, _ = extract_sections(src_text, rc.sections)
        if not src_parts:
            continue
        for lang in langs:
            if args.limit and stats["reviewed"] >= args.limit:
                hit_limit = True
                break
            out_rel = tfmod.sibling_path(rel_src, lang)
            out_abs = os.path.join(site.root, out_rel)
            state, src_sha = tfmod.classify(site, manifest, rel_src, lang)
            if state in ("MISSING", "STALE"):
                print(f"skip [{state}] {lang} {rel_src}: translate first",
                      file=sys.stderr)
                stats["skipped"] += 1
                continue
            if state == "UNTRACKED":
                print(f"skip [UNTRACKED] {lang} {rel_src}: run 'accept' first",
                      file=sys.stderr)
                stats["skipped"] += 1
                continue
            cur_text = tfmod.read_text(out_abs)
            if state == "EDITED":
                if not args.include_edited:
                    print(f"skip [EDITED] {lang} {rel_src}: hand-edited "
                          "(use --include-edited)", file=sys.stderr)
                    stats["skipped"] += 1
                    stats["edited"] += 1
                    continue
                overlap = edited_overlap(site, rc, out_rel, cur_text)
                if overlap is None:
                    print(f"REFUSE [EDITED] {lang} {rel_src}: no backup to "
                          "prove the hand-edit is outside reviewed sections",
                          file=sys.stderr)
                    stats["skipped"] += 1
                    stats["edited"] += 1
                    continue
                if overlap:
                    print(f"REFUSE [EDITED] {lang} {rel_src}: hand-edit "
                          "overlaps reviewed section(s): "
                          + ", ".join(overlap), file=sys.stderr)
                    stats["skipped"] += 1
                    stats["edited"] += 1
                    continue
            out_parts, _, _ = extract_sections(cur_text, rc.sections)
            common = {k: out_parts[k] for k in src_parts if k in out_parts}
            if not common:
                stats["skipped"] += 1
                continue
            src_common = {k: src_parts[k] for k in common}
            sha = sections_sha(src_common, common, rc)
            entry = manifest.get(rel_src, lang) or {}
            rev = entry.get("review") or {}
            if rev.get("sha") == sha:
                stats["skipped"] += 1
                continue
            if args.dry_run:
                print(f"would review {lang} {rel_src}: "
                      + ", ".join(sorted(common)))
                stats["reviewed"] += 1
                continue
            try:
                raw = client.chat(
                    review_prompt(site, lang),
                    "SOURCE (English):\n"
                    + json.dumps(src_common, ensure_ascii=False, indent=1,
                                 default=str)
                    + f"\n\nTRANSLATION ({site.lang_name(lang)}):\n"
                    + json.dumps(common, ensure_ascii=False, indent=1,
                                 default=str))
                reply = json.loads(strip_fences(raw))
                if not isinstance(reply, dict):
                    raise ValueError("reply is not a JSON object")
            except (RuntimeError, ValueError, json.JSONDecodeError) as e:
                print(f"WARN {lang} {rel_src}: review reply unusable, "
                      f"keeping original ({e})", file=sys.stderr)
                stats["fail"] += 1
                exit_code = 1
                continue
            fixed, changed, kept = validate_reply(common, reply)
            if kept:
                print(f"WARN {lang} {rel_src}: reviewer reply invalid for "
                      f"section(s) {', '.join(kept)} — originals kept",
                      file=sys.stderr)
                stats["partial"] += 1
            if not changed:
                manifest.set(rel_src, lang, {**entry, "review": {
                    "sha": sha, "model": rc.model, "at": tfmod.now_iso(),
                    "changed": []}})
                manifest.save()
                stats["reviewed"] += 1
                print(f"OK   {lang} {rel_src}: clean (no changes)")
                continue
            new_text = apply_sections(cur_text, fixed)
            # Only the reviewed regions may differ. If anything else moved, the
            # splice itself misbehaved — refuse rather than rewrite the page.
            if apply_sections(cur_text, {}) != cur_text:
                print(f"WARN {lang} {rel_src}: document does not survive a "
                      "no-op splice unchanged, keeping original",
                      file=sys.stderr)
                stats["fail"] += 1
                exit_code = 1
                continue
            # Judge the reviewer on what IT changed: a sibling that already
            # tripped a check (hand-fixes, older pipeline output) must not make
            # every future review look like a failure.
            before = set(tfmod.verify_structure(src_text, cur_text, lang, site))
            problems = [p for p in
                        tfmod.verify_structure(src_text, new_text, lang, site)
                        if p not in before]
            if problems:
                print(f"WARN {lang} {rel_src}: reviewed text failed "
                      "verification, keeping original: "
                      + "; ".join(problems), file=sys.stderr)
                stats["fail"] += 1
                exit_code = 1
                continue
            tfmod.backup_existing(site, out_abs, stamp)
            tfmod.write_atomic(out_abs, new_text)
            new_parts, _, _ = extract_sections(new_text, rc.sections)
            new_common = {k: new_parts[k] for k in src_common if k in new_parts}
            manifest.set(rel_src, lang, {
                **entry,
                "src_sha": entry.get("src_sha") or src_sha,
                "out_sha": tfmod.sha256_text(new_text),
                "out": out_rel,
                "review": {"sha": sections_sha(src_common, new_common, rc),
                           "model": rc.model, "at": tfmod.now_iso(),
                           "changed": sorted(changed)}})
            manifest.save()
            stats["reviewed"] += 1
            stats["changed"] += 1
            print(f"OK   {lang} {rel_src}: corrected "
                  + ", ".join(sorted(changed)))

    u = client.usage
    cache = ""
    if u["cache_hit_tokens"] or u["cache_miss_tokens"]:
        cache = (f" (prompt cache {u['cache_hit_tokens']} hit / "
                 f"{u['cache_miss_tokens']} miss)")
    print(f"review done: {stats['reviewed']} reviewed "
          f"({stats['changed']} corrected, {stats['partial']} partial), "
          f"{stats['skipped']} skipped ({stats['edited']} hand-EDITED), "
          f"{stats['fail']} failed | {u['calls']} API call(s), "
          f"{u['prompt_tokens']} prompt + {u['completion_tokens']} completion "
          f"tokens{cache} [{rc.model}]")
    return exit_code


def add_cli(sub):
    p = sub.add_parser(
        "review",
        help="cloud-review the high-visibility sections of translated siblings")
    p.add_argument("--site")
    p.add_argument("--langs", nargs="*")
    p.add_argument("--files", nargs="*")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--include-edited", action="store_true",
                   help="also review hand-edited siblings when the hand-edit "
                        "is provably outside every reviewed section")
    p.add_argument("--limit", type=int)
    return p


def main():
    ap = argparse.ArgumentParser(prog="transforge-review", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_cli(sub)
    args = ap.parse_args()
    _, sites = tfmod.load_config()
    sys.exit(cmd_review(args, sites))


if __name__ == "__main__":
    main()
