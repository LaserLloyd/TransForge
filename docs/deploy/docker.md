# Running TransForge in Docker

TransForge is a batch command-line tool: it reads a site tree, asks your local
LLM server to translate what is missing or stale, verifies the output and writes
sibling files. It has no web interface and no listening port. The image is the
tool plus its single dependency; you run it against a mounted site tree.

```bash
git clone https://github.com/LaserLloyd/TransForge.git && cd TransForge     # scrub-ok: public repo URL  (or: git pull)
mkdir -p deploy/compose/config deploy/compose/sites                          # Linux: also chown 1000:1000 both
cp -r /path/to/your/site deploy/compose/sites/example                        # or set SITES_PATH
docker compose -f deploy/compose/compose.yml run --rm app status --all
```

The first command writes `deploy/compose/config/config.toml` from a template
whose `endpoint` is your LLM server (see below) and which has one site,
`example`, rooted at `/sites/example`. Edit that file to match your site
(`content_dirs`, `languages`, `model`, ...; see the README's Config reference),
then:

```bash
docker compose -f deploy/compose/compose.yml run --rm app plan   --site example
docker compose -f deploy/compose/compose.yml run --rm app run    --site example --dry-run
docker compose -f deploy/compose/compose.yml run --rm app run    --site example --limit 3
```

## What is mounted where

| In the container | Host side (default) | Why |
|---|---|---|
| `/data/.config/transforge` | `deploy/compose/config` (`CONFIG_PATH`) | `config.toml`; a host directory so you can edit it with your editor |
| `/sites` | `deploy/compose/sites` (`SITES_PATH`) | the site trees; **written in place** (siblings such as `foo.ja.md` appear next to `foo.md`) |
| `/data/.local/state/transforge` | named volume `transforge-state` | manifests and backups. Keep it: it is what makes re-runs incremental and protects hand-edited translations |

TransForge hard-codes `~/.config/transforge/config.toml` and
`~/.local/state/transforge`; the image sets `HOME=/data` so those resolve to the
mounts above. In `config.toml` write site roots as container paths
(`root = "/sites/example"`).

On Linux the container runs as UID 1000. If you let Docker create the host
directories they are owned by root and the seed step cannot write; create them
yourself first (`mkdir`, then `chown 1000:1000` if your user is not UID 1000).

## Ports

None. TransForge opens no listening socket (it is a client of your LLM server).

## Configuration

`deploy/compose/.env` (copy `.env.example`; never commit it, keep it `0600`):

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `http://host.docker.internal:1234/v1` | Your StudioForge / llama.cpp server **as seen from the container**. TransForge has no environment variable of its own for this (it is `endpoint` in `config.toml`), so it is applied once, when the config is first seeded; a trailing `/v1` is dropped because TransForge wants the server root. Edit `endpoint` in `config.toml` afterwards. |
| `TRANSFORGE_LLM_API_KEY` | empty | StudioForge API key, sent as `Authorization: Bearer` on every request. A 401/403 from the server stops the run with exit code 2 and a hint. |
| `STUDIOFORGE_MCP_PIN` | empty | Legacy StudioForge management PIN for `warmup` / `load-recommended` (deprecated; not needed with an API key). Without either TransForge warns and runs serially on whatever plan the server chose. |
| `DEEPSEEK_API_KEY` | empty | Only for the opt-in `transforge review` stage; the variable *name* must match `[review] api_key_env`. |
| `CONFIG_PATH`, `SITES_PATH` | `./config`, `./sites` | Host paths (relative to `deploy/compose/`). |
| `MEM_LIMIT` / `CPU_LIMIT` | `512m` / `1.0` | Resource limits. |

### Pointing it at StudioForge or LM Studio on the host

`host.docker.internal` is the Docker host (Docker Desktop provides it; the
compose file adds `host-gateway` for Linux engines). The server must listen on
an address the container can reach, not only on the host's `127.0.0.1`.
TransForge is written against a StudioForge/llama.cpp server; see "Warmup,
concurrency, leases" in the README for the endpoints it uses.

## Running commands

```bash
docker compose -f deploy/compose/compose.yml run --rm app --help
docker compose -f deploy/compose/compose.yml run --rm app models
docker compose -f deploy/compose/compose.yml run --rm app run --site example --langs ja
echo "Thanks for reading." | docker compose -f deploy/compose/compose.yml run --rm -T app text --to ja
```

A first argument that is not an executable is treated as a `transforge`
subcommand. Exit codes are TransForge's own (README, "Exit codes").

## Scheduled runs (optional)

The `schedule` service re-runs one command on an interval and is the only part
with `restart: unless-stopped`:

```bash
TRANSFORGE_SCHEDULE_CMD="run --site example" TRANSFORGE_INTERVAL=3600 \
  docker compose -f deploy/compose/compose.yml --profile schedule up -d
docker compose -f deploy/compose/compose.yml --profile schedule logs -f schedule
```

Its healthcheck watches a heartbeat file, not the translation result: a failing
run is logged and retried at the next interval. Put the two variables in `.env`
to make them permanent. Stop it with `... --profile schedule down`.

## Your data, upgrades, backups

TransForge keeps its state in three places, all outside the image (its root
filesystem is read-only and `/tmp` is a tmpfs, so a stray write fails loudly):

| What | Where |
|---|---|
| `config.toml` | host directory `CONFIG_PATH` (default `deploy/compose/config`) |
| manifests (`<site>-manifest.json`) and the backups of replaced translations | named volume `transforge-state` |
| your translated pages | your site tree, `SITES_PATH`, written in place |

| Command | Effect on your data |
|---|---|
| `docker compose ... down` | stops and removes the containers; **the state volume is kept** |
| `docker compose ... down -v` | **also deletes `transforge-state`: manifests and replaced-file backups are gone** (translations in your site tree stay; TransForge then treats existing siblings as `UNTRACKED` until you run `accept`) |
| `docker compose ... build` after `git pull` | new image, same mounts: data intact |

### Upgrades

```bash
git pull
docker compose -f deploy/compose/compose.yml build
```

On the first start of a new image version the entrypoint copies `config.toml` and
the manifests to `<state>/image-backups/<old>-to-<new>-<timestamp>/` (inside the
state volume; last 5 kept). TransForge's own `<state>/backups/` (files it replaced
while translating) is separate and untouched. The version compared is `<package
version>-<VERSION build arg>`; pass `--build-arg VERSION=...` when building locally
if you want a backup on every rebuild.

### Back up and restore the state volume

Stop the scheduler first if you use it. The volume is named
`transforge_transforge-state` (`docker volume ls`).

```bash
docker compose -f deploy/compose/compose.yml --profile schedule stop
docker run --rm -v transforge_transforge-state:/data:ro -v "$PWD":/backup alpine \
    tar czf /backup/transforge-state-$(date +%F).tgz -C /data .
```

Restore (replaces the volume's contents):

```bash
docker compose -f deploy/compose/compose.yml --profile schedule down
docker run --rm -v transforge_transforge-state:/data -v "$PWD":/backup alpine \
    sh -c 'rm -rf /data/* /data/.[!.]* && tar xzf /backup/transforge-state-YYYY-MM-DD.tgz -C /data'
```

Back up `CONFIG_PATH` and your site tree with your usual tools (they are plain
host directories; a site tree is normally in git anyway).

### Volume permissions (bind mounts)

The image runs as UID/GID 1000, so `config/` and `sites/` must be writable by it
(the sites tree because translations are written next to the sources). A named
volume (`transforge-state`) just works. If a bind mount belongs to someone else the
container stops at once with a message saying so. Either `chown` it, or run as your
user:

```bash
PUID=$(id -u) PGID=$(id -g) docker compose \
    -f deploy/compose/compose.yml -f deploy/compose/compose.puid.yml run --rm app status --all
```

That override starts the container as root only to `chown` the config and state
directories (never `/sites`: make your site tree writable by `PUID` yourself) and
drop to `PUID:PGID` (`setpriv`); the app process runs with no capabilities.

## Image

Built `FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim` and run on
`python:3.13-slim-trixie` (same interpreter at the same path, uv not in the
runtime image), non-root UID 1000, tini via `init: true`. There is no
`HEALTHCHECK` in the image (no HTTP surface). Tagged releases are published as
`ghcr.io/laserlloyd/transforge:<version>` with a CycloneDX SBOM, SHA256SUMS, a
cosign keyless signature and build-provenance attestation (see
`.github/workflows/containers.yml`).
