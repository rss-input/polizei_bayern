# Bayerische Polizeimeldungen als RSS-Feed

Dieses Repository erzeugt täglich einen RSS-2.0-Feed aus den
[Pressemitteilungen der Bayerischen Polizei](https://www.polizei.bayern.de/aktuelles/pressemitteilungen/index.html).

**Feed-Adresse:**
<https://rss-input.github.io/polizei_bayern/feed.xml>

Der Feed enthält Fälle aus diesen Bereichen:

- Gewaltdelikte
- Sexualdelikte
- Drogendelikte
- organisierte Kriminalität

Sammelmeldungen werden in einzelne Fälle zerlegt. Veröffentlichte Angaben zur
Staatsangehörigkeit und Personenbeschreibungen bleiben im Originaltext des
jeweiligen Falls erhalten. Das Programm ergänzt keine solchen Angaben und
leitet insbesondere nichts aus Namen ab.

Jeder RSS-Eintrag enthält zwei Darstellungen:

- `description`: kompakte Kurzform mit PM-Datum, Polizeipräsidium, Tatdatum,
  Tatort, Delikt und Kurzbeschreibung
- `content:encoded`: längere, gegliederte Form mit den zusätzlichen Feldern
  Opfer, Tatverdächtige, Ergebnis und Besonderheiten, soweit diese Angaben
  ausdrücklich und zuverlässig aus dem Meldungstext entnommen werden können

Der vollständige Pressemitteilungstext wird nicht in den Feed kopiert. Er ist
über den Link zur Originalmeldung erreichbar. Wenn ein Feld nicht zuverlässig
automatisch ermittelt werden kann, wird es ausgelassen; insbesondere werden
keine Staatsangehörigkeiten aus Namen oder anderen Indizien abgeleitet.

## Funktionsweise

`update_feed.py` lädt die aktuelle Meldungsliste und verarbeitet neue oder mit
einem neuen Veröffentlichungszeitpunkt versehene Quellen. Es unterstützt sowohl
normale Artikelseiten als auch die von einigen Polizeipräsidien verwendeten
eingebetteten Sammelmeldungen.

Die Auswahl erfolgt anhand nachvollziehbarer deutscher Deliktsbegriffe. Reine
Verkehrsunfälle mit nur fahrlässiger Körperverletzung werden nicht als
Gewaltdelikt gewertet. Jeder passende Fall erhält eine stabile GUID. Zusätzlich
werden identische Inhalte anhand eines Inhalts-Hashes zusammengeführt.

Die Dateien haben folgende Aufgaben:

- `feed.xml`: veröffentlichter RSS-2.0-Feed, neueste Fälle zuerst
- `data/items.json`: dauerhaft gespeicherte Feed-Einträge
- `data/state.json`: bereits geprüfte Pressemitteilungen
- `.github/workflows/update.yml`: tägliche Aktualisierung um 05:17 Uhr UTC
- `.github/workflows/pages.yml`: Veröffentlichung als GitHub Pages

Es gibt keine Python-Abhängigkeiten außerhalb der Standardbibliothek.

## Einmalige GitHub-Einrichtung

Nach dem ersten Commit ist nur noch die Aktivierung von GitHub Pages nötig:

1. Im Repository **Settings → Pages** öffnen.
2. Unter **Build and deployment** als Quelle **GitHub Actions** wählen.
3. Unter **Actions → RSS-Feed aktualisieren → Run workflow** den ersten Lauf
   manuell starten. Spätere Läufe erfolgen täglich automatisch.

Falls GitHub Actions im Repository generell deaktiviert sind, müssen sie unter
**Settings → Actions → General** erlaubt werden. Der Aktualisierungs-Workflow
benötigt Schreibrecht auf Repository-Inhalte, damit er `feed.xml` und die beiden
Datendateien committen kann. Dieses Recht ist im Workflow eng auf
`contents: write` begrenzt.

Nach erfolgreicher Pages-Bereitstellung ist der Feed hier erreichbar:

```text
https://rss-input.github.io/polizei_bayern/feed.xml
```

## Lokal ausführen

```bash
python3 -m unittest discover -s tests -v
python3 update_feed.py
```

Ein kleiner, nicht schreibender Probelauf lässt sich so ausführen:

```bash
python3 update_feed.py --dry-run --max-sources 10
```

Für einen vollständigen Neuaufbau aus dem aktuellen Online-Index steht
`python3 update_feed.py --rebuild-all` zur Verfügung.

Nur die Darstellung des Feeds aus den bereits gespeicherten Fällen lässt sich
ohne Netzwerkabruf neu erzeugen:

```bash
python3 update_feed.py --render-only
```

Optionale Umgebungsvariablen:

- `MAX_FEED_ITEMS` – maximale Zahl gespeicherter Feed-Einträge, Standard: 500
- `FETCH_WORKERS` – parallele Abrufe, Standard: 4
- `FEED_URL` – öffentliche URL des Feeds

## Fehlerverhalten und Wartung

Netzwerkabrufe werden mit Wartezeit wiederholt. Einzelne fehlgeschlagene
Meldungsseiten bleiben unverarbeitet und werden beim nächsten Lauf erneut
versucht. Wenn die Übersichtsseite ihr Format ändert oder zu viele Seiten
fehlschlagen, beendet sich das Programm ohne die bestehenden Daten zu
überschreiben. Alle Ausgabedateien werden atomar ersetzt.

Die automatischen Tests decken insbesondere Indexformat, normale Meldungen,
nummerierte und eingebettete Sammelmeldungen, Personenbeschreibungen,
Nicht-Ableitung von Staatsangehörigkeiten, Duplikate, Sortierung und gültiges
RSS-XML ab.

## Hinweis

Der Feed ist ein unabhängiges, automatisch erzeugtes Angebot. Maßgeblich ist
immer die verlinkte Originalmeldung der Bayerischen Polizei. Für
Beschuldigte und Tatverdächtige gilt die Unschuldsvermutung.
