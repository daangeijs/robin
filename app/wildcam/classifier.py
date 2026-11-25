"""Species classification.

`SpeciesNetClassifier` wraps Google's SpeciesNet (MegaDetector + species classifier
+ geofencing). `DeepFauneClassifier` runs the CNRS DeepFaune models, trained on
European camera traps. `ClaudeClassifier` asks Claude through the Anthropic API,
with a key the user fills in. `MockClassifier` returns fake but stable labels so the
dashboard can be developed and tested without downloading a model.

Every classifier returns one `Result` per input image. Labels of all models are
written in SpeciesNet's form, so their readings can be weighed against each other.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
import threading
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .taxa import TAXA

log = logging.getLogger(__name__)

BLANK = "blank"
# SpeciesNet's label for a frame where the detector and the classifier both came up empty.
NO_RESULT = "no cv result"


@dataclass
class Result:
    filepath: str
    taxon: str | None = None  # full label, e.g. "uuid;mammalia;carnivora;canidae;vulpes;vulpes;red fox"
    common_name: str | None = None  # e.g. "red fox"
    score: float = 0.0
    blank: bool = False
    detections: list[dict] = field(default_factory=list)  # [{label, conf, bbox:[x,y,w,h]}]
    top: list[tuple[str, float]] = field(default_factory=list)  # alternatives
    error: str | None = None
    weight: float = 1.0  # how heavily this reading counts in the verdict


def common_from_taxon(taxon: str | None) -> str | None:
    """SpeciesNet labels look like 'uuid;class;order;family;genus;species;common name'."""
    if not taxon:
        return None
    parts = taxon.split(";")
    return parts[-1].strip() or None


class Classifier:
    name = "base"

    def classify(self, paths: list[Path]) -> list[Result]:  # pragma: no cover - interface
        raise NotImplementedError


class MockClassifier(Classifier):
    """Deterministic fake labels, picked from the file hash. For tests and UI work."""

    name = "mock"

    def __init__(self, salt: int = 0) -> None:
        self.salt = salt  # two mock models with a different salt disagree now and then

    CHOICES = [
        ("red fox", 0.94),
        ("european badger", 0.88),
        ("beech marten", 0.71),
        ("western european hedgehog", 0.9),
        ("domestic cat", 0.97),
        ("eurasian blackbird", 0.83),
        ("roe deer", 0.66),
        ("brown rat", 0.42),
        (BLANK, 0.99),
    ]

    def classify(self, paths: list[Path]) -> list[Result]:
        out = []
        for p in paths:
            h = int(hashlib.sha256(Path(p).read_bytes()).hexdigest(), 16)
            common, score = self.CHOICES[(h + self.salt) % len(self.CHOICES)]
            if common == BLANK:
                out.append(Result(filepath=str(p), taxon=BLANK, common_name=BLANK, score=score, blank=True))
                continue
            x = (h >> 8) % 50 / 100
            y = (h >> 16) % 40 / 100
            out.append(
                Result(
                    filepath=str(p),
                    taxon=f"mock;;;;;;{common}",
                    common_name=common,
                    score=score,
                    detections=[{"label": "animal", "conf": round(score, 2), "bbox": [x, y, 0.35, 0.4]}],
                    top=[(common, score), ("animal", round(1 - score, 2))],
                )
            )
        return out


class SpeciesNetClassifier(Classifier):
    """Google SpeciesNet, loaded lazily on first use (the first run downloads the model)."""

    name = "speciesnet"

    def __init__(self, model_name: str = "", country: str = "NLD", detection_threshold: float = 0.2) -> None:
        self.model_name = model_name
        self.country = country or None
        self.detection_threshold = detection_threshold
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                import speciesnet  # heavy import, keep it lazy

                name = self.model_name or speciesnet.DEFAULT_MODEL
                log.info("Loading SpeciesNet model %s (first run downloads it)", name)
                self._model = speciesnet.SpeciesNet(name, geofence=True)
        return self._model

    def classify(self, paths: list[Path]) -> list[Result]:
        model = self._load()
        raw = model.predict(
            filepaths=[str(p) for p in paths],
            country=self.country,
            run_mode="single_thread",
            progress_bars=False,
        )
        preds = (raw or {}).get("predictions", [])
        by_path = {p.get("filepath"): p for p in preds}
        return [self._parse(str(p), by_path.get(str(p))) for p in paths]

    def _parse(self, filepath: str, pred: dict | None) -> Result:
        if pred is None:
            return Result(filepath=filepath, error="geen voorspelling ontvangen")
        if pred.get("failures") and not pred.get("detections") and "prediction" not in pred:
            return Result(filepath=filepath, error="SpeciesNet: " + ", ".join(pred["failures"]))

        detections = [
            {
                "label": d.get("label"),
                "conf": round(float(d.get("conf", 0.0)), 3),
                "bbox": d.get("bbox"),
            }
            for d in pred.get("detections", []) or []
            if d.get("bbox") and float(d.get("conf", 0.0)) >= self.detection_threshold
        ]

        # SpeciesNet <= 4 puts the final answer at the top level.
        if "prediction" in pred:
            taxon = pred.get("prediction")
            score = float(pred.get("prediction_score", 0.0))
            classes = (pred.get("classifications") or {}).get("classes", [])
            scores = (pred.get("classifications") or {}).get("scores", [])
        else:
            # SpeciesNet >= 5 attaches a processed classification to each detection.
            best = None
            for d in pred.get("detections", []) or []:
                pc = d.get("processed_classification")
                if pc and (best is None or pc.get("score", 0) > best[0].get("score", 0)):
                    best = (pc, d.get("raw_classifications") or {})
            if best is None:
                taxon, score, classes, scores = BLANK, 1.0, [], []
            else:
                taxon = best[0].get("class")
                score = float(best[0].get("score", 0.0))
                classes = best[1].get("classes", [])
                scores = best[1].get("scores", [])

        common = common_from_taxon(taxon) or BLANK
        is_blank = common == BLANK
        top = [(common_from_taxon(c) or c, round(float(s), 3)) for c, s in zip(classes[:5], scores[:5])]
        return Result(
            filepath=filepath,
            taxon=taxon,
            common_name=common,
            score=round(score, 3),
            blank=is_blank,
            detections=[] if is_blank else detections,
            top=top,
        )


class DeepFauneClassifier(Classifier):
    """DeepFaune v1.4 (CNRS): its own detector plus a classifier with 38 European classes.

    Coarser than SpeciesNet: all martens, weasels and polecats are one "mustelid",
    all mice, voles and shrews one "micromammal". The weights (1.2 GB) are fetched
    from the project's server on first use.
    """

    name = "deepfaune"
    SOURCE = "https://pbil.univ-lyon1.fr/software/download/deepfaune/v1.4/"
    DETECTOR = "deepfaune-yolov8s_960.pt"
    WEIGHTS = "deepfaune-vit_large_patch14_dinov2.lvd142m.v4.pt"
    BACKBONE = "vit_large_patch14_dinov2.lvd142m"
    CROP = 182
    DETECTOR_WIDTH = 960
    DETECTOR_THRESHOLD = 0.6
    # The detector's classes, in its order.
    SEEN = ("animal", "human", "vehicle")
    # The classifier's classes, in its order: SpeciesNet's name for the same taxon, then
    # class;order;family;genus;species. "micromammal" has no taxon of its own; it gets an
    # order of its own so that it is not mistaken for "any mammal".
    CLASSES = (
        ("european bison", "mammalia;artiodactyla;bovidae;bison;bonasus"),
        ("eurasian badger", "mammalia;carnivora;mustelidae;meles;meles"),
        ("alpine ibex", "mammalia;artiodactyla;bovidae;capra;ibex"),
        ("eurasian beaver", "mammalia;rodentia;castoridae;castor;fiber"),
        ("red deer", "mammalia;artiodactyla;cervidae;cervus;elaphus"),
        ("golden jackal", "mammalia;carnivora;canidae;canis;aureus"),
        ("chamois", "mammalia;artiodactyla;bovidae;rupicapra;rupicapra"),
        ("domestic cat", "mammalia;carnivora;felidae;felis;catus"),
        ("domestic goat", "mammalia;artiodactyla;bovidae;capra;aegagrus hircus"),
        ("european roe deer", "mammalia;artiodactyla;cervidae;capreolus;capreolus"),
        ("domestic dog", "mammalia;carnivora;canidae;canis;familiaris"),
        ("raccoon dog", "mammalia;carnivora;canidae;nyctereutes;procyonoides"),
        ("common fallow deer", "mammalia;artiodactyla;cervidae;dama;dama"),
        ("sciuridae family", "mammalia;rodentia;sciuridae;;"),
        ("moose", "mammalia;artiodactyla;cervidae;alces;alces"),
        ("equidae family", "mammalia;perissodactyla;equidae;;"),
        ("common genet", "mammalia;carnivora;viverridae;genetta;genetta"),
        ("wolverine", "mammalia;carnivora;mustelidae;gulo;gulo"),
        ("western european hedgehog", "mammalia;eulipotyphla;erinaceidae;erinaceus;europaeus"),
        ("lagomorpha order", "mammalia;lagomorpha;;;"),
        ("grey wolf", "mammalia;carnivora;canidae;canis;lupus"),
        ("eurasian otter", "mammalia;carnivora;mustelidae;lutra;lutra"),
        ("eurasian lynx", "mammalia;carnivora;felidae;lynx;lynx"),
        ("alpine marmot", "mammalia;rodentia;sciuridae;marmota;marmota"),
        ("micromammal", "mammalia;micromammal;;;"),
        ("mouflon", "mammalia;artiodactyla;bovidae;ovis;orientalis"),
        ("domestic sheep", "mammalia;artiodactyla;bovidae;ovis;aries"),
        ("weasel family", "mammalia;carnivora;mustelidae;;"),
        ("bird", "aves;;;;"),
        ("brown bear", "mammalia;carnivora;ursidae;ursus;arctos"),
        ("crested porcupine", "mammalia;rodentia;hystricidae;hystrix;cristata"),
        ("nutria", "mammalia;rodentia;myocastoridae;myocastor;coypus"),
        ("muskrat", "mammalia;rodentia;cricetidae;ondatra;zibethicus"),
        ("raccoon", "mammalia;carnivora;procyonidae;procyon;lotor"),
        ("red fox", "mammalia;carnivora;canidae;vulpes;vulpes"),
        ("reindeer", "mammalia;artiodactyla;cervidae;rangifer;tarandus"),
        ("wild boar", "mammalia;artiodactyla;suidae;sus;scrofa"),
        ("domestic cattle", "mammalia;artiodactyla;bovidae;bos;taurus"),
    )

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self._loaded = None
        self._lock = threading.Lock()

    def _fetch(self, name: str) -> Path:
        path = self.cache_dir / name
        if not path.exists():
            log.info("Downloading %s (first use of DeepFaune)", name)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            partial = path.with_suffix(".part")
            urllib.request.urlretrieve(self.SOURCE + name, partial)  # noqa: S310 - fixed https source
            partial.rename(path)
        return path

    def _load(self):
        with self._lock:
            if self._loaded is None:
                import timm  # heavy imports, keep them lazy
                import torch
                from torchvision.transforms import InterpolationMode, transforms
                from ultralytics import YOLO

                detector = YOLO(str(self._fetch(self.DETECTOR)))
                model = timm.create_model(
                    self.BACKBONE, pretrained=False, num_classes=len(self.CLASSES), dynamic_img_size=True
                )
                saved = torch.load(self._fetch(self.WEIGHTS), map_location="cpu", weights_only=False)
                if saved["args"]["num_classes"] != len(self.CLASSES):
                    raise RuntimeError("DeepFaune-gewichten passen niet bij de klassenlijst")
                model.load_state_dict({k.removeprefix("base_model."): v for k, v in saved["state_dict"].items()})
                model.eval()
                prepare = transforms.Compose(
                    [
                        transforms.Resize((self.CROP, self.CROP), interpolation=InterpolationMode.BICUBIC),
                        transforms.ToTensor(),
                        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
                    ]
                )
                self._loaded = (torch, detector, model, prepare)
        return self._loaded

    def classify(self, paths: list[Path]) -> list[Result]:
        return [self._classify(Path(p)) for p in paths]

    def _classify(self, path: Path) -> Result:
        from PIL import Image

        torch, detector, model, prepare = self._load()
        found = detector(str(path), imgsz=self.DETECTOR_WIDTH, conf=self.DETECTOR_THRESHOLD, verbose=False)[0].boxes
        kinds = [int(c) for c in found.cls.tolist()]
        if not kinds:
            return Result(filepath=str(path), taxon=BLANK, common_name=BLANK, score=1.0, blank=True)
        # Like DeepFaune itself: the first animal if there is one, else whatever was seen.
        k = kinds.index(0) if 0 in kinds else 0
        seen = self.SEEN[kinds[k]]
        conf = round(float(found.conf[k]), 3)
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        x0, y0, x1, y1 = (float(v) for v in found.xyxy[k].tolist())
        detections = [
            {"label": seen, "conf": conf, "bbox": [x0 / image.width, y0 / image.height, (x1 - x0) / image.width, (y1 - y0) / image.height]}
        ]
        if seen != "animal":
            return Result(filepath=str(path), taxon=f"deepfaune;;;;;;{seen}", common_name=seen, score=conf, detections=detections)

        # The classifier looks at a square around the animal.
        grow = abs((x1 - x0) - (y1 - y0)) / 2
        if x1 - x0 > y1 - y0:
            y0, y1 = y0 - grow, y1 + grow
        else:
            x0, x1 = x0 - grow, x1 + grow
        crop = image.crop((max(0, round(x0)), max(0, round(y0)), min(image.width, round(x1)), min(image.height, round(y1))))
        with torch.no_grad():
            scores = model(prepare(crop).unsqueeze(0)).softmax(dim=1)[0]
        order = scores.argsort(descending=True)[:5].tolist()
        name, lineage = self.CLASSES[order[0]]
        return Result(
            filepath=str(path),
            taxon=f"deepfaune;{lineage};{name}",
            common_name=name,
            score=round(float(scores[order[0]]), 3),
            detections=detections,
            top=[(self.CLASSES[i][0], round(float(scores[i]), 3)) for i in order],
        )


RANKS = ("class", "order", "family", "genus", "species")


def _taxon_index() -> dict[str, tuple[str, str]]:
    """SpeciesNet's labels by their deepest rank: "martes foina" or "mustelidae" -> (name, lineage)."""
    index = {}
    for name, lineage in TAXA.items():
        ranks = lineage.split(";")
        deepest = " ".join(ranks[3:5]) if ranks[4] else next(r for r in reversed(ranks) if r)
        index.setdefault(deepest, (name, lineage))
    return index


class ClaudeClassifier(Classifier):
    """Asks Claude what is on the still. Names species the camera-trap models only know as a group.

    Claude answers with a Dutch name, its certainty and the scientific classification.
    The classification is looked up in SpeciesNet's taxonomy, so "steenmarter" becomes
    the same "beech marten" label SpeciesNet uses and the readings can be weighed
    against each other. A species SpeciesNet has no label for keeps Claude's own name.
    """

    name = "claude"
    MODEL = "claude-opus-5-5"
    PROMPT = (
        "What animal is this, in Dutch? This is a still from a trail camera in {country} (ISO country code).\n"
        "Reply with JSON: the Dutch name in `species`, your certainty from 0 to 1 in `certainty`, and the "
        "scientific classification in `scientific`. Name the species when you can tell; when you can only "
        "tell the group (for example a marten, but not which one), give the Dutch name of that group and "
        "leave the ranks below it empty. Certainty is how sure you are of exactly the name you give. "
        "With no animal or person in view, set `animal` to false and leave the rest empty."
    )
    SCHEMA = {
        "type": "object",
        "properties": {
            "animal": {"type": "boolean"},
            "species": {"type": "string"},
            "certainty": {"type": "number"},
            "scientific": {
                "type": "object",
                "properties": {rank: {"type": "string"} for rank in RANKS},
                "required": list(RANKS),
                "additionalProperties": False,
            },
        },
        "required": ["animal", "species", "certainty", "scientific"],
        "additionalProperties": False,
    }

    def __init__(self, key: Callable[[], str], country: str = "NLD") -> None:
        self.last_error: str | None = None
        self.key = key
        self.country = country
        self.index = _taxon_index()

    def classify(self, paths: list[Path]) -> list[Result]:
        key = self.key()
        if not key:
            self.last_error = "geen sleutel ingevuld"
            return [Result(filepath=str(p), error="Claude: geen sleutel ingevuld") for p in paths]
        # A token of a Claude subscription only works through Claude Code; an API key goes to the API.
        ask = self._ask_claude_code if key.startswith("sk-ant-oat") else self._ask_api
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(lambda p: self._classify(ask, key, Path(p)), paths))

    def _classify(self, ask, key: str, path: Path) -> Result:
        try:
            result = self.result(str(path), ask(key, path))
        except RuntimeError as exc:
            # Kept for the settings page, so a refused key or a used-up limit shows up there.
            self.last_error = str(exc)
            return Result(filepath=str(path), error=f"Claude: {exc}")
        self.last_error = None
        return result

    def _ask_claude_code(self, key: str, path: Path) -> dict:
        """One headless Claude Code run that reads the still, signed in with the subscription token."""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
        command = [
            "claude", "-p", f"Look at the image {path.resolve()}. " + self.PROMPT.format(country=self.country),
            "--model", self.MODEL, "--tools", "Read", "--allowedTools", "Read", "--setting-sources", "",
            "--no-session-persistence", "--output-format", "json", "--json-schema", json.dumps(self.SCHEMA),
        ]  # fmt: skip
        try:
            done = subprocess.run(  # noqa: S603 - fixed command, no shell
                command, env={**env, "CLAUDE_CODE_OAUTH_TOKEN": key}, cwd=path.parent, capture_output=True, text=True, timeout=180
            )
        except FileNotFoundError:
            raise RuntimeError("Claude Code is niet ge\u00efnstalleerd, nodig voor een abonnementstoken") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError("geen antwoord binnen drie minuten") from None
        try:
            out = json.loads(done.stdout)
        except ValueError:
            raise RuntimeError((done.stderr or done.stdout).strip()[:200] or "Claude Code gaf geen antwoord") from None
        if out.get("is_error") or not out.get("structured_output"):
            raise RuntimeError(str(out.get("result") or out.get("subtype") or "geen antwoord")[:200])
        return out["structured_output"]

    def _ask_api(self, key: str, path: Path) -> dict:
        import anthropic  # keep start-up light

        client = anthropic.Anthropic(api_key=key)
        try:
            # If Claude declines, the API hands the request to another Claude model by itself.
            response = client.beta.messages.create(
                model=self.MODEL,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"format": {"type": "json_schema", "schema": self.SCHEMA}},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": base64.standard_b64encode(path.read_bytes()).decode(),
                                },
                            },
                            {"type": "text", "text": self.PROMPT.format(country=self.country)},
                        ],
                    }
                ],
            )
        except anthropic.AuthenticationError:
            raise RuntimeError("de API-sleutel wordt geweigerd") from None
        except anthropic.RateLimitError:
            raise RuntimeError("te veel verzoeken, later opnieuw proberen") from None
        except anthropic.APIStatusError as exc:
            raise RuntimeError(f"fout {exc.status_code}: {exc.message}") from None
        except anthropic.APIConnectionError:
            raise RuntimeError("geen verbinding met de API") from None
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"geen antwoord ({response.stop_reason})")
        return json.loads(next(b.text for b in response.content if b.type == "text"))

    def result(self, filepath: str, answer: dict) -> Result:
        """Claude's answer as a reading with SpeciesNet's label for the same taxon."""
        ranks = [str(answer["scientific"].get(rank, "")).strip().lower() for rank in RANKS]
        if ranks[4].startswith(ranks[3] + " "):  # "martes foina" in the species field
            ranks[4] = ranks[4].removeprefix(ranks[3] + " ")
        if not answer["animal"] or not any(ranks):
            return Result(filepath=filepath, taxon=BLANK, common_name=BLANK, score=1.0, blank=True)
        deepest = " ".join(ranks[3:5]) if ranks[4] else next(r for r in reversed(ranks) if r)
        name, lineage = self.index.get(deepest, (answer["species"].strip().lower(), ";".join(ranks)))
        score = round(max(0.0, min(1.0, float(answer["certainty"]))), 3)
        return Result(filepath=filepath, taxon=f"claude;{lineage};{name}", common_name=name, score=score)


# What the settings page says about each model.
CATALOGUE = {
    "speciesnet": {
        "label": "SpeciesNet",
        "maker": "Google",
        "about": "Kent ruim 2000 soorten wereldwijd en houdt rekening met wat in Nederland voorkomt. "
        "Noemt de soort als het zeker is, anders de groep (marterachtige, muis of rat).",
    },
    "deepfaune": {
        "label": "DeepFaune",
        "maker": "CNRS",
        "about": "Getraind op Europese wildcamera's, 38 klassen. Grover: alle marters, wezels en bunzingen zijn "
        "samen \u201cmarterachtige\u201d, alle muizen \u201cklein zoogdier\u201d. Eerste gebruik downloadt 1,2 GB.",
    },
    "claude": {
        "label": "Claude",
        "maker": "Anthropic",
        "about": "Een taalmodel dat naar de still kijkt en de soort noemt, ook waar de andere modellen bij de groep "
        "blijven (steenmarter of boommarter). Elke still gaat naar Anthropic. Met een API-sleutel kost dat ongeveer "
        "een cent per still; met een token van je Claude-abonnement telt het mee in je limiet en duurt het langer.",
        "needs_key": True,
    },
}


def build_models(
    kind: str,
    *,
    model_name: str = "",
    country: str = "NLD",
    cache_dir: Path = Path("/models"),
    claude_key: Callable[[], str] = lambda: "",
) -> dict[str, Classifier]:
    """Every model this installation can run, by name. Which ones vote is a setting."""
    kind = (kind or "speciesnet").lower()
    if kind == "mock":
        return {"speciesnet": MockClassifier(), "deepfaune": MockClassifier(salt=3), "claude": MockClassifier(salt=7)}
    if kind == "none":
        return {}
    if kind == "speciesnet":
        return {
            "speciesnet": SpeciesNetClassifier(model_name=model_name, country=country),
            "deepfaune": DeepFauneClassifier(cache_dir / "deepfaune"),
            "claude": ClaudeClassifier(claude_key, country=country),
        }
    raise ValueError(f"Onbekende classifier: {kind}")
