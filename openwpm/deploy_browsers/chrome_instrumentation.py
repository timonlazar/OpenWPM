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
reconstruct the data.

What IS collected per page visit (triggered in collect_after_load()):
  • JavaScript API events    → javascript
  • All cookies              → javascript_cookies
  • HTTP request log         → http_requests
  • HTTP response log        → http_responses
  • HTTP redirects           → http_redirects
  • DNS resolution metadata  → dns_responses
  • Navigation history entry → navigations
"""

import base64
import hashlib
import json
import logging
import socket
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

from selenium.webdriver import Chrome

from ..config import BrowserParamsInternal, ManagerParamsInternal
from ..socket_interface import ClientSocket
from ..types import VisitId

# Maximum number of bytes to store for a response body (safe default to avoid
# unbounded memory usage). Responses larger than this will not be stored.
MAX_CONTENT_BYTES = 2 * 1024 * 1024  # 2 MiB

logger = logging.getLogger("openwpm")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _headers_to_pairs_json(headers: Any) -> str:
    """Store headers in extension-compatible shape: [[name, value], ...]."""
    pairs: List[List[str]] = []
    if isinstance(headers, dict):
        for name, value in headers.items():
            pairs.append([str(name), str(value)])
    elif isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict):
                pairs.append([str(item.get("name", "")), str(item.get("value", ""))])
    return json.dumps(pairs)


def _header_lookup(headers: Any, key: str) -> Optional[str]:
    key_l = key.lower()
    if isinstance(headers, dict):
        for name, value in headers.items():
            if str(name).lower() == key_l:
                return str(value)
    elif isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict) and str(item.get("name", "")).lower() == key_l:
                return str(item.get("value", ""))
    return None


def _strip_nul_chars(value: Any) -> Any:
    """Recursively remove NUL chars from values before DB insertion."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_strip_nul_chars(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_nul_chars(item) for item in value)
    if isinstance(value, dict):
        return {k: _strip_nul_chars(v) for k, v in value.items()}
    return value


def _coerce_js_storage_string(value: Any) -> Optional[str]:
    """Match Firefox JS instrumentation storage semantics for primitive values."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float, list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


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
        self._prepared_window_handles: Set[str] = set()

        # Use dill serialization so binary fields (e.g. post_body_raw bytes)
        # can be sent to the storage controller without lossy JSON coercion.
        self._sock = ClientSocket(serialization="dill")
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
        if self.browser_params.js_instrument:
            try:
                self._install_js_instrumentation()
            except Exception:
                logger.debug(
                    "BROWSER %i: Could not install in-page JS instrumentation",
                    self.browser_id,
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_visit_id(self, visit_id: VisitId) -> None:
        with self._lock:
            self._visit_id = visit_id
        self._last_js_callstacks = {}
        try:
            self.driver.get_log("performance")
        except Exception:
            pass
        try:
            self.driver.execute_script(
                "if (window.__openwpm_js_events) { window.__openwpm_js_events = []; }"
            )
        except Exception:
            pass

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

        if self.browser_params.dns_instrument:
            self._collect_dns(visit_id)

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
                or self.browser_params.dns_instrument
            ):
                self.driver.execute_cdp_cmd("Network.enable", {})
                logger.debug("BROWSER %i: CDP Network domain enabled.", self.browser_id)

            if self.browser_params.navigation_instrument or self.browser_params.js_instrument:
                self.driver.execute_cdp_cmd("Page.enable", {})
                logger.debug("BROWSER %i: CDP Page domain enabled.", self.browser_id)

            if self.browser_params.js_instrument:
                self.prepare_page_instrumentation()

        except Exception as e:
            logger.warning(
                "BROWSER %i: Could not enable CDP domains: %s", self.browser_id, e
            )

    def prepare_page_instrumentation(self) -> None:
        """Ensure JS instrumentation is registered for the currently selected Chrome target."""
        if not self.browser_params.js_instrument:
            return

        try:
            self.driver.execute_cdp_cmd("Page.enable", {})
        except Exception:
            pass

        handle: Optional[str] = None
        try:
            handle = self.driver.current_window_handle
        except Exception:
            handle = None

        if handle is None or handle not in self._prepared_window_handles:
            try:
                self.driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": self._get_js_instrumentation_code()},
                )
                if handle is not None:
                    self._prepared_window_handles.add(handle)
                logger.debug(
                    "BROWSER %i: Registered JS instrumentation for target %s.",
                    self.browser_id,
                    handle,
                )
            except Exception as e:
                logger.debug(
                    "BROWSER %i: Could not register JS instrumentation for target %s: %s",
                    self.browser_id,
                    handle,
                    e,
                )

        try:
            self._install_js_instrumentation()
        except Exception:
            pass

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
                if not isinstance(ev, dict):
                    continue

                ev_type = ev.get("type")
                detail = ev.get("detail") or {}
                stack = ev.get("stack")

                if "symbol" in ev or "operation" in ev:
                    symbol = ev.get("symbol")
                    operation = ev.get("operation") or ev_type
                    script_url = ev.get("script_url") or ev.get("scriptUrl")
                    script_line = ev.get("script_line") or ev.get("scriptLine")
                    script_col = ev.get("script_col") or ev.get("scriptCol")
                    func_name = ev.get("func_name") or ev.get("funcName")
                    script_loc_eval = ev.get("script_loc_eval") or ev.get("scriptLocEval")
                    stack = ev.get("call_stack")
                    if stack is None:
                        stack = ev.get("callStack")
                    if stack is None:
                        stack = ev.get("stack")
                    event_doc_url = ev.get("document_url") or doc_url
                    top_level_url = ev.get("top_level_url") or event_doc_url
                    event_ordinal = ev.get("event_ordinal")
                    page_scoped_event_ordinal = ev.get("page_scoped_event_ordinal")
                    value = _coerce_js_storage_string(ev.get("value"))
                    arguments = ev.get("arguments")
                    if arguments is not None and not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)

                    url = None
                    if isinstance(arguments, str):
                        try:
                            parsed_args = json.loads(arguments)
                            if isinstance(parsed_args, list) and parsed_args:
                                first = parsed_args[0]
                                if isinstance(first, str):
                                    url = first
                                elif isinstance(first, dict) and first.get("url"):
                                    url = first.get("url")
                        except Exception:
                            pass

                    if not url and isinstance(value, str):
                        try:
                            parsed_val = json.loads(value)
                            if isinstance(parsed_val, dict) and parsed_val.get("url"):
                                url = parsed_val.get("url")
                        except Exception:
                            pass

                    if url and stack:
                        self._last_js_callstacks[url] = stack
                        self._last_js_callstacks[url.split("?")[0]] = stack

                    js_record = {
                        "visit_id": visit_id,
                        "browser_id": self.browser_id,
                        "extension_session_uuid": None,
                        "event_ordinal": event_ordinal,
                        "page_scoped_event_ordinal": page_scoped_event_ordinal,
                        "window_id": None,
                        "tab_id": None,
                        "frame_id": None,
                        "script_url": script_url,
                        "script_line": script_line,
                        "script_col": script_col,
                        "func_name": func_name,
                        "script_loc_eval": script_loc_eval,
                        "document_url": event_doc_url,
                        "top_level_url": top_level_url,
                        "call_stack": stack,
                        "symbol": symbol,
                        "operation": operation,
                        "value": value,
                        "arguments": arguments,
                        "time_stamp": ev.get("time_stamp") or ev.get("timeStamp") or _now_ts(),
                    }
                    self._send("javascript", js_record)

                    if url and stack:
                        try:
                            req_id = hash(f"{visit_id}-{url}") & 0x7FFFFFFF
                            self._send(
                                "callstacks",
                                {
                                    "request_id": req_id,
                                    "browser_id": self.browser_id,
                                    "visit_id": visit_id,
                                    "call_stack": stack,
                                },
                            )
                        except Exception:
                            pass
                    continue

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

    def _get_js_instrumentation_code(self) -> str:
        """Return a Chrome page-script mirroring OpenWPM's Firefox JS instrumentation."""
        settings = self.browser_params.cleaned_js_instrument_settings or []
        settings_json = json.dumps(settings, ensure_ascii=False)
        testing = "true" if self.manager_params.testing else "false"
        return f"""
        (function(){{
            try{{
                if (window.__openwpm_js_installed) return;
                window.__openwpm_js_installed = true;
                window.__openwpm_js_events = window.__openwpm_js_events || [];
                var JSInstrumentRequests = {settings_json};
                var OPENWPM_TESTING = {testing};
                var maxLogCount = 500;
                var logCounter = {{}};
                var inLog = false;
                var ordinal = 0;

                function getPropertyDescriptor(subject, name) {{
                    if (subject === undefined || subject === null) throw new Error("Can't get property descriptor");
                    var pd = Object.getOwnPropertyDescriptor(subject, name);
                    var proto = Object.getPrototypeOf(subject);
                    while (pd === undefined && proto !== null) {{
                        pd = Object.getOwnPropertyDescriptor(proto, name);
                        proto = Object.getPrototypeOf(proto);
                    }}
                    return pd;
                }}

                function getPropertyNames(subject) {{
                    if (subject === undefined || subject === null) throw new Error("Can't get property names");
                    var props = Object.getOwnPropertyNames(subject);
                    var proto = Object.getPrototypeOf(subject);
                    while (proto !== null) {{
                        props = props.concat(Object.getOwnPropertyNames(proto));
                        proto = Object.getPrototypeOf(proto);
                    }}
                    return Array.from(new Set(props));
                }}

                function getPathToDomElement(element) {{
                    try {{
                        if (!element || !element.tagName) return String(element);
                        if (element === document.body) return element.tagName;
                        if (element.parentNode === null) return "NULL/" + element.tagName;
                        var siblingIndex = 1;
                        var siblings = element.parentNode.childNodes || [];
                        for (var i = 0; i < siblings.length; i++) {{
                            var sibling = siblings[i];
                            if (sibling === element) {{
                                var path = getPathToDomElement(element.parentNode);
                                path += "/" + element.tagName + "[" + siblingIndex + "," + (element.id || "") + "," + (element.className || "");
                                if (element.tagName === "A") path += "," + (element.href || "");
                                path += "]";
                                return path;
                            }}
                            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) siblingIndex++;
                        }}
                    }} catch (e) {{}}
                    return String(element);
                }}

                function serializeObject(object, stringifyFunctions) {{
                    try {{
                        if (object === null) return "null";
                        if (typeof object === "function") return stringifyFunctions ? String(object) : "FUNCTION";
                        if (typeof object !== "object") return object;
                        var seenObjects = [];
                        return JSON.stringify(object, function(key, value) {{
                            if (value === null) return "null";
                            if (typeof value === "function") return stringifyFunctions ? String(value) : "FUNCTION";
                            if (typeof value === "object") {{
                                if (value && value.wrappedJSObject) value = value.wrappedJSObject;
                                if (typeof HTMLElement !== "undefined" && value instanceof HTMLElement) return getPathToDomElement(value);
                                if (key === "" || seenObjects.indexOf(value) < 0) {{
                                    seenObjects.push(value);
                                    return value;
                                }}
                                return typeof value;
                            }}
                            return value;
                        }});
                    }} catch (error) {{
                        return "SERIALIZATION ERROR: " + error;
                    }}
                }}

                function updateCounterAndCheckIfOver(scriptUrl, symbol) {{
                    var key = String(scriptUrl || "") + "|" + String(symbol || "");
                    if (key in logCounter && logCounter[key] >= maxLogCount) return true;
                    if (!(key in logCounter)) logCounter[key] = 1;
                    else logCounter[key] += 1;
                    return false;
                }}

                function getStackTrace() {{
                    try {{ throw new Error(); }} catch (err) {{ return err && err.stack ? String(err.stack) : ""; }}
                }}

                function getOriginatingScriptContext(getCallStack) {{
                    var trace = getStackTrace();
                    var lines = trace.split("\\n").map(function(line) {{ return line.trim(); }}).filter(Boolean);
                    var emptyContext = {{
                        scriptUrl: "",
                        scriptLine: "",
                        scriptCol: "",
                        funcName: "",
                        scriptLocEval: "",
                        callStack: getCallStack ? trace : "",
                    }};
                    for (var i = 0; i < lines.length; i++) {{
                        var line = lines[i];
                        if (!line) continue;
                        if (line.indexOf("getOriginatingScriptContext") !== -1 ||
                            line.indexOf("getStackTrace") !== -1 ||
                            line.indexOf("logValue") !== -1 ||
                            line.indexOf("logCall") !== -1 ||
                            line.indexOf("instrumentFunction") !== -1 ||
                            line.indexOf("instrumentObjectProperty") !== -1) {{
                            continue;
                        }}
                        var match = line.match(/^at\s+(.*?)\s+\((.*?):(\d+):(\d+)\)$/);
                        if (!match) match = line.match(/^at\s+(.*?):(\d+):(\d+)$/);
                        if (match) {{
                            if (match.length === 5) {{
                                return {{
                                    scriptUrl: match[2] || "",
                                    scriptLine: match[3] || "",
                                    scriptCol: match[4] || "",
                                    funcName: match[1] || "",
                                    scriptLocEval: "",
                                    callStack: getCallStack ? trace : "",
                                }};
                            }}
                            return {{
                                scriptUrl: match[1] || "",
                                scriptLine: match[2] || "",
                                scriptCol: match[3] || "",
                                funcName: "",
                                scriptLocEval: "",
                                callStack: getCallStack ? trace : "",
                            }};
                        }}
                        var ffMatch = line.match(/^(.*?)@(.*?):(\d+):(\d+)$/);
                        if (ffMatch) {{
                            return {{
                                scriptUrl: ffMatch[2] || "",
                                scriptLine: ffMatch[3] || "",
                                scriptCol: ffMatch[4] || "",
                                funcName: ffMatch[1] || "",
                                scriptLocEval: "",
                                callStack: getCallStack ? trace : "",
                            }};
                        }}
                    }}
                    return emptyContext;
                }}

                function pushRecord(record) {{
                    try {{
                        record.page_scoped_event_ordinal = ordinal;
                        record.event_ordinal = ordinal;
                        ordinal += 1;
                        record.time_stamp = new Date().toISOString();
                        try {{ record.document_url = String(window.location.href || ""); }} catch (e) {{ record.document_url = ""; }}
                        try {{ record.top_level_url = String(window.top.location.href || record.document_url || ""); }} catch (e) {{ record.top_level_url = record.document_url || ""; }}
                        window.__openwpm_js_events.push(record);
                    }} catch (e) {{}}
                }}

                function logValue(instrumentedVariableName, value, operation, callContext, logSettings) {{
                    if (inLog) return;
                    inLog = true;
                    if (updateCounterAndCheckIfOver(callContext.scriptUrl, instrumentedVariableName)) {{
                        inLog = false;
                        return;
                    }}
                    pushRecord({{
                        symbol: instrumentedVariableName,
                        operation: operation,
                        value: serializeObject(value, logSettings.logFunctionsAsStrings),
                        arguments: null,
                        script_url: callContext.scriptUrl,
                        script_line: callContext.scriptLine,
                        script_col: callContext.scriptCol,
                        func_name: callContext.funcName,
                        script_loc_eval: callContext.scriptLocEval,
                        call_stack: callContext.callStack,
                    }});
                    inLog = false;
                }}

                function logCall(instrumentedFunctionName, args, callContext, logSettings) {{
                    if (inLog) return;
                    inLog = true;
                    if (updateCounterAndCheckIfOver(callContext.scriptUrl, instrumentedFunctionName)) {{
                        inLog = false;
                        return;
                    }}
                    try {{
                        var serialArgs = [];
                        for (var i = 0; i < args.length; i++) {{
                            serialArgs.push(serializeObject(args[i], logSettings.logFunctionsAsStrings));
                        }}
                        pushRecord({{
                            symbol: instrumentedFunctionName,
                            operation: "call",
                            value: "",
                            arguments: serialArgs.length ? JSON.stringify(serialArgs) : null,
                            script_url: callContext.scriptUrl,
                            script_line: callContext.scriptLine,
                            script_col: callContext.scriptCol,
                            func_name: callContext.funcName,
                            script_loc_eval: callContext.scriptLocEval,
                            call_stack: callContext.callStack,
                        }});
                    }} catch (e) {{}}
                    inLog = false;
                }}

                function isObject(object, propertyName) {{
                    var property;
                    try {{ property = object[propertyName]; }} catch (error) {{ return false; }}
                    if (property === null) return false;
                    return typeof property === "object";
                }}

                function instrumentFunction(objectName, methodName, func, logSettings) {{
                    var wrapper = function() {{
                        var callContext = getOriginatingScriptContext(logSettings.logCallStack);
                        logCall(objectName + "." + methodName, arguments, callContext, logSettings);
                        return func.apply(this, arguments);
                    }};
                    try {{
                        if (func && func.prototype) {{
                            wrapper.prototype = func.prototype;
                            if (func.prototype.constructor) wrapper.prototype.constructor = func.prototype.constructor;
                        }}
                    }} catch (e) {{}}
                    return wrapper;
                }}

                function instrumentObjectProperty(object, objectName, propertyName, logSettings) {{
                    if (!object || !objectName || !propertyName || propertyName === "undefined") {{
                        throw new Error("Invalid request to instrumentObjectProperty");
                    }}
                    var propDesc = getPropertyDescriptor(object, propertyName);
                    if (!propDesc && logSettings.nonExistingPropertiesToInstrument.indexOf(propertyName) === -1) return;

                    var undefinedPropValue;
                    var undefinedPropDesc = {{
                        get: function() {{ return undefinedPropValue; }},
                        set: function(value) {{ undefinedPropValue = value; }},
                        enumerable: false,
                    }};

                    var originalGetter = propDesc ? propDesc.get : undefinedPropDesc.get;
                    var originalSetter = propDesc ? propDesc.set : undefinedPropDesc.set;
                    var originalValue = propDesc && ("value" in propDesc) ? propDesc.value : undefinedPropValue;

                    Object.defineProperty(object, propertyName, {{
                        configurable: true,
                        get: function() {{
                            var origProperty;
                            var callContext = getOriginatingScriptContext(logSettings.logCallStack);
                            var instrumentedVariableName = objectName + "." + propertyName;

                            if (!propDesc) origProperty = undefinedPropValue;
                            else if (originalGetter) origProperty = originalGetter.call(this);
                            else if ("value" in propDesc) origProperty = originalValue;
                            else {{
                                logValue(instrumentedVariableName, "", "get(failed)", callContext, logSettings);
                                return;
                            }}

                            if (typeof origProperty === "function") {{
                                if (logSettings.logFunctionGets) {{
                                    logValue(instrumentedVariableName, origProperty, "get(function)", callContext, logSettings);
                                }}
                                return instrumentFunction(objectName, propertyName, origProperty, logSettings);
                            }} else if (typeof origProperty === "object" && origProperty !== null && logSettings.recursive && logSettings.depth > 0) {{
                                return origProperty;
                            }}
                            logValue(instrumentedVariableName, origProperty, "get", callContext, logSettings);
                            return origProperty;
                        }},
                        set: function(value) {{
                            var callContext = getOriginatingScriptContext(logSettings.logCallStack);
                            var instrumentedVariableName = objectName + "." + propertyName;
                            var returnValue;
                            if (logSettings.preventSets && (typeof originalValue === "function" || typeof originalValue === "object")) {{
                                logValue(instrumentedVariableName, value, "set(prevented)", callContext, logSettings);
                                return value;
                            }}
                            if (originalSetter) {{
                                returnValue = originalSetter.call(this, value);
                            }} else if (!propDesc || ("value" in propDesc)) {{
                                inLog = true;
                                try {{
                                    if (Object.prototype.isPrototypeOf.call(object, this)) Object.defineProperty(this, propertyName, {{ value: value, configurable: true, writable: true }});
                                    else originalValue = value;
                                    returnValue = value;
                                }} finally {{
                                    inLog = false;
                                }}
                            }} else {{
                                logValue(instrumentedVariableName, value, "set(failed)", callContext, logSettings);
                                return value;
                            }}
                            logValue(instrumentedVariableName, value, "set", callContext, logSettings);
                            return returnValue;
                        }},
                    }});
                }}

                function instrumentObject(object, instrumentedName, logSettings) {{
                    if (object === undefined || object === null) return;
                    var propertiesToInstrument;
                    if (logSettings.propertiesToInstrument === null) propertiesToInstrument = [];
                    else if (!logSettings.propertiesToInstrument || logSettings.propertiesToInstrument.length === 0) propertiesToInstrument = getPropertyNames(object);
                    else propertiesToInstrument = logSettings.propertiesToInstrument;

                    for (var i = 0; i < propertiesToInstrument.length; i++) {{
                        var propertyName = propertiesToInstrument[i];
                        if (logSettings.excludedProperties.indexOf(propertyName) !== -1) continue;
                        if (logSettings.recursive && logSettings.depth > 0 && isObject(object, propertyName) && propertyName !== "__proto__") {{
                            try {{
                                var newLogSettings = Object.assign({{}}, logSettings);
                                newLogSettings.depth = logSettings.depth - 1;
                                newLogSettings.propertiesToInstrument = [];
                                instrumentObject(object[propertyName], instrumentedName + "." + propertyName, newLogSettings);
                            }} catch (e) {{}}
                        }}
                        try {{
                            instrumentObjectProperty(object, instrumentedName, propertyName, logSettings);
                        }} catch (error) {{
                            if (!String(error && error.message || "").includes("can't redefine non-configurable property")) {{
                                if (OPENWPM_TESTING) console.warn("OpenWPM Chrome JS instrumentation error", instrumentedName, propertyName, error);
                            }}
                        }}
                    }}

                    for (var j = 0; j < (logSettings.nonExistingPropertiesToInstrument || []).length; j++) {{
                        var nonExisting = logSettings.nonExistingPropertiesToInstrument[j];
                        if (logSettings.excludedProperties.indexOf(nonExisting) !== -1) continue;
                        try {{
                            instrumentObjectProperty(object, instrumentedName, nonExisting, logSettings);
                        }} catch (error) {{}}
                    }}
                }}

                function resolveObject(item) {{
                    if (!item) return undefined;
                    if (typeof item.object === "string") return eval(item.object);
                    return item.object;
                }}

                function instrumentJS(requests) {{
                    (requests || []).forEach(function(item) {{
                        try {{
                            instrumentObject(resolveObject(item), item.instrumentedName, item.logSettings || {{}});
                        }} catch (error) {{
                            if (OPENWPM_TESTING) console.warn("OpenWPM: failed to instrument", item, error);
                        }}
                    }});
                }}

                if (OPENWPM_TESTING) window.instrumentJS = instrumentJS;
                instrumentJS(JSInstrumentRequests);
            }} catch (e) {{}}
        }})();
        """

    def _install_js_instrumentation(self) -> None:
        """Inject the JS monitoring script into the current page context (best-effort)."""
        try:
            self.driver.execute_script(self._get_js_instrumentation_code())
        except Exception:
            pass

    def _collect_network(self, visit_id: VisitId) -> None:
        """
        Read the CDP network log via Performance.getEntries (JS) because
        the plain CDP Network domain only provides live events (no history).
        We use the PerformanceResourceTiming API which is always available.
        Additionally, extract DNS timing info from resource timing entries
        and send as 'dns' records to approximate Firefox DNS instrumentation.
        """
        # Prefer Chrome performance logs when available: they contain real
        # Network.* events with redirect linkage and requestIds.
        if self._collect_network_from_performance_logs(visit_id):
            return

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
        request_id_by_url: Dict[str, int] = {}

        for entry in entries:
            url = entry.get("name", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            req_id = hash(f"{visit_id}-{url}") & 0x7FFFFFFF
            request_id_by_url[url] = req_id
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
                "location": "",
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

        for idx, de in enumerate(dns_entries):
            try:
                dns_url = de.get("name", "")
                hostname = urlparse(dns_url).hostname
                if not hostname:
                    continue

                start = de.get("domainLookupStart") or 0
                end = de.get("domainLookupEnd") or 0
                if end > start:
                    linked_request_id = request_id_by_url.get(dns_url)
                    dns_request_id = (
                        linked_request_id
                        if linked_request_id is not None
                        else hash(f"{visit_id}-dns-{dns_url}-{start}-{end}-{idx}") & 0x7FFFFFFF
                    )
                    # Map to dns_responses schema
                    dns_rec = {
                        "request_id": dns_request_id,
                        "browser_id": self.browser_id,
                        "visit_id": visit_id,
                        "hostname": hostname,
                        "addresses": None,
                        "used_address": None,
                        "canonical_name": None,
                        "is_TRR": None,
                        "time_stamp": _now_ts(),
                    }
                    self._send("dns_responses", dns_rec)
            except Exception:
                pass

    def _save_content_types(self) -> Optional[Set[str]]:
        save_content = self.browser_params.save_content
        if save_content is True:
            return None
        if not save_content:
            return set()
        if isinstance(save_content, str):
            return {item.strip().lower() for item in save_content.split(",") if item.strip()}
        return set()

    def _should_save_content_for_resource(self, resource_type: str) -> bool:
        configured = self._save_content_types()
        if configured is None:
            return True
        return resource_type.lower() in configured

    def _normalize_resource_type(
        self, cdp_type: Optional[str], url: str, top_level_url: str
    ) -> str:
        mapped = {
            "document": "main_frame" if url == top_level_url else "sub_frame",
            "script": "script",
            "stylesheet": "stylesheet",
            "image": "image",
            "font": "font",
            "media": "media",
            "xhr": "xmlhttprequest",
            "fetch": "xmlhttprequest",
            "websocket": "websocket",
            "manifest": "web_manifest",
            "other": "other",
        }
        return mapped.get((cdp_type or "other").lower(), (cdp_type or "other").lower())

    def _collect_network_from_performance_logs(self, visit_id: VisitId) -> bool:
        """Use Chrome performance logs for request/response/redirect reconstruction."""
        try:
            perf_logs = self.driver.get_log("performance")
        except Exception:
            return False

        if not perf_logs:
            return False

        top_url = ""
        try:
            top_url = self.driver.current_url
        except Exception:
            pass

        serial = 0
        sent_any = False
        active_ids: Dict[str, int] = {}
        request_meta: Dict[int, Dict[str, Any]] = {}
        pending_responses: Dict[str, Dict[str, Any]] = {}
        pending_capture: Dict[str, bool] = {}
        cache_hits: Set[str] = set()

        def _next_request_id(cdp_request_id: str, url: str) -> int:
            nonlocal serial
            serial += 1
            return hash(f"{visit_id}-{cdp_request_id}-{serial}-{url}") & 0x7FFFFFFF

        for entry in perf_logs:
            try:
                payload = json.loads(entry.get("message", "{}"))
                message = payload.get("message", {})
                method = message.get("method")
                params = message.get("params", {})
            except Exception:
                continue

            if method == "Network.requestServedFromCache":
                request_id = params.get("requestId")
                if request_id:
                    cache_hits.add(request_id)
                continue

            if method == "Network.requestWillBeSent":
                cdp_request_id = params.get("requestId")
                request = params.get("request", {})
                url = request.get("url", "")
                if not cdp_request_id or not url:
                    continue

                redirect_response = params.get("redirectResponse")
                if redirect_response and cdp_request_id in active_ids:
                    old_request_id = active_ids[cdp_request_id]
                    old_meta = request_meta.get(old_request_id, {})
                    old_url = old_meta.get("url") or redirect_response.get("url", "")
                    old_method = old_meta.get("method", "GET")
                    old_headers = redirect_response.get("headers", {})

                    self._send(
                        "http_responses",
                        {
                            "visit_id": visit_id,
                            "browser_id": self.browser_id,
                            "url": old_url,
                            "method": old_method,
                            "response_status": int(redirect_response.get("status", 0) or 0),
                            "response_status_text": redirect_response.get("statusText", ""),
                            "is_cached": False,
                            "headers": _headers_to_pairs_json(old_headers),
                            "request_id": old_request_id,
                            "location": _header_lookup(old_headers, "location") or "",
                            "time_stamp": _now_ts(),
                            "content_hash": None,
                        },
                    )

                    new_request_id = _next_request_id(cdp_request_id, url)
                    active_ids[cdp_request_id] = new_request_id
                    self._send(
                        "http_redirects",
                        {
                            "visit_id": visit_id,
                            "browser_id": self.browser_id,
                            "old_request_url": old_url,
                            "old_request_id": old_request_id,
                            "new_request_url": url,
                            "new_request_id": new_request_id,
                            "extension_session_uuid": None,
                            "event_ordinal": None,
                            "window_id": None,
                            "tab_id": None,
                            "frame_id": None,
                            "response_status": int(redirect_response.get("status", 0) or 0),
                            "response_status_text": redirect_response.get("statusText", ""),
                            "headers": _headers_to_pairs_json(old_headers),
                            "time_stamp": _now_ts(),
                        },
                    )
                elif cdp_request_id not in active_ids:
                    active_ids[cdp_request_id] = _next_request_id(cdp_request_id, url)

                openwpm_request_id = active_ids[cdp_request_id]
                req_headers = request.get("headers", {})
                resource_type = self._normalize_resource_type(params.get("type"), url, top_url)
                request_record: Dict[str, Any] = {
                    "visit_id": visit_id,
                    "browser_id": self.browser_id,
                    "url": url,
                    "top_level_url": top_url,
                    "method": request.get("method", "GET"),
                    "referrer": _header_lookup(req_headers, "referer") or "",
                    "headers": _headers_to_pairs_json(req_headers),
                    "request_id": openwpm_request_id,
                    "resource_type": resource_type,
                    "post_body": request.get("postData"),
                    "post_body_raw": None,
                    "is_XHR": resource_type == "xmlhttprequest",
                    "req_call_stack": None,
                    "frame_id": None,
                    "time_stamp": _now_ts(),
                }

                stack = self._last_js_callstacks.get(url) or self._last_js_callstacks.get(url.split("?")[0])
                if stack:
                    request_record["req_call_stack"] = stack

                request_meta[openwpm_request_id] = {
                    "url": url,
                    "method": request.get("method", "GET"),
                    "resource_type": resource_type,
                }
                self._send("http_requests", request_record)
                sent_any = True
                continue

            if method == "Network.responseReceived":
                cdp_request_id = params.get("requestId")
                if not cdp_request_id or cdp_request_id not in active_ids:
                    continue

                openwpm_request_id = active_ids[cdp_request_id]
                meta = request_meta.get(openwpm_request_id, {})
                response = params.get("response", {})
                headers = response.get("headers", {})
                pending_responses[cdp_request_id] = {
                    "visit_id": visit_id,
                    "browser_id": self.browser_id,
                    "url": meta.get("url") or response.get("url", ""),
                    "method": meta.get("method", "GET"),
                    "response_status": int(response.get("status", 0) or 0),
                    "response_status_text": response.get("statusText", ""),
                    "is_cached": cdp_request_id in cache_hits
                    or bool(response.get("fromDiskCache") or response.get("fromServiceWorker")),
                    "headers": _headers_to_pairs_json(headers),
                    "request_id": openwpm_request_id,
                    "location": _header_lookup(headers, "location") or "",
                    "time_stamp": _now_ts(),
                    "content_hash": None,
                }
                pending_capture[cdp_request_id] = self._should_save_content_for_resource(
                    str(meta.get("resource_type", "other"))
                )
                continue

            if method == "Network.loadingFinished":
                cdp_request_id = params.get("requestId")
                if not cdp_request_id:
                    continue
                response_record = pending_responses.pop(cdp_request_id, None)
                if response_record is None:
                    continue
                should_capture = pending_capture.pop(cdp_request_id, False)
                if should_capture:
                    try:
                        body = self.driver.execute_cdp_cmd(
                            "Network.getResponseBody", {"requestId": cdp_request_id}
                        )
                        body_text = body.get("body", "")
                        body_bytes = (
                            base64.b64decode(body_text)
                            if body.get("base64Encoded", False)
                            else body_text.encode("utf-8")
                        )
                        if len(body_bytes) <= MAX_CONTENT_BYTES:
                            content_hash = hashlib.sha256(body_bytes).hexdigest()
                            response_record["content_hash"] = content_hash
                            self._sock.send(
                                (
                                    "page_content",
                                    (base64.b64encode(body_bytes).decode("ascii"), content_hash),
                                )
                            )
                        else:
                            response_record["content_hash"] = "<skipped>"
                    except Exception:
                        response_record["content_hash"] = None
                self._send("http_responses", response_record)
                continue

            if method == "Network.loadingFailed":
                cdp_request_id = params.get("requestId")
                if not cdp_request_id:
                    continue
                response_record = pending_responses.pop(cdp_request_id, None)
                pending_capture.pop(cdp_request_id, None)
                if response_record is not None:
                    self._send("http_responses", response_record)

        for cdp_request_id, response_record in list(pending_responses.items()):
            pending_capture.pop(cdp_request_id, None)
            self._send("http_responses", response_record)

        return sent_any

    def _collect_dns(self, visit_id: VisitId) -> None:
        """
        Collect DNS resolution data for all hostnames seen on the current page.
        Hostnames are extracted from PerformanceResourceTiming entries and the
        current URL, then resolved via Python's socket module to obtain IP addresses.
        """
        # Gather all resource URLs visible to the page
        try:
            resource_urls: List[str] = self.driver.execute_script(
                """
                var entries = (performance.getEntriesByType('resource') || [])
                    .concat(performance.getEntriesByType('navigation') || []);
                return entries.map(function(e){ return e.name; });
                """
            ) or []
        except Exception:
            resource_urls = []

        # Also include the top-level URL
        try:
            resource_urls.append(self.driver.current_url)
        except Exception:
            pass

        # Extract unique hostnames
        seen_hosts: Set[str] = set()
        for url in resource_urls:
            try:
                host = urlparse(url).hostname
                if host:
                    seen_hosts.add(host)
            except Exception:
                continue

        for hostname in seen_hosts:
            req_id = hash(f"{visit_id}-dns-{hostname}") & 0x7FFFFFFF
            addresses: Optional[str] = None
            used_address: Optional[str] = None
            canonical_name: Optional[str] = None

            try:
                results = socket.getaddrinfo(hostname, None)
                addr_list = list(dict.fromkeys(r[4][0] for r in results))  # unique, ordered
                if addr_list:
                    addresses = ",".join(addr_list)
                    used_address = addr_list[0]
                # Best-effort canonical name via reverse lookup
                try:
                    canonical_name = socket.gethostbyaddr(addr_list[0])[0] if addr_list else hostname
                except Exception:
                    canonical_name = hostname
            except Exception:
                canonical_name = hostname

            dns_rec: Dict[str, Any] = {
                "request_id": req_id,
                "browser_id": self.browser_id,
                "visit_id": visit_id,
                "hostname": hostname,
                "addresses": addresses,
                "used_address": used_address,
                "canonical_name": canonical_name,
                "is_TRR": None,
                "time_stamp": _now_ts(),
            }
            self._send("dns_responses", dns_rec)

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
            self._sock.send((table, _strip_nul_chars(record)))
        except Exception as e:
            logger.debug(
                "BROWSER %i: Failed to send record to storage [%s]: %s",
                self.browser_id, table, e,
            )
