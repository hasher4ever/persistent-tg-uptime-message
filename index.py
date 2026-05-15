import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KUMA_URL      = os.environ.get("KUMA_URL", "").rstrip("/")
STATUS_SLUG   = os.environ.get("STATUS_SLUG", "")
BOT_TOKEN     = os.environ.get("BOT_TOKEN", "")
CHAT_ID       = os.environ.get("CHAT_ID", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
TITLE         = os.environ.get("TITLE", "Status")
PORT          = int(os.environ.get("PORT", "3000"))

for k, v in (("KUMA_URL", KUMA_URL), ("STATUS_SLUG", STATUS_SLUG),
             ("BOT_TOKEN", BOT_TOKEN), ("CHAT_ID", CHAT_ID)):
    if not v:
        print(f"[fatal] missing env var: {k}", file=sys.stderr, flush=True)
        sys.exit(1)

state_lock = threading.Lock()
last_tick = {"at": 0, "ok": False, "action": None, "error": None}


def http_get_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def tg(method, **params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(params).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read())
    if not j.get("ok"):
        raise RuntimeError(f"{method}: {j.get('description', 'unknown error')}")
    return j


def fetch_state():
    page = http_get_json(f"{KUMA_URL}/api/status-page/{STATUS_SLUG}")
    beat = http_get_json(f"{KUMA_URL}/api/status-page/heartbeat/{STATUS_SLUG}")
    out = []
    for group in page.get("publicGroupList", []):
        for m in group.get("monitorList", []):
            mid = str(m["id"])
            beats = beat.get("heartbeatList", {}).get(mid, [])
            status = beats[-1]["status"] if beats else None
            uptime = beat.get("uptimeList", {}).get(f"{mid}_24", 0) * 100
            out.append({"name": m["name"], "status": status, "uptime": uptime})
    return out


def render(monitors):
    all_up = bool(monitors) and all(m["status"] == 1 for m in monitors)
    stamp = time.strftime("%H:%M", time.gmtime())
    lines = [
        f"{'🟢' if all_up else '🔴'} *{TITLE}* — updated {stamp} UTC",
        "─────────────────────",
    ]
    if not monitors:
        lines.append("_no monitors found on status page_")
    for m in monitors:
        icon = "🟢" if m["status"] == 1 else "🔴" if m["status"] == 0 else "❓"
        lines.append(f"{icon} `{m['name']:<18}` {m['uptime']:.1f}%")
    return "\n".join(lines)


def get_pinned_mid():
    r = tg("getChat", chat_id=CHAT_ID)
    pinned = r.get("result", {}).get("pinned_message")
    return pinned.get("message_id") if pinned else None


def do_tick():
    global last_tick
    try:
        text = render(fetch_state())
        mid = get_pinned_mid()
        if mid is not None:
            try:
                tg("editMessageText", chat_id=CHAT_ID, message_id=mid,
                   text=text, parse_mode="Markdown")
                with state_lock:
                    last_tick = {"at": int(time.time() * 1000), "ok": True,
                                 "action": "edited", "error": None}
                return
            except Exception:
                pass
        r = tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="Markdown")
        new_mid = r["result"]["message_id"]
        tg("pinChatMessage", chat_id=CHAT_ID, message_id=new_mid,
           disable_notification=True)
        with state_lock:
            last_tick = {"at": int(time.time() * 1000), "ok": True,
                         "action": "created", "error": None}
    except Exception as e:
        with state_lock:
            last_tick = {"at": int(time.time() * 1000), "ok": False,
                         "action": None, "error": str(e)}
        print(f"[tick] {e}", file=sys.stderr, flush=True)


def tick_loop():
    while True:
        do_tick()
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _respond(self, status, body, ctype="text/plain"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._respond(200, "status-bot running")
        elif path == "/healthz":
            with state_lock:
                snap = dict(last_tick)
            stale = (time.time() * 1000 - snap["at"]) > POLL_INTERVAL * 1000 * 3
            healthy = snap["ok"] and not stale
            body = json.dumps({
                "status": "ok" if healthy else "degraded",
                "lastTick": snap,
                "pollIntervalMs": POLL_INTERVAL * 1000,
            })
            self._respond(200 if healthy else 503, body, "application/json")
        else:
            self._respond(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/tick":
            do_tick()
            with state_lock:
                snap = dict(last_tick)
            self._respond(200, json.dumps(snap), "application/json")
        else:
            self._respond(404, "not found")


def main():
    threading.Thread(target=tick_loop, daemon=True).start()
    print(f"status-bot listening on :{PORT}, ticking every {POLL_INTERVAL}s",
          flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
