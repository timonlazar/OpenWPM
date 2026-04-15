from unittest.mock import patch

from openwpm.commands.cookie_consent import AcceptCookieConsentCommand


class _FakeWebDriver:
    current_url = "https://www.google.com/"


def test_successful_click_does_not_emit_timeout_after_click(capsys):
    command = AcceptCookieConsentCommand()

    with patch.object(command, "CMP_SELECTORS", ["//button"]), patch.object(
        command, "try_click_fast", return_value=True
    ), patch(
        "openwpm.commands.cookie_consent.time.monotonic", side_effect=[0.0, 0.1, 0.2]
    ):
        command.execute(_FakeWebDriver(), None, None, None)

    out = capsys.readouterr().out
    assert "CMP selector matched" in out
    assert "TIMEOUT after" not in out


def test_search_timeout_still_fires_when_no_match(capsys):
    command = AcceptCookieConsentCommand()

    with patch.object(command, "CMP_SELECTORS", ["//button"]), patch.object(
        command, "try_click_fast", return_value=False
    ), patch(
        "openwpm.commands.cookie_consent.generate_xpaths", return_value=[]
    ), patch(
        "openwpm.commands.cookie_consent.time.monotonic", side_effect=[0.0, 11.0]
    ):
        command.execute(_FakeWebDriver(), None, None, None)

    out = capsys.readouterr().out
    assert "TIMEOUT during CMP selectors" in out

