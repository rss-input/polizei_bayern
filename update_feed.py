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
from datetime import UTC, datetime
from email.utils import format_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


INDEX_URL = "https://www.polizei.bayern.de/aktuelles/pressemitteilungen/index.html"
BASE_URL = "https://www.polizei.bayern.de"
DEFAULT_FEED_URL = "https://rss-input.github.io/polizei_bayern/feed.xml"
USER_AGENT = (
    "polizei-bayern-rss/1.0 "
    "(+https://github.com/rss-input/polizei_bayern; contact via repository)"
)
ATOM_NS = "http://www.w3.org/2005/Atom"
ET.register_namespace("atom", ATOM_NS)


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
                title = first_paragraph.text

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
        case = _case_from_events(
            combined, source_title=source_title, ordinal=0, collection=False
        )
        return [case] if case else []

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


def _rss_description(item: dict[str, object]) -> str:
    categories = ", ".join(str(value) for value in item.get("categories", []))
    organization = escape(str(item.get("organization", "")))
    parts = [
        f"<p><strong>Kategorie:</strong> {escape(categories)}</p>",
        f"<p><strong>Herausgeber:</strong> {organization}</p>",
    ]
    for paragraph in item.get("body", []):
        rendered = escape(str(paragraph)).replace("\n", "<br>")
        parts.append(f"<p>{rendered}</p>")
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
        ET.SubElement(node, "description").text = _rss_description(stored)

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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--dry-run", action="store_true")
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
