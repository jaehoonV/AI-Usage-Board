import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from ai_usage_board.core.store import UsageStore
from ai_usage_board.bridge.server import BridgeServer


class FakeAccounts:
    def status(self):
        return {"claude": "connected"}

    def errors(self):
        return {"claude": ""}

    def command(self, provider):
        return {"show": 0, "refresh": 0, "close": False}


def snapshot(provider="claude", used=50.0):
    return {
        "provider": provider,
        "windows": [{"label": "주간", "used": used, "resetText": ""}],
        "note": "",
        "updatedAt": time.time(),
        "source": "account",
        "error": None,
    }


def test_store_round_trips_the_latest_reading(tmp_path):
    store = UsageStore(tmp_path / "cache.json")
    store.update(snapshot(used=80))
    store.update(snapshot(used=72))

    window = store.snapshot()["claude"]["windows"][0]
    assert window["used"] == 72
    # A window holds exactly what the provider reported - nothing derived.
    assert set(window) == {"label", "used", "resetText"}

    reloaded = UsageStore(tmp_path / "cache.json")
    assert reloaded.snapshot()["claude"]["windows"][0]["used"] == 72


def test_store_failure_keeps_last_numbers(tmp_path):
    store = UsageStore(tmp_path / "cache.json")
    store.update(snapshot(used=40))
    store.fail("claude", "계정 연결 필요", source="account", error_type="login_required")
    item = store.snapshot()["claude"]
    assert item["error"] == "계정 연결 필요"
    assert item["windows"][0]["used"] == 40


@pytest.fixture()
def server(tmp_path):
    store = UsageStore(tmp_path / "cache.json")
    instance = BridgeServer(store, FakeAccounts(), "a" * 64, port=0)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, store
    instance.shutdown()
    instance.server_close()


def call(instance, path, *, token=None, origin=None, host=None, payload=None):
    port = instance.server_port
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
    )
    request.add_header("Host", host or f"127.0.0.1:{port}")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if origin:
        request.add_header("Origin", origin)
    return urllib.request.urlopen(request, timeout=3)


def test_bridge_requires_token(server):
    instance, _ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(instance, "/snapshot")
    assert caught.value.code == 403
    assert call(instance, "/snapshot", token="a" * 64).status == 200


def test_bridge_rejects_wrong_host_header(server):
    instance, _ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(instance, "/snapshot", token="a" * 64, host="evil.example")
    assert caught.value.code == 403


@pytest.mark.parametrize("origin", ["https://claude.ai", "chrome-extension://" + "a" * 32, "null"])
def test_bridge_rejects_any_browser_origin(server, origin):
    instance, _ = server
    with pytest.raises(urllib.error.HTTPError) as caught:
        call(instance, "/snapshot", token="a" * 64, origin=origin)
    assert caught.value.code == 403


def test_bridge_usage_post_validates(server):
    instance, store = server
    assert call(instance, "/usage", token="a" * 64, payload=snapshot()).status == 200
    assert store.snapshot()["claude"]["windows"][0]["used"] == 50

    with pytest.raises(urllib.error.HTTPError) as caught:
        call(instance, "/usage", token="a" * 64, payload={"provider": "nope", "windows": []})
    assert caught.value.code == 400


def test_health_is_public_but_tokenless_routes_are_not(server):
    instance, _ = server
    body = json.load(call(instance, "/health"))
    assert body["app"] == "AI Usage Board"
