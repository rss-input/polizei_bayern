#!/usr/bin/env python3
"""Build an RSS 2.0 feed from Bavarian Police press releases.

The project deliberately uses only Python's standard library.  The source site
has two relevant content formats: ordinary HTML articles and collection pages
whose accordion bodies are base64 encoded in ``bp-item`` attributes.  Both are
handled here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


INDEX_URL = "https://www.polizei.bayern.de/aktuelles/pressemitteilungen/index.html"
BASE_URL = "https://www.polizei.bayern.de"
DEFAULT_FEED_URL = "https://rss-input.github.io/polizei_bayern/feed.xml"
USER_AGENT = (
    "polizei-bayern-rss/1.0 "
    "(+https://github.com/rss-input/polizei_bayern; contact via repository)"
)
ATOM_NS = "http://www.w3.org/2005/Atom"
CONTENT_NS = "http://purl.org/rss/1.0/modules/content/"
BERLIN_TZ = ZoneInfo("Europe/Berlin")
ET.register_namespace("atom", ATOM_NS)
ET.register_namespace("content", CONTENT_NS)


COLLECTION_RE = re.compile(
    r"(?:medieninfo(?:rmation)?|presse(?:mitteilungen|meldungen|bericht)|"
    r"polizeimeldungen|tagesmeldungen|sammelmeldung)",
    re.IGNORECASE,
)

STRUCTURAL_HEADING_RE = re.compile(
    r"^(?:"
    r"zeugen(?:aufruf|hinweise| gesucht)?|hinweis(?:e)?|warnhinweis(?:e)?|"
    r"personenbeschreibung|(?:der|die|eine|ein) .* beschrieben|"
    r"polizei(?:inspektion|station|präsidium)|kriminalpolizei(?:inspektion)?|"
    r"verkehrspolizei(?:inspektion)?|autobahnpolizei(?:station)?|"
    r"bereich\b|weitere zeugenaufrufe|mobile wache|termine|örtlichkeiten|"
    r"ab hier neu|in abstimmung mit|medienkontakt|sie wollen zur|"
    r"landkreis\b|stadtgebiet\b|kriminalitätsgeschehen|verkehrsgeschehen"
    r")",
    re.IGNORECASE,
)

CASE_HEADING_HINT_RE = re.compile(
    r"(?:festnahme|ermittlung|angriff|attack|auseinandersetzung|bedroh|"
    r"greift\b|körperverletz|töt|mord|totschlag|raub|überfall|schuss|stich|messer|"
    r"sexual|vergewalt|missbrauch|belästig|exhibition|pornograf|"
    r"drogen|rauschgift|betäubungsmittel|cannabis|kokain|heroin|amphetamin|"
    r"ecstasy|mdma|handel|bande|organisiert|mafia|schleus|menschenhandel|"
    r"geldwäsche|brandstift|widerstand|polizist.*angegriffen)",
    re.IGNORECASE,
)

CATEGORY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "Gewaltdelikt": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\btötungsdelikt", r"\bversuchte?\s+tötung", r"\bmord(?:es)?\b",
            r"\btotschlag", r"\braub", r"\bberaubt", r"\büberfall", r"\bkörperverletzung",
            r"\btätliche[rsn]?\s+angriff", r"\bangegriffen", r"\battackiert",
            r"\bgriff\b.{0,100}\ban\b", r"\bgewürgt", r"\bniedergestochen",
            r"\bstach\b", r"\bmesserangriff", r"\bschussabgabe", r"\bschüsse?\b",
            r"\bwaffe[n]?\s+(?:bedroht|eingesetzt)", r"\bbrandstiftung",
        )
    ),
    "Sexualdelikt": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bsexual(?:delikt|straftat|isierte|motiviert|handlung|übergriff)",
            r"\bvergewaltig", r"\bsexuelle?\s+nötigung", r"\bsexuelle?\s+missbrauch",
            r"\bsexuell\s+belästig", r"\bunsittlich\s+berührt", r"\bexhibitionis",
            r"\bkinderpornograf", r"\bjugendpornograf", r"\bpornografische[nr]?\s+inhalt",
        )
    ),
    "Drogendelikt": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\bbetäubungsmittel", r"\brauschgift", r"\bdrogen?\b", r"\bdrogenhandel",
            r"\bdealer", r"\bcannabis", r"\bmarihuana", r"\bhaschisch", r"\bkokain",
            r"\bheroin", r"\bcrack\b", r"\bmethamphetamin", r"\bamphetamin",
            r"\becstasy", r"\bmdma\b", r"\blsd\b", r"\bopium", r"\bbtmg\b",
        )
    ),
    "Organisierte Kriminalität": tuple(
        re.compile(pattern, re.IGNORECASE)
        for pattern in (
            r"\borganisierte[rsn]?\s+(?:kriminalität|betrug|callcenterbetrug)",
            r"\borganisiert(?:e|er|en)?\s+callcenter", r"\bbandenmäßig", r"\btäterbande",
            r"\bdrogenbande", r"\bschleuserbande", r"\bkriminelle[nr]?\s+vereinigung",
            r"\bmafia", r"\bmenschenhandel", r"\bzwangsprostitution", r"\bgeldwäsche",
            r"\brockergruppierung", r"\bclankriminalität", r"\borganisierte kriminalität",
        )
    ),
}

DELIBERATE_VIOLENCE_RE = re.compile(
    r"(?:raub|überfall|angriff|attack|geschlagen|getreten|gewürgt|gestochen|"
    r"schuss|waffe|brandstiftung|mord|totschlag)",
    re.IGNORECASE,
)
TRAFFIC_NEGLIGENCE_RE = re.compile(
    r"(?:verkehrsunfall|unfallgeschehen|fahrlässige[rsn]?\s+"
    r"(?:körperverletzung|tötung))",
    re.IGNORECASE,
)
STATISTICAL_SUMMARY_RE = re.compile(
    r"(?:\bbilanz\b|\banzahl\b|\bim vorjahr\b|\bim jahr 20\d\d\b|\(20\d\d\s*:)",
    re.IGNORECASE,
)
INCIDENT_TIME_RE = re.compile(
    r"(?:\b(?:montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
    r"gestern|vorgestern)\b|\b\d{1,2}\.\d{1,2}\.20\d\d\b|\bgegen\s+\d{1,2}[:.]\d{2}\s+uhr)",
    re.IGNORECASE,
)
STATISTICAL_SECTION_TITLES = {
    "körperverletzungsdelikte",
    "diebstahlsdelikte",
    "gewalt gegen polizeibeamte",
    "sexualdelikte",
    "cannabiskonsum",
    "sachbeschädigungen",
    "gewahrsam und platzverweise",
    "fund und verlust",
    "strassenverkehr",
}

DATE_RANGE_RE = re.compile(
    r"\b(\d{1,2})\.\s*[-–]\s*(\d{1,2})\.(\d{1,2})\.(20\d{2})\b"
)
NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b")
TEXT_DATE_RE = re.compile(
    r"\b(\d{1,2})\.\s*"
    r"(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|"
    r"November|Dezember)(?:\s+(20\d{2}))?\b",
    re.IGNORECASE,
)
RELATIVE_DATE_RE = re.compile(
    r"\b(?:in der Nacht (?:von )?"
    r"(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)"
    r"(?: auf (?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag))?"
    r"|am (?:vergangenen |gestrigen )?"
    r"(?:Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)"
    r"(?:morgen|vormittag|mittag|nachmittag|abend|nacht)?"
    r"|kurz nach Mitternacht)\b",
    re.IGNORECASE,
)
LOCATION_LEAD_RE = re.compile(
    r"^(.{2,120}?)\.\s+"
    r"(?=(?:\(\d{1,5}\)\s*)?"
    r"(?:Am|Bei|In|Im|Kurz|Gegen|Nach|Ein|Eine|Aus|Wie|Der|Die|Das|Zu|Vor|Seit)\b)"
)
CITY_AREA_RE = re.compile(
    r"\b(?:im|in dem) Stadtgebiet(?: von)?\s+"
    r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)?)\b"
)
DETAIL_LOCATION_RE = re.compile(
    r"\b(?:im Bereich der|in einer Wohnung in der|in der|an der|am|beim|vor dem)\s+"
    r"((?:[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+\s+){0,3}"
    r"(?:Straße|Strasse|Platz|Weg|Allee|Markt|Bahnhof|"
    r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+(?:straße|strasse|platz|weg|allee|markt|bahnhof)))\b",
    re.IGNORECASE,
)
BOILERPLATE_RE = re.compile(
    r"^(?:Erstellt durch:|Medienkontakt:|Herausgeber:|Rückfragen bitte an:)",
    re.IGNORECASE,
)
CASE_TRANSITION_RE = re.compile(
    r"^(?:Unabhängig davon|In einem weiteren Fall|Ein weiterer Fall|"
    r"Davon unabhängig|Außerdem nahmen .{0,80} einen weiteren)",
    re.IGNORECASE,
)
VICTIM_SENTENCE_RE = re.compile(
    r"\b(?:Opfer|Geschädigte[nr]?|Angegriffene[nr]?|Betroffene[nr]?|"
    r"Aufgegriffene[nr]?)\b|"
    r"\bwurde[n]?\b.{0,180}\b(?:verletzt|angegriffen|beraubt|bedroht|"
    r"belästigt|missbraucht|geschlagen|gestochen|zu Boden gestoßen|"
    r"zur Herausgabe .{0,50} aufgefordert)\b|"
    r"\b(?:bedrängt|missbraucht|belästigt|angegriffen|beraubt|bedroht|"
    r"verletzt)\s+worden\b|"
    r"\b(?:eine[nr]?|einen weiteren|weitere)\s+\d{1,3}-jährig\w*.{0,180}"
    r"\b(?:missbraucht|bedrängt|belästigt|angegriffen|beraubt|bedroht)\b|"
    r"\b(?:schlug|trat|stach|besprühte|sprühte|berührte)\b.{0,180}"
    r"\b(?:\d{1,3}-Jährig\w*|Mann|Frau|Person|Opfer|Geschädigt\w*)\b|"
    r"\b\d{1,3}-Jährig\w*.{0,180}\b(?:unsittlich berührte|zu Boden stürzte|"
    r"nicht mehr ansprechbar|in ein Krankenhaus)\b|"
    r"\b\d{1,3}-jährig\w*.{0,160}\bbislang unbekannten Täter\b|"
    r"\bExhibitionist\w*.{0,120}\beiner Frau\b|"
    r"\b(?:ging|lief)\b.{0,100}\bauf (?:den|die|einen?)\s+Polizeibeamt\w*\b",
    re.IGNORECASE,
)
SUSPECT_SENTENCE_RE = re.compile(
    r"\b(?:Tatverdächtig\w*|Beschuldigt\w*|Täter\w*)\b|"
    r"\bsteht im Verdacht\b|\bermittelt\b.{0,80}\bgegen\b",
    re.IGNORECASE,
)
AGE_SENTENCE_RE = re.compile(r"\b\d{1,3}-jährig\w*\b", re.IGNORECASE)
ACTOR_PHRASE_RE = re.compile(
    r"\bvon (?:einem|einer) \d{1,3}-jährig\w*\s+[A-ZÄÖÜ][a-zäöüß-]+\b|"
    r"\b\d{1,3}-Jährig\w*.{0,120}\b(?:welcher|der)\b.{0,160}"
    r"\b(?:schlug|trat|stach|berührte|bedrohte|beraubte)\b",
    re.IGNORECASE,
)
RESULT_SENTENCE_RE = re.compile(
    r"\b(?:festgenommen|vorläufig festgenommen|Haftbefehl|Untersuchungshaft\w*|"
    r"auf freien Fuß|entlassen|Heimreise|(?:Täter|Tatverdächtig\w*)"
    r"\s+(?:(?:ist|sind|war|blieb(?:en)?)\s+)?flüchtig|flüchtete|"
    r"Fahndung.{0,80}(?:erfolglos|ohne Erfolg)|"
    r"sichergestellt|beschlagnahmt|"
    r"überstellt|Justizvollzugsanstalt|Krankenhaus|Fachklinik|"
    r"Blutentnahme|Weiterfahrt untersagt|Strafverfahren|Strafanzeige|"
    r"Ermittlungen? (?:aufgenommen|eingeleitet)|ermittelt nun)\b",
    re.IGNORECASE,
)
SUSPECT_DESCRIPTION_RE = re.compile(
    r"\b(?:Personenbeschreibung|beschrieben|Beschreibung des Täters|"
    r"\d{3}\s*cm|Haare|Bekleidung|Jacke|Hose|Pullover|T-Shirt|Rucksack)\b",
    re.IGNORECASE,
)
SPECIAL_SENTENCE_RE = re.compile(
    r"\b(?:Schusswaffe|Messer|Tierabwehrspray|Pfefferspray|Teleskopschlagstock|"
    r"DEIG|Distanzelektroimpulsgerät|Waffe|Drogen angeboten|Drogen bekommen|"
    r"Betäubungsmittel.{0,100}(?:versteckt|abgegeben)|versteckt|"
    r"Kilogramm|Gramm|fünfstelligen|sechsstelligen|Beute|Bargeldforderung|"
    r"zur Herausgabe|sexuell bedrängt|sexuell missbraucht|unsittlich berührt\w*|"
    r"schlug|trat|stach|wollte sich .{0,40} nicht äußern)\b",
    re.IGNORECASE,
)
NATIONALITY_DETAIL_RE = re.compile(
    r"\b(?:Staatsangehörig\w*|deutsch\w*|syrisch\w*|syrer\w*|italienisch\w*|"
    r"kosovar\w*|somali\w*|irak\w*|rumän\w*|griech\w*|senegales\w*|"
    r"liby\w*|schweizer\w*|türk\w*|afghan\w*|ukrain\w*|poln\w*|"
    r"tschech\w*|österreich\w*|französ\w*|franzose\w*|slowak\w*|"
    r"kambodschan\w*|amerikan\w*|brit\w*|niederländ\w*|belg\w*|"
    r"bulgar\w*|kroat\w*|serb\w*|bosni\w*|ungar\w*|russ\w*|"
    r"georg\w*|moldau\w*|alban\w*|mazedon\w*|pakistan\w*|"
    r"indisch\w*|inder\w*|iran\w*|nigerian\w*|alger\w*|marokkan\w*|"
    r"tunes\w*|eritre\w*|äthiop\w*|gamb\w*|ghana\w*|chines\w*|"
    r"vietnames\w*|thai\w*|span\w*|portugies\w*|schwed\w*|"
    r"norweg\w*|dän\w*|finn\w*|irisch\w*)\b",
    re.IGNORECASE,
)
MONTH_NUMBERS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}
WEEKDAY_NUMBERS = {
    "montag": 0,
    "dienstag": 1,
    "mittwoch": 2,
    "donnerstag": 3,
    "freitag": 4,
    "samstag": 5,
    "sonntag": 6,
}
NATIONALITY_LABELS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), label)
    for pattern, label in (
        (r"\bdeutsch\w*\b", "deutsch"),
        (r"\b(?:syrisch\w*|Syrer\w*)\b", "syrisch"),
        (r"\bkosovar\w*\b", "kosovarisch"),
        (r"\bsomali\w*\b", "somalisch"),
        (r"\birak\w*\b", "irakisch"),
        (r"\bgriech\w*\b", "griechisch"),
        (r"\bschweizer\w*\b", "schweizerisch"),
        (r"\bitalien\w*\b", "italienisch"),
        (r"\brumän\w*\b", "rumänisch"),
        (r"\bsenegales\w*\b", "senegalesisch"),
        (r"\bliby\w*\b", "libysch"),
        (r"\btürk\w*\b", "türkisch"),
        (r"\bafghan\w*\b", "afghanisch"),
        (r"\bukrain\w*\b", "ukrainisch"),
        (r"\bpoln\w*\b", "polnisch"),
        (r"\btschech\w*\b", "tschechisch"),
        (r"\bösterreich\w*\b", "österreichisch"),
        (r"\b(?:französ\w*|Franzose\w*)\b", "französisch"),
        (r"\bslowak\w*\b", "slowakisch"),
        (r"\bkambodschan\w*\b", "kambodschanisch"),
        (r"\bamerikan\w*\b", "amerikanisch"),
        (r"\bbrit\w*\b", "britisch"),
        (r"\bniederländ\w*\b", "niederländisch"),
        (r"\bbelg\w*\b", "belgisch"),
        (r"\bbulgar\w*\b", "bulgarisch"),
        (r"\bkroat\w*\b", "kroatisch"),
        (r"\bserb\w*\b", "serbisch"),
        (r"\bbosni\w*\b", "bosnisch"),
        (r"\bungar\w*\b", "ungarisch"),
        (r"\bruss\w*\b", "russisch"),
        (r"\bgeorg\w*\b", "georgisch"),
        (r"\bmoldau\w*\b", "moldauisch"),
        (r"\balban\w*\b", "albanisch"),
        (r"\bmazedon\w*\b", "mazedonisch"),
        (r"\bpakistan\w*\b", "pakistanisch"),
        (r"\b(?:indisch\w*|Inder\w*)\b", "indisch"),
        (r"\biran\w*\b", "iranisch"),
        (r"\bnigerian\w*\b", "nigerianisch"),
        (r"\balger\w*\b", "algerisch"),
        (r"\bmarokkan\w*\b", "marokkanisch"),
        (r"\btunes\w*\b", "tunesisch"),
        (r"\beritre\w*\b", "eritreisch"),
        (r"\bäthiop\w*\b", "äthiopisch"),
        (r"\bgamb\w*\b", "gambisch"),
        (r"\bghana\w*\b", "ghanaisch"),
        (r"\bchines\w*\b", "chinesisch"),
        (r"\bvietnames\w*\b", "vietnamesisch"),
        (r"\bthai\w*\b", "thailändisch"),
    )
)


def normalize_space(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[\t\r\f\v ]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    return value.strip()


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", value.casefold()).strip()


def source_id_from_url(url: str) -> str:
    match = re.search(r"/pressemitteilungen/(\d+)/", url)
    if not match:
        raise ValueError(f"Keine Meldungs-ID in URL: {url}")
    return match.group(1)


def fetch_text(url: str, *, attempts: int = 3, timeout: int = 35) -> str:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            )
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="strict")
        except (HTTPError, URLError, TimeoutError, UnicodeError, OSError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(1.5 * (2 ** (attempt - 1)))
    raise RuntimeError(f"Abruf fehlgeschlagen ({url}): {last_error}")


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    def descendants(self, tag: str | None = None) -> Iterator[Node]:
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.descendants(tag)

    def text(self) -> str:
        pieces: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                pieces.append(child)
            elif child.tag == "br":
                pieces.append("\n")
            else:
                pieces.append(child.text())
        return normalize_space("".join(pieces))

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()

    def has_ancestor_class(self, name: str) -> bool:
        current = self.parent
        while current is not None:
            if current.has_class(name):
                return True
            current = current.parent
        return False


class TreeParser(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack: list[Node] = [self.root]
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = Node(tag, {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag in {"script", "style"}:
            self.skip_depth += 1
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.stack[-1].children.append(data)


@dataclass(frozen=True)
class Event:
    kind: str
    text: str = ""
    level: int = 0
    strong_only: bool = False
    leading_strong: str = ""


@dataclass(frozen=True)
class ParsedCase:
    title: str
    paragraphs: tuple[str, ...]
    categories: tuple[str, ...]
    ordinal: int


@dataclass(frozen=True)
class ItemSummary:
    pm: str
    tatdatum: str
    tatort: str
    delikt: str
    opfer: str
    tatverdaechtige: str
    ergebnis: str
    besonderheiten: str


def parse_html(value: str) -> Node:
    parser = TreeParser()
    parser.feed(value)
    parser.close()
    return parser.root


def parse_index(html_text: str) -> list[dict[str, object]]:
    match = re.search(
        r"window\.montagedata\s*=\s*(\[.*?\])\s*;\s*window\.filterdata",
        html_text,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("window.montagedata wurde auf der Übersichtsseite nicht gefunden")
    raw_entries = json.loads(match.group(1))
    entries: list[dict[str, object]] = []
    for raw in raw_entries:
        href = str(raw.get("href", ""))
        if not re.search(r"/pressemitteilungen/\d+/", href):
            continue
        organization = raw.get("organization") or {}
        entry = {
            "source_id": source_id_from_url(href),
            "url": urljoin(BASE_URL, href),
            "title": normalize_space(str(raw.get("title", ""))),
            "teaser": normalize_space(str(raw.get("teaser", ""))),
            "published_ts": int(raw.get("date", 0)) // 1000,
            "organization": normalize_space(str(organization.get("name", ""))),
        }
        entries.append(entry)
    if not entries:
        raise ValueError("Die Übersichtsseite enthielt keine Meldungen")
    entries.sort(key=lambda item: (-int(item["published_ts"]), str(item["source_id"])))
    return entries


def _leading_strong(node: Node) -> str:
    for child in node.children:
        if isinstance(child, str):
            if child.strip():
                return ""
            continue
        if child.tag == "strong":
            return child.text()
        if child.tag != "br":
            return ""
    return ""


def _strong_only(node: Node) -> bool:
    strong_text = " ".join(item.text() for item in node.descendants("strong"))
    return bool(strong_text) and normalize_key(strong_text) == normalize_key(node.text())


def events_from_node(root: Node) -> list[Event]:
    events: list[Event] = []

    def walk(node: Node) -> None:
        for child in node.children:
            if not isinstance(child, Node):
                continue
            if child.tag == "hr":
                events.append(Event("separator"))
            elif child.tag in {"h1", "h2", "h3", "h4", "h5"}:
                text = child.text()
                if text:
                    events.append(Event("heading", text, int(child.tag[1])))
            elif child.tag in {"p", "li"}:
                text = child.text()
                if not text:
                    continue
                if re.fullmatch(r"[-–—_\s]{1,80}", text):
                    events.append(Event("separator"))
                else:
                    events.append(
                        Event(
                            "paragraph",
                            text,
                            strong_only=_strong_only(child),
                            leading_strong=_leading_strong(child),
                        )
                    )
            else:
                walk(child)

    walk(root)
    return events


def _decoded_accordion_roots(root: Node) -> list[tuple[str, Node]]:
    result: list[tuple[str, Node]] = []
    for item in root.descendants("bp-item"):
        encoded_json = item.attrs.get("json")
        if not encoded_json:
            continue
        try:
            payload = json.loads(encoded_json)
            encoded_html = payload.get("data", {}).get("iwe2")
            if not encoded_html:
                continue
            decoded = base64.b64decode(encoded_html).decode("utf-8")
            result.append((normalize_space(str(payload.get("title", ""))), parse_html(decoded)))
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError(f"Accordion-Inhalt konnte nicht dekodiert werden: {exc}") from exc
    return result


def detail_content_roots(html_text: str) -> tuple[str, str, list[tuple[str, Node]]]:
    root = parse_html(html_text)
    title = ""
    for headline in root.descendants("bp-headline"):
        if headline.attrs.get("title"):
            title = normalize_space(headline.attrs["title"])
            break

    page_date = ""
    for section in root.descendants("section"):
        if section.has_class("bp-page-date"):
            page_date = section.text()
            break

    roots: list[tuple[str, Node]] = []
    for node in root.descendants("div"):
        if node.has_class("bp-iwe2") and not node.has_ancestor_class("bp-presse"):
            roots.append(("", node))
    roots.extend(_decoded_accordion_roots(root))

    if not roots:
        fallback = [node for node in root.descendants("div") if node.has_class("bp-iwe2")]
        if len(fallback) > 1:
            fallback = fallback[1:]
        roots.extend(("", node) for node in fallback)
    return title, page_date, roots


def looks_like_case_heading(event: Event) -> bool:
    text = normalize_space(event.text)
    structural_text = text.strip("-–—_ ")
    if not text or len(text) > 240 or STRUCTURAL_HEADING_RE.search(structural_text):
        return False
    if re.match(r"^\d{1,5}\s*[.)]\s*\S", text):
        return True
    if event.kind == "heading" and event.level >= 3 and len(text.split()) >= 2:
        return True
    if event.strong_only:
        return True
    return False


def _looks_like_location_lead(event: Event) -> bool:
    lead = normalize_space(event.leading_strong).strip(" –—-")
    if not lead or len(lead) > 120 or STRUCTURAL_HEADING_RE.search(lead):
        return False
    letters = "".join(character for character in lead if character.isalpha())
    return bool(letters) and letters == letters.upper()


def _split_on_separators(events: Sequence[Event]) -> list[list[Event]]:
    chunks: list[list[Event]] = []
    current: list[Event] = []
    for event in events:
        if event.kind == "separator":
            if any(item.text for item in current):
                chunks.append(current)
            current = []
        else:
            current.append(event)
    if any(item.text for item in current):
        chunks.append(current)
    return chunks


def _split_on_headings(events: Sequence[Event]) -> list[list[Event]]:
    chunks: list[list[Event]] = []
    current: list[Event] = []
    for event in events:
        starts_case = (
            looks_like_case_heading(event)
            or (event.kind == "heading" and event.level in {2, 3})
            or _looks_like_location_lead(event)
        )
        has_body = any(item.kind == "paragraph" and item.text for item in current)
        if starts_case and has_body:
            chunks.append(current)
            current = [event]
        else:
            current.append(event)
    if any(item.text for item in current):
        chunks.append(current)
    return chunks


def _split_on_case_transitions(events: Sequence[Event]) -> list[list[Event]]:
    chunks: list[list[Event]] = []
    current: list[Event] = []
    for event in events:
        if (
            event.kind == "paragraph"
            and CASE_TRANSITION_RE.match(event.text)
            and any(item.kind == "paragraph" and item.text for item in current)
        ):
            chunks.append(current)
            current = [event]
        else:
            current.append(event)
    if any(item.text for item in current):
        chunks.append(current)
    return chunks


def classify(text: str) -> tuple[str, ...]:
    categories = [
        category
        for category, patterns in CATEGORY_PATTERNS.items()
        if any(pattern.search(text) for pattern in patterns)
    ]
    if (
        "Gewaltdelikt" in categories
        and TRAFFIC_NEGLIGENCE_RE.search(text)
        and not DELIBERATE_VIOLENCE_RE.search(text)
    ):
        categories.remove("Gewaltdelikt")
    if (
        "Gewaltdelikt" in categories
        and "Organisierte Kriminalität" in categories
        and re.search(r"callcenter|falsche[rsn]?\s+polizei", text, re.IGNORECASE)
        and not re.search(
            r"körperverletz|angegriffen|attackiert|geschlagen|getreten|gewürgt|"
            r"gestochen|schussverletz|verletzt(?:e|en)?\s+(?:das|die|den)\s+opfer",
            text,
            re.IGNORECASE,
        )
    ):
        categories.remove("Gewaltdelikt")
    return tuple(categories)


def _short_exact_title(text: str, limit: int = 190) -> str:
    text = normalize_space(text).replace("\n", " ")
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit - 1)
    return text[: cut if cut > 40 else limit - 1].rstrip(" ,;:-") + "…"


def _case_from_events(
    events: Sequence[Event],
    *,
    source_title: str,
    ordinal: int,
    collection: bool,
) -> ParsedCase | None:
    paragraphs = tuple(event.text for event in events if event.text)
    if not paragraphs:
        return None
    all_text = "\n\n".join(paragraphs)

    title = source_title
    if collection:
        for event in events:
            if looks_like_case_heading(event):
                title = event.text
                break
        else:
            first_paragraph = next(
                (
                    event
                    for event in events
                    if event.kind == "paragraph"
                    and not STRUCTURAL_HEADING_RE.search(event.text.strip("-–—_ "))
                    and not re.fullmatch(r"[-–—_\s]+", event.text)
                ),
                None,
            )
            if first_paragraph is not None:
                title = re.split(
                    r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„])", first_paragraph.text, maxsplit=1
                )[0]

    if (
        collection
        and STATISTICAL_SUMMARY_RE.search(all_text)
        and (
            not INCIDENT_TIME_RE.search(all_text)
            or normalize_key(title) in STATISTICAL_SECTION_TITLES
        )
    ):
        return None
    categories = classify(all_text)
    if not categories:
        return None
    return ParsedCase(_short_exact_title(title), paragraphs, categories, ordinal)


def parse_relevant_cases(html_text: str, *, fallback_title: str) -> list[ParsedCase]:
    page_title, _page_date, roots = detail_content_roots(html_text)
    source_title = page_title or fallback_title
    if not roots:
        raise ValueError("Kein Meldungstext gefunden")

    collection = bool(COLLECTION_RE.search(source_title)) or len(roots) > 1
    all_root_events = [(group, events_from_node(root)) for group, root in roots]
    if not any(events for _group, events in all_root_events):
        raise ValueError("Der Meldungstext enthielt keine auswertbaren Absätze")

    cases: list[ParsedCase] = []
    ordinal = 0
    if not collection:
        combined: list[Event] = []
        for _group, events in all_root_events:
            combined.extend(events)
        transition_chunks = _split_on_case_transitions(combined)
        result: list[ParsedCase] = []
        for ordinal, chunk in enumerate(transition_chunks):
            case = _case_from_events(
                chunk,
                source_title=source_title,
                ordinal=ordinal,
                collection=ordinal > 0,
            )
            if case:
                result.append(case)
        return result

    for _group, events in all_root_events:
        for separator_chunk in _split_on_separators(events):
            for chunk in _split_on_headings(separator_chunk):
                case = _case_from_events(
                    chunk,
                    source_title=source_title,
                    ordinal=ordinal,
                    collection=True,
                )
                ordinal += 1
                if case:
                    cases.append(case)
    return cases


def _case_key(case: ParsedCase) -> str:
    number = re.match(r"^(\d{1,6})\s*[.)]", case.title)
    if number:
        return number.group(1)
    material = normalize_key(case.title) + "\n" + normalize_key("\n".join(case.paragraphs[:2]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def make_item(entry: dict[str, object], case: ParsedCase) -> dict[str, object]:
    source_id = str(entry["source_id"])
    case_key = _case_key(case)
    body = list(case.paragraphs)
    content_material = normalize_key(case.title) + "\n" + normalize_key("\n".join(body))
    content_hash = hashlib.sha256(content_material.encode("utf-8")).hexdigest()
    return {
        "guid": f"urn:polizei-bayern:{source_id}:{case_key}",
        "source_id": source_id,
        "case_key": case_key,
        "case_ordinal": case.ordinal,
        "title": case.title,
        "link": str(entry["url"]),
        "published_ts": int(entry["published_ts"]),
        "organization": str(entry["organization"]),
        "source_title": str(entry["title"]),
        "categories": list(case.categories),
        "body": body,
        "content_hash": content_hash,
    }


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def deduplicate_and_sort(items: Iterable[dict[str, object]], limit: int) -> list[dict[str, object]]:
    by_guid: dict[str, dict[str, object]] = {}
    for item in items:
        by_guid[str(item["guid"])] = item
    ordered = sorted(
        by_guid.values(),
        key=lambda item: (
            -int(item["published_ts"]),
            int(item.get("case_ordinal", 0)),
            str(item["guid"]),
        ),
    )
    result: list[dict[str, object]] = []
    seen_content: set[str] = set()
    for item in ordered:
        content_hash = str(item.get("content_hash", ""))
        if content_hash and content_hash in seen_content:
            continue
        if content_hash:
            seen_content.add(content_hash)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _trim_at_word(value: str, limit: int) -> str:
    value = normalize_space(value).replace("\n", " ")
    if len(value) <= limit:
        return value
    cut = value.rfind(" ", 0, limit - 1)
    return value[: cut if cut > limit // 2 else limit - 1].rstrip(" ,;:-") + "…"


def _summary_paragraphs(item: dict[str, object]) -> list[str]:
    title_key = normalize_key(str(item.get("title", "")))
    paragraphs: list[str] = []
    for raw in item.get("body", []):
        paragraph = normalize_space(str(raw))
        if not paragraph or BOILERPLATE_RE.match(paragraph):
            continue
        if normalize_key(paragraph) == title_key:
            continue
        paragraphs.append(paragraph)
    return paragraphs


def _sentences(paragraphs: Sequence[str]) -> list[str]:
    result: list[str] = []
    abbreviations = re.compile(r"\b(?:LKR|bzw|ca|Nr|Dr|u\. a|z\. B)\.", re.IGNORECASE)
    for paragraph in paragraphs:
        protected = abbreviations.sub(lambda match: match.group(0).replace(".", "\u2024"), paragraph)
        protected = re.sub(
            r"\b(\d{1,2})\.(?=\s+(?:Januar|Februar|März|April|Mai|Juni|"
            r"Juli|August|September|Oktober|November|Dezember)\b)",
            lambda match: match.group(1) + "\u2024",
            protected,
            flags=re.IGNORECASE,
        )
        chunks = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„])", protected)
        result.extend(normalize_space(chunk.replace("\u2024", ".")) for chunk in chunks if chunk)
    return result


def _short_date(value: date) -> str:
    return value.strftime("%d.%m.%y")


def _most_recent_weekday(name: str, *, published: date, force_previous: bool = False) -> date:
    wanted = WEEKDAY_NUMBERS[name.casefold()]
    days_back = (published.weekday() - wanted) % 7
    if force_previous and days_back == 0:
        days_back = 7
    return published - timedelta(days=days_back)


def _format_tatdatum(text: str, *, published: date) -> str | None:
    ranges = list(DATE_RANGE_RE.finditer(text))
    occupied = [range_match.span() for range_match in ranges]
    numeric = [
        match
        for match in NUMERIC_DATE_RE.finditer(text)
        if not any(start <= match.start() and match.end() <= end for start, end in occupied)
    ]
    if numeric:
        day, month, year = (int(value) for value in numeric[0].groups())
        result = f"{day:02d}.{month:02d}.{year % 100:02d}"
        if re.search(r"\bzurückliegend\b", text[numeric[0].end():], re.IGNORECASE):
            result += " und zuvor"
        return result
    if ranges:
        first_day, last_day, month, year = (int(value) for value in ranges[0].groups())
        return f"{first_day:02d}.–{last_day:02d}.{month:02d}.{year % 100:02d}"
    if match := TEXT_DATE_RE.search(text):
        day = int(match.group(1))
        month = MONTH_NUMBERS[match.group(2).casefold()]
        year = int(match.group(3)) if match.group(3) else published.year
        return f"{day:02d}.{month:02d}.{year % 100:02d}"

    night = re.search(
        r"in der Nacht von (Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)"
        r" auf (Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)",
        text,
        re.IGNORECASE,
    )
    if night:
        end = _most_recent_weekday(night.group(2), published=published)
        start = end - timedelta(days=1)
        return f"{start:%d}.–{end:%d.%m.%y}"

    weekday = re.search(
        r"\b(?:(vergangenen|gestrigen)\s+)?"
        r"(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag)"
        r"(?:morgen|vormittag|mittag|nachmittag|abend|nacht)?\b",
        text,
        re.IGNORECASE,
    )
    if weekday:
        resolved = _most_recent_weekday(
            weekday.group(2),
            published=published,
            force_previous=bool(weekday.group(1) and weekday.group(1).casefold() == "vergangenen"),
        )
        return _short_date(resolved)
    if re.search(r"\bvorgestern\b", text, re.IGNORECASE):
        return _short_date(published - timedelta(days=2))
    if re.search(r"\bgestern\b", text, re.IGNORECASE):
        return _short_date(published - timedelta(days=1))
    if match := RELATIVE_DATE_RE.search(text):
        return normalize_space(match.group(0))
    return None


def _format_location(value: str) -> str:
    value = normalize_space(value).strip(" –—-")
    letters = "".join(character for character in value if character.isalpha())
    value = value.title() if letters and letters == letters.upper() else value
    replacements = {
        "Unteren": "Untere",
        "Oberen": "Obere",
        "Vorderen": "Vordere",
        "Hinteren": "Hintere",
        "Äußeren": "Äußere",
        "Inneren": "Innere",
    }
    for source, target in replacements.items():
        value = re.sub(
            rf"\b{source}\b(?=\s+\S*(?:straße|strasse|platz|weg|allee|markt)\b)",
            target,
            value,
            flags=re.IGNORECASE,
        )
    return value


def _extract_location(paragraphs: Sequence[str]) -> str | None:
    base: str | None = None
    for paragraph in paragraphs[:3]:
        if match := LOCATION_LEAD_RE.match(paragraph):
            base = _format_location(match.group(1))
            break
        if not base and (match := CITY_AREA_RE.search(paragraph)):
            base = _format_location(match.group(1))
    detail: str | None = None
    for paragraph in paragraphs:
        if match := DETAIL_LOCATION_RE.search(paragraph):
            detail = _format_location(match.group(1))
            break
    if base and detail and normalize_key(detail) not in normalize_key(base):
        return f"{base}, {detail}"
    return base or detail


def _without_location_lead(value: str) -> str:
    if match := LOCATION_LEAD_RE.match(value):
        letters = "".join(character for character in match.group(1) if character.isalpha())
        if letters and letters == letters.upper():
            return value[match.end():].lstrip()
    return value


def _join_selected(sentences: Sequence[str], indexes: Sequence[int], *, limit: int = 620) -> str | None:
    selected: list[str] = []
    for index in indexes:
        sentence = sentences[index]
        if sentence not in selected:
            selected.append(sentence)
        if len(selected) == 2:
            break
    if not selected:
        return None
    return _trim_at_word(" ".join(selected), limit)


def _extract_delikt(item: dict[str, object], text: str) -> str:
    title = re.sub(r"^\d{1,6}\s*[.)]\s*", "", str(item["title"])).strip()
    material = f"{title}\n{text}"
    rules = (
        (r"(?:Drogen|Betäubungsmittel).{0,100}\ban\b.{0,80}"
         r"(?:minderjähr|(?<!\d)(?:[0-9]|1[0-7])-jähr|Jugendliche)|"
         r"(?:minderjähr|(?<!\d)(?:[0-9]|1[0-7])-jähr|Jugendliche).{0,180}"
         r"(?:Drogen|Betäubungsmittel).{0,80}\b(?:bekommen|erhalten|angeboten|abgegeben|überlassen)",
         "Drogenabgabe an Minderjährige"),
        (r"(?:sexuell bedrängt|sexuell missbraucht).{0,400}(?:Drogen|Betäubungsmittel)|"
         r"(?:Drogen|Betäubungsmittel).{0,400}(?:sexuell bedrängt|sexuell missbraucht)",
         "Sexualdelikte sowie Drogenabgabe an Minderjährige"),
        (r"versuchte[nr]?\s+(?:bewaffnete[nr]?\s+)?Raub|versuchten Raub", "versuchter bewaffneter Raub"),
        (r"schwere[nr]?\s+Raub", "schwerer Raub"),
        (r"Raub.{0,120}(?:Schusswaffe|Messer)|(?:Schusswaffe|Messer).{0,120}Raub", "bewaffneter Raub"),
        (r"Drogenschmuggel|Rauschgift.{0,100}geschmuggelt|Betäubungsmittel.{0,100}geschmuggelt",
         "Drogenschmuggel und illegale Einfuhr eines Arzneimittels"
         if re.search(r"illegal eingeführte[sn]? Arzneimittel", material, re.IGNORECASE)
         else "Drogenschmuggel"),
        (r"illegal(?:em|er|en)? Handel.{0,80}Betäubungsmittel", "illegaler Handel mit Betäubungsmitteln"),
        (r"(?:Besitz|aufgefunden|bei sich|stellte[n]? bei .{0,80} fest|fanden? bei)"
         r".{0,100}(?:Betäubungsmittel|synthetische Drogen)|"
         r"(?:Betäubungsmittel|synthetische Drogen).{0,100}(?:bei sich|aufgefunden)",
         "Besitz von Betäubungsmitteln"),
        (r"(?:Fahrt|Fahren|Fahrer).{0,120}(?:Drogen|Betäubungsmittel|Cannabis|THC)|"
         r"(?:Drogen|Betäubungsmittel|Cannabis|THC).{0,120}(?:Fahrt|Fahren|Fahrer)",
         "Fahren unter Betäubungsmitteleinfluss"),
        (r"tätliche[rsn]? Angriff.{0,80}(?:Polizei|Vollstreckungsbeamte)",
         "tätlicher Angriff auf Vollstreckungsbeamte"),
        (r"sexuelle Belästigung|sexuell belästigt|unsittlich berührt", "sexuelle Belästigung"),
        (r"exhibition", "exhibitionistische Handlung"),
        (r"gefährliche Körperverletzung", "gefährliche Körperverletzung"),
        (r"Körperverletzung", "Körperverletzung"),
        (r"Raub|beraubt", "Raub"),
    )
    # Combined sexual/drug conduct is more specific than drug conduct alone.
    combined = rules[1]
    if re.search(combined[0], material, re.IGNORECASE | re.DOTALL):
        return combined[1]
    for pattern, label in (rules[0], *rules[2:]):
        if re.search(pattern, material, re.IGNORECASE | re.DOTALL):
            return label
    categories = [str(value) for value in item.get("categories", [])]
    if len(categories) == 1:
        return categories[0]
    return title


def _result_priority(sentence: str) -> int:
    if re.search(
        r"Untersuchungshaft|Haftbefehl|Justizvollzugsanstalt|auf freien Fuß|"
        r"entlassen|Heimreise|flüchtig|Fahndung",
        sentence,
        re.IGNORECASE,
    ):
        return 0
    if re.search(r"Krankenhaus|Fachklinik", sentence, re.IGNORECASE):
        return 1
    if re.search(r"sichergestellt|beschlagnahmt|Blutentnahme|Weiterfahrt", sentence, re.IGNORECASE):
        return 2
    return 3


def _special_priority(sentence: str) -> int:
    if re.search(
        r"DEIG|Distanzelektroimpulsgerät|versteckt|Kilogramm|Gramm|"
        r"sexuell bedrängt|sexuell missbraucht|unsittlich berührt\w*",
        sentence,
        re.IGNORECASE,
    ):
        return 0
    if re.search(r"Schusswaffe|Messer|Waffe|Pfefferspray|Tierabwehrspray", sentence, re.IGNORECASE):
        return 1
    return 2


def _special_kind(sentence: str) -> str:
    rules = (
        (r"DEIG|Distanzelektroimpulsgerät", "deig"),
        (r"sexuell bedrängt|sexuell missbraucht|unsittlich berührt\w*", "sexual"),
        (r"versteckt|Kilogramm|Gramm|fünfstelligen|sechsstelligen", "menge-versteck"),
        (r"Drogen angeboten|Drogen bekommen|Betäubungsmittel.{0,100}abgegeben", "drogenabgabe"),
        (r"Schusswaffe|Messer|Waffe|Pfefferspray|Tierabwehrspray", "waffe"),
        (r"wollte sich .{0,40} nicht äußern", "keine-angaben"),
    )
    for pattern, kind in rules:
        if re.search(pattern, sentence, re.IGNORECASE):
            return kind
    return normalize_key(sentence)[:80]


def _select_special_indexes(sentences: Sequence[str]) -> list[int]:
    candidates = [
        index
        for index, sentence in enumerate(sentences)
        if SPECIAL_SENTENCE_RE.search(sentence)
    ]
    candidates.sort(key=lambda index: (_special_priority(sentences[index]), index))
    selected: list[int] = []
    kinds: set[str] = set()
    for index in candidates:
        kind = _special_kind(sentences[index])
        if kind in kinds:
            continue
        kinds.add(kind)
        selected.append(index)
        if len(selected) == 2:
            break
    return selected


def _field_value(
    sentences: Sequence[str],
    indexes: Sequence[int],
    *,
    missing: str,
    limit: int = 380,
) -> str:
    return _join_selected(sentences, indexes, limit=limit) or missing


def _compact_result(sentences: Sequence[str], indexes: Sequence[int]) -> str:
    text = " ".join(sentences)
    parts: list[str] = []

    if re.search(r"\bauf freien Fuß gesetzt\b", text, re.IGNORECASE):
        parts.append("nach Abschluss der polizeilichen Maßnahmen auf freien Fuß gesetzt")
    elif re.search(r"\bHeimreise antreten\b", text, re.IGNORECASE):
        parts.append("nach Abschluss der polizeilichen Maßnahmen Heimreise angetreten")
    else:
        was_arrested = bool(re.search(
            r"\bfestgenommen\b|\bvorläufig festgenommen\b|"
            r"\b(?:nahm|nahmen)\b.{0,160}\bfest\b|"
            r"\b(?:bei der|nach erfolgter)\s+Festnahme\b",
            text,
            re.IGNORECASE,
        ))
        if was_arrested:
            parts.append("festgenommen")
        if re.search(r"\bUntersuchungshaftbefehl\w*\b", text, re.IGNORECASE):
            parts.append("Untersuchungshaftbefehl erlassen")
        elif re.search(r"\bUntersuchungshaft\b", text, re.IGNORECASE):
            parts.append("Untersuchungshaft")
        if re.search(r"\bJustizvollzugsanstalt\b", text, re.IGNORECASE):
            parts.append("in Justizvollzugsanstalt untergebracht")
        elif re.search(r"\bHaftanstalt\b", text, re.IGNORECASE):
            parts.append("in Haftanstalt überstellt")
        if not was_arrested and re.search(
            r"(?:Täter|Tatverdächtig\w*)\s+(?:(?:ist|sind|war|blieb(?:en)?)\s+)?flüchtig",
            text,
            re.IGNORECASE,
        ):
            parts.append("tatverdächtige Person flüchtig")
        if not was_arrested and re.search(r"\bflüchtete\b", text, re.IGNORECASE):
            parts.append("tatverdächtige Person flüchtig")
        if re.search(
            r"Fahndung.{0,120}(?:erfolglos|ohne Erfolg|nicht zur Festnahme)",
            text,
            re.IGNORECASE,
        ):
            parts.append("Fahndung ohne Erfolg")
        if re.search(r"\bentlassen\b", text, re.IGNORECASE):
            parts.append("nach Abschluss der polizeilichen Maßnahmen entlassen")

    hospital = re.search(r"\bKrankenhaus\b", text, re.IGNORECASE)
    hospital_ages = (
        list(
            re.finditer(
                r"\b(\d{1,3})-Jährig\w*\b",
                text[max(0, hospital.start() - 220):hospital.start()],
                re.IGNORECASE,
            )
        )
        if hospital
        else []
    )
    if hospital_ages:
        parts.append(
            f"eine {int(hospital_ages[-1].group(1))}-jährige Person vorübergehend im Krankenhaus behandelt"
        )
    elif hospital:
        parts.append("Krankenhausbehandlung bzw. -unterbringung")
    if re.search(r"\bFachklinik\b", text, re.IGNORECASE):
        parts.append("in Fachklinik überstellt")
    if re.search(r"\bStrafverfahren\b", text, re.IGNORECASE):
        parts.append("Strafverfahren")
    if re.search(r"\bBlutentnahme\b", text, re.IGNORECASE):
        parts.append("Blutentnahme angeordnet")
    if re.search(r"\bWeiterfahrt\s+untersagt\b", text, re.IGNORECASE):
        parts.append("Weiterfahrt untersagt")

    unique: list[str] = []
    for part in parts:
        if part not in unique:
            unique.append(part)
    if unique:
        return "; ".join(unique)
    return _field_value(sentences, indexes, missing="nicht mitgeteilt", limit=300)


def _nationality_labels(text: str) -> list[str]:
    labels: list[str] = []
    for pattern, label in NATIONALITY_LABELS:
        if pattern.search(text) and label not in labels:
            labels.append(label)
    return labels


def _person_noun(text: str) -> str | None:
    nouns = (
        (r"\bMädchen\b", "Mädchen"),
        (r"\bJunge[n]?\b", "Junge"),
        (r"\bJugendlich\w*\b", "jugendliche Person"),
        (r"\bPolizeibeamt\w*\b|\bBeamte[nr]?\b", "Polizeikraft"),
        (r"\bKassierer\w*\b", "Kassenkraft"),
        (r"\bFrau\b", "Frau"),
        (r"\bMann\b|\bFahrgast\b", "Mann"),
    )
    for pattern, label in nouns:
        if re.search(pattern, text, re.IGNORECASE):
            return label
    return None


def _looks_like_person_description(sentence: str) -> bool:
    if re.fullmatch(r"(?:Täter|Personen)beschreibung\s*:?", sentence, re.IGNORECASE):
        return False
    indicators = sum(
        bool(re.search(pattern, sentence, re.IGNORECASE))
        for pattern in (
            r"\b\d{3}\s*cm\b",
            r"\bHaare?\b|\bHaar\b",
            r"\bbekleidet\b|\btrug\b",
            r"\bJacke\b|\bHose\b|\bPullover\b|\bT-Shirt\b",
            r"\bStatur\b|\bFigur\b|\bErscheinungsbild\b",
            r"\bRucksack\b|\bTasche\b",
        )
    )
    return indicators >= 2 or (
        bool(re.search(r"\bbeschrieben\b", sentence, re.IGNORECASE))
        and not sentence.rstrip().endswith(":")
    )


def _role_target_ages(sentence: str, role: str) -> set[int]:
    if role == "victim":
        patterns = (
            r"(?:gegen (?:den )?Kopf|auf (?:den )?Körper|im Gesicht|am Arm)\s+des\s+"
            r"(\d{1,3})-Jährig\w*",
            r"sich\s+der\s+(\d{1,3})-Jährig\w*.{0,160}\b(?:näherte|berührte)",
            r"\b(?:betroffene|aufgegriffene|verletzte)\s+(\d{1,3})-Jährig\w*",
            r"\b(\d{1,3})-Jährig\w*.{0,140}\b(?:wurde\b.{0,80}\bvon|"
            r"nicht mehr ansprechbar|zu Boden stürzte|Krankenhaus)",
        )
    else:
        patterns = (
            r"\bvon (?:einem|einer)\s+(\d{1,3})-Jährig\w*",
            r"\b(\d{1,3})-Jährig\w*(?:(?!\d{1,3}-Jährig).){0,180}"
            r"\b(?:flüchtete|festgenommen|"
            r"steht im Verdacht|Tatverdächtig\w*|Beschuldigt\w*)",
            r"\bgegen (?:den|die)\s+(\d{1,3})-Jährig\w*",
            r"\b(\d{1,3})-Jährig\w*.{0,120}\b(?:welcher|der)\b.{0,160}"
            r"\b(?:schlug|trat|stach|berührte|bedrohte|beraubte)",
            r"\b(?:der|die)\s+(\d{1,3})-Jährig\w*.{0,140}"
            r"\b(?:zog\b.{0,50}\bMesser|ging\b.{0,60}\bauf\s+(?:den\s+)?Polizeibeamt)",
        )
    values: set[int] = set()
    for pattern in patterns:
        values.update(
            int(match.group(1))
            for match in re.finditer(pattern, sentence, re.IGNORECASE)
        )
    return values


def _compact_people(
    sentences: Sequence[str],
    indexes: Sequence[int],
    *,
    missing: str,
    role: str,
) -> str:
    selected = [sentences[index] for index in indexes]
    if not selected:
        return missing
    text = " ".join(selected)
    entries: list[str] = []
    consumed_sentences: set[int] = set()
    covered_ages: set[int] = set()
    global_target_ages = set().union(
        *(_role_target_ages(sentence, role) for sentence in selected)
    )
    opposing_target_ages = set().union(
        *(_role_target_ages(sentence, "suspect" if role == "victim" else "victim") for sentence in selected)
    )

    for position, sentence in enumerate(selected):
        if role == "suspect":
            actor = re.search(
                r"\bvon (?:einem|einer)\s+(\d{1,3})-jährig\w*\s+"
                r"([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)",
                sentence,
            )
            if actor:
                labels = _nationality_labels(actor.group(0))
                details = [f"{int(actor.group(1))} Jahre", *labels]
                entries.append(f"1 ({', '.join(details)})")
                consumed_sentences.add(position)
                covered_ages.add(int(actor.group(1)))
                continue
        group = re.search(
            r"(?:zwei|2)\s+(\d{1,3})-jährig\w*\s+Tatverdächtig\w*,\s*"
            r"ein\w*\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)\s+und\s+"
            r"ein\w*\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)",
            sentence,
            re.IGNORECASE,
        )
        if group:
            labels = _nationality_labels(group.group(0))
            details = " und ".join(labels) if labels else "Staatsangehörigkeiten nicht mitgeteilt"
            entries.append(f"2 (je {int(group.group(1))} Jahre; {details})")
            consumed_sentences.add(position)
            covered_ages.add(int(group.group(1)))
            continue
        same_age = re.search(
            r"(\d{1,3})-Jährig\w*\s+und\s+eine\s+gleichaltrige\s+\w+",
            sentence,
            re.IGNORECASE,
        )
        if same_age:
            labels = _nationality_labels(same_age.group(0))
            details = " und ".join(labels) if labels else "Staatsangehörigkeiten nicht mitgeteilt"
            entries.append(f"2 (je {int(same_age.group(1))} Jahre; {details})")
            consumed_sentences.add(position)
            covered_ages.add(int(same_age.group(1)))

    individual_entries: list[tuple[int, tuple[str, ...], str | None, bool]] = []
    for position, sentence in enumerate(selected):
        if position in consumed_sentences:
            continue
        target_ages = _role_target_ages(sentence, role)
        age_matches = list(re.finditer(r"\b(\d{1,3})-jährig\w*\b", sentence, re.IGNORECASE))
        for age_match in age_matches:
            age = int(age_match.group(1))
            if global_target_ages and age not in global_target_ages:
                continue
            if not global_target_ages and age in opposing_target_ages:
                continue
            if target_ages and age not in target_ages:
                continue
            before = sentence[max(0, age_match.start() - 24):age_match.start()]
            after = sentence[age_match.start():min(len(sentence), age_match.end() + 55)]
            context = before + after
            labels = tuple(_nationality_labels(context))
            if not labels:
                related_labels: list[str] = []
                for related_sentence in sentences:
                    for related_match in re.finditer(
                        rf"\b{age}-jährig\w*\b",
                        related_sentence,
                        re.IGNORECASE,
                    ):
                        related_context = related_sentence[
                            max(0, related_match.start() - 20):
                            min(len(related_sentence), related_match.end() + 55)
                        ]
                        for label in _nationality_labels(related_context):
                            if label not in related_labels:
                                related_labels.append(label)
                labels = tuple(related_labels)
            noun = None if labels else _person_noun(after)
            explicitly_additional = bool(
                re.search(
                    r"\b(?:weitere[nr]?|zusätzliche[nr]?|auch\s+ein\w*)\b",
                    before,
                    re.IGNORECASE,
                )
            )
            if age in covered_ages and not explicitly_additional:
                continue
            existing = next(
                (
                    idx
                    for idx, (existing_age, existing_labels, _noun, extra) in enumerate(individual_entries)
                    if existing_age == age and not extra and not explicitly_additional
                    and (existing_labels == labels or not existing_labels or not labels)
                ),
                None,
            )
            if existing is not None:
                if labels and not individual_entries[existing][1]:
                    individual_entries[existing] = (age, labels, noun, explicitly_additional)
                continue
            individual_entries.append((age, labels, noun, explicitly_additional))

    grouped_plain_ages: dict[int, int] = {}
    for age, labels, _noun, _extra in individual_entries:
        if not labels:
            grouped_plain_ages[age] = grouped_plain_ages.get(age, 0) + 1

    emitted_plain_ages: set[int] = set()
    for age, labels, noun, _extra in individual_entries:
        if not labels and grouped_plain_ages.get(age, 0) > 1:
            if age not in emitted_plain_ages:
                count = grouped_plain_ages[age]
                entries.append(
                    f"{count} (je {age} Jahre; Staatsangehörigkeiten nicht mitgeteilt)"
                )
                emitted_plain_ages.add(age)
            continue
        details = []
        if noun:
            details.append(noun)
        details.append(f"{age} Jahre")
        details.extend(labels)
        entries.append(f"1 ({', '.join(details)})")

    if entries:
        result = "; ".join(entries)
        if not _nationality_labels(result) and "Staatsangehörigkeit" not in result:
            suffix = (
                "Staatsangehörigkeiten nicht mitgeteilt"
                if len(entries) > 1 or entries[0].startswith("2 ")
                else "Staatsangehörigkeit nicht mitgeteilt"
            )
            result += f"; {suffix}"
        return _trim_at_word(result, 360)

    if role == "victim" and re.search(r"\b(?:einer|eine)\s+Frau\b", text, re.IGNORECASE):
        return "1 (Frau; Staatsangehörigkeit nicht mitgeteilt)"
    if role == "victim" and re.search(r"\bPolizeibeamt\w*\b|\bBeamte[nr]?\b", text, re.IGNORECASE):
        return "1 (Polizeikraft; Staatsangehörigkeit nicht mitgeteilt)"

    if role == "suspect":
        description = next(
            (
                sentence
                for sentence in selected
                if _looks_like_person_description(sentence)
            ),
            None,
        )
        if description:
            return _trim_at_word(
                f"unbekannte Person ({description}; Staatsangehörigkeit nicht mitgeteilt)",
                360,
            )

    unknown = re.search(
        r"\b(?:unbekannt\w+|nicht identifiziert\w*)\s+(?:Mann|Frau|Person)"
        r"(?:[^.;]|\.(?!\s+[A-ZÄÖÜ])){0,260}",
        text,
        re.IGNORECASE,
    )
    if unknown:
        return _trim_at_word(unknown.group(0), 340)
    return _field_value(sentences, indexes, missing=missing, limit=320)


def summarize_item(item: dict[str, object]) -> ItemSummary:
    paragraphs = _summary_paragraphs(item)
    sentences = _sentences([_without_location_lead(paragraph) for paragraph in paragraphs])
    searchable = "\n".join(paragraphs)
    published = datetime.fromtimestamp(int(item["published_ts"]), tz=BERLIN_TZ)
    organization = normalize_space(str(item.get("organization", "")))
    if organization.casefold().startswith("polizeipräsidium "):
        organization = "PP " + organization[len("Polizeipräsidium "):]
    pm = f"PM {published:%d.%m.%Y}" + (f", {organization}" if organization else "")

    victim_indexes = [
        index for index, sentence in enumerate(sentences) if VICTIM_SENTENCE_RE.search(sentence)
    ]
    suspect_indexes: list[int] = []
    for index, sentence in enumerate(sentences):
        explicit_suspect = bool(SUSPECT_SENTENCE_RE.search(sentence))
        actor_phrase = bool(ACTOR_PHRASE_RE.search(sentence))
        weak_age_match = (
            bool(AGE_SENTENCE_RE.search(sentence))
            and index not in victim_indexes
            and bool(
                NATIONALITY_DETAIL_RE.search(sentence)
                or RESULT_SENTENCE_RE.search(sentence)
                or re.search(r"\b(?:kontrolliert|steht im Verdacht)\b", sentence, re.IGNORECASE)
            )
        )
        if (explicit_suspect or actor_phrase or weak_age_match) and not (
            index in victim_indexes
            and not actor_phrase
            and not re.search(
                r"\b(?:Tatverdächtig\w*|Beschuldigt\w*|steht im Verdacht)\b",
                sentence,
                re.IGNORECASE,
            )
        ):
            suspect_indexes.append(index)
    result_indexes = [
        index for index, sentence in enumerate(sentences) if RESULT_SENTENCE_RE.search(sentence)
    ]
    result_indexes.sort(key=lambda index: (_result_priority(sentences[index]), index))

    description_indexes = [
        index
        for index, sentence in enumerate(sentences)
        if _looks_like_person_description(sentence)
    ]
    for index in description_indexes:
        if index not in suspect_indexes:
            suspect_indexes.append(index)
    suspect_indexes.sort(
        key=lambda index: (
            not bool(NATIONALITY_DETAIL_RE.search(sentences[index])),
            not bool(AGE_SENTENCE_RE.search(sentences[index])),
            not bool(
                _looks_like_person_description(sentences[index])
            ),
            index,
        )
    )

    selected_victim_indexes = victim_indexes[:2]
    selected_suspect_indexes = suspect_indexes[:2]
    selected_result_indexes = result_indexes[:2]
    used_indexes = (
        set(selected_victim_indexes)
        | set(selected_suspect_indexes)
        | set(selected_result_indexes)
    )
    special_indexes = _select_special_indexes(sentences)
    if not special_indexes:
        special_indexes = [
            index
            for index, sentence in enumerate(sentences)
            if index not in used_indexes
            and not sentence.rstrip().endswith(":")
            and not re.fullmatch(
                r"(?:Täter|Personen)beschreibung\s*:?",
                sentence,
                re.IGNORECASE,
            )
            and not re.match(r"^Zeugenaufruf\b", sentence, re.IGNORECASE)
            and not re.search(
                r"\b(?:Ermittlungen? (?:übernommen|fortgeführt)|Polizei ermittelt)\b",
                sentence,
                re.IGNORECASE,
            )
        ][:1]

    categories = {str(value) for value in item.get("categories", [])}
    victim_missing = (
        "keine individualisierten Geschädigten"
        if categories == {"Drogendelikt"}
        else "nicht näher mitgeteilt"
    )
    victim_value = _compact_people(
        sentences,
        selected_victim_indexes,
        missing=victim_missing,
        role="victim",
    )
    suspect_value = _compact_people(
        sentences,
        selected_suspect_indexes,
        missing="nicht näher mitgeteilt",
        role="suspect",
    )

    return ItemSummary(
        pm=pm,
        tatdatum=_format_tatdatum(searchable, published=published.date()) or "nicht mitgeteilt",
        tatort=_extract_location(paragraphs) or "nicht näher mitgeteilt",
        delikt=_extract_delikt(item, searchable),
        opfer=victim_value,
        tatverdaechtige=suspect_value,
        ergebnis=_compact_result(sentences, selected_result_indexes),
        besonderheiten=_field_value(
            sentences,
            special_indexes,
            missing="keine weiteren Angaben",
        ),
    )


def _rss_short_description(item: dict[str, object]) -> str:
    summary = summarize_item(item)
    fields = [
        summary.pm,
        f"Tatdatum {summary.tatdatum}",
        f"Tatort: {summary.tatort}",
        f"Delikt: {summary.delikt}",
        f"Opfer: {summary.opfer}",
        f"Tatverdächtige: {summary.tatverdaechtige}",
        f"Ergebnis: {summary.ergebnis}",
        f"Besonderheiten: {summary.besonderheiten}",
    ]
    link = escape(str(item["link"]), quote=True)
    compact = escape(" / ".join(fields))
    return f'<p>{compact} / <a href="{link}">{link}</a></p>'


def _rss_long_description(item: dict[str, object]) -> str:
    parts = [
        f"<p>{escape(normalize_space(str(paragraph)))}</p>"
        for paragraph in item.get("body", [])
        if normalize_space(str(paragraph))
    ]
    link = escape(str(item["link"]), quote=True)
    parts.append(f'<p><a href="{link}">Originalmeldung der Bayerischen Polizei</a></p>')
    return "\n".join(parts)


def build_rss(items: Sequence[dict[str, object]], *, feed_url: str, built_at: datetime) -> str:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Bayerische Polizeimeldungen – ausgewählte Delikte"
    ET.SubElement(channel, "link").text = INDEX_URL
    ET.SubElement(channel, "description").text = (
        "Gewalt-, Sexual- und Drogendelikte sowie organisierte Kriminalität aus den "
        "Pressemitteilungen der Bayerischen Polizei."
    )
    ET.SubElement(channel, "language").text = "de-DE"
    ET.SubElement(channel, "generator").text = "polizei-bayern-rss"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(built_at.astimezone(UTC))
    ET.SubElement(channel, "ttl").text = "1440"
    ET.SubElement(
        channel,
        f"{{{ATOM_NS}}}link",
        {"href": feed_url, "rel": "self", "type": "application/rss+xml"},
    )

    if items:
        ET.SubElement(channel, "pubDate").text = format_datetime(
            datetime.fromtimestamp(int(items[0]["published_ts"]), tz=UTC)
        )

    for stored in items:
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = str(stored["title"])
        ET.SubElement(node, "link").text = str(stored["link"])
        ET.SubElement(node, "guid", {"isPermaLink": "false"}).text = str(stored["guid"])
        ET.SubElement(node, "pubDate").text = format_datetime(
            datetime.fromtimestamp(int(stored["published_ts"]), tz=UTC)
        )
        for category in stored.get("categories", []):
            ET.SubElement(node, "category").text = str(category)
        ET.SubElement(node, "description").text = _rss_short_description(stored)
        ET.SubElement(node, f"{{{CONTENT_NS}}}encoded").text = _rss_long_description(stored)

    ET.indent(rss, space="  ")
    return "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" + ET.tostring(
        rss, encoding="unicode", short_empty_elements=True
    ) + "\n"


def update(
    *,
    root: Path,
    feed_url: str,
    max_items: int,
    workers: int,
    max_sources: int | None = None,
    dry_run: bool = False,
    rebuild_all: bool = False,
) -> tuple[int, int, int]:
    index_html = fetch_text(INDEX_URL)
    entries = parse_index(index_html)
    state_path = root / "data" / "state.json"
    items_path = root / "data" / "items.json"
    feed_path = root / "feed.xml"

    state = (
        {"version": 1, "processed_sources": {}}
        if rebuild_all
        else _read_json(state_path, {"version": 1, "processed_sources": {}})
    )
    items = [] if rebuild_all else list(_read_json(items_path, []))
    if not isinstance(state, dict) or not isinstance(state.get("processed_sources"), dict):
        raise ValueError("data/state.json hat ein unbekanntes Format")
    if not isinstance(items, list):
        raise ValueError("data/items.json hat ein unbekanntes Format")

    processed: dict[str, dict[str, object]] = dict(state["processed_sources"])
    pending = [
        entry
        for entry in entries
        if str(entry["source_id"]) not in processed
        or int(processed[str(entry["source_id"])].get("published_ts", -1))
        != int(entry["published_ts"])
    ]
    if max_sources is not None:
        pending = pending[:max_sources]

    fetched: dict[str, tuple[dict[str, object], str]] = {}
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_map = {executor.submit(fetch_text, str(entry["url"])): entry for entry in pending}
        for future in as_completed(future_map):
            entry = future_map[future]
            try:
                fetched[str(entry["source_id"])] = (entry, future.result())
            except Exception as exc:  # individual pages are retried on the next run
                failures.append((str(entry["url"]), str(exc)))

    new_items: list[dict[str, object]] = []
    successful_sources = 0
    for source_id in sorted(
        fetched, key=lambda value: -int(fetched[value][0]["published_ts"])
    ):
        entry, detail_html = fetched[source_id]
        try:
            cases = parse_relevant_cases(detail_html, fallback_title=str(entry["title"]))
        except Exception as exc:
            if not classify(f"{entry['title']}\n{entry['teaser']}"):
                processed[source_id] = {
                    "published_ts": int(entry["published_ts"]),
                    "content_sha256": hashlib.sha256(detail_html.encode("utf-8")).hexdigest(),
                    "item_count": 0,
                    "processed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                }
                successful_sources += 1
                continue
            failures.append((str(entry["url"]), f"Parserfehler: {exc}"))
            continue
        items = [item for item in items if str(item.get("source_id")) != source_id]
        generated = [make_item(entry, case) for case in cases]
        new_items.extend(generated)
        processed[source_id] = {
            "published_ts": int(entry["published_ts"]),
            "content_sha256": hashlib.sha256(detail_html.encode("utf-8")).hexdigest(),
            "item_count": len(generated),
            "processed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        successful_sources += 1

    if failures and (not successful_sources or len(failures) > max(5, len(pending) // 5)):
        sample = "\n".join(f"- {url}: {error}" for url, error in failures[:10])
        raise RuntimeError(f"Zu viele Meldungsseiten konnten nicht verarbeitet werden:\n{sample}")
    for url, error in failures:
        print(f"WARNUNG: {url}: {error}", file=sys.stderr)

    items = deduplicate_and_sort([*items, *new_items], max_items)
    kept_sources = sorted(
        processed.items(),
        key=lambda pair: int(pair[1].get("published_ts", 0)),
        reverse=True,
    )[:5000]
    new_state = {"version": 1, "processed_sources": dict(kept_sources)}
    rss_text = build_rss(items, feed_url=feed_url, built_at=datetime.now(UTC))
    ET.fromstring(rss_text)

    if not dry_run:
        _write_text_atomic(state_path, _json_text(new_state))
        _write_text_atomic(items_path, _json_text(items))
        _write_text_atomic(feed_path, rss_text)

    return successful_sources, len(new_items), len(items)


def render_existing(*, root: Path, feed_url: str) -> int:
    items_path = root / "data" / "items.json"
    feed_path = root / "feed.xml"
    items = _read_json(items_path, [])
    if not isinstance(items, list):
        raise ValueError("data/items.json hat ein unbekanntes Format")
    rss_text = build_rss(items, feed_url=feed_url, built_at=datetime.now(UTC))
    ET.fromstring(rss_text)
    _write_text_atomic(feed_path, rss_text)
    return len(items)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="feed.xml nur aus den gespeicherten Einträgen neu erzeugen",
    )
    parser.add_argument(
        "--rebuild-all",
        action="store_true",
        help="Gespeicherten Stand verwerfen und den aktuellen Index vollständig neu aufbauen",
    )
    parser.add_argument("--max-sources", type=int, default=None, help="Nur für lokale Probeläufe")
    parser.add_argument("--workers", type=int, default=int(os.getenv("FETCH_WORKERS", "4")))
    parser.add_argument("--max-items", type=int, default=int(os.getenv("MAX_FEED_ITEMS", "500")))
    parser.add_argument("--feed-url", default=os.getenv("FEED_URL", DEFAULT_FEED_URL))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.render_only:
        total = render_existing(root=args.root, feed_url=args.feed_url)
        print(f"Feed neu erzeugt: {total} Einträge")
        return 0
    sources, added, total = update(
        root=args.root,
        feed_url=args.feed_url,
        max_items=args.max_items,
        workers=args.workers,
        max_sources=args.max_sources,
        dry_run=args.dry_run,
        rebuild_all=args.rebuild_all,
    )
    print(f"Verarbeitet: {sources} Meldungen; neue Fälle: {added}; Feed-Einträge: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
