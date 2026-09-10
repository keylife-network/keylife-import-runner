# Key Life — Scheduled WP All Import Runner

A small container that keeps WP All Import #13 running on keylife.org without
anyone having to do anything:

| Call | Cadence | Purpose |
|---|---|---|
| `...&action=processing` | every **2 minutes** | works through whatever is queued, a chunk at a time |
| `...&action=trigger` | every **4 hours** | queues a fresh import |

That's the cadence WP All Import's own cron documentation describes — `trigger`
queues an import, `processing` is the worker that chews through it.

A status page shows what the schedule is doing and offers a **Run Import Now**
button for when someone doesn't want to wait for the next 4-hour slot.

## Status page

```
http://192.168.7.110:8123/?token=YOUR_ACCESS_TOKEN
```

The first visit asks for the token and remembers it in a cookie, so
Unraid's WebUI button works without the secret in the link.

It shows a live countdown to the next processing pass and next trigger, whether
an import is currently running, request and error counts, and a log of the raw
responses from keylife.org. **Run Import Now** fires a trigger immediately and
resets the 4-hour clock. **Pause** stops the schedule until resumed.

Nobody needs to visit the page for the imports to run — the schedule is
entirely inside the container.

## Deploy on Unraid

The image builds straight from GitHub, so nothing needs to be copied to the
server.

```bash
ssh root@192.168.7.110
mkdir -p /mnt/user/appdata/keylife-import/data

docker build -t keylife-import \
  https://github.com/keylife-network/keylife-import-runner.git

docker run -d --name keylife-import --restart unless-stopped -p 8123:8080 \
  -v /mnt/user/appdata/keylife-import/data:/data \
  -e WPAI_IMPORT_KEY=your-import-key-here \
  -e WPAI_IMPORT_ID=13 \
  -e ACCESS_TOKEN=pick-a-long-random-string \
  keylife-import
```

Check it came up: `curl http://192.168.7.110:8123/healthz`

The `/data` mount holds one file: the timestamp of the last trigger. Without it
the container re-runs the 4-hour clock from zero on every restart. Nothing
breaks if you omit it; the schedule just resets.

### Unraid template (configure from the web UI)

`unraid/keylife-import.xml` gives you the container as a normal Unraid app —
every setting is a labeled form field, and the import key and access token are
masked password inputs.

```bash
curl -o /boot/config/plugins/dockerMan/templates-user/my-keylife-import.xml \
  https://raw.githubusercontent.com/keylife-network/keylife-import-runner/main/unraid/keylife-import.xml
```

Then **Docker → Add Container → Select a template → keylife-import**, fill in
the Import Key and Access Token, and Apply. Updates become the normal Unraid
"update ready" flow, and the WebUI button opens the status page.

Two things to know:

- The values you type are stored in that XML **in plaintext on the flash
  drive**, and flash backups include it. `Mask="true"` hides them in the
  browser, not on disk.
- The template pulls `ghcr.io/keylife-network/keylife-import-runner:latest`,
  which the GitHub Actions workflow publishes on every push to `main`. **After
  the first workflow run, set that package to Public** under the repo's
  Packages settings — otherwise Unraid gets a 403 trying to pull it.

### Updating

```bash
/mnt/user/appdata/keylife-import/update.sh
```

Copy `update.sh` and `.env.example` to that directory once during setup, fill in
`.env`, and updates become one command. It reads the key and token from `.env`,
so secrets never appear in the repo or your shell history.

### Alternative: docker compose

```bash
cd /mnt/user/appdata
git clone https://github.com/keylife-network/keylife-import-runner.git keylife-import
cd keylife-import
cp .env.example .env && nano .env
docker compose up -d --build
```

Update with `git pull && docker compose up -d --build`. This needs `git` on the
server — already present on 192.168.7.110 (git 2.55.0, docker 29.5.3).

## If the status page needs to be reachable off-site

Port 8123 is LAN-only. To expose it, put it behind Nginx Proxy Manager or a
Cloudflare Tunnel. **Do not port forward 8123 as-is.** `ACCESS_TOKEN` is a
shared secret in a query string, not real authentication — it keeps out
drive-by traffic, nothing more. The imports themselves run regardless of
whether the page is reachable.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WPAI_BASE_URL` | `https://www.keylife.org/wp-load.php` | Endpoint |
| `WPAI_IMPORT_KEY` | *(required)* | The `import_key` secret |
| `WPAI_IMPORT_ID` | `13` | Which import to run |
| `ACCESS_TOKEN` | *(blank)* | Required in `?token=`. Blank disables the check |
| `SITE_LABEL` | `Key Life Import` | Heading on the page |
| `PROCESSING_INTERVAL_SECONDS` | `120` | Gap between processing passes |
| `TRIGGER_INTERVAL_SECONDS` | `14400` | Gap between triggers (4 hours) |
| `TRIGGER_ON_START` | `false` | Trigger immediately on first boot |
| `SCHEDULER_ENABLED` | `true` | Start paused when `false` |
| `STATE_FILE` | `/data/state.json` | Where the last trigger time is saved |
| `HTTP_TIMEOUT` | `120` | Per-request timeout, seconds |
| `DONE_PATTERNS` | see below | Regex marking the import as idle |

Both intervals are floored at 10 seconds so a typo can't turn this into a
hammer on keylife.org.

## `DONE_PATTERNS` only affects the display

The app labels each processing response as *working* or *idle* by matching:

```
no import|not triggered|nothing to (do|process)|
import .*(complete|completed|finished)|already (imported|up.?to.?date)
```

This drives the "Import running" badge and keeps hundreds of idle polls
collapsed into one log line. It does **not** control the schedule — processing
fires every 2 minutes either way, so a wrong pattern costs you accurate status,
not a working import. Read the log after the first real run and adjust if the
badge looks wrong.

## Notes

- Every request is made by one worker thread, so a trigger and a processing
  pass can never overlap.
- A trigger is followed by a processing pass 5 seconds later, so a freshly
  queued import doesn't sit for 2 minutes.
- 2-minute polling is ~720 requests a day to `wp-load.php`. That's the normal
  WP All Import cron load, but it is not zero — widen the interval if the host
  complains.
- Run state is in memory, which is why the container runs a single gunicorn
  worker. Restarting clears the activity log but not the trigger schedule.
