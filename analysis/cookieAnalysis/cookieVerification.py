import json
import os
import re
import requests
from typing import List, Dict, Any

# -------------------------------------------------
# Konfiguration
# -------------------------------------------------
EASYPRIVACY_URL = "https://easylist.to/easylist/easyprivacy.txt"
EASYPRIVACY_FILE = "easyprivacy.txt"
COOKIES_FILE = "cookies.json"
REPORT_FILE = "report.json"

# -------------------------------------------------
# Tracking Cookie Heuristiken (Hauptsignal)
# -------------------------------------------------
COOKIE_PATTERNS = {
    "analytics": [
        "_ga", "_gid", "_gat", "_gac_", "_gcl_", "_utm"
    ],
    "ads": [
        "_fbp", "ide", "_uet", "_tt_", "_scid"
    ],
    "social": [
        "fr", "xs", "c_user"
    ]
}

# -------------------------------------------------
# EasyPrivacy Download (einmalig)
# -------------------------------------------------
def download_easyprivacy() -> None:
    if os.path.exists(EASYPRIVACY_FILE):
        print("[i] EasyPrivacy bereits lokal vorhanden")
        return

    print("[i] Lade EasyPrivacy herunter …")
    r = requests.get(EASYPRIVACY_URL, timeout=15)
    r.raise_for_status()

    with open(EASYPRIVACY_FILE, "w", encoding="utf-8") as f:
        f.write(r.text)

    print("[✓] EasyPrivacy gespeichert")


# -------------------------------------------------
# Domains aus EasyPrivacy extrahieren (Bonus-Signal)
# -------------------------------------------------
def load_easyprivacy_domains() -> List[str]:
    domains = set()

    with open(EASYPRIVACY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line or line.startswith("!"):
                continue

            match = re.match(r"\|\|([^\\^/]+)\^", line)
            if match:
                domains.add(match.group(1).lower())

    print(f"[i] {len(domains)} EasyPrivacy-Domains geladen (Bonus)")
    return sorted(domains)


# -------------------------------------------------
# Cookies laden
# -------------------------------------------------
def load_cookies() -> List[Dict[str, str]]:
    with open(COOKIES_FILE, encoding="utf-8") as f:
        cookies = json.load(f)

    print(f"[i] {len(cookies)} Cookies geladen")
    return cookies


# -------------------------------------------------
# Cookie-Namen klassifizieren (Hauptlogik)
# -------------------------------------------------
def classify_cookie_name(name: str) -> str:
    lname = name.lower()

    for category, patterns in COOKIE_PATTERNS.items():
        for p in patterns:
            if lname.startswith(p):
                return category

    return "unknown"


# -------------------------------------------------
# Tracking-Score berechnen
# -------------------------------------------------
def calculate_score(name_category: str, domain_match: bool) -> int:
    score = 0

    # Hauptsignal: Cookie-Name
    if name_category != "unknown":
        score += 70

    # Bonus-Signal: EasyPrivacy-Domain
    if domain_match:
        score += 20

    return min(score, 100)


# -------------------------------------------------
# Analyse-Engine
# -------------------------------------------------
def analyze_cookies(
        cookies: List[Dict[str, str]],
        tracking_domains: List[str]
) -> List[Dict[str, Any]]:

    results = []

    for cookie in cookies:
        cookie_name = cookie["name"]
        cookie_domain = cookie["domain"].lstrip(".").lower()

        # EasyPrivacy Domain-Match (optional)
        domain_match = None
        for td in tracking_domains:
            if cookie_domain == td or cookie_domain.endswith("." + td):
                domain_match = td
                break

        # Name-basierte Klassifikation
        name_category = classify_cookie_name(cookie_name)

        score = calculate_score(
            name_category=name_category,
            domain_match=bool(domain_match)
        )

        results.append({
            "cookie_name": cookie_name,
            "cookie_domain": cookie_domain,
            "name_category": name_category,
            "easyprivacy_domain_match": domain_match,
            "tracking_score": score,
            "likely_tracking": score >= 60,
            "detection_method": (
                "cookie-name"
                if name_category != "unknown"
                else "unknown"
            )
        })

    return results


# -------------------------------------------------
# Main
# -------------------------------------------------
def main() -> None:
    download_easyprivacy()

    tracking_domains = load_easyprivacy_domains()
    cookies = load_cookies()

    results = analyze_cookies(cookies, tracking_domains)

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    print("\n=== Analyse-Ergebnis ===")
    for r in results:
        flag = "⚠️" if r["likely_tracking"] else "✓"
        print(
            f"{flag} {r['cookie_name']} "
            f"({r['cookie_domain']}) "
            f"Score={r['tracking_score']} "
            f"Typ={r['name_category']} "
            f"EasyPrivacy={r['easyprivacy_domain_match']}"
        )

    print(f"\n[✓] Report geschrieben: {REPORT_FILE}")


if __name__ == "__main__":
    main()
