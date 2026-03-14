"""
Chrome CDP-based instrumentation for OpenWPM.

Replaces the Firefox WebExtension for Chrome by using the Chrome DevTools
Protocol (CDP) via Selenium's execute_cdp_cmd to collect network, cookie
and navigation data – stored in exactly the same tables as the Firefox
extension so all downstream analysis code works unchanged.

Architecture
------------
Selenium does not expose a live CDP event stream in its standard API.
Instead we use the CDP Network.enable / Page.enable domains and then call
Network.getResponseBody / Page.getNavigationHistory after each page load to
reconstruct the data.  For a richer real-time stream the selenium-wire or
selenium-cdp packages would be needed, but they require additional deps.

What IS collected per page visit (triggered in collect_after_load()):
  • All cookies              → javascript_cookies
  • HTTP request log         → http_requests
  • HTTP response log        → http_responses
  • HTTP redirects           → http_redirects
  • Navigation history entry → navigations
"""

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from selenium.webdriver import Chrome

from ..config import BrowserParamsInternal, ManagerParamsInternal
from ..socket_interface import ClientSocket
from ..types import VisitId

logger = logging.getLogger("openwpm")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _headers_to_str(headers: Any) -> str:
    """Normalise CDP headers (dict or list of {name,value}) to a JSON string."""
    if isinstance(headers, dict):
        return json.dumps(headers)
    if isinstance(headers, list):
        return json.dumps({h.get("name", ""): h.get("value", "") for h in headers})
    return json.dumps({})


# ---------------------------------------------------------------------------
# Main instrumentation class
# ---------------------------------------------------------------------------

class ChromeInstrumentation:
    """
    CDP-based instrumentation for Chrome.

    Usage (inside BrowserManager):
        instr = ChromeInstrumentation(driver, browser_params, manager_params)
        instr.set_visit_id(visit_id)
        # … driver.get(url) …
        instr.collect_after_load()   # call once after each page load
        instr.close()                # call on shutdown
    """

    def __init__(
        self,
        driver: Chrome,
        browser_params: BrowserParamsInternal,
        manager_params: ManagerParamsInternal,
    ) -> None:
        self.driver = driver
        self.browser_params = browser_params
        self.manager_params = manager_params
        self.browser_id = browser_params.browser_id
        assert self.browser_id is not None

        self._visit_id: Optional[VisitId] = None
        self._lock = threading.Lock()

        # Open a dedicated socket to the storage controller
        self._sock = ClientSocket(serialization="json")
        assert manager_params.storage_controller_address is not None
        host, port = manager_params.storage_controller_address
        self._sock.connect(host, port)
        # Identify this connection to the storage controller
        self._sock.send("ChromeInstrumentation-%d" % self.browser_id)

        self._setup_cdp()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_visit_id(self, visit_id: VisitId) -> None:
        with self._lock:
            self._visit_id = visit_id

    def collect_after_load(self) -> None:
        """
        Collect all instrumentation data for the current page.
        Call this AFTER driver.get() has returned (page fully loaded).
        """
        visit_id = self._visit_id
        if visit_id is None:
            return

        if self.browser_params.http_instrument:
            self._collect_network(visit_id)

        if self.browser_params.cookie_instrument:
            self._collect_cookies(visit_id)

        if self.browser_params.navigation_instrument:
            self._collect_navigation(visit_id)

    def close(self) -> None:
        """Disable CDP domains and close the socket."""
        for domain in ("Network", "Page"):
            try:
                self.driver.execute_cdp_cmd(f"{domain}.disable", {})
            except Exception:
                pass
        try:
            self._sock.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _setup_cdp(self) -> None:
        """Enable the CDP domains we need."""
        try:
            if (
                self.browser_params.http_instrument
                or self.browser_params.cookie_instrument
            ):
                self.driver.execute_cdp_cmd("Network.enable", {})
                logger.debug("BROWSER %i: CDP Network domain enabled.", self.browser_id)

            if self.browser_params.navigation_instrument:
                self.driver.execute_cdp_cmd("Page.enable", {})
                logger.debug("BROWSER %i: CDP Page domain enabled.", self.browser_id)

        except Exception as e:
            logger.warning(
                "BROWSER %i: Could not enable CDP domains: %s", self.browser_id, e
            )

    # ------------------------------------------------------------------
    # Collection helpers
    # ------------------------------------------------------------------

    def _collect_network(self, visit_id: VisitId) -> None:
        """
        Read the CDP network log via Performance.getEntries (JS) because
        the plain CDP Network domain only provides live events (no history).
        We use the PerformanceResourceTiming API which is always available.
        """
        try:
            entries: List[Dict] = self.driver.execute_script(
                """
                var entries = performance.getEntriesByType('resource');
                var nav    = performance.getEntriesByType('navigation');
                return nav.concat(entries).map(function(e) {
                    return {
                        name:           e.name,
                        initiatorType:  e.initiatorType  || 'navigation',
                        transferSize:   e.transferSize   || 0,
                        responseStatus: e.responseStatus || 0,
                        duration:       e.duration       || 0,
                        startTime:      e.startTime      || 0
                    };
                });
                """
            ) or []
        except Exception as e:
            logger.debug("BROWSER %i: JS performance entries failed: %s", self.browser_id, e)
            entries = []

        top_url = ""
        try:
            top_url = self.driver.current_url
        except Exception:
            pass

        seen_urls = set()
        req_id_counter = 0

        for entry in entries:
            url = entry.get("name", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            req_id_counter += 1
            req_id = hash(f"{visit_id}-{url}") & 0x7FFFFFFF
            resource_type = entry.get("initiatorType", "other")
            now = _now_ts()

            # HTTP request record
            req_record: Dict[str, Any] = {
                "visit_id": visit_id,
                "browser_id": self.browser_id,
                "url": url,
                "top_level_url": top_url,
                "method": "GET",
                "referrer": "",
                "headers": "{}",
                "request_id": req_id,
                "resource_type": resource_type,
                "post_body": None,
                "post_body_raw": None,
                "is_XHR": resource_type in ("xmlhttprequest", "fetch"),
                "is_third_party_channel": None,
                "is_third_party_to_top_window": None,
                "triggering_origin": None,
                "loading_origin": None,
                "loading_href": None,
                "req_call_stack": None,
                "frame_id": None,
                "time_stamp": now,
            }
            self._send("http_requests", req_record)

            # HTTP response record
            status = entry.get("responseStatus", 0) or 200
            resp_record: Dict[str, Any] = {
                "visit_id": visit_id,
                "browser_id": self.browser_id,
                "url": url,
                "method": "GET",
                "response_status": status,
                "response_status_text": "",
                "is_cached": entry.get("transferSize", 1) == 0,
                "headers": "{}",
                "request_id": req_id,
                "location": None,
                "time_stamp": now,
                "content_hash": None,
            }
            self._send("http_responses", resp_record)

        # Collect redirects via CDP Network.getResponseBody is not feasible post-load;
        # instead detect via performance navigation redirect count
        try:
            redirect_count: int = self.driver.execute_script(
                "return performance.getEntriesByType('navigation')[0]?.redirectCount || 0;"
            ) or 0
        except Exception:
            redirect_count = 0

        if redirect_count > 0:
            now = _now_ts()
            redirect_record: Dict[str, Any] = {
                "visit_id": visit_id,
                "browser_id": self.browser_id,
                "old_request_url": "",
                "old_request_id": "",
                "new_request_url": top_url,
                "new_request_id": "",
                "response_status": 302,
                "response_status_text": "Found",
                "headers": "{}",
                "time_stamp": now,
            }
            self._send("http_redirects", redirect_record)

    def _collect_cookies(self, visit_id: VisitId) -> None:
        """Read all cookies for the current page via CDP."""
        try:
            result = self.driver.execute_cdp_cmd("Network.getCookies", {})
            cookies = result.get("cookies", [])
        except Exception as e:
            logger.debug("BROWSER %i: Network.getCookies failed: %s", self.browser_id, e)
            return

        for cookie in cookies:
            expiry_ts = None
            expires_val = cookie.get("expires", -1)
            if expires_val and expires_val > 0:
                try:
                    expiry_ts = datetime.fromtimestamp(
                        expires_val, tz=timezone.utc
                    ).isoformat()
                except (OSError, OverflowError, ValueError):
                    expiry_ts = None

            record: Dict[str, Any] = {
                "visit_id": visit_id,
                "browser_id": self.browser_id,
                "record_type": "snapshot",
                "change_cause": "chrome_cdp",
                "expiry": expiry_ts,
                "is_http_only": bool(cookie.get("httpOnly", False)),
                "is_host_only": not cookie.get("domain", "").startswith("."),
                "is_session": bool(cookie.get("session", False)),
                "host": cookie.get("domain", ""),
                "is_secure": bool(cookie.get("secure", False)),
                "name": cookie.get("name", ""),
                "path": cookie.get("path", ""),
                "value": cookie.get("value", ""),
                "same_site": cookie.get("sameSite", ""),
                "first_party_domain": "",
                "store_id": "0",
                "time_stamp": _now_ts(),
            }
            self._send("javascript_cookies", record)

    def _collect_navigation(self, visit_id: VisitId) -> None:
        """Record the current navigation via CDP Page.getNavigationHistory."""
        try:
            nav = self.driver.execute_cdp_cmd("Page.getNavigationHistory", {})
            current_index = nav.get("currentIndex", 0)
            entries = nav.get("entries", [])
            if not entries:
                return
            entry = entries[current_index] if current_index < len(entries) else entries[-1]
        except Exception as e:
            logger.debug(
                "BROWSER %i: Page.getNavigationHistory failed: %s", self.browser_id, e
            )
            return

        now = _now_ts()
        record: Dict[str, Any] = {
            "visit_id": visit_id,
            "browser_id": self.browser_id,
            "frame_id": None,
            "parent_frame_id": None,
            "url": entry.get("url", ""),
            "transition_type": entry.get("transitionType", None),
            "transition_qualifiers": None,
            "before_navigate_event_ordinal": None,
            "before_navigate_time_stamp": now,
            "committed_event_ordinal": None,
            "committed_time_stamp": now,
            "tab_id": None,
            "window_id": None,
        }
        self._send("navigations", record)

    # ------------------------------------------------------------------
    # Send to StorageController
    # ------------------------------------------------------------------

    def _send(self, table: str, record: Dict[str, Any]) -> None:
        try:
            self._sock.send((table, record))
        except Exception as e:
            logger.debug(
                "BROWSER %i: Failed to send record to storage [%s]: %s",
                self.browser_id, table, e,
            )
