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

    for name in ("app.ico", "extractor.js", "connect-guide.html", "contact.html"):
        assert (RESOURCE_DIR / name).is_file(), name


def test_issue_link_carries_only_the_version():
    from urllib.parse import parse_qs, urlsplit

    from ai_usage_board import REPO_URL, __version__

    url = config.new_issue_url()
    parts = urlsplit(url)
    assert url.startswith(f"{REPO_URL}/issues/new?")
    assert list(parse_qs(parts.query)) == ["body"]
    body = parse_qs(parts.query)["body"][0]
    assert body.strip().splitlines()[-1] == f"ver {__version__}"


def test_mail_links_carry_the_address_subject_and_version():
    from urllib.parse import parse_qs, unquote, urlsplit

    from ai_usage_board import CONTACT_EMAIL, __version__

    mailto = config.mailto_url()
    assert mailto.startswith(f"mailto:{CONTACT_EMAIL}?")
    # A mail app shows '+' literally, so spaces must be %20.
    assert "+" not in mailto
    assert unquote(mailto).endswith(f"ver {__version__}")

    gmail = parse_qs(urlsplit(config.gmail_compose_url()).query)
    assert gmail["to"] == [CONTACT_EMAIL]
    assert gmail["body"][0].strip().endswith(f"ver {__version__}")


def test_pages_show_this_machines_data_folder(tmp_path, monkeypatch):
    import html

    from ai_usage_board import CONTACT_EMAIL, __version__

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(config, "GUIDE_FILE", tmp_path / "connect-guide.html")
    monkeypatch.setattr(config, "CONTACT_FILE", tmp_path / "contact.html")
    config.prepare_user_files()

    guide = (tmp_path / "connect-guide.html").read_text(encoding="utf-8")
    contact = (tmp_path / "contact.html").read_text(encoding="utf-8")
    for text in (guide, contact):
        assert "{{" not in text
        # The real folder of the machine running it, ready for Explorer.
        assert f"<code>{html.escape(str(tmp_path))}</code>" in text
    assert 'href="contact.html"' in guide
    assert 'href="https://github.com/jaehoonV/AI-Usage-Board/issues/new?body=' in contact
    assert CONTACT_EMAIL in contact
    assert 'href="https://mail.google.com/mail/?view=cm&amp;' in contact
    assert f'href="mailto:{CONTACT_EMAIL}?subject=' in contact
    assert __version__ in contact
    # The web links leave the contact page open behind them; mailto has no tab to open.
    assert contact.count('target="_blank" rel="noopener"') == 2


def _point_pages_at(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(config, "GUIDE_FILE", tmp_path / "connect-guide.html")
    monkeypatch.setattr(config, "CONTACT_FILE", tmp_path / "contact.html")
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "runtime.log")


def test_unchanged_pages_are_not_rewritten(tmp_path, monkeypatch):
    _point_pages_at(tmp_path, monkeypatch)
    config.prepare_user_files()

    writes = []
    monkeypatch.setattr(config, "atomic_write", lambda path, text: writes.append(path))
    config.prepare_user_files()
    assert writes == []


def test_a_locked_page_does_not_stop_startup(tmp_path, monkeypatch):
    _point_pages_at(tmp_path, monkeypatch)

    def locked(path, text):
        raise PermissionError(32, "in use", str(path))

    monkeypatch.setattr(config, "atomic_write", locked)
    config.prepare_user_files()  # must not raise
    assert "PermissionError" in (tmp_path / "runtime.log").read_text(encoding="utf-8")


def test_atomic_write_retries_a_briefly_locked_target(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(config, "_REPLACE_PAUSE", 0)
    real_replace = Path.replace
    calls = []

    def flaky(self, target):
        calls.append(target)
        if len(calls) < 3:
            raise PermissionError(32, "in use")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    target = tmp_path / "page.html"
    config.atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"
    assert len(calls) == 3
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_gives_up_and_cleans_its_temp(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest

    monkeypatch.setattr(config, "_REPLACE_PAUSE", 0)

    def always_locked(self, target):
        raise PermissionError(32, "in use")

    monkeypatch.setattr(Path, "replace", always_locked)
    with pytest.raises(PermissionError):
        config.atomic_write(tmp_path / "page.html", "hello")
    assert list(tmp_path.glob("*.tmp")) == []
