"""File handling: storing originals, timestamps, thumbnails and video frames."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".3gp"}
THUMB_SIZE = (640, 640)


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).name)
    return keep[-120:] or "clip"


def store_original(src: Path, media_dir: Path, sha: str, original_name: str, captured_at: str) -> str:
    """Move/copy a file into media_dir/YYYY/MM/DD/. Returns the path relative to media_dir."""
    day = captured_at[:10].replace("-", "/")
    rel = Path(day) / f"{sha[:10]}_{safe_name(original_name)}"
    dest = media_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), dest)
    except OSError:
        shutil.copy2(src, dest)
    return rel.as_posix()


# ---- timestamps --------------------------------------------------------------------


def captured_at_of(path: Path, kind: str) -> str | None:
    """Best guess of when the camera recorded this file (ISO 8601, camera local time)."""
    try:
        if kind == "image":
            return _exif_time(path)
        if kind == "video":
            return _video_time(path)
    except Exception as exc:  # noqa: BLE001 - timestamps are best effort
        log.debug("No timestamp for %s: %s", path, exc)
    return None


def _exif_time(path: Path) -> str | None:
    with Image.open(path) as im:
        exif = im.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
    if not raw:
        return None
    return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").isoformat()


def _video_time(path: Path) -> str | None:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    tags = (json.loads(out.stdout or "{}").get("format") or {}).get("tags") or {}
    raw = tags.get("creation_time")
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def video_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        return float(json.loads(out.stdout or "{}")["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        return 0.0


# ---- thumbnails and frames -----------------------------------------------------------


def make_thumb(src_image: Path, dest: Path) -> None:
    with Image.open(src_image) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail(THUMB_SIZE)
        dest.parent.mkdir(parents=True, exist_ok=True)
        im.save(dest, "JPEG", quality=82)


def spread_times(video: Path, count: int) -> list[float]:
    """`count` moments spread evenly over the clip, in seconds."""
    duration = video_duration(video)
    if duration <= 0:
        return [0.0]
    step = duration / (count + 1)
    return [round(step * (i + 1), 2) for i in range(count)]


def extract_frames(video: Path, times: list[float], workdir: Path) -> list[tuple[float, Path]]:
    """One still per requested moment. Moments ffmpeg cannot deliver are left out."""
    frames: list[tuple[float, Path]] = []
    for i, t in enumerate(times):
        out = workdir / f"frame_{i:02d}.jpg"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if out.exists() and out.stat().st_size > 0:
            frames.append((t, out))
    return frames


FILM_FRAMES = 40
FILM_CELL = (160, 90)


def make_filmstrip(video: Path, dest: Path) -> bool:
    """One wide image with FILM_FRAMES evenly spaced frames side by side, for the time bar."""
    duration = video_duration(video)
    if duration <= 0:
        return False
    w, h = FILM_CELL
    cell = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", f"fps={FILM_FRAMES / duration:.6f},{cell},tile={FILM_FRAMES}x1",
         "-frames:v", "1", "-q:v", "5", str(dest)],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return dest.exists() and dest.stat().st_size > 0


def tempdir() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="wildcam-")


# ---- the date banner ---------------------------------------------------------------------
# Like the bar the camera burns in (which can be switched off): camera name left, date and
# time in the middle, sun or moon, weather and temperature right. What it shows is set on the
# settings page. Shown over the footage in the page, and burnt into a download copy on request.

BANNER_RATIO = 0.046  # bar height as part of the frame width
BANNER_PARTS = {      # setting key -> label on the settings page; all on by default
    "camera": "Naam van de camera",
    "date": "Datum",
    "time": "Tijd",
    "ticking": "Tijd loopt mee met de video",
    "moon": "Zon en maan",
    "weather": "Weertype",
    "temperature": "Temperatuur",
}


def banner_options(saved: dict | None) -> dict:
    return {key: bool((saved or {}).get(key, True)) for key in BANNER_PARTS}


def moon_illumination(when: datetime) -> tuple[float, bool]:
    """Lit fraction of the moon (0 new, 1 full) and whether it is waxing (Meeus, ch. 48)."""
    jd = when.timestamp() / 86400 + 2440587.5
    t = (jd - 2451545) / 36525
    rad = math.radians
    d = (297.8501921 + 445267.1114034 * t) % 360   # mean elongation of the moon
    m = (357.5291092 + 35999.0502909 * t) % 360    # sun's mean anomaly
    mm = (134.9633964 + 477198.8675055 * t) % 360  # moon's mean anomaly
    i = (180 - d - 6.289 * math.sin(rad(mm)) + 2.100 * math.sin(rad(m)) - 1.274 * math.sin(rad(2 * d - mm))
         - 0.658 * math.sin(rad(2 * d)) - 0.214 * math.sin(rad(2 * mm)) - 0.110 * math.sin(rad(d)))
    return (1 + math.cos(rad(i))) / 2, d < 180


def moon_points(when: datetime, n: int = 32) -> list[tuple[float, float]]:
    """Outline of the lit part of the moon on that moment, on a unit circle (y down)."""
    lit, waxing = moon_illumination(when)
    c = 1 - 2 * lit                   # terminator: an ellipse with x-radius |c|
    side = 1 if waxing else -1        # waxing: lit on the right (northern hemisphere)
    angles = [math.pi * (i / n - 0.5) for i in range(n + 1)]  # top to bottom
    outer = [(side * math.cos(a), math.sin(a)) for a in angles]
    terminator = [(side * c * math.cos(a), math.sin(a)) for a in reversed(angles)]
    return outer + terminator


def fmt_temperature(t: float | None) -> str:
    return "" if t is None else f"{round(t)} \u00b0C"  # plain characters: the bar's font has no others


# Icons as shapes in a box from -1 to 1 (y down), drawn by Pillow here and as SVG in the page.
#   ("disc", cx, cy, r)  ("ring", cx, cy, r)  ("line", x1, y1, x2, y2)  ("poly", [(x, y), ...])
def _cloud(dx=0.0, dy=0.0, k=1.0):
    return [("disc", dx + k * x, dy + k * y, k * r) for x, y, r in ((-0.42, 0.12, 0.36), (0.02, -0.12, 0.5), (0.48, 0.16, 0.34))] + [
        ("poly", [(dx + k * x, dy + k * y) for x, y in ((-0.42, 0.48), (0.48, 0.5), (0.48, 0.16), (-0.42, 0.12))])]


def _sun(dx=0.0, dy=0.0, k=1.0):
    rays = [("line", dx + k * 0.62 * math.cos(a), dy + k * 0.62 * math.sin(a), dx + k * 0.92 * math.cos(a), dy + k * 0.92 * math.sin(a))
            for a in (j * math.pi / 4 for j in range(8))]
    return [("disc", dx, dy, k * 0.42)] + rays


def icon_shapes(name: str, when: datetime | None = None) -> list[tuple]:
    if name == "sun":
        return _sun()
    if name == "moon":
        return [("ring", 0, 0, 1), ("poly", moon_points(when))]
    if name == "partly":
        return _sun(-0.38, -0.32, 0.62) + _cloud(0.12, 0.22, 0.78)
    if name == "cloudy":
        return _cloud(0, 0.05)
    if name == "fog":
        return [("line", -0.8, y, 0.8, y) for y in (-0.4, 0.0, 0.4)]
    base = _cloud(0, -0.28, 0.9)
    if name == "drizzle":
        return base + [("line", x, 0.45, x - 0.06, 0.6) for x in (-0.4, 0.05, 0.5)]
    if name in ("rain", "showers"):
        return base + [("line", x, 0.4, x - 0.18, 0.88) for x in (-0.4, 0.05, 0.5)]
    if name == "snow":
        return base + [("disc", x, y, 0.09) for x, y in ((-0.4, 0.62), (0.05, 0.8), (0.5, 0.62))]
    if name == "thunder":
        return base + [("poly", [(0.1, 0.25), (-0.22, 0.65), (0.0, 0.65), (-0.12, 0.98), (0.3, 0.5), (0.06, 0.5), (0.22, 0.25)])]
    return []


def icon_svg(shapes: list[tuple]) -> str:
    """The shapes as SVG elements, to put inside <svg viewBox="-1.1 -1.1 2.2 2.2">."""
    out = []
    for s in shapes:
        if s[0] == "disc":
            out.append(f'<circle cx="{s[1]:.3f}" cy="{s[2]:.3f}" r="{s[3]:.3f}"/>')
        elif s[0] == "ring":
            out.append(f'<circle class="ring" cx="{s[1]:.3f}" cy="{s[2]:.3f}" r="{s[3]:.3f}"/>')
        elif s[0] == "line":
            out.append(f'<line x1="{s[1]:.3f}" y1="{s[2]:.3f}" x2="{s[3]:.3f}" y2="{s[4]:.3f}"/>')
        else:
            out.append('<polygon points="' + " ".join(f"{x:.3f},{y:.3f}" for x, y in s[1]) + '"/>')
    return "".join(out)


def _draw_icon(draw: ImageDraw.ImageDraw, shapes: list[tuple], cx: float, cy: float, r: float, fill) -> None:
    w = max(1, round(r * 0.16))
    for s in shapes:
        if s[0] in ("disc", "ring"):
            x, y, rr = cx + s[1] * r, cy + s[2] * r, s[3] * r
            if s[0] == "disc":
                draw.ellipse((x - rr, y - rr, x + rr, y + rr), fill=fill)
            else:
                draw.ellipse((x - rr, y - rr, x + rr, y + rr), outline=fill, width=w)
        elif s[0] == "line":
            draw.line((cx + s[1] * r, cy + s[2] * r, cx + s[3] * r, cy + s[4] * r), fill=fill, width=w)
        else:
            draw.polygon([(cx + x * r, cy + y * r) for x, y in s[1]], fill=fill)


def banner_right(info: dict, opts: dict) -> list[tuple[str, object]]:
    """The right part as ('icon', shapes) and ('text', str) items, for both renderers."""
    items = []
    when = info["when"]
    if opts["moon"]:
        night = info.get("is_day") == 0 or (info.get("is_day") is None)
        items.append(("icon", icon_shapes("moon" if night else "sun", when)))
    weather = info.get("weather")
    if opts["weather"] and weather:
        label, icon = weather
        if icon not in ("clear",):
            items.append(("icon", icon_shapes(icon)))
        items.append(("text", label))
    if opts["temperature"] and info.get("temperature") is not None:
        items.append(("text", fmt_temperature(info["temperature"])))
    return items


def banner_image(width: int, info: dict, opts: dict) -> Image.Image:
    """The bar as a transparent PNG layer, as wide as the frame."""
    h = max(18, round(width * BANNER_RATIO))
    img = Image.new("RGBA", (width, h), (0, 0, 0, 148))  # same darkness as the bar in the page
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=round(h * 0.44))
    white = (255, 255, 255, 240)
    pad, mid, when = round(h * 0.45), h / 2, info["when"]
    if opts["camera"] and info.get("camera"):
        draw.text((pad, mid), info["camera"], font=font, fill=white, anchor="lm")
    center = "  ".join(p for p, on in ((when.strftime("%d-%m-%Y"), opts["date"]), (when.strftime("%H:%M:%S"), opts["time"])) if on)
    if center:
        draw.text((width / 2, mid), center, font=font, fill=white, anchor="mm")
    r, gap = h * 0.24, h * 0.3
    x = width - pad
    for kind, value in reversed(banner_right(info, opts)):
        if kind == "text":
            draw.text((x, mid), value, font=font, fill=white, anchor="rm")
            x -= draw.textlength(value, font=font) + gap
        else:
            _draw_icon(draw, value, x - r, mid, r, white)
            x -= 2 * r + gap
    return img


def with_banner(src: Path, dest: Path, kind: str, info: dict, opts: dict) -> None:
    """A copy of a photo or video with the bar burnt in at the bottom."""
    if kind == "image":
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            bar = banner_image(im.width, info, opts)
            im.paste(bar, (0, im.height - bar.height), bar)
            im.save(dest, "JPEG", quality=92)
        return
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width",
                            "-of", "csv=p=0", str(src)], capture_output=True, text=True, check=True)
    width = int(probe.stdout.strip().split(",")[0])
    # A ticking clock is one bar per second, laid over the video as a 1 fps picture stream.
    seconds = math.ceil(video_duration(src)) + 1 if opts["ticking"] and opts["time"] else 1
    with tempfile.TemporaryDirectory() as tmp:
        when = info["when"]
        for i in range(seconds):
            frame = dict(info, when=datetime.fromtimestamp(when.timestamp() + i, when.tzinfo))
            banner_image(width, frame, opts).save(Path(tmp) / f"bar{i:05d}.png")
        bars = ["-framerate", "1", "-i", str(Path(tmp) / "bar%05d.png")] if seconds > 1 else ["-i", str(Path(tmp) / "bar00000.png")]
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), *bars,
                        "-filter_complex", "[0:v][1:v]overlay=0:H-h:eof_action=repeat", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(dest)],
                       check=True, timeout=600)
