"""Small HTTP API on the home network (stdlib ThreadingHTTPServer)."""
import hmac
import json
import logging
import os
import re
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .store import parse_iso

log = logging.getLogger(__name__)

CONTENT_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg"}
CLIP_RE = re.compile(r"^/clips/(\d+)$")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def make_server(bridge, cfg):
    class Handler(BaseHTTPRequestHandler):
        server_version = "trailcam-bridge"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---- helpers
        def _json(self, status, obj):
            body = json.dumps(obj, indent=1).encode() + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _auth(self):
            if not cfg.token:
                return
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got.encode(), ("Bearer " + cfg.token).encode()):
                raise ApiError(401, "missing or wrong bearer token")

        def _handle(self, fn):
            try:
                self._auth()
                fn(urlsplit(self.path))
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                log.exception("request failed")
                try:
                    self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
                except OSError:
                    pass

        def _clip_or_404(self, path):
            m = CLIP_RE.match(path)
            if not m:
                raise ApiError(404, "not found")
            clip = bridge.store.get(int(m.group(1)))
            if not clip:
                raise ApiError(404, "no such clip")
            return clip

        def _url(self, clip):
            host = self.headers.get("Host") or "%s:%d" % (cfg.http_host, cfg.http_port)
            return "http://%s/clips/%d" % (host, clip["id"])

        # ---- routes
        def do_GET(self):
            self._handle(self._get)

        def do_HEAD(self):
            self._handle(self._get)

        def do_DELETE(self):
            self._handle(self._delete)

        def do_POST(self):
            self._handle(self._post)

        def _get(self, u):
            path = u.path.rstrip("/") or "/"
            if path == "/status":
                return self._status()
            if path == "/clips":
                return self._clips(parse_qs(u.query))
            clip = self._clip_or_404(path)
            fpath = bridge.store.path(clip)
            try:
                fh = open(fpath, "rb")
            except FileNotFoundError:
                raise ApiError(410, "file missing on disk") from None
            with fh:
                size = os.fstat(fh.fileno()).st_size
                self.send_response(200)
                self.send_header("Content-Type",
                                 CONTENT_TYPES.get(os.path.splitext(fpath)[1], "application/octet-stream"))
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % clip["filename"])
                self.end_headers()
                if self.command != "HEAD":
                    shutil.copyfileobj(fh, self.wfile, 256 * 1024)

        def _status(self):
            n, size = bridge.store.count()
            st = bridge.state
            self._json(200, {
                "esp": st.get("esp"),
                "camera": st.get("last_status"),            # last STATUS after a good session
                "last_status_sent": st.get("last_status_sent"),
                "last_seen": st.get("last_seen"),
                "last_session": st.get("last_session"),
                "pending_settings": st.get("pending_settings"),
                "bridge": {
                    "state": "session" if bridge.running else "idle",
                    "current_session": dict(bridge.current or {}) or None,
                    "link": dict(bridge.link.stats, bad_frames=bridge.link.decoder.bad),
                },
                "clips_stored": n,
                "bytes_stored": size,
            })

        def _clips(self, q):
            clips = bridge.store.list()
            if q.get("since"):
                try:
                    since = parse_iso(q["since"][0], bridge.tz)
                except ValueError:
                    raise ApiError(400, "since must be ISO 8601") from None
                clips = [c for c in clips if c["fetched_at"] and parse_iso(c["fetched_at"], bridge.tz) > since]
            self._json(200, {"clips": [dict(c, url=self._url(c)) for c in clips]})

        def _delete(self, u):
            clip = self._clip_or_404(u.path.rstrip("/"))
            bridge.store.remove(clip["id"], bridge.now_iso())
            self._json(200, {"deleted": clip["id"]})

        def _post(self, u):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 65536)) if length else b""
            if u.path.rstrip("/") == "/settings":
                return self._settings(body)
            if u.path.rstrip("/") != "/poll":
                raise ApiError(404, "not found")
            if bridge.poll_async(trigger="api"):
                self._json(202, {"started": True})
            else:
                self._json(409, {"error": "a session is already running", "current_session": dict(bridge.current or {}) or None})

        def _settings(self, body):
            """Queue camera settings for the next session: {"osdInfoEnable": 0}."""
            try:
                asked = json.loads(body or b"{}")
                fields = {k: bridge.SETTINGS[k](v) for k, v in asked.items()}
            except (ValueError, TypeError, KeyError):
                raise ApiError(400, "allowed: %s" % ", ".join(bridge.SETTINGS)) from None
            pending = dict(bridge.state.get("pending_settings") or {}, **fields)
            bridge.state["pending_settings"] = pending
            bridge.store.save_state(bridge.state)
            self._json(202, {"pending_settings": pending})

    srv = ThreadingHTTPServer((cfg.http_host, cfg.http_port), Handler)
    srv.daemon_threads = True
    return srv
