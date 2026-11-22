from ai_usage_board.providers import manager
from ai_usage_board.providers.browser_account import (
    SITES,
    build_expression,
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
        def __init__(self, _profile):
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
