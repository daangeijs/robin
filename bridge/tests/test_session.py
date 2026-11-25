"""End to end on the Mac: Bridge -> Link -> fake ESP -> fake camera, then the HTTP API."""
import calendar
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailcam_bridge import link as L  # noqa: E402
from trailcam_bridge.api import make_server  # noqa: E402
from trailcam_bridge.config import Config  # noqa: E402
from trailcam_bridge.session import Bridge, camera_time_iso  # noqa: E402
from trailcam_bridge.store import Store  # noqa: E402
from fakes import FakeCamera, FakeESP, FakeSerial, FastCam, make_h264  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
TZ = ZoneInfo("Europe/Amsterdam")

PHOTO = {"mediaId": 955438153, "mediaTime": 1795565808, "mediaDirNum": 100, "mediaNum": 2, "fileType": 0}
VIDEO = {"mediaId": 955438160, "mediaTime": 1795569408, "mediaDirNum": 100, "mediaNum": 3, "fileType": 1}


class StatusTest(unittest.TestCase):
    def test_mode_and_schedule_variants(self):
        from trailcam_bridge.session import fit_status, status_from_devinfo
        st = status_from_devinfo({"workMode": 2, "workTimeOnOff": 0, "cameraNameOnOff": 1, "cameraName": "Bos"})
        self.assertEqual(st, {"mode": "2", "schedule": "uit", "camera_name": "Bos"})
        self.assertEqual(status_from_devinfo({"workMode": 1})["mode"], "timelapse")
        self.assertEqual(status_from_devinfo({}), {})
        big = {"camera_name": "x" * 2000, "battery": 1}
        self.assertEqual(fit_status(big), {"battery": 1})


class TimeTest(unittest.TestCase):
    def test_camera_time_is_local_wall_clock(self):
        # the camera writes its wall clock as if it were UTC
        t = calendar.timegm((2026, 11, 24, 22, 56, 48))
        self.assertEqual(camera_time_iso(t, TZ), "2026-11-24T22:56:48+01:00")
        t = calendar.timegm((2026, 7, 1, 6, 0, 0))
        self.assertEqual(camera_time_iso(t, TZ), "2026-07-01T06:00:00+02:00")
        self.assertIsNone(camera_time_iso(None, TZ))


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe needed")
class SessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h264 = make_h264(3)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = Config(data_dir=self.dir, http_host="127.0.0.1", http_port=0, video_idle=1.5)
        self.pi, self.esp_end = FakeSerial.pair()
        self.link = L.Link(lambda: self.pi, ping_wait=0).open()
        self.cam = FakeCamera([VIDEO, PHOTO], h264=self.h264)
        self.esp = FakeESP(self.esp_end, self.cam)
        self.bridge = Bridge(self.cfg, self.link, Store(self.dir), camera_factory=FastCam)

    def tearDown(self):
        self.link.close()
        self.esp_end.close()
        shutil.rmtree(self.dir)

    def test_full_session_and_api(self):
        res = self.bridge.run()
        self.assertIsNone(res["error"], res)
        self.assertEqual((res["fetched"], res["deleted"], res["clips_left"]), (2, 2, 0))
        self.assertEqual(res["result"], "2 clips opgehaald")
        self.assertEqual(sorted(self.cam.deleted), [PHOTO["mediaId"], VIDEO["mediaId"]])
        self.assertEqual(self.cam.files, [])

        types = [t for t, _ in self.esp.received]
        self.assertEqual(types[0], L.START)
        self.assertLess(types.index(L.STOP), types.index(L.STATUS))
        st = dict(self.esp.status[-1])
        self.assertIsInstance(st.pop("time"), int)
        self.assertEqual(st, {
            "battery": 60, "charging": True, "solar": False, "temperature": -2, "sd_free_mb": 239,
            "sd_total_mb": 255, "firmware": "5.1.0.26", "model": "WSW-8120", "mode": "bewegingsdetectie", "timelapse_interval": 3600,
            "shoot_mode": 1, "video_resolution": 2, "photo_resolution": 2, "clip_length": 10, "audio": True,
            "pir_sensitivity": 1, "detection_delay": 5, "side_pir": True, "motion_filter": False,
            "schedule": "20:00-07:05", "ir_range": 3, "date_stamp": True, "loop_record": False, "beep_volume": 80,
            "led": False, "camera_name": "", "clips": 0, "fetched": 2, "result": "2 clips opgehaald"})
        raw = [p for t, p in self.esp.received if t == L.STATUS][-1]
        self.assertLess(len(raw), 1200)

        clips = {c["id"]: c for c in self.bridge.store.list()}
        self.assertEqual(clips[PHOTO["mediaId"]]["kind"], "photo")
        v = clips[VIDEO["mediaId"]]
        self.assertEqual(v["kind"], "video")
        self.assertGreaterEqual(v["duration"], 2.0)
        self.assertEqual(v["captured_at"], "2026-11-25T01:16:48+01:00")   # 1795569408 as wall clock
        self.assertTrue(v["filename"].endswith(".mp4"))
        self.assertEqual(os.listdir(os.path.join(self.dir, "tmp")), [])

        # ---- HTTP API
        self.bridge.ping_esp()
        token = "s3cret"
        self.cfg.token = token
        srv = make_server(self.bridge, self.cfg)
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]

        def call(method, path, auth=True):
            req = urllib.request.Request(base + path, method=method)
            if auth:
                req.add_header("Authorization", "Bearer " + token)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.headers, r.read()
            except HTTPError as e:
                return e.code, e.headers, e.read()

        try:
            self.assertEqual(call("GET", "/status", auth=False)[0], 401)
            code, _, body = call("GET", "/status")
            s = json.loads(body)
            self.assertEqual((code, s["clips_stored"], s["camera"]["battery"]), (200, 2, 60))
            self.assertEqual(s["last_session"]["result"], "2 clips opgehaald")
            self.assertEqual(s["camera"]["schedule"], "20:00-07:05")
            self.assertEqual(s["esp"]["state"], "idle")
            self.assertIn("at", s["esp"])
            self.assertEqual(s["bridge"]["state"], "idle")

            code, _, body = call("GET", "/clips")
            listed = json.loads(body)["clips"]
            self.assertEqual([c["id"] for c in listed], [VIDEO["mediaId"], PHOTO["mediaId"]])   # newest first
            self.assertTrue(listed[0]["url"].endswith("/clips/%d" % VIDEO["mediaId"]))
            self.assertEqual(json.loads(call("GET", "/clips?since=2100-01-01T00:00:00")[2])["clips"], [])
            self.assertEqual(call("GET", "/clips?since=garbage")[0], 400)

            code, hdr, body = call("GET", "/clips/%d" % PHOTO["mediaId"])
            self.assertEqual((code, hdr["Content-Type"], int(hdr["Content-Length"])), (200, "image/jpeg", len(body)))
            self.assertTrue(body.startswith(b"\xff\xd8"))
            code, hdr, body = call("GET", "/clips/%d" % VIDEO["mediaId"])
            self.assertEqual((code, hdr["Content-Type"]), (200, "video/mp4"))
            self.assertEqual(call("GET", "/clips/1")[0], 404)
            self.assertEqual(call("GET", "/nope")[0], 404)

            self.assertEqual(call("DELETE", "/clips/%d" % PHOTO["mediaId"])[0], 200)
            self.assertEqual(call("DELETE", "/clips/%d" % PHOTO["mediaId"])[0], 404)
            self.assertEqual(len(json.loads(call("GET", "/clips")[2])["clips"]), 1)
            self.assertTrue(self.bridge.store.known(PHOTO["mediaId"]))      # id stays known

            # POST /poll: 202, then 409 while it runs
            self.cam.files = [dict(PHOTO, mediaId=1, mediaNum=9)]
            self.assertEqual(call("POST", "/poll")[0], 202)
            self.assertEqual(json.loads(call("GET", "/status")[2])["bridge"]["state"], "session")
            self.assertEqual(call("POST", "/poll")[0], 409)
            for _ in range(100):
                if not self.bridge.running:
                    break
                time.sleep(0.1)
            self.assertFalse(self.bridge.running)
            self.assertEqual(self.bridge.state["last_session"]["fetched"], 1)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_live_progress(self):
        import threading
        seen, stop = [], threading.Event()

        def sample():
            while not stop.is_set():
                cur = self.bridge.current
                if cur:
                    seen.append(dict(cur))
                time.sleep(0.01)

        phase_log = []
        orig = Bridge._phase

        def logged(res, name, progress=None):
            phase_log.append(name)
            orig(res, name, progress)

        self.bridge._phase = logged
        fp_log = []
        bridge = self.bridge

        class ProgressCam(FastCam):
            @property
            def on_video_progress(self):
                return self._cb

            @on_video_progress.setter
            def on_video_progress(self, cb):
                def wrapped(frames, total):
                    cb(frames, total)
                    fp_log.append((bridge.current["file_progress"], bridge.current["progress"]))
                self._cb = wrapped

        self.bridge.camera_factory = ProgressCam
        self.bridge.current = None
        t = threading.Thread(target=sample)
        t.start()
        phase_log.append("wekken")                               # set in the initial dict
        try:
            self.bridge.run()
        finally:
            stop.set()
            t.join()
        phases = []
        for name in phase_log:
            if not phases or phases[-1] != name:
                phases.append(name)
        self.assertEqual(phases, ["wekken", "inloggen", "lijst", "downloaden", "wissen",
                                  "downloaden", "wissen", "afronden"])
        for p in ("inloggen", "lijst", "downloaden", "wissen"):
            self.assertIn(p, phases)
        prog = [c["progress"] for c in seen]
        self.assertEqual(prog, sorted(prog))                     # never goes backwards
        self.assertEqual(prog[-1], 1.0)
        video = [c for c in seen if c["phase"] == "downloaden" and c["file_kind"] == "video"]
        self.assertTrue(video)
        fps = [fp for fp, _ in fp_log]
        self.assertTrue(any(0 < fp < 1 for fp in fps), fps[:5])   # frames / totalFrame
        self.assertEqual(fps[-1], 1.0)
        self.assertTrue(all(0.08 <= p <= 1.0 for _, p in fp_log))
        self.assertEqual({c["files_total"] for c in seen if c["phase"] == "downloaden"}, {2})
        self.assertEqual({c["file_index"] for c in seen if c["phase"] == "downloaden"}, {1, 2})

    def test_pending_setting_is_written_once(self):
        self.bridge.state["pending_settings"] = {"osdInfoEnable": 0}
        res = self.bridge.run()
        self.assertIsNone(res["error"], res)
        self.assertIs(self.cam.devinfo["osdInfoEnable"], False)
        self.assertEqual(self.cam.commands.count(513), 1)
        self.assertNotIn("pending_settings", self.bridge.state)
        self.assertIs(self.esp.status[-1]["date_stamp"], False)
        self.bridge.run()
        self.assertEqual(self.cam.commands.count(513), 1)

    def test_known_clip_is_only_wiped(self):
        self.bridge.store.add({"id": PHOTO["mediaId"], "filename": "x.jpg", "kind": "photo", "captured_at": None,
                               "size": 1, "fetched_at": "2026-11-25T00:00:00+01:00", "duration": None})
        self.cam.files = [PHOTO]
        res = self.bridge.run()
        self.assertEqual((res["fetched"], res["deleted"], res["result"]), (0, 1, "niets nieuw"))
        self.assertNotIn(1285, self.cam.commands)

    def test_start_fail_sends_status(self):
        self.esp.start_reply = (L.FAIL, {"reason": "ble_timeout"})
        res = self.bridge.run()
        self.assertEqual(res["result"], "camera niet bereikbaar")
        self.assertIn("ble_timeout", res["error"])
        self.assertIn(L.STOP, [t for t, _ in self.esp.received])
        self.assertEqual(sorted(self.esp.status[-1]), ["result", "time"])
        self.assertIsNotNone(self.bridge.state["last_session"]["finished_at"])


if __name__ == "__main__":
    unittest.main()
