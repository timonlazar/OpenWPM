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

import base64
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from selenium.webdriver import Chrome

from ..config import BrowserParamsInternal, ManagerParamsInternal
from ..socket_interface import ClientSocket
from ..types import VisitId
import hashlib

# Maximum number of bytes to store for a response body (safe default to avoid
# unbounded memory usage). Responses larger than this will not be stored.
MAX_CONTENT_BYTES = 2 * 1024 * 1024  # 2 MiB

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
        # temporary map url -> last observed JS call stack
        self._last_js_callstacks: Dict[str, str] = {}

        # Open a dedicated socket to the storage controller
        self._sock = ClientSocket(serialization="json")
        assert manager_params.storage_controller_address is not None
        host, port = manager_params.storage_controller_address
        self._sock.connect(host, port)
        # Identify this connection to the storage controller
        self._sock.send("ChromeInstrumentation-%d" % self.browser_id)

        self._setup_cdp()
        # Install a lightweight in-page JS instrumentation helper. We don't
        # rely on the extension for Chrome, so inject a script that wraps
        # fetch/XHR/WebSocket and other Web APIs to capture events and
        # stacktraces into window.__openwpm_js_events for later retrieval.
        try:
            self._install_js_instrumentation()
        except Exception:
            logger.debug("BROWSER %i: Could not install in-page JS instrumentation", self.browser_id)

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

        # Re-inject instrumentation into the freshly loaded page (best-effort)
        try:
            if self.browser_params.js_instrument:
                self._install_js_instrumentation()
        except Exception:
            pass

        # Collect JS-level instrumentation events first so we can attach
        # callstacks to subsequent network records
        if self.browser_params.js_instrument:
            self._collect_js_events(visit_id)

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

    def _collect_js_events(self, visit_id: VisitId) -> None:
        """Retrieve events recorded by the injected JS instrumentation and send them."""
        try:
            events = self.driver.execute_script(
                "var ev = window.__openwpm_js_events || []; window.__openwpm_js_events = []; return ev;"
            ) or []
        except Exception as e:
            logger.debug("BROWSER %i: Failed to retrieve injected JS events: %s", self.browser_id, e)
            return

        # Clear previous mapping for this visit
        self._last_js_callstacks = {}

        doc_url = ""
        try:
            doc_url = self.driver.current_url
        except Exception:
            pass

        for ev in events:
            try:
                ev_type = ev.get("type")
                detail = ev.get("detail") or {}
                stack = ev.get("stack")

                # Try to extract a URL for correlation with resource entries
                url = None
                if isinstance(detail, dict):
                    args = detail.get("arguments")
                    if isinstance(args, list) and args:
                        first = args[0]
                        if isinstance(first, str):
                            url = first
                        elif isinstance(first, dict) and first.get("url"):
                            url = first.get("url")
                    if not url and detail.get("url"):
                        url = detail.get("url")

                # Store last observed stack for this URL
                if url and stack:
                    try:
                        self._last_js_callstacks[url] = stack
                    except Exception:
                        pass

                # Compose a record matching the `javascript` table schema
                js_record: Dict[str, Any] = {
                    "visit_id": visit_id,
                    "browser_id": self.browser_id,
                    "extension_session_uuid": None,
                    "event_ordinal": None,
                    "page_scoped_event_ordinal": None,
                    "window_id": None,
                    "tab_id": None,
                    "frame_id": None,
                    "script_url": detail.get("script_url") if isinstance(detail, dict) else None,
                    "script_line": None,
                    "script_col": None,
                    "func_name": None,
                    "script_loc_eval": None,
                    "document_url": doc_url,
                    "top_level_url": doc_url,
                    "call_stack": stack,
                    "symbol": None,
                    "operation": ev_type,
                    "value": json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail,
                    "arguments": json.dumps(detail.get("arguments")) if isinstance(detail, dict) and detail.get("arguments") else None,
                    "time_stamp": _now_ts(),
                }

                self._send("javascript", js_record)

                # If the event correlates to a network URL, emit a callstacks row
                if url and stack:
                    try:
                        req_id = hash(f"{visit_id}-{url}") & 0x7FFFFFFF
                        cs_rec = {
                            "request_id": req_id,
                            "browser_id": self.browser_id,
                            "visit_id": visit_id,
                            "call_stack": stack,
                        }
                        self._send("callstacks", cs_rec)
                    except Exception:
                        pass

            except Exception:
                logger.debug("BROWSER %i: Failed to process JS event: %s", self.browser_id, ev)

    def _install_js_instrumentation(self) -> None:
        """Injects a script into the page context to capture common Web API calls.

        The injected script maintains window.__openwpm_js_events array and
        pushes small JSON-serializable objects describing each event.
        """
        js = r"""
        (function(){
            try{
                if(window.__openwpm_js_installed) return;
                window.__openwpm_js_installed = true;
                window.__openwpm_js_events = window.__openwpm_js_events || [];

                function pushEvent(type, detail){
                    var stack = (new Error()).stack || '';
                    try{
                        window.__openwpm_js_events.push({type: type, detail: detail, stack: stack});
                    }catch(e){ /* silent */ }
                }

                // Wrap fetch
                if(window.fetch){
                    var _origFetch = window.fetch;
                    window.fetch = function(){
                        try{ pushEvent('fetch', {arguments: Array.prototype.slice.call(arguments)}); }catch(e){}
                        return _origFetch.apply(this, arguments);
                    };
                }

                // Wrap XMLHttpRequest
                try{
                    var _open = XMLHttpRequest.prototype.open;
                    var _send = XMLHttpRequest.prototype.send;
                    XMLHttpRequest.prototype.open = function(method, url){
                        this.__openwpm_xhr_url = url;
                        this.__openwpm_xhr_method = method;
                        return _open.apply(this, arguments);
                    };
                    XMLHttpRequest.prototype.send = function(body){
                        try{ pushEvent('xhr', {method: this.__openwpm_xhr_method, url: this.__openwpm_xhr_url}); }catch(e){}
                        return _send.apply(this, arguments);
                    };
                }catch(e){}

                // Wrap WebSocket
                try{
                    var _WS = window.WebSocket;
                    if(_WS){
                        window.WebSocket = function(url, protocols){
                            try{ pushEvent('websocket', {url: url}); }catch(e){}
                            return protocols ? new _WS(url, protocols) : new _WS(url);
                        };
                        window.WebSocket.prototype = _WS.prototype;
                    }
                }catch(e){}

                // Monitor navigator.geolocation.getCurrentPosition/watchPosition
                try{
                    if(navigator.geolocation){
                        var geo = navigator.geolocation;
                        var _get = geo.getCurrentPosition;
                        if(_get){
                            geo.getCurrentPosition = function(success, error, opts){
                                try{ pushEvent('geolocation.getCurrentPosition', {}); }catch(e){}
                                return _get.apply(this, arguments);
                            };
                        }
                        var _watch = geo.watchPosition;
                        if(_watch){
                            geo.watchPosition = function(success, error, opts){
                                try{ pushEvent('geolocation.watchPosition', {}); }catch(e){}
                                return _watch.apply(this, arguments);
                            };
                        }
                    }
                }catch(e){}

                // Monitor postMessage
                try{
                    var _post = window.postMessage;
                    window.postMessage = function(message, targetOrigin, transfer){
                        try{ pushEvent('postMessage', {message: message, targetOrigin: targetOrigin}); }catch(e){}
                        return _post.apply(this, arguments);
                    };
                }catch(e){}

            }catch(e){/* swallow */}
        })();
        """
        # Execute the injection as a raw script via Selenium so it's present in
        # newly opened pages. We inject once into the top-level browsing context.
        try:
            self.driver.execute_script(js)
        except Exception:
            # Best effort only
            pass

    def _collect_network(self, visit_id: VisitId) -> None:
        """
        Read the CDP network log via Performance.getEntries (JS) because
        the plain CDP Network domain only provides live events (no history).
        We use the PerformanceResourceTiming API which is always available.
        Additionally, extract DNS timing info from resource timing entries
        and send as 'dns' records to approximate Firefox DNS instrumentation.
        """
        try:
            # Determine top-level URL early so we can resolve relative Location headers
            top_level_url = ""
            try:
                top_level_url = self.driver.current_url
            except Exception:
                top_level_url = ""

            # If selenium-wire is available, prefer using driver.requests for
            # richer event-based data (full headers, response code, bodies).
            entries: List[Dict] = []
            try:
                # selenium-wire exposes .requests containing an ordered list of requests
                if hasattr(self.driver, "requests"):
                    sw_requests = getattr(self.driver, "requests") or []
                    # Collect processed requests and candidate redirects so we
                    # can try to link redirects to the follow-up request ids
                    processed_url_to_reqid: Dict[str, int] = {}
                    redirect_candidates: List[Dict[str, Any]] = []
                    for r in sw_requests:
                        try:
                            url = getattr(r, "url", "")
                            if not url:
                                continue
                            req_id = hash(f"{visit_id}-{url}") & 0x7FFFFFFF
                            now = _now_ts()
                            # Build request record
                            req_record = {
                                "visit_id": visit_id,
                                "browser_id": self.browser_id,
                                "url": url,
                                "top_level_url": self.driver.current_url if hasattr(self.driver, "current_url") else "",
                                "method": getattr(r, "method", "GET"),
                                "referrer": getattr(r, "headers", {}).get("Referer", "") if getattr(r, "headers", None) else "",
                                "headers": json.dumps(dict(getattr(r, "headers", {}))) if getattr(r, "headers", None) else "{}",
                                "request_id": req_id,
                                "resource_type": getattr(r, "resource_type", "other") if hasattr(r, "resource_type") else "other",
                                "post_body": getattr(r, "body", None),
                                "post_body_raw": None,
                                "is_XHR": getattr(r, "is_xhr", False) if hasattr(r, "is_xhr") else False,
                                "req_call_stack": None,
                                "frame_id": None,
                                "time_stamp": now,
                            }
                            # Attach any JS-collected callstack
                            try:
                                stack = None
                                stack = self._last_js_callstacks.get(url)
                                if not stack:
                                    base = url.split('?')[0]
                                    stack = self._last_js_callstacks.get(base)
                                if stack:
                                    req_record["req_call_stack"] = stack
                            except Exception:
                                pass

                            # Build response record if available. Ensure resp/status/resp_headers
                            # are always defined to avoid later name errors.
                            resp = None
                            status = 0
                            resp_headers: Dict[str, Any] = {}
                            try:
                                # Obtain response object, headers and status (may be None)
                                resp = getattr(r, "response", None)
                                status = getattr(resp, "status_code", 0) if resp is not None else 0
                                resp_headers = dict(getattr(resp, "headers", {}) if resp is not None else {})
                            except Exception:
                                # Keep defaults if anything fails
                                resp = None
                                status = 0
                                resp_headers = {}

                            resp_record = {
                                "visit_id": visit_id,
                                "browser_id": self.browser_id,
                                "url": url,
                                "method": getattr(r, "method", "GET"),
                                "response_status": status,
                                "response_status_text": "",
                                "is_cached": False,
                                "headers": json.dumps(resp_headers) if resp_headers else "{}",
                                "request_id": req_id,
                                "location": resp_headers.get("Location") if resp_headers else None,
                                "time_stamp": now,
                                "content_hash": None,
                            }

                            # If response looks like JavaScript (content-type or .js) we
                            # attempt to capture and store the response body via the
                            # StorageController's page_content channel. This mirrors the
                            # Firefox extension behaviour.
                            try:
                                # Normalize content type and decide if this is JS
                                content_type = "".join([resp_headers.get("Content-Type", "")]) if resp_headers else ""
                                is_js = False
                                if content_type:
                                    ct = content_type.lower()
                                    if any(x in ct for x in ("javascript", "ecmascript")):
                                        is_js = True
                                if not is_js and url.lower().split('?')[0].endswith('.js'):
                                    is_js = True

                                # Try to obtain raw body from selenium-wire response
                                body = None
                                if resp is not None:
                                    # selenium-wire usually exposes `body` as bytes
                                    body = getattr(resp, "body", None) or getattr(resp, "raw_body", None) or getattr(resp, "text", None)

                                # If body is text, convert to bytes
                                if body is not None and isinstance(body, str):
                                    try:
                                        body = body.encode("utf-8")
                                    except Exception:
                                        body = None

                                if is_js and body is not None and isinstance(body, (bytes, bytearray)):
                                    # Respect size limit
                                    if len(body) <= MAX_CONTENT_BYTES:
                                        chash = hashlib.md5(body).hexdigest()
                                        resp_record["content_hash"] = chash
                                        try:
                                            b64 = base64.b64encode(body).decode("ascii")
                                            # Send content to storage controller using the
                                            # special page_content channel expected by the
                                            # StorageController
                                            try:
                                                self._sock.send(("page_content", (b64, chash)))
                                            except Exception:
                                                # Fall back to sending via _send if socket has issues
                                                self._send("page_content", {"content": b64, "content_hash": chash})
                                        except Exception:
                                            # If encoding fails, leave content_hash None
                                            resp_record["content_hash"] = None
                                    else:
                                        # Too large, skip storing body but record that we skipped
                                        resp_record["content_hash"] = "<skipped>"
                            except Exception:
                                pass

                            # If response is a redirect (3xx) create a candidate redirect
                            try:
                                if 300 <= status < 400 and resp_headers and resp_headers.get("Location"):
                                    raw_loc = resp_headers.get("Location")
                                    redirect_candidates.append({
                                        "old_request_url": url,
                                        "old_request_id": req_id,
                                        "raw_location": raw_loc,
                                        "response_status": status,
                                        "headers": resp_headers,
                                        "time_stamp": now,
                                    })
                            except Exception:
                                pass

                            # record mapping from URL to req_id for later redirect linking
                            try:
                                processed_url_to_reqid[url] = req_id
                            except Exception:
                                pass

                            # Send the response record once
                            self._send("http_responses", resp_record)
                        except Exception:
                            continue
                    # After processing selenium-wire queue, try to resolve redirect targets
                    try:
                        for cand in redirect_candidates:
                            raw_loc = cand.get("raw_location")
                            try:
                                new_loc = urljoin(top_level_url or cand.get("old_request_url"), raw_loc)
                            except Exception:
                                new_loc = raw_loc

                            # Try to find a matching processed request id
                            new_req_id = processed_url_to_reqid.get(new_loc)
                            # Normalized no-query form
                            new_loc_no_q = new_loc.split('?')[0]
                            if new_req_id is None:
                                new_req_id = processed_url_to_reqid.get(new_loc_no_q)
                            if new_req_id is None:
                                # Fallback: try suffix match (path only) since some requests may differ by scheme/host normalization
                                for k, v in processed_url_to_reqid.items():
                                    try:
                                        if k.endswith(new_loc) or new_loc.endswith(k) or k.split('?')[0].endswith(new_loc_no_q):
                                            new_req_id = v
                                            break
                                    except Exception:
                                        continue

                            redirect_rec = {
                                "visit_id": visit_id,
                                "browser_id": self.browser_id,
                                "old_request_url": cand.get("old_request_url"),
                                "old_request_id": cand.get("old_request_id"),
                                "new_request_url": new_loc,
                                "new_request_id": new_req_id,
                                "extension_session_uuid": None,
                                "event_ordinal": None,
                                "window_id": None,
                                "tab_id": None,
                                "frame_id": None,
                                "response_status": cand.get("response_status"),
                                "response_status_text": "",
                                "headers": json.dumps(cand.get("headers") or {}),
                                "time_stamp": cand.get("time_stamp"),
                            }
                            self._send("http_redirects", redirect_rec)
                    except Exception:
                        pass

                    # After processing selenium-wire queue, clear it for next visit
                    try:
                        if hasattr(self.driver, "requests") and hasattr(self.driver.requests, "clear"):
                            self.driver.requests.clear()
                    except Exception:
                        pass
                    return
            except Exception:
                # Fall back to performance entries if selenium-wire access fails
                entries = []

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
            # Attach any JS-collected callstack for this URL
            try:
                stack = None
                # Direct match
                stack = self._last_js_callstacks.get(url)
                # Try normalized match by stripping query params if not found
                if not stack:
                    base = url.split('?')[0]
                    stack = self._last_js_callstacks.get(base)
                if stack:
                    req_record["req_call_stack"] = stack
            except Exception:
                pass

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

        # Extract DNS-like records from resource timing entries
        try:
            dns_entries = self.driver.execute_script(
                "return (performance.getEntriesByType('resource') || []).map(function(e){ return {name: e.name, domainLookupStart: e.domainLookupStart, domainLookupEnd: e.domainLookupEnd}; });"
            ) or []
        except Exception:
            dns_entries = []

        for de in dns_entries:
            try:
                start = de.get("domainLookupStart") or 0
                end = de.get("domainLookupEnd") or 0
                if end > start:
                    # Map to dns_responses schema
                    dns_rec = {
                        "request_id": req_id_counter,  # best-effort unique token for this batch
                        "browser_id": self.browser_id,
                        "visit_id": visit_id,
                        "hostname": de.get("name", ""),
                        "addresses": None,
                        "used_address": None,
                        "canonical_name": None,
                        "is_TRR": None,
                        "time_stamp": _now_ts(),
                    }
                    self._send("dns_responses", dns_rec)
            except Exception:
                pass

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
