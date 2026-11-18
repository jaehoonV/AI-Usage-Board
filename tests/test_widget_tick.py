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


def test_no_beat_is_booked_once_closing(monkeypatch):
    monkeypatch.setattr(widget_module, "log_exception", lambda heading="": None)
    fake = fake_widget()
    fake.closing.set()
    Widget.tick(fake)
    assert fake.booked == []
