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


if __name__ == "__main__":
    unittest.main()
