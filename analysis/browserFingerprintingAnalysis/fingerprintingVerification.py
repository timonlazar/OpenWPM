import json
from urllib.parse import urlparse
from typing import Dict, Any, List

try:
    from whotracksme.data.loader import DataSource
except ImportError as exc:
    raise ImportError(
        "WhoTracks.me data loader not found. Please install the 'whotracksme' package."
    ) from exc

# -----------------------------
# Helper
# -----------------------------

def domain_match(domain: str, tracker_domain: str) -> bool | dict[str, None | list[Any] | bool | str | Any]:
    """
    Subdomain-Matching
    """
    domain = normalize_domain(domain)
    tracker_domain = normalize_domain(tracker_domain)
    return (
            domain == tracker_domain
            or domain.endswith("." + tracker_domain)
    )

def extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def normalize_domain(domain: str) -> str:
    return domain.lstrip(".").lower()

def get_registrable_domain(domain: str) -> str:
    """
    Return the registrable domain (eTLD\+1) for a given hostname.
    Uses `tldextract` if available; falls back to a simple last-two-label heuristic.
    """
    domain = normalize_domain(domain)
    if not domain:
        return ""

    try:
        import tldextract  # optional dependency
        ext = tldextract.extract(domain)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        if ext.domain:
            return ext.domain
        return domain
    except Exception:
        parts = domain.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return domain

# -----------------------------
# Load WhoTracks.me DB
# -----------------------------

def load_wtm_trackers() -> Dict[str, Dict[str, Any]]:
    """
    Baut eine Domain → Tracker-Lookup-Tabelle
    aus WhoTracks.me Daten.
    """

    data = DataSource(populate=True)

    trackers_db: Dict[str, Dict[str, Any]] = {}

    # Nur letzter Monat (reicht völlig)
    for row in data.trackers.get_snapshot():
        tracker_id = row.tracker

        tracker_info = data.trackers.get_tracker(tracker_id)
        if not tracker_info:
            continue

        domains = tracker_info.get("domains", [])
        if not domains:
            continue

        company = tracker_info.get("company_id")
        category = tracker_info.get("category")

        for d in domains:
            d = normalize_domain(d)
            trackers_db[d] = {
                "company": company,
                "categories": [category] if category else []
            }

    return trackers_db


# -----------------------------
# Verification Logic
# -----------------------------

def verify_wtm_tracker(
        event: Dict[str, Any],
        wtm_db: Dict[str, Any]
) -> Dict[str, Any]:

    script_domain = extract_domain(event.get("script_url", ""))
    site_domain = extract_domain(event.get("top_level_url", ""))

    script_domain = normalize_domain(script_domain)
    site_domain = normalize_domain(site_domain)

    for tracker_domain, meta in wtm_db.items():
        if domain_match(script_domain, tracker_domain):
            return {
                "is_tracker": True,
                "tracker_domain": tracker_domain,
                "company": meta.get("company"),
                "categories": meta.get("categories", []),
                "third_party": script_domain != site_domain
            }

    return {
        "is_tracker": False,
        "third_party": script_domain != site_domain
    }


# -----------------------------
# OpenWPM Analysis
# -----------------------------

def analyze_openwpm_events(
        openwpm_events: List[Dict[str, Any]],
        wtm_db: Dict[str, Any]
) -> List[Dict[str, Any]]:

    results = []

    for ev in openwpm_events:
        verification = verify_wtm_tracker(ev, wtm_db)

        results.append({
            "script_url": ev.get("script_url"),
            "top_level_url": ev.get("top_level_url"),
            "object": ev.get("object"),
            "property": ev.get("property"),
            "timestamp": ev.get("timestamp"),
            **verification
        })

    return results


# -----------------------------
# Main
# -----------------------------

if __name__ == "__main__":

    OPENWPM_JSON_FILE = "./openwpm_events.json"

    with open(OPENWPM_JSON_FILE, "r", encoding="utf-8") as f:
        openwpm_events = json.load(f)

    # 🔧 WICHTIG: einzelnes Event → Liste
    if isinstance(openwpm_events, dict):
        openwpm_events = [openwpm_events]

    wtm_db = load_wtm_trackers()

    results = analyze_openwpm_events(openwpm_events, wtm_db)

    for r in results:
        print(json.dumps(r, indent=2))
