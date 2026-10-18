# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) (pre-1.0: minor bumps may change behaviour).

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
- On Windows, discovered page paths are normalised (`os.path.normpath`), so
  `--files content/projects/foo.md` matches and manifest keys no longer mix `/`
  and `\`. Manifests written by an earlier Windows run used mixed separators;
  re-run `transforge accept --site <name> --all` once to rebuild them.
- The test suite is platform-aware (path separators, POSIX file modes) and is
  green on Windows (221 pre-existing tests, 11 previously failing).

### Deprecated
- `X-MCP-Pin` on model load/unload (`STUDIOFORGE_MCP_PIN`,
  `TRANSFORGE_PIN_ENV_FILE`). Still sent when available; to be removed in the
  release after 0.2.0. Use the API key instead.

## 0.1.0
- Initial release.
