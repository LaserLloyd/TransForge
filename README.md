# TransForge

Config-driven website translator that runs entirely against a local StudioForge
LLM server (the rig, `http://localhost:1234`). No cloud API, no per-word cost.

It discovers English source pages in a site tree, decides which language
siblings are missing or stale using a **content-hash manifest** (never mtimes),
translates only those, verifies the output structurally, and writes siblings
atomically. A hand-edited sibling is never overwritten without `--force`.

Single file: `transforge.py` (installed as `~/.local/bin/transforge`).
Requires Python 3.11+ (`tomllib`) and PyYAML.

- Config: `~/.config/transforge/config.toml`
- State: `~/.local/state/transforge/` — `<site>-manifest.json`, `backups/<site>/<stamp>/`

Sibling naming: `content/projects/foo.md` → `content/projects/foo.ja.md`.

## Config reference

`[defaults]` applies to every site; any key may be repeated inside
`[sites.<name>]` to override it for that site only.

| Key | Default | Meaning |
|---|---|---|
| `endpoint` | `http://localhost:1234` | StudioForge base URL |
| `model` | `unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-Q5_K_S` | Full StudioForge model id (`publisher/repo/file-stem`) |
| `concurrency` | `"auto"` | `"auto"` = follow the server's parallel slots; an integer caps it (still clamped to the live slot count) |
| `ctx_per_slot` | `16384` | Context per slot requested at warmup |
| `temperature` | `0.3` | Sampling temperature |
| `api_timeout` | `300` | Seconds per `/v1/chat/completions` call |
| `priority` | `3` | Load priority tier — background, never displaces chat models |
| `disable_thinking` | `true` | Sends `chat_template_kwargs.enable_thinking = false` |
| `prompt_template` | `"instruct"` | `"instruct"` for general instruct models, `"hunyuan-mt"` for pure-MT models (Hy-MT2 etc.) |
| `max_tokens_fm` | `8192` | Token budget for a frontmatter call |
| `max_tokens_fm_retry` | `24000` | Budget on the frontmatter retry |
| `max_tokens_body` | `12288` | Token budget for a body chunk |
| `max_tokens_body_retry` | `24000` | Budget on a body-chunk retry |
| `max_chunk_chars` | `12000` | Chunk size ceiling before a body is split further |
| `extra_params` | `{}` | Merged verbatim into the completion payload |

Site keys (only meaningful per-site):

| Key | Meaning |
|---|---|
| `root` | Site tree root (`~` expanded) |
| `content_dirs` | Directories scanned for sources, relative to `root`. Not recursive |
| `pages_dir` | Where standalone pages live; used when resolving bare `/dir/` links |
| `languages` | Target language codes; each source gets one sibling per code |
| `lang_names` | Code → language name used in the prompt (`ja = "Japanese"`) |
| `link_dirs` | URL path segments treated as internal links (`projects`, `about`, …) |
| `site_description` | One-paragraph context and tone note injected into prompts |
| `no_translate` | Brand/technical terms kept verbatim |
| `translate_fields` | Frontmatter keys to translate; all others are copied through |
| `list_fields` | Subset of `translate_fields` that are arrays; item count must be preserved |
| `style` | Per-language convention notes (`ja = "Use polite です/ます調 form."`) |

## Manifest and staleness model

The manifest keys `<rel_source>|<lang>` → `{src_sha, out_sha, cfg, model, out,
translated_at, duration_s}`. `cfg` is `cfg_hash()`: a hash of everything that
changes output — model, prompt template, `no_translate`, `style`,
`site_description`, `translate_fields`, `temperature`. It deliberately ignores
transport settings such as `concurrency`, `endpoint` and token budgets.

State is derived per (source, language), checked in this order:

| State | Trigger | Translated by `run`? |
|---|---|---|
| `MISSING` | Sibling file does not exist | yes |
| `UNTRACKED` | Sibling exists but has no manifest entry | only with `--force` |
| `EDITED` | Sibling's current sha ≠ manifest `out_sha` — a human changed it | only with `--force` **and** explicit `--files` |
| `STALE` | Sibling matches the manifest, but the source sha changed | yes |
| `DRIFT` | Source and sibling both match, but `cfg` ≠ current `cfg_hash()` (model or prompt inputs changed) | informational only; `--force` + `--files` |
| `CURRENT` | Everything matches | no |

`EDITED` is checked before `STALE`, so a hand-edited sibling stays protected
even when its source also changed.

`accept` records existing siblings as current without translating them — the way
to adopt a tree that was translated by something else, or to bless a hand edit.

Existing siblings are copied to `~/.local/state/transforge/backups/<site>/<stamp>/`
before being overwritten.

## Commands

```
transforge status [--site X | --all] [-v]     per-language state table
transforge plan   [--site X] [--langs ..] [--files ..] [--force]
transforge run    [--site X] [--langs ..] [--files ..] [--force] [--dry-run]
                  [--workers N] [--no-warmup] [--limit N]
transforge single FILE --lang L [--out PATH] [--site X] [--no-warmup]
transforge accept [--site X] [--langs ..] [--files ..] [--all]
transforge verify [--site X]                  re-run structural checks on outputs
transforge warmup [--site X]                  load the model on a sane plan
transforge models                             rig models, with loaded plans
transforge report [--append FILE]             one-shot text report (cron-friendly)
transforge config [--site X]                  resolved config + cfg_hash
```

`--site` may be omitted when the config defines exactly one site.

Examples:

```bash
transforge status --all
transforge plan --site laserlloyd --langs ja zh
transforge run --site laserlloyd --langs ja --limit 3
transforge run --site laserlloyd --dry-run
transforge run --site laserlloyd --files content/projects/foo.md --langs ja --force
transforge single ~/notes/page.md --lang de --out /tmp/page.de.md
transforge accept --site laserlloyd --all
transforge verify --site laserlloyd
transforge report --append ~/reports/translations.md
```

## Switching models

Edit `model` in `~/.config/transforge/config.toml` — under `[defaults]` to move
every site, or inside a `[sites.<name>]` block for one site. It must be the
**full StudioForge id** (`publisher/repo/file-stem`), e.g.

```toml
model = "mradermacher/Hy-MT2-30B-A3B-i1-GGUF/Hy-MT2-30B-A3B.i1-Q4_K_S"
prompt_template = "hunyuan-mt"   # pure-MT models take the short MT prompt
```

`transforge models` lists what the rig actually serves; an unknown id exits 5
with the nearest matches. Changing the model changes `cfg_hash()`, so previously
translated pages report `DRIFT` — they are **not** retranslated automatically.
Re-run them deliberately with `--force --files ...`, or leave them.

## Warmup, concurrency, leases

`warmup` (run automatically by `run` and `single` unless `--no-warmup`) does:

1. Check `/api/leases`. If a lease is held by `crucibleforge` or `gauntlet`, it
   prints the holder and exits **6** without touching the rig — benchmark runs
   are never disturbed.
2. Confirm the model is in `/v1/models`; exit 5 if not.
3. Inspect the loaded plan. If the model is already resident with
   `parallel >= 2` (or `>= 1` when `concurrency` is an integer), keep it.
4. Otherwise unload a degenerate resident (a leftover JIT single-slot load —
   `load-recommended` would return that plan unchanged) and call
   `load-recommended` with `ctx_size = ctx_per_slot` and `priority = 3`, so the
   load queues behind chat models rather than evicting them. `503`/`507`
   responses carrying `retry_after_s` are honoured, up to 6 attempts.
5. Wait for `state == "ready"` (up to 900 s).

Worker count is the model's live `parallel` slot count when
`concurrency = "auto"`, otherwise `min(concurrency, parallel)`. `--workers N`
overrides it. Smaller `ctx_per_slot` generally buys more slots.

Steps 3–4 need `STUDIOFORGE_MCP_PIN`, read from
`~/.openclaw/gateway.systemd.env` and sent as `X-MCP-Pin`. Without it TransForge
warns and runs serially against whatever plan the rig chooses.

## Structural verification

Every translated document is checked before it is written; a failure aborts that
file and leaves the old sibling in place. Checks: equal counts of h2/h3
headings, `<svg>`/`</svg>`, `<pre><code>`/`</code></pre>`, `<code>`, tables,
figures, `<img>`, `href="`, `src="`, `d="`, `<text>`; internal-link accounting;
target-script presence (CJK for ja/zh, Arabic for ar, Hangul for ko); and an
output-size floor of `max(200, 0.35 × source)` characters.

Internal links are rewritten deterministically **after** the model runs, never
by it: `/projects/foo/` becomes `/ja/projects/foo/` only when
`content/projects/foo.ja.md` (or `<pages_dir>/foo.ja.md`) exists on disk.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success / nothing to do |
| 1 | Translation or verification failures (also `status` when work is pending) |
| 2 | Usage or config error |
| 4 | Rig unreachable |
| 5 | Configured model not present on the rig |
| 6 | Rig leased by a benchmark holder — backed off, nothing done |

## Tests

```bash
python3 -m unittest discover -s tests
```

Offline only: frontmatter splitting, chunking, structural verification, link
rewriting, manifest round-trip, the state machine, discovery and config hashing.
No test touches the network.
