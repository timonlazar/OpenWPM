# Cookie-Tracking-Analyse mit EasyPrivacy

Dieses Skript analysiert Cookies hinsichtlich ihres möglichen Tracking-Charakters.  
Dabei kombiniert es **heuristische Cookie-Namen-Erkennung** mit einer **Domain-Prüfung gegen EasyPrivacy** und berechnet daraus einen Tracking-Score.

Das Ziel ist eine **transparente, nachvollziehbare Einschätzung**, ob ein Cookie wahrscheinlich zu Tracking-Zwecken eingesetzt wird.

---

## Überblick

**Eingaben**
- `cookies.json` – Liste gesetzter Cookies (z. B. aus Browser- oder Crawl-Daten)
- `easyprivacy.txt` – Tracking-Domain-Liste (automatisch heruntergeladen)

**Ausgabe**
- `report.json` – Analyse-Ergebnisse pro Cookie inkl. Tracking-Score

---

## Funktionsweise

Die Analyse basiert auf zwei Signalen:

### 1. Cookie-Namen-Heuristiken (Hauptsignal)
Bestimmte Cookie-Namen sind typisch für Tracking, Analytics oder Werbung  
(z. B. `_ga`, `_fbp`, `_gid`).

Diese Heuristik ist das **stärkste Signal**.

### 2. EasyPrivacy-Domain-Abgleich (Bonus-Signal)
Domains werden mit der **EasyPrivacy-Filterliste** abgeglichen.  
Findet sich eine Übereinstimmung, erhöht dies die Tracking-Wahrscheinlichkeit.

---

## Tracking-Kategorien

### Analytics
Typische Cookies für Reichweiten- und Nutzungsanalyse:
- `_ga`, `_gid`, `_gat`
- `_gac_`, `_gcl_`, `_utm`

### Advertising
Cookies für Werbung und Conversion-Tracking:
- `_fbp`, `ide`
- `_uet`, `_tt_`, `_scid`

### Social Media
Cookies sozialer Netzwerke:
- `fr`, `xs`, `c_user`

### Unknown
Cookies, die keinem bekannten Muster entsprechen.

---

## Scoring-Modell

Der Tracking-Score liegt zwischen **0 und 100**.

| Signal | Punkte |
|------|--------|
| Bekannter Cookie-Name | +70 |
| EasyPrivacy-Domain-Match | +20 |
| **Maximalwert** | **100** |

Ein Cookie gilt als **wahrscheinlich Tracking**, wenn:

```text
tracking_score ≥ 60
