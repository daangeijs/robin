"""trailcam-bridge: camera sessions through the ESP32 serial link, clips over HTTP.

Configuration comes from TRAILCAM_* environment variables (see trailcam-bridge.env).
"""
import argparse
import json
import logging
import signal
import sys
import threading

from .config import Config


def build(cfg):
    from .link import Link, open_serial
    from .session import Bridge
    from .store import Store

    link = Link(lambda: open_serial(cfg.serial_port, cfg.baud), settle=3.0).open()
    return link, Bridge(cfg, link, Store(cfg.data_dir))


def cmd_serve(cfg):
    from .api import make_server

    link, bridge = build(cfg)
    bridge.start_esp_monitor(60.0)
    srv = make_server(bridge, cfg)
    logging.info("listening on %s:%d; sessions only on POST /poll", cfg.http_host, cfg.http_port)

    def shutdown(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        srv.serve_forever()
    finally:
        if bridge.running:                    # do not leave the ESP on the camera AP
            logging.info("waiting for the running session to finish")
            with bridge.lock:
                pass
        link.close()


def cmd_poll(cfg):
    link, bridge = build(cfg)
    try:
        res = bridge.run(trigger="cli")
        print(json.dumps(res, indent=1))
        return 0 if res and not res.get("error") else 1
    finally:
        link.close()


def cmd_ping(cfg):
    link, _ = build(cfg)
    try:
        print(json.dumps(link.ping()))
        return 0
    finally:
        link.close()


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m trailcam_bridge", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="serve", choices=["serve", "poll", "ping", "config"],
                   help="serve: HTTP API (default); poll: one session now; "
                        "ping: ESP liveness; config: print effective configuration")
    args = p.parse_args(argv)
    cfg = Config.from_env()
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "config":
        d = dict(vars(cfg), token="***" if cfg.token else "")
        print(json.dumps(d, indent=1))
        return 0
    return {"serve": cmd_serve, "poll": cmd_poll, "ping": cmd_ping}[args.command](cfg) or 0


if __name__ == "__main__":
    sys.exit(main())
