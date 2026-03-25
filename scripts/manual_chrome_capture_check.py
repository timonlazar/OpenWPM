"""Manual check for ChromeInstrumentation _collect_network using CDP log mocks.

Run with:
python -c "import sys; sys.path.insert(0, '.'); exec(open('scripts/manual_chrome_capture_check.py').read())"
"""

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openwpm.deploy_browsers.chrome_instrumentation import ChromeInstrumentation


def _perf_message(method, params):
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


class MockDriver:
    def __init__(self, current_url):
        self.current_url = current_url
        self._perf_logs = [
            _perf_message(
                "Network.requestWillBeSent",
                {
                    "requestId": "req-1",
                    "request": {
                        "url": "https://example.com/static/app.js",
                        "method": "GET",
                        "headers": {"Referer": "https://example.com/"},
                    },
                    "type": "Script",
                },
            ),
            _perf_message(
                "Network.responseReceived",
                {
                    "requestId": "req-1",
                    "response": {
                        "url": "https://example.com/static/app.js",
                        "status": 200,
                        "statusText": "OK",
                        "headers": {"Content-Type": "application/javascript"},
                    },
                },
            ),
            _perf_message("Network.loadingFinished", {"requestId": "req-1"}),
        ]

    def get_log(self, kind):
        return self._perf_logs if kind == "performance" else []

    def execute_cdp_cmd(self, method, _params):
        if method == "Network.getResponseBody":
            data = b"console.log('hello from cdp mock');"
            return {"base64Encoded": True, "body": base64.b64encode(data).decode("ascii")}
        return {}

    def execute_script(self, *_args, **_kwargs):
        return []


class MockSocket:
    def __init__(self):
        self.records = []

    def connect(self, host, port):
        return None

    def send(self, msg):
        self.records.append(msg)
        print(f"SENT: {msg[0]} -> {json.dumps(msg[1], default=str)[:400]}")


def run_manual_check():
    instr = object.__new__(ChromeInstrumentation)
    instr.driver = MockDriver(current_url="https://example.com/")
    instr.browser_params = SimpleNamespace(http_instrument=True, js_instrument=True, browser_id=1, save_content=True)
    instr.manager_params = SimpleNamespace()
    instr.browser_id = 1
    instr._visit_id = None
    instr._last_js_callstacks = {}
    instr._sock = MockSocket()

    instr._collect_network(1)

    http_requests = [rec for table, rec in instr._sock.records if table == "http_requests"]
    http_responses = [rec for table, rec in instr._sock.records if table == "http_responses"]
    page_contents = [rec for table, rec in instr._sock.records if table == "page_content"]

    print("\n" + "=" * 80)
    print("CHROME INSTRUMENTATION VERIFICATION RESULTS")
    print("=" * 80)
    print(f"HTTP Requests:  {len(http_requests)}")
    print(f"HTTP Responses: {len(http_responses)}")
    print(f"Page Content:   {len(page_contents)}")
    print("Expected: >=1 request, >=1 response, >=1 page_content")
    print("=" * 80)

if __name__ == '__main__':
    run_manual_check()
