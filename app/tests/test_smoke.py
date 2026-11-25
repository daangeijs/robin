"""End-to-end smoke test with the mock classifier: ingest, process, render."""

import io
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from PIL import Image

TMP = Path(tempfile.mkdtemp(prefix="wildcam-test-"))
os.environ.update(
    WILDCAM_DATA_DIR=str(TMP),
    WILDCAM_CLASSIFIER="mock",
    WILDCAM_INGEST_TOKEN="test-token",
    WILDCAM_INBOX_POLL_SECONDS="1",
)

from fastapi.testclient import TestClient  # noqa: E402

from wildcam.classifier import Result, SpeciesNetClassifier  # noqa: E402
from wildcam.main import app, db, worker  # noqa: E402
from wildcam.names_nl import to_dutch  # noqa: E402
from wildcam.worker import combine  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}


def jpeg(seed: int, when: str | None = None) -> bytes:
    im = Image.new("RGB", (320, 240), (seed * 37 % 255, seed * 91 % 255, seed * 53 % 255))
    buf = io.BytesIO()
    exif = Image.Exif()
    if when:
        exif.get_ifd(0x8769)[36867] = when
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def wait_done(client, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if not db.status_counts().get("pending"):
            return
        time.sleep(0.2)
    raise AssertionError("worker did not finish")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_ingest_requires_token(client):
    r = client.post("/api/ingest", files={"files": ("a.jpg", jpeg(1), "image/jpeg")})
    assert r.status_code == 401


def test_ingest_process_and_render(client):
    files = [("files", (f"IMG_{i}.JPG", jpeg(i, f"2026:11:2{i % 3} 0{i}:15:00"), "image/jpeg")) for i in range(1, 7)]
    r = client.post("/api/ingest", files=files, headers=AUTH)
    assert r.status_code == 200
    assert all(x["status"] == "queued" for x in r.json()["results"])

    # duplicate is ignored
    r = client.post("/api/ingest", files={"files": ("dup.jpg", jpeg(1, "2026:11:21 01:15:00"), "image/jpeg")}, headers=AUTH)
    assert r.json()["results"][0]["status"] == "duplicate"

    wait_done(client)
    clips = client.get("/api/clips?leeg=1").json()["clips"]
    assert len(clips) == 6
    assert all(c["status"] == "done" for c in clips)
    assert clips[0]["captured_at"].startswith("2026-11-2")

    assert client.get("/").status_code == 200
    assert client.get("/statistiek").status_code == 200
    page = client.get(f"/clip/{clips[0]['id']}")
    assert page.status_code == 200 and "Wat is het dan?" in page.text


def test_video_ingest(client):
    video = TMP / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=10", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    r = client.post("/api/ingest", files={"files": ("PICT0001.MP4", video.read_bytes(), "video/mp4")}, headers=AUTH)
    clip_id = r.json()["results"][0]["clip_id"]
    wait_done(client)
    clip = db.get(clip_id)
    assert clip["status"] == "done" and clip["kind"] == "video" and clip["thumb"]
    assert len(clip["votes"]) == 6 and all((TMP / "thumbs" / v["thumb"]).exists() for v in clip["votes"])

    # the user picks the stills to look at
    assert client.post(f"/clip/{clip_id}/opnieuw", data={"times": "2.5, 0.4,0.4"}, follow_redirects=False).status_code == 303
    wait_done(client)
    clip = db.get(clip_id)
    assert [v["t"] for v in clip["votes"]] == [0.4, 2.5] and clip["frame_times"] == [0.4, 2.5]
    assert len(list((TMP / "thumbs").glob(f"{clip_id:06d}_s*.jpg"))) == 2
    page = client.get(f"/clip/{clip_id}").text
    assert page.count('class="still"') == 2
    assert client.get(f"/api/clips/{clip_id}").json()["status"] == "done"

    # a still the model has read is not read again
    read = []
    model = worker.models["speciesnet"]
    original = model.classify
    model.classify = lambda paths: read.extend(paths) or original(paths)
    client.post(f"/clip/{clip_id}/opnieuw", data={"times": "0.4,1.5,2.5"})
    wait_done(client)
    model.classify = original
    assert len(read) == 1 and [v["t"] for v in db.get(clip_id)["votes"]] == [0.4, 1.5, 2.5]
    assert all(set(v["by"]) == {"speciesnet"} for v in db.get(clip_id)["votes"])
    assert len(list((TMP / "thumbs").glob(f"{clip_id:06d}_s*.jpg"))) == 3
    assert client.post(f"/clip/{clip_id}/opnieuw", data={"times": "abc"}).status_code == 400
    assert client.post(f"/clip/{clip_id}/opnieuw", data={"times": ",".join(str(i / 10) for i in range(60))}).status_code == 400


def test_inbox_and_manual_label(client):
    (TMP / "inbox" / "SD").mkdir(parents=True, exist_ok=True)
    f = TMP / "inbox" / "SD" / "IMG_9999.JPG"
    f.write_bytes(jpeg(42, "2026:11:22 23:01:00"))
    old = time.time() - 60
    os.utime(f, (old, old))
    end = time.time() + 15
    while time.time() < end and f.exists():
        time.sleep(0.3)
    assert not f.exists()
    wait_done(client)
    clip = client.get("/api/clips?leeg=1&limit=500").json()["clips"]
    target = next(c for c in clip if c["original_name"] == "IMG_9999.JPG")
    r = client.post(f"/clip/{target['id']}/label", data={"label": "vos"}, follow_redirects=False)
    assert r.status_code == 303
    assert db.get(target["id"])["label"] == "vos"


def test_speciesnet_v5_parsing():
    sn = SpeciesNetClassifier()
    pred = {
        "filepath": "x.jpg",
        "detections": [
            {
                "category": "1",
                "label": "animal",
                "conf": 0.91,
                "bbox": [0.1, 0.2, 0.3, 0.4],
                "raw_classifications": {
                    "classes": ["u;mammalia;carnivora;canidae;vulpes;vulpes;red fox", "u;;;;;;animal"],
                    "scores": [0.93, 0.04],
                },
                "processed_classification": {"class": "u;mammalia;carnivora;canidae;vulpes;vulpes;red fox", "score": 0.93, "source": "classifier"},
            }
        ],
    }
    r = sn._parse("x.jpg", pred)
    assert r.common_name == "red fox" and not r.blank and r.detections and r.top[0][0] == "red fox"

    blank = sn._parse("y.jpg", {"filepath": "y.jpg", "detections": []})
    assert blank.blank

    v4 = sn._parse("z.jpg", {"filepath": "z.jpg", "prediction": "u;;;;;;blank", "prediction_score": 0.8, "detections": []})
    assert v4.blank


MUSTELIDS = "u;mammalia;carnivora;mustelidae"


def frame(name: str, score: float, taxon: str = "", blank: bool = False) -> Result:
    return Result(filepath=f"{name}-{score}.jpg", taxon=taxon, common_name=name, score=score, blank=blank)


def test_frames_agree_on_species():
    pine = f"{MUSTELIDS};martes;martes;pine marten"
    v = combine([frame("pine marten", 0.8, pine), frame("pine marten", 0.7, pine), frame("blank", 0.9, blank=True), frame("no cv result", 0.3)], 0.5)
    # frames without an animal or without a result do not vote
    assert v.common_name == "pine marten" and v.score == 0.75 and v.filepath == "pine marten-0.8.jpg"


def test_frames_disagree_on_species_agree_on_family():
    frames = [
        frame("pine marten", 0.6, f"{MUSTELIDS};martes;martes;pine marten"),
        frame("beech marten", 0.55, f"{MUSTELIDS};martes;foina;beech marten"),
        frame("weasel family", 0.9, f"{MUSTELIDS};;;weasel family"),
        frame("pine marten", 0.7, f"{MUSTELIDS};martes;martes;pine marten"),
    ]
    v = combine(frames, 0.5)
    assert v.common_name == "weasel family" and v.score == 0.688
    assert dict(v.top)["pine marten"] == 0.325
    assert to_dutch(v.common_name) == "marterachtige"


def test_no_agreement_is_reported_as_uncertain():
    v = combine([frame("red fox", 0.4, "u;mammalia;carnivora;canidae;vulpes;vulpes;red fox"), frame("vehicle", 0.3, "u;;;;;;vehicle")], 0.5)
    assert v.common_name == "red fox" and v.score < 0.5


def test_all_frames_empty():
    assert combine([frame("blank", 0.9, blank=True), frame("blank", 0.8, blank=True)], 0.5).blank


def test_review_marks(client):
    clip = client.get("/api/clips?leeg=1").json()["clips"][0]
    cid = clip["id"]
    assert client.post(f"/clip/{cid}/review", data={"review": "onzin"}, follow_redirects=False).status_code == 400
    client.post(f"/clip/{cid}/review", data={"review": "twijfel"})
    assert db.get(cid)["review"] == "twijfel"
    page = client.get("/?review=twijfel")
    assert f"/clip/{cid}" in page.text and page.text.count('class="card') == 1
    client.post(f"/clip/{cid}/review", data={"review": "bevestigd"})
    assert db.get(cid)["review"] == "bevestigd" and client.get(f"/clip/{cid}").text.count('aria-pressed="true"') == 1
    # a new verdict from the model has not been reviewed
    client.post(f"/clip/{cid}/opnieuw")
    wait_done(client)
    again = db.get(cid)
    assert again["review"] is None and again["status"] == "done"
    # naming the animal yourself confirms it
    client.post(f"/clip/{cid}/label", data={"label": "das"})
    assert db.get(cid)["review"] == "bevestigd"


def test_stored_names_follow_the_name_table(client):
    cid = client.get("/api/clips?leeg=1").json()["clips"][-1]["id"]
    db.update_clip(cid, common_name="weasel family", name_nl="weasel family")
    db.retranslate(to_dutch)
    assert db.get(cid)["name_nl"] == "marterachtige"


def test_weights_decide_between_models():
    fox = frame("red fox", 0.9, "u;mammalia;carnivora;canidae;vulpes;vulpes;red fox")
    cat = frame("domestic cat", 0.9, "d;mammalia;carnivora;felidae;felis;catus;domestic cat")
    cat.weight = 0.25
    assert combine([fox, cat], 0.5).common_name == "red fox"
    fox.weight, cat.weight = 0.25, 1.0
    assert combine([fox, cat], 0.5).common_name == "domestic cat"


def test_coarse_model_backs_the_group():
    marten = frame("pine marten", 0.6, f"{MUSTELIDS};martes;martes;pine marten")
    family = frame("weasel family", 0.99, "deepfaune;mammalia;carnivora;mustelidae;;;weasel family")
    v = combine([marten, family], 0.5)
    assert v.common_name == "weasel family" and v.score == 0.795


def test_model_choice(client):
    assert "DeepFaune" in client.get("/instellingen").text
    assert client.post("/instellingen", data={}).status_code == 400
    clip_id = next(c["id"] for c in client.get("/api/clips?leeg=1&limit=500").json()["clips"] if c["kind"] == "video")
    before = db.get(clip_id)["votes"]

    # switching on a second model reads every still once more, with that model only
    read = {"speciesnet": [], "deepfaune": []}
    originals = {name: model.classify for name, model in worker.models.items()}
    for name, model in worker.models.items():
        model.classify = lambda paths, name=name: read[name].extend(paths) or originals[name](paths)
    r = client.post("/instellingen", data={"on-speciesnet": "on", "weight-speciesnet": "100", "on-deepfaune": "on", "weight-deepfaune": "50"}, follow_redirects=False)
    assert r.status_code == 303 and db.setting("models", None) == {"speciesnet": 1.0, "deepfaune": 0.5}
    wait_done(client)
    for name, model in worker.models.items():
        model.classify = originals[name]
    votes = db.get(clip_id)["votes"]
    assert all(set(v["by"]) == {"speciesnet", "deepfaune"} for v in votes) and len(votes) == len(before)
    assert not any(str(p).endswith(".jpg") and "frame_" in str(p) for p in read["speciesnet"])
    page = client.get(f"/clip/{clip_id}").text
    assert page.count('class="src"') == 2 * len(votes) and "Modellen" not in page

    client.post("/instellingen", data={"on-speciesnet": "on", "weight-speciesnet": "100"})
    wait_done(client)
    assert db.setting("models", None) == {"speciesnet": 1.0}


def test_claude_answer_becomes_the_shared_label():
    from wildcam.classifier import ClaudeClassifier

    claude = ClaudeClassifier(lambda: "")
    ranks = {"class": "Mammalia", "order": "Carnivora", "family": "Mustelidae", "genus": "Martes", "species": "Martes foina"}
    r = claude.result("x.jpg", {"animal": True, "species": "Steenmarter", "certainty": 0.6, "scientific": ranks})
    assert (r.common_name, r.score) == ("beech marten", 0.6)
    assert r.taxon == "claude;mammalia;carnivora;mustelidae;martes;foina;beech marten"

    group = claude.result("x.jpg", {"animal": True, "species": "marterachtige", "certainty": 0.9, "scientific": {**ranks, "genus": "", "species": ""}})
    assert group.common_name == "weasel family"

    # A species SpeciesNet has no label for keeps Claude's own name and classification.
    odd = {"class": "mammalia", "order": "rodentia", "family": "gliridae", "genus": "graphiurus", "species": "nagtglasii"}
    r = claude.result("x.jpg", {"animal": True, "species": "Slaapmuis", "certainty": 0.7, "scientific": odd})
    assert r.common_name == "slaapmuis" and r.taxon == "claude;mammalia;rodentia;gliridae;graphiurus;nagtglasii;slaapmuis"
    raccoon = {"class": "mammalia", "order": "carnivora", "family": "procyonidae", "genus": "procyon", "species": "lotor"}
    r = claude.result("x.jpg", {"animal": True, "species": "wasbeer", "certainty": 0.6, "scientific": raccoon})
    assert r.common_name == "northern raccoon" and to_dutch(r.common_name) == "wasbeer"

    empty = claude.result("x.jpg", {"animal": False, "species": "", "certainty": 0, "scientific": dict.fromkeys(ranks, "")})
    assert empty.blank
    assert claude.classify(["x.jpg"])[0].error  # no key filled in


def test_claude_needs_a_key(client):
    both = {"on-speciesnet": "on", "weight-speciesnet": "100", "on-claude": "on", "weight-claude": "100"}
    assert client.post("/instellingen", data=both).status_code == 400
    assert client.post("/instellingen", data={**both, "anthropic-key": "geen-sleutel"}).status_code == 400
    assert "niets bewaard" in client.post("/instellingen", data={**both, "anthropic-key": "geen-sleutel"}).text
    r = client.post("/instellingen", data={**both, "anthropic-key": "sk-ant-api03-test-1234"}, follow_redirects=False)
    assert r.status_code == 303 and db.setting("models", None) == {"speciesnet": 1.0, "claude": 1.0}
    wait_done(client)
    page = client.get("/instellingen").text
    assert "sk-ant-api…1234" in page and "api03-test" not in page
    client.post("/instellingen", data={"on-speciesnet": "on", "weight-speciesnet": "100", "anthropic-key-weg": "on"})
    wait_done(client)
    assert db.setting("anthropic_key", None) == "" and db.setting("models", None) == {"speciesnet": 1.0}


def test_failed_reading_is_shown(client):
    clip_id = next(c["id"] for c in client.get("/api/clips?leeg=1&limit=500").json()["clips"] if c["kind"] == "video")
    claude = worker.models["claude"]
    original = claude.classify

    def refuse(paths):
        claude.last_error = "de API-sleutel wordt geweigerd"
        return [Result(filepath=str(p), error="Claude: de API-sleutel wordt geweigerd") for p in paths]

    claude.classify = refuse
    try:
        client.post("/instellingen", data={"on-speciesnet": "on", "weight-speciesnet": "100", "on-claude": "on", "weight-claude": "100", "anthropic-key": "sk-ant-api03-x"})
        wait_done(client)
        client.post(f"/clip/{clip_id}/opnieuw")  # forget earlier readings, so Claude is asked again
        wait_done(client)
        page = client.get(f"/clip/{clip_id}").text
        assert "Claude werkt niet" in page and 'class="who failed"' in page
        assert all("claude" not in v["by"] and "claude" in v["errors"] for v in db.get(clip_id)["votes"])
        assert "Mislukte opnieuw" in page
        assert db.count_failed_readings() >= 1
        claude.classify = original  # the limit is over: retrying fills in the missing readings
        client.post("/instellingen/opnieuw", data={"wat": "mislukt"})
        wait_done(client)
        assert db.count_failed_readings() == 0
        assert all("claude" in v["by"] for v in db.get(clip_id)["votes"])
        assert "werkt niet" not in client.get("/instellingen").text
    finally:
        claude.classify = original
        claude.last_error = None
        client.post("/instellingen", data={"on-speciesnet": "on", "weight-speciesnet": "100", "anthropic-key-weg": "on"})
        wait_done(client)


def test_redo_everything(client):
    wait_done(client)
    r = client.post("/instellingen/opnieuw", data={"wat": "alles"}, follow_redirects=True)
    assert r.status_code == 200 and "terug in de wachtrij" in r.text
    wait_done(client)
    assert all(c["status"] == "done" for c in client.get("/api/clips?leeg=1&limit=500").json()["clips"])
    assert client.post("/instellingen/opnieuw", data={"wat": "iets"}).status_code == 400



class FakeBridge:
    """A camera bridge on localhost: /status, /clips, /clips/<id> and DELETE, /poll."""

    def __init__(self, clips):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.waiting, self.deleted, self.polls, self.settings = dict(clips), [], 0, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/status":
                    self._send(200, json.dumps({"bridge": {"state": "idle"}, "camera": {"battery": 60},
                                                "esp": {"camera": "AA:BB:CC:DD:12:34", "rssi": -84, "seen_s": 12}}).encode())
                elif self.path == "/clips":
                    self._send(200, json.dumps({"clips": [
                        {"id": k, "filename": n, "size": len(d), "captured_at": "2026-11-25T09:15:00+01:00"}
                        for k, (n, d) in outer.waiting.items()]}).encode())
                else:
                    self._send(200, outer.waiting[self.path.rsplit("/", 1)[1]][1], "image/jpeg")

            def do_DELETE(self):
                key = self.path.rsplit("/", 1)[1]
                outer.deleted.append(key)
                outer.waiting.pop(key, None)
                self._send(204)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/settings":
                    outer.settings.append(json.loads(body))
                else:
                    outer.polls += 1
                self._send(202, b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def test_pull_from_bridge(client):
    """Clips waiting on a camera bridge come in once, and are then deleted on the Pi."""
    from wildcam import bridge

    fake = FakeBridge({"111": ("20261125-091500_111.jpg", jpeg(201)), "222": ("20261125-091600_222.jpg", jpeg(202))})
    try:
        cam = {"id": "tuin", "name": "Tuin", "url": fake.url, "token": "", "hours": []}
        assert bridge.pull(db, worker.settings, cam) == 2
        assert sorted(fake.deleted) == ["111", "222"] and not fake.waiting
        assert bridge.pull(db, worker.settings, cam) == 0  # nothing left
        wait_done(client)
        clips = {c["original_name"]: c for c in client.get("/api/clips?leeg=1&limit=500").json()["clips"]}
        assert clips["20261125-091500_111.jpg"]["source"] == "Tuin"
    finally:
        fake.server.shutdown()


def test_add_camera_and_dashboard(client):
    fake = FakeBridge({})
    try:
        r = client.post("/instellingen/camera", data={"name": "Tuin", "url": "http://127.0.0.1:1", "hours": "0,2"})
        assert r.status_code == 400 and "antwoordt niet" in r.text and db.setting("cameras", []) == []
        r = client.post("/instellingen/camera", data={"name": "Tuin", "url": fake.url, "hours": "25"})
        assert r.status_code == 400 and "0 tot en met 23" in r.text
        r = client.post("/instellingen/camera", data={"name": "Tuin", "url": fake.url, "hours": "0, 2,22"})
        assert r.status_code == 200 and "Camera toegevoegd" in r.text
        cam = db.setting("cameras", [])[0]
        assert cam["id"] == "tuin" and cam["hours"] == [0, 2, 22]
        page = client.get("/").text
        for text in ("Tuin", "TC100_1234", "in bereik", "-84 dBm, matig", "60%", "Nu controleren"):
            assert text in page, text
        client.post("/camera/tuin/nu")
        assert fake.polls == 1
        r = client.post("/instellingen/camera/tuin", data={"name": "Achtertuin", "url": fake.url, "hours": "1,3",
                                                           "stempel_was": "1"})
        assert r.status_code == 200 and "Camera bewaard" in r.text
        cam = db.setting("cameras", [])[0]
        assert (cam["id"], cam["name"], cam["hours"]) == ("tuin", "Achtertuin", [1, 3])
        assert fake.settings == [{"osdInfoEnable": 0}]
        r = client.post("/instellingen/camera/tuin", data={"name": "Achtertuin", "url": fake.url, "hours": "99"})
        assert r.status_code == 400 and "0 tot en met 23" in r.text
        client.post("/instellingen/camera/tuin/verwijder")
        assert db.setting("cameras", []) == [] and "Nu controleren" not in client.get("/").text
    finally:
        fake.server.shutdown()


def test_camera_summary():
    from wildcam import bridge

    def view(name, tone):
        return {"name": name, "tone": tone, "state": "x"}

    assert bridge.summary([view("a", "ok")])["tone"] == "ok"
    s = bridge.summary([view("a", "busy"), view("b", "bad")])
    assert s["tone"] == "bad" and s["busy"] and s["label"] == "2 camera's" and "b: x" in s["title"]
    bridge._status["t"] = {"data": {"bridge": {"state": "idle"}, "last_session": {"error": "boom", "result": "camera niet bereikbaar"}}, "at": 0}
    assert bridge.camera_view({"id": "t", "name": "T", "url": "http://x"})["tone"] == "bad"
    bridge.forget("t")


def test_banner_settings_and_burn(client, tmp_path):
    from datetime import datetime
    from PIL import Image
    from wildcam import media
    from wildcam.main import db

    r = client.post("/instellingen/balk", data={"date": "on", "time": "on"}, follow_redirects=False)
    assert r.status_code == 303 and db.setting("banner", None) == {
        "camera": False, "date": True, "time": True, "ticking": False, "moon": False, "weather": False, "temperature": False}
    assert client.post("/instellingen/balk", data={"locatie": "200, 5"}).status_code == 400
    assert db.setting("location", None) is None
    assert "Balk bewaard" in client.get("/instellingen?balk=1").text
    src, out = tmp_path / "a.jpg", tmp_path / "b.jpg"
    Image.new("RGB", (640, 480), (40, 80, 40)).save(src)
    info = {"camera": "Tuin", "when": datetime(2026, 11, 25, 10, 37, 12), "temperature": -1.2, "is_day": 1,
            "weather": ("Regen", "rain")}
    media.with_banner(src, out, "image", info, media.banner_options(None))
    with Image.open(out) as im:
        assert im.size == (640, 480) and im.getpixel((5, 470)) != (40, 80, 40)  # bar at the bottom
        assert im.getpixel((5, 5)) == (40, 80, 40) or max(abs(a - b) for a, b in zip(im.getpixel((5, 5)), (40, 80, 40))) < 4


def test_weather_and_moon():
    from datetime import datetime, timezone
    from wildcam import media, weather

    assert weather.parse_location("52.0, 5.8") == (52.0, 5.8)
    assert weather.describe(3) == ("Bewolkt", "cloudy") and weather.describe(None) is None
    full, _ = media.moon_illumination(datetime(2026, 11, 24, 14, 53, tzinfo=timezone.utc))
    new, _ = media.moon_illumination(datetime(2026, 12, 9, 0, 0, tzinfo=timezone.utc))
    assert full > 0.99 and new < 0.01


def test_english_pages(client):
    """Accept-Language picks the language; the cookie set by the switch wins over it."""
    from wildcam import i18n

    en, nl = {"Accept-Language": "en-GB,en;q=0.9,nl;q=0.5"}, {"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.5"}
    i18n.missing.clear()
    page = client.get("/", headers=en).text
    assert '<html lang="en">' in page and "Timeline" in page and "Statistics" in page and "Tijdlijn" not in page
    assert "Tijdlijn" in client.get("/", headers=nl).text
    assert "Tijdlijn" in client.get("/").text  # no header at all: the app's own Dutch
    assert "Settings" in client.get("/instellingen", headers={"Accept-Language": "de-DE"}).text

    r = client.get("/taal/en?terug=/instellingen", headers=nl, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/instellingen" and "lang=en" in r.headers["set-cookie"]
    assert client.get("/taal/en?terug=//evil.example", follow_redirects=False).headers["location"] == "/"
    try:
        settings_page = client.get("/instellingen", headers=nl).text
        assert "Date bar on the footage" in settings_page and "Balk over de beelden" not in settings_page
        assert "Models" in settings_page and "Modellen" not in settings_page and "Language" in settings_page
        stats = client.get("/statistiek", headers=nl).text
        assert "When active" in stats and "All animals" in stats

        cid = next(c["id"] for c in client.get("/api/clips?leeg=1&limit=500").json()["clips"] if c["kind"] == "image")
        client.post(f"/clip/{cid}/label", data={"label": "Red fox"})
        assert db.get(cid)["label"] == "vos"  # typed in English, stored as the Dutch label filters use
        clip_page = client.get(f"/clip/{cid}").text
        assert "What is it then?" in clip_page and "Correct" in clip_page and "<h1>Red fox</h1>" in clip_page
        index = client.get("/?soort=vos").text
        assert 'href="/?soort=vos"' in index and "Red fox <b>" in index
        assert not i18n.missing, i18n.missing
    finally:
        client.cookies.clear()
    assert "Wat is het dan?" in client.get(f"/clip/{cid}").text

    from wildcam.main import _weather  # the weather label of the date bar, also when burnt into a download
    token = i18n.set_lang("en")
    try:
        assert _weather(63) == ("Rain", "rain")
    finally:
        i18n.reset_lang(token)
    assert _weather(63) == ("Regen", "rain")


def test_api_camera_state_follows_language(client):
    from wildcam import bridge

    db.set_setting("cameras", [{"id": "t", "name": "T", "url": "http://127.0.0.1:1", "token": "", "hours": [0]}])
    bridge._status["t"] = {"data": {"bridge": {"state": "idle"}, "esp": {"seen_s": 700, "rssi": -60}}, "at": 0}
    try:
        en = client.get("/api/cameras", headers={"Accept-Language": "en"}).json()
        nl = client.get("/api/cameras", headers={"Accept-Language": "nl"}).json()
        assert en["cameras"][0]["state"] == "no signal for 11 min" and en["cameras"][0]["signal"] == "good"
        assert nl["cameras"][0]["state"] == "11 min geen signaal" and nl["cameras"][0]["signal"] == "goed"
        assert en["summary"]["title"] == "1 camera, T: no signal for 11 min" and en["summary"]["tone"] == nl["summary"]["tone"] == "warn"
        bridge._status["t"]["data"]["bridge"] = {"state": "session", "current_session": {"phase": "downloaden", "files_total": 3, "file_index": 2}}
        assert client.get("/api/cameras", headers={"Accept-Language": "en"}).json()["cameras"][0]["progress"]["text"] == "Fetching clip 2 of 3"
        client.cookies.set("lang", "nl")  # the cookie wins over the header
        assert client.get("/api/cameras", headers={"Accept-Language": "en"}).json()["cameras"][0]["progress"]["text"] == "Clip 2 van 3 ophalen"
    finally:
        client.cookies.clear()
        db.set_setting("cameras", [])
        bridge.forget("t")
