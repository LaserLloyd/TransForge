# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (pre-1.0: minor bumps may change behaviour).

## 0.3.0 — 2026-11-01

### Added
- `transforge init` writes a starter `config.toml` (never overwrites without
  `--force`; `--name/--root/--languages/--endpoint/--model`) (#7).
- `transforge doctor` checks the config, site folders, the state directory, the
  API key source, that the rig answers and offers the configured model. It is
  read-only: no model is loaded and nothing is translated. Exit 0 or 1 (#8).
- `~/.local/state/transforge/transforge.log`: one JSON line per
  run/single/text/accept/warmup/review/init/doctor (command, exit code; no page
  text, no keys), rotated to `.1` at 1 MB (#8).

### Fixed
- `uv.lock` records 0.3.0, so `uv sync --locked` in the container build passes.

### Removed
- **The `X-MCP-Pin` credential** (`STUDIOFORGE_MCP_PIN`, `TRANSFORGE_PIN_ENV_FILE`),
  deprecated in 0.2.0. Both variables are now ignored. The StudioForge API key
  (`TRANSFORGE_LLM_API_KEY`, `STUDIOFORGE_API_KEY`, or `api_key_env` /
  `api_key` in the config) is the only credential; a model load/unload without
  one is refused locally with a message naming it. The compose files no longer
  pass the PIN through.

### Changed
- **Lease check is no longer silently fail-open on a bad 200.** A `/api/leases`
  answer of 200 whose body is not a JSON object with a list under `leases`
  (an HTML page, a JSON error object from a server that is not StudioForge, a
  list or scalar) still reads as "no lease", but now prints one warning per
  process on stderr naming the endpoint. A non-list `leases` value no longer
  raises `TypeError`. 401/403/3xx still fail closed (exit 2); unreachable, 404
  and 5xx are unchanged.
- Docker: the entrypoint refuses to start when the config template is missing
  from the image (exit 1) instead of leaving an empty `config.toml` that was
  never re-seeded; `LLM_BASE_URL` values containing `|`, `&` or `\` no longer
  corrupt the seeded config; the file is written via a temp file and renamed.
- Docker: the compose healthcheck's trade-off is documented in `compose.yml`
  and the entrypoint — "healthy" proves the scheduler loop is alive, not that a
  run is progressing; read the container log for run outcomes.
- `scripts/scrub_check.py`: new `scrub-ok-next` marker suppresses the following
  line (file scan only), for lines that cannot carry a trailing comment such as
  a backslash-continued Dockerfile `LABEL`.
- README: `--no-warmup` is documented accurately (`run` still reads the lease
  list once before the first job; `single`/`text` make no lease check); the
  "two files" sentence describes the program, not the repository.
- `SECURITY.md`: GitHub private vulnerability reporting is the reporting
  channel (the placeholder contact is gone).
- `docs/deploy/docker.md`: no image is published yet; the first `v*` tag
  publishes to GHCR. The Dockerfile's documentation label pointed at `blob/main`
  on a repository whose default branch is `master`.
- `.gitignore`: `.scratch-data/`, `.pytest_cache/`.
- **Licence: MIT No Attribution (MIT-0).** You can use, copy, change and ship
  TransForge with no conditions; no copyright notice has to be kept. You don't
  need to credit this project — but if you do, a link back is appreciated.
  `LICENSE`, `pyproject.toml`, the Docker image label and the README say so.
  Releases up to 0.2.0 were MIT.

## 0.2.0

### Added
- **StudioForge API key.** `Authorization: Bearer <key>` is now sent on every
  StudioForge request (models, status, leases, chat, load, unload). Sources, in
  order: the env var named by `api_key_env`, `TRANSFORGE_LLM_API_KEY`,
  `STUDIOFORGE_API_KEY`, then `api_key` in the config. `transforge config`
  reports where the key came from, never its value. The key is not part of the
  translation config hash (rotating it does not mark pages STALE).
- Contract tests pinning the StudioForge fields TransForge depends on
  (`tests/test_contract_studioforge.py`, fixtures in `tests/contract_studioforge/`).
- Hermetic test suite: a socket guard (`tests/_netguard.py`) refuses any connect
  except to ephemeral loopback ports the tests open themselves; tests talk to an
  in-process fake StudioForge (`tests/_fakesf.py`).
- Windows CI job.
- `SECURITY.md`.

### Changed
- **Lease check fails closed on auth errors.** A 401/403 from `/api/leases` used
  to read as "no lease" (silently disabling benchmark protection); it now stops
  the run with a clear message and exit code 2. An unreachable StudioForge, a
  missing `/api/leases` (404) or a 5xx still read as "no lease" (documented in
  `Rig.leases`).
- Model load/unload no longer requires a PIN when an API key is configured.
- 401/403 responses print a hint naming `TRANSFORGE_LLM_API_KEY` and exit with
  code 2 on every request (not only the lease list); other HTTP refusals keep
  exit code 1.
- `models` and `warmup` report an unreachable StudioForge as `rig unreachable`
  (exit code 4) and a refusal as one line (exit 1/2) instead of a traceback.
- Docker: `TRANSFORGE_LLM_API_KEY` is passed through to the container
  (compose, `.env.example`, docs); the `schedule` service's heartbeat keeps
  ticking during a long translation run, so the healthcheck no longer flips
  to unhealthy while a run takes longer than two minutes.
- README: the install section explains the virtual-environment requirement on
  Debian/Ubuntu (PEP 668) and the Windows equivalent.

### Fixed
- Docker: a rebuild after a source-only change (`transforge.py` / `review.py`
  edited, version unchanged) reused uv's cached wheel from the build-cache
  mount and shipped the previous code. `[tool.uv] cache-keys` now includes the
  two source files and the builder reinstalls the project on every build.
- On Windows, discovered page paths are normalised (`os.path.normpath`), so
  `--files content/projects/foo.md` matches.
- Manifest keys are `/`-separated on every OS. Keys from an earlier Windows run
  (mixed `/` and `\`), backslash keys and `./`-prefixed keys are re-keyed on
  load with their history; they used to read as UNTRACKED, get pruned by the
  next `status`/`plan`/`run`, and the suggested `accept --all` then recorded
  STALE siblings as current.
- The LLM client no longer follows HTTP redirects: urllib re-sent the bearer
  key and the PIN to whatever host a 3xx named. A redirected lease list now
  fails closed (exit 2), like a 401/403.
- `run`: a lease list refused mid-run exits 2 (was 6, "leased"); `--no-warmup`
  reads the lease list before the first job (it used to start a minute of jobs
  unchecked); 401/403 on load-recommended exits 2 with the PIN hint (was 1).
- Docker: the PUID override (`compose.puid.yml`) works for UIDs other than
  1000 (`setpriv --init-groups` needed a passwd entry; `/data` was 0750), and
  a failing manifest scan no longer ends the entrypoint without a message.
- The test suite is platform-aware (path separators, POSIX file modes) and is
  green on Windows (221 pre-existing tests, 11 previously failing).

### Deprecated
- `X-MCP-Pin` on model load/unload (`STUDIOFORGE_MCP_PIN`,
  `TRANSFORGE_PIN_ENV_FILE`). Still sent when available; to be removed in the
  release after 0.2.0. Use the API key instead. A load/unload that rides on
  the PIN alone prints a one-time deprecation warning on stderr.

## 0.1.0
- Initial release.
