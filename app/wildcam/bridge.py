"""Trail cameras behind a bridge (bridge/ in this repo, e.g. on a Raspberry Pi).

A bridge only acts when asked: wildcam decides when a camera is woken. At each
camera's check hours we POST /poll; the bridge wakes the camera, downloads what
is new and wipes it from the camera. Once the bridge reports the session done,
we fetch every waiting clip, ingest it like any upload, and delete it on the Pi.

Cameras are added on the settings page and kept in the database:
    {"id": "tuin", "name": "Tuin", "url": "http://192.168.1.50:8090", "token": "", "hours": [0, 2, ...]}
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings
from .db import Database
from .i18n import _, ngettext, translate_known
from .worker import ingest_file

log = logging.getLogger(__name__)

# Mostly at night, when animals come: each check costs the camera a short wifi session.
DEFAULT_HOURS = (0, 2, 4, 6, 8, 14, 18, 20, 22)


def cameras(db: Database) -> list[dict]:
    return db.setting("cameras", [])


def camera_id(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "camera"
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    return cid


def _request(cam: dict, method: str, path: str) -> urllib.request.Request:
    req = urllib.request.Request(cam["url"].rstrip("/") + path, method=method)
    if cam.get("token"):
        req.add_header("Authorization", f"Bearer {cam['token']}")
    return req


def fetch_status(cam: dict, timeout: float = 10) -> dict:
    with urllib.request.urlopen(_request(cam, "GET", "/status"), timeout=timeout) as resp:
        data = json.load(resp)
    if not isinstance(data, dict) or "bridge" not in data:
        raise ValueError(_("dit adres geeft geen camerabridge-status"))
    return data


def pull(db: Database, settings: Settings, cam: dict) -> int:
    """Fetch every clip waiting on the camera's bridge. Returns how many were new here."""
    with urllib.request.urlopen(_request(cam, "GET", "/clips"), timeout=30) as resp:
        clips = json.load(resp).get("clips", [])
    # The camera reports no temperature per file; the one it gave at this check is close enough.
    temperature = ((_status.get(cam["id"], {}).get("data") or {}).get("camera") or {}).get("temperature")
    new = 0
    for clip in clips:
        clip_id, name = clip["id"], clip["filename"]
        with tempfile.NamedTemporaryFile(delete=False, dir=settings.data_dir, suffix=Path(name).suffix) as tmp:
            with urllib.request.urlopen(_request(cam, "GET", f"/clips/{clip_id}"), timeout=300) as resp:
                shutil.copyfileobj(resp, tmp)
            path = Path(tmp.name)
        if clip.get("size") and path.stat().st_size != clip["size"]:
            log.warning("Clip %s from %s came in incomplete, trying again next time", clip_id, cam["name"])
            path.unlink(missing_ok=True)
            continue
        _, status = ingest_file(db, settings, path, name, source=cam["name"], captured_at=clip.get("captured_at"),
                                temperature=temperature)
        if status == "unsupported":
            log.warning("Clip %s (%s) from %s is not a photo or video", clip_id, name, cam["name"])
            continue
        new += status == "queued"
        # Here now (or already, as a duplicate): the Pi can let it go.
        with urllib.request.urlopen(_request(cam, "DELETE", f"/clips/{clip_id}"), timeout=30):
            pass
    if clips:
        log.info("%s: %d clips fetched, %d new", cam["name"], len(clips), new)
    return new


def queue_settings(cam: dict, fields: dict) -> dict:
    """Camera settings for the bridge to write at the next check, e.g. {"osdInfoEnable": 0}."""
    req = _request(cam, "POST", "/settings")
    req.data = json.dumps(fields).encode()
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def start_session(cam: dict) -> bool:
    """Ask the bridge to wake the camera and fetch what is new. False if one already runs."""
    try:
        with urllib.request.urlopen(_request(cam, "POST", "/poll"), timeout=15):
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return False
        raise


# ---- last known status per camera -----------------------------------------------------

_status: dict[str, dict] = {}  # camera id -> {"data": GET /status, "at": t} or with "error"


def refresh_status(cam: dict) -> None:
    old = _status.get(cam["id"], {})
    try:
        _status[cam["id"]] = {"data": fetch_status(cam), "at": time.time()}
    except (OSError, ValueError) as exc:
        _status[cam["id"]] = {"error": str(exc), "at": time.time(), "data": old.get("data")}


def forget(cid: str) -> None:
    _status.pop(cid, None)


def session_done(cid: str, since: float) -> bool:
    """True once the bridge is idle again after a session that ended after `since`."""
    data = _status.get(cid, {}).get("data") or {}
    if (data.get("bridge") or {}).get("state") != "idle":
        return False
    finished = (data.get("last_session") or {}).get("finished_at")
    try:
        return finished is not None and datetime.fromisoformat(finished).timestamp() >= since
    except ValueError:
        return False


def next_check(hours) -> str | None:
    """When the camera will next be woken, as ISO local time."""
    now = datetime.now().astimezone().replace(minute=0, second=0, microsecond=0)
    for add in range(1, 25):
        t = now + timedelta(hours=add)
        if t.hour in hours:
            return t.isoformat()
    return None


def _signal(rssi: float | None) -> str:
    if rssi is None:
        return ""
    return _("goed") if rssi >= -70 else _("matig") if rssi >= -85 else _("zwak")


def camera_view(cam: dict) -> dict:
    """What the dashboard and the settings page show about one camera."""
    known = _status.get(cam["id"], {})
    data = known.get("data") or {}
    info = data.get("camera") or {}
    esp = data.get("esp") or {}
    session = data.get("last_session") or {}
    mac = esp.get("camera") or ""
    seen = esp.get("seen_s")
    if not known:
        state, tone = _("status nog onbekend"), "warn"
    elif known.get("error"):
        state, tone = _("bridge niet bereikbaar"), "bad"
    elif (data.get("bridge") or {}).get("state") == "session":
        state, tone = _("bezig met ophalen"), "busy"
    elif esp.get("error"):
        state, tone = _("ESP niet bereikbaar"), "bad"
    elif session.get("error"):
        state, tone = _("laatste check mislukt"), "bad"
    elif seen is not None and seen < 300:
        state, tone = _("in bereik"), "ok"
    elif seen is not None:
        state, tone = _("{n} min geen signaal", n=seen // 60), "warn"
    else:
        state, tone = _("nog geen signaal"), "warn"
    return {
        "id": cam["id"],
        "name": cam["name"],
        "url": cam["url"],
        "hours": cam.get("hours", []),
        "state": state,
        "tone": tone,
        "model": info.get("model") or "",
        "ble": f"TC100_{mac.replace(':', '')[-4:]}" if mac else "",
        "mac": mac,
        "rssi": esp.get("rssi"),
        "signal": _signal(esp.get("rssi")),
        "battery": info.get("battery"),
        "charging": info.get("charging"),
        "solar": info.get("solar"),
        "temperature": info.get("temperature"),
        "sd_free": info.get("sd_free_mb"),
        "sd_total": info.get("sd_total_mb"),
        "last_check": session.get("finished_at") or session.get("started_at"),
        "result": translate_known(session.get("result") or info.get("result")),
        "next_run": next_check(cam.get("hours", [])),
        "error": translate_known(known.get("error")),
        "progress": _progress(data),
        "token_set": bool(cam.get("token")),
        "date_stamp": (data.get("pending_settings") or {}).get("osdInfoEnable", info.get("date_stamp")),
        "date_stamp_pending": "osdInfoEnable" in (data.get("pending_settings") or {}),
    }


def summary(views: list[dict]) -> dict:
    """One status for the camera button: red if any camera fails, spinning while one is fetching."""
    tones = {v["tone"] for v in views}
    tone = next((t for t in ("bad", "busy", "warn") if t in tones), "ok")
    n = len(views)
    label = ngettext("{n} camera", "{n} camera's", n)
    problems = [f"{v['name']}: {v['state']}" for v in views if v["tone"] in ("bad", "warn")]
    busy = [v["name"] for v in views if v["tone"] == "busy"]
    detail = "; ".join(problems + [_("{name}: bezig met ophalen", name=name) for name in busy]) or _("alles in orde")
    return {"tone": tone, "busy": bool(busy), "count": n, "label": label, "title": f"{label}, {detail}"}


PHASES = {  # bridge phase -> Dutch text (translated when shown)
    "wekken": "Camera wekken",
    "inloggen": "Verbinden met de camera",
    "lijst": "Nieuwe clips zoeken",
    "wissen": "Wissen op de camera",
    "afronden": "Afronden",
}


def _progress(data: dict) -> dict | None:
    """A running session as the progress view shows it, or None."""
    if (data.get("bridge") or {}).get("state") != "session":
        return None
    cur = (data.get("bridge") or {}).get("current_session") or {}
    phase = cur.get("phase")
    if phase == "downloaden" and cur.get("files_total"):
        what = "Foto {i} van {n} ophalen" if cur.get("file_kind") == "photo" else "Clip {i} van {n} ophalen"
        text = _(what, i=cur.get("file_index") or 1, n=cur["files_total"])
    else:
        text = _(PHASES.get(phase, "Bezig"))
    value = cur.get("progress")
    return {"text": text, "value": value if isinstance(value, (int, float)) else None,
            "fetched": cur.get("fetched", 0), "started_at": cur.get("started_at")}
