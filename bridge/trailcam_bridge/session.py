"""One camera session: START, login, fetch everything, wipe what we verified, STOP, STATUS."""
import logging
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .camera import TrailCam
from .link import LinkError, StartFailed

log = logging.getLogger(__name__)


class Busy(Exception):
    pass


def camera_time_iso(media_time, tz):
    """mediaTime is the camera's local wall clock written as if it were UTC epoch."""
    if not media_time:
        return None
    return datetime.fromtimestamp(int(media_time), UTC).replace(tzinfo=tz).isoformat()


PHASE_PROGRESS = {"wekken": 0.02, "inloggen": 0.05, "lijst": 0.08, "afronden": 1.0}
DOWNLOAD_FROM = PHASE_PROGRESS["lijst"]


def overall(done, total):
    """Overall progress during downloads: (done files + current fraction) / total, mapped onto
    0.08..1.0 so the bar never goes back after the fixed wekken/inloggen/lijst steps."""
    return round(DOWNLOAD_FROM + (1 - DOWNLOAD_FROM) * min(1.0, done / total), 4) if total else DOWNLOAD_FROM


WORK_MODES = {0: "bewegingsdetectie", 1: "timelapse"}
STATUS_MAX = 1200                             # bytes; ESP frame limit is 1500


def status_from_devinfo(info):
    """Flat STATUS fields for HA from a 512 devinfo answer (docs/serial-link.md). Unknown keys omitted."""
    out = {}

    def put(key, src, conv):
        v = info.get(src)
        if v is not None and v != "":
            try:
                out[key] = conv(v)
            except (TypeError, ValueError):
                pass

    put("battery", "batPercent", int)
    put("charging", "isCharging", bool)
    put("solar", "isSolarCharging", bool)
    put("temperature", "temperature", int)
    put("sd_free_mb", "sdFreeMB", int)
    put("sd_total_mb", "sdTotalMB", int)
    put("firmware", "fwVerName", str)
    put("model", "subModelName" if info.get("subModelName") else "modelName", str)
    put("mode", "workMode", lambda v: WORK_MODES.get(v, str(v)))
    put("timelapse_interval", "timelapseInterval", int)
    put("shoot_mode", "shootMode", int)
    put("video_resolution", "videoResolution", int)
    put("photo_resolution", "picResolution", int)
    put("clip_length", "recordDuration", int)
    put("audio", "recordAudio", bool)
    put("pir_sensitivity", "pirSens", int)
    put("detection_delay", "pirCoolingTime", int)
    put("side_pir", "sidePirOnOff", bool)
    put("motion_filter", "enableAiFilter", bool)
    if info.get("workTimeOnOff") is not None:
        if not info["workTimeOnOff"]:
            out["schedule"] = "uit"
        else:
            try:
                out["schedule"] = "%02d:%02d-%02d:%02d" % (
                    int(info["workTimeStartHour"]), int(info["workTimeStartMin"]),
                    int(info["workTimeEndHour"]), int(info["workTimeEndMin"]))
            except (KeyError, TypeError, ValueError):
                pass
    put("ir_range", "infraRedLum", int)
    put("date_stamp", "osdInfoEnable", bool)
    put("loop_record", "recordMode", bool)
    put("beep_volume", "spkVol", int)
    put("led", "WiFiLedEnable", bool)
    if info.get("cameraNameOnOff") is not None:
        out["camera_name"] = str(info.get("cameraName") or "") if info["cameraNameOnOff"] else ""
    return out


def fit_status(obj, limit=STATUS_MAX):
    """Drop the least important keys until the compact JSON fits."""
    import json
    obj = dict(obj)
    for key in ("camera_name", "firmware", "model", "schedule", "mode"):
        if len(json.dumps(obj, separators=(",", ":")).encode()) <= limit:
            break
        obj.pop(key, None)
    return obj


def ffprobe_duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def mux(h264, aac, out):
    """h264 + aac -> mp4 without re-encoding; video only if there is no (usable) audio."""
    tries = []
    if os.path.exists(aac) and os.path.getsize(aac) > 0:
        tries.append(["-i", h264, "-i", aac])
    tries.append(["-i", h264])
    err = ""
    for inputs in tries:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-c", "copy", out],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return
        err = r.stderr.strip()[-300:]
    raise RuntimeError("ffmpeg failed: %s" % err)


def _rm(*paths):
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


class Bridge:
    def __init__(self, cfg, link, store, camera_factory=TrailCam):
        self.cfg = cfg
        self.link = link
        self.store = store
        self.camera_factory = camera_factory
        self.tz = ZoneInfo(cfg.tz)
        self.lock = threading.Lock()
        self.current = None                   # progress of the running session
        self.state = store.load_state()
        self.tmp_dir = os.path.join(cfg.data_dir, "tmp")
        os.makedirs(self.tmp_dir, exist_ok=True)

    ESP_KEYS = ("state", "uptime", "camera", "rssi", "seen_s", "wifi_rssi")

    def ping_esp(self):
        """PING the ESP (only between sessions) and remember the answer for /status."""
        if self.running:
            return
        try:
            info = self.link.ping()
            esp = {k: info[k] for k in self.ESP_KEYS if k in info}
        except LinkError as e:
            esp = {"error": str(e)}
        esp["at"] = self.now_iso()
        self.state["esp"] = esp

    def start_esp_monitor(self, every=60.0):
        def loop():
            while True:
                try:
                    self.ping_esp()
                except Exception:
                    log.exception("ESP ping failed")
                time.sleep(every)
        threading.Thread(target=loop, name="esp-ping", daemon=True).start()

    def now_iso(self):
        return datetime.now(self.tz).isoformat(timespec="seconds")

    @property
    def running(self):
        return self.lock.locked()

    def poll_async(self, trigger="api"):
        """Start a session in the background. False if one is already running."""
        if not self.lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run_locked, args=(trigger,), name="session", daemon=True).start()
        return True

    def run(self, trigger="manual"):
        if not self.lock.acquire(blocking=False):
            raise Busy("a session is already running")
        return self._run_locked(trigger)

    def _run_locked(self, trigger):
        try:
            return self._session(trigger)
        except Exception:
            log.exception("session crashed")
        finally:
            self.current = None
            self.lock.release()

    # ---- the session
    def _session(self, trigger):
        cfg, link = self.cfg, self.link
        t0 = time.monotonic()
        res = {"trigger": trigger, "started_at": self.now_iso(), "finished_at": None, "result": None,
               "error": None, "fetched": 0, "deleted": 0, "failed": 0, "clips_left": None,
               "phase": "wekken", "progress": PHASE_PROGRESS["wekken"], "files_total": None,
               "file_index": None, "file_kind": None, "file_progress": None}
        self.current = res
        cam_status = None
        cam = sock = None
        sent_start = False
        log.info("session start (%s)", trigger)
        try:
            sent_start = True
            ready = link.start()
            log.info("ESP ready: %s", ready)
            sock = link.udp_socket()
            cam = self.camera_factory(cfg.camera_ip, sock=sock)
            cam.video_idle = cfg.video_idle
            self._phase(res, "inloggen")
            cam.connect()
            self.state["last_seen"] = self.now_iso()
            info = cam.dev_info()
            info = self._apply_settings(cam, info)
            cam_status = status_from_devinfo(info)
            self._phase(res, "lijst")
            files = cam.list_media()
            total = len(files)
            res["files_total"] = total

            def on_video(frames, total_frames):
                fp = min(1.0, frames / total_frames) if total_frames else None
                res["file_progress"] = fp
                res["progress"] = overall(res["file_index"] - 1 + (fp or 0), total)

            cam.on_video_progress = on_video
            log.info("camera: %d files, battery %s%%", len(files), info.get("batPercent"))
            left = len(files)
            for i, f in enumerate(reversed(files)):   # list is newest first; take oldest first
                if time.monotonic() - t0 > cfg.session_budget:
                    log.warning("session budget of %.0f s used, %d files left for next time",
                                cfg.session_budget, left)
                    break
                if link.session_ended.is_set():
                    raise LinkError("ESP ended the session: %s" % link.last_stopped)
                mid = f.get("mediaId")
                if mid is None:
                    continue
                res.update(file_index=i + 1, file_kind="photo" if f.get("fileType") == 0 else "video",
                           file_progress=0.0)
                if not self.store.known(mid):
                    self._phase(res, "downloaden", overall(i, total))
                    try:
                        clip = self._download(cam, f)
                    except Exception as e:
                        res["failed"] += 1
                        log.warning("download %s failed: %s", mid, e)
                        continue
                    self.store.add(clip)
                    res["fetched"] += 1
                    log.info("stored %s (%d bytes)", clip["filename"], clip["size"])
                else:
                    log.info("%s already stored, wiping it on the camera", mid)
                res["file_progress"] = 1.0
                self._phase(res, "wissen", overall(i + 1, total))
                if cam.delete(f):
                    res["deleted"] += 1
                    left -= 1
                else:
                    log.warning("camera did not delete %s", mid)
            res["clips_left"] = left
            n = res["fetched"]
            res["result"] = "niets nieuw" if not n else "1 clip opgehaald" if n == 1 else "%d clips opgehaald" % n
            if res["failed"]:
                res["result"] += ", %d mislukt" % res["failed"]
        except StartFailed as e:
            res["error"] = str(e)
            res["result"] = "camera niet bereikbaar"
        except Exception as e:
            log.exception("session failed")
            res["error"] = "%s: %s" % (type(e).__name__, e)
            res["result"] = ("fout: %s" % (e if str(e) else type(e).__name__))[:80]
        finally:
            self._phase(res, "afronden")
            if cam is not None:
                cam.close()
                time.sleep(0.5)               # let the close packet and rx loop finish
            if sock is not None:
                sock.close()
            if sent_start:
                try:
                    res["stopped"] = link.stop()
                except LinkError as e:
                    log.warning("STOP: %s", e)
                    res["error"] = res["error"] or "STOP: %s" % e
            res["finished_at"] = self.now_iso()
            res["duration_s"] = round(time.monotonic() - t0, 1)
            self._finish(res, cam_status)
        return res

    @staticmethod
    def _phase(res, name, progress=None):
        res["phase"] = name
        res["progress"] = PHASE_PROGRESS.get(name) if progress is None else progress

    def _finish(self, res, cam_status):
        now = int(time.time())
        if res["error"] or cam_status is None:
            esp = {"result": res["result"], "time": now}           # failure: HA shows only this
        else:
            esp = dict(cam_status, fetched=res["fetched"], result=res["result"], time=now)
            if res["clips_left"] is not None:
                esp["clips"] = res["clips_left"]
            esp = fit_status(esp)
            self.state["last_status"] = esp
        self.state["last_status_sent"] = esp
        self.state["last_session"] = res
        try:
            self.link.status(esp)
        except LinkError as e:
            log.warning("STATUS not sent: %s", e)
        try:
            self.store.save_state(self.state)
        except OSError as e:
            log.warning("state not saved: %s", e)
        log.info("session done: %s (%s s)", res["result"], res.get("duration_s"))

    # Settings wildcam asked for (POST /settings), written in the next session. Only these:
    SETTINGS = {"osdInfoEnable": int}   # date stamp burnt into photos and videos

    def _apply_settings(self, cam, info):
        pending = self.state.get("pending_settings") or {}
        todo = {k: v for k, v in pending.items() if k in self.SETTINGS and info.get(k) is not None and int(info[k]) != v}
        if todo:
            log.info("writing camera settings %s", todo)
            cam.set_settings(todo)
            info = cam.dev_info()
        if pending and all(info.get(k) is None or int(info[k]) == v for k, v in pending.items()):
            self.state.pop("pending_settings", None)
            self.store.save_state(self.state)
        return info

    def _download(self, cam, f):
        mid = f["mediaId"]
        captured = camera_time_iso(f.get("mediaTime"), self.tz)
        stamp = (datetime.fromtimestamp(int(f["mediaTime"]), UTC).strftime("%Y%m%d-%H%M%S")
                 if f.get("mediaTime") else "unknown")
        base = "%s_%d" % (stamp, mid)
        clip = {"id": mid, "captured_at": captured, "fetched_at": None, "duration": None}
        if f.get("fileType") == 0:
            data = cam.download_photo(f)
            if not data.startswith(b"\xff\xd8"):
                raise RuntimeError("not a JPEG (%d bytes)" % len(data))
            clip.update(kind="photo", filename=base + ".jpg")
            tmp = os.path.join(self.tmp_dir, clip["filename"])
            with open(tmp, "wb") as fh:
                fh.write(data)
        else:
            h264 = os.path.join(self.tmp_dir, base + ".h264")
            aac = os.path.join(self.tmp_dir, base + ".aac")
            tmp = os.path.join(self.tmp_dir, base + ".mp4")
            try:
                frames = cam.download_video(f, h264, aac)
                if not frames:
                    raise RuntimeError("no video frames received")
                mux(h264, aac, tmp)
            finally:
                _rm(h264, aac)
            dur = ffprobe_duration(tmp)
            if dur < self.cfg.min_video_seconds:
                _rm(tmp)
                raise RuntimeError("incomplete video: %.1f s, %d frames" % (dur, frames))
            clip.update(kind="video", filename=base + ".mp4", duration=round(dur, 2))
        final = os.path.join(self.store.clips_dir, clip["filename"])
        os.replace(tmp, final)
        clip["size"] = os.path.getsize(final)
        clip["fetched_at"] = self.now_iso()
        return clip
