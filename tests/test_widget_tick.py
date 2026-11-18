"""The widget's one-second clock must survive a failing beat.

Tested on a stand-in object so no Tk window is needed.
"""
import threading
from types import SimpleNamespace

from ai_usage_board.ui import widget as widget_module
from ai_usage_board.ui.widget import TICK_MS, Widget


def fake_widget(fail_with=None):
    booked = []

    def beat():
        if fail_with is not None:
            raise fail_with

    return SimpleNamespace(
        closing=threading.Event(),
        _last_tick_error="",
        _tick=beat,
        root=SimpleNamespace(after=lambda ms, callback: booked.append((ms, callback))),
        tick=lambda: None,
        booked=booked,
    )


def test_a_failing_beat_still_books_the_next_one(monkeypatch):
    logged = []
    monkeypatch.setattr(widget_module, "log_exception", lambda heading="": logged.append(heading))
    fake = fake_widget(RuntimeError("boom"))

    Widget.tick(fake)
    Widget.tick(fake)

    assert [ms for ms, _ in fake.booked] == [TICK_MS, TICK_MS]
    # The same fault twice in a row is written once, not once a second.
    assert logged == ["widget tick"]


def settings_widget(monkeypatch):
    saved = []
    monkeypatch.setattr(widget_module, "save_settings", lambda settings: saved.append(dict(settings)))
    fake = SimpleNamespace(
        settings={"pinned": True, "compact": False, "x": None, "y": None},
        root=SimpleNamespace(attributes=lambda *_a: None, winfo_x=lambda: 120, winfo_y=lambda: -15),
        pin_button=SimpleNamespace(configure=lambda **_k: None),
        compact_button=SimpleNamespace(configure=lambda **_k: None),
        _pin_label=lambda: "",
        _layout=lambda: None,
        _fit=lambda: None,
        _fitted=None,
    )
    fake._remember_position = lambda: Widget._remember_position(fake)
    return fake, saved


def test_pin_and_compact_are_saved_when_changed(monkeypatch):
    fake, saved = settings_widget(monkeypatch)
    Widget.toggle_pin(fake)
    Widget.toggle_compact(fake)
    assert [s["pinned"] for s in saved] == [False, False]
    assert saved[-1]["compact"] is True


def test_position_is_saved_when_a_drag_ends(monkeypatch):
    fake, saved = settings_widget(monkeypatch)
    fake._drag_offset = (10, 10)
    Widget.end_drag(fake)
    assert saved == [dict(fake.settings, x=120, y=-15)]
    # A release with no drag behind it (a click on a button) saves nothing.
    Widget.end_drag(fake)
    assert len(saved) == 1


def test_no_beat_is_booked_once_closing(monkeypatch):
    monkeypatch.setattr(widget_module, "log_exception", lambda heading="": None)
    fake = fake_widget()
    fake.closing.set()
    Widget.tick(fake)
    assert fake.booked == []
