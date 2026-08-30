#!/usr/bin/env python3
"""TransForge — config-driven website translator on a local LLM server.

Point it at a local site tree; it discovers English source pages, decides which
language siblings are MISSING or STALE via a content-hash manifest (never
mtimes), translates only those through a configurable model on your own
StudioForge/llama.cpp server, verifies structure deterministically, and writes
siblings atomically.
Nothing is ever re-translated while its EN source hash is unchanged, and a
hand-edited sibling is never overwritten without --force.

Config: ~/.config/transforge/config.toml  ([defaults] + [sites.<name>])
State:  ~/.local/state/transforge/<site>-manifest.json  (+ backups/, logs)

Commands:
  status   [--site X|--all]                what is current/stale/missing/edited
  plan     [--site X] [--langs ..]         list the jobs run would execute
  run      [--site X] [--langs ..] [--files ..] [--force] [--dry-run]
           [--workers N] [--no-warmup] [--limit N]
  single   FILE --lang L [--out PATH]      translate one arbitrary page
  accept   [--site X] [--files ..|--all]   record existing siblings as current
  verify   [--site X]                      re-run structural checks on outputs
  warmup   [--site X]                      ensure the model is loaded well
  models                                   list rig models (loaded state)
  report   [--append FILE]                 one-shot text report (cron-friendly)
  config   [--site X]                      show resolved configuration

Exit codes: 0 ok · 1 translation/verify failures · 2 usage/config ·
            4 rig unreachable · 5 model missing on rig · 6 rig leased (backoff)
"""
import sys

if sys.version_info < (3, 11):
    sys.exit("transforge needs Python 3.11+")

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

import yaml

CONFIG_PATH = os.path.expanduser("~/.config/transforge/config.toml")
STATE_DIR = os.path.expanduser("~/.local/state/transforge")
# Optional fallback location for STUDIOFORGE_MCP_PIN (an OpenClaw install);
# the environment variable takes precedence and is the portable way to set it.
PIN_ENV_FILE = os.path.expanduser("~/.openclaw/gateway.systemd.env")

# lease holders whose runs must not be disturbed (CrucibleForge doctrine)
BENCH_LEASE_HOLDERS = ("crucibleforge", "gauntlet")

USAGE_ERROR = 2


def die(msg, code=USAGE_ERROR):
    """Usage/config error: message on stderr, deterministic exit code."""
    print(msg, file=sys.stderr)
    sys.exit(code)


DEFAULTS = {
    "endpoint": "http://localhost:1234",
    "model": "unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S",
    "concurrency": "auto",          # follow server parallel slots, or an int cap
    "ctx_per_slot": 16384,          # warmup load-recommended tier
    "temperature": 0.3,
    "api_timeout": 300,
    "priority": 3,                  # background load tier: never displace chat models
    "disable_thinking": True,
    "prompt_template": "instruct",  # or "hunyuan-mt" for pure-MT models
    "max_tokens_fm": 8192,
    "max_tokens_fm_retry": 24000,
    "max_tokens_body": 12288,
    "max_tokens_body_retry": 24000,
    "max_chunk_chars": 12000,
    "extra_params": {},             # merged verbatim into the completion payload
}

CJK_RE = re.compile(r"[぀-ヿ一-鿿]")
AR_RE = re.compile(r"[؀-ۿ]")
KO_RE = re.compile(r"[가-힯]")
SCRIPT_CHECKS = {"ja": CJK_RE, "zh": CJK_RE, "zh-CN": CJK_RE, "zh-TW": CJK_RE,
                 "ar": AR_RE, "ko": KO_RE}


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_atomic(path, text, mode=0o644):
    """Atomic write with explicit world-readable mode (dsh's 0600 default once
    403'd production after an rsync — never inherit a restrictive umask here)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ------------------------------------------------------------------ config
class SiteConfig:
    def __init__(self, name, defaults, site):
        self.name = name
        merged = dict(DEFAULTS)
        merged.update(defaults or {})
        merged.update(site or {})
        self.raw = merged
        self.root = os.path.normpath(os.path.expanduser(merged.get("root", ".")))
        self.content_dirs = merged.get("content_dirs", [])
        self.languages = merged.get("languages", [])
        self.lang_names = merged.get("lang_names", {})
        self.link_dirs = merged.get("link_dirs", [])
        self.pages_dir = merged.get("pages_dir", "content/pages")
        self.site_description = merged.get("site_description", "")
        self.no_translate = merged.get("no_translate", [])
        self.translate_fields = merged.get("translate_fields", [])
        self.list_fields = set(merged.get("list_fields", []))
        self.style = merged.get("style", {})
        for k in ("endpoint", "model", "concurrency", "ctx_per_slot",
                  "temperature", "api_timeout", "priority", "disable_thinking",
                  "prompt_template", "max_tokens_fm", "max_tokens_fm_retry",
                  "max_tokens_body", "max_tokens_body_retry", "max_chunk_chars",
                  "extra_params"):
            setattr(self, k, merged[k])
        if self.concurrency != "auto":
            try:
                self.concurrency = int(self.concurrency)
            except (TypeError, ValueError):
                die(f"site {name}: concurrency must be \"auto\" or an integer "
                    f"(got {self.concurrency!r})")

    def lang_name(self, lang):
        return self.lang_names.get(lang, lang)

    def cfg_hash(self):
        """Hash of everything that changes translation OUTPUT (model + prompt
        inputs). Stored per manifest entry; a mismatch is reported as drift,
        never auto-retranslated."""
        basis = json.dumps({
            "model": self.model, "prompt_template": self.prompt_template,
            "no_translate": self.no_translate, "style": self.style,
            "site_description": self.site_description,
            "translate_fields": self.translate_fields,
            "temperature": self.temperature,
        }, sort_keys=True)
        return sha256_text(basis)[:16]


def load_config():
    if not os.path.isfile(CONFIG_PATH):
        die(f"config not found: {CONFIG_PATH} (create it; see README)")
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        die(f"config is not valid TOML: {CONFIG_PATH}: {e}")
    defaults = cfg.get("defaults", {})
    sites = {name: SiteConfig(name, defaults, sc)
             for name, sc in cfg.get("sites", {}).items()}
    if not sites:
        die("config has no [sites.*] sections")
    return defaults, sites


def pick_site(sites, name):
    if name:
        if name not in sites:
            die(f"unknown site '{name}' (have: {', '.join(sorted(sites))})")
        return sites[name]
    if len(sites) == 1:
        return next(iter(sites.values()))
    die(f"--site required (have: {', '.join(sorted(sites))})")


# ---------------------------------------------------------------- manifest
class Manifest:
    """entries key: '<rel_src>|<lang>' -> {src_sha, out_sha, cfg, model,
    out, translated_at, duration_s, accepted}"""

    def __init__(self, site_name):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.path = os.path.join(STATE_DIR, f"{site_name}-manifest.json")
        self.lock = threading.Lock()
        if os.path.isfile(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                die(f"manifest is corrupt: {self.path}: {e}\n"
                    f"delete it and re-run `transforge accept --site {site_name} "
                    f"--all` to rebuild from the siblings on disk")
            self.entries = data.get("entries", {}) if isinstance(data, dict) else {}
        else:
            self.entries = {}

    def get(self, rel_src, lang):
        return self.entries.get(f"{rel_src}|{lang}")

    def set(self, rel_src, lang, entry):
        with self.lock:
            self.entries[f"{rel_src}|{lang}"] = entry

    def prune(self, valid_keys):
        """Drop entries whose source page or language no longer exists.
        Returns the number removed."""
        with self.lock:
            dead = [k for k in self.entries if k not in valid_keys]
            for k in dead:
                del self.entries[k]
            return len(dead)

    def save(self):
        with self.lock:
            write_atomic(self.path, json.dumps(
                {"version": 1, "saved_at": now_iso(), "entries": self.entries},
                indent=1, sort_keys=True), mode=0o600)


# --------------------------------------------------------------- discovery
LANG_SUFFIX_RE = re.compile(r"\.([a-z]{2}(?:-[A-Za-z]{2})?)\.(md|html)$")


def discover_sources(site):
    """EN source files (relative to site root), skipping language siblings."""
    out = []
    for d in site.content_dirs:
        full = os.path.join(site.root, d)
        if not os.path.isdir(full):
            continue
        for fn in sorted(os.listdir(full)):
            if not fn.endswith((".md", ".html")) or LANG_SUFFIX_RE.search(fn):
                continue
            out.append(os.path.join(d, fn))
    return out


def sibling_path(rel_src, lang):
    stem, ext = os.path.splitext(rel_src)
    return f"{stem}.{lang}{ext}"


def classify(site, manifest, rel_src, lang):
    """-> (state, detail). States: MISSING, STALE, EDITED, UNTRACKED, CURRENT, DRIFT."""
    src_abs = os.path.join(site.root, rel_src)
    out_rel = sibling_path(rel_src, lang)
    out_abs = os.path.join(site.root, out_rel)
    src_sha = sha256_text(read_text(src_abs))
    entry = manifest.get(rel_src, lang)
    if not os.path.isfile(out_abs):
        return "MISSING", src_sha
    if entry is None:
        return "UNTRACKED", src_sha
    out_sha = sha256_text(read_text(out_abs))
    if out_sha != entry.get("out_sha"):
        return "EDITED", src_sha          # human touched the sibling: protect it
    if src_sha != entry.get("src_sha"):
        return "STALE", src_sha
    if entry.get("cfg") != site.cfg_hash():
        return "DRIFT", src_sha           # informational only
    return "CURRENT", src_sha


def scan_site(site, manifest):
    rows = {}
    for rel_src in discover_sources(site):
        for lang in site.languages:
            state, src_sha = classify(site, manifest, rel_src, lang)
            rows[(rel_src, lang)] = (state, src_sha)
    return rows


# --------------------------------------------------------------- rig client
class Rig:
    def __init__(self, site):
        self.base = site.endpoint.rstrip("/")
        self.site = site
        self._pin = None

    def _req(self, method, path, body=None, pin=False, timeout=30):
        headers = {"Content-Type": "application/json"}
        if pin:
            p = self.pin()
            if not p:
                raise RuntimeError(
                    "STUDIOFORGE_MCP_PIN unavailable — export it in the "
                    "environment (the server's management PIN is required for "
                    "model load/settings calls)")
            headers["X-MCP-Pin"] = p
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw else {}

    def pin(self):
        """Management PIN: environment first, then an optional env-file."""
        if self._pin is None:
            self._pin = os.environ.get("STUDIOFORGE_MCP_PIN", "").strip()
            if self._pin:
                return self._pin
            try:
                for line in read_text(PIN_ENV_FILE).splitlines():
                    m = re.match(r"STUDIOFORGE_MCP_PIN=\"?([^\s\"]+)\"?", line.strip())
                    if m:
                        self._pin = m.group(1)
                        break
            except OSError:
                pass
        return self._pin

    def models(self):
        return self._req("GET", "/v1/models").get("data", [])

    def status(self):
        return self._req("GET", "/api/status")

    def leases(self):
        try:
            return self._req("GET", "/api/leases", timeout=8).get("leases", [])
        except Exception:
            return []      # unreadable = no lease; the rig, not us, decides

    def bench_lease(self):
        for l in self.leases():
            if str(l.get("holder", "")).lower() in BENCH_LEASE_HOLDERS:
                return l
        return None

    def loaded_row(self, model_id):
        for row in self.status().get("loaded", []):
            if row.get("model_id") == model_id or row.get("id") == model_id:
                return row
        return None

    def live_parallel(self, model_id):
        row = self.loaded_row(model_id)
        if not row:
            return 0
        plan = row.get("plan") or {}
        return max(0, int(plan.get("parallel") or 0))

    def unload(self, model_id):
        self._req("POST", f"/api/models/{urllib.parse.quote(model_id, safe='')}/unload",
                  body={}, pin=True, timeout=120)

    def load_recommended(self, model_id, ctx_size, priority):
        return self._req(
            "POST",
            f"/api/models/{urllib.parse.quote(model_id, safe='')}/load-recommended",
            body={"ctx_size": ctx_size, "priority": priority}, pin=True, timeout=600)

    def wait_ready(self, model_id, timeout_s=900):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            row = self.loaded_row(model_id)
            if row and row.get("state") == "ready":
                return row
            time.sleep(3)
        raise RuntimeError(f"model not ready after {timeout_s}s: {model_id}")

    def chat(self, messages, max_tokens, temperature):
        payload = {"model": self.site.model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": temperature}
        if self.site.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload.update(self.site.extra_params or {})
        last_err = None
        for attempt in range(3):
            try:
                data = self._req("POST", "/v1/chat/completions", body=payload,
                                 timeout=self.site.api_timeout)
                served = data.get("model", "")
                if served and self.site.model not in (served,) and \
                        not served.endswith(self.site.model.split("/")[-1]):
                    raise RuntimeError(
                        f"wrong model served: asked {self.site.model!r}, got {served!r}")
                msg = data["choices"][0]["message"]
                msg["content"] = re.sub(r"<think>.*?</think>", "",
                                        msg.get("content") or "", flags=re.DOTALL).strip()
                return data
            except urllib.error.HTTPError as e:
                body = e.read()[:300]
                last_err = f"HTTP {e.code}: {body!r}"
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(6 * (attempt + 1))
                    continue
                raise RuntimeError(last_err)
            except RuntimeError:
                raise
            except Exception as e:
                last_err = str(e)
                if attempt < 2:
                    time.sleep(6 * (attempt + 1))
                    continue
                raise RuntimeError(last_err)
        raise RuntimeError(last_err)


def warmup(site, rig, quiet=False):
    """Ensure the configured model is resident on a sane plan. Returns worker
    count. Never displaces a busy/pinned resident; backs off for bench leases."""
    lease = rig.bench_lease()
    if lease:
        print(f"rig leased by '{lease.get('holder')}' — backing off", file=sys.stderr)
        sys.exit(6)
    ids = [m.get("id") for m in rig.models()]
    if site.model not in ids:
        near = [i for i in ids if site.model.split("/")[-1].lower() in i.lower()] or \
               [i for i in ids if site.model.split("/")[0].lower() in i.lower()]
        hint = f" nearest: {near[:3]}" if near else ""
        print(f"model not on rig: {site.model}{hint}", file=sys.stderr)
        sys.exit(5)
    par = rig.live_parallel(site.model)
    desired_min = 2 if site.concurrency == "auto" else 1
    if par >= desired_min:
        row = rig.loaded_row(site.model)
        if not row or row.get("state") != "ready":
            rig.wait_ready(site.model)
        if not quiet:
            plan = (rig.loaded_row(site.model) or {}).get("plan") or {}
            print(f"model resident: parallel={plan.get('parallel')} "
                  f"ctx/slot={plan.get('ctx_per_slot') or plan.get('ctx_size')}")
        return workers_for(site, rig)
    # not loaded, or resident on a degenerate plan (e.g. a leftover JIT load:
    # load-recommended would return the existing plan unchanged, so unload first)
    if not rig.pin():
        if par > 0:
            print("warning: model resident with parallel=1 and no PIN to re-plan; "
                  "running serial", file=sys.stderr)
            return 1
        print("warning: no PIN available — first request will JIT-load (slow, "
              "planner-default plan)", file=sys.stderr)
        return 1
    if par > 0:
        if not quiet:
            print(f"re-planning: unloading degenerate resident (parallel={par})")
        rig.unload(site.model)
    if not quiet:
        print(f"loading {site.model} at ctx/slot={site.ctx_per_slot} (priority {site.priority})")
    for attempt in range(6):
        try:
            rig.load_recommended(site.model, site.ctx_per_slot, site.priority)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            retry = None
            try:
                err = json.loads(body)
                retry = (err.get("retry_after_s")
                         or (err.get("error") or {}).get("retry_after_s")
                         or ((err.get("error") or {}).get("studioforge") or {}).get("retry_after_s"))
            except Exception:
                pass
            if e.code in (503, 507) and retry and attempt < 5:
                time.sleep(max(2.0, min(float(retry), 60.0)))
                continue
            raise RuntimeError(f"load failed: HTTP {e.code}: {body}")
    rig.wait_ready(site.model)
    return workers_for(site, rig)


def workers_for(site, rig):
    par = max(1, rig.live_parallel(site.model))
    if site.concurrency == "auto":
        return par
    return max(1, min(int(site.concurrency), par))


# -------------------------------------------------------------- translation
class Translator:
    def __init__(self, site, rig):
        self.site = site
        self.rig = rig

    # ---- prompts
    def _terms(self):
        return ", ".join(self.site.no_translate) if self.site.no_translate else ""

    def _style(self, lang):
        return self.site.style.get(lang, "")

    def fm_prompt(self, keys, lang):
        s = self.site
        p = (f"You translate the YAML frontmatter of a web page from English into "
             f"{s.lang_name(lang)}.\n")
        if s.site_description:
            p += f"Site context: {s.site_description}\n"
        p += ("Translate ONLY these fields, keeping their exact names: "
              + ", ".join(keys) + ".\nRules:\n"
              "- Scalar fields: natural, fluent translation.\n"
              "- Array fields: translate every string, keep the SAME number of "
              "items in the SAME order.\n")
        if self._terms():
            p += (f"- Keep these brand/technical terms exactly as written: "
                  f"{self._terms()}. Also keep code fragments, file paths and "
                  "commands as-is.\n")
        if self._style(lang):
            p += f"- Language conventions: {self._style(lang)}\n"
        p += ("- Do NOT add any keys.\n"
              "Respond with ONLY a valid JSON object containing exactly those "
              "keys. No markdown fences, no commentary.")
        return p

    def body_prompt(self, lang):
        s = self.site
        name = s.lang_name(lang)
        if s.prompt_template == "hunyuan-mt":
            p = (f"Translate the following segment into {name}, without additional "
                 "explanation. The segment is HTML: translate only human-readable "
                 "text; copy every tag, attribute, URL, path and all code inside "
                 "<code>/<pre> exactly as-is.")
            if self._terms():
                p += f" Keep these terms in English: {self._terms()}."
            if self._style(lang):
                p += f" {self._style(lang)}"
            return p
        p = (f"You are a professional translator localizing a web page from English "
             f"into {name}.\n")
        if s.site_description:
            p += f"Site context: {s.site_description}\n"
        p += (
            "The input is HTML. Translate ALL human-readable text into natural, "
            f"fluent {name} while preserving the exact HTML structure.\n\n"
            "STRICT RULES:\n"
            "1. Translate visible text: paragraphs, headings, list items, table "
            "text, <strong>/<em> content, figure aria-label attributes, img alt "
            "attributes, and text inside SVG <text> elements.\n"
            "2. NEVER change tag names or attribute names. Copy every attribute "
            "value that is a URL, path, class, id, or coordinate EXACTLY (href, "
            "src, class, id, width, height, viewBox, x, y, fill, stroke, "
            "font-size, d, etc.).\n"
            "3. Copy ALL content inside <code>...</code> and <pre><code>...</code>"
            "</pre> byte-for-byte, untranslated.\n")
        if self._terms():
            p += (f"4. Keep brand names, product names, commands, file paths and "
                  f"identifiers in English: {self._terms()}.\n")
        p += (
            "5. Preserve blank lines between blocks and the section order. Keep "
            "the same number of headings, SVG blocks, tables, figures, images and "
            "code blocks as the source.\n"
            "6. Do NOT rewrite, add or remove any URL. EVERY <a> link and <img> "
            "in the source must appear in your output exactly once — never omit, "
            "merge, split, or reword any link.\n"
            "7. Output ONLY the translated HTML. No markdown fences, no "
            "commentary, no trailing notes.\n"
            f"8. Use natural {name} typography.")
        if self._style(lang):
            p += f"\n9. Language conventions: {self._style(lang)}"
        return p

    # ---- frontmatter
    def _mt_text(self, text, lang):
        """Plain-text translation of one string via a pure-MT prompt (no JSON
        round-trip — MT specialists emit unescaped CJK quotes inside JSON)."""
        s = self.site
        prompt = (f"Translate the following segment into {s.lang_name(lang)}, "
                  "without additional explanation.")
        if self._terms():
            prompt += (f" Keep these terms exactly as written: {self._terms()}. "
                       "Keep code fragments, file paths and commands as-is.")
        if self._style(lang):
            prompt += f" {self._style(lang)}"
        prompt += "\n\n" + text
        messages = [{"role": "user", "content": prompt}]
        data = self.rig.chat(messages, s.max_tokens_fm, s.temperature)
        choice = data["choices"][0]
        out = choice["message"]["content"].strip()
        if choice.get("finish_reason") == "length" or not out:
            data = self.rig.chat(messages, s.max_tokens_fm_retry, s.temperature)
            out = data["choices"][0]["message"]["content"].strip()
        if not out:
            raise RuntimeError("empty frontmatter field translation")
        return out

    def translate_frontmatter(self, fm_text, lang):
        s = self.site
        orig = yaml.safe_load(fm_text)
        if not isinstance(orig, dict):
            raise ValueError("frontmatter is not a mapping")
        keys = [k for k in s.translate_fields if k in orig]
        if not keys:
            return fm_text.strip()
        if s.prompt_template == "hunyuan-mt":
            # field-by-field plain text: item counts hold by construction
            translated = {}
            for k in keys:
                v = orig[k]
                if isinstance(v, str) and v.strip():
                    translated[k] = self._mt_text(v, lang)
                elif isinstance(v, list):
                    translated[k] = [self._mt_text(x, lang)
                                     if isinstance(x, str) and x.strip() else x
                                     for x in v]
                else:
                    continue
            merged = dict(orig)
            merged.update(translated)
            return yaml.safe_dump(merged, allow_unicode=True, sort_keys=False,
                                  default_flow_style=False).strip()
        sys_prompt = self.fm_prompt(keys, lang)
        messages = [{"role": "system", "content": sys_prompt},
                    {"role": "user", "content": fm_text}]
        data = self.rig.chat(messages, s.max_tokens_fm, s.temperature)
        choice = data["choices"][0]
        raw = choice["message"]["content"].strip()
        if choice.get("finish_reason") == "length" or not raw:
            messages = messages + [
                {"role": "assistant", "content": raw or "(empty)"},
                {"role": "user", "content":
                 "Your previous output was cut off before any JSON was produced. "
                 "Re-translate the WHOLE frontmatter and respond with ONLY a valid "
                 "JSON object containing exactly the requested keys."}]
            data = self.rig.chat(messages, s.max_tokens_fm_retry, s.temperature)
            raw = data["choices"][0]["message"]["content"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
        try:
            translated = json.loads(raw)
        except json.JSONDecodeError:
            data = self.rig.chat(
                [{"role": "system", "content": sys_prompt},
                 {"role": "user", "content": fm_text},
                 {"role": "assistant", "content": raw or "(empty)"},
                 {"role": "user", "content":
                  "That was not valid JSON. Respond with ONLY a valid JSON object "
                  "containing exactly the requested keys."}],
                s.max_tokens_fm_retry, s.temperature)
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "",
                         data["choices"][0]["message"]["content"].strip()).strip()
            try:
                translated = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"frontmatter JSON parse failed after retry: {e}")
        missing = [k for k in keys if k not in translated]
        if missing:
            raise RuntimeError(f"frontmatter JSON missing keys: {missing}")
        for k in self.site.list_fields & set(keys):
            if isinstance(orig.get(k), list) and \
                    len(translated.get(k) or []) != len(orig.get(k) or []):
                raise RuntimeError(
                    f"frontmatter field '{k}' item count changed "
                    f"({len(orig.get(k) or [])} -> {len(translated.get(k) or [])})")
        merged = dict(orig)
        merged.update({k: translated[k] for k in keys})
        return yaml.safe_dump(merged, allow_unicode=True, sort_keys=False,
                              default_flow_style=False).strip()

    # ---- body
    def split_chunks(self, body):
        """Split at <h2 line starts; oversized chunks split again at <h3, then
        at blank lines, so no single call can truncate."""
        def split_at(text, tag):
            lines, chunks, cur = text.split("\n"), [], []
            for line in lines:
                if line.lstrip().startswith(tag) and cur:
                    chunks.append("\n".join(cur))
                    cur = [line]
                else:
                    cur.append(line)
            if cur:
                chunks.append("\n".join(cur))
            return chunks

        out = []
        for c in split_at(body, "<h2"):
            if len(c) <= self.site.max_chunk_chars:
                out.append(c)
                continue
            for c2 in split_at(c, "<h3"):
                if len(c2) <= self.site.max_chunk_chars:
                    out.append(c2)
                    continue
                # last resort: pack paragraphs (blank-line separated)
                cur, size = [], 0
                for para in c2.split("\n\n"):
                    if size + len(para) > self.site.max_chunk_chars and cur:
                        out.append("\n\n".join(cur))
                        cur, size = [], 0
                    cur.append(para)
                    size += len(para) + 2
                if cur:
                    out.append("\n\n".join(cur))
        return [c.strip() for c in out if c.strip()]

    def translate_chunk(self, chunk, lang):
        s = self.site
        need_links = count(chunk, r'href="')
        need_src = count(chunk, r'src="')
        budget = s.max_tokens_body
        messages = [{"role": "system", "content": self.body_prompt(lang)},
                    {"role": "user", "content": chunk}]
        for attempt in range(3):
            data = self.rig.chat(messages, budget, s.temperature)
            choice = data["choices"][0]
            text = choice["message"]["content"].strip()
            if choice.get("finish_reason") == "length":
                budget = s.max_tokens_body_retry     # reasoning ate the budget
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content":
                     "Your previous output was cut off mid-way. Re-translate the "
                     "WHOLE chunk, complete."}]
                continue
            got_links = count(text, r'href="')
            got_src = count(text, r'src="')
            if got_links == need_links and got_src == need_src:
                return text
            if attempt < 2:
                budget = s.max_tokens_body_retry
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content":
                     f"ERROR: the source chunk has {need_links} <a href> links and "
                     f"{need_src} src= attributes, but your output has {got_links} "
                     f"and {got_src}. Re-translate the WHOLE chunk from scratch. "
                     "Every link and image must appear exactly once, byte-for-byte "
                     "(same URL, same position). Never omit, merge, or reword any link."}]
                continue
            raise RuntimeError(
                f"body chunk link mismatch after retries (href {need_links}->"
                f"{got_links}, src {need_src}->{got_src})")
        raise RuntimeError("body chunk still truncated after retries")

    def translate_body(self, body, lang):
        parts = [self.translate_chunk(c, lang) for c in self.split_chunks(body)]
        return "\n\n".join(p for p in parts if p)

    # ---- whole documents
    def translate_document(self, source_text, lang):
        fm_text, body = split_frontmatter(source_text)
        if fm_text is None:
            out = self.translate_body(source_text, lang)
        else:
            fm_out = self.translate_frontmatter(fm_text, lang)
            body_out = self.translate_body(body, lang)
            out = "---\n" + fm_out + "\n---\n\n" + body_out + "\n"
        out = rewrite_internal_links(out, lang, self.site)
        problems = verify_structure(source_text, out, lang, self.site)
        if problems:
            raise RuntimeError("structural verification failed: " + "; ".join(problems))
        return out


# ------------------------------------------------------- structure helpers
def split_frontmatter(text):
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def count(text, pat):
    return len(re.findall(pat, text))


def _translation_exists(site, link_dir, slug, lang):
    slug = slug.split("#")[0]
    content_root = os.path.join(site.root, "content")
    return any(os.path.isfile(p) for p in (
        os.path.join(content_root, link_dir, f"{slug}.{lang}.md"),
        os.path.join(site.root, site.pages_dir, f"{slug}.{lang}.md"),
    ))


def link_dirs_pattern(site):
    """Alternation of the configured link dirs, each escaped — a dir name may
    legitimately contain regex metacharacters (e.g. 'privacy-policy', 'c++')."""
    return "|".join(re.escape(d) for d in site.link_dirs)


def rewrite_internal_links(text, lang, site):
    """Prefix internal links with /<lang>/ ONLY when that translation exists
    on disk (deterministic, after the model runs — never asked of the model)."""
    if not site.link_dirs:
        return text
    dirs = link_dirs_pattern(site)

    def _repl(m):
        link_dir, slug, trailing = m.group(1), m.group(2), m.group(3)
        if not slug:
            slug = link_dir
        if _translation_exists(site, link_dir, slug, lang):
            if slug == link_dir and not m.group(2):
                return f'href="/{lang}/{link_dir}/{trailing}'
            return f'href="/{lang}/{link_dir}/{slug}{trailing}'
        return m.group(0)

    return re.sub(rf'href="/({dirs})/([^"/]*)(/?)', _repl, text)


def _term_re(term):
    # ASCII word-ish boundaries so "dsh" can't match inside another word while
    # CJK/Arabic context (non-ASCII) never suppresses a match. Inside a
    # multi-word term, hyphens/dashes count as the space ("Claude-Code-style"
    # in EN must satisfy "Claude Code" in the output).
    body = r"[\s\-‐-―]+".join(re.escape(w) for w in term.split())
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])")


def glossary_deltas(src, out, site):
    """(injected, increased): no_translate terms the model INJECTED (present in
    the output, absent from the source — the Hy-MT2 'OpenClaw' bleed class) vs
    merely used more often (usually legit: CJK swaps a pronoun for the noun)."""
    injected, increased = [], []
    for term in site.no_translate:
        a = len(_term_re(term).findall(src))
        b = len(_term_re(term).findall(out))
        if b and not a:
            injected.append(f"{term} (0 -> {b})")
        elif b > a:
            increased.append(f"{term} ({a} -> {b})")
    return injected, increased


def verify_structure(src, out, lang, site):
    problems = []
    injected, _ = glossary_deltas(src, out, site)
    if injected:
        problems.append("glossary term injected by model: " + ", ".join(injected))
    checks = [
        ("headings h2/h3", r"<h[23][\s>]"),
        ("svg open", r"<svg[\s>]"), ("svg close", r"</svg>"),
        ("code blocks open", r"<pre><code>"), ("code blocks close", r"</code></pre>"),
        ("inline code", r"<code[\s>]"),
        ("tables open", r"<table[\s>]"), ("tables close", r"</table>"),
        ("figures open", r"<figure[\s>]"), ("figures close", r"</figure>"),
        ("images", r"<img[\s>]"),
        ("links", r'href="'), ("src attrs", r'src="'),
        ("svg path d=", r'd="'), ("svg text elements", r"<text[\s>]"),
    ]
    for name, pat in checks:
        a, b = count(src, pat), count(out, pat)
        if a != b:
            problems.append(f"{name}: {a} -> {b}")
    if site.link_dirs:
        dirs = link_dirs_pattern(site)
        bare_src = count(src, rf'href="/(?:{dirs})/')
        bare_out = count(out, rf'href="/(?:{dirs})/')
        pref_out = count(out, rf'href="/{re.escape(lang)}/(?:{dirs})/')
        if pref_out + bare_out != bare_src:
            problems.append(
                f"internal links: source={bare_src}, output bare={bare_out}, "
                f"prefixed /{lang}/={pref_out}")
        for m in re.finditer(rf'href="/({dirs})/([^"/]*)(/?)', out):
            link_dir, slug = m.group(1), m.group(2) or m.group(1)
            if _translation_exists(site, link_dir, slug, lang):
                problems.append(f"link should be /{lang}/-prefixed: /{link_dir}/{slug}/")
        for m in re.finditer(rf'href="/{re.escape(lang)}/({dirs})/([^"/]*)(/?)', out):
            link_dir, slug = m.group(1), m.group(2) or m.group(1)
            if not _translation_exists(site, link_dir, slug, lang):
                problems.append(
                    f"link /{lang}/-prefixed but no translation: /{lang}/{link_dir}/{slug}/")
    script_re = SCRIPT_CHECKS.get(lang)
    if script_re and not script_re.search(out):
        problems.append(f"output contains no {lang} script characters (echoed EN?)")
    # size-ratio floor (from the proven wave-script gate)
    if len(out) < max(200, int(len(src) * 0.35)):
        problems.append(f"output suspiciously small ({len(out)} vs source {len(src)} chars)")
    return problems


# --------------------------------------------------------------- run logic
def backup_existing(site, out_abs, stamp):
    if not os.path.isfile(out_abs):
        return
    rel = os.path.relpath(out_abs, site.root)
    dst = os.path.join(STATE_DIR, "backups", site.name, stamp, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(out_abs, dst)


def build_jobs(site, manifest, langs, files, force):
    rows = scan_site(site, manifest)
    jobs = []
    for (rel_src, lang), (state, src_sha) in sorted(rows.items()):
        if lang not in langs:
            continue
        if files and rel_src not in files:
            continue
        if state in ("MISSING", "STALE"):
            jobs.append((rel_src, lang, state, src_sha))
        elif state in ("UNTRACKED", "EDITED", "DRIFT", "CURRENT") and force and files:
            jobs.append((rel_src, lang, f"FORCE({state})", src_sha))
        elif state == "UNTRACKED" and force and not files:
            jobs.append((rel_src, lang, "FORCE(UNTRACKED)", src_sha))
    return jobs, rows


def prune_manifest(manifest, rows):
    """Forget manifest entries for sources/languages that no longer exist."""
    removed = manifest.prune({f"{rel_src}|{lang}" for rel_src, lang in rows})
    if removed:
        manifest.save()
        print(f"pruned {removed} stale manifest entry(ies)")
    return removed


def cmd_run(args, sites):
    site = pick_site(sites, args.site)
    manifest = Manifest(site.name)
    langs = args.langs or site.languages
    for l in langs:
        if l not in site.languages:
            die(f"language '{l}' not configured for site {site.name}")
    files = normalize_files(site, args.files)
    jobs, rows = build_jobs(site, manifest, langs, files, args.force)
    prune_manifest(manifest, rows)
    if args.limit:
        jobs = jobs[: args.limit]
    if not jobs:
        print("nothing to do — all requested siblings are current")
        return 0
    print(f"{len(jobs)} job(s): "
          + ", ".join(sorted({j[2] for j in jobs}))
          + f" | model {site.model}")
    if args.dry_run:
        for rel_src, lang, state, _ in jobs:
            print(f"  would translate [{state}] {rel_src} -> {sibling_path(rel_src, lang)}")
        return 0

    rig = Rig(site)
    try:
        if args.no_warmup:
            workers = max(1, rig.live_parallel(site.model)) if site.concurrency == "auto" \
                else max(1, int(site.concurrency))
        else:
            workers = warmup(site, rig)
    except urllib.error.HTTPError as e:
        # reachable, but the server refused: not an unreachable-rig condition
        print(f"rig returned HTTP {e.code} {e.reason} for {e.url}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"rig unreachable: {e}", file=sys.stderr)
        return 4
    except RuntimeError as e:
        print(f"warmup failed: {e}", file=sys.stderr)
        return 1
    if args.workers:
        workers = max(1, args.workers)
    print(f"workers: {workers}")

    tr = Translator(site, rig)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    results = {"ok": 0, "fail": 0, "leased": 0}
    lock = threading.Lock()
    t_start = time.time()
    lease_state = {"checked": time.time(), "stop": False}

    def lease_gate():
        """Throttled bench-lease re-check between jobs: once a CrucibleForge/
        gauntlet lease appears mid-run, stop starting new jobs."""
        with lock:
            if lease_state["stop"]:
                return True
            if time.time() - lease_state["checked"] < 60:
                return False
            lease_state["checked"] = time.time()
        lease = rig.bench_lease()
        if lease:
            with lock:
                lease_state["stop"] = True
                lease_state["holder"] = lease.get("holder")
            return True
        return False

    def one(job):
        rel_src, lang, state, src_sha = job
        src_abs = os.path.join(site.root, rel_src)
        out_rel = sibling_path(rel_src, lang)
        out_abs = os.path.join(site.root, out_rel)
        t0 = time.time()
        if lease_gate():
            with lock:
                results["leased"] += 1
            print(f"SKIP [{state:>16}] {lang} {rel_src}: rig leased by "
                  f"'{lease_state.get('holder', '?')}' — backing off", file=sys.stderr)
            return
        try:
            source_text = read_text(src_abs)
            out_text = tr.translate_document(source_text, lang)
            with lock:
                backup_existing(site, out_abs, stamp)
            write_atomic(out_abs, out_text)
            dur = time.time() - t0
            manifest.set(rel_src, lang, {
                "src_sha": src_sha, "out_sha": sha256_text(out_text),
                "cfg": site.cfg_hash(), "model": site.model, "out": out_rel,
                "translated_at": now_iso(), "duration_s": round(dur, 1)})
            with lock:
                results["ok"] += 1
                manifest.save()
            print(f"OK   [{state:>16}] {lang} {rel_src} ({dur:.0f}s)")
            _, increased = glossary_deltas(source_text, out_text, site)
            if increased:
                print(f"WARN [{state:>16}] {lang} {rel_src}: glossary terms used "
                      "more often than the source (spot-check for bleed): "
                      + ", ".join(increased), file=sys.stderr)
        except Exception as e:
            with lock:
                results["fail"] += 1
            print(f"FAIL [{state:>16}] {lang} {rel_src}: {e}", file=sys.stderr)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, jobs))
    manifest.save()
    dur = time.time() - t_start
    print(f"done: {results['ok']} ok, {results['fail']} failed, "
          f"{results['leased']} deferred (rig leased) in {dur:.0f}s "
          f"({workers} workers)")
    if results["fail"]:
        return 1
    return 6 if results["leased"] else 0


def under_root(path, root):
    """True only for a real descendant of root — a plain startswith() would
    also match a sibling directory sharing the name prefix."""
    root = os.path.normpath(root)
    path = os.path.normpath(path)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def normalize_files(site, files):
    if not files:
        return None
    out = []
    for f in files:
        p = os.path.abspath(os.path.expanduser(f))
        rel = os.path.relpath(p, site.root) if under_root(p, site.root) else f
        out.append(rel)
    return out


# ------------------------------------------------------------ other cmds
STATE_ORDER = ["MISSING", "STALE", "EDITED", "UNTRACKED", "DRIFT", "CURRENT"]


def cmd_status(args, sites):
    targets = sites.values() if args.all or not args.site else [pick_site(sites, args.site)]
    exit_code = 0
    for site in targets:
        manifest = Manifest(site.name)
        rows = scan_site(site, manifest)
        prune_manifest(manifest, rows)
        counts = {}
        for (rel_src, lang), (state, _) in rows.items():
            counts.setdefault(lang, {s: 0 for s in STATE_ORDER})[state] += 1
        n_src = len({k[0] for k in rows})
        print(f"site {site.name}: {n_src} source pages x {len(site.languages)} "
              f"languages | model {site.model}")
        print(f"  {'lang':6} " + " ".join(f"{s.lower():>9}" for s in STATE_ORDER))
        for lang in site.languages:
            c = counts.get(lang, {s: 0 for s in STATE_ORDER})
            print(f"  {lang:6} " + " ".join(f"{c[s]:>9}" for s in STATE_ORDER))
        todo = sum(c["MISSING"] + c["STALE"] for c in counts.values())
        if todo:
            exit_code = 1
            if args.verbose:
                for (rel_src, lang), (state, _) in sorted(rows.items()):
                    if state in ("MISSING", "STALE", "EDITED"):
                        print(f"    {state:9} {lang} {rel_src}")
        print(f"  -> {todo} sibling(s) need translation"
              + ("" if todo else " — all current"))
    return exit_code


def cmd_plan(args, sites):
    site = pick_site(sites, args.site)
    manifest = Manifest(site.name)
    langs = args.langs or site.languages
    jobs, _ = build_jobs(site, manifest, langs, normalize_files(site, args.files),
                         args.force)
    for rel_src, lang, state, _ in jobs:
        print(f"{state:>16} {lang} {rel_src} -> {sibling_path(rel_src, lang)}")
    print(f"{len(jobs)} job(s)")
    return 0


def cmd_accept(args, sites):
    site = pick_site(sites, args.site)
    manifest = Manifest(site.name)
    files = normalize_files(site, args.files)
    langs = args.langs or site.languages
    n = 0
    for rel_src in discover_sources(site):
        if files and rel_src not in files:
            continue
        src_sha = sha256_text(read_text(os.path.join(site.root, rel_src)))
        for lang in langs:
            out_rel = sibling_path(rel_src, lang)
            out_abs = os.path.join(site.root, out_rel)
            if not os.path.isfile(out_abs):
                continue
            state, _ = classify(site, manifest, rel_src, lang)
            if state in ("UNTRACKED", "EDITED", "STALE") or (args.all and state != "CURRENT"):
                manifest.set(rel_src, lang, {
                    "src_sha": src_sha,
                    "out_sha": sha256_text(read_text(out_abs)),
                    "cfg": site.cfg_hash(), "model": "(accepted-existing)",
                    "out": out_rel, "translated_at": now_iso(),
                    "duration_s": 0, "accepted": True})
                n += 1
    manifest.save()
    print(f"accepted {n} existing sibling(s) as current")
    return 0


def cmd_verify(args, sites):
    site = pick_site(sites, args.site)
    bad = 0
    for rel_src in discover_sources(site):
        src = read_text(os.path.join(site.root, rel_src))
        for lang in site.languages:
            out_abs = os.path.join(site.root, sibling_path(rel_src, lang))
            if not os.path.isfile(out_abs):
                continue
            out_text = read_text(out_abs)
            problems = verify_structure(src, out_text, lang, site)
            if problems:
                bad += 1
                print(f"BAD {lang} {rel_src}: {'; '.join(problems)}")
            _, increased = glossary_deltas(src, out_text, site)
            if increased:
                print(f"WARN {lang} {rel_src}: glossary terms used more often "
                      f"than the source (spot-check for bleed): "
                      + ", ".join(increased))
    print(f"verify: {bad} problem file(s)" if bad else "verify: all structural checks pass")
    return 1 if bad else 0


def cmd_single(args, sites):
    # site auto-detect from path, else first site as parameter donor
    src_abs = os.path.abspath(os.path.expanduser(args.file))
    site = None
    for s in sites.values():
        if under_root(src_abs, s.root):
            site = s
            break
    site = site or (pick_site(sites, args.site) if args.site else next(iter(sites.values())))
    rig = Rig(site)
    if not args.no_warmup:
        warmup(site, rig, quiet=True)
    tr = Translator(site, rig)
    text = read_text(src_abs)
    out = tr.translate_document(text, args.lang)
    out_path = args.out or (os.path.splitext(src_abs)[0] + f".{args.lang}"
                            + os.path.splitext(src_abs)[1])
    write_atomic(out_path, out)
    print(f"OK {args.lang}: {out_path}")
    return 0


def cmd_warmup(args, sites):
    site = pick_site(sites, args.site)
    rig = Rig(site)
    workers = warmup(site, rig)
    print(f"ready: {site.model} — {workers} worker slot(s)")
    return 0


def cmd_models(args, sites):
    site = next(iter(sites.values())) if not args.site else pick_site(sites, args.site)
    rig = Rig(site)
    loaded = {r.get("model_id") or r.get("id"): r for r in rig.status().get("loaded", [])}
    for m in rig.models():
        mid = m.get("id")
        row = loaded.get(mid)
        if row:
            plan = row.get("plan") or {}
            print(f"LOADED  {mid}  parallel={plan.get('parallel')} "
                  f"ctx/slot={plan.get('ctx_per_slot') or plan.get('ctx_size')} "
                  f"state={row.get('state')}")
        else:
            print(f"        {mid}")
    return 0


def cmd_report(args, sites):
    lines = [f"## Translation scan (transforge) — {now_iso()}"]
    total_todo = 0
    for site in sites.values():
        manifest = Manifest(site.name)
        try:
            rows = scan_site(site, manifest)
        except Exception as e:
            lines.append(f"- {site.name}: SCAN FAILED — {e}")
            total_todo += 1
            continue
        missing = [(k, v) for k, v in rows.items() if v[0] == "MISSING"]
        stale = [(k, v) for k, v in rows.items() if v[0] == "STALE"]
        edited = [(k, v) for k, v in rows.items() if v[0] == "EDITED"]
        untracked = [(k, v) for k, v in rows.items() if v[0] == "UNTRACKED"]
        total_todo += len(missing) + len(stale)
        lines.append(f"- **{site.name}**: {len(rows)} pairs — "
                     f"{len(missing)} missing, {len(stale)} stale, "
                     f"{len(edited)} hand-edited (protected), "
                     f"{len(untracked)} untracked")
        for (rel_src, lang), _ in sorted(missing + stale)[:40]:
            state = rows[(rel_src, lang)][0]
            lines.append(f"  - {state} {lang}: {rel_src}")
    lines.append(f"- Fix: `transforge run --site <name>` (local model, no cloud). "
                 f"{total_todo} job(s) pending." if total_todo
                 else "- All translations current. Nothing to do.")
    text = "\n".join(lines) + "\n"
    if args.append:
        path = os.path.expanduser(args.append)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + text)
        print(f"appended report to {path}")
    else:
        print(text, end="")
    return 0


def cmd_config(args, sites):
    for name, site in sites.items():
        if args.site and name != args.site:
            continue
        print(f"[sites.{name}]  cfg_hash={site.cfg_hash()}")
        for k in ("root", "content_dirs", "languages", "model", "endpoint",
                  "concurrency", "ctx_per_slot", "prompt_template", "temperature",
                  "no_translate", "link_dirs"):
            print(f"  {k} = {getattr(site, k)}")
    return 0


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="transforge", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, langs=True, files=False):
        p.add_argument("--site")
        if langs:
            p.add_argument("--langs", nargs="*")
        if files:
            p.add_argument("--files", nargs="*")

    p = sub.add_parser("status"); common(p, langs=False)
    p.add_argument("--all", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("plan"); common(p, files=True)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("run"); common(p, files=True)
    p.add_argument("--force", action="store_true",
                   help="also (re)translate untracked/edited/current targets "
                        "(edited/current only with explicit --files)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--workers", type=int)
    p.add_argument("--no-warmup", action="store_true")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("single")
    p.add_argument("file"); p.add_argument("--lang", required=True)
    p.add_argument("--out"); p.add_argument("--site")
    p.add_argument("--no-warmup", action="store_true")
    p = sub.add_parser("accept"); common(p, files=True)
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("verify"); common(p, langs=False)
    p = sub.add_parser("warmup"); common(p, langs=False)
    p = sub.add_parser("models"); common(p, langs=False)
    p = sub.add_parser("report"); p.add_argument("--append")
    p = sub.add_parser("config"); p.add_argument("--site")

    args = ap.parse_args()
    _, sites = load_config()
    fn = {"status": cmd_status, "plan": cmd_plan, "run": cmd_run,
          "single": cmd_single, "accept": cmd_accept, "verify": cmd_verify,
          "warmup": cmd_warmup, "models": cmd_models, "report": cmd_report,
          "config": cmd_config}[args.cmd]
    sys.exit(fn(args, sites))


if __name__ == "__main__":
    main()
