# Minecraft Server (Paper) on Docker Compose

[![Deployment Verification](https://github.com/heyvaldemar/minecraft-server-docker-compose/actions/workflows/deployment-verification.yml/badge.svg?branch=main)](https://github.com/heyvaldemar/minecraft-server-docker-compose/actions/workflows/deployment-verification.yml)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/14890/badge)](https://www.bestpractices.dev/projects/14890)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Contents

- [Why this stack?](#why-this-stack)
- [Prerequisites](#prerequisites)
- [Getting started](#getting-started)
- [Features](#features)
- [Plugins](#plugins)
- [Supply chain trust](#supply-chain-trust)
- [Production checklist](#production-checklist)
- [Backups](#backups)
- [Scale-to-zero](#scale-to-zero-sleep-when-idle)
- [Admin panel](#admin-panel)
- [Testing](#testing)
- [About the maintainer](#about-the-maintainer)

This repository deploys a Paper Minecraft server with automatic plugin installation (Modrinth + direct URLs, Geyser/Floodgate for Bedrock crossplay out of the box) and a scheduled world backup container. One `docker compose up` away from a survival server your friends can join.

📙 Full narrative installation guide on the blog: [heyvaldemar.com/install-minecraft-server-using-docker-compose/](https://www.heyvaldemar.com/install-minecraft-server-using-docker-compose/).

## Why this stack?

| Need | This stack | Manual install | Other compose examples |
|------|-----------|----------------|------------------------|
| Ready to deploy in <10 min | ✅ | ❌ java, jars, systemd | Often |
| Plugins auto-installed on start | ✅ Modrinth + URL lists | Manual downloads | Rare |
| Bedrock crossplay (Geyser/Floodgate) preconfigured | ✅ | Manual setup | Rare |
| Scheduled world backups + pruning | ✅ RCON-coordinated `mc-backup` | Manual cron + save-off dance | Rare |
| Upstream images pinned by `sha256` digest | ✅ | N/A | Almost never (usually `latest`) |
| Weekly pin-freshness check in CI | ✅ | N/A | Rare |
| CI-verified deployment on every push | ✅ boots a real server | N/A | Rare |
| Every setting tunable via env | ✅ 40+ knobs with sane defaults | server.properties by hand | Varies |

Six moving parts (socket proxy, router, server, admin panel, backup watcher, backups sidecar). The heavy lifting comes from the excellent [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server), [`itzg/mc-router`](https://github.com/itzg/mc-router) and [`itzg/mc-backup`](https://github.com/itzg/docker-mc-backup) images, a [`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy) that walls off the Docker socket, a tiny busybox watcher that backs up the world on shutdown, and a single standard-library Python script that serves the admin panel on loopback - all pinned and wired together.

## Prerequisites

- **A server** (Linux recommended) with Docker Engine 24+ and Docker Compose 2.20+.
- **~3 GB free RAM** (2 GB heap default + overhead) and a CPU core or two; more for many players or heavy plugins.
- **Port 25565 open** (TCP) on the firewall for Java Edition; Geyser's Bedrock port needs extra config if you use it beyond LAN.
- **Disk for the world and backups**: worlds grow; the default retention keeps 7 days of archives.

## Getting started

```bash
# 1. Clone
git clone https://github.com/heyvaldemar/minecraft-server-docker-compose
cd minecraft-server-docker-compose

# 2. Create the Docker network the stack expects
docker network create minecraft-server-network

# 3. Copy the environment template and set the RCON password and the router hostname
cp .env.example .env
$EDITOR .env
# ^ Required: MINECRAFT_SERVER_RCON_PASSWORD. Everything else has defaults.
#   Set MINECRAFT_SERVER_ROUTER_HOST to the hostname you will tell players -
#   until then the router refuses every connection and the server never wakes.
#   Keeping MINECRAFT_SERVER_EULA=true means you accept the Minecraft EULA.

# 4. Deploy
docker compose -f minecraft-server-docker-compose.yml -p minecraft up -d
```

First boot downloads the Paper jar and the configured plugins, then generates the world. Give it a few minutes. Players connect to `<MINECRAFT_SERVER_ROUTER_HOST>:25565` - the hostname you set in `.env`, not the raw IP. The router refuses connections that do not name a mapped server, which is also why an unconfigured deployment costs nothing: nobody can wake the server.

### What success looks like

```bash
# The server container turns healthy once it accepts connections:
docker compose -f minecraft-server-docker-compose.yml -p minecraft ps

# RCON works (lists online players):
docker compose -p minecraft exec minecraft-server rcon-cli list

# Watch the boot log:
docker compose -p minecraft logs -f minecraft-server

# Backups land in ./minecraft-server-data-backups on the schedule:
ls minecraft-server-data-backups/
```

### Common first-deploy issues

- **Container exits immediately with an EULA message.** `MINECRAFT_SERVER_EULA` must be `true` (you are accepting the [Minecraft EULA](https://www.minecraft.net/eula)).
- **`docker compose up` fails with `set in .env`.** `MINECRAFT_SERVER_RCON_PASSWORD` is empty: generate one per `.env.example`.
- **`network minecraft-server-network not found`.** Step 2 was skipped.
- **Slow first start.** Paper jar + plugins download once; later starts are much faster.

### Apply `.env` or compose-file changes

```bash
docker compose -f minecraft-server-docker-compose.yml -p minecraft up -d --force-recreate
```

## Features

- **Paper server** (`TYPE=PAPER`, `VERSION=LATEST` by default: pin a game version via `MINECRAFT_SERVER_VERSION` for stability).
- **Automatic plugin install** from Modrinth project slugs and direct download URLs, with dependency resolution.
- **Bedrock crossplay ready**: Floodgate ships in the default plugin list; pair with Geyser to let Bedrock players join.
- **RCON enabled** for admin commands (`rcon-cli` inside the container) and coordinated backups.
- **RCON-coordinated backups**: `mc-backup` runs `save-off`/`save-all` around each archive so world saves are consistent, then prunes archives older than the retention window.
- **40+ game settings** (mode, difficulty, view distance, whitelist, ops, world type…) exposed as env vars with compose-level defaults.
- **Local bind mounts**: world data in `./minecraft-server-data`, archives in `./minecraft-server-data-backups`, custom plugin jars in `./plugins`.

## Plugins

Two mechanisms, combinable:

- `MINECRAFT_SERVER_MODRINTH_PROJECTS`: comma-separated [Modrinth](https://modrinth.com/plugins) slugs (default: `viaversion,viabackwards,skinsrestorer`), with `MODRINTH_DOWNLOAD_DEPENDENCIES=required`.
- `MINECRAFT_SERVER_PLUGINS`: newline/comma-separated direct jar URLs (default: latest Floodgate build).
- Drop `.jar` files into `./plugins/` for anything not available by URL.

Plugins are re-resolved on every container start, so version bumps arrive with a `--force-recreate`.

### Before you move to a new Minecraft version

A Minecraft release always outpaces its plugin ecosystem. `VERSION` defaults to `LATEST`, so the server takes the new one the moment it appears — and if the plugins pinned in `MODRINTH_PROJECTS` have no build for it yet, it exits during plugin resolution, in a loop, with its version already bumped and its backup already taken. A world converted to a version its plugins cannot load is converted either way.

```bash
tools/plugins-for-version.sh            # what LATEST resolves to today
tools/plugins-for-version.sh 1.21.4     # or a version you are considering
```

It reads the project list out of the compose file, so it cannot drift from what the server would actually download, and it exits `2` rather than `1` when Modrinth does not answer: a question that could not be asked is not a no. The daily verification runs it, so the answer arrives before anybody restarts anything.

## Supply chain trust

This repository is a deployment template orchestrating six upstream images:

- [`itzg/minecraft-server`](https://github.com/itzg/docker-minecraft-server): the de-facto standard Minecraft server image
- [`itzg/mc-router`](https://github.com/itzg/mc-router): the scale-to-zero router in front of it
- [`itzg/mc-backup`](https://github.com/itzg/docker-mc-backup): the companion backup sidecar
- [`tecnativa/docker-socket-proxy`](https://github.com/Tecnativa/docker-socket-proxy): the filtered Docker API wall the router and watcher go through
- [`busybox`](https://hub.docker.com/_/busybox): the tiny base for the shutdown-backup watcher script
- [`python`](https://hub.docker.com/_/python): the runtime for the admin panel, which is one standard-library-only script with no installed dependencies

All six are pinned to `tag@sha256:<digest>` as interpolation defaults in the compose file's `x-images` block: `git pull` alone delivers the version combination this repository has tested. Setting an `*_IMAGE_TAG` variable in `.env` overrides the default. The daily `check-pin-freshness` CI job re-resolves every pinned tag against Docker Hub and compares the pinned versions against the latest itzg releases: any drift fails the run and notifies the maintainer. GitHub Actions are pinned by commit SHA; Dependabot keeps those fresh.

Two override levels exist per image. `<PREFIX>_IMAGE_VERSION` in `.env` swaps only the version of that image (Compose then pulls the tag, without a digest) and leaves every other pin as tested; `<PREFIX>_IMAGE_TAG` replaces the whole reference, digest included. The variable names are listed in `.env.example`. Nested defaults need Docker Compose v2.5 or newer (2022); v2.0 to v2.4 leave the inner `${...}` unexpanded and `docker compose up` fails with an invalid reference instead of deploying something unexpected.

Note the deliberate trade-off: the image is pinned for reproducibility, while `VERSION=LATEST` floats the game version by default. Pin `MINECRAFT_SERVER_VERSION` too if plugin compatibility matters to you.

### Verify what you deploy

Every release from v1.6.0 on carries three files made on GitHub's runner with a short-lived identity and no stored key: `minecraft-server-docker-compose-<tag>.tar.gz`, a `git archive` of exactly the tree the tag points at; `minecraft-server-docker-compose-<tag>.tar.gz.sigstore.json`, a keyless [Sigstore](https://www.sigstore.dev/) signature over it; and `minecraft-server-docker-compose-<tag>.intoto.jsonl`, [SLSA](https://slsa.dev/) build provenance from the SLSA generator. To check them with nothing from this repository trusted:

```bash
cosign verify-blob minecraft-server-docker-compose-<tag>.tar.gz \
  --bundle minecraft-server-docker-compose-<tag>.tar.gz.sigstore.json \
  --certificate-identity-regexp '^https://github.com/heyvaldemar/minecraft-server-docker-compose/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com

slsa-verifier verify-artifact minecraft-server-docker-compose-<tag>.tar.gz \
  --provenance-path minecraft-server-docker-compose-<tag>.intoto.jsonl \
  --source-uri github.com/heyvaldemar/minecraft-server-docker-compose
```

Add `--source-tag <tag>` for a release published after 24 September 2026, which is signed by the run that published it. The five releases before that date were signed by a run started by hand on `main`, so their provenance names the branch, not the tag; the archive is still the tag's tree, and the signature still belongs to this repository's workflow. The workflow that makes them is [`release-assets.yml`](.github/workflows/release-assets.yml).

## Production checklist

- [ ] **Strong RCON password**: it is remote admin access to the server console.
- [ ] **Do not expose 25575 (RCON)** beyond the Docker network; the compose file does not publish it: keep it that way.
- [ ] **Set `MINECRAFT_SERVER_ROUTER_HOST`** to the hostname players will use: until then the router refuses every connection and the server never wakes.
- [ ] **Keep the admin panel on loopback**: it is published on `127.0.0.1` only. Reach it from elsewhere with an SSH tunnel, and set `MINECRAFT_SERVER_ADMIN_PANEL_TOKEN` if you will - the panel executes in-game commands.
- [ ] **Set `MINECRAFT_SERVER_OPS`** to your username(s) so you can moderate in-game.
- [ ] **Consider a whitelist** (`MINECRAFT_SERVER_WHITELIST`) for private servers; `ONLINE_MODE=true` (default) keeps authentication against Mojang.
- [ ] **Off-host backups**: `./minecraft-server-data-backups` lives on the same disk as the world. Sync it elsewhere (restic, rclone, S3) for real disaster recovery.
- [ ] **Pin the game version** before inviting players if you rely on specific plugins.

## Backups

There are two backup triggers, and they share one pool and one retention policy.

**Scheduled.** The `mc-backup` sidecar coordinates with the server over RCON: `save-off` → `save-all` → tar the world → `save-on`. It runs on an interval (`MINECRAFT_SERVER_BACKUP_INTERVAL`, default 23h) - the interval counts from the sidecar's start, so the backup window drifts to wherever the last restart put it. To pin it to the clock, set `CRON_SCHEDULE` in `.env`, for example `0 4 * * *`, read in `MINECRAFT_SERVER_SERVER_TIMEZONE`; the interval and initial delay are then ignored. With scale-to-zero the window only lands while the server is awake - see [Scale-to-zero](#scale-to-zero-sleep-when-idle).

**Shutdown.** The `backup-watcher` service watches the server container's state through the socket proxy and, when it sees running → stopped (the router's scale-down), tars the world directory. The graceful stop has just flushed the world to disk, and RCON is gone with the server, so this archive is taken without the RCON dance - and it is the backup that covers the days nobody is online. The watcher needs `/usr/share/zoneinfo` on the host (standard on every distro) so its archive timestamps fall on the same calendar days as the sidecar's.

**Joint retention.** Both triggers write `world-<timestamp>.tar.gz` archives into `./minecraft-server-data-backups` (the watcher's carry a `-shutdown` suffix), and the watcher is the single keeper of the policy that applies to the whole pool: at most **2 archives per calendar day** (newest kept) across at most **3 distinct calendar days** (newest kept) - so at most 6 archives on disk. `mc-backup`'s own age prune (`MINECRAFT_SERVER_PRUNE_BACKUPS_DAYS`) is set far out (3650) as a safety net so it never fights that policy. Restore by stopping the stack, extracting an archive over `./minecraft-server-data`, and starting again.

CI runs a second sidecar from the same service definition on `* * * * *` and requires an archive with the world in it; it restores one on every run and requires the world to land at the same relative path the running server keeps it at — not merely somewhere inside the archive. An archive whose world sits a directory deeper than that unpacks cleanly, leaves the server with an empty data directory, and the server does not complain: it generates a fresh world over the one being restored.

## Scale-to-zero (sleep when idle)

The `router` service (`itzg/mc-router`) is the only published service in the stack. It listens on 25565, matches the hostname a client requests against the `mc-router.host` label on the server container, and routes the connection there. With auto-scaling on, it also owns the server's lifecycle:

- **Wake on join.** A player connecting to the mapped hostname while the server is stopped makes the router start the container and wait for it to become reachable (up to a minute; the client sees the loading MOTD while that happens). First joins after a sleep are slow: the jar is warm but the world still loads.
- **Sleep after idle.** When no players have been connected for `MINECRAFT_SERVER_ROUTER_AUTO_SCALE_DOWN_AFTER` (default 2h), the router stops the container. The stop is graceful, so Paper flushes the world on the way down - sleeping loses nothing.
- **Unmapped hostnames are refused.** A client that does not name a mapped server - raw IP, port scanner - gets no response, so nobody can wake your server by scanning it.

### Point your clients at the hostname

Set `MINECRAFT_SERVER_ROUTER_HOST` in `.env` to the hostname players will type into the client's *Add Server* box (comma-separated for several). It must resolve to this host: a DNS A/AAAA record, or a `hosts` entry on each client, e.g.

```
# /etc/hosts on the client machine
1.2.3.4  mc.example.com
```

Until you set a real hostname, the default placeholder routes nothing: the router refuses every connection and the server never wakes.

### The Docker socket, and the wall in front of it

The router starts and stops the server container through the Docker daemon, but it does not hold the socket: the `socket-proxy` container (`tecnativa/docker-socket-proxy`) is the only service that mounts `/var/run/docker.sock`, and it answers a filtered subset of the Docker API over the compose network - no published ports. The allow-list is the least this stack needs; everything else answers 403:

| Who | What it can do |
|---|---|
| router | list/inspect containers, start, stop, unpause, plus the event stream (allowed by the proxy's defaults) |
| backup-watcher | list containers (read-only) |

Know what the wall contains: **the Docker socket is root-equivalent on the host.** Anything that can talk to the daemon can mount the host's filesystem into a container (`docker run -v /:/host --rm alpine chroot /host`) and walk away with full control. The proxy does not change that for itself - it is a small HAProxy with no published port - but it removes the escape from the internet-facing router: with no socket of its own, a compromised router cannot create containers, and `docker run -v /:/host` is impossible from inside it. The image runs as root by design so it can dial the host socket regardless of the host's docker group; there is nothing to configure. To go further, rootless Docker removes the host-root equivalence entirely, at the cost of moving the whole stack.

### Interaction with backups

The scheduled backup needs the server awake: the sidecar flushes the world over RCON, and a stopped server refuses RCON, so the window is skipped - the sidecar retries, gives up, and waits for the next one. Point `CRON_SCHEDULE` at a time your players are usually online. The shutdown backup covers the other direction: every time the server sleeps, the watcher archives the just-flushed world, so an all-day-asleep server still leaves a backup behind. Both triggers share the joint retention policy described in [Backups](#backups).

### Tuning and turning it off

All of the router's behaviour is overridable in `.env` (the section is listed there): the idle timeout, the asleep/loading MOTDs, and the auto-scale switches. Setting `MINECRAFT_SERVER_ROUTER_AUTO_SCALE_UP` and `MINECRAFT_SERVER_ROUTER_AUTO_SCALE_DOWN` to `false` keeps the router - hostname routing, rate limiting, scanner refusal - but lets you run the server continuously. If you want raw-IP joins as well, add the `mc-router.default: true` label to the server service; know that scanners can then wake the server too.

## Admin panel

The `admin-panel` service is a single standard-library Python script ([`scripts/admin-panel.py`](scripts/admin-panel.py)) on the official `python` image - no installed dependencies, a few tens of megabytes of RAM. It serves a small web UI on `127.0.0.1:8080` (`MINECRAFT_SERVER_ADMIN_PANEL_PORT`) with three things on it:

- **Online players.** The panel asks the server for its player list over RCON every ten seconds and shows the result in the header and the player card.
- **Console commands.** A command box that executes anything you would type on the server console (`say …`, `op …`, `save-all`, …) through RCON, with the server's response underneath. Commands you have run are recalled with ↑/↓.
- **Console feed.** Player chat and system lines - joins, leaves, deaths, advancements, plugin output, stack traces - mirrored live from `logs/latest.log` in the read-only data mount. Chat is highlighted, system lines are dimmed, `WARN`/`ERROR` are coloured.

**It never wakes the server.** That is the whole security contract of the panel: it dials RCON and reads a log file, nothing else. With the server container stopped, the RCON dial is refused, the UI shows *offline* with the last state it knows, and no code path in it can start the server - it does not talk to the router or the Docker daemon. Accessing the UI while the server sleeps costs nothing but a refused connection.

**Reach it from another machine with a tunnel**, not a published port - the compose file binds it to loopback on purpose:

```bash
ssh -L 8080:127.0.0.1:8080 user@your-host
# then open http://127.0.0.1:8080 in the browser on your machine
```

**Optional token.** Because the command box is the server console, set `MINECRAFT_SERVER_ADMIN_PANEL_TOKEN` in `.env` if you will ever tunnel to the panel from a machine you do not fully control. With it set, the page asks for the token once (kept in the browser session, never sent anywhere else) and every API request must carry it in the `X-Panel-Token` header. Without it, the panel trusts whoever is on the host - the same trust model as the loopback binding itself.

The panel's own settings - port, token, resource limits - are in the *Admin panel* section of `.env.example`; every one has a compose-level default, so there is nothing to configure for it to run.

## Unattended updates

Releases are the update channel: a tag is cut only after CI has built the pinned images, booted the full stack, and passed the smoke tests. `update.sh` moves a deployment to the newest tag and nothing else:

```bash
./update.sh --dry-run   # show what would be applied
./update.sh             # update within the current major and redeploy
```

Put it on a timer for hands-off minor/patch updates:

```bash
# crontab -e
17 5 * * *  /opt/minecraft-server-docker-compose/update.sh >> /var/log/minecraft-server-update.log 2>&1
```

The script refuses to cross a MAJOR template version on its own. Majors are breaking by definition and their release notes exist to be read. After reading them, `./update.sh --allow-major` performs the jump. It also refuses to touch a checkout with local modifications: your customization belongs in `.env`, which updates never overwrite.

This is deliberately a host-side script and not a container in the stack: an in-stack updater needs the Docker socket (root on the host) and turns "someone pushed to a repo" into "someone deployed to your machine" with no operator in the loop. A cron job under your own user updates only to tagged, CI-verified states and leaves the trust boundary where it was.

## Resource limits

Every service carries memory and CPU limits plus reservations as compose-level defaults, the same values CI boots the stack under. Override any of them in `.env` (the knobs and their defaults are listed in `.env.example`, e.g. `TRAEFIK_MEMORY_LIMIT=512m`) and the override survives every `git pull`. If a service is OOM-killed under real load, `docker inspect <container> --format '{{.State.OOMKilled}}'` says so; raise its `_MEMORY_LIMIT` and recreate.

## Container hardening

Every service runs with `security_opt: no-new-privileges:true`, so a process cannot gain privileges through setuid binaries even if it escapes its initial capability set. Infrastructure containers (the reverse proxy, databases, caches, backups) run with `cap_drop: [ALL]` and add back only what their entrypoints need: `NET_BIND_SERVICE` for Traefik to bind :80/:443, `CHOWN`/`SETUID`/`SETGID` (and friends) for database images to own their data directory and drop to their service user. Application containers keep the default capability set on purpose: upstream images assume it, and a wrong guess there is a boot loop in production rather than a hardening win. CI boots the stack under exactly these settings on every push, so what ships is what was tested.

## Testing

The [Deployment Verification](https://github.com/heyvaldemar/minecraft-server-docker-compose/actions/workflows/deployment-verification.yml?query=branch%3Amain) workflow runs on every push, pull request, and every day at 06:00 UTC:

1. **Lint**: actionlint on the workflow.
2. **Trivy scans** of all six pinned images (CRITICAL/HIGH, SARIF to the Security tab).
3. **Pin freshness** (daily/manual): digest drift for every pinned image against Docker Hub, plus release-lag checks against the three itzg upstreams.
4. **Deploy-and-test**: boots a real Paper server with ephemeral credentials, waits for the built-in healthcheck to pass (jar + plugin download + world generation), proves RCON answers `list`, verifies the router answers the Minecraft status protocol on 25565, checks the admin panel answers `/api/state` with the RCON-backed state and a console feed that already carries the boot lines (auto-scale-down is disabled in the ephemeral `.env`, so the router cannot stop the server mid-smoke), runs the backup watcher with its retention limits raised so it cannot prune the test's high-frequency archives, and requires a backup archive to appear before the run may pass.

A green run is the authoritative proof that the shipped configuration produces a joinable server, not just a started container.

---

## About the maintainer

<div align="center">

**Maintained by [Vladimir Mikhalev](https://github.com/heyvaldemar)** · Docker Captain · CNCF Ambassador · IBM Champion · AWS Community Builder

[YouTube](https://www.youtube.com/channel/UCf85kQ0u1sYTTTyKVpxrlyQ?sub_confirmation=1) · [Blog](https://heyvaldemar.com) · [LinkedIn](https://www.linkedin.com/in/heyvaldemar/)

</div>
