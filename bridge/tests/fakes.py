"""Test doubles: a loopback serial pair, a fake ESP32 (serial-link.md) and a tiny fake camera."""
import json
import os
import struct
import threading

from trailcam_bridge import camera as tc
from trailcam_bridge import link as L

CAM = ("192.168.43.1", 40000)


class FakeSerial:
    """One end of an in-memory full-duplex serial line."""

    def __init__(self):
        self.rx = bytearray()
        self.cv = threading.Condition()
        self.peer = None
        self.closed = False
        self.timeout = 0.05

    @classmethod
    def pair(cls):
        a, b = cls(), cls()
        a.peer, b.peer = b, a
        return a, b

    @property
    def in_waiting(self):
        return len(self.rx)

    def write(self, data):
        if self.closed:
            raise OSError("closed")
        with self.peer.cv:
            self.peer.rx += data
            self.peer.cv.notify_all()
        return len(data)

    def read(self, n=1):
        with self.cv:
            if not self.rx and not self.closed:
                self.cv.wait(self.timeout)
            if self.closed:
                raise OSError("closed")
            out = bytes(self.rx[:n])
            del self.rx[:n]
            return out

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


class FakeESP:
    """Speaks the ESP side of docs/serial-link.md; relays UDP to a fake camera."""

    def __init__(self, ser, camera=None, start_reply=None):
        self.ser = ser
        self.camera = camera
        self.start_reply = start_reply or (L.READY, {"ssid": "TrailCam_TEST", "ip": "192.168.43.2"})
        self.dec = L.SlipDecoder()
        self.received = []                     # (type, payload) of non-UDP frames
        self.status = []
        self.in_session = False
        self.wlock = threading.Lock()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def send(self, ftype, payload=b""):
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        with self.wlock:
            self.ser.write(L.encode_frame(ftype, payload))

    def _loop(self):
        while self.running:
            try:
                data = self.ser.read(4096)
            except OSError:
                return
            for ftype, payload in self.dec.feed(data):
                self._handle(ftype, payload)

    def _handle(self, ftype, payload):
        if ftype != L.UDP_SEND:
            self.received.append((ftype, payload))
        if ftype == L.PING:
            self.send(L.PONG, {"state": "ready" if self.in_session else "idle", "uptime": 12})
        elif ftype == L.START:
            if self.in_session:
                self.send(L.FAIL, {"reason": "busy"})
                return
            rtype, info = self.start_reply
            if rtype is None:
                return                         # never answers
            self.in_session = rtype == L.READY
            self.send(rtype, info)
        elif ftype == L.STOP:
            self.in_session = False
            self.send(L.STOPPED, {"reason": "requested"})
        elif ftype == L.STATUS:
            self.status.append(json.loads(payload))
        elif ftype == L.UDP_SEND and self.in_session and self.camera:
            dst = L.unpack_addr(payload)
            for src, data in self.camera.handle(payload[6:], dst):
                self.send(L.UDP_RECV, L.pack_addr(src) + data)


def make_h264(seconds=3):
    """Small Annex-B H.264 stream (needs ffmpeg)."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "t.h264")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=size=160x120:rate=10:duration=%d" % seconds, "-c:v", "libx264",
                        "-bf", "0", "-g", "10", "-pix_fmt", "yuv420p", "-f", "h264", out], check=True)
        with open(out, "rb") as fh:
            return fh.read()


def split_annexb(stream):
    idx = []
    i = stream.find(b"\x00\x00\x00\x01")
    while i >= 0:
        idx.append(i)
        i = stream.find(b"\x00\x00\x00\x01", i + 4)
    return [stream[a:b] for a, b in zip(idx, idx[1:] + [len(stream)])]


DEVINFO = {  # trimmed real 512 answer (settings_original.json), a few values changed
    "fwVerName": "5.1.0.26", "modelName": "S610W", "subModelName": "WSW-8120", "cameraNameOnOff": 0, "cameraName": "0000", "infraRedLum": 3,
    "osdInfoEnable": True, "WiFiLedEnable": False, "batPercent": 60, "isCharging": True,
    "isSolarCharging": False, "temperature": -2, "workMode": 0, "timelapseInterval": 3600, "shootMode": 1,
    "picResolution": 2, "videoResolution": 2, "recordDuration": 10, "recordAudio": 1, "workTimeOnOff": 1,
    "workTimeStartHour": 20, "workTimeStartMin": 0, "workTimeEndHour": 7, "workTimeEndMin": 5, "pirSens": 1,
    "pirCoolingTime": 5, "sidePirOnOff": 1, "enableAiFilter": 0, "sdTotalMB": 255, "sdFreeMB": 239,
    "recordMode": 0, "spkVol": 80, "lum": 0, "capability": {"platform": "T23ZN"}, "errorMsg": "Success",
}

JPEG = b"\xff\xd8\xff\xe0" + os.urandom(3000) + b"\xff\xd9"


class FakeCamera:
    """Enough CS2/ARTEMIS to log in, report status, list, send a photo/video and delete."""

    def __init__(self, files, h264=b""):
        self.files = list(files)               # newest first, like the camera
        self.h264 = h264
        self.streams = {}
        self.tx_seq = {}                       # per channel, like CS2
        self.deleted = []
        self.commands = []
        self.devinfo = dict(DEVINFO)
        self.lock = threading.Lock()

    def _drw(self, chn, payload):
        out = []
        for off in range(0, len(payload), 1024):
            seq = self.tx_seq.get(chn, 0)
            self.tx_seq[chn] = (seq + 1) & 0xFFFF
            out.append((CAM, tc.f1(0xD0, bytes([0xD1, chn]) + struct.pack(">H", seq) + payload[off:off + 1024])))
        return out

    def handle(self, data, dst):
        with self.lock:
            return self._handle(data, dst)

    def _handle(self, data, dst):
        if len(data) < 4 or data[0] != 0xF1:
            return []
        op, body = data[1], data[4:]
        if op == 0x30:                          # new P2P session: fresh sequence numbers
            self.streams, self.tx_seq = {}, {}
            return [(CAM, tc.f1(0x41, b"\x00\x02\x00\x00"))]
        if op != 0xD0 or body[0] != 0xD1:
            return []
        chn, seq = body[1], struct.unpack(">H", body[2:4])[0]
        out = [(CAM, tc.f1(0xD1, bytes([0xD1, chn, 0, 1]) + struct.pack(">H", seq)))]
        buf = self.streams.setdefault(chn, bytearray())
        buf += body[4:]
        while True:
            i = buf.find(tc.ARTEMIS)
            if i < 0 or len(buf) < i + 20:
                break
            kind, aseq, ln = struct.unpack("<III", buf[i + 8:i + 20])
            if len(buf) < i + 20 + ln:
                break
            req = tc.decode_cmd(bytes(buf[i + 20:i + 20 + ln]))
            del buf[:i + 20 + ln]
            out += self._command(req, aseq)
        return out

    def _reply(self, obj, aseq):
        return self._drw(0, tc.artemis(3, aseq, tc.encode_cmd(obj)))

    def _find(self, req):
        for f in self.files:
            if f["mediaDirNum"] == req["dirNum"] and f["mediaNum"] == req["mediaNum"]:
                return f
        return None

    def _command(self, req, aseq):
        cid = req["cmdId"]
        self.commands.append(cid)
        if cid == 525:
            return []
        if cid == 0:
            return self._reply({"cmdId": 0, "result": 0, "token": 4242}, aseq)
        if cid == 512:
            return self._reply(dict(self.devinfo, cmdId=512, result=0), aseq)
        if cid == 513:                          # settings: only the fields sent change
            self.devinfo.update({k: bool(v) if isinstance(self.devinfo.get(k), bool) else v
                                 for k, v in req.items() if k != "cmdId"})
            return self._reply({"cmdId": 513, "result": 0}, aseq)
        if cid == 768:
            page = self.files[req["pageNo"] * req["itemCntPerPage"]:][:req["itemCntPerPage"]]
            return self._reply({"cmdId": 768, "result": 0, "mediaFiles": page}, aseq)
        if cid == 773:
            f = self._find(req)
            if f:
                self.files.remove(f)
                self.deleted.append(f["mediaId"])
            return self._reply({"cmdId": 773, "result": 0, "delRet": 0 if f else 1}, aseq)
        if cid == 1285:
            dl = req["downloadReqs"][0]
            hdr = bytearray(72)
            struct.pack_into("<HHI", hdr, 0x20, dl["dirNum"], dl["mediaNum"], len(JPEG))
            return (self._reply({"cmdId": 1285, "cmdRet": 0, "result": 0}, aseq)
                    + self._drw(3, tc.artemis(4, 1, bytes(hdr) + JPEG)))
        if cid == 769:
            frames = split_annexb(self.h264)
            out = self._reply({"cmdId": 769, "result": 0, "startPbRet": 0, "totalFrame": len(frames)}, aseq)
            for n, fr in enumerate(frames):
                hdr = bytearray(108)
                struct.pack_into("<II", hdr, 16, len(fr), 0)
                data = bytearray(fr)
                for off in range(0, len(data), 0x1000):
                    if len(data) - off > 0x5F:
                        data[off:off + 0x60] = tc.cbc_encrypt(bytes(data[off:off + 0x60]))
                out += self._drw(2, tc.artemis(4, n, bytes(hdr) + bytes(data)))
            return out
        if cid == 770:
            return self._reply({"cmdId": 770, "result": 0}, aseq)
        return self._reply({"cmdId": cid, "result": -1}, aseq)


class FastCam(tc.TrailCam):
    connect_prelude = 0.3
    login_settle = 0.2
