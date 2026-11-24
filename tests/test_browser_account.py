from ai_usage_board.providers import manager
from ai_usage_board.providers.browser_account import (
    SITES,
    build_expression,
    extract_usage,
    is_challenge,
    is_signed_out,
    on_usage_page,
)

CLAUDE, GEMINI = SITES["claude"], SITES["gemini"]


def page(url, title=""):
    return {"url": url, "title": title}


def test_usage_page_is_matched_per_site():
    assert on_usage_page(CLAUDE, page("https://claude.ai/settings/usage"))
    assert on_usage_page(CLAUDE, page("https://claude.ai/#/settings/usage"))
    assert on_usage_page(GEMINI, page("https://gemini.google.com/usage/"))
    # Hash routing is a Claude quirk only.
    assert not on_usage_page(GEMINI, page("https://gemini.google.com/app#usage"))
    assert not on_usage_page(CLAUDE, page("http://claude.ai/settings/usage"))
    assert not on_usage_page(CLAUDE, page("https://evil.example/settings/usage"))
    assert not on_usage_page(GEMINI, page("https://claude.ai/usage"))


def test_challenge_keeps_the_usage_url():
    # Cloudflare's check sits on the usage URL itself, so only the title
    # gives it away.
    check = page("https://claude.ai/settings/usage", "잠시만 기다리십시오…")
    assert on_usage_page(CLAUDE, check)
    assert is_challenge(check)
    assert is_challenge(page("https://claude.ai/settings/usage?__cf_chl_rt_tk=x"))
    assert is_challenge(page("https://www.google.com/sorry/index?continue=x"))
    assert not is_challenge(page("https://claude.ai/settings/usage", "Settings - Claude"))


def test_signed_out_detection():
    assert is_signed_out(CLAUDE, page("https://claude.ai/login"))
    assert not is_signed_out(CLAUDE, page("https://claude.ai/settings/usage"))
    assert is_signed_out(GEMINI, page("https://accounts.google.com/v3/signin/identifier"))
    assert not is_signed_out(GEMINI, page("https://gemini.google.com/usage"))
    # A check on the sign-in host is not a sign-out.
    assert not is_signed_out(CLAUDE, page("https://claude.ai/login", "Just a moment..."))


def test_expression_guards_on_the_sites_own_page():
    claude = build_expression(CLAUDE, "/* reader */")
    gemini = build_expression(GEMINI, "/* reader */")
    assert '"claude.ai"' in claude and '"/settings/usage"' in claude and "location.hash" in claude
    assert '"gemini.google.com"' in gemini and '"/usage"' in gemini and "location.hash" not in gemini


def test_a_browser_that_cannot_start_keeps_its_reason(tmp_path, monkeypatch):
    """The closing "closed" report must not bury the engine error."""
    from ai_usage_board.providers import browser_account
    from ai_usage_board.providers.chrome_session import BrowserError

    class Missing:
        def __init__(self, _profile, **_options):
            pass

        def start(self, **_kwargs):
            raise BrowserError("BrowserNotInstalled")

    monkeypatch.setattr(browser_account, "ACCOUNTS_DIR", tmp_path)
    monkeypatch.setattr(browser_account, "load_extractor", lambda: "")
    monkeypatch.setattr(browser_account, "BrowserSession", Missing)
    reports = []
    browser_account.run(
        "claude",
        report=lambda state, error="": reports.append((state, error)),
        publish=lambda _snapshot: None,
        command=lambda: {},
        login_only=False,
    )
    assert reports == [("starting", ""), ("engine_error", "BrowserNotInstalled")]


def test_every_managed_account_uses_the_browser_helper():
    assert set(manager.MANAGED) == set(SITES)
    for provider in manager.MANAGED:
        command = manager._helper_command(provider, hidden=True)
        assert command[-3:] == ["--account-window", provider, "--hidden"]


def test_a_check_that_clears_by_itself_is_not_stuck():
    from ai_usage_board.providers.browser_account import ChallengeWatch

    watch = ChallengeWatch()
    watch.seen(0)
    assert not watch.stuck(10, revealed_at=None)
    watch.read()
    assert watch.count == 0 and watch.since is None


def test_a_check_left_on_screen_after_opening_the_window_is_stuck():
    from ai_usage_board.providers.browser_account import STUCK_AFTER_REVEAL_SECONDS, ChallengeWatch

    watch = ChallengeWatch()
    watch.seen(0)
    # Sitting hidden is just waiting for the user, however long it takes.
    assert not watch.stuck(10_000, revealed_at=None)
    # Once they opened the window, it gets its time from that moment on.
    revealed = 5_000
    assert not watch.stuck(revealed + STUCK_AFTER_REVEAL_SECONDS - 1, revealed_at=revealed)
    assert watch.stuck(revealed + STUCK_AFTER_REVEAL_SECONDS, revealed_at=revealed)


def test_a_check_newer_than_the_window_gets_its_own_time():
    from ai_usage_board.providers.browser_account import STUCK_AFTER_REVEAL_SECONDS, ChallengeWatch

    watch = ChallengeWatch()
    watch.seen(1_000)  # appeared long after the window was opened at 0
    assert not watch.stuck(1_000 + STUCK_AFTER_REVEAL_SECONDS - 1, revealed_at=0)


def test_checks_that_keep_coming_back_are_stuck():
    from ai_usage_board.providers.browser_account import STUCK_AFTER_CHECKS, ChallengeWatch

    watch = ChallengeWatch()
    for moment in range(STUCK_AFTER_CHECKS):
        assert not watch.stuck(moment, revealed_at=None)
        watch.seen(moment)
        watch.seen(moment + 0.5)  # the same check, polled again
        if moment < STUCK_AFTER_CHECKS - 1:
            watch.gone()  # passed, but no numbers followed
    assert watch.count == STUCK_AFTER_CHECKS
    assert watch.stuck(STUCK_AFTER_CHECKS, revealed_at=None)
    # One good reading wipes the slate.
    watch.read()
    assert not watch.stuck(STUCK_AFTER_CHECKS + 1, revealed_at=None)


def test_stuck_is_a_state_the_manager_and_bridge_know():
    from ai_usage_board.bridge.server import _ACCOUNT_MESSAGES

    accounts = manager.AccountManager()
    accounts.set_status("claude", "verify_stuck", "BotCheckLoop")
    assert accounts.status()["claude"] == "verify_stuck"
    assert "다시 연결" in _ACCOUNT_MESSAGES["verify_stuck"]


class IsolatedSession:
    """Stands in for BrowserSession: the reader must run through evaluate_isolated."""

    def __init__(self):
        self.evaluated = []

    def pages(self):
        return [{
            "url": "https://claude.ai/settings/usage",
            "title": "Claude",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/x",
        }]

    def evaluate_isolated(self, expression, page):
        self.evaluated.append((expression, page))
        return {"result": {"value": {"windows": [1]}}}

    def call(self, *_args, **_kwargs):
        raise AssertionError("the reader must not run in the page's own world")


def test_usage_reader_runs_in_an_isolated_world():
    session = IsolatedSession()
    value = extract_usage(CLAUDE, session, "expr")
    assert value == {"windows": [1]}
    assert [expression for expression, _page in session.evaluated] == ["expr"]
    assert session.evaluated[0][1]["url"] == "https://claude.ai/settings/usage"


def test_only_claude_keeps_its_reader_shown_off_screen():
    # claude.ai's Cloudflare check cannot finish in a hidden page; Gemini has
    # no such check, and a shown page keeps painting for nothing.
    assert SITES["claude"].park_visible
    assert not SITES["gemini"].park_visible


# -- clearing a bot check in a port-less browser --------------------------


def test_window_title_tells_check_from_settled_page():
    from ai_usage_board.providers.browser_account import title_state

    assert title_state(CLAUDE, "잠시만 기다리십시오… - Chrome") == "checking"
    assert title_state(CLAUDE, "Just a moment... - Google Chrome") == "checking"
    assert title_state(CLAUDE, "새 채팅 - Claude - Chrome") == "settled"
    # Still loading: Chrome shows the address, which also says "claude".
    assert title_state(CLAUDE, "claude.ai/settings/usage - Chrome") == "loading"
    assert title_state(CLAUDE, "") == "loading"


def test_only_claude_clears_checks_without_a_port():
    assert CLAUDE.settled_title
    assert GEMINI.settled_title is None


class FakeBrowser:
    """One browser launch: a reader stuck on a check, or a port-less window."""

    launched = []

    def __init__(self, _profile, **options):
        self.options = options
        self.closed = False
        FakeBrowser.launched.append(self)

    def start(self, **kwargs):
        self.kwargs = kwargs

    def hide(self, timeout=10.0):
        pass

    def keep_hidden(self):
        pass

    def bring_to_front(self):
        pass

    @property
    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True

    # reader
    def pages(self):
        return [{"url": "https://claude.ai/api/challenge_redirect?to=x", "title": "잠시만 기다리십시오…",
                 "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/x"}]

    def call(self, *_args, **_kwargs):
        raise AssertionError("nothing may be sent to a page during a check")

    def evaluate_isolated(self, *_args, **_kwargs):
        raise AssertionError("nothing may be sent to a page during a check")

    # port-less window
    def frame_title(self):
        return "새 채팅 - Claude - Chrome"


def run_until(tmp_path, monkeypatch, stop):
    from ai_usage_board.providers import browser_account

    FakeBrowser.launched = []
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / "connected").write_text("1", encoding="ascii")
    monkeypatch.setattr(browser_account, "ACCOUNTS_DIR", tmp_path)
    monkeypatch.setattr(browser_account, "load_extractor", lambda: "")
    monkeypatch.setattr(browser_account, "BrowserSession", FakeBrowser)
    monkeypatch.setattr(browser_account, "CLEAR_AFTER_SECONDS", 0)
    monkeypatch.setattr(browser_account, "CLEAR_SETTLE_SECONDS", 0)
    monkeypatch.setattr(browser_account.time, "sleep", lambda _seconds: None)
    reports = []
    browser_account.run(
        "claude",
        report=lambda state, error="": reports.append(state),
        publish=lambda _snapshot: None,
        command=lambda: {"close": stop(reports)},
        login_only=False,
    )
    return reports


def test_a_check_the_reader_cannot_pass_goes_to_a_port_less_window(tmp_path, monkeypatch):
    run_until(tmp_path, monkeypatch, lambda _reports: len(FakeBrowser.launched) >= 3)
    reader, clearer, reader_again = FakeBrowser.launched[:3]
    assert reader.kwargs["debug"] and not reader.kwargs["offscreen"]
    # The check is handed to a browser with no DevTools port, parked off
    # screen but awake, on the usage page.
    assert not clearer.kwargs["debug"] and clearer.kwargs["offscreen"]
    assert clearer.kwargs["landing"] == CLAUDE.usage_url
    assert clearer.options["park_visible"]
    # Once its title says the page settled, it is closed cleanly - the cookie
    # it earned is written - and reading starts again.
    assert reader.closed and clearer.closed
    assert reader_again.kwargs["debug"]


def test_a_check_that_keeps_coming_back_is_called_stuck(tmp_path, monkeypatch):
    from ai_usage_board.providers.browser_account import STUCK_AFTER_CHECKS

    reports = run_until(tmp_path, monkeypatch, lambda reports: "verify_stuck" in reports)
    debug = [browser.kwargs["debug"] for browser in FakeBrowser.launched]
    # reader, window, reader, window, ... then no more windows once stuck.
    assert debug == [True, False] * (STUCK_AFTER_CHECKS - 1) + [True]
    assert "verify_stuck" in reports
