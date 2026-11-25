"""Dutch and English, without a dependency.

The source strings are the Dutch text itself; EN maps each one to English. The language is
chosen per request (cookie `lang`, otherwise Accept-Language) and kept in a context variable,
so template helpers, the camera views and other strings built in Python all follow it.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

from markupsafe import Markup, escape

LANGS = ("nl", "en")
DEFAULT = "nl"
COOKIE = "lang"

_lang: ContextVar[str] = ContextVar("lang", default=DEFAULT)

# Strings asked for in English that have no translation yet (for tests and checks).
missing: set[str] = set()


def get_lang() -> str:
    return _lang.get()


def set_lang(lang: str):
    return _lang.set(lang if lang in LANGS else DEFAULT)


def reset_lang(token) -> None:
    _lang.reset(token)


def choose(cookie: str | None, accept_language: str | None) -> str:
    """The cookie wins; then Accept-Language (nl* is Dutch, anything else English).
    Without either header the app's own language, Dutch, is used."""
    if cookie in LANGS:
        return cookie
    if not accept_language:
        return DEFAULT
    best, best_q = None, -1.0
    for i, part in enumerate(accept_language.split(",")):
        tag, _, params = part.strip().partition(";")
        tag = tag.strip().lower()
        if not tag:
            continue
        q = 1.0
        m = re.search(r"q=([0-9.]+)", params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if q > best_q:
            best, best_q = tag, q
    return "nl" if best and best.startswith("nl") else "en"


def gettext(message: str, /, **values) -> str:
    """The message in the current language, with {name} placeholders filled in."""
    text = message
    if _lang.get() == "en":
        text = EN.get(message)
        if text is None:
            missing.add(message)
            text = message
    if not values:
        return text
    if any(isinstance(v, Markup) for v in values.values()):
        return Markup(escape(text)).format(**values)
    return text.format(**values)


def ngettext(singular: str, plural: str, n: int, /, **values) -> str:
    """Dutch and English share the plural rule: one is singular, the rest plural."""
    return gettext(singular if n == 1 else plural, n=n, **values)


_ = gettext


# Messages that come from elsewhere (model errors, the bridge): translated when known,
# otherwise shown as they are, without counting as missing.
_PATTERNS = [
    (re.compile(r"^(\d+) clips opgehaald$"), "{0} clips fetched"),
    (re.compile(r"^fout: (.*)$"), "error: {0}"),
    (re.compile(r"^fout (\d+): (.*)$"), "error {0}: {1}"),
    (re.compile(r"^geen antwoord \((.*)\)$"), "no answer ({0})"),
]


def translate_known(text: str | None) -> str | None:
    if not text or _lang.get() != "en":
        return text
    if text in EN:
        return EN[text]
    for pattern, english in _PATTERNS:
        m = pattern.match(text)
        if m:
            return english.format(*m.groups())
    prefix, sep, rest = text.partition(": ")
    if sep and prefix and " " not in prefix and rest:
        return f"{prefix}: {translate_known(rest)}"
    return text


# The clip page's still button reads "Beeld toevoegen" / "Add frame"; on a phone only the first
# part shows. Word order differs, so each language gives both parts itself.
STILL_BUTTON = {
    "nl": {"add": ("Beeld", " toevoegen"), "remove": ("Beeld", " weghalen")},
    "en": {"add": ("Add", " frame"), "remove": ("Remove", " frame")},
}

EN: dict[str, str] = {
    "multi-wildcam-app": "multi wildcam app",
    # ---- top bar, drawer, shared -------------------------------------------------------
    "balk": "bar",
    "Hoofdmenu": "Main menu",
    "Tijdlijn": "Timeline",
    "Statistiek": "Statistics",
    "Instellingen": "Settings",
    "{n} in wachtrij": "{n} queued",
    "{n} mislukt": "{n} failed",
    "{name} werkt niet:": "{name} is not working:",
    "Opnieuw proberen": "Try again",
    "Naar instellingen": "Go to settings",
    "Camera's": "Cameras",
    "Sluiten": "Close",
    "Signaal": "Signal",
    "Batterij": "Battery",
    "zon laadt": "charging from the sun",
    "laadt": "charging",
    "Temperatuur": "Temperature",
    "SD vrij": "SD free",
    "{free} van {total}": "{free} of {total}",
    "Laatste check": "Last check",
    "nog niet": "not yet",
    "Volgende check": "Next check",
    "geen controle-uren": "no check hours",
    "Bezig met ophalen": "Fetching",
    "Nu controleren": "Check now",
    "Camera's beheren": "Manage cameras",
    "{n} binnen": "{n} received",
    "Mislukt: {reason}": "Failed: {reason}",
    "Mislukt. De bridge geeft geen antwoord.": "Failed. The bridge does not answer.",
    "Klaar. Nieuwe clips worden geanalyseerd.": "Done. New clips are being analysed.",
    "De bridge antwoordt niet. Probeer het later opnieuw.": "The bridge does not answer. Please try again later.",
    "Annuleren": "Cancel",
    "Doorgaan": "Continue",
    # ---- camera states (bridge.py) ------------------------------------------------------
    "status nog onbekend": "status not known yet",
    "bridge niet bereikbaar": "bridge unreachable",
    "bezig met ophalen": "fetching",
    "ESP niet bereikbaar": "ESP unreachable",
    "laatste check mislukt": "last check failed",
    "in bereik": "in range",
    "{n} min geen signaal": "no signal for {n} min",
    "nog geen signaal": "no signal yet",
    "goed": "good",
    "matig": "fair",
    "zwak": "weak",
    "{n} camera": "{n} camera",
    "{n} camera's": "{n} cameras",
    "{name}: bezig met ophalen": "{name}: fetching",
    "alles in orde": "all good",
    "Camera wekken": "Waking the camera",
    "Verbinden met de camera": "Connecting to the camera",
    "Nieuwe clips zoeken": "Looking for new clips",
    "Wissen op de camera": "Deleting on the camera",
    "Afronden": "Finishing",
    "Bezig": "Working",
    "Clip {i} van {n} ophalen": "Fetching clip {i} of {n}",
    "Foto {i} van {n} ophalen": "Fetching photo {i} of {n}",
    "dit adres geeft geen camerabridge-status": "this address does not give a camera bridge status",
    # results the bridge reports (Dutch from the Pi)
    "niets nieuw": "nothing new",
    "1 clip opgehaald": "1 clip fetched",
    "camera niet bereikbaar": "camera unreachable",
    # ---- dates ------------------------------------------------------------------------------
    "Onbekende datum": "Unknown date",
    "Vandaag": "Today",
    "Gisteren": "Yesterday",
    "maandag": "Monday", "dinsdag": "Tuesday", "woensdag": "Wednesday", "donderdag": "Thursday",
    "vrijdag": "Friday", "zaterdag": "Saturday", "zondag": "Sunday",
    "jan": "Jan", "feb": "Feb", "mrt": "Mar", "apr": "Apr", "mei": "May", "jun": "Jun",
    "jul": "Jul", "aug": "Aug", "sep": "Sep", "okt": "Oct", "nov": "Nov", "dec": "Dec",
    # ---- weather (weather.py) ----------------------------------------------------------------
    "Helder": "Clear",
    "Licht bewolkt": "Mostly clear",
    "Half bewolkt": "Partly cloudy",
    "Bewolkt": "Cloudy",
    "Mist": "Fog",
    "Motregen": "Drizzle",
    "Lichte regen": "Light rain",
    "Regen": "Rain",
    "Zware regen": "Heavy rain",
    "IJzel": "Freezing rain",
    "Lichte sneeuw": "Light snow",
    "Sneeuw": "Snow",
    "Zware sneeuw": "Heavy snow",
    "Buien": "Showers",
    "Zware buien": "Heavy showers",
    "Sneeuwbuien": "Snow showers",
    "Onweer": "Thunderstorm",
    "Onbekend weer": "Unknown weather",
    "Geef twee getallen: breedtegraad en lengtegraad, bijvoorbeeld 52.0, 5.8":
        "Enter two numbers: latitude and longitude, for example 52.0, 5.8",
    "Die coördinaten bestaan niet": "Those coordinates do not exist",
    # ---- date bar (media.py) -----------------------------------------------------------------
    "Naam van de camera": "Camera name",
    "Datum": "Date",
    "Tijd": "Time",
    "Tijd loopt mee met de video": "Time runs with the video",
    "Zon en maan": "Sun and moon",
    "Weertype": "Weather",
    # ---- index ------------------------------------------------------------------------------
    "Clip verwijderd.": "Clip deleted.",
    "Alle dieren": "All animals",
    "Wat staat erop": "What's on it",
    "Zonder dier": "No animal",
    "Beoordeling": "Review",
    "Te beoordelen": "To review",
    "Nog niets bevestigd": "Nothing confirmed yet",
    "Druk op een clip op Klopt, of vul zelf in wat het is.": "Press Correct on a clip, or fill in what it is yourself.",
    "Niets te zien met deze filters": "Nothing to see with these filters",
    "Alle clips tonen": "Show all clips",
    "Twijfel": "Unsure",
    "Bevestigd": "Confirmed",
    "Nog niks gezien": "Nothing seen yet",
    "Zet foto's of video's in de inbox-map, of stuur ze naar {api}.": "Put photos or videos in the inbox folder, or send them to {api}.",
    "video": "video",
    "bezig": "working",
    "mislukt": "failed",
    "bevestigd": "confirmed",
    "twijfel": "unsure",
    "Nieuwer": "Newer",
    "Ouder": "Older",
    "Clip": "Clip",
    # ---- clip page ---------------------------------------------------------------------------
    "Terug": "Back",
    "Afspelen": "Play",
    "Pauzeren": "Pause",
    "Detectie": "Detections",
    "Geluid uit": "Mute",
    "Geluid aan": "Unmute",
    "Volledig scherm": "Full screen",
    "Het model bekijkt de beelden…": "The model is looking at the frames…",
    "Sleep de lijn naar een moment waarop het dier goed in beeld is en voeg een beeld toe.":
        "Drag the line to a moment where the animal is clearly visible and add a frame.",
    "Ga naar {t}": "Go to {t}",
    "nog niet bekeken": "not looked at yet",
    "Beeld op {t} weghalen": "Remove frame at {t}",
    "Beeld op {t}": "Frame at {t}",
    "Mislukte opnieuw": "Retry failed",
    "Clip opnieuw analyseren?": "Analyse the clip again?",
    "Alle modellen lezen alle beelden van deze clip opnieuw.": "Every model reads every frame of this clip again.",
    "Alle modellen lezen alle stills van deze clip opnieuw.": "Every model reads every still of this clip again.",
    "Opnieuw analyseren": "Analyse again",
    "Analyseer opnieuw": "Analyse again",
    "Nog niet bepaald": "Not identified yet",
    "Zelf ingevuld": "Set by you",
    "Zelf ingevuld. Het model zei {name}": "Set by you. The model said {name}",
    "{pct} zeker": "{pct} sure",
    "onzeker": "uncertain",
    "In de wachtrij voor determinatie": "Queued for identification",
    "Goedgekeurd": "Approved",
    "Aanpassen": "Change",
    "Beoordeling": "Review",
    "Klopt": "Correct",
    "Nee": "No",
    "Detecties": "Detections",
    "Wat is het dan?": "What is it then?",
    "Soort": "Species",
    "soort, bijv. vos": "species, e.g. red fox",
    "Opslaan": "Save",
    "Geen dier te zien": "No animal visible",
    "Model volgen": "Follow the model",
    "Bestand": "File",
    "Bron": "Source",
    "Binnengekomen": "Received",
    "Video downloaden": "Download video",
    "Foto downloaden": "Download photo",
    "Met de balk staan de camera, datum en tijd, zon of maan, het weer en de temperatuur onderin het beeld.":
        "The bar puts the camera, date and time, sun or moon, weather and temperature at the bottom of the picture.",
    "Die versie wordt nu gemaakt en duurt even.": "That version is made now, which takes a moment.",
    "Balk wordt in de video gezet…": "Adding the bar to the video…",
    "Zonder balk": "Without bar",
    "Met balk": "With bar",
    "Clip verwijderen?": "Delete this clip?",
    "Het origineel en de stills gaan weg. Dit kan niet ongedaan worden.":
        "The original and its stills will be removed. This cannot be undone.",
    "Verwijderen": "Delete",
    "Clip verwijderen": "Delete clip",
    "wordt bekeken…": "being looked at…",
    "Plaats een beeld om het model te laten kijken.": "Add a frame for the model to look at.",
    "Het model kijkt…": "The model is looking…",
    "Analyse mislukt. Plaats of verwijder een beeld om het opnieuw te proberen.":
        "Analysis failed. Add or remove a frame to try again.",
    "Meer dan {max} beelden kan niet.": "No more than {max} frames.",
    "Beeld weghalen": "Remove frame",
    "Beeld toevoegen": "Add frame",
    "Tijdlijn. Pijltjes verplaatsen de lijn, Enter of S voegt een still toe of haalt hem weg.":
        "Timeline. Arrow keys move the line, Enter or S adds or removes a still.",
    "leeg": "empty",
    # ---- settings -----------------------------------------------------------------------------
    "Taal": "Language",
    "Taal van {app}.": "Language of {app}.",
    "Modellen": "Models",
    "Elk model dat aan staat bekijkt elke still en stemt mee over de soort. Zijn ze het oneens, dan telt de stem van een model met meer gewicht zwaarder.":
        "Every model that is switched on looks at every still and votes on the species. When they disagree, a model with more weight counts for more.",
    "Bewaard.": "Saved.",
    "{n} clip wordt opnieuw beoordeeld; stills die een model al gelezen heeft, leest het niet nog eens.":
        "{n} clip is being judged again; stills a model has already read are not read again.",
    "{n} clips worden opnieuw beoordeeld; stills die een model al gelezen heeft, leest het niet nog eens.":
        "{n} clips are being judged again; stills a model has already read are not read again.",
    "Alle clips zijn beoordeeld met deze keuze.": "All clips have been judged with this choice.",
    "{name} gebruiken": "Use {name}",
    "Gewicht": "Weight",
    "API-sleutel of abonnementstoken": "API key or subscription token",
    "Bewaard: {hint}": "Saved: {hint}",
    "Sleutel wissen": "Remove key",
    "Een API-sleutel van console.anthropic.com, of een token van je Claude-abonnement ({cmd}). Zonder kan dit model niet aan.":
        "An API key from console.anthropic.com, or a token from your Claude subscription ({cmd}). Without one this model cannot be switched on.",
    "Na opslaan worden alle clips opnieuw beoordeeld.": "After saving, all clips are judged again.",
    "Een wildcamera achter een bridge (de ESP32 met de Pi). {app} wekt hem op de controle-uren, haalt nieuwe clips op en wist ze daarna op de camera.":
        "A trail camera behind a bridge (the ESP32 with the Pi). {app} wakes it at the check hours, fetches new clips and then deletes them on the camera.",
    "Camera bewaard.": "Camera saved.",
    "Camera toegevoegd. De eerste controle is op het volgende controle-uur, of druk op de tijdlijn op Nu controleren.":
        "Camera added. The first check is at the next check hour, or press Check now on the timeline.",
    "laatste check {when}": "last check {when}",
    "controle om {hours} uur": "checks at {hours}h",
    "{name} verwijderen?": "Delete {name}?",
    "{app} controleert deze camera dan niet meer. Clips die al binnen zijn blijven staan.":
        "{app} will stop checking this camera. Clips already received are kept.",
    "Bewerken": "Edit",
    "Naam": "Name",
    "Adres van de bridge": "Bridge address",
    "Token": "Token",
    "bewaard; leeg laten om te houden": "saved; leave empty to keep it",
    "als de bridge er een heeft": "if the bridge has one",
    "Controle-uren": "Check hours",
    "Camera brandt zelf datum en logo in het beeld": "Camera burns its own date and logo into the picture",
    "wordt bij de volgende controle ingesteld": "will be set at the next check",
    "Bewaren": "Save",
    "+ Camera toevoegen": "+ Add camera",
    "Tuin": "Garden",
    "Elke controle zet de wifi van de camera kort aan en kost dus wat batterij. Daarom vooral 's nachts.":
        "Each check briefly turns on the camera's wifi, which costs some battery. That is why most are at night.",
    "Toevoegen": "Add",
    "Balk over de beelden": "Date bar on the footage",
    "Een balk onderin elke clip, zoals de camera die zelf inbrandt. Hier staat hij alleen over het beeld; bij downloaden kies je of hij erin komt. Zet de datumstempel op de camera zelf uit, dan heb je alleen deze.":
        "A bar at the bottom of every clip, like the one the camera burns in itself. Here it is only laid over the picture; when downloading you choose whether it is burnt in. Switch off the date stamp on the camera itself and you only have this one.",
    "Balk bewaard.": "Bar saved.",
    "Locatie van de camera": "Camera location",
    "breedtegraad, lengtegraad; voor weer, zon en temperatuur": "latitude, longitude; for weather, sun and temperature",
    "Met een locatie haalt {app} per clip het weer van dat uur op bij Open-Meteo (gratis, zonder account): weertype, dag of nacht en de luchttemperatuur. Alleen de locatie en de datum gaan daarheen. Zonder locatie toont de balk de temperatuur van de camera bij de controle.":
        "With a location, {app} looks up the weather of that hour for each clip at Open-Meteo (free, no account): weather, day or night and the air temperature. Only the location and the date are sent there. Without a location the bar shows the camera's temperature at the check.",
    "Opnieuw beoordelen": "Judge again",
    "{n} clip gaat terug in de wachtrij.": "{n} clip goes back into the queue.",
    "{n} clips gaan terug in de wachtrij.": "{n} clips go back into the queue.",
    "Mislukte lezingen": "Failed readings",
    "Bij {n} clip kon een model een still niet lezen, bijvoorbeeld door een limiet. Dit gebeurt ook vanzelf elk uur.":
        "For {n} clip a model could not read a still, for instance because of a limit. This is also retried every hour.",
    "Bij {n} clips kon een model een still niet lezen, bijvoorbeeld door een limiet. Dit gebeurt ook vanzelf elk uur.":
        "For {n} clips a model could not read a still, for instance because of a limit. This is also retried every hour.",
    "Geen mislukte lezingen.": "No failed readings.",
    "Alles": "All",
    "Elk model leest elke still opnieuw en je beoordelingen van het model vervallen. Met Claude aan kost dit flink van je limiet.":
        "Every model reads every still again and your reviews of the model's verdicts are dropped. With Claude on, this uses a good part of your limit.",
    "Alle clips opnieuw beoordelen?": "Judge all clips again?",
    "Alles opnieuw": "Redo everything",
    "Zet minstens één model aan.": "Switch on at least one model.",
    "Alleen {name} bepaalt het oordeel.": "Only {name} decides the verdict.",
    "Stemverhouding: {shares}.": "Vote shares: {shares}.",
    # model descriptions (classifier.CATALOGUE)
    "Kent ruim 2000 soorten wereldwijd en houdt rekening met wat in Nederland voorkomt. Noemt de soort als het zeker is, anders de groep (marterachtige, muis of rat).":
        "Knows over 2000 species worldwide and takes into account what occurs in the Netherlands. Names the species when it is sure, otherwise the group (mustelid, mouse or rat).",
    "Getraind op Europese wildcamera's, 38 klassen. Grover: alle marters, wezels en bunzingen zijn samen “marterachtige”, alle muizen “klein zoogdier”. Eerste gebruik downloadt 1,2 GB.":
        "Trained on European trail cameras, 38 classes. Coarser: all martens, weasels and polecats are one “mustelid”, all mice “small mammal”. First use downloads 1.2 GB.",
    "Een taalmodel dat naar de still kijkt en de soort noemt, ook waar de andere modellen bij de groep blijven (steenmarter of boommarter). Elke still gaat naar Anthropic. Met een API-sleutel kost dat ongeveer een cent per still; met een token van je Claude-abonnement telt het mee in je limiet en duurt het langer.":
        "A language model that looks at the still and names the species, also where the other models stop at the group (beech marten or pine marten). Every still is sent to Anthropic. With an API key that costs about a cent per still; with a token from your Claude subscription it counts towards your limit and takes longer.",
    # ---- stats --------------------------------------------------------------------------------
    "Wanneer actief": "When active",
    "Soorten": "Species",
    "Nog geen dieren": "No animals yet",
    "Laatste 30 dagen": "Last 30 days",
    "Nog geen data": "No data yet",
    # ---- errors (main.py) -----------------------------------------------------------------------
    "Uploaden staat uit: zet WILDCAM_INGEST_TOKEN": "Uploads are off: set WILDCAM_INGEST_TOKEN",
    "Ongeldig token": "Invalid token",
    "Clip niet gevonden": "Clip not found",
    "Geen filmstrook voor deze clip": "No filmstrip for this clip",
    "Het bestand is er niet meer": "The file is no longer there",
    "De balk kon niet in de video gezet worden": "The bar could not be added to the video",
    "Onbekende beoordeling": "Unknown review",
    "Ongeldige tijden": "Invalid times",
    "Kies hooguit {n} stills": "Choose at most {n} stills",
    "Geef de camera een naam.": "Give the camera a name.",
    "Het adres van de bridge begint met http:// of https://, bijvoorbeeld http://192.168.1.50:8090.":
        "The bridge address starts with http:// or https://, for example http://192.168.1.50:8090.",
    "Controle-uren zijn hele uren, gescheiden door komma's, zoals 0,2,4,22.":
        "Check hours are whole hours, separated by commas, such as 0,2,4,22.",
    "Controle-uren lopen van 0 tot en met 23.": "Check hours run from 0 up to and including 23.",
    "De bridge op {url} antwoordt niet ({reason}). Klopt het adres, en draait de bridge?":
        "The bridge at {url} does not answer ({reason}). Is the address right, and is the bridge running?",
    "Er is al een camera met die naam.": "There is already a camera with that name.",
    "Bewaard, maar de datumstempel kon niet naar de bridge: {error}": "Saved, but the date stamp could not be sent to the bridge: {error}",
    "Onbekende camera": "Unknown camera",
    "Onbekende keuze": "Unknown choice",
    "Dat lijkt geen sleutel van Anthropic: die begint met sk-ant-. Er is niets bewaard.":
        "That does not look like an Anthropic key: those start with sk-ant-. Nothing was saved.",
    "Ongeldig gewicht. Er is niets bewaard.": "Invalid weight. Nothing was saved.",
    "{name} heeft een sleutel nodig. Er is niets bewaard.": "{name} needs a key. Nothing was saved.",
    "Kies minstens één model met een gewicht boven nul. Er is niets bewaard.":
        "Choose at least one model with a weight above zero. Nothing was saved.",
    # ---- model and worker errors (shown as they come, translated when known) ---------------------
    "geen sleutel ingevuld": "no key filled in",
    "kon geen stills uit de video halen": "could not get stills from the video",
    "geen voorspelling ontvangen": "no prediction received",
    "DeepFaune-gewichten passen niet bij de klassenlijst": "DeepFaune weights do not match the class list",
    "Claude Code is niet geïnstalleerd, nodig voor een abonnementstoken": "Claude Code is not installed; it is needed for a subscription token",
    "geen antwoord binnen drie minuten": "no answer within three minutes",
    "Claude Code gaf geen antwoord": "Claude Code gave no answer",
    "geen antwoord": "no answer",
    "de API-sleutel wordt geweigerd": "the API key is refused",
    "te veel verzoeken, later opnieuw proberen": "too many requests, try again later",
    "geen verbinding met de API": "no connection to the API",
}
