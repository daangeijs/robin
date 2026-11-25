# trailcam-bridge

The Raspberry Pi side of the camera link. It talks to the trail camera through the ESP32 on USB, downloads the clips, deletes them from the camera once they're safely stored, and offers them over HTTP to Robin.

## Parts

- `trailcam_bridge/link.py`: SLIP + CRC-16 frames over the serial line (PING/START/STOP/STATUS) and `SerialUDP`, a UDP socket stand-in that tunnels packets through the ESP32.
- `trailcam_bridge/camera.py`: the camera client (`TrailCam(sock=...)`): login, status, file list, photo/video download, delete, settings.
- `trailcam_bridge/session.py`: one session: wake, log in, status, list, download (video to MP4 with ffmpeg, photo to JPEG), verify (ffprobe ≥ 2 s, JPEG header), delete on the camera, sleep, status to the ESP32.
- `trailcam_bridge/api.py`: the HTTP API. Passive: a session only starts on `POST /poll`. Every 60 s it pings the ESP32 for `GET /status` (that doesn't wake the camera).
- Storage: `/var/lib/trailcam-bridge/clips/`, an index in `index.db` (SQLite), the last status in `state.json`.

## Install

```sh
rsync -a --exclude tests --exclude __pycache__ bridge/ pi@192.168.1.50:trailcam-bridge/
ssh pi@192.168.1.50 'cd trailcam-bridge && sudo ./install.sh'
ssh pi@192.168.1.50 'sudo systemctl enable --now trailcam-bridge'   # once the ESP32 firmware is on
```

Settings live in `/etc/trailcam-bridge.env` (see `trailcam-bridge.env`): the serial port, a token, the timezone and a few timings. One manual check without the service:

```sh
sudo -u trailcam sh -c 'set -a; . /etc/trailcam-bridge.env; cd /opt/trailcam-bridge && exec venv/bin/python -m trailcam_bridge ping'
```

## API

Port 8090 by default. With `TRAILCAM_TOKEN` set, clients send `Authorization: Bearer <token>`.

| | |
| --- | --- |
| `GET /status` | `esp` (last ping), `camera` (last status), `last_session`, `bridge.state` (`idle`/`session`, with live progress during a session), number of clips |
| `GET /clips[?since=ISO]` | stored clips, newest first; `since` filters on `fetched_at` |
| `GET /clips/<id>` | the file (`video/mp4` or `image/jpeg`) |
| `DELETE /clips/<id>` | remove it from the Pi (the client has it) |
| `POST /poll` | start a session now: 202, or 409 when one is already running |
| `POST /settings` | queue camera settings for the next session, e.g. `{"osdInfoEnable": 0}` to switch off the camera's own date stamp |

A client does `POST /poll`, waits on `GET /status` until `bridge.state` is `idle` again and `last_session.finished_at` is after its request, then fetches `GET /clips`, downloads each clip and deletes it.

## Tests

```sh
python -m pytest tests
```

The tests run a full session against a fake ESP32 and a fake camera.
