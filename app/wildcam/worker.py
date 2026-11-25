"""Ingest and background processing.

Files enter in two ways:
  * POST /api/ingest (used by the fetcher), or
  * dropped into the inbox folder (handy for an SD-card dump or an SMB share).

A single background thread imports the inbox and classifies pending clips one by
one, so a small CPU node is enough.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import media, notify
from .classifier import NO_RESULT, Classifier, Result
from .config import Settings
from .db import Database, now_iso
from .names_nl import to_dutch

log = logging.getLogger(__name__)
RETRY_SECONDS = 3600
WEATHER_SECONDS = 600  # look up the weather for new clips
BRIDGE_STATUS_SECONDS = 300  # how often the camera block on the dashboard is refreshed from the bridge
BRIDGE_WAIT_SECONDS = 15  # status reads while a camera session runs
BRIDGE_GIVE_UP_SECONDS = 20 * 60  # collect whatever is there if a session never reports back  # how often stills a model failed on (e.g. a usage limit) are tried again


def ingest_file(
    db: Database,
    settings: Settings,
    path: Path,
    original_name: str | None = None,
    source: str = "upload",
    captured_at: str | None = None,
    temperature: float | None = None,
) -> tuple[int | None, str]:
    """Register a file. Moves it into media storage. Returns (clip_id, status)."""
    original_name = original_name or path.name
    kind = media.kind_of(Path(original_name)) or media.kind_of(path)
    if kind is None:
        path.unlink(missing_ok=True)
        return None, "unsupported"
    sha = media.sha256_of(path)
    if db.exists_sha(sha):
        path.unlink(missing_ok=True)
        return None, "duplicate"
    captured = captured_at or media.captured_at_of(path, kind) or now_iso()[:19]
    rel = media.store_original(path, settings.media_dir, sha, original_name, captured)
    clip_id = db.insert_clip(
        sha256=sha,
        original_name=original_name,
        media_path=rel,
        kind=kind,
        captured_at=captured,
        source=source,
        temperature=temperature,
    )
    return clip_id, "queued" if clip_id else "duplicate"


def import_inbox(db: Database, settings: Settings) -> int:
    count = 0
    for path in sorted(settings.inbox_dir.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        # Skip files that are still being written.
        if time.time() - path.stat().st_mtime < 5:
            continue
        clip_id, status = ingest_file(db, settings, path, source="inbox")
        if clip_id:
            count += 1
        log.info("inbox %s: %s", path.name, status)
    return count


def still_name(clip_id: int, t: float) -> str:
    return f"{clip_id:06d}_s{round(t * 100):06d}.jpg"


def remove_stills(settings: Settings, clip_id: int, keep: set[str] = frozenset()) -> None:
    for old in settings.thumbs_dir.glob(f"{clip_id:06d}_s*.jpg"):
        if old.name not in keep:
            old.unlink(missing_ok=True)


DEFAULT_MODELS = {"speciesnet": 1.0}


def voters(db: Database, models: dict[str, Classifier]) -> dict[str, tuple[Classifier, float]]:
    """The models that have a say, with the weight of their vote."""
    chosen = db.setting("models", DEFAULT_MODELS)
    return {name: (models[name], float(weight)) for name, weight in chosen.items() if name in models and weight > 0}


def _reading(result: Result) -> dict:
    return {"name": result.common_name, "score": result.score, "taxon": result.taxon,
            "blank": result.blank, "detections": result.detections}


def read_stills(settings: Settings, voting: dict[str, tuple[Classifier, float]], clip: dict, tmpdir: Path) -> list[dict]:
    """Every still of a video with what each voting model sees on it, in time order.

    A still a model has read before is not read again, so adding one still, or
    switching on a second model, only costs the readings that are missing.
    """
    src = settings.media_dir / clip["media_path"]
    times = clip["frame_times"] or media.spread_times(src, settings.video_frames)
    known = {v["t"]: v for v in clip["votes"] if (settings.thumbs_dir / v["thumb"]).exists()}
    lacking = [t for t in times if t not in known or any(name not in known[t]["by"] for name in voting)]
    frames = dict(media.extract_frames(src, lacking, tmpdir))
    for t, frame in frames.items():
        if t not in known:
            known[t] = {"t": t, "thumb": still_name(clip["id"], t), "by": {}}
            media.make_thumb(frame, settings.thumbs_dir / known[t]["thumb"])
    failed: list[str] = []
    for name, (model, _) in voting.items():
        todo = [t for t in frames if name not in known[t]["by"]]
        for t, result in zip(todo, model.classify([frames[t] for t in todo]) if todo else []):
            if result.error and not result.common_name:
                # Not kept as a reading, so the model reads this still again on the next pass;
                # the reason is kept to show on the clip page.
                failed.append(result.error)
                known[t].setdefault("errors", {})[name] = result.error
                continue
            known[t]["by"][name] = _reading(result)
            known[t].get("errors", {}).pop(name, None)
    if failed:
        log.warning("Clip %s: %d lezingen mislukt: %s", clip["id"], len(failed), failed[0])
    return [known[t] for t in sorted(times) if t in known]


def process_clip(db: Database, settings: Settings, models: dict[str, Classifier], clip: dict) -> None:
    src = settings.media_dir / clip["media_path"]
    thumb_name = f"{clip['id']:06d}.jpg"
    voting = voters(db, models)
    votes: list[dict] = []
    with media.tempdir() as tmp:
        if clip["kind"] == "video":
            votes = read_stills(settings, voting, clip, Path(tmp))
            if not votes:
                raise RuntimeError("kon geen stills uit de video halen")
            results = [
                Result(filepath=str(settings.thumbs_dir / v["thumb"]), taxon=r["taxon"], common_name=r["name"],
                       score=r["score"], blank=r["blank"], detections=r["detections"], weight=voting[name][1])
                for v in votes for name, r in v["by"].items() if name in voting
            ]
            middle = settings.thumbs_dir / votes[len(votes) // 2]["thumb"]
        else:
            results = []
            for model, weight in voting.values():
                for result in model.classify([src]):
                    result.weight = weight
                    results.append(result)
            middle = src

        verdict = combine(results, settings.min_score)
        shown = Path(verdict.filepath) if verdict is not None and not verdict.blank else middle
        media.make_thumb(shown if clip["kind"] == "video" else src, settings.thumbs_dir / thumb_name)
        best_frame = None
        if clip["kind"] == "video" and verdict is not None and not verdict.blank:
            best_frame = f"{clip['id']:06d}_best.jpg"
            shutil.copy(verdict.filepath, settings.thumbs_dir / best_frame)

    current = db.get(clip["id"])
    if current is None or current["frame_times"] != clip["frame_times"]:
        # The stills were changed while we were looking. Leave the clip pending for the next pass.
        return
    remove_stills(settings, clip["id"], keep={v["thumb"] for v in votes})

    if verdict is None:
        db.update_clip(clip["id"], status="done", thumb=thumb_name, votes_json=votes)
        return
    if verdict.error and not verdict.common_name:
        raise RuntimeError(verdict.error)

    db.update_clip(
        clip["id"],
        status="done",
        error=None,
        thumb=thumb_name,
        best_frame=best_frame,
        taxon=verdict.taxon,
        common_name=verdict.common_name,
        name_nl=to_dutch(verdict.common_name),
        score=verdict.score,
        is_blank=1 if verdict.blank else 0,
        detections_json=verdict.detections,
        top_json=verdict.top,
        votes_json=votes,
    )


def _lineage(taxon: str | None) -> tuple[str, ...]:
    """('mammalia', 'carnivora', 'mustelidae') for a family-level label; () when unknown."""
    ranks = (taxon or "").split(";")[1:6]
    return tuple(r for r in ranks if r)


def _covers(candidate: Result, frame: Result) -> bool:
    """True when the frame's verdict is the candidate itself or a taxon below it."""
    if candidate.common_name == frame.common_name:
        return True
    line = _lineage(candidate.taxon)
    if not line:
        return candidate.common_name == "animal"
    return _lineage(frame.taxon)[: len(line)] == line


def combine(results: list[Result], min_score: float) -> Result | None:
    """One verdict for a set of readings: every still, by every voting model.

    Every reading with an animal votes for its own label with its score, times the
    weight of its model. A label's support is the weighted score of the readings at
    or below it in the taxonomy, divided by the weight of all animal readings: three
    saying "pine marten" and one saying "beech marten" all support "weasel family",
    and so does a coarser model that only knows "weasel family". The verdict is the
    most specific label with at least `min_score` support, or the best supported one
    when none gets there. Readings without an animal or without any result do not vote.
    """
    usable = [r for r in results if not r.error or r.common_name]
    if not usable:
        return results[0] if results else None
    animals = [r for r in usable if not r.blank and r.common_name and r.common_name != NO_RESULT and r.weight > 0]
    if not animals:
        return usable[0]

    candidates: dict[str, Result] = {}
    for r in animals:
        candidates.setdefault(r.common_name, r)
    total = sum(r.weight for r in animals)
    support = {
        name: sum(r.score * r.weight for r in animals if _covers(c, r)) / total for name, c in candidates.items()
    }
    agreed = [n for n in candidates if support[n] >= min_score] or list(candidates)
    winner = max(agreed, key=lambda n: (len(_lineage(candidates[n].taxon)) if support[n] >= min_score else 0, support[n]))

    # Show the still that argues best for the verdict.
    frame = max((r for r in animals if _covers(candidates[winner], r)), key=lambda r: r.score * r.weight)
    return Result(
        filepath=frame.filepath,
        taxon=candidates[winner].taxon,
        common_name=winner,
        score=round(support[winner], 3),
        detections=frame.detections,
        top=sorted(((n, round(s, 3)) for n, s in support.items()), key=lambda x: -x[1]),
    )


def maybe_notify(db: Database, settings: Settings, clip_id: int) -> None:
    clip = db.get(clip_id)
    if not clip or clip["notified"] or clip["status"] != "done":
        return
    first_time = not db.label_seen_before(clip["label"], clip_id) if clip["label"] else False
    if notify.should_notify(settings, clip["label"], clip["common_name"], first_time):
        if notify.send(settings, clip, first_time):
            db.update_clip(clip_id, notified=1)


class Worker(threading.Thread):
    def __init__(self, db: Database, settings: Settings, models: dict[str, Classifier]) -> None:
        super().__init__(name="wildcam-worker", daemon=True)
        self.db = db
        self.settings = settings
        self.models = models
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self._last_inbox = 0.0
        self._last_retry = 0.0
        self._last_weather = 0.0
        # Per camera id: last hour it was woken, when we started waiting for its session, last status read.
        self._started_hour = datetime.now().strftime("%Y-%m-%d %H")  # a restart does not wake the cameras
        self._cam_hour: dict[str, str] = {}
        self._cam_since: dict[str, float] = {}
        self._cam_status: dict[str, float] = {}

    def run(self) -> None:
        log.info("Worker started (models: %s)", ", ".join(voters(self.db, self.models)) or "none")
        while not self.stopping.is_set():
            try:
                if time.time() - self._last_inbox > self.settings.inbox_poll_seconds:
                    self._last_inbox = time.time()
                    import_inbox(self.db, self.settings)
                if time.time() - self._last_retry > RETRY_SECONDS:
                    self._last_retry = time.time()
                    if n := self.db.requeue_failed_readings():
                        log.info("Retrying %s clips with failed readings", n)
                if time.time() - self._last_weather > WEATHER_SECONDS:
                    self._last_weather = time.time()
                    from . import weather
                    if n := weather.fill(self.db):
                        log.info("Weather added to %s clips", n)
                self.tend_cameras()
                if not self.run_once():
                    self.wake.wait(timeout=5)
                    self.wake.clear()
            except Exception:  # noqa: BLE001 - keep the worker alive
                log.exception("Worker loop error")
                time.sleep(5)

    def tend_cameras(self) -> None:
        """Wake each camera at its check hours, and collect the clips once its bridge is done."""
        from . import bridge  # bridge uses ingest_file from this module

        now = time.time()
        hour = datetime.now().strftime("%Y-%m-%d %H")
        for cam in bridge.cameras(self.db):
            cid = cam["id"]
            try:
                if datetime.now().hour in cam.get("hours", []) and self._cam_hour.get(cid, self._started_hour) != hour:
                    self._cam_hour[cid] = hour
                    self.start_camera_session(cam)
                since = self._cam_since.get(cid)
                every = BRIDGE_WAIT_SECONDS if since is not None else BRIDGE_STATUS_SECONDS
                if now - self._cam_status.get(cid, 0.0) > every:
                    self._cam_status[cid] = now
                    bridge.refresh_status(cam)
                    if since is not None and (bridge.session_done(cid, since) or now - since > BRIDGE_GIVE_UP_SECONDS):
                        del self._cam_since[cid]
                        bridge.pull(self.db, self.settings, cam)
                        bridge.refresh_status(cam)
            except OSError as exc:  # Pi rebooting or unreachable: try again on the next round
                log.warning("Camera %s not reachable: %s", cam["name"], exc)

    def start_camera_session(self, cam: dict) -> bool:
        """Have the camera checked now. False if its bridge is busy or unreachable."""
        from . import bridge

        try:
            started = bridge.start_session(cam)
        except OSError as exc:
            log.warning("Camera %s not reachable: %s", cam["name"], exc)
            return False
        # Also when a session was already running: collect its clips when it ends.
        self._cam_since[cam["id"]] = time.time() - (0 if started else BRIDGE_GIVE_UP_SECONDS / 2)
        self._cam_status[cam["id"]] = 0.0
        self.wake.set()
        return started

    def run_once(self) -> bool:
        clip = self.db.next_pending()
        if clip is None:
            return False
        try:
            process_clip(self.db, self.settings, self.models, clip)
            maybe_notify(self.db, self.settings, clip["id"])
            log.info("Processed clip %s", clip["id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("Clip %s failed", clip["id"])
            self.db.update_clip(clip["id"], status="failed", error=str(exc)[:500])
        return True

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()
