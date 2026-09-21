# Unshackle watcher

`unshackle watch` is a small, maintained polling layer around the normal Unshackle
service and download contracts. It does **not** implement service login, catalogue
parsing, media extraction, DRM, track selection, muxing, or downloading. It asks the
selected service for titles, notices new items, and then calls the same download
pipeline used by `unshackle dl`.

The watcher is useful when an episode does not have a reliable release schedule, when
a service exposes a title before its manifests are ready, or when a short release
window should be checked more often than a normal cron interval.

## 1. Install and make a first check

Use the normal Unshackle installation and update procedure for this checkout. Configure
and test the service with its ordinary command first:

```bash
unshackle SERVICE --help
unshackle SERVICE 'https://service.example/shows/my-series' --list-titles
```

The value passed to a service is an **opaque `title_ref`**. Preserve it exactly. A
watcher does not assume that it is numeric: it can be a full SonyLIV, HBO Max, or other
service URL, a slug, or a service-native identifier. The same exact value is passed to
the service constructor for polling and to the normal download pipeline for a trigger.

Watcher location comes from `unshackle.yaml`. Prefer setting it there rather than
passing `--config` on every run:

```yaml
# ~/.config/unshackle/unshackle.yaml
directories:
  downloads: ~/unshackle/downloads
  watchers: ~/.local/share/unshackle/watchers   # state/journal files

watch:
  config: ~/.config/unshackle/watchers.yaml     # dedicated watcher YAML
```

You can also put the `watchers:` list itself in `unshackle.yaml`. When
`download.output_dir` is omitted, the watcher uses `directories.downloads`.

Copy the safe example if you want a dedicated file:

```bash
mkdir -p ~/.config/unshackle
cp watchers.example.yaml ~/.config/unshackle/watchers.yaml
$EDITOR ~/.config/unshackle/watchers.yaml
```

Run one check immediately before starting a daemon:

```bash
unshackle watch --once --now
```

`--now` ignores the release schedule for this check. It does not make the service API
push data; availability remains polling-based.

## 2. Minimal configuration

```yaml
watchers:
  - id: my-show
    service: SERVICE_TAG
    title_ref: "https://service.example/show/or/a/complete/url"
    mode: sequential
    proxy:
      service: direct
      metadata: direct
      download: inherit
    availability:
      no_cache: true
      stability_delay: 900
    download:
      output_dir: "~/unshackle/downloads"
```

The normal Unshackle service configuration, cookies, credentials, CDM, and provider
settings remain in their normal locations. The watcher does not duplicate those
implementations. If a service requires a constructor option that is normally exposed by
its CLI, put it under `service_params`:

```yaml
service_params:
  device: living-room
  region: bd
```

Only options accepted by that service's constructor are used. Check
`unshackle SERVICE --help` and the service documentation for the actual names.

### Automatic metadata resolution

Users do not need to enter TMDB, IMDb, TVDB, SIMKL, or AniList IDs. After a fresh title
poll, the watcher sends the service-returned title, year, and movie/TV kind through
Unshackle's existing provider ordering and fuzzy matching logic. The resolved IDs are
stored as enrichment in watcher state and are useful for logs, notifications, and later
inspection. They never replace `title_ref` and are not used to call the service.

## 3. Modes and selectors

### Sequential

```yaml
mode: sequential
```

For a series, downloads every unseen episode after the saved ordering cursor, one at a
time. A missing first-run state is safe by default: the latest currently visible item is
used as the initial baseline and the old catalogue is not downloaded unexpectedly. To
intentionally backfill from the current catalogue, use recovery mode or set:

```yaml
recovery:
  initial_policy: all
```

### Latest

```yaml
mode: latest
```

Uses the existing Unshackle latest-episode behavior. This is convenient for a daily
watcher, but missed episodes can be skipped by design. It is mutually exclusive in
intent with an explicit `wanted` list.

### Explicit selectors

```yaml
mode: explicit
wanted:
  - S02E07
  - S05E01
  - S02E03-S02E07
  - S02E07.2
```

The watcher reuses the same `SeasonRange` parser as `-w/--wanted`:

| Selector | Meaning |
| --- | --- |
| `S02E07` | One episode |
| `S02` | Every episode in season 2 |
| `S02E03-S02E07` | An episode range |
| `S02E07.2` | One multipart episode part |
| `S02,-S02E04` | Season 2 except episode 4 |

Selectors may also be comma- or semicolon-separated in one string. Explicit selectors
are tracked separately, so a successful `S02E07` does not incorrectly advance past an
unrelated `S05E01`. A failed trigger remains retryable.

A movie target is processed once when its service title becomes available. The movie
URL or other service reference is still passed through unchanged.

## 4. Polling, freshness, and release timing

Availability checks use the service's title method with `no_cache: true` by default.
This bypasses the title/provider result cache for the poll; it does not delete cache
files and does not disable authentication/token caches. The watcher then stops after
the title tree and optional metadata enrichment. It does not call `get_tracks()` during
availability polling.

A service can list an episode before its manifest and tracks are ready. The default
safe pattern waits and confirms it again:

```yaml
availability:
  no_cache: true
  stability_delay: 900
schedule:
  checks_after_release: [0, 5, 15, 30]
```

The first sighting is persisted as a pending candidate. A later fresh poll must still
show it after the stability delay. If the download fails, the pending item and retry
backoff remain in state; the success cursor is not advanced. Pending items remain
retryable outside a weekly release window, so a manifest that appears a few minutes
late does not wait until the next scheduled release.

### Immediate release mode

For low latency, set the delay to zero and run a persistent watcher process:

```yaml
schedule:
  timezone: Asia/Dhaka
  weekday: thursday
  release_time: "20:00"
  preflight_minutes: 5
  burst_poll_seconds: 3
  burst_window_minutes: 30

availability:
  no_cache: true
  stability_delay: 0
```

This checks at most once every three seconds inside the bounded release window and
triggers on the first fresh appearance. It is still API polling; it cannot promise
push notifications or sub-second detection. A five-minute cron job cannot provide a
three-second burst, so use a user-level systemd service or another long-running
process.

The watcher also supports irregular releases. If there is no predictable weekday or
release time, omit those fields and use `poll_interval`:

```yaml
schedule:
  poll_interval: 300
```

An episode released ten days late is found when the next uncached catalog check sees it.

## 5. Proxies: three separate roles

```yaml
proxy:
  service: "socks5://127.0.0.1:1080"
  metadata: "http://metadata-proxy:8080"
  download: "in"
```

* `service` is used for service session setup, authentication, and fresh title calls.
* `metadata` is used only by external metadata-provider HTTP sessions.
* `download` is passed to the normal download pipeline. `inherit` uses the service
  proxy; it can instead be a raw HTTP/SOCKS URI or a configured provider selector.

Provider selectors such as `in` use the same configured proxy providers as Unshackle.
A raw URI is not a provider name. `direct`, `none`, or an omitted role means no explicit
proxy for that role. A URI containing `127.0.0.1` points to the machine/container where
the watcher executes, not automatically to the host running a different component.

The three roles matter when, for example, a streaming service must use a country exit,
TMDB must use a normal egress, and the actual download should use a separate route.

## 6. One-shot, daemon, cron, and rootless operation

A one-shot run checks only targets that are due:

```bash
unshackle watch --once
```

For a cron entry, use `--once` and a user-owned config/data directory. A cron interval
cannot be shorter than its own interval, so do not use it for the three-second release
example. The command is designed to run as a normal user; it stores watcher files under
Unshackle's configured user data directory, normally:

```text
~/.local/share/unshackle/watchers/
```

For a persistent user-level systemd unit:

```ini
[Unit]
Description=Unshackle watcher

[Service]
ExecStart=/path/to/unshackle watch
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Enable it without root:

```bash
systemctl --user daemon-reload
systemctl --user enable --now unshackle-watcher.service
journalctl --user -u unshackle-watcher.service -f
```

Use a container/namespace-specific address for proxies and make sure the container user
can read cookies/CDM material and write the configured data/download directories. Do not
run the daemon as root just to make a cache writable.

## 7. Recovery after lost state

Watcher state is deliberately separate from Unshackle's title/provider cache. For each
watcher the data directory contains:

* an atomic current JSON state file;
* a `.bak` replacement backup;
* an append-only `.events.jsonl` journal of sightings, successes, and failures;
* a lock file preventing overlapping cron/daemon runs.

If the JSON file is lost, the journal is used to recover known successes. To stage a
backfill from the current fresh catalogue rather than launching everything at once:

```bash
unshackle watch \
  --cache \
  --from 2026-09-03 \
  --once
```

`--recover-state` is a clearer alias for `--cache`. Recovery queues one candidate,
checkpoints state after each successful download, waits between retries, and leaves the
recovery flag active after `--once` so a later run resumes where it stopped. It does not
start all discovered episodes concurrently. `--from` filters by service-provided air
date when one is available; items without an air date are retained.

`--cache` here means **recover watcher state**. It does not mean “use the title cache”
and it does not turn stale availability results back on.

## 8. Notifications

Both Telegram and Discord can receive success and failure events:

```yaml
notifications:
  telegram:
    enabled: true
    bot_token_env: UNSHACKLE_TELEGRAM_BOT_TOKEN
    chat_id: "123456789"
  discord:
    enabled: true
    webhook_url_env: UNSHACKLE_DISCORD_WEBHOOK_URL
  error_cooldown: 900
```

Supply secrets only through the environment of the user service:

```bash
export UNSHACKLE_TELEGRAM_BOT_TOKEN='set-this-in-your-secret-store'
export UNSHACKLE_DISCORD_WEBHOOK_URL='set-this-in-your-secret-store'
```

Success messages include the service, title, episode selector, and output files when the
normal pipeline reports them. Failure messages include the phase and retry time. Repeated
identical failures are deduplicated for `error_cooldown` seconds. Notification failures
do not mark a successful download as failed.

Never paste a GitHub PAT, service password, cookie value, bot token, or Discord webhook
into this repository, its YAML examples, logs, or commit history. Rotate any credential
that was exposed before pushing source anywhere.

## 9. What is loaded when

The `watch` command and its availability path use lazy imports. Service authentication,
title code, cookie/credential loading, and metadata-provider code are available for the
poll. The downloader, CDM/DRM, vault, track extraction, muxing, and media-processing
stack is imported only inside the trigger call. Once a candidate is eligible, the
watcher calls `perform_download`, which uses the ordinary `unshackle dl` execution
pipeline and its normal selectors/options.

If a service requires behavior not represented by its existing service class, fix or
update that service in the normal upstream service layer; do not add a service-specific
extractor to the watcher.

## 10. Troubleshooting

* **No item is found:** run the service's `--list-titles` manually, verify the exact
  `title_ref`, and check the service profile/cookies.
* **The same old item appears:** confirm `availability.no_cache: true`; watcher JSON state
  and the title cache are separate, so inspect both the watcher state and service logs.
* **An item is seen but not downloaded:** inspect `pending` and `eligible_at` in the state;
  `stability_delay` may be intentionally waiting. A failed download has `attempts` and
  `next_retry_at`.
* **A three-second schedule does not fire:** ensure the process is persistent and that
  the local timezone/release weekday are correct. `--once` cannot emulate a burst.
* **Metadata lookup fails:** set a distinct `metadata` proxy, check the configured
  provider keys/order, and remember that metadata enrichment is non-fatal to service
  availability.
* **The daemon starts twice:** use the per-watcher lock and run one systemd/cron owner;
  a second invocation skips while the first holds the lock.
* **A missing JSON causes a large backfill:** use `--cache`/`--recover-state`; it is
  deliberately sequential and checkpoints every success.
