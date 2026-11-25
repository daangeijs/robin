"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# The app's name as shown in the top bar, page titles and texts. Kept in one place for a later rename.
APP_NAME = "Robin"  # named after the first visitor; shown name only, code and URLs keep "wildcam"
APP_TAGLINE = "multi-wildcam-app"  # Dutch source text, translated in i18n.EN


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("WILDCAM_DATA_DIR", "/data")))
    # Where originals live. Point this at an NFS share on the NAS if you like.
    media_dir: Path = field(
        default_factory=lambda: Path(_env("WILDCAM_MEDIA_DIR", "")) if _env("WILDCAM_MEDIA_DIR") else Path()
    )
    # Drop files here (SMB/NFS share, rsync, scp) and they get imported automatically.
    inbox_dir: Path = field(
        default_factory=lambda: Path(_env("WILDCAM_INBOX_DIR", "")) if _env("WILDCAM_INBOX_DIR") else Path()
    )

    # speciesnet | mock | none
    classifier: str = field(default_factory=lambda: _env("WILDCAM_CLASSIFIER", "speciesnet").lower())
    # Where models that are fetched on first use are kept.
    models_dir: Path = field(default_factory=lambda: Path(_env("WILDCAM_MODELS_DIR", "/models")))
    speciesnet_model: str = field(default_factory=lambda: _env("WILDCAM_SPECIESNET_MODEL", ""))
    country: str = field(default_factory=lambda: _env("WILDCAM_COUNTRY", "NLD"))
    # Below this score a detection is shown as "onzeker".
    min_score: float = field(default_factory=lambda: float(_env("WILDCAM_MIN_SCORE", "0.5")))
    # Frames sampled per video clip for classification.
    video_frames: int = field(default_factory=lambda: int(_env("WILDCAM_VIDEO_FRAMES", "6")))

    # Bearer token for POST /api/ingest. Empty disables uploads over HTTP.
    ingest_token: str = field(default_factory=lambda: _env("WILDCAM_INGEST_TOKEN"))

    # Home Assistant webhook, e.g. http://ha.local:8123/api/webhook/wildcam
    ha_webhook_url: str = field(default_factory=lambda: _env("WILDCAM_HA_WEBHOOK_URL"))
    # Always notify for these (Dutch or English names, comma separated).
    notify_species: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            s.strip().lower()
            for s in _env("WILDCAM_NOTIFY_SPECIES", "vos,das,steenmarter,boommarter,hermelijn,wezel").split(",")
            if s.strip()
        )
    )
    # Also notify the first time a species is ever seen.
    notify_new_species: bool = field(
        default_factory=lambda: _env("WILDCAM_NOTIFY_NEW_SPECIES", "true").lower() in ("1", "true", "yes")
    )
    # Public URL of the dashboard, used for links in notifications.
    base_url: str = field(default_factory=lambda: _env("WILDCAM_BASE_URL", "").rstrip("/"))

    inbox_poll_seconds: int = field(default_factory=lambda: int(_env("WILDCAM_INBOX_POLL_SECONDS", "30")))

    def __post_init__(self) -> None:
        if self.media_dir == Path():
            object.__setattr__(self, "media_dir", self.data_dir / "media")
        if self.inbox_dir == Path():
            object.__setattr__(self, "inbox_dir", self.data_dir / "inbox")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "wildcam.sqlite3"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.inbox_dir, self.thumbs_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
