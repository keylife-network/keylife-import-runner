# Key Life — One-Click WP All Import Runner

A tiny web app. The end user opens **one URL** and the container does the rest:

1. `GET .../wp-load.php?...&action=trigger` — must return HTTP 200
2. short pause
3. `GET .../wp-load.php?...&action=processing` — repeatedly, every few seconds,
   until WP All Import reports there's nothing left to process

Live progress and the raw responses from keylife.org stream onto the page, so
the user can see whether it worked instead of guessing.

## The URL to hand the end user

```
http://192.168.7.110:8123/go?token=YOUR_ACCESS_TOKEN
```

`/go` starts the import the moment the page loads. `/` shows the same page with
a **Run Import** button but does not start anything — useful for checking the
result of the last run.

## Deploy on Unraid

The image builds straight from GitHub, so nothing needs to be copied to the
server and `git` does not need to be installed on Unraid.

```bash
ssh root@192.168.7.110

docker build -t keylife-import \
  https://github.com/keylife-network/keylife-import-runner.git

docker run -d --name keylife-import --restart unless-stopped -p 8123:8080 \
  -e WPAI_IMPORT_KEY=your-import-key-here \
  -e WPAI_IMPORT_ID=13 \
  -e ACCESS_TOKEN=pick-a-long-random-string \
  keylife-import
```

Check it came up: `curl http://192.168.7.110:8123/healthz`

### Updating

Push a change to `main`, then on the server:

```bash
/mnt/user/appdata/keylife-import/update.sh
```

Or run the same three lines by hand:

```bash
docker build --pull --no-cache -t keylife-import \
  https://github.com/keylife-network/keylife-import-runner.git
docker rm -f keylife-import
docker run -d --name keylife-import --restart unless-stopped -p 8123:8080 \
  -e WPAI_IMPORT_KEY=... -e WPAI_IMPORT_ID=13 -e ACCESS_TOKEN=... keylife-import
```

`update.sh` reads its settings from `/mnt/user/appdata/keylife-import/.env`, so
the secrets live on the server and never appear in the repo or your shell
history. Copy `update.sh` and `.env.example` there once during setup.

### Alternative: docker compose

If you use the Compose Manager plugin and want `git pull` semantics:

```bash
cd /mnt/user/appdata
git clone https://github.com/keylife-network/keylife-import-runner.git keylife-import
cd keylife-import
cp .env.example .env && nano .env      # fill in the key and token
docker compose up -d --build
```

Update with `git pull && docker compose up -d --build`. This needs `git` on the
server — already present on 192.168.7.110 (git 2.55.0, docker 29.5.3).

## If the end user is not on your LAN

Port 8123 is only reachable inside the network. To let someone off-site use it,
put it behind something that already handles ingress — Nginx Proxy Manager with
a hostname and a Let's Encrypt cert, or a Cloudflare Tunnel. **Do not port
forward 8123 to the internet as-is.** The `ACCESS_TOKEN` is a shared secret in a
query string, not real authentication; it keeps out drive-by traffic, nothing
more.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `WPAI_BASE_URL` | `https://www.keylife.org/wp-load.php` | Endpoint |
| `WPAI_IMPORT_KEY` | *(required)* | The `import_key` secret |
| `WPAI_IMPORT_ID` | `13` | Which import to run |
| `ACCESS_TOKEN` | *(blank)* | Required in `?token=`. Blank disables the check |
| `SITE_LABEL` | `Key Life Import` | Heading on the page |
| `TRIGGER_PAUSE_SECONDS` | `3` | Wait after a successful trigger |
| `POLL_SECONDS` | `5` | Gap between processing passes |
| `MAX_ATTEMPTS` | `240` | Hard cap on processing passes |
| `MAX_SECONDS` | `1800` | Hard cap on total run time |
| `STABLE_REPEATS` | `3` | Identical responses that count as "finished" |
| `HTTP_TIMEOUT` | `120` | Per-request timeout, seconds |
| `DONE_PATTERNS` | see below | Regex marking the import as finished |

## Tune `DONE_PATTERNS` after the first real run

The loop stops when a processing response matches `DONE_PATTERNS`:

```
no import|not triggered|nothing to (do|process)|
import .*(complete|completed|finished)|already (imported|up.?to.?date)
```

Those are the phrasings WP All Import commonly returns, but the exact wording
varies by version. **Run it once for real and read the log on the page** — it
prints every response verbatim. If the finishing message isn't matched, set
`DONE_PATTERNS` to a regex that catches it and restart the container.

There's a safety net either way: if a processing pass returns the exact same
text `STABLE_REPEATS` times running, the app treats the import as done. That
prevents an endless loop, but it's slower and less precise than a real match,
so it's worth tuning the pattern.

## Notes

- Only one import can run at a time. A second request while one is in flight is
  refused, so an impatient double-click can't start two.
- Run state lives in process memory, which is why the container uses a single
  gunicorn worker. Restarting the container clears the log of the last run; it
  does not stop an import already running on keylife.org.
- Timing out here does **not** cancel the import — WordPress keeps going. The
  message says so.
