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


class FakeProcess:
    pid = 4242

    def __init__(self):
        self.running = True
        self.terminated = False

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated = True
        self.running = False

    def kill(self):
        self.running = False

    def wait(self, timeout=None):
        return 0


def test_a_browser_that_never_got_ready_is_not_left_running(tmp_path, monkeypatch):
    process = FakeProcess()
    monkeypatch.setattr(chrome_session, "find_browser", lambda: Path("chrome.exe"))
    monkeypatch.setattr(chrome_session.subprocess, "Popen", lambda *_a, **_k: process)

    def never_ready(self, _port_file, _debug):
        raise BrowserError("BrowserStartTimeout")

    monkeypatch.setattr(BrowserSession, "_await_ready", never_ready)
    session = BrowserSession(tmp_path / "profile")
    with pytest.raises(BrowserError, match="BrowserStartTimeout"):
        session.start(minimized=True, landing="https://claude.ai/settings/usage")
    assert process.terminated


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


def test_only_top_level_browser_frames_count_as_windows():
    assert chrome_session.is_browser_frame(0x06CF0000)
    # A popup, even one with a caption, is not a browser window.
    assert not chrome_session.is_browser_frame(0x96000000)
    assert not chrome_session.is_browser_frame(0x84000000)


DESKTOPS = [
    (0, 0, 2560, 1600),
    # A monitor left of the primary one gives the desktop negative x and y.
    (-1920, -10, 4480, 1600),
]


@pytest.mark.parametrize("desktop", DESKTOPS)
def test_parked_window_sits_wholly_off_the_desktop(desktop):
    width, height = 1200, 900
    x, y = chrome_session.parking_spot(desktop, width)
    parked = (x, y, x + width, y + height)
    assert not chrome_session.overlaps(parked, desktop)
    assert chrome_session.overlaps((10, 10, 1000, 900), desktop)


class ScriptedSocket:
    """Answers each DevTools command; an event is sent ahead of every answer."""

    created = []

    def __init__(self, address, timeout=None):
        self.address = address
        self.sent = []
        self.queue = []
        self.closed = False
        ScriptedSocket.created.append(self)

    def send_json(self, value):
        self.sent.append(value)
        self.queue.append({"method": "Page.frameNavigated"})  # no id: must be skipped
        self.queue.append({"id": value["id"], **ScriptedSocket.answers(value)})

    def recv_json(self):
        return self.queue.pop(0)

    def close(self):
        self.closed = True


def script(monkeypatch, answers):
    ScriptedSocket.created = []
    ScriptedSocket.answers = staticmethod(answers)
    monkeypatch.setattr(chrome_session, "WebSocket", ScriptedSocket)
    session = BrowserSession(Path("profile"))
    session.port = 9222
    return session


PAGE = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/x"}


def test_isolated_evaluation_walks_frame_then_world_then_evaluate(monkeypatch):
    def answers(message):
        method = message["method"]
        if method == "Page.getFrameTree":
            return {"result": {"frameTree": {"frame": {"id": "F1"}}}}
        if method == "Page.createIsolatedWorld":
            return {"result": {"executionContextId": 7}}
        if method == "Runtime.evaluate":
            return {"result": {"result": {"value": 42}}}
        raise AssertionError(method)

    session = script(monkeypatch, answers)
    result = session.evaluate_isolated("1 + 41", PAGE)

    assert result["result"]["value"] == 42
    (socket,) = ScriptedSocket.created
    assert socket.address == PAGE["webSocketDebuggerUrl"]
    assert socket.closed
    sent = {m["method"]: m for m in socket.sent}
    assert [m["id"] for m in socket.sent] == [1, 2, 3]
    assert sent["Page.createIsolatedWorld"]["params"]["frameId"] == "F1"
    assert sent["Runtime.evaluate"]["params"]["contextId"] == 7
    assert sent["Runtime.evaluate"]["params"]["expression"] == "1 + 41"


def test_isolated_evaluation_without_a_frame_id_is_a_browser_error(monkeypatch):
    def answers(message):
        return {"result": {"frameTree": {"frame": {}}}}

    session = script(monkeypatch, answers)
    with pytest.raises(BrowserError, match="FrameNotFound"):
        session.evaluate_isolated("1", PAGE)
    assert ScriptedSocket.created[0].closed


def test_isolated_evaluation_without_a_context_is_a_browser_error(monkeypatch):
    def answers(message):
        if message["method"] == "Page.getFrameTree":
            return {"result": {"frameTree": {"frame": {"id": "F1"}}}}
        return {"result": {}}

    session = script(monkeypatch, answers)
    with pytest.raises(BrowserError, match="IsolatedWorldFailed"):
        session.evaluate_isolated("1", PAGE)
    assert ScriptedSocket.created[0].closed
