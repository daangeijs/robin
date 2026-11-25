"""
TrailCam Go / "ARTEMIS over CS2-PPPP" client for the Denver WSW-8120
(BLE name TC100_xxxx, AP TrailCam_xxxx). Stdlib only.

Copied from the client that was tested end to end against the camera from a Mac
(scratchpad trailcam_client.py). Changes for the bridge: the UDP socket can be
injected (`TrailCam(sock=SerialUDP)`), the receive loop survives send errors,
AES decrypt uses lookup tables (the Pi 2 is slow), and the video idle timeout is
an attribute. Protocol logic is unchanged.

Markers:  [APP]  = seen in the APK code,  [HUB]/[PWN] = documented by those projects
          as verified on hardware,  [GUESS] = inference.

Waking the camera over BLE and joining its AP is done by the ESP32
(see docs/serial-link.md); camera = 192.168.43.1.
"""
import base64
import json
import os
import socket
import struct
import threading
import time

CAM_IP = "192.168.43.1"
LAN_SEARCH_PORT = 32108                      # [PWN] app broadcasts F1 30 00 00 here
AES_KEY = b"xs38nul7cqf7m1va"               # [APP] string in libArLink.so, [HUB][PWN]
USER, PASSWORD = "admin", "admin"           # [APP] logIn(..,"admin",accessPsw); default psw "admin"
PAGE = 40                                   # [APP] app limits itemCntPerPage to 1..40 (camera rejects >=50)

# ---------------------------------------------------------------- AES-128 (pure python)
_SBOX = bytearray(256)
_INV = bytearray(256)


def _init_sbox():
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (((q << 1) | (q >> 7)) & 0xFF) ^ (((q << 2) | (q >> 6)) & 0xFF) \
            ^ (((q << 3) | (q >> 5)) & 0xFF) ^ (((q << 4) | (q >> 4)) & 0xFF)
        _SBOX[p] = x ^ 0x63
        if p == 1:
            break
    _SBOX[0] = 0x63
    for i in range(256):
        _INV[_SBOX[i]] = i


_init_sbox()


def _xt(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


def _expand(key):
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xt(rcon)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


_RK = _expand(AES_KEY)
_M9, _M11, _M13, _M14 = (bytes(_mul(i, k) for i in range(256)) for k in (9, 11, 13, 14))


def _enc_block(b):
    s = [x ^ k for x, k in zip(b, _RK[0])]
    for r in range(1, 11):
        s = [_SBOX[x] for x in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]          # ShiftRows
        if r != 10:
            n = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                n += [_xt(a[0]) ^ _xt(a[1]) ^ a[1] ^ a[2] ^ a[3],
                      a[0] ^ _xt(a[1]) ^ _xt(a[2]) ^ a[2] ^ a[3],
                      a[0] ^ a[1] ^ _xt(a[2]) ^ _xt(a[3]) ^ a[3],
                      _xt(a[0]) ^ a[0] ^ a[1] ^ a[2] ^ _xt(a[3])]
            s = n
        s = [x ^ k for x, k in zip(s, _RK[r])]
    return bytes(s)


def _dec_block(b):
    s = [x ^ k for x, k in zip(b, _RK[10])]
    for r in range(9, -1, -1):
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]          # InvShiftRows
        s = [_INV[x] for x in s]
        s = [x ^ k for x, k in zip(s, _RK[r])]
        if r != 0:
            n = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                n += [_M14[a[0]] ^ _M11[a[1]] ^ _M13[a[2]] ^ _M9[a[3]],
                      _M9[a[0]] ^ _M14[a[1]] ^ _M11[a[2]] ^ _M13[a[3]],
                      _M13[a[0]] ^ _M9[a[1]] ^ _M14[a[2]] ^ _M11[a[3]],
                      _M11[a[0]] ^ _M13[a[1]] ^ _M9[a[2]] ^ _M14[a[3]]]
            s = n
    return bytes(s)


def cbc_encrypt(pt):  # zero IV, zero padding   [HUB][PWN]
    pt += b"\x00" * ((-len(pt)) % 16)
    out, prev = b"", b"\x00" * 16
    for i in range(0, len(pt), 16):
        prev = _enc_block(bytes(a ^ b for a, b in zip(pt[i:i + 16], prev)))
        out += prev
    return out


def cbc_decrypt(ct):
    out, prev = b"", b"\x00" * 16
    for i in range(0, len(ct) - len(ct) % 16, 16):
        blk = ct[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_dec_block(blk), prev))
        prev = blk
    return out


# self-check against a captured heartbeat MzlB36X/IVo8ZzI5rG9j1w== [HUB]. It decrypts to
# '{"cmdId":525}\0' plus 2 junk pad bytes, so the camera's padding is not checked; we pad with zeros.
assert cbc_decrypt(base64.b64decode("MzlB36X/IVo8ZzI5rG9j1w==")).startswith(b'{"cmdId":525}\x00')

# ---------------------------------------------------------------- framing
ARTEMIS = b"ARTEMIS\x00"


def f1(op, body=b""):
    return bytes([0xF1, op]) + struct.pack(">H", len(body)) + body


def artemis(kind, seq, payload):
    # 20-byte header: magic, u32 kind (2=cmd request, 3=cmd response, 4=media), u32 seq, u32 len
    return ARTEMIS + struct.pack("<III", kind, seq, len(payload)) + payload


def encode_cmd(obj):
    js = json.dumps(obj, separators=(",", ":")).encode()
    return base64.b64encode(cbc_encrypt(js + b"\x00")) + b"\x00"


def decode_cmd(payload):
    b64 = payload.split(b"\x00", 1)[0]
    b64 += b"=" * ((-len(b64)) % 4)
    pt = cbc_decrypt(base64.b64decode(b64)).rstrip(b"\x00")
    return json.loads(pt[pt.find(b"{"):pt.rfind(b"}") + 1])


class Channel:
    """One CS2 DRW channel (0=commands, 2=playback, 3/4=file+thumbnail bulk) [PWN][HUB]."""

    def __init__(self):
        self.next_rx = 0
        self.pending = {}
        self.stream = bytearray()
        self.lock = threading.Lock()


class TrailCam:
    video_idle = 3.0                          # s without stream data before a video counts as done
    connect_prelude = 3.0                     # [PWN] handshake prelude before login
    login_settle = 3.5                        # [HUB] first command only after a heartbeat
    on_video_progress = None                  # callable(frames, total_frames) during download_video

    def __init__(self, ip=CAM_IP, sock=None):
        self.ip = ip
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("0.0.0.0", 0))
        self.sock = sock
        self.sock.settimeout(0.2)
        self.addr = None                      # camera (ip, port), learned from first packet
        self.ch = {i: Channel() for i in range(8)}
        self.tx_seq = 0
        self.unacked = {}                     # seq -> (packet, last_sent)
        self.app_seq = 1
        self.hb_seq = 0x10001                 # [HUB][PWN] heartbeats use 0x10001..
        self.token = None
        self.responses = []                   # decoded JSON from channel 0
        self.cv = threading.Condition()
        self.stop = threading.Event()
        self.tx_lock = threading.Lock()

    # ---- low level
    def send(self, pkt):
        self.sock.sendto(pkt, self.addr)

    def _rx_loop(self):
        while not self.stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                try:
                    self._retransmit()
                except OSError:
                    pass                      # transport hiccup; next round retries
                continue
            except OSError:
                self.stop.set()               # transport gone (ESP ended the session)
                break
            if addr[0] != self.ip or len(data) < 4 or data[0] != 0xF1:
                continue
            if self.addr is None:
                self.addr = addr
            op, body = data[1], data[4:4 + struct.unpack(">H", data[2:4])[0]]
            if op in (0x41, 0x42, 0x43):
                # CS2 PUNCH_PKT / P2P_RDY (+0x43): echo back, app sends twice [PWN]
                self.send(f1(op, body))
                self.send(f1(op, body))
            elif op == 0xE0:                  # ALIVE -> must answer or camera sends CLOSE [HUB]
                self.send(f1(0xE1))
            elif op == 0xF0:                  # CLOSE from camera
                self.stop.set()
            elif op == 0xD1 and len(body) >= 4 and body[1] == 0:
                n = body[3]
                for i in range(n):
                    self.unacked.pop(struct.unpack(">H", body[4 + 2 * i:6 + 2 * i])[0], None)
            elif op == 0xD0 and len(body) >= 4 and body[0] == 0xD1:
                chn, seq = body[1], struct.unpack(">H", body[2:4])[0]
                # ACK immediately: D1 <ch> 00 <count> <seq16...>
                # [GUESS] single-index ACKs everywhere; app batches a 17-entry window for bulk
                try:
                    self.send(f1(0xD1, bytes([0xD1, chn, 0, 1]) + struct.pack(">H", seq)))
                except OSError:
                    pass                      # camera retransmits, we ACK the copy
                if chn in self.ch:
                    self._deliver(chn, seq, bytes(body[4:]))
        self.sock.close()

    def _retransmit(self):
        now = time.time()
        with self.tx_lock:
            for seq, (pkt, t) in list(self.unacked.items()):
                if now - t > 0.8:             # [HUB] 800 ms retransmit, window of 4
                    self.send(pkt)
                    self.unacked[seq] = (pkt, now)

    def _deliver(self, chn, seq, payload):
        c = self.ch[chn]
        with c.lock:
            if ((seq - c.next_rx) & 0xFFFF) >= 0x8000:
                return                        # duplicate
            c.pending[seq] = payload
            while c.next_rx in c.pending:
                c.stream += c.pending.pop(c.next_rx)
                c.next_rx = (c.next_rx + 1) & 0xFFFF
            if chn == 0:
                self._parse_cmd_stream(c)

    def _parse_cmd_stream(self, c):
        # Large responses span several DATA frames; only the first carries the header [HUB]
        while True:
            i = c.stream.find(ARTEMIS)
            if i < 0 or len(c.stream) < i + 20:
                return
            kind, seq, ln = struct.unpack("<III", c.stream[i + 8:i + 20])
            if len(c.stream) < i + 20 + ln:
                return
            payload = bytes(c.stream[i + 20:i + 20 + ln])
            del c.stream[:i + 20 + ln]
            try:
                obj = decode_cmd(payload)
            except Exception:
                continue
            with self.cv:
                self.responses.append(obj)
                self.cv.notify_all()

    def _send_data(self, chn, payload):
        for off in range(0, len(payload), 1024):   # [PWN] 1 KB chunks
            with self.tx_lock:
                seq = self.tx_seq
                self.tx_seq = (self.tx_seq + 1) & 0xFFFF
                pkt = f1(0xD0, bytes([0xD1, chn]) + struct.pack(">H", seq) + payload[off:off + 1024])
                self.unacked[seq] = (pkt, time.time())
            self.send(pkt)

    def cmd(self, obj, wait=True, timeout=15.0, heartbeat=False):
        if self.token is not None and not heartbeat:
            obj = dict(obj, token=self.token)       # [HUB][PWN] every command after login
        if heartbeat:
            seq, self.hb_seq = self.hb_seq, self.hb_seq + 1
        else:
            seq, self.app_seq = self.app_seq, self.app_seq + 1
        self._send_data(0, artemis(2, seq, encode_cmd(obj)))
        if not wait:
            return None
        end = time.time() + timeout
        with self.cv:
            while time.time() < end:
                for r in self.responses:
                    if r.get("cmdId") == obj["cmdId"]:
                        self.responses.remove(r)
                        return r
                self.cv.wait(0.2)
        raise TimeoutError("no answer to cmdId %s" % obj["cmdId"])

    def _hb_loop(self):
        while not self.stop.wait(3.0):        # [HUB][PWN] ~3 s
            try:
                self.cmd({"cmdId": 525}, wait=False, heartbeat=True)
            except OSError:
                pass

    # ---- session
    def connect(self):
        # CS2 LAN search; camera answers with 0x41 PUNCH_PKT from its session port [PWN]
        for _ in range(4):
            for dst in (("192.168.43.255", LAN_SEARCH_PORT), ("255.255.255.255", LAN_SEARCH_PORT),
                        (self.ip, LAN_SEARCH_PORT)):
                try:
                    self.sock.sendto(f1(0x30), dst)
                except OSError:
                    pass
            time.sleep(0.05)
        end = time.time() + 5
        while self.addr is None and time.time() < end:
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            if addr[0] == self.ip:
                self.addr = addr
                # [GUESS] re-inject the first packet by echoing it if it is a punch/ready
                if len(data) >= 4 and data[0] == 0xF1 and data[1] in (0x41, 0x42):
                    self.send(data)
                    self.send(data)
        if self.addr is None:
            raise TimeoutError("camera did not answer LAN search")
        threading.Thread(target=self._rx_loop, daemon=True).start()
        time.sleep(self.connect_prelude)      # [PWN] ~3 s handshake prelude before login
        # [HUB] alternatively sends PRE_LOGIN 0xF9 (AES key a01bc23ed45fF56A) + 'Magic1/2'; pwn's
        # client works without, so it is omitted here.
        login = {"cmdId": 0, "usrName": USER, "password": PASSWORD, "needVideo": 0, "needAudio": 0,
                 # [PWN] local wall clock written as if it were UTC; app passes local secs too [APP]
                 "utcTime": int(time.time() + time.localtime().tm_gmtoff),
                 "supportHeartBeat": True}
        r = self.cmd(login, timeout=8)
        if r.get("result") != 0:
            raise RuntimeError("login failed: %r" % r)
        self.token = r["token"]
        threading.Thread(target=self._hb_loop, daemon=True).start()
        time.sleep(self.login_settle)         # [HUB] settle: first command only after a heartbeat
        return r

    def close(self):
        # App logout = PPCS_Connect_Break + PPCS_Close, no ARTEMIS logout command [APP]
        try:
            self.send(f1(0xF0))               # CS2 MSG_CLOSE   [GUESS: exact bytes PPCS_Close sends]
        except OSError:
            pass
        self.stop.set()

    # ---- operations
    def dev_info(self):
        return self.cmd({"cmdId": 512})       # batPercent, sdTotalMB, sdFreeMB, workMode, ...

    def set_settings(self, fields):
        """Change camera settings, as the app does: cmdId 513 with only the changed fields [APP]."""
        r = self.cmd(dict({"cmdId": 513}, **fields))
        if r.get("cmdRet") not in (0, None) or r.get("result") not in (0, None):
            raise RuntimeError("camera refused settings %s: %s" % (fields, r))
        return r

    def list_media(self):
        out, page = [], 0
        while True:
            r = self.cmd({"cmdId": 768, "itemCntPerPage": PAGE, "pageNo": page})
            files = r.get("mediaFiles", [])
            out += files
            if len(files) < PAGE or r.get("hasMore") is False:
                return out                    # list is newest first; pageNo is 0-based [HUB]
            page += 1

    def delete(self, f):
        # flat fields, dirNum (not mediaDirNum), fileType exactly as listed [HUB]
        r = self.cmd({"cmdId": 773, "fileType": f["fileType"], "dirNum": f["mediaDirNum"],
                      "mediaNum": f["mediaNum"]})
        return r.get("delRet") == 0           # result is 0 even when nothing was deleted [HUB]

    def _collect(self, chn, idle=4.0, limit=120.0, done=None):
        c, start, last, size = self.ch[chn], time.time(), time.time(), -1
        while time.time() - start < limit:
            time.sleep(0.2)
            with c.lock:
                n = len(c.stream)
            if n != size:
                size, last = n, time.time()
            if done and done(c):
                break
            if size > 0 and time.time() - last > idle:
                break
        with c.lock:
            data = bytes(c.stream)
            c.stream.clear()
        return data

    @staticmethod
    def _records(blob):
        i = 0
        while True:
            i = blob.find(ARTEMIS, i)
            if i < 0 or i + 20 > len(blob):
                return
            kind, seq, ln = struct.unpack("<III", blob[i + 8:i + 20])
            if ln <= 0 or i + 20 + ln > len(blob):
                i += 1
                continue
            yield kind, seq, blob[i + 20:i + 20 + ln]
            i += 20 + ln

    def download_photo(self, f):
        req = {"cmdId": 1285, "downloadReqs": [{"fileType": f["fileType"], "dirNum": f["mediaDirNum"],
                                                "mediaNum": f["mediaNum"]}]}
        r = self.cmd(req)                     # {"cmdRet":0,"result":0,"cmdId":1285}
        if r.get("cmdRet") not in (0, None):
            raise RuntimeError(r)
        blob = self._collect(3) or self._collect(4, idle=1.0, limit=5)
        for kind, seq, p in self._records(blob):
            # 72-byte transfer header: dirNum@0x20 u16, mediaNum@0x22 u16, dataLen@0x24 u32,
            # mediaId@0x30 u32 (all LE) [PWN]
            if len(p) < 72:
                continue
            d, m, ln = struct.unpack_from("<HHI", p, 0x20)
            if d == f["mediaDirNum"] and m == f["mediaNum"]:
                return p[72:72 + ln] if ln else p[72:]
        raise RuntimeError("no photo data received")

    def download_video(self, f, out_h264, out_aac):
        # Videos come via playback, not 1285 [HUB]. fileType: try list value, then 1 [HUB]
        sess = int.from_bytes(os.urandom(2), "big")
        for ft in (f["fileType"], 1):
            r = self.cmd({"cmdId": 769, "fileType": ft, "dirNum": f["mediaDirNum"],
                          "mediaNum": f["mediaNum"], "sessionNo": sess})
            if r.get("startPbRet") == 0 and r.get("result") == 0:
                break
        else:
            raise RuntimeError("playback refused: %r" % r)
        total = r.get("totalFrame", 0)
        if self.on_video_progress:
            self.on_video_progress(0, total)
        c, vframes = self.ch[2], [0]
        with open(out_h264, "wb") as vh, open(out_aac, "wb") as ah:
            buf = bytearray()
            end_idle = time.time()
            while time.time() - end_idle < (0.5 if total and vframes[0] >= total else self.video_idle):
                time.sleep(0.05)
                with c.lock:
                    if c.stream:
                        buf += c.stream
                        c.stream.clear()
                        end_idle = time.time()
                while True:                   # consume complete records from buf
                    i = buf.find(ARTEMIS)
                    if i < 0 or len(buf) < i + 20:
                        break
                    kind, seq, ln = struct.unpack("<III", buf[i + 8:i + 20])
                    if len(buf) < i + 20 + ln:
                        break
                    p = bytes(buf[i + 20:i + 20 + ln])
                    del buf[:i + 20 + ln]
                    if kind != 4 or len(p) < 108:
                        continue
                    vlen, alen = struct.unpack_from("<II", p, 16)   # 108-byte header [HUB][PWN]
                    data = bytearray(p[108:])
                    for off in range(0, len(data), 0x1000):         # first 0x60 of each page
                        if len(data) - off > 0x5F:                  # encrypted, short tail plain
                            data[off:off + 0x60] = cbc_decrypt(bytes(data[off:off + 0x60]))
                    if vlen:
                        s = data.find(b"\x00\x00\x00\x01")          # [GUESS] alignment prefix
                        frame = data[s:] if s >= 0 else data
                        if not frame.startswith(b"\x00\x00\x00\x01\x01"):   # app drops marker
                            vh.write(frame)
                            vframes[0] += 1
                            if self.on_video_progress:
                                self.on_video_progress(vframes[0], total)
                    elif alen:
                        ah.write(data)
        try:
            self.cmd({"cmdId": 770}, timeout=5)
        except TimeoutError:
            pass  # the stream ended on its own; the camera does not always answer the stop
        time.sleep(1.0)                       # let stray frames of this stream drain [HUB]
        return vframes[0]
