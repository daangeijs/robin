import json
import os
import socket
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trailcam_bridge import link as L  # noqa: E402
from fakes import FakeESP, FakeSerial  # noqa: E402


class FramingTest(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(L.crc16(b"123456789"), 0x29B1)    # CRC-16/CCITT-FALSE check value

    def test_encode_layout(self):
        f = L.encode_frame(L.PING)
        self.assertEqual(f[0], 0xC0)
        self.assertEqual(f[-1], 0xC0)
        self.assertEqual(f[1:-1], bytes([0x01]) + L.crc16(b"\x01").to_bytes(2, "big"))

    def test_roundtrip_with_escapes(self):
        payload = bytes(range(256)) + b"\xc0\xdb\xc0\xdb\xdc\xdd" * 10
        frame = L.encode_frame(L.UDP_RECV, payload)
        self.assertNotIn(b"\xc0", frame[1:-1])
        self.assertEqual(L.SlipDecoder().feed(frame), [(L.UDP_RECV, payload)])

    def test_byte_by_byte_and_noise(self):
        d = L.SlipDecoder()
        stream = b"[I][esphome] log text\r\n" + L.encode_frame(L.PONG, b'{"state":"idle"}') \
            + L.encode_frame(L.READY, b"{}")
        out = []
        for b in stream:
            out += d.feed(bytes([b]))
        self.assertEqual(out, [(L.PONG, b'{"state":"idle"}'), (L.READY, b"{}")])
        self.assertEqual(d.bad, 1)                          # the log text

    def test_bad_crc_short_and_oversize_dropped(self):
        good = L.encode_frame(L.STOPPED, b"{}")
        corrupt = bytearray(good)
        corrupt[2] ^= 0x01
        short = b"\xc0\x84\x00\xc0"
        oversize = b"\xc0" + b"A" * (L.MAX_PAYLOAD + 10) + b"\xc0"
        d = L.SlipDecoder()
        self.assertEqual(d.feed(bytes(corrupt) + short + oversize + good), [(L.STOPPED, b"{}")])
        self.assertEqual(d.bad, 3)

    def test_max_payload(self):
        L.encode_frame(L.UDP_SEND, b"x" * L.MAX_PAYLOAD)
        with self.assertRaises(ValueError):
            L.encode_frame(L.UDP_SEND, b"x" * (L.MAX_PAYLOAD + 1))

    def test_addr(self):
        self.assertEqual(L.pack_addr(("192.168.43.255", 32108)), bytes([192, 168, 43, 255, 0x7D, 0x6C]))
        self.assertEqual(L.unpack_addr(L.pack_addr(("10.1.2.3", 65535))), ("10.1.2.3", 65535))
        self.assertEqual(L.pack_addr(("<broadcast>", 1))[:4], b"\xff\xff\xff\xff")


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.pi, self.esp_end = FakeSerial.pair()
        self.link = L.Link(lambda: self.pi, ping_wait=0).open()

    def tearDown(self):
        self.link.close()
        self.esp_end.close()

    def test_ping_start_stop_status(self):
        esp = FakeESP(self.esp_end)
        self.assertEqual(self.link.ping()["state"], "idle")
        self.assertEqual(self.link.start()["ssid"], "TrailCam_TEST")
        with self.assertRaises(L.StartFailed) as cm:
            self.link.start(timeout=2)
        self.assertEqual(cm.exception.reason, "busy")
        self.assertEqual(self.link.stop()["reason"], "requested")
        self.link.status({"battery": 60, "result": "2 clips"})
        time.sleep(0.2)
        self.assertEqual(esp.status, [{"battery": 60, "result": "2 clips"}])

    def test_start_fail_and_timeout(self):
        FakeESP(self.esp_end, start_reply=(L.FAIL, {"reason": "ble_timeout"}))
        with self.assertRaises(L.StartFailed) as cm:
            self.link.start(timeout=2)
        self.assertEqual(cm.exception.reason, "ble_timeout")

    def test_start_no_answer(self):
        FakeESP(self.esp_end, start_reply=(None, None))
        t = time.monotonic()
        with self.assertRaises(L.LinkError):
            self.link.start(timeout=0.5)
        self.assertLess(time.monotonic() - t, 2)

    def test_serial_udp(self):
        esp = FakeESP(self.esp_end)
        self.link.start()
        sock = self.link.udp_socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)   # no-op
        sock.settimeout(0.2)
        t = time.monotonic()
        with self.assertRaises(socket.timeout):
            sock.recvfrom(2048)
        self.assertGreaterEqual(time.monotonic() - t, 0.18)

        # UDP_SEND carries ip + port + data
        sock.sendto(b"\xf1\x30\x00\x00", ("192.168.43.255", 32108))
        time.sleep(0.1)
        self.assertNotIn(L.UDP_SEND, [t for t, _ in esp.received])   # FakeESP keeps UDP out of the log

        # UDP_RECV is queued and returned with its source address
        esp.send(L.UDP_RECV, L.pack_addr(("192.168.43.1", 40000)) + b"hello")
        self.assertEqual(sock.recvfrom(2048), (b"hello", ("192.168.43.1", 40000)))
        esp.send(L.UDP_RECV, L.pack_addr(("192.168.43.1", 40000)) + b"0123456789")
        self.assertEqual(sock.recvfrom(4)[0], b"0123")

        with self.assertRaises(OSError):
            sock.sendto(b"x" * (L.MAX_UDP + 1), ("192.168.43.1", 1))

        # an unsolicited STOPPED (ESP watchdog) ends the socket
        esp.send(L.STOPPED, {"reason": "timeout"})
        time.sleep(0.2)
        with self.assertRaises(OSError):
            sock.sendto(b"x", ("192.168.43.1", 1))
        with self.assertRaises(OSError) as cm:
            sock.recvfrom(10)
        self.assertNotIsInstance(cm.exception, socket.timeout)
        self.assertEqual(self.link.stop()["reason"], "timeout")      # no second STOP needed

    def test_udp_overflow_counts(self):
        esp = FakeESP(self.esp_end)
        self.link._udp_queue = 3
        self.link.start()
        sock = self.link.udp_socket()
        for i in range(5):
            esp.send(L.UDP_RECV, L.pack_addr(("192.168.43.1", 1)) + bytes([i]))
        time.sleep(0.3)
        self.assertEqual(sock.dropped, 2)
        self.assertEqual(self.link.stats["udp_dropped"], 2)
        sock.settimeout(0)
        got = [sock.recvfrom(10)[0] for _ in range(3)]
        self.assertEqual(got, [b"\x00", b"\x01", b"\x02"])

    def test_status_json_compact(self):
        esp = FakeESP(self.esp_end)
        self.link.status({"a": 1})
        time.sleep(0.2)
        self.assertEqual(esp.received[-1], (L.STATUS, json.dumps({"a": 1}, separators=(",", ":")).encode()))


if __name__ == "__main__":
    unittest.main()
