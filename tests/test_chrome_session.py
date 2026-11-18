import socket
from pathlib import Path

import pytest

from ai_usage_board.providers import chrome_session
from ai_usage_board.providers.chrome_session import BrowserError, BrowserSession, browser_command

AWAKE = (
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
)


def features(command):
    flags = [flag for flag in command if flag.startswith("--disable-features=")]
    # Chrome honours only the last one, so there must be exactly one.
    assert len(flags) == 1
    return flags[0].split("=", 1)[1].split(",")


def test_reader_keeps_its_hidden_page_awake():
    command = browser_command(Path("chrome.exe"), Path("profile"), "https://claude.ai/settings/usage",
                              minimized=True, debug=True)
    assert all(flag in command for flag in AWAKE)
    assert {"Translate", "MediaRouter", "IntensiveWakeUpThrottling"} <= set(features(command))


def test_sign_in_window_stays_an_ordinary_browser():
    command = browser_command(Path("chrome.exe"), Path("profile"), "https://claude.ai/login",
                              minimized=False, debug=False)
    assert not any(flag in command for flag in AWAKE)
    assert not any(flag.startswith("--remote-debugging") for flag in command)
    assert "IntensiveWakeUpThrottling" not in features(command)


def test_a_page_that_never_answers_is_a_browser_error(monkeypatch):
    class Silent:
        def __init__(self, *_args, **_kwargs):
            pass

        def send_json(self, _value):
            pass

        def recv_json(self):
            raise socket.timeout("timed out")

        def close(self):
            pass

    monkeypatch.setattr(chrome_session, "WebSocket", Silent)
    session = BrowserSession(Path("profile"))
    session.port = 9222
    session.socket_url = "ws://127.0.0.1:9222/devtools/browser/x"
    with pytest.raises(BrowserError, match="BrowserReplyTimeout"):
        session.call("Runtime.evaluate")
