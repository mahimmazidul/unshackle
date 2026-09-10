# Running unshackle without root (Rootless / Seedbox)

This fork is **modified for rootless servers by Paw.** The same codebase now works
with and without root: every writable path defaults to your home directory, and every
default can be overridden in `unshackle.yaml`. You do **not** need `sudo`, systemd,
cron, or any privileged port.

> [!IMPORTANT]
> This page is for shared/seedbox hosts. If you have root and prefer system paths,
> just set them in `unshackle.yaml` (they are honoured exactly as written).

---

## 1. Prerequisites you install manually

The seedbox must already provide (or you install under `~/.local` / `~/bin`) these
**external tools**. unshackle never runs `apt`/`pip install` for you.

| Tool | Binary | Why |
|---|---|---|
| FFmpeg ≥ 6.0 | `ffmpeg`, `ffprobe` | media processing / analysis |
| MKVToolNix ≥ 80 | `mkvmerge`, `mkvpropedit` | MKV muxing & metadata |
| shaka-packager | `shaka-packager` / `packager` | DRM decryption (default) |
| Bento4 | `mp4decrypt` | optional alternative decryptor |
| dovi_tool | `dovi_tool` | optional Dolby Vision |
| git | `git` | optional, for remote service repos |

Check what's present on your box:

```shell
command -v ffmpeg ffprobe mkvmerge mkvpropedit shaka-packager packager git
```

If a tool is missing, ask your seedbox provider (many install these on request), or put a
static binary in `~/bin` and add it to `PATH`. On hosts without root you **cannot** run
`apt install` yourself — unshackle prints a hint instead of trying.

### Python

unshackle needs **Python 3.11 – 3.14**. Install it with `uv` (no root needed):

```shell
curl -LsSf https://astral.sh/uv/install.sh | sh        # installs uv into ~/.local/bin
uv tool install git+https://github.com/mahimmazidul/unshackle.git
```

or run straight from the clone:

```shell
git clone https://github.com/mahimmazidul/unshackle.git ~/unshackle
cd ~/unshackle
uv run unshackle --help
```

> `uv tool install` puts `unshackle` in `~/.local/bin`. Add `~/.local/bin` to your `PATH`.

---

## 2. Starting / stopping without root

The only long-running service is `unshackle serve` (REST API + Widevine/PlayReady CDM).
Two launcher scripts are included (they use `nohup` + a PID file — no systemd):

```shell
# default: 127.0.0.1:8786, logs to ~/.local/state/unshackle/serve.log
./start.sh

# bind a public port for remote clients (pick a HIGH port; <1024 needs root)
UNSHACKLE_HOST=0.0.0.0 UNSHACKLE_PORT=9000 ./start.sh

# extra serve flags
UNSHACKLE_SERVE_ARGS="--api-only --no-playready" ./start.sh

./stop.sh          # stop
./start.sh restart # restart
tail -f ~/.local/state/unshackle/serve.log   # logs
```

On hosts that provide `tmux`/`screen`/`pm2`, those also work:

```shell
tmux new -ds unshackle 'unshackle serve -h 0.0.0.0 -p 9000'
pm2 start "unshackle serve -h 0.0.0.0 -p 9000" --name unshackle
```

> **Port note:** the default port is already high (8786). Do **not** try port 80/443 —
> binding <1024 requires root on Linux. Use any high port and let your seedbox's reverse
> proxy (or a `Caddyfile` next to your config) terminate TLS.

---

## 3. Where everything lives now (defaults)

When a path is **not** set in `unshackle.yaml`, these XDG-style defaults are used:

| Purpose | Default location |
|---|---|
| Config (`unshackle.yaml`) | `~/.config/unshackle/unshackle.yaml` |
| Data base (cookies, WVDs, PRDs, …) | `~/.local/share/unshackle/` |
| Logs | `~/.local/share/unshackle/logs/` |
| Cookies | `~/.local/share/unshackle/cookies/` |
| Widevine devices (`.wvd`) | `~/.local/share/unshackle/WVDs/` |
| PlayReady devices (`.prd`) | `~/.local/share/unshackle/PRDs/` |
| Exports | `~/.local/share/unshackle/exports/` |
| Cache & title cache | `~/.cache/unshackle/` |
| Temp scratch (download/decrypt/mux) | `~/.cache/unshackle/temp/` |
| tempfile redirect (`mkstemp`, etc.) | `~/.cache/unshackle/tmp/` |
| Downloads (finished files) | `~/unshackle/downloads/` |
| Service-repo clones (git) | `~/.cache/unshackle/services/_repos/` |
| `serve` PID + log (start.sh) | `~/.local/state/unshackle/` |

All of these directories are created automatically at startup.

---

## 4. Overriding defaults in `unshackle.yaml`

Create `~/.config/unshackle/unshackle.yaml` and set any of these. Your values are used
**exactly as written** (and never overridden by the fallback logic):

```yaml
directories:
  downloads: ~/media/unshackle          # e.g. point at your seedbox download dir
  temp: ~/media/unshackle-tmp           # a big/fast mount is ideal for muxing
  cache: ~/.cache/unshackle
  logs: ~/.local/share/unshackle/logs
  wvds: ~/.local/share/unshackle/WVDs
  prds: ~/.local/share/unshackle/PRDs

# Optional disk-quota guard: cap the temp dir (bytes). 0 = unlimited (default).
temp_max_bytes: 10737418240            # 10 GiB, oldest files pruned first
```

A user with root who wants system paths can set `/var/log/unshackle` etc. the same way.

Quick commands to manage config and inspect the resolved paths:

```shell
unshackle cfg directories.downloads "~/media/unshackle"
unshackle env info        # prints every resolved directory
unshackle env check       # checks the external tools
```

---

## 5. Seedbox-specific notes

- **Disk quota:** downloads, temp and cache all live under your home dir by default.
  Point `directories.downloads` / `directories.temp` at a large mount and set
  `temp_max_bytes` so a stuck job can't silently fill your quota. `unshackle env clear
  temp` / `env clear cache` free space manually; stale task dirs are swept automatically.
- **Shared `/tmp`:** seedboxes may purge or size-limit `/tmp`. At startup unshackle
  redirects Python's `tempfile` module to `~/.cache/unshackle/tmp`, so no temp work lands
  in `/tmp`.
- **Shared / NFS filesystems:** the per-task lock now falls back from `flock()` to an
  atomic PID-file lock when the filesystem rejects `flock()`, so downloads don't crash on
  NFS mounts.
- **Process limits:** if your box caps `ulimit -u`, unshackle logs a warning at startup.
  Keep `--downloads` (concurrent tracks) and `--workers` modest; download **one title at a
  time** (`unshackle dl …` per title) on tight hosts.
- **Firewall / egress:** unshackle only makes ordinary outbound HTTPS calls (services,
  license servers, and the GitHub update check). No raw sockets or inbound ports are
  needed except the one you choose for `serve`. If GitHub is blocked, set
  `update_checks: false` in your config — this also speeds up startup.
- **Python/Node versions:** Python **3.11+** is required (seedboxes sometimes lag —
  check `python3 --version`). There is no Node component; `npm`/`pm2` is only an optional
  process manager.
- **Docker:** there is no Dockerfile (the `docker` binary is only used to control a
  Gluetun VPN container). Everything runs directly as your user.
- **Startup speed:** `rootless_check()` is cheap (a few `PATH` lookups), and the GitHub
  update check now runs in a background thread so it can't block startup. Disable it
  entirely with `update_checks: false` if you want zero network on launch.

---

## 6. What was changed (summary)

- Writable-path **defaults** moved from the installed package/clone into XDG user dirs
  (config-first: YAML always wins).
- `rootless_check()` added — runs at startup, detects uid/writable paths/tools and
  auto-adjusts (same code works root and rootless).
- `tempfile` redirected to `~/.cache/unshackle/tmp`; NFS-safe lock fallback; optional
  `temp_max_bytes` quota guard; service-repo clones moved to the user cache dir.
- `sudo` removed from the one printed install hint; no systemd, no privileged ports,
  no iptables/ufw/firewalld, no `chown root:`.
- `start.sh` / `stop.sh` rootless launcher added (systemd-free).
