"""
Scheduled WP All Import runner.

A background scheduler calls the WP All Import endpoints on a fixed cadence:

  * action=processing  every PROCESSING_INTERVAL_SECONDS   (default 2 minutes)
  * action=trigger     every TRIGGER_INTERVAL_SECONDS      (default 4 hours)

That is the cadence WP All Import's own cron documentation describes: trigger
queues an import, and processing chews through it a chunk at a time.

A single worker thread performs every request, so a trigger and a processing
pass can never overlap. The web page is a read-only view of that schedule plus
a "Run Import Now" button that pulls the next trigger forward.
"""

import hmac
import html
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, Response, jsonify, make_response, redirect, request

app = Flask(__name__)


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_int(name, default):
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def env_bool(name, default=False):
    val = env(name).lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


# --- Configuration ----------------------------------------------------------
BASE_URL = env("WPAI_BASE_URL", "https://www.keylife.org/wp-load.php")
IMPORT_KEY = env("WPAI_IMPORT_KEY")
IMPORT_ID = env("WPAI_IMPORT_ID", "13")
ACCESS_TOKEN = env("ACCESS_TOKEN")
SITE_LABEL = env("SITE_LABEL", "Key Life Import")

PROCESSING_INTERVAL = env_int("PROCESSING_INTERVAL_SECONDS", 120)
TRIGGER_INTERVAL = env_int("TRIGGER_INTERVAL_SECONDS", 14400)
TRIGGER_ON_START = env_bool("TRIGGER_ON_START", False)
SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED", True)
HTTP_TIMEOUT = env_int("HTTP_TIMEOUT", 120)
STATE_FILE = env("STATE_FILE", "/data/state.json")

DEFAULT_DONE_PATTERNS = (
    r"no import|not triggered|nothing to (do|process)|"
    r"import .*(complete|completed|finished)|already (imported|up.?to.?date)"
)
# An empty or unparseable value falls back to the default: an empty regex would
# match every response and make the app think an import is never running.
DONE_PATTERNS = env("DONE_PATTERNS") or DEFAULT_DONE_PATTERNS
try:
    IDLE_RE = re.compile(DONE_PATTERNS, re.IGNORECASE)
except re.error:
    DONE_PATTERNS = DEFAULT_DONE_PATTERNS
    IDLE_RE = re.compile(DONE_PATTERNS, re.IGNORECASE)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

MIN_INTERVAL = 10  # guard against a typo turning this into a hammer
PROCESSING_INTERVAL = max(PROCESSING_INTERVAL, MIN_INTERVAL)
TRIGGER_INTERVAL = max(TRIGGER_INTERVAL, MIN_INTERVAL)


# --- Shared state -----------------------------------------------------------
LOCK = threading.Lock()
WAKE = threading.Event()

STATE = {
    "importing": False,          # last processing pass reported active work
    "paused": not SCHEDULER_ENABLED,
    "busy": None,                # "trigger" | "processing" | None
    "last_trigger": None,        # {"at":…, "status":…, "text":…, "ok":bool}
    "last_processing": None,
    "trigger_count": 0,
    "processing_count": 0,
    "error_count": 0,
    "last_error": None,
    "next_trigger_in": None,     # seconds
    "next_processing_in": None,
    "log": [],
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(level, text):
    """Append a log line, collapsing runs of identical idle polls."""
    entry = {
        "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "level": level,
        "text": text,
        "count": 1,
    }
    with LOCK:
        tail = STATE["log"][-1] if STATE["log"] else None
        if tail and tail["level"] == "muted" and level == "muted" \
                and tail["text"] == text:
            tail["count"] += 1
            tail["t"] = entry["t"]
        else:
            STATE["log"].append(entry)
            if len(STATE["log"]) > 300:
                del STATE["log"][:-300]


def snippet(body, limit=300):
    text = WS_RE.sub(" ", TAG_RE.sub(" ", body or "")).strip()
    if not text:
        return "(empty response)"
    return text[:limit] + ("…" if len(text) > limit else "")


def import_url(action):
    return (
        f"{BASE_URL}?import_key={IMPORT_KEY}"
        f"&import_id={IMPORT_ID}&action={action}"
    )


# --- Persistence ------------------------------------------------------------
# Only the last trigger time is persisted, so restarting the container does not
# re-trigger an import that already ran a few minutes ago.
def load_last_trigger():
    try:
        with open(STATE_FILE) as fh:
            return float(json.load(fh).get("last_trigger_epoch") or 0) or None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_last_trigger(epoch):
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"last_trigger_epoch": epoch}, fh)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass  # no writable volume mounted; schedule just resets on restart


# --- The one worker that makes every request --------------------------------
FORCE_TRIGGER = threading.Event()


def do_call(action):
    """Perform one request. Returns the recorded result dict."""
    with LOCK:
        STATE["busy"] = action
    try:
        resp = requests.get(
            import_url(action),
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "keylife-import-runner/2.0"},
        )
        text = snippet(resp.text)
        ok = resp.status_code == 200
        result = {"at": now_iso(), "status": resp.status_code, "text": text, "ok": ok}
    except requests.RequestException as exc:
        result = {"at": now_iso(), "status": None, "text": str(exc), "ok": False}
    finally:
        with LOCK:
            STATE["busy"] = None

    with LOCK:
        STATE["last_" + action] = result
        STATE[action + "_count"] += 1
        if not result["ok"]:
            STATE["error_count"] += 1
            STATE["last_error"] = f"{action}: {result['text']}"
    return result


def run_trigger(reason):
    log("info", f"Trigger ({reason}) — queueing import #{IMPORT_ID}")
    r = do_call("trigger")
    if r["ok"]:
        log("success", f"trigger → HTTP 200: {r['text']}")
    else:
        log("error", f"trigger → {r['status'] or 'network error'}: {r['text']}")
    return r


def run_processing():
    r = do_call("processing")
    if not r["ok"]:
        log("error", f"processing → {r['status'] or 'network error'}: {r['text']}")
        return r

    idle = bool(IDLE_RE.search(r["text"]))
    with LOCK:
        was_importing = STATE["importing"]
        STATE["importing"] = not idle

    if idle:
        if was_importing:
            log("success", f"Import finished — {r['text']}")
        else:
            # Collapsed in the log so 700 idle polls a day stay readable.
            log("muted", f"idle — {r['text']}")
    else:
        if not was_importing:
            log("info", "Import is running…")
        log("info", f"processing → HTTP 200: {r['text']}")
    return r


def scheduler():
    last_trigger_at = load_last_trigger()
    if last_trigger_at is None:
        # No saved state: fire on boot only if explicitly asked to.
        last_trigger_at = 0 if TRIGGER_ON_START else time.time()
    next_processing = time.time()

    log("info",
        f"Scheduler started — processing every {PROCESSING_INTERVAL}s, "
        f"trigger every {TRIGGER_INTERVAL}s")

    while True:
        if STATE["paused"]:
            with LOCK:
                STATE["next_trigger_in"] = None
                STATE["next_processing_in"] = None
            WAKE.wait(2)
            WAKE.clear()
            continue

        now = time.time()
        forced = FORCE_TRIGGER.is_set()

        if forced or now >= last_trigger_at + TRIGGER_INTERVAL:
            FORCE_TRIGGER.clear()
            run_trigger("manual" if forced else "scheduled")
            last_trigger_at = time.time()
            save_last_trigger(last_trigger_at)
            # Don't make a freshly queued import wait for the next slot.
            next_processing = time.time() + 5

        if time.time() >= next_processing:
            run_processing()
            next_processing = time.time() + PROCESSING_INTERVAL

        now = time.time()
        with LOCK:
            STATE["next_trigger_in"] = max(
                0, int(last_trigger_at + TRIGGER_INTERVAL - now))
            STATE["next_processing_in"] = max(0, int(next_processing - now))

        WAKE.wait(1)
        WAKE.clear()


_started = threading.Event()


def start_scheduler():
    if _started.is_set():
        return
    _started.set()
    if not IMPORT_KEY:
        log("error", "WPAI_IMPORT_KEY is not set — scheduler will not run.")
        return
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()


TOKEN_COOKIE = "kli_token"


def authorized():
    """Accept the token from the query string, a cookie, or a header.

    The cookie is what makes Unraid's WebUI button work: that link can't carry
    a secret, so the first visit asks for the token and remembers it.
    """
    if not ACCESS_TOKEN:
        return True
    supplied = (
        request.args.get("token")
        or request.cookies.get(TOKEN_COOKIE)
        or request.headers.get("X-Access-Token", "")
    )
    return hmac.compare_digest(supplied.encode(), ACCESS_TOKEN.encode())


# --- Page -------------------------------------------------------------------
PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__LABEL__</title>
<style>
  :root { --bg:#f4f4f2; --card:#fff; --ink:#1c1c1c; --muted:#6b6b6b;
          --line:#e2e2de; --accent:#7d1d3f; --ok:#1e7a4d; --err:#b3261e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141414; --card:#1e1e1e; --ink:#f0efec; --muted:#9a9a95;
            --line:#333; --accent:#c9647f; }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:32px 16px; background:var(--bg); color:var(--ink);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .wrap { max-width:820px; margin:0 auto; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 24px; font-size:.9rem; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:24px; margin-bottom:16px; }
  .state { display:flex; align-items:center; gap:10px; font-weight:600;
           font-size:1.1rem; }
  .dot { width:11px; height:11px; border-radius:50%; background:var(--muted); flex:none; }
  .dot.run { background:var(--accent); animation:pulse 1s ease-in-out infinite; }
  .dot.ok { background:var(--ok); } .dot.err { background:var(--err); }
  @keyframes pulse { 50% { opacity:.25; } }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
          gap:16px; margin-top:20px; }
  .tile { border:1px solid var(--line); border-radius:8px; padding:14px; }
  .tile .k { font-size:.75rem; text-transform:uppercase; letter-spacing:.05em;
             color:var(--muted); }
  .tile .v { font-size:1.35rem; font-weight:600; margin-top:4px;
             font-variant-numeric:tabular-nums; }
  .tile .n { font-size:.8rem; color:var(--muted); margin-top:2px;
             word-break:break-word; }
  .row { display:flex; gap:10px; flex-wrap:wrap; margin-top:20px; }
  button { font:600 .95rem/1 inherit; padding:14px 22px; border:0; border-radius:8px;
           background:var(--accent); color:#fff; cursor:pointer; flex:1 1 160px; }
  button.ghost { background:transparent; color:var(--ink);
                 border:1px solid var(--line); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .loghead { display:flex; justify-content:space-between; align-items:center;
             font-size:.8rem; color:var(--muted); margin-bottom:8px; }
  .loghead label { cursor:pointer; user-select:none; }
  pre { margin:0; padding:14px; background:var(--bg); border:1px solid var(--line);
        border-radius:8px; max-height:360px; overflow:auto; font-size:12px;
        white-space:pre-wrap; word-break:break-word; }
  .l-error { color:var(--err); } .l-success { color:var(--ok); }
  .l-muted { color:var(--muted); }
</style></head>
<body><div class="wrap">
  <h1>__LABEL__</h1>
  <p class="sub">Import #__IMPORT_ID__ &middot; keylife.org &middot; runs automatically</p>

  <div class="card">
    <div class="state"><span class="dot" id="dot"></span><span id="stateText">Loading…</span></div>
    <div class="grid">
      <div class="tile"><div class="k">Next processing</div>
        <div class="v" id="nextProc">—</div>
        <div class="n" id="procNote"></div></div>
      <div class="tile"><div class="k">Next trigger</div>
        <div class="v" id="nextTrig">—</div>
        <div class="n" id="trigNote"></div></div>
      <div class="tile"><div class="k">Requests</div>
        <div class="v" id="counts">—</div>
        <div class="n" id="errNote"></div></div>
    </div>
    <div class="row">
      <button id="runNow">Run Import Now</button>
      <button id="pause" class="ghost">Pause</button>
    </div>
  </div>

  <div class="card">
    <div class="loghead">
      <span>Activity</span>
      <label><input type="checkbox" id="showIdle"> show idle polls</label>
    </div>
    <pre id="log">…</pre>
  </div>
</div>
<script>
const TOKEN = "__TOKEN__";
const q = TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "";
const $ = id => document.getElementById(id);
let showIdle = localStorage.getItem("showIdle") === "1";
$("showIdle").checked = showIdle;
$("showIdle").onchange = e => {
  showIdle = e.target.checked;
  localStorage.setItem("showIdle", showIdle ? "1" : "0");
  tick();
};

$("runNow").onclick = async () => {
  $("runNow").disabled = true;
  await fetch("/run" + q, { method: "POST" });
  setTimeout(tick, 400);
};
$("pause").onclick = async () => {
  await fetch("/toggle-pause" + q, { method: "POST" });
  setTimeout(tick, 200);
};

const dur = s => {
  if (s === null || s === undefined) return "—";
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), h = Math.floor(m / 60);
  return h ? h + "h " + (m % 60) + "m" : m + "m " + (s % 60) + "s";
};
const esc = t => t.replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));

function render(s) {
  const busy = s.busy !== null;
  $("dot").className = "dot " + (s.paused ? "err" : busy || s.importing ? "run" : "ok");
  $("stateText").textContent = s.paused ? "Paused"
    : busy ? "Calling " + s.busy + "…"
    : s.importing ? "Import running" : "Idle — waiting for next run";

  $("nextProc").textContent = s.paused ? "paused" : dur(s.next_processing_in);
  $("nextTrig").textContent = s.paused ? "paused" : dur(s.next_trigger_in);
  $("procNote").textContent = s.last_processing
    ? "last " + s.last_processing.at.slice(11, 19) + "Z" : "not yet run";
  $("trigNote").textContent = s.last_trigger
    ? "last " + s.last_trigger.at.slice(0, 16).replace("T", " ") + "Z" : "not yet run";
  $("counts").textContent = s.processing_count + " / " + s.trigger_count;
  $("errNote").textContent = s.error_count
    ? s.error_count + " error(s) — " + s.last_error : "processing / trigger";

  $("pause").textContent = s.paused ? "Resume" : "Pause";
  $("runNow").disabled = s.paused;

  const lines = s.log.filter(e => showIdle || e.level !== "muted");
  $("log").innerHTML = lines.length
    ? lines.map(e => '<div class="l-' + e.level + '">[' + e.t + '] ' + esc(e.text)
        + (e.count > 1 ? ' <em>(x' + e.count + ')</em>' : '') + '</div>').join("")
    : "Nothing to show." + (showIdle ? "" : " (idle polls hidden)");
}

async function tick() {
  try { render(await (await fetch("/status" + q)).json()); } catch (e) {}
}
tick();
setInterval(tick, 2000);
</script>
</body></html>"""


def page():
    body = (
        PAGE.replace("__LABEL__", html.escape(SITE_LABEL))
        .replace("__IMPORT_ID__", html.escape(IMPORT_ID))
        # Auth rides on the cookie; never render the token into the page.
        .replace("__TOKEN__", "")
    )
    return Response(body, mimetype="text/html")


# --- Routes -----------------------------------------------------------------
LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__LABEL__</title>
<style>
  :root { --bg:#f4f4f2; --card:#fff; --ink:#1c1c1c; --muted:#6b6b6b;
          --line:#e2e2de; --accent:#7d1d3f; --err:#b3261e; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#141414; --card:#1e1e1e; --ink:#f0efec; --muted:#9a9a95;
            --line:#333; --accent:#c9647f; }
  }
  * { box-sizing:border-box; }
  body { margin:0; padding:64px 16px; background:var(--bg); color:var(--ink);
         font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  form { max-width:400px; margin:0 auto; background:var(--card);
         border:1px solid var(--line); border-radius:12px; padding:28px; }
  h1 { font-size:1.2rem; margin:0 0 6px; }
  p { color:var(--muted); font-size:.9rem; margin:0 0 20px; }
  input { width:100%; padding:13px; font:inherit; border:1px solid var(--line);
          border-radius:8px; background:var(--bg); color:var(--ink); }
  button { width:100%; margin-top:12px; padding:14px; font:600 1rem/1 inherit;
           border:0; border-radius:8px; background:var(--accent); color:#fff;
           cursor:pointer; }
  .err { color:var(--err); font-size:.85rem; margin:12px 0 0; }
</style></head>
<body>
  <form method="post" action="/login">
    <h1>__LABEL__</h1>
    <p>Enter the access token to view import status.</p>
    <input type="password" name="token" placeholder="Access token" autofocus
           autocomplete="current-password">
    <button type="submit">Continue</button>
    __ERROR__
  </form>
</body></html>"""


def login_page(error=False):
    body = (
        LOGIN_PAGE.replace("__LABEL__", html.escape(SITE_LABEL))
        .replace("__ERROR__", '<p class="err">That token was not accepted.</p>'
                 if error else "")
    )
    return Response(body, status=401 if error else 200, mimetype="text/html")


def remember_token(response):
    """Persist a valid token so later visits skip the prompt."""
    response.set_cookie(
        TOKEN_COOKIE, ACCESS_TOKEN, max_age=60 * 60 * 24 * 365,
        httponly=True, samesite="Lax", secure=request.is_secure,
    )
    return response


@app.route("/")
def index():
    if not authorized():
        return login_page()
    resp = make_response(page())
    if ACCESS_TOKEN and request.args.get("token"):
        remember_token(resp)
    return resp


@app.route("/login", methods=["POST"])
def login():
    supplied = request.form.get("token", "")
    if not ACCESS_TOKEN or hmac.compare_digest(
            supplied.encode(), ACCESS_TOKEN.encode()):
        return remember_token(make_response(redirect("/")))
    return login_page(error=True)


@app.route("/status")
def status():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    with LOCK:
        return jsonify(dict(STATE))


@app.route("/run", methods=["POST"])
def run_now():
    """Pull the next trigger forward. Resets the trigger interval."""
    if not authorized():
        return jsonify(error="unauthorized"), 401
    if not IMPORT_KEY:
        return jsonify(error="WPAI_IMPORT_KEY is not configured"), 500
    FORCE_TRIGGER.set()
    WAKE.set()
    return jsonify(queued=True)


@app.route("/toggle-pause", methods=["POST"])
def toggle_pause():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    with LOCK:
        STATE["paused"] = not STATE["paused"]
        paused = STATE["paused"]
    log("info", "Scheduler paused." if paused else "Scheduler resumed.")
    WAKE.set()
    return jsonify(paused=paused)


@app.route("/healthz")
def healthz():
    with LOCK:
        return jsonify(
            ok=True,
            configured=bool(IMPORT_KEY),
            import_id=IMPORT_ID,
            paused=STATE["paused"],
            importing=STATE["importing"],
            processing_count=STATE["processing_count"],
            trigger_count=STATE["trigger_count"],
            error_count=STATE["error_count"],
        )


start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=env_int("PORT", 8080), use_reloader=False)
