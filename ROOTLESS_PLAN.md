# Rootless / Seedbox Port — Change Plan

> **Branch:** all work is on `dev` — the repository's default branch
> (`origin/HEAD → origin/dev`). There is no `master` branch.

Target: make `unshackle-dl/unshackle` run on a rootless shared host (seedbox) with
**no changes required** for users who still have root. The same codebase works in both
modes; the user's `unshackle.yaml` always wins over every default.

Analysis was done against commit `35dbfa5` (`master`). Every file in the repo was read.

## Audit findings (what actually needed changing)

| Concern | Finding |
|---|---|
| `sudo` | Exactly one actionable hit: `commands/dl.py` prints `$ sudo apt install …` (font hint). It never *executes* sudo. |
| Absolute paths | Only `/usr/share/fonts`, `/usr/local/share/fonts` (read-only system **font lookups**, already skipped when absent). No `/etc` `/var` `/opt` runtime defaults. |
| systemd / `.service` / `.timer` | **None exist.** No `systemctl` calls anywhere. The rootless launcher below is therefore additive. |
| Privileged ports | `serve` defaults to **port 8786** (already >1024). Nothing binds <1024. |
| Package installs | No `apt`/`pip`/`npm` executed at runtime — only the printed font hint (and a `apt install gpac` string in `tracks.py`, which has no sudo). |
| `chmod`/`chown root:` | Only `chmod 600/700` on the user's own VPN/cookie files — already rootless-safe. No `chown root:`. |
| cron / scheduler | None. |
| `flock()`/`filelock` | `filelock.FileLock` in `core/temp.py` (task-dir lock). On NFS `flock()` can return `ENOLCK` — needs a fallback. |
| Docker | **No Dockerfile / compose / entrypoint.** `docker` binary is only used to control a Gluetun VPN container. |
| `/tmp` usage | `tempfile.mkstemp`/`TemporaryDirectory`/`NamedTemporaryFile` (no `dir=`) in `api/download_manager.py`, `api/handlers.py`, `proxies/gluetun.py`, `utils/firefox_cookie_extractor.py`, `utils/tags.py`. Seedboxes may purge/limit `/tmp`. |
| Big temp files | Download/decrypt/mux scratch lives in `directories.temp` (default was inside the clone). No size cap → quota risk. |
| Outbound network | GitHub API (update check), git clone over https (service repos), proxy providers. All standard outbound https; no raw sockets. |
| Config-first requirement | `Config.__init__` already overlays `directories:` from YAML over `_Directories` defaults, and protects `user_configs`/`data`/`core_dir`/`namespace_dir`/`app_dirs`. The only problem was **where the fallback defaults point** (into the package/clone). |

## Files changed

### 1. `unshackle/core/config.py` — relocate *fallback* defaults (config-first preserved)
`_Directories` defaults only (attribute names unchanged; YAML still overrides all
user-settable keys):

| Attribute | Before | After (fallback default) |
|---|---|---|
| `user_configs` | `<package>/` | `~/.config/unshackle` (AppDirs) |
| `data` | `<package>/` | `~/.local/share/unshackle` (AppDirs) |
| `downloads` | `<clone>/downloads` | `~/unshackle/downloads` |
| `temp` | `<clone>/temp` | `~/.cache/unshackle/temp` |
| `cache` | `data/cache` | `~/.cache/unshackle` |
| `cookies` `logs` `exports` `wvds` `prds` `dcsl` | `data/…` | `~/.local/share/unshackle/…` |

`commands`, `services`, `vaults`, `fonts`, `core_dir`, `namespace_dir` stay
package-relative (they point at *code*, not writable state).
Also adds a new optional key `temp_max_bytes` (int, `0` = unlimited) for quota safety.

### 2. `unshackle/core/rootless.py` — **NEW** `rootless_check()`
Runs at startup: detects uid/root, `mkdir`s the writable dirs, points Python's
`tempfile` module at `~/.cache/unshackle/tmp` (so *all* `mkstemp`/`TemporaryDirectory`
work redirects off `/tmp`), reports available tools (`ffmpeg`, `mkvmerge`, `git`, …) and
a low `RLIMIT_NPROC`. Never raises.

### 3. `unshackle/core/__main__.py` — wire in `rootless_check()` + faster startup
Calls `rootless_check()` once after logging setup, and moves the GitHub update check
into a background daemon thread so a cache-miss no longer blocks startup on up to 5 s of
network I/O.

### 4. `unshackle/core/temp.py` — NFS-safe locking + quota trim
- `_acquire_task_lock()`: `FileLock` first; on `OSError` (e.g. NFS `ENOLCK`) falls back to
  an atomic `O_CREAT|O_EXCL` **pid-file lock**. `Timeout` still means "held".
- `trim_temp_to_limit()`: when `temp_max_bytes > 0`, prunes the temp dir oldest-first
  (skipping live task dirs) before each task starts.

### 5. `unshackle/core/service_repo.py` — writable clone location
`repos_base()` fallback (only used when *no* local services dir is configured) now clones
into `~/.cache/unshackle/services/_repos` instead of inside the installed package.

### 6. `unshackle/commands/dl.py` — drop `sudo` from the font hint
`$ sudo apt install …` → `$ apt install …` with a seedbox note (manual / `~/.fonts`).

### 7. `unshackle/core/utilities.py` — clarify read-only system font dirs (comment only)

### 8. `start.sh` / `stop.sh` — **NEW** rootless launcher pair
`nohup` + PID-file launcher for `unshackle serve` (honours `$UNSHACKLE_HOST` /
`$UNSHACKLE_PORT`), plus `status`/`logs` helpers and `tmux`/`pm2` notes. No systemd.

### 9. `ROOTLESS_SETUP.md` — **NEW** user guide (see deliverable 2)

### 10. `README.md`, `mkdocs.yml`, `docs/…` — logo, attribution, docs updates
- Rewritten `README.md` as the fork's front page (install, rootless section, quick start,
  credits — "Modified for rootless servers by Paw.").
- Add generated logo (`docs/assets/logo.png`).
- `docs/getting-started/installation.md`: new "Rootless / seedbox installs" section.
- `docs/reference/configuration/directories.md`: new default column + `temp_max_bytes`.

### 11. `unshackle/core/__main__.py` — ASCII-art headline
Adds a small centred headline **"Modified for rootless servers by Paw"** above the
unshackle ASCII-art banner in the CLI, and runs `rootless_check()` *after* the banner
(so startup output stays clean; headless `serve --quiet` still runs it first).

## Rules compliance
- No identifier/config-key/flag/env-var/function/class was renamed.
- No unrelated code was touched or "cleaned up".
- No feature removed — everything degrades with a warning (`logger.warning`) and a
  user-space fallback.
- No upstream `.service` files exist, so nothing byte-identical to preserve; launcher is additive.
