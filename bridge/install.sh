#!/bin/sh
# Install or update trailcam-bridge on the Pi. Run from the bridge/ directory: sudo ./install.sh
# Does not enable or start the service.
set -eu
cd "$(dirname "$0")"

id trailcam >/dev/null 2>&1 || useradd --system --home-dir /var/lib/trailcam-bridge --shell /usr/sbin/nologin trailcam
usermod -a -G dialout trailcam

install -d -m 755 /opt/trailcam-bridge /opt/trailcam-bridge/trailcam_bridge
install -m 644 trailcam_bridge/*.py /opt/trailcam-bridge/trailcam_bridge/
[ -x /opt/trailcam-bridge/venv/bin/python ] || python3 -m venv /opt/trailcam-bridge/venv
/opt/trailcam-bridge/venv/bin/pip install --quiet --disable-pip-version-check pyserial

install -d -o trailcam -g trailcam -m 750 /var/lib/trailcam-bridge
[ -f /etc/trailcam-bridge.env ] || install -m 640 -g trailcam trailcam-bridge.env /etc/trailcam-bridge.env
install -m 644 trailcam-bridge.service /etc/systemd/system/trailcam-bridge.service
systemctl daemon-reload
echo "installed; enable with: sudo systemctl enable --now trailcam-bridge"
