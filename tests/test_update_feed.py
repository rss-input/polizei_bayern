import base64
import json
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import update_feed


def page(title: str, body: str) -> str:
    return f"""
    <html><body><main>
      <section class="bp-template bp-page-date"><span>18.08.2026, Testpräsidium</span></section>
      <section class="bp-template bp-presse">
        <bp-headline title="{title}"></bp-headline>
        <div class="bp-iwe2"><p>Kurztext, der nicht zum Haupttext gehört.</p></div>
      </section>
      <div class="bp-iwe2">{body}</div>
    </main></body></html>
    """


class IndexTests(unittest.TestCase):
    def test_extracts_and_sorts_montagedata(self):
        payload = [
            {
                "href": "/aktuelles/pressemitteilungen/10/index.html",
                "title": "Älter",
                "teaser": "Text",
                "date": 1000,
                "organization": {"name": "PP A"},
            },
            {
                "href": "/aktuelles/pressemitteilungen/11/index.html",
                "title": "Neuer",
                "teaser": "Text",
                "date": 2000,
                "organization": {"name": "PP B"},
            },
        ]
        html = f"<script>window.montagedata = {json.dumps(payload)}; window.filterdata = [];</script>"
        entries = update_feed.parse_index(html)
        self.assertEqual([entry["source_id"] for entry in entries], ["11", "10"])
        self.assertEqual(entries[0]["url"], update_feed.BASE_URL + payload[1]["href"])


class ParserTests(unittest.TestCase):
    def test_single_release_is_one_case(self):
        html = page(
            "Drogenschmuggel aufgedeckt",
            "<p>Bei einer Kontrolle fanden Beamte Kokain.</p>"
            "<p>Der 31-jährige Deutsche wurde festgenommen.</p>",
        )
        cases = update_feed.parse_relevant_cases(html, fallback_title="Fallback")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].title, "Drogenschmuggel aufgedeckt")
        self.assertEqual(cases[0].categories, ("Drogendelikt",))
        self.assertIn("31-jährige Deutsche", " ".join(cases[0].paragraphs))

    def test_single_release_with_explicit_transition_is_split(self):
        html = page(
            "Zwei Tatverdächtige festgenommen",
            "<p>Bei einer Kontrolle wurde ein Mann mit Kokain festgenommen.</p>"
            "<p>Gegen ihn erging Haftbefehl.</p>"
            "<p>Unabhängig davon nahmen Beamte eine weitere Frau mit Heroin fest.</p>"
            "<p>Sie wurde nach den Maßnahmen entlassen.</p>",
        )
        cases = update_feed.parse_relevant_cases(html, fallback_title="Fallback")
        self.assertEqual(len(cases), 2)
        self.assertNotIn("Unabhängig davon", " ".join(cases[0].paragraphs))
        self.assertIn("Unabhängig davon", " ".join(cases[1].paragraphs))
        self.assertEqual(
            cases[1].title,
            "Unabhängig davon nahmen Beamte eine weitere Frau mit Heroin fest.",
        )

    def test_numbered_collection_splits_cases_and_keeps_description(self):
        html = page(
            "Medieninformation vom 18.08.2026",
            "<h3>1201. Fahrraddiebstahl</h3><p>Ein Fahrrad wurde gestohlen.</p>"
            "<hr><h3>1202. Raub</h3><p>Ein Mann wurde beraubt.</p>"
            "<p><strong>Der Täter wurde wie folgt beschrieben:</strong><br>"
            "Männlich, 180 cm groß, schwarze Jacke.</p>",
        )
        cases = update_feed.parse_relevant_cases(html, fallback_title="Fallback")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].title, "1202. Raub")
        self.assertIn("Männlich, 180 cm groß", " ".join(cases[0].paragraphs))

    def test_base64_accordion_is_decoded_and_split(self):
        decoded = (
            "<p>Einleitung</p><p><strong>Jugendlicher greift Polizisten an</strong></p>"
            "<p>Der Jugendliche griff einen Beamten an. Er besitzt die deutsche "
            "Staatsangehörigkeit.</p><p>-</p>"
            "<p><strong>Sachbeschädigung</strong></p><p>Eine Scheibe wurde beschädigt.</p>"
        )
        encoded = base64.b64encode(decoded.encode()).decode()
        payload = json.dumps({"title": "Region", "data": {"iwe2": encoded}})
        html = f"""
        <bp-headline title="Pressemeldungen vom 18.08.2026"></bp-headline>
        <bp-item json='{payload}'></bp-item>
        """
        cases = update_feed.parse_relevant_cases(html, fallback_title="Fallback")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].title, "Jugendlicher greift Polizisten an")
        self.assertIn("deutsche Staatsangehörigkeit", " ".join(cases[0].paragraphs))

    def test_location_led_collection_splits_at_horizontal_rules(self):
        html = page(
            "Medieninfo Nordschwaben - 18.08.2026",
            "<p><strong>Innenstadt</strong> – Eine Frau wurde unvermittelt angegriffen.</p>"
            "<p>Die Polizei ermittelt wegen Körperverletzung.</p><hr>"
            "<p><strong>Hochzoll</strong> – Ein Fahrrad wurde entwendet.</p>",
        )
        cases = update_feed.parse_relevant_cases(html, fallback_title="Fallback")
        self.assertEqual(len(cases), 1)
        self.assertTrue(cases[0].title.startswith("Innenstadt"))

    def test_negligent_traffic_case_is_not_classified_as_violence(self):
        categories = update_feed.classify(
            "Bei einem Verkehrsunfall wurde eine Person verletzt. "
            "Ermittelt wird wegen fahrlässiger Körperverletzung."
        )
        self.assertNotIn("Gewaltdelikt", categories)

    def test_statistical_collection_section_is_not_treated_as_a_case(self):
        html = page(
            "Abschlusspressebericht zum Volksfest",
            "<p><strong>Körperverletzungsdelikte</strong></p>"
            "<p>Die Anzahl der Körperverletzungsdelikte blieb im Vergleich zum Vorjahr "
            "gleich. Im Jahr 2026 waren 19 Fälle zu verzeichnen (2025: 17).</p>",
        )
        self.assertEqual(
            update_feed.parse_relevant_cases(html, fallback_title="Fallback"), []
        )

    def test_no_nationality_is_inferred(self):
        html = page(
            "Raubdelikt",
            "<p>Max Mustermann soll einen Mann beraubt haben.</p>",
        )
        case = update_feed.parse_relevant_cases(html, fallback_title="Fallback")[0]
        body = " ".join(case.paragraphs)
        self.assertNotIn("Staatsangehörigkeit", body)
        self.assertNotIn("deutsch", body.casefold())


class FeedTests(unittest.TestCase):
    @staticmethod
    def item(guid: str, timestamp: int, content_hash: str):
        return {
            "guid": guid,
            "source_id": guid,
            "case_ordinal": 0,
            "title": guid,
            "link": "https://example.test/release",
            "published_ts": timestamp,
            "organization": "Test",
            "categories": ["Gewaltdelikt"],
            "body": ["Text"],
            "content_hash": content_hash,
        }

    def test_deduplicates_and_orders_newest_first(self):
        items = [
            self.item("old", 100, "a"),
            self.item("new", 300, "b"),
            self.item("duplicate-content", 200, "a"),
        ]
        result = update_feed.deduplicate_and_sort(items, 10)
        self.assertEqual([item["guid"] for item in result], ["new", "duplicate-content"])

    def test_rss_is_valid_and_has_stable_guids(self):
        items = [self.item("urn:test:1", 300, "a"), self.item("urn:test:2", 100, "b")]
        xml = update_feed.build_rss(
            items,
            feed_url="https://example.test/feed.xml",
            built_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        root = ET.fromstring(xml)
        self.assertEqual(root.tag, "rss")
        self.assertEqual(
            [node.text for node in root.findall("./channel/item/guid")],
            ["urn:test:1", "urn:test:2"],
        )
        self.assertIsNotNone(
            root.find(
                "./channel/item/{http://purl.org/rss/1.0/modules/content/}encoded"
            )
        )

    def test_short_and_long_forms_are_structured_and_not_the_full_release(self):
        item = self.item("urn:test:summary", 1787049960, "summary")
        item.update(
            {
                "title": "Zwei Tatverdächtige nach Raub festgenommen",
                "organization": "Polizeipräsidium Oberpfalz",
                "body": [
                    "REGENSBURG. Am Sonntag, 16.08.2026, forderten drei "
                    "Tatverdächtige einen 27-jährigen Mann zur Herausgabe von Geld auf.",
                    "Der 27-jährige Geschädigte wurde mit einer Schusswaffe bedroht.",
                    "Bei den Tatverdächtigen handelt es sich um drei 16-Jährige.",
                    "Die drei Tatverdächtigen wurden nach den polizeilichen Maßnahmen "
                    "wieder auf freien Fuß gesetzt.",
                    "Dieser redaktionelle Restabsatz darf nicht vollständig im Feed stehen.",
                ],
            }
        )
        summary = update_feed.summarize_item(item)
        self.assertEqual(summary.pm, "PM 18.08.2026, PP Oberpfalz")
        self.assertEqual(summary.tatdatum, "16.08.26")
        self.assertEqual(summary.tatort, "Regensburg")
        self.assertIn("27-jährige Geschädigte", summary.opfer)
        self.assertIn("drei 16-Jährige", summary.tatverdaechtige)
        self.assertIn("freien Fuß", summary.ergebnis)

        xml = update_feed.build_rss(
            [item],
            feed_url="https://example.test/feed.xml",
            built_at=datetime(2026, 8, 18, tzinfo=UTC),
        )
        root = ET.fromstring(xml)
        description = root.findtext("./channel/item/description", "")
        long_form = root.findtext(
            "./channel/item/{http://purl.org/rss/1.0/modules/content/}encoded", ""
        )
        self.assertIn("PM 18.08.2026, PP Oberpfalz", description)
        self.assertIn("Tatdatum: 16.08.26", description)
        self.assertIn("<strong>Opfer:</strong>", long_form)
        self.assertIn("<strong>Tatverdächtige:</strong>", long_form)
        self.assertNotIn("redaktionelle Restabsatz", description)
        self.assertNotIn("redaktionelle Restabsatz", long_form)

    def test_victim_is_not_mislabeled_when_actor_is_explicit(self):
        item = self.item("urn:test:actor", 1787049960, "actor")
        item.update(
            {
                "title": "Jugendliche aufgegriffen",
                "body": [
                    "PASSAU. Die aufgegriffene 15-Jährige musste anschließend in ein Krankenhaus gebracht werden.",
                    "Sie war zuvor von einem 21-jährigen Griechen über die Grenze gebracht worden.",
                    "Der Tatverdächtige wurde festgenommen.",
                ],
            }
        )
        summary = update_feed.summarize_item(item)
        self.assertIn("aufgegriffene 15-Jährige", summary.opfer or "")
        self.assertNotIn("Krankenhaus", summary.tatverdaechtige or "")
        self.assertIn("21-jährigen Griechen", summary.tatverdaechtige or "")

    def test_city_area_is_used_as_published_location(self):
        item = self.item("urn:test:city", 1787049960, "city")
        item.update(
            {
                "title": "Weitere Festnahme",
                "body": [
                    "Unabhängig davon nahmen Ermittler einen Mann im Stadtgebiet Nürnberg fest."
                ],
            }
        )
        self.assertEqual(update_feed.summarize_item(item).tatort, "Nürnberg")


if __name__ == "__main__":
    unittest.main()
