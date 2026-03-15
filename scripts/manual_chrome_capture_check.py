"""Manual check for ChromeInstrumentation _collect_network using mocks.

Run with: python scripts/manual_chrome_capture_check.py

This script constructs a ChromeInstrumentation instance without running its
constructor, injects a mock driver with selenium-wire-like `requests`, and a
mock socket that captures messages sent by _send. It then runs
_collect_network(visit_id) and prints the recorded messages.
"""
from types import SimpleNamespace
import base64
import json
import importlib.util
import sys
from pathlib import Path

# Load the chrome_instrumentation module dynamically but strip selenium import
ci_path = Path(__file__).resolve().parents[1] / "openwpm" / "deploy_browsers" / "chrome_instrumentation.py"
ci_src = ci_path.read_text(encoding="utf-8")
# Remove or neutralize `from selenium.webdriver import Chrome` which requires selenium installed
ci_src = ci_src.replace("from selenium.webdriver import Chrome", "# selenium import removed for mock testing")
# Patch relative imports so the module can be exec'd standalone
ci_src = ci_src.replace("from ..config import BrowserParamsInternal, ManagerParamsInternal", "import openwpm.config as _cfg\nBrowserParamsInternal = _cfg.BrowserParamsInternal\nManagerParamsInternal = _cfg.ManagerParamsInternal")
ci_src = ci_src.replace("from ..socket_interface import ClientSocket", "import openwpm.socket_interface as _sock_if\nClientSocket = _sock_if.ClientSocket")
ci_src = ci_src.replace("from ..types import VisitId", "import openwpm.types as _tmod\nVisitId = _tmod.VisitId")

spec = importlib.util.spec_from_loader("ci_mock", loader=None)
ci_mod = importlib.util.module_from_spec(spec)
exec(compile(ci_src, str(ci_path), 'exec'), ci_mod.__dict__)
ChromeInstrumentation = ci_mod.ChromeInstrumentation

# Create mock response/request objects similar to selenium-wire's objects
class MockResponse:
    def __init__(self, status_code=200, headers=None, body=None, text=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body
        self.raw_body = body
        self.text = text if text is not None else (body.decode('utf-8') if isinstance(body, (bytes, bytearray)) else None)

class MockRequest:
    def __init__(self, url, method='GET', headers=None, response=None, resource_type='script', body=None, is_xhr=False):
        self.url = url
        self.method = method
        self.headers = headers or {}
        self.response = response
        self.resource_type = resource_type
        self.body = body
        self.is_xhr = is_xhr

class MockDriver:
    def __init__(self, current_url, requests):
        self.current_url = current_url
        self.requests = requests
    def execute_cdp_cmd(self, *args, **kwargs):
        return {}
    def execute_script(self, *args, **kwargs):
        return []

class MockSocket:
    def __init__(self):
        self.records = []
    def connect(self, host, port):
        pass
    def send(self, msg):
        # Simply store message for inspection
        self.records.append(msg)
        print(f"SENT: {msg[0]} -> {json.dumps(msg[1], default=str)[:500]}")

def run_manual_check():
    # Build mock selenium-wire requests
    # 1) JS resource with body
    js_body = b"console.log('hello world'); var x = 1;"
    resp_js = MockResponse(status_code=200, headers={'Content-Type': 'application/javascript'}, body=js_body)
    req_js = MockRequest(url='https://example.com/static/app.js', method='GET', headers={'Referer': 'https://example.com'}, response=resp_js, resource_type='script', body=None)

    # 2) Redirect response (relative Location)
    resp_redir = MockResponse(status_code=302, headers={'Location': '/redirected'}, body=b'')
    req_redir = MockRequest(url='https://example.com/login', method='GET', headers={}, response=resp_redir)

    # 3) Target request for redirect
    resp_target = MockResponse(status_code=200, headers={'Content-Type': 'text/html'}, body=b'<html>OK</html>')
    req_target = MockRequest(url='https://example.com/redirected', method='GET', headers={}, response=resp_target)

    mock_driver = MockDriver(current_url='https://example.com/', requests=[req_js, req_redir, req_target])

    # Create an instance of ChromeInstrumentation without calling __init__
    instr = object.__new__(ChromeInstrumentation)
    instr.driver = mock_driver
    instr.browser_params = SimpleNamespace(http_instrument=True, js_instrument=True, browser_id=1)
    instr.manager_params = SimpleNamespace()
    instr.browser_id = 1
    instr._visit_id = None
    instr._last_js_callstacks = {}
    instr._sock = MockSocket()

    # Call collect_network; give a visit id 1
    try:
        instr._collect_network(1)
    except Exception as e:
        print('Error during _collect_network:', e)

    # Print summary of messages collected
    print('\nSummary of messages captured by MockSocket:')
    for table, rec in instr._sock.records:
        print(f'- {table}: {rec}')

if __name__ == '__main__':
    run_manual_check()
