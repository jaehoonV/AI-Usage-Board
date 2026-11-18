import json

from ai_usage_board import config
from ai_usage_board.core.models import order_services


def load(tmp_path, monkeypatch, stored):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    return config.load_settings()


def test_hidden_defaults_to_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "missing.json")
    first = config.load_settings()
    assert first["hidden"] == []
    # Each load gets its own list, never the shared default.
    first["hidden"].append("codex")
    assert config.load_settings()["hidden"] == []
    assert config.DEFAULT_SETTINGS["hidden"] == []


def test_hidden_keeps_strings_and_drops_junk(tmp_path, monkeypatch):
    settings = load(tmp_path, monkeypatch, {"hidden": ["codex", 3, None, "", "codex", "x" * 40, "gemini"]})
    assert settings["hidden"] == ["codex", "gemini"]


def test_hidden_must_be_a_list(tmp_path, monkeypatch):
    assert load(tmp_path, monkeypatch, {"hidden": "codex"})["hidden"] == []


def test_order_skips_hidden_services():
    visible = ["claude", "gemini"]
    assert order_services(["gemini", "codex"], visible) == ["gemini", "claude"]
    assert order_services([], []) == []


def test_strip_defaults_off_and_only_accepts_a_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "missing.json")
    assert config.load_settings()["strip"] is False
    assert load(tmp_path, monkeypatch, {"strip": True})["strip"] is True
    assert load(tmp_path, monkeypatch, {"strip": "yes"})["strip"] is True
    assert load(tmp_path, monkeypatch, {"strip": None})["strip"] is False


def test_strip_position_is_kept_only_when_it_is_a_number(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "missing.json")
    assert config.load_settings()["strip_x"] is None
    kept = load(tmp_path, monkeypatch, {"strip_x": 1400, "strip_y": 980.6})
    assert (kept["strip_x"], kept["strip_y"]) == (1400, 980)
    for junk in (True, "1400", None, [1400]):
        assert load(tmp_path, monkeypatch, {"strip_x": junk})["strip_x"] is None


def test_unknown_settings_keys_are_dropped(tmp_path, monkeypatch):
    settings = load(tmp_path, monkeypatch, {"dense": True, "height": 480, "pinned": False})
    assert "dense" not in settings and "height" not in settings
    assert settings["pinned"] is False


def test_a_byte_order_mark_does_not_wipe_every_setting(tmp_path, monkeypatch):
    # PowerShell's Set-Content -Encoding utf8 writes one, and json.loads
    # refuses it; reading as utf-8 lost the window position and every toggle.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"x": 6048, "y": 181, "strip": True}), encoding="utf-8-sig")
    monkeypatch.setattr(config, "SETTINGS_FILE", path)
    settings = config.load_settings()
    assert (settings["x"], settings["y"]) == (6048, 181)
    assert settings["strip"] is True


# -- window placement across monitors --------------------------------------
# The arrangement this was found on: two monitors sitting higher than the
# primary one, so the desktop starts at y = -19, not 0.
DESKTOP = (0, -19, 6400, 1600)


def test_a_window_can_reach_the_top_of_a_raised_monitor():
    from ai_usage_board.ui.win32 import clamp_to

    # Clamping at y=0 would stop 19px below this monitor's top edge.
    assert clamp_to(DESKTOP, 5207, -500, 342, 951) == (5207, -19)
    assert clamp_to(DESKTOP, 5207, -19, 342, 951) == (5207, -19)


def test_a_window_stays_on_the_desktop():
    from ai_usage_board.ui.win32 import clamp_to

    assert clamp_to(DESKTOP, 99999, 0, 342, 951) == (6400 - 342, 0)
    assert clamp_to(DESKTOP, -500, 0, 342, 951) == (0, 0)
    assert clamp_to(DESKTOP, 100, 99999, 342, 951) == (100, 1600 - 951)


def test_a_position_on_a_second_monitor_is_not_dragged_back_to_the_first():
    from ai_usage_board.ui.win32 import clamp_to

    # Clamping against the primary monitor alone (2560 wide) moved it here.
    assert clamp_to(DESKTOP, 6048, 181, 342, 951)[0] == 6048


def test_resources_are_found():
    """The tray icon, page reader and guide all come from RESOURCE_DIR."""
    from ai_usage_board.config import RESOURCE_DIR

    for name in ("app.ico", "extractor.js", "connect-guide.html"):
        assert (RESOURCE_DIR / name).is_file(), name
