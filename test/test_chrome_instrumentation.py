from types import SimpleNamespace
from typing import cast

from openwpm.deploy_browsers.chrome_instrumentation import ChromeInstrumentation
from openwpm.types import VisitId


class _DriverFallbackMock:
    current_url = "https://top.example/"

    def execute_script(self, script):
        if "getEntriesByType('resource')" in script and "navigation" in script:
            return [
                {
                    "name": "https://cdn.example/app.js",
                    "initiatorType": "script",
                    "transferSize": 42,
                    "responseStatus": 200,
                    "duration": 10,
                    "startTime": 1,
                }
            ]
        if "redirectCount" in script:
            return 1
        if "domainLookupStart" in script and "domainLookupEnd" in script:
            return [
                {
                    "name": "https://cdn.example/app.js",
                    "domainLookupStart": 1,
                    "domainLookupEnd": 2,
                }
            ]
        return []


def _make_instr(driver, callstack_instrument=False):
    instr = object.__new__(ChromeInstrumentation)
    instr.driver = driver
    instr.browser_id = 1
    instr.browser_params = SimpleNamespace(
        callstack_instrument=callstack_instrument,
        cleaned_js_instrument_settings=[],
        save_content=False,
    )
    instr.manager_params = SimpleNamespace(testing=False)
    instr._last_js_callstacks = {}
    instr._prepared_window_handles = set()
    instr._sock = None
    sent = []
    instr._send = lambda table, record: sent.append((table, record))
    return instr, sent


def test_collect_network_fallback_uses_pair_header_encoding():
    instr, sent = _make_instr(_DriverFallbackMock())
    instr._collect_network_from_performance_logs = lambda _visit_id: False

    instr._collect_network(visit_id=cast(VisitId, 1))

    rows_by_table = {}
    for table, rec in sent:
        rows_by_table.setdefault(table, []).append(rec)

    assert rows_by_table["http_requests"][0]["headers"] == "[]"
    assert rows_by_table["http_responses"][0]["headers"] == "[]"
    assert rows_by_table["http_redirects"][0]["headers"] == "[]"


def test_collect_js_events_emits_callstacks_only_when_enabled():
    class Driver:
        current_url = "https://example.com/"

        def execute_script(self, _script):
            return [
                {
                    "symbol": "window.fetch",
                    "operation": "call",
                    "arguments": ["https://example.com/api"],
                    "call_stack": "stacktrace",
                }
            ]

    instr_disabled, sent_disabled = _make_instr(Driver(), callstack_instrument=False)
    instr_disabled._collect_js_events(visit_id=cast(VisitId, 1))
    assert any(table == "javascript" for table, _ in sent_disabled)
    assert not any(table == "callstacks" for table, _ in sent_disabled)

    instr_enabled, sent_enabled = _make_instr(Driver(), callstack_instrument=True)
    instr_enabled._collect_js_events(visit_id=cast(VisitId, 1))
    assert any(table == "callstacks" for table, _ in sent_enabled)

