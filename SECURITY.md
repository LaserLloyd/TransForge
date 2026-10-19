# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem.

- Contact: **[placeholder: the maintainer sets a private security contact here
  before the first public release]**
- If the repository is hosted on GitHub, use "Report a vulnerability" (private
  vulnerability reporting) under the Security tab once it is enabled.

Include the TransForge version (`pyproject.toml`), your OS, and the smallest
config that reproduces the problem. Do not attach real API keys, StudioForge
keys, or page content you do not have the right to share.

## Supported versions

Only the latest release receives fixes while the project is pre-1.0.

## What TransForge does with secrets

- **StudioForge API key** (`TRANSFORGE_LLM_API_KEY`, or `STUDIOFORGE_API_KEY`,
  or `api_key_env` / `api_key` in the config) is sent as
  `Authorization: Bearer <key>` to the configured `endpoint` only. It is never
  logged, printed by `transforge config`, or included in error messages.
  Prefer the environment variable: `config.toml` is a plain file.
- **`X-MCP-Pin`** on model load/unload is deprecated and will be removed in the
  release after 0.2.0. It is read from `STUDIOFORGE_MCP_PIN` (or the opt-in file
  named by `TRANSFORGE_PIN_ENV_FILE`) and is sent only on load/unload.
- **Legacy MCP PIN** is set and rotated in StudioForge (`mcp.pin` setting) and is scheduled for removal; do not rely on it for new setups.
- **Cloud `review` stage** is off by default; when enabled it sends the
  above-the-fold part of each page to the configured API, with a key read from
  the environment variable named by `api_key_env` in `[review]`.
- TransForge has no listener and makes no network request other than to the
  configured `endpoint` (and `[review].base_url` when enabled).

## Fail-closed lease check

Before it uses the GPUs, TransForge reads StudioForge's lease list to avoid
disturbing a running benchmark. If StudioForge refuses that read (HTTP 401/403)
TransForge stops (exit code 2) instead of assuming "no lease". An *unreachable*
StudioForge, a server without `/api/leases`, or a 5xx there still reads as "no
lease" because there is then no gate to consult; the next call fails with the
real error (exit code 4).
