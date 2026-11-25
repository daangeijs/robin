"""The dashboard: clips from the trail camera with species identification."""

from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.requests import HTTPConnection
from fastapi.templating import Jinja2Templates

from . import media
from .classifier import CATALOGUE, build_models
from .config import APP_NAME, APP_TAGLINE, settings
from .db import REVIEWS, STATES, Database
from .names_nl import NL_NAMES, to_dutch
from . import bridge, i18n, weather
from .i18n import _, ngettext
from .worker import Worker, ingest_file, remove_stills, voters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wildcam")

HERE = Path(__file__).parent
PAGE_SIZE = 48
MONTHS = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]  # translated when shown
MAX_STILLS = 48
DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]

settings.ensure_dirs()
db = Database(settings.db_path)
db.retranslate(to_dutch)
models = build_models(
    settings.classifier,
    model_name=settings.speciesnet_model,
    country=settings.country,
    cache_dir=settings.models_dir,
    claude_key=lambda: db.setting("anthropic_key", ""),
)
worker = Worker(db, settings, models)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker.start()
    yield
    worker.stop()


app = FastAPI(title=APP_NAME, lifespan=lifespan)


class LanguageMiddleware:
    """Picks the language of each request (cookie, then Accept-Language) for everything it renders."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        conn = HTTPConnection(scope)
        token = i18n.set_lang(i18n.choose(conn.cookies.get(i18n.COOKIE), conn.headers.get("accept-language")))
        try:
            await self.inner(scope, receive, send)
        finally:
            i18n.reset_lang(token)


app.add_middleware(LanguageMiddleware)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
app.mount("/thumbs", StaticFiles(directory=settings.thumbs_dir), name="thumbs")
templates = Jinja2Templates(directory=HERE / "templates")


# ---- template helpers --------------------------------------------------------------


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fmt_time(value: str | None) -> str:
    d = _dt(value)
    return d.strftime("%H:%M") if d else ""


def fmt_day(value: str | None, inline: bool = False) -> str:
    """'Vandaag', 'Gisteren' or 'maandag 3 nov 2026'. `inline` is for the middle of a sentence."""
    d = _dt(value)
    today = datetime.now().date()
    if not d:
        word = _("Onbekende datum")
    elif d.date() == today:
        word = _("Vandaag")
    elif (today - d.date()).days == 1:
        word = _("Gisteren")
    else:
        return f"{_(DAYS[d.weekday()])} {d.day} {_(MONTHS[d.month - 1])} {d.year}"
    return word.lower() if inline else word


def fmt_pct(value: float | None) -> str:
    return f"{round((value or 0) * 100)}%"


def fmt_clock(seconds: float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 60}:{s % 60:02d}"


def fmt_scientific(taxon: str | None) -> str:
    """'Vulpes vulpes' for a species, otherwise the lowest rank the label reaches."""
    ranks = (taxon or "").split(";")[1:6]
    if len(ranks) == 5 and ranks[3] and ranks[4]:
        return f"{ranks[3].capitalize()} {ranks[4]}"
    known = [r for r in ranks if r]
    return known[-1].capitalize() if known else ""


def cap(v: str | None) -> str | None:
    return v[:1].upper() + v[1:] if v else v  # species names start with a capital


# ---- species names in the user's language ---------------------------------------------
# A clip's label (what filters and links use) is Dutch: the model's name_nl, 'leeg', or what the
# user typed. In English the model's own English common name is shown instead; labels typed by
# the user that match a Dutch name we know are shown in English too, anything else as typed.

RAW_EN = {"blank": "empty", "no cv result": "no result"}


def _fallback_english() -> dict[str, str]:
    """Dutch name -> English, from the name table. Prefers a plain name over 'x species/family'."""
    out: dict[str, str] = {}
    for english, dutch in NL_NAMES.items():
        coarse = any(w in english for w in (" species", " family", " order", "idae", "/"))
        if dutch not in out or (out[dutch][1] and not coarse):
            out[dutch] = (english, coarse)
    return {dutch: RAW_EN.get(english, english) for dutch, (english, _coarse) in out.items()}


FALLBACK_EN = _fallback_english()
_names_cache: dict = {"at": -1e9, "names": FALLBACK_EN}


def english_names() -> dict[str, str]:
    """Dutch label -> English name: the common name the models gave it most here, else the name table."""
    if time.monotonic() - _names_cache["at"] > 30:
        _names_cache["names"] = {**FALLBACK_EN, **{d: RAW_EN.get(e, e) for d, e in db.english_names().items()}}
        _names_cache["at"] = time.monotonic()
    return _names_cache["names"]


def species(label: str | None) -> str | None:
    """A stored (Dutch) label as shown to the user."""
    if not label or i18n.get_lang() != "en":
        return cap(label)
    return cap(english_names().get(label) or label)


def clip_name(clip: dict) -> str:
    """The name shown for a clip; in English the model's own common name where it has one."""
    if i18n.get_lang() != "en" or clip.get("manual_label"):
        return species(clip.get("label")) or ""
    if clip.get("is_blank"):
        return cap(_("leeg"))
    if clip.get("common_name"):
        return cap(RAW_EN.get(clip["common_name"], clip["common_name"]))
    return species(clip.get("label")) or ""


def animal(name: str | None) -> str | None:
    """A model's raw (English) name, e.g. on a still or a detection box."""
    if not name or i18n.get_lang() != "en":
        return to_dutch(name) or name
    return RAW_EN.get(name.strip().lower(), name)


def label_from_english(text: str) -> str:
    """What an English name typed on the clip page is stored as: the Dutch label, when we know it."""
    low = text.lower()
    if low in ("empty", "blank"):
        return "leeg"
    for dutch, english in english_names().items():
        if english.lower() == low:
            return dutch
    return NL_NAMES.get(low, text)


templates.env.filters.update(time=fmt_time, day=fmt_day, day_inline=lambda v: fmt_day(v, inline=True), pct=fmt_pct,
                            clock=fmt_clock, sci=fmt_scientific, temp=media.fmt_temperature, animal=animal, cap=cap,
                            species=species, msg=i18n.translate_known)
templates.env.globals.update(min_score=settings.min_score, catalogue=CATALOGUE, app_name=APP_NAME, app_tagline=APP_TAGLINE, _=_, ngettext=ngettext,
                             lang=i18n.get_lang, clip_name=clip_name, still_button=i18n.STILL_BUTTON)


def model_problems() -> list[tuple[str, str]]:
    """Models that vote but failed on their last attempt, with why. Shown on every page."""
    voting = voters(db, models)
    return [(CATALOGUE[name]["label"], i18n.translate_known(m.last_error)) for name, m in models.items() if name in voting and getattr(m, "last_error", None)]


templates.env.globals["model_problems"] = model_problems
templates.env.globals["camera_views"] = lambda: [bridge.camera_view(c) for c in bridge.cameras(db)]
templates.env.globals["camera_summary"] = bridge.summary
templates.env.globals["banner"] = lambda: media.banner_options(db.setting("banner", None))


def camera_name(clip: dict) -> str:
    """The camera a clip came from, for the date bar; empty for uploads and other sources."""
    names = {cam["name"] for cam in bridge.cameras(db)}
    return clip.get("source") if clip.get("source") in names else ""


templates.env.globals["camera_name"] = camera_name


def banner_info(clip: dict) -> dict:
    """What the date bar knows about a clip. Air temperature of that hour wins over the camera's."""
    return {
        "camera": camera_name(clip),
        "when": _dt(clip["captured_at"]),
        "temperature": clip.get("air_temp") if clip.get("air_temp") is not None else clip.get("temperature"),
        "is_day": clip.get("is_day"),
        "weather": _weather(clip.get("weather_code")),
    }


def _weather(code: int | None) -> tuple[str, str] | None:
    found = weather.describe(code)
    return (_(found[0]), found[1]) if found else None


def banner_right(clip: dict) -> list[tuple[str, str]]:
    """The right part of the bar for the page: ('icon', svg elements) and ('text', str)."""
    info = banner_info(clip)
    if not info["when"]:
        return []
    opts = media.banner_options(db.setting("banner", None))
    return [(kind, media.icon_svg(v) if kind == "icon" else v) for kind, v in media.banner_right(info, opts)]


templates.env.globals["banner_right"] = banner_right


def _check_token(authorization: str | None) -> None:
    if not settings.ingest_token:
        raise HTTPException(403, _("Uploaden staat uit: zet WILDCAM_INGEST_TOKEN"))
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, settings.ingest_token):
        raise HTTPException(401, _("Ongeldig token"))


# ---- pages -------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, soort: str | None = None, leeg: bool = False, review: str | None = None,
          status: str | None = None, pagina: int = 1):
    """The timeline. Two independent filters: what is on the clip (an animal, or 'leeg' for
    triggers without one) and how far I am with it (open or confirmed)."""
    pagina = max(1, pagina)
    if leeg and not soort:
        soort = "leeg"  # old links: ?leeg=1
    if review == "bevestigd" and not status:
        status = "bevestigd"  # old links: ?review=bevestigd
    if review not in REVIEWS:
        review = None
    if status not in STATES:
        status = None
    clips = db.list_clips(label=soort, review=review, state=status, limit=PAGE_SIZE, offset=(pagina - 1) * PAGE_SIZE)
    total = db.count_clips(label=soort, review=review, state=status)
    counts = db.label_counts(include_blank=True, state=status)
    animals = [(label, n) for label, n in counts if label != "leeg"]
    # how many clips each review state would show, for what is picked in the first filter
    state_counts = {s: db.count_clips(label=soort, state=s) for s in STATES}
    groups: list[tuple[str, list[dict]]] = []
    for clip in clips:
        day = fmt_day(clip["captured_at"])
        if not groups or groups[-1][0] != day:
            groups.append((day, []))
        groups[-1][1].append(clip)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "groups": groups,
            "labels": animals,
            "animals_total": sum(n for _l, n in animals),
            "empty_total": dict(counts).get("leeg", 0),
            "soort": soort,
            "review": review,
            "status_filter": status,
            "state_counts": state_counts,
            "all_count": db.count_clips(label=soort),
            "pagina": pagina,
            "has_next": pagina * PAGE_SIZE < total,
            "total": total,
            "status": db.status_counts(),
        },
    )


@app.get("/clip/{clip_id}", response_class=HTMLResponse)
def clip_detail(request: Request, clip_id: int):
    clip = db.get(clip_id)
    if not clip:
        raise HTTPException(404, _("Clip niet gevonden"))
    choices = sorted({*NL_NAMES.values(), *(label for label, _n in db.label_counts(include_blank=True))}, key=str.lower)
    if i18n.get_lang() == "en":
        choices = sorted({species(c) for c in choices}, key=str.lower)
    return templates.TemplateResponse(
        request,
        "clip.html",
        {"clip": clip, "choices": choices, "max_stills": MAX_STILLS, "film_frames": media.FILM_FRAMES, "voting": list(voters(db, models))},
    )


@app.get("/clip/{clip_id}/film.jpg")
def filmstrip(clip_id: int):
    """Frames of the whole clip side by side; made on first use."""
    clip = db.get(clip_id)
    if not clip or clip["kind"] != "video":
        raise HTTPException(404)
    path = settings.thumbs_dir / f"{clip_id:06d}_film.jpg"
    if not path.exists() and not media.make_filmstrip(settings.media_dir / clip["media_path"], path):
        raise HTTPException(404, _("Geen filmstrook voor deze clip"))
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/clip/{clip_id}/download")
def download(clip_id: int, balk: int = 0):
    """The original file, or a copy with the date banner burnt in (made now, removed after sending)."""
    clip = db.get(clip_id)
    if not clip:
        raise HTTPException(404)
    src = settings.media_dir / clip["media_path"]
    if not src.exists():
        raise HTTPException(404, _("Het bestand is er niet meer"))
    when = _dt(clip["captured_at"])
    base = media.safe_name(f"{clip_name(clip).lower() or 'clip'}_{when.strftime('%Y-%m-%d_%H%M') if when else clip_id}")
    if not balk or not when:
        return FileResponse(src, filename=base + src.suffix.lower())
    suffix = ".mp4" if clip["kind"] == "video" else ".jpg"
    tmp = Path(tempfile.mkstemp(dir=settings.data_dir, suffix=suffix)[1])
    try:
        media.with_banner(src, tmp, clip["kind"], banner_info(clip), media.banner_options(db.setting("banner", None)))
    except (OSError, subprocess.SubprocessError) as exc:
        tmp.unlink(missing_ok=True)
        log.warning("Banner for clip %s failed: %s", clip_id, exc)
        raise HTTPException(500, _("De balk kon niet in de video gezet worden"))
    return FileResponse(tmp, filename=f"{base}_{_('balk')}{suffix}", background=BackgroundTask(tmp.unlink, missing_ok=True))


@app.post("/clip/{clip_id}/label")
def set_label(clip_id: int, label: str = Form("")):
    if not db.get(clip_id):
        raise HTTPException(404)
    label = label.strip()
    if label and i18n.get_lang() == "en":
        label = label_from_english(label)  # stored as the Dutch label, so filters stay one list
    # Naming the animal yourself is a confirmation; going back to the model withdraws it.
    db.update_clip(clip_id, manual_label=label or None, review="bevestigd" if label else None)
    return RedirectResponse(f"/clip/{clip_id}", status_code=303)


@app.post("/clip/{clip_id}/review")
def set_review(clip_id: int, review: str = Form("")):
    if not db.get(clip_id):
        raise HTTPException(404)
    if review and review not in REVIEWS:
        raise HTTPException(400, _("Onbekende beoordeling"))
    db.update_clip(clip_id, review=review or None)
    return RedirectResponse(f"/clip/{clip_id}", status_code=303)


@app.post("/clip/{clip_id}/opnieuw")
def reprocess(clip_id: int, times: str = Form(""), mislukt: str = Form("")):
    """Classify again. `times` (seconds, comma separated) replaces the stills to look at;
    stills the model has read before are not read again. `mislukt` only retries the readings
    that failed. Without either everything is read afresh."""
    clip = db.get(clip_id)
    if not clip:
        raise HTTPException(404)
    fields: dict = {"status": "pending", "error": None}
    if mislukt:
        pass  # failed readings were never kept, so judging again asks only for those
    elif times.strip():
        try:
            chosen = sorted({round(max(0.0, float(t)), 2) for t in times.split(",") if t.strip()})
        except ValueError:
            raise HTTPException(400, _("Ongeldige tijden")) from None
        if len(chosen) > MAX_STILLS:
            raise HTTPException(400, _("Kies hooguit {n} stills", n=MAX_STILLS))
        fields["frame_times_json"] = chosen
    else:
        fields["votes_json"] = []
    if not clip["manual_label"]:
        # A new verdict from the model has not been looked at yet.
        fields["review"] = None
    db.update_clip(clip_id, **fields)
    worker.wake.set()
    return RedirectResponse(f"/clip/{clip_id}", status_code=303)


@app.post("/clip/{clip_id}/verwijder")
def delete(clip_id: int):
    clip = db.get(clip_id)
    if not clip:
        raise HTTPException(404)
    (settings.media_dir / clip["media_path"]).unlink(missing_ok=True)
    for name in (clip.get("thumb"), clip.get("best_frame")):
        if name:
            (settings.thumbs_dir / name).unlink(missing_ok=True)
    remove_stills(settings, clip_id)
    (settings.thumbs_dir / f"{clip_id:06d}_film.jpg").unlink(missing_ok=True)
    db.delete_clip(clip_id)
    return RedirectResponse("/?verwijderd=1", status_code=303)


@app.get("/statistiek", response_class=HTMLResponse)
def stats(request: Request, soort: str | None = None):
    hours = db.hourly_activity(soort)
    daily = db.daily_counts(30)
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "labels": db.label_counts(),
            "soort": soort,
            "hours": hours,
            "hours_max": max(hours) or 1,
            "daily": daily,
            "daily_max": max((n for _, n in daily), default=0) or 1,
            "status": db.status_counts(),
        },
    )


def _settings_page(
    request: Request,
    *,
    saved: bool = False,
    error: str | None = None,
    redone: int | None = None,
    camera_error: str | None = None,
    camera_form: dict | None = None,
    camera_saved: str | None = None,
    banner_saved: bool = False,
    banner_error: str | None = None,
    camera_edit: str | None = None,
    camera_changed: str | None = None,
    status_code: int = 200,
):
    voting = voters(db, models)
    key = db.setting("anthropic_key", "")
    rows = [
        {"name": name, **CATALOGUE[name], "on": name in voting, "weight": round(voting[name][1] * 100) if name in voting else 100}
        for name in models
    ]
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "models": rows,
            "saved": saved,
            "banner_parts": media.BANNER_PARTS,
            "banner_saved": banner_saved,
            "banner_error": banner_error,
            "camera_edit": camera_edit,
            "camera_changed": camera_changed,
            "location": weather.location(db),
            "error": error,
            "status": db.status_counts(),
            # Of a saved key only the start and the tail are shown, enough to recognise it.
            "key_hint": f"{key[:10]}…{key[-4:]}" if key else "",
            "failed_clips": db.count_failed_readings(),
            "redone": redone,
            "cameras": [bridge.camera_view(c) for c in bridge.cameras(db)],
            "camera_error": camera_error,
            "camera_form": camera_form or {"hours": ",".join(map(str, bridge.DEFAULT_HOURS))},
            "camera_saved": camera_saved,
        },
        status_code=status_code,
    )


@app.get("/instellingen", response_class=HTMLResponse)
def settings_page(request: Request, bewaard: bool = False, opnieuw: int | None = None, camera: str | None = None,
                  balk: bool = False, gewijzigd: str | None = None):
    return _settings_page(request, saved=bewaard, redone=opnieuw, camera_saved=camera, banner_saved=balk,
                          camera_changed=gewijzigd)


def _check_camera(name: str, url: str, hours: str) -> tuple[dict, list[int] | None, str | None]:
    """Clean the camera form. Returns (form, hours, error)."""
    form = {"name": name.strip(), "url": url.strip().rstrip("/"), "hours": hours.strip()}
    if not form["name"]:
        return form, None, _("Geef de camera een naam.")
    if not form["url"].startswith(("http://", "https://")):
        return form, None, _("Het adres van de bridge begint met http:// of https://, bijvoorbeeld http://192.168.1.50:8090.")
    try:
        chosen = sorted({int(h) for h in form["hours"].replace(" ", "").split(",") if h})
    except ValueError:
        return form, None, _("Controle-uren zijn hele uren, gescheiden door komma's, zoals 0,2,4,22.")
    if any(h < 0 or h > 23 for h in chosen):
        return form, None, _("Controle-uren lopen van 0 tot en met 23.")
    return form, chosen, None


def _bridge_answers(cam: dict) -> str | None:
    try:
        bridge.fetch_status(cam, timeout=8)
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return _("De bridge op {url} antwoordt niet ({reason}). Klopt het adres, en draait de bridge?", url=cam["url"], reason=reason)
    return None


@app.post("/instellingen/camera")
def add_camera(request: Request, name: str = Form(""), url: str = Form(""), token: str = Form(""), hours: str = Form("")):
    """Add a camera behind a bridge. Its bridge must answer before it is saved."""
    form, chosen, error = _check_camera(name, url, hours)
    current = bridge.cameras(db)
    if not error:
        cam = {"id": bridge.camera_id(form["name"], {c["id"] for c in current}), "name": form["name"],
               "url": form["url"], "token": token.strip(), "hours": chosen}
        error = _bridge_answers(cam)
    if error:
        return _settings_page(request, camera_error=error, camera_form=form, status_code=400)
    db.set_setting("cameras", [*current, cam])
    bridge.refresh_status(cam)
    return RedirectResponse(f"/instellingen?camera={cam['id']}#cameras", status_code=303)


@app.post("/instellingen/camera/{cid}")
def edit_camera(request: Request, cid: str, name: str = Form(""), url: str = Form(""), token: str = Form(""),
                hours: str = Form(""), stempel: str | None = Form(None), stempel_was: str = Form("")):
    """Change a camera's name, bridge address, token or check hours; its id stays."""
    old = _camera(cid)
    form, chosen, error = _check_camera(name, url, hours)
    if not error and any(c["name"] == form["name"] and c["id"] != cid for c in bridge.cameras(db)):
        error = _("Er is al een camera met die naam.")
    cam = dict(old, name=form["name"], url=form["url"], hours=chosen or [], token=token.strip() or old.get("token", ""))
    if not error and cam["url"] != old["url"]:
        error = _bridge_answers(cam)
    if error:
        return _settings_page(request, camera_error=error, camera_edit=cid, status_code=400)
    db.set_setting("cameras", [cam if c["id"] == cid else c for c in bridge.cameras(db)])
    if cam["name"] != old["name"]:
        db.rename_source(old["name"], cam["name"])  # clips keep showing their camera
    want = 1 if stempel else 0
    if stempel_was in ("0", "1") and want != int(stempel_was):
        try:
            bridge.queue_settings(cam, {"osdInfoEnable": want})
        except (OSError, ValueError) as exc:
            return _settings_page(request, camera_error=_("Bewaard, maar de datumstempel kon niet naar de bridge: {error}", error=exc),
                                  camera_edit=cid, status_code=502)
    bridge.refresh_status(cam)
    return RedirectResponse(f"/instellingen?gewijzigd={cid}#cameras", status_code=303)


@app.post("/instellingen/camera/{cid}/verwijder")
def remove_camera(cid: str):
    db.set_setting("cameras", [c for c in bridge.cameras(db) if c["id"] != cid])
    bridge.forget(cid)
    return RedirectResponse("/instellingen#cameras", status_code=303)


def _camera(cid: str) -> dict:
    cam = next((c for c in bridge.cameras(db) if c["id"] == cid), None)
    if cam is None:
        raise HTTPException(404, _("Onbekende camera"))
    return cam


@app.post("/camera/{cid}/nu")
def check_camera_now(request: Request, cid: str):
    """Wake the camera now instead of waiting for its next check hour."""
    cam = _camera(cid)
    started = worker.start_camera_session(cam)
    if "application/json" in request.headers.get("accept", ""):
        bridge.refresh_status(cam)
        return {"started": started, "camera": bridge.camera_view(cam)}
    return RedirectResponse("/", status_code=303)


@app.get("/api/cameras")
def api_cameras():
    """Last known status of every camera, for the camera button in the top bar.

    Uses what the worker last heard (every 5 min, every 15 s during a check), so polling this
    never wakes a bridge."""
    views = [bridge.camera_view(c) for c in bridge.cameras(db)]
    return {"summary": bridge.summary(views), "cameras": views}


@app.get("/api/cameras/{cid}")
def api_camera(cid: str):
    """Live view of one camera, for the progress shown while it is being checked."""
    cam = _camera(cid)
    bridge.refresh_status(cam)
    return bridge.camera_view(cam)


@app.post("/instellingen/balk")
async def save_banner(request: Request):
    """Which parts the date bar shows, and where the camera stands (for the weather)."""
    form = await request.form()
    text = str(form.get("locatie", "")).strip()
    try:
        loc = weather.parse_location(text) if text else None
    except ValueError as exc:
        return _settings_page(request, banner_error=str(exc), status_code=400)
    db.set_setting("banner", {key: key in form for key in media.BANNER_PARTS})
    old = weather.location(db)
    if loc != old:
        db.set_setting("location", {"lat": loc[0], "lon": loc[1]} if loc else None)
        db.reset_weather()
        worker._last_weather = 0.0  # look it up now
        worker.wake.set()
    return RedirectResponse("/instellingen?balk=1#balk", status_code=303)


@app.post("/instellingen/opnieuw")
def redo_all(wat: str = Form(...)):
    """Judge clips again in bulk: only where a reading failed, or everything from scratch."""
    if wat == "mislukt":
        if "claude" in models:
            models["claude"].last_error = None
        n = db.requeue_failed_readings()
    elif wat == "alles":
        n = db.forget_all_readings()
    else:
        raise HTTPException(400, _("Onbekende keuze"))
    worker.wake.set()
    return RedirectResponse(f"/instellingen?opnieuw={n}", status_code=303)


@app.post("/instellingen")
async def save_settings(request: Request):
    form = await request.form()

    def refuse(message: str):
        return _settings_page(request, error=message, status_code=400)

    typed = str(form.get("anthropic-key", "")).strip().strip("\"'")
    if typed and not typed.startswith("sk-ant-"):
        return refuse(_("Dat lijkt geen sleutel van Anthropic: die begint met sk-ant-. Er is niets bewaard."))
    key = typed or db.setting("anthropic_key", "")
    if form.get("anthropic-key-weg"):
        key = ""
    chosen = {}
    for name in models:
        if form.get(f"on-{name}"):
            try:
                weight = max(0, min(100, int(form.get(f"weight-{name}", 100)))) / 100
            except ValueError:
                return refuse(_("Ongeldig gewicht. Er is niets bewaard."))
            if CATALOGUE[name].get("needs_key") and not key:
                return refuse(_("{name} heeft een sleutel nodig. Er is niets bewaard.", name=CATALOGUE[name]["label"]))
            if weight > 0:
                chosen[name] = weight
    if not chosen:
        return refuse(_("Kies minstens één model met een gewicht boven nul. Er is niets bewaard."))
    new_key = key != db.setting("anthropic_key", "")
    db.set_setting("anthropic_key", key)
    if new_key and "claude" in models:
        models["claude"].last_error = None
    if chosen != db.setting("models", None) or (new_key and "claude" in chosen):
        db.set_setting("models", chosen)
        # Stills a model has read before are reused, so this only costs the new readings.
        db.requeue_all()
        worker.wake.set()
    return RedirectResponse("/instellingen?bewaard=1", status_code=303)


@app.api_route("/taal/{code}", methods=["GET", "POST"])
def set_language(request: Request, code: str, terug: str = ""):
    """Remember the chosen language in a cookie and go back to where the switch was used."""
    if code not in i18n.LANGS:
        raise HTTPException(404)
    if not terug:
        ref = urlsplit(request.headers.get("referer", ""))
        terug = (ref.path or "/") + (f"?{ref.query}" if ref.query else "")
    if not terug.startswith("/") or terug.startswith("//") or "\\" in terug:
        terug = "/"
    resp = RedirectResponse(terug, status_code=303)
    resp.set_cookie(i18n.COOKIE, code, max_age=365 * 86400, samesite="lax", path="/")
    return resp


# ---- API ---------------------------------------------------------------------------


@app.post("/api/ingest")
async def api_ingest(
    files: list[UploadFile] = File(...),
    captured_at: str | None = Form(None),
    source: str = Form("upload"),
    authorization: str | None = Header(None),
):
    _check_token(authorization)
    out = []
    for upload in files:
        with tempfile.NamedTemporaryFile(delete=False, dir=settings.data_dir, suffix=Path(upload.filename or "").suffix) as tmp:
            shutil.copyfileobj(upload.file, tmp)
            tmp_path = Path(tmp.name)
        clip_id, status = ingest_file(db, settings, tmp_path, upload.filename, source=source, captured_at=captured_at)
        out.append({"file": upload.filename, "clip_id": clip_id, "status": status})
    worker.wake.set()
    return {"results": out}


@app.get("/api/clips")
def api_clips(soort: str | None = None, leeg: bool = False, limit: int = 50, offset: int = 0):
    limit = max(1, min(limit, 500))
    return {"clips": db.list_clips(label=soort, include_blank=leeg, limit=limit, offset=offset)}


@app.get("/api/clips/{clip_id}")
def api_clip(clip_id: int):
    clip = db.get(clip_id)
    if not clip:
        raise HTTPException(404, "Clip niet gevonden")
    return clip


@app.get("/api/stats")
def api_stats():
    return {"labels": dict(db.label_counts()), "status": db.status_counts(), "hours": db.hourly_activity()}


@app.get("/healthz")
def healthz():
    alive = worker.is_alive()
    return JSONResponse({"ok": alive, "classifier": ",".join(voters(db, models)) or "none"}, status_code=200 if alive else 503)
