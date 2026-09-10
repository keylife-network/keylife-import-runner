"""
One-click WP All Import runner.

Hitting a single URL triggers the import, then polls the processing
endpoint until WP All Import reports there is nothing left to do.
"""

import html
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, redirect, request, url_for

app = Flask(__name__)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_int(name, default):
    try:
        return int(env(name) or default)
    except ValueError:
        return default


# --- Configuration (all via environment) ------------------------------------
BASE_URL = env("WPAI_BASE_URL", "https://www.keylife.org/wp-load.php")
IMPORT_KEY = env("WPAI_IMPORT_KEY")
IMPORT_ID = env("WPAI_IMPORT_ID", "13")
ACCESS_TOKEN = env("ACCESS_TOKEN")          # blank = no auth
SITE_LABEL = env("SITE_LABEL", "Key Life Import")

POLL_SECONDS = env_int("POLL_SECONDS", 5)
TRIGGER_PAUSE = env_int("TRIGGER_PAUSE_SECONDS", 3)
MAX_ATTEMPTS = env_int("MAX_ATTEMPTS", 240)
MAX_SECONDS = env_int("MAX_SECONDS", 1800)
STABLE_REPEATS = env_int("STABLE_REPEATS", 3)
HTTP_TIMEOUT = env_int("HTTP_TIMEOUT", 120)

# Response text that means "the import is finished / nothing running".
DEFAULT_DONE_PATTERNS = (
    r"no import|not triggered|nothing to (do|process)|"
    r"import .*(complete|completed|finished)|already (imported|up.?to.?date)"
)
# An empty or unparseable value falls back to the default: an empty regex would
# match every response and end the import after a single pass.
DONE_PATTERNS = env("DONE_PATTERNS") or DEFAULT_DONE_PATTERNS
try:
    DONE_RE = re.compile(DONE_PATTERNS, re.IGNORECASE)
except re.error:
    DONE_PATTERNS = DEFAULT_DONE_PATTERNS
    DONE_RE = re.compile(DONE_PATTERNS, re.IGNORECASE)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# --- Run state --------------------------------------------------------------
LOCK = threading.Lock()
STATE = {
    "running": False,
    "result": None,       # None | "success" | "error"
    "message": "Idle — nothing has been run yet.",
    "started_at": None,
    "finished_at": None,
    "attempts": 0,
    "log": [],
}


def log(level, text):
    entry = {
        "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "level": level,
        "text": text,
    }
    with LOCK:
        STATE["log"].append(entry)
        if len(STATE["log"]) > 500:
            del STATE["log"][:-500]


def snippet(body, limit=400):
    """Flatten an HTML/text response into one readable line."""
    text = WS_RE.sub(" ", TAG_RE.sub(" ", body or "")).strip()
    if not text:
        return "(empty response)"
    return text[:limit] + ("…" if len(text) > limit else "")


def import_url(action):
    return (
        f"{BASE_URL}?import_key={IMPORT_KEY}"
        f"&import_id={IMPORT_ID}&action={action}"
    )


def call(action):
    resp = requests.get(
        import_url(action),
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": "keylife-import-runner/1.0"},
    )
    return resp.status_code, resp.text or ""


def run_import():
    """Trigger, then poll processing until done. Runs on a worker thread."""
    started = time.monotonic()
    try:
        # Step 1 — trigger
        log("info", f"Triggering import #{IMPORT_ID}…")
        status, body = call("trigger")
        log("info" if status == 200 else "error",
            f"trigger → HTTP {status}: {snippet(body)}")
        if status != 200:
            finish("error", f"Trigger failed with HTTP {status}. Import not started.")
            return

        time.sleep(TRIGGER_PAUSE)

        # Step 2 — process in chunks until WP All Import says it's finished
        last_body = None
        repeats = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            with LOCK:
                STATE["attempts"] = attempt

            status, body = call("processing")
            flat = snippet(body)

            if status != 200:
                log("error", f"processing #{attempt} → HTTP {status}: {flat}")
                finish("error", f"Processing failed with HTTP {status} on pass {attempt}.")
                return

            log("info", f"processing #{attempt} → HTTP 200: {flat}")

            if DONE_RE.search(flat):
                finish("success",
                       f"Import #{IMPORT_ID} finished after {attempt} processing pass"
                       f"{'es' if attempt != 1 else ''}.")
                return

            # Fallback: identical response several times running = nothing changing.
            if flat == last_body:
                repeats += 1
                if repeats >= STABLE_REPEATS:
                    finish("success",
                           f"Import #{IMPORT_ID} appears finished — response unchanged "
                           f"for {STABLE_REPEATS} passes.")
                    return
            else:
                repeats = 0
                last_body = flat

            if time.monotonic() - started > MAX_SECONDS:
                finish("error",
                       f"Gave up after {MAX_SECONDS}s. The import may still be "
                       f"running on the server.")
                return

            time.sleep(POLL_SECONDS)

        finish("error",
               f"Gave up after {MAX_ATTEMPTS} processing passes. The import may "
               f"still be running on the server.")

    except requests.RequestException as exc:
        log("error", f"Network error: {exc}")
        finish("error", f"Could not reach the site: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface anything to the operator
        log("error", f"Unexpected error: {exc}")
        finish("error", f"Unexpected error: {exc}")


def finish(result, message):
    log("success" if result == "success" else "error", message)
    with LOCK:
        STATE["running"] = False
        STATE["result"] = result
        STATE["message"] = message
        STATE["finished_at"] = datetime.now(timezone.utc).isoformat()


def start_run():
    """Returns True if a new run was started, False if one is already going."""
    with LOCK:
        if STATE["running"]:
            return False
        STATE.update(
            running=True,
            result=None,
            message="Running…",
            started_at=datetime.now(timezone.utc).isoformat(),
            finished_at=None,
            attempts=0,
            log=[],
        )
    threading.Thread(target=run_import, daemon=True).start()
    return True


def authorized():
    if not ACCESS_TOKEN:
        return True
    supplied = request.args.get("token") or request.headers.get("X-Access-Token", "")
    return supplied == ACCESS_TOKEN


# --- Page -------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__LABEL__</title>
<style>
  :root {
    --bg:#f4f4f2; --card:#fff; --ink:#1c1c1c; --muted:#6b6b6b;
    --line:#e2e2de; --accent:#7d1d3f; --ok:#1e7a4d; --err:#b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141414; --card:#1e1e1e; --ink:#f0efec; --muted:#9a9a95;
            --line:#333; --accent:#c9647f; }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:32px 16px; background:var(--bg); color:var(--ink);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:760px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 24px; font-size:.9rem; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:24px; }
  button { font:600 1rem/1 inherit; padding:16px 28px; border:0; border-radius:8px;
           background:var(--accent); color:#fff; cursor:pointer; width:100%; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .status { display:flex; align-items:center; gap:10px; margin:20px 0 0;
            font-weight:600; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--muted);
         flex:none; }
  .dot.run { background:var(--accent); animation:pulse 1s ease-in-out infinite; }
  .dot.ok  { background:var(--ok); }
  .dot.err { background:var(--err); }
  @keyframes pulse { 50% { opacity:.25; } }
  .msg { font-weight:400; color:var(--muted); }
  pre { margin:20px 0 0; padding:14px; background:var(--bg); border:1px solid var(--line);
        border-radius:8px; max-height:340px; overflow:auto; font-size:12px;
        white-space:pre-wrap; word-break:break-word; }
  .l-error { color:var(--err); }
  .l-success { color:var(--ok); }
</style></head>
<body><div class="wrap">
  <h1>__LABEL__</h1>
  <p class="sub">Import #__IMPORT_ID__ &middot; keylife.org</p>
  <div class="card">
    <button id="go">Run Import</button>
    <div class="status">
      <span class="dot" id="dot"></span>
      <span id="state">Idle</span>
      <span class="msg" id="msg"></span>
    </div>
    <pre id="log">No activity yet.</pre>
  </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const q = TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "";
const btn = document.getElementById("go");
const dot = document.getElementById("dot");
const stateEl = document.getElementById("state");
const msgEl = document.getElementById("msg");
const logEl = document.getElementById("log");

btn.onclick = async () => {
  btn.disabled = true;
  await fetch("/run" + q, { method: "POST" });
  poll();
};

function render(s) {
  const running = s.running;
  btn.disabled = running;
  btn.textContent = running ? "Running…" : "Run Import";
  dot.className = "dot " + (running ? "run" : s.result === "success" ? "ok"
                                     : s.result === "error" ? "err" : "");
  stateEl.textContent = running
    ? "Running (pass " + s.attempts + ")"
    : s.result === "success" ? "Complete"
    : s.result === "error" ? "Failed" : "Idle";
  msgEl.textContent = running ? "" : s.message;
  logEl.innerHTML = s.log.length
    ? s.log.map(e => '<div class="l-' + e.level + '">[' + e.t + '] '
        + e.text.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))
        + '</div>').join("")
    : "No activity yet.";
  logEl.scrollTop = logEl.scrollHeight;
  return running;
}

async function poll() {
  try {
    const s = await (await fetch("/status" + q)).json();
    if (render(s)) setTimeout(poll, 1500);
  } catch (e) { setTimeout(poll, 3000); }
}

if (__AUTOSTART__) { btn.click(); } else { poll(); }
</script>
</body></html>"""


def page(autostart=False):
    body = (
        PAGE.replace("__LABEL__", html.escape(SITE_LABEL))
        .replace("__IMPORT_ID__", html.escape(IMPORT_ID))
        .replace("__TOKEN__", html.escape(ACCESS_TOKEN or "", quote=True))
        .replace("__AUTOSTART__", "true" if autostart else "false")
    )
    return Response(body, mimetype="text/html")


# --- Routes -----------------------------------------------------------------
@app.route("/")
def index():
    if not authorized():
        return Response("Unauthorized", status=401)
    return page(autostart=False)


@app.route("/go")
def go():
    """The one URL: opens the page and starts the import immediately."""
    if not authorized():
        return Response("Unauthorized", status=401)
    return page(autostart=True)


@app.route("/run", methods=["POST", "GET"])
def run():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    if not IMPORT_KEY:
        return jsonify(error="WPAI_IMPORT_KEY is not configured"), 500
    started = start_run()
    if request.method == "GET":
        return redirect(url_for("index", token=ACCESS_TOKEN or None))
    return jsonify(started=started)


@app.route("/status")
def status():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    with LOCK:
        return jsonify(dict(STATE))


@app.route("/healthz")
def healthz():
    return jsonify(ok=True, import_id=IMPORT_ID, configured=bool(IMPORT_KEY))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=env_int("PORT", 8080))
