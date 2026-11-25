"""Serial link to the ESP32: SLIP frames with CRC-16, per docs/serial-link.md.

Frame on the wire: END | slip(type + payload + crc16_be) | END
"""
import binascii
import errno
import json
import logging
import queue
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD
MAX_PAYLOAD = 1500
MAX_UDP = MAX_PAYLOAD - 6                     # ip (4) + port (2) + data

# Pi -> ESP
PING, START, UDP_SEND, STOP, STATUS = 0x01, 0x02, 0x03, 0x04, 0x05
# ESP -> Pi
PONG, READY, FAIL, STOPPED, UDP_RECV = 0x81, 0x82, 0x83, 0x84, 0x85

START_TIMEOUT = 90.0
STOP_TIMEOUT = 60.0                           # ESP rejoins home wifi before STOPPED
PING_TIMEOUT = 2.0


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout.

    binascii.crc_hqx is that CRC in C; a Python bit loop is too slow for a Pi 2 at 921600 baud.
    """
    return binascii.crc_hqx(data, crc)


def slip_escape(data):
    return data.replace(b"\xdb", b"\xdb\xdd").replace(b"\xc0", b"\xdb\xdc")


def encode_frame(ftype, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large: %d" % len(payload))
    body = bytes([ftype]) + payload
    body += struct.pack(">H", crc16(body))
    return b"\xc0" + slip_escape(body) + b"\xc0"


class SlipDecoder:
    """Feed raw bytes, get (type, payload) for every frame with a valid crc."""

    def __init__(self):
        self.buf = bytearray()
        self.esc = False
        self.overflow = False
        self.bad = 0                          # frames dropped for crc/length/escape errors

    def feed(self, data):
        """Whole frames at a time (split on END), so a Pi 2 keeps up with the line."""
        out = []
        self.buf += data
        *frames, rest = self.buf.split(b"\xc0")
        self.buf = bytearray(rest)
        if len(self.buf) > 2 * (MAX_PAYLOAD + 3):  # no END for too long, e.g. ESPHome log text
            self.buf.clear()
            self.overflow = True
        for raw in frames:
            if self.overflow:  # the tail of an oversized run: skip it, the next frame is clean
                self.overflow = False
                self.bad += 1
                continue
            if not raw:
                continue
            frame = self._unescape(raw)
            if frame is None or len(frame) > MAX_PAYLOAD + 3:
                self.bad += 1
                continue
            checked = self._check(frame)
            if checked:
                out.append(checked)
        return out

    @staticmethod
    def _unescape(raw):
        if b"\xdb" not in raw:
            return bytes(raw)
        first, *parts = bytes(raw).split(b"\xdb")
        out = [first]
        for part in parts:
            if not part or part[0] not in (ESC_END, ESC_ESC):
                return None  # protocol error
            out.append(b"\xc0" if part[0] == ESC_END else b"\xdb")
            out.append(part[1:])
        return b"".join(out)

    def _check(self, raw):
        if len(raw) < 3 or crc16(raw[:-2]) != struct.unpack(">H", raw[-2:])[0]:
            self.bad += 1
            return None
        return raw[0], raw[1:-2]


def pack_addr(addr):
    ip, port = addr
    if ip in ("<broadcast>", ""):
        ip = "255.255.255.255"
    return socket.inet_aton(ip) + struct.pack(">H", port)


def unpack_addr(payload):
    return (socket.inet_ntoa(payload[:4]), struct.unpack(">H", payload[4:6])[0])


class LinkError(Exception):
    pass


class StartFailed(LinkError):
    def __init__(self, reason):
        super().__init__("ESP start failed: %s" % reason)
        self.reason = reason


def open_serial(port, baud):
    """Open the CP2102 without toggling DTR/RTS (they drive EN/IO0 on ESP32 boards)."""
    import serial  # pyserial, only needed on the Pi

    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 0.1
    s.write_timeout = 5
    s.dtr = False
    s.rts = False
    s.open()
    try:                                      # closing must not drop DTR (another reset)
        import termios
        attrs = termios.tcgetattr(s.fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(s.fd, termios.TCSANOW, attrs)
    except Exception as e:
        log.debug("could not clear HUPCL: %s", e)
    return s


class Link:
    """Owns the serial port: reader thread, request/response, UDP relay.

    `opener` returns a serial-like object (read(n), write(b), close(), in_waiting);
    it is called again after a read/write error (USB replug).
    """

    def __init__(self, opener, udp_queue=4096, settle=0.0, ping_wait=10.0):
        self._opener = opener
        self.settle = settle                  # s of input to discard after open (ESP resets on open)
        self.ping_wait = ping_wait
        self.ready = threading.Event()        # ESP answered a PING since the last (re)open
        self._ser = None
        self._wlock = threading.Lock()
        self._rlock = threading.Lock()        # one request at a time
        self._ctrl = queue.Queue()
        self._udp = None                      # current SerialUDP, gets UDP_RECV
        self._udp_queue = udp_queue
        self._closed = threading.Event()
        self._thread = None
        self.decoder = SlipDecoder()
        self.session_ended = threading.Event()  # set on any STOPPED
        self.last_stopped = None
        self.stats = {"rx_frames": 0, "tx_frames": 0, "udp_rx": 0, "udp_tx": 0,
                      "udp_dropped": 0, "unknown": 0, "reopen": 0}

    # ---- lifecycle
    def open(self):
        self._ser = self._opener()
        self._thread = threading.Thread(target=self._reader, name="serial-reader", daemon=True)
        self._thread.start()
        self._wait_esp()
        return self

    def _wait_esp(self):
        """Opening the port resets the ESP: skip its boot output, then PING until PONG."""
        self.ready.clear()
        if self.settle:
            time.sleep(self.settle)
            self.decoder.bad = 0
        end = time.monotonic() + self.ping_wait
        while time.monotonic() < end and not self._closed.is_set():
            try:
                info = self.ping(timeout=1.0)
                self.ready.set()
                log.info("ESP up: %s", info)
                return True
            except LinkError:
                pass
        log.warning("ESP did not answer PING within %.0f s after open", self.settle + self.ping_wait)
        return False

    def close(self):
        self._closed.set()
        ser, self._ser = self._ser, None
        if ser:
            try:
                ser.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(2)

    def _reopen(self):
        old, self._ser = self._ser, None
        if old:
            try:
                old.close()
            except Exception:
                pass
        while not self._closed.wait(5):
            try:
                self._ser = self._opener()
                self.stats["reopen"] += 1
                self.decoder = SlipDecoder()
                log.info("serial port reopened")
                threading.Thread(target=self._wait_esp, daemon=True).start()
                return
            except Exception as e:
                log.warning("serial reopen failed: %s", e)

    def _reader(self):
        while not self._closed.is_set():
            ser = self._ser
            if ser is None:
                self._reopen()
                continue
            try:
                data = ser.read(max(1, getattr(ser, "in_waiting", 0) or 0))
            except Exception as e:
                if self._closed.is_set():
                    return
                log.warning("serial read error: %s", e)
                self._reopen()
                continue
            if data:
                for ftype, payload in self.decoder.feed(data):
                    self._dispatch(ftype, payload)

    def _dispatch(self, ftype, payload):
        self.stats["rx_frames"] += 1
        if ftype == UDP_RECV:
            if len(payload) < 6:
                return
            udp = self._udp
            if udp is None:
                self.stats["udp_dropped"] += 1
                return
            self.stats["udp_rx"] += 1
            udp._push(payload[6:], unpack_addr(payload))
            return
        if ftype in (PONG, READY, FAIL, STOPPED):
            try:
                info = json.loads(payload.decode() or "{}")
            except ValueError:
                info = {"raw": payload.decode("latin-1")}
            if ftype == STOPPED:
                self.last_stopped = info
                self.session_ended.set()
                udp = self._udp
                if udp is not None:
                    udp._ended()
                log.info("ESP: STOPPED %s", info)
            self._ctrl.put((ftype, info))
            return
        self.stats["unknown"] += 1

    # ---- sending
    def send(self, ftype, payload=b""):
        frame = encode_frame(ftype, payload)
        with self._wlock:
            ser = self._ser
            if ser is None:
                raise LinkError("serial port not open")
            try:
                ser.write(frame)
            except Exception as e:
                raise LinkError("serial write failed: %s" % e) from e
            self.stats["tx_frames"] += 1

    def _request(self, ftype, expect, timeout, payload=b""):
        with self._rlock:
            while True:                       # drop stale control frames
                try:
                    self._ctrl.get_nowait()
                except queue.Empty:
                    break
            self.send(ftype, payload)
            end = time.monotonic() + timeout
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    raise LinkError("no answer to frame 0x%02x within %.0f s" % (ftype, timeout))
                try:
                    rtype, info = self._ctrl.get(timeout=left)
                except queue.Empty:
                    continue
                if rtype in expect:
                    return rtype, info

    # ---- requests
    def ping(self, timeout=PING_TIMEOUT):
        return self._request(PING, {PONG}, timeout)[1]

    def start(self, timeout=START_TIMEOUT):
        """Wake the camera and join its AP. Returns READY info, raises StartFailed."""
        self.session_ended.clear()
        rtype, info = self._request(START, {READY, FAIL}, timeout)
        if rtype == FAIL:
            raise StartFailed(info.get("reason", "unknown"))
        return info

    def stop(self, timeout=STOP_TIMEOUT):
        if self.session_ended.is_set():       # ESP already stopped on its own
            self.session_ended.clear()
            return self.last_stopped
        rtype, info = self._request(STOP, {STOPPED}, timeout)
        self.session_ended.clear()
        return info

    def status(self, obj):
        payload = json.dumps(obj, separators=(",", ":")).encode()
        self.send(STATUS, payload)

    def udp_socket(self):
        """A fresh socket-like object for one camera session."""
        sock = SerialUDP(self, self._udp_queue)
        self._udp = sock
        return sock

    def _detach(self, sock):
        if self._udp is sock:
            self._udp = None


class SerialUDP:
    """Just enough of a UDP socket for TrailCam, relayed through the ESP32."""

    def __init__(self, link, maxsize=4096):
        self._link = link
        self._q = queue.Queue(maxsize)
        self._timeout = None
        self._closed = False
        self._gone = False
        self.dropped = 0

    # called by the link reader
    def _push(self, data, addr):
        try:
            self._q.put_nowait((data, addr))
        except queue.Full:
            self.dropped += 1
            self._link.stats["udp_dropped"] += 1

    def _ended(self):
        self._gone = True

    # socket API
    def settimeout(self, t):
        self._timeout = t

    def gettimeout(self):
        return self._timeout

    def setsockopt(self, *args):
        pass

    def bind(self, addr):
        pass

    def sendto(self, data, addr):
        if self._closed:
            raise OSError(errno.EBADF, "socket closed")
        if self._gone:
            raise OSError(errno.ENETDOWN, "ESP ended the session")
        if len(data) > MAX_UDP:
            raise OSError(errno.EMSGSIZE, "datagram too large for serial frame")
        try:
            self._link.send(UDP_SEND, pack_addr(addr) + bytes(data))
        except LinkError as e:
            raise OSError(errno.EIO, str(e)) from e
        self._link.stats["udp_tx"] += 1
        return len(data)

    def recvfrom(self, bufsize):
        if self._closed:
            raise OSError(errno.EBADF, "socket closed")
        try:
            if self._timeout is None:
                while True:
                    try:
                        data, addr = self._q.get(timeout=1.0)
                        break
                    except queue.Empty:
                        if self._closed or self._gone:
                            raise OSError(errno.ENETDOWN, "ESP ended the session")
            elif self._timeout <= 0:
                data, addr = self._q.get_nowait()
            else:
                data, addr = self._q.get(timeout=self._timeout)
        except queue.Empty:
            if self._gone:
                raise OSError(errno.ENETDOWN, "ESP ended the session") from None
            raise socket.timeout("timed out") from None
        return data[:bufsize], addr

    def close(self):
        self._closed = True
        self._link._detach(self)
