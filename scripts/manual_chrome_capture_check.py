"""Manual check for ChromeInstrumentation _collect_network using mocks.

Run with: python -c "import sys; sys.path.insert(0, '.'); exec(open('scripts/manual_chrome_capture_check.py').read())"

This script constructs a ChromeInstrumentation instance without running its
constructor, injects a mock driver with selenium-wire-like `requests`, and a
mock socket that captures messages sent by _send. It then runs
_collect_network(visit_id) and prints the recorded messages.
"""
from types import SimpleNamespace
import base64
import json
import sys
from pathlib import Path

# Add project root to path so we can import openwpm
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Now we can import the actual modules
from openwpm.config import BrowserParamsInternal, ManagerParamsInternal
from openwpm.socket_interface import ClientSocket
from openwpm.types import VisitId
from openwpm.deploy_browsers.chrome_instrumentation import ChromeInstrumentation

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
    # 1) JS resource with body (using resource_type='script' for Chrome detection)
    js_body = b"console.log('hello world'); var x = 1;"
    resp_js = MockResponse(status_code=200, headers={'Content-Type': 'application/javascript'}, body=js_body)
    req_js = MockRequest(url='https://example.com/static/app.js', method='GET', headers={'Referer': 'https://example.com'}, response=resp_js, resource_type='script', body=None)

    # 2) Redirect response (relative Location, status 302)
    resp_redir = MockResponse(status_code=302, headers={'Location': '/redirected'}, body=b'')
    req_redir = MockRequest(url='https://example.com/login', method='GET', headers={}, response=resp_redir)

    # 3) Target request for redirect (should be linked by normalized URL matching)
    resp_target = MockResponse(status_code=200, headers={'Content-Type': 'text/html'}, body=b'<html>OK</html>')
    req_target = MockRequest(url='https://example.com/redirected', method='GET', headers={}, response=resp_target)

    mock_driver = MockDriver(current_url='https://example.com/redirected', requests=[req_js, req_redir, req_target])

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
        import traceback
        traceback.print_exc()

    # Print summary of messages collected
    print('\n' + '='*80)
    print('CHROME INSTRUMENTATION VERIFICATION RESULTS')
    print('='*80)

    http_requests = [rec for table, rec in instr._sock.records if table == 'http_requests']
    http_responses = [rec for table, rec in instr._sock.records if table == 'http_responses']
    http_redirects = [rec for table, rec in instr._sock.records if table == 'http_redirects']
    page_contents = [rec for table, rec in instr._sock.records if table == 'page_content']

    print(f'\n✓ HTTP Requests ({len(http_requests)}):')
    for rec in http_requests:
        print(f'  - {rec.get("url", "?")} [resource_type: {rec.get("resource_type")}]')

    print(f'\n✓ HTTP Responses ({len(http_responses)}):')
    for rec in http_responses:
        status = rec.get("response_status", "?")
        has_hash = "YES" if rec.get("content_hash") else "NO"
        print(f'  - {rec.get("url", "?")} [status: {status}, has_content: {has_hash}]')

    print(f'\n✓ HTTP Redirects ({len(http_redirects)}):')
    for rec in http_redirects:
        print(f'  - {rec.get("old_request_url", "?")} -> {rec.get("new_request_url", "?")}')

    print(f'\n✓ Page Content / JS Bodies ({len(page_contents)}):')
    for rec in page_contents:
        if isinstance(rec, tuple):
            b64, chash = rec
            print(f'  - Content hash: {chash}, Base64 size: {len(b64)} chars')
        else:
            print(f'  - {rec}')

    print('\n' + '='*80)
    print('SUMMARY: Expected 3 http_requests, 1 redirect, 1 JS body capture')
    print('='*80)

if __name__ == '__main__':
    run_manual_check()
