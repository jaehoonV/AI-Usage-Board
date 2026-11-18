import socket
import threading
from datetime import datetime, timezone

import pytest

from ai_usage_board.providers import antigravity as agy


WINDSURF = (
    r"C:\Users\x\AppData\Local\Programs\Windsurf\resources\bin\language_server.exe "
    "--standalone --csrf_token 11111111-1111-1111-1111-111111111111 --app_data_dir windsurf"
)
ANTIGRAVITY = (
    r"C:\Users\x\AppData\Local\Programs\antigravity\resources\bin\language_server.exe "
    "--standalone --override_ide_name antigravity --https_server_port 0 "
    "--csrf_token 0a1b2c3d-0000-4000-8000-00000000abcd --app_data_dir antigravity "
    "--api_server_url https://generativelanguage.googleapis.com"
)


def reply(models, tier="Antigravity Starter Quota", plan="Pro"):
    """A reply shaped like the real one, which carries two different plans."""
    return {
        "userStatus": {
            # Inherited from Windsurf and unrelated to the Google subscription.
            "planStatus": {"planInfo": {"planName": plan, "teamsTier": "TEAMS_TIER_PRO"}},
            "userTier": {"id": "free-tier", "name": tier},
            "cascadeModelConfigData": {"clientModelConfigs": models},
        }
    }


# -- telling the servers apart ---------------------------------------------
def test_only_the_antigravity_server_is_ours():
    assert agy.is_antigravity(ANTIGRAVITY)
    # Same binary, same RPC, another account: Windsurf must not be read as
    # Antigravity or the card would show someone else's quota.
    assert not agy.is_antigravity(WINDSURF)
    assert not agy.is_antigravity("")


@pytest.mark.parametrize(
    "argument",
    [
        "--app_data_dir antigravity",
        "--app_data_dir=antigravity",
        '--app_data_dir "antigravity"',
        # A whole path is just as valid a spelling, and matching the bare
        # string would leave such an install looking like "앱이 꺼져 있음".
        r"--app_data_dir C:\Users\x\AppData\Roaming\antigravity",
        r'--app_data_dir "C:\Program Files\antigravity\"',
        "--app_data_dir /home/x/.config/Antigravity",
    ],
)
def test_the_data_directory_is_recognised_in_every_spelling(argument):
    assert agy.is_antigravity(f"ls.exe --standalone {argument} --x")


@pytest.mark.parametrize(
    "argument",
    [r"--app_data_dir C:\Users\x\windsurf", "--app_data_dir antigravity-old", "--app_data_dir=windsurf"],
)
def test_a_lookalike_directory_is_not_ours(argument):
    assert not agy.is_antigravity(f"ls.exe {argument}")


def test_reads_the_csrf_token_in_either_spelling():
    assert agy.csrf_token(ANTIGRAVITY) == "0a1b2c3d-0000-4000-8000-00000000abcd"
    assert agy.csrf_token("ls.exe --csrf_token=abcd1234 --x") == "abcd1234"
    assert agy.csrf_token("ls.exe --standalone") == ""


# -- the survey ------------------------------------------------------------
def test_parse_survey_accepts_a_lone_object():
    # PowerShell's ConvertTo-Json collapses a one-element array to an object
    # on some hosts; one server must still be found.
    records = agy.parse_survey('{"pid":7,"cmd":"ls.exe","ports":52596}')
    assert records == [{"pid": 7, "cmd": "ls.exe", "ports": [52596]}]


def test_parse_survey_drops_junk_ports_and_unusable_rows():
    records = agy.parse_survey(
        '[{"pid":7,"cmd":"a.exe","ports":[80,"x",0,70000,80]},'
        '{"pid":8,"ports":[1]},'
        '"nonsense"]'
    )
    assert records == [{"pid": 7, "cmd": "a.exe", "ports": [80]}]


def test_parse_survey_survives_empty_output():
    assert agy.parse_survey("") == []
    assert agy.parse_survey("not json") == []


# -- normalising -----------------------------------------------------------
def test_a_missing_remaining_fraction_means_spent_not_unknown():
    # Protobuf JSON omits zero-valued fields, so an exhausted group arrives
    # with no remainingFraction at all. Read as "unknown" it would paint a
    # dead quota as a full one.
    snapshot = agy.normalize(
        reply([{"label": "Claude Opus 4.6 (Thinking)", "quotaInfo": {"resetTime": "2026-11-19T03:24:06Z"}}])
    )
    assert snapshot["windows"][0]["used"] == 100.0


def test_models_sharing_a_quota_collapse_into_one_meter():
    snapshot = agy.normalize(
        reply(
            [
                {"label": "Gemini 3.8 Flash (High)", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}},
                {"label": "Gemini 3.8 Flash (Low)", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}},
                {"label": "Claude Sonnet 4.6 (Thinking)", "quotaInfo": {"resetTime": "2026-11-19T03:24:06Z"}},
                {"label": "GPT-OSS 120B (Medium)", "quotaInfo": {"resetTime": "2026-11-19T03:24:06Z"}},
            ]
        )
    )
    labels = [w["label"] for w in snapshot["windows"]]
    assert labels == ["Gemini", "Claude · GPT-OSS"]
    assert [w["used"] for w in snapshot["windows"]] == [0.0, 100.0]
    assert snapshot["provider"] == "antigravity"
    assert snapshot["note"] == "Antigravity Starter Quota"
    assert snapshot["source"] == "local"
    assert snapshot["error"] is None


def test_the_plan_shown_is_the_antigravity_one_not_the_windsurf_one():
    # The server answers with both. `planStatus.planInfo` is the Windsurf plan
    # this codebase grew out of and reads "Pro" for a free Google account;
    # reading it told a free user they were on Pro.
    models = [{"label": "Gemini", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}}]
    snapshot = agy.normalize(reply(models, tier="Antigravity Starter Quota", plan="Pro"))
    assert snapshot["note"] == "Antigravity Starter Quota"
    assert "Pro" not in snapshot["note"]


def test_a_reply_without_a_tier_falls_back_rather_than_guessing():
    models = [{"label": "Gemini", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}}]
    payload = reply(models)
    del payload["userStatus"]["userTier"]
    # Never fall through to planInfo: a wrong plan is worse than none.
    assert agy.normalize(payload)["note"] == "Antigravity 로컬 연동"


def test_half_spent_quota_becomes_used_percent():
    snapshot = agy.normalize(reply([{"label": "Gemini 3 Pro", "quotaInfo": {"remainingFraction": 0.25}}]))
    assert snapshot["windows"][0]["used"] == 75.0
    assert snapshot["windows"][0]["resetText"] == ""


def test_reset_time_is_shown_in_local_time():
    snapshot = agy.normalize(
        reply([{"label": "Gemini", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}}])
    )
    expected = datetime(2026, 11, 22, 21, 59, 49, tzinfo=timezone.utc).astimezone().strftime("%m/%d %H:%M")
    assert snapshot["windows"][0]["resetText"] == expected


def test_models_without_quota_are_skipped():
    snapshot = agy.normalize(reply([{"label": "Gemini", "quotaInfo": None}, {"label": "X"}]))
    assert snapshot["windows"] == []
    assert snapshot["note"] == "조회 가능한 사용 한도가 없습니다"


def test_an_empty_quota_object_is_not_an_empty_quota():
    # Antigravity lists unreleased models with quotaInfo {} - nothing is being
    # tracked. The zero-field rule must not turn that into "100% 사용".
    snapshot = agy.normalize(
        reply(
            [
                {"label": "Gemini 3.8 Flash (High)", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}},
                {"label": "Gemini 4 Carbon", "quotaInfo": {}},
                {"label": "Gemini 4 Argon", "quotaInfo": {}},
            ]
        )
    )
    assert [(w["label"], w["used"]) for w in snapshot["windows"]] == [("Gemini", 0.0)]


def test_two_groups_from_one_house_are_named_by_model():
    snapshot = agy.normalize(
        reply(
            [
                {"label": "Gemini 3.8 Flash (High)", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}},
                {"label": "Gemini 3 Pro", "quotaInfo": {"resetTime": "2026-11-19T03:24:06Z"}},
            ]
        )
    )
    assert [w["label"] for w in snapshot["windows"]] == ["Gemini 3.8 Flash", "Gemini 3 Pro"]


def test_a_reply_without_an_account_asks_for_a_login():
    with pytest.raises(agy.AntigravityLoginRequired):
        agy.normalize({"somethingElse": {}})


@pytest.mark.parametrize("junk", ["oops", 7, [1, 2], True])
def test_a_nested_field_of_the_wrong_type_is_not_a_crash(junk):
    # `(value or {}).get(...)` would raise AttributeError here: a string, a
    # number and a list are all truthy and none of them has .get.
    snapshot = agy.normalize(
        {"userStatus": {"cascadeModelConfigData": junk, "planStatus": junk}}
    )
    assert snapshot["windows"] == []
    assert snapshot["note"] == "조회 가능한 사용 한도가 없습니다"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_an_unreadable_number_is_dropped_rather_than_drawn(value):
    # NaN survives max(0, min(1, x)) as a full quota, because every comparison
    # with it is false. A number we cannot read must not reach a meter.
    snapshot = agy.normalize(reply([{"label": "Gemini", "quotaInfo": {"remainingFraction": value}}]))
    assert snapshot["windows"] == []


# -- the endpoint cache ----------------------------------------------------
@pytest.fixture(autouse=True)
def clean_cache():
    agy.forget()
    yield
    agy.forget()


def test_a_working_endpoint_is_reused_without_another_discovery(monkeypatch):
    discoveries = []

    def discover(cancel=None, budget=agy.DISCOVERY_BUDGET):
        discoveries.append(1)
        return agy.Endpoint("https://127.0.0.1:1/x", "tok", 7), reply([])

    monkeypatch.setattr(agy, "discover", discover)
    monkeypatch.setattr(agy, "request", lambda url, token, timeout=8.0: reply([]))

    agy.read_antigravity()
    agy.read_antigravity()
    # Discovery costs a PowerShell process; once per server lifetime is the point.
    assert len(discoveries) == 1


def test_discovery_hands_back_the_reply_that_proved_the_endpoint(monkeypatch):
    # discover() already made a full GetUserStatus call to pick the port.
    # Asking again a millisecond later is a second round trip for nothing.
    calls = []

    def request(url, token, timeout=8.0):
        calls.append(url)
        return reply([])

    monkeypatch.setattr(agy, "request", request)
    monkeypatch.setattr(
        agy, "survey",
        lambda cancel=None: [{"pid": 1, "cmd": ANTIGRAVITY, "ports": [52596]}],
    )

    agy.read_antigravity()
    assert len(calls) == 1


def test_a_signed_out_account_keeps_the_endpoint(monkeypatch):
    # The server is right there and the token is good; it is the account that
    # is missing. Dropping the endpoint would spawn PowerShell on every retry.
    surveys = []

    def survey(cancel=None):
        surveys.append(1)
        return [{"pid": 1, "cmd": ANTIGRAVITY, "ports": [52596]}]

    def request(url, token, timeout=8.0):
        raise agy.AntigravityLoginRequired("Antigravity 로그인이 필요합니다")

    monkeypatch.setattr(agy, "survey", survey)
    monkeypatch.setattr(agy, "request", request)

    agy._cached = agy.Endpoint("https://127.0.0.1:52596/x", "tok", 1)
    for _ in range(3):
        with pytest.raises(agy.AntigravityLoginRequired):
            agy.read_antigravity()
    assert surveys == []
    assert agy._cached is not None


def test_a_restarted_server_is_found_again(monkeypatch):
    # The port and the token both change on every launch, so the cached
    # endpoint must be dropped the moment it stops answering.
    endpoints = [
        (agy.Endpoint("https://127.0.0.1:1/x", "old", 7), reply([])),
        (agy.Endpoint("https://127.0.0.1:2/x", "new", 8), reply([])),
    ]
    monkeypatch.setattr(agy, "discover", lambda cancel=None, budget=0: endpoints.pop(0))

    def request(url, token, timeout=8.0):
        # The old server is gone, so its port and token answer nothing.
        if token == "old":
            raise agy.AntigravityError("gone")
        return reply([])

    monkeypatch.setattr(agy, "request", request)

    agy.read_antigravity()
    assert agy._cached.token == "old"
    # The next read finds the cached endpoint dead and hunts again.
    agy.read_antigravity()
    assert agy._cached.token == "new"


def test_no_server_running_is_its_own_failure(monkeypatch):
    monkeypatch.setattr(
        agy, "survey", lambda cancel=None: [{"pid": 1, "cmd": WINDSURF, "ports": [1234]}]
    )
    with pytest.raises(agy.AntigravityNotRunning):
        agy.discover()


def test_the_hunt_gives_up_instead_of_multiplying_timeouts(monkeypatch):
    # Four seconds per port is fine for three ports and not for thirty.
    monkeypatch.setattr(
        agy, "survey",
        lambda cancel=None: [{"pid": 1, "cmd": ANTIGRAVITY, "ports": list(range(2000, 2030))}],
    )
    tries = []

    def request(url, token, timeout=8.0):
        tries.append(url)
        raise agy.AntigravityError("refused")

    monkeypatch.setattr(agy, "request", request)
    with pytest.raises(agy.AntigravityError):
        agy.discover(budget=0.0)
    assert tries == []


def test_shutdown_stops_the_hunt(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(agy, "survey", lambda cancel=None: pytest.fail("should not run"))
    with pytest.raises(InterruptedError):
        agy.read_antigravity(cancel=cancel)


# -- the network boundary --------------------------------------------------
def test_an_address_off_this_machine_is_refused_before_anything_is_sent():
    # The TLS context skips certificate checks, which is only defensible while
    # the address cannot be anywhere but here.
    for url in (
        "https://example.com/x",
        "https://localhost/x",  # a name, and a name can be repointed
        "file:///etc/passwd",
    ):
        with pytest.raises(agy.AntigravityError):
            agy.request(url, "tok", timeout=1)


def test_a_redirect_never_carries_the_token_elsewhere():
    """urllib copies custom headers onto a redirect, so this must not follow one."""
    stolen = {}
    first = socket.socket()
    first.bind(("127.0.0.1", 0))
    first.listen(1)
    second = socket.socket()
    second.bind(("127.0.0.1", 0))
    second.listen(1)

    def redirector():
        client, _ = first.accept()
        client.recv(4096)
        body = (
            f"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{second.getsockname()[1]}/taken\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        client.sendall(body.encode())
        client.close()

    def thief():
        try:
            second.settimeout(2)
            client, _ = second.accept()
            stolen["request"] = client.recv(8192).decode("utf-8", "replace")
            client.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            client.close()
        except OSError:
            pass

    threading.Thread(target=redirector, daemon=True).start()
    catcher = threading.Thread(target=thief, daemon=True)
    catcher.start()
    try:
        with pytest.raises(agy.AntigravityError):
            agy.request(f"http://127.0.0.1:{first.getsockname()[1]}/x", "SECRET", timeout=3)
        catcher.join(timeout=3)
        assert "SECRET" not in stolen.get("request", "")
        assert "request" not in stolen
    finally:
        first.close()
        second.close()


def test_a_mangled_reply_does_not_escape_as_an_http_client_error():
    # http.client's own exceptions are not OSError, so without catching that
    # family a bad status line leaves this module as an unrelated crash.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def garbage():
        client, _ = listener.accept()
        client.recv(4096)
        client.sendall(b"NOT EVEN HTTP\r\n\r\n")
        client.close()

    threading.Thread(target=garbage, daemon=True).start()
    try:
        with pytest.raises(agy.AntigravityError):
            agy.request(f"http://127.0.0.1:{listener.getsockname()[1]}/x", "tok", timeout=3)
    finally:
        listener.close()


# -- reading with the app closed -------------------------------------------
def test_the_launch_arguments_carry_what_the_backend_needs(monkeypatch, tmp_path):
    # Measured: without --enable_sidecars or --override_ide_version the reply
    # comes back with the Gemini group's quota missing, which a card reads as
    # "no limit". Both must be on every launch.
    binary = tmp_path / "resources" / "bin" / "language_server.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ")
    monkeypatch.setattr(agy, "server_binary", lambda: binary)
    monkeypatch.setattr(agy, "app_version", lambda _b: "2.32.1")

    seen = {}

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            seen["terminated"] = True

        def wait(self, timeout=None):
            return 0

    def popen(command, **kwargs):
        seen["command"] = command
        return FakeProcess()

    monkeypatch.setattr(agy.subprocess, "Popen", popen)
    monkeypatch.setattr(
        agy, "request",
        lambda url, token, timeout=8.0: reply(
            [{"label": "Gemini", "quotaInfo": {"remainingFraction": 1, "resetTime": "2026-11-22T21:59:49Z"}}]
        ),
    )
    monkeypatch.setattr(agy, "SETTLE_POLLS", 0)

    snapshot = agy.launch_and_read(timeout=5)
    assert snapshot["windows"][0]["label"] == "Gemini"
    assert "--enable_sidecars" in seen["command"]
    assert "--override_ide_version" in seen["command"]
    assert seen["command"][seen["command"].index("--override_ide_version") + 1] == "2.32.1"
    assert "--standalone" in seen["command"]
    # The server we started must not outlive the question we asked it.
    assert seen.get("terminated") is True


def test_without_a_readable_version_it_says_the_app_is_closed(monkeypatch, tmp_path):
    binary = tmp_path / "language_server.exe"
    binary.write_bytes(b"MZ")
    monkeypatch.setattr(agy, "server_binary", lambda: binary)
    monkeypatch.setattr(agy, "app_version", lambda _b: "")
    monkeypatch.setattr(agy.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    with pytest.raises(agy.AntigravityNotRunning):
        agy.launch_and_read(timeout=1)


def test_no_installed_app_is_not_a_crash(monkeypatch):
    monkeypatch.setattr(agy, "server_binary", lambda: None)
    with pytest.raises(agy.AntigravityNotRunning):
        agy.launch_and_read(timeout=1)
