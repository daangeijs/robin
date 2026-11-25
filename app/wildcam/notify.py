"""Home Assistant notifications via a webhook trigger."""

from __future__ import annotations

import json
import logging
import urllib.request

from .config import Settings

log = logging.getLogger(__name__)


def should_notify(settings: Settings, label: str | None, common_name: str | None, first_time: bool) -> bool:
    if not settings.ha_webhook_url or not label or label in ("leeg", "mens", "voertuig"):
        return False
    names = {label.lower(), (common_name or "").lower()}
    if names & set(settings.notify_species):
        return True
    return settings.notify_new_species and first_time


def send(settings: Settings, clip: dict, first_time: bool) -> bool:
    base = settings.base_url
    payload = {
        "event": "wildcam_detection",
        "label": clip.get("label"),
        "common_name": clip.get("common_name"),
        "score": clip.get("score"),
        "first_time": first_time,
        "captured_at": clip.get("captured_at"),
        "clip_id": clip.get("id"),
        "clip_url": f"{base}/clip/{clip['id']}" if base else None,
        "thumb_url": f"{base}/thumbs/{clip['thumb']}" if base and clip.get("thumb") else None,
    }
    req = urllib.request.Request(
        settings.ha_webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except OSError as exc:
        log.warning("Home Assistant webhook failed: %s", exc)
        return False
