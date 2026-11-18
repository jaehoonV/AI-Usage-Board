"""Account quota through a dedicated, hidden Chrome/Edge profile.

Runs in a helper process (``--account-window <provider>``) so a browser crash
can never take the widget down. It reports state and snapshots back to the
parent through the loopback bridge.

Claude and Gemini share this one path. Each gets its own profile folder, and
everything that differs between the two sites lives in a :class:`Site`.
"""
from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from ..config import ACCOUNTS_DIR, RESOURCE_DIR
from .chrome_session import BrowserError, BrowserSession


@dataclass(frozen=True)
class Site:
    provider: str
    host: str
    #: Path of the usage page on ``host``.
    usage_path: str
    #: Where the sign-in window opens.
    login_url: str
    #: (host, path) pairs that mean "signed out". A path of None matches any
    #: path on that host.
    signed_out: Tuple[Tuple[str, Optional[str]], ...]
    #: Paths on ``host`` we may navigate away from. Never a sign-in form and
    #: never a challenge: reloading there throws away the user's progress.
    safe_to_leave: Tuple[str, ...]
    #: Claude can route the usage page through a hash (``/#settings/usage``).
    hash_route: bool = False

    @property
    def usage_url(self) -> str:
        return f"https://{self.host}{self.usage_path}"


SITES: Dict[str, Site] = {
    "claude": Site(
        provider="claude",
        host="claude.ai",
        usage_path="/settings/usage",
        login_url="https://claude.ai/login",
        signed_out=(("claude.ai", "/login"),),
        safe_to_leave=("", "/", "/new", "/chats", "/recents"),
        hash_route=True,
    ),
    "gemini": Site(
        provider="gemini",
        host="gemini.google.com",
        usage_path="/usage",
        login_url="https://gemini.google.com/app",
        signed_out=(("accounts.google.com", None),),
        safe_to_leave=("", "/", "/app"),
    ),
}

INTERSTITIAL_TITLES = (
    "just a moment...",
    "just a moment…",
    "잠시만 기다리십시오…",
    "잠시만 기다리십시오...",
    "attention required!",
    "verifying you are human",
)

#: Bot checks mark themselves in the URL as well as the title. Catching both
#: matters because the title is briefly empty while the page loads.
#: Cloudflare (claude.ai) and Google's "unusual traffic" page.
CHALLENGE_MARKERS = ("/api/challenge_redirect", "__cf_chl", "/cdn-cgi/challenge", "google.com/sorry/")

#: How often the hidden usage page is reloaded so its numbers stay current.
RELOAD_SECONDS = 60

#: How long a bot check may sit there before the card asks the user for help.
#: Most "Just a moment..." pages clear on their own within a few seconds.
#: The window itself is never shown uninvited; the card offers "창 열기".
CHALLENGE_GRACE_SECONDS = 20

#: How long a page may stay untitled (still loading) before we read it anyway.
LOADING_GRACE_SECONDS = 15

#: A window shown for a check or on request stays up at least this long
#: before a successful read tucks it away again.
REHIDE_AFTER_SECONDS = 8


def is_challenge(page: Dict[str, Any]) -> bool:
    url = (page.get("url") or "").lower()
    if any(marker in url for marker in CHALLENGE_MARKERS):
        return True
    return (page.get("title") or "").strip().lower() in INTERSTITIAL_TITLES


def load_extractor() -> str:
    """The page reader that ships with this build - never a copy on disk the
    page or anything else could have edited."""
    source = RESOURCE_DIR / "extractor.js"
    if not source.is_file():
        raise BrowserError("ExtractorMissing")
    return source.read_text(encoding="utf-8")


def page_guard(site: Site) -> str:
    """JS condition: true only on the site's usage page, over https."""
    host, path = json.dumps(site.host), json.dumps(site.usage_path)
    guard = (
        f"location.protocol === 'https:' && location.hostname === {host}"
        f" && (location.pathname.replace(/\\/$/, '') === {path}"
    )
    if site.hash_route:
        guard += f" || location.hash.slice(1).split('?')[0].replace(/^\\//, '') === {json.dumps(site.usage_path.lstrip('/'))}"
    return guard + ")"


def build_expression(site: Site, extractor: str) -> str:
    return (
        f"(() => {{ if (!({page_guard(site)})) return null;\n"
        f"{extractor}\n"
        "const snapshot = AIUsageBoardReader.read();\n"
        "if (snapshot) snapshot.pageUpdatedAt = Date.now() / 1000;\n"
        "return snapshot; })()"
    )


class ChallengePending(BrowserError):
    """A bot check is asking the user to prove they are human.

    Not a failure to retry around: nothing we do helps, and reloading actively
    destroys the check the user is in the middle of.
    """


def current_page(site: Site, session: BrowserSession) -> Optional[Dict[str, Any]]:
    pages = session.pages()
    for page in pages:
        if urlparse(page.get("url") or "").hostname == site.host:
            return page
    return pages[0] if pages else None


def on_usage_page(site: Site, page: Dict[str, Any]) -> bool:
    parsed = urlparse(page.get("url") or "")
    if parsed.scheme != "https" or parsed.hostname != site.host:
        return False
    if parsed.path.rstrip("/") == site.usage_path:
        return True
    return site.hash_route and parsed.fragment.lstrip("/").split("?")[0] == site.usage_path.lstrip("/")


def is_signed_out(site: Site, page: Dict[str, Any]) -> bool:
    if is_challenge(page):
        return False
    parsed = urlparse(page.get("url") or "")
    path = parsed.path.rstrip("/")
    return any(parsed.hostname == host and (want is None or path == want) for host, want in site.signed_out)


def extract_usage(site: Site, session: BrowserSession, expression: str) -> Optional[Dict[str, Any]]:
    page = current_page(site, session)
    if page is None:
        return None

    if is_challenge(page):
        raise ChallengePending("BrowserVerificationRequired")

    # Attach the debugger only on the one page we actually read. Bot checks
    # look for a debugger on the pages they guard, and a login page polled
    # every 15 seconds is the surest way to be sent back to a challenge - the
    # loop the user cannot click their way out of. While they sign in we are
    # a browser that nobody is driving.
    if not on_usage_page(site, page):
        return None

    result = session.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": False},
        page=page,
    )
    if "exceptionDetails" in result:
        raise BrowserError("UsageExtractorFailed")
    return (result.get("result") or {}).get("value")


def run(
    provider: str,
    *,
    report: Callable[[str, str], None],
    publish: Callable[[Dict[str, Any]], None],
    command: Callable[[], Dict[str, Any]],
    login_only: bool,
) -> None:
    """Main loop of a browser helper process.

    Two phases, because signing in and reading want opposite things.

    * **sign-in** - the browser runs with *no* DevTools port. Nothing can
      attach to it, so it is an ordinary browser in every observable way and
      a bot check has nothing to catch. The user signs in and presses
      "로그인 완료"; we close it cleanly so the cookies land in the profile.
    * **read** - the same profile is reopened hidden *with* the port, and
      only the usage page is ever scripted.

    ``report(state, error_type)`` and ``publish(snapshot)`` talk to the bridge;
    ``command()`` returns ``{"show": n, "refresh": n, "done": n, "close": bool}``
    counters so the widget can nudge this process without a second socket.
    """
    site = SITES.get(provider)
    if site is None:
        raise ValueError("Unknown provider")
    profile = ACCOUNTS_DIR / provider / "browser"
    marker = ACCOUNTS_DIR / provider / "connected"
    expression = build_expression(site, load_extractor())

    phase = "read" if (marker.is_file() and not login_only) else "login"
    session: Optional[BrowserSession] = None
    seen_show = seen_refresh = seen_done = 0
    last_poll = 0.0
    last_reload = time.monotonic()
    failures = 0
    state = ""
    #: When the current bot check was first seen, or None.
    challenge_since: Optional[float] = None
    #: When the reading browser was put on screen, or None while hidden.
    revealed_at: Optional[float] = None
    #: When the current page was first seen without a title (still loading).
    untitled_since: Optional[float] = None

    def open_session(for_phase: str) -> BrowserSession:
        new = BrowserSession(profile)
        new.start(
            # The reading browser is infrastructure, not something to look at:
            # always hidden, whatever triggered the connect. Only the sign-in
            # window is meant to be on screen.
            minimized=for_phase == "read",
            landing=site.usage_url if for_phase == "read" else site.login_url,
            debug=for_phase == "read",
        )
        if for_phase == "read":
            new.hide()
        return new

    def reveal() -> None:
        nonlocal revealed_at
        with contextlib.suppress(Exception):
            session.bring_to_front()
        revealed_at = time.monotonic()

    def set_state(name: str, error_type: str = "") -> None:
        nonlocal state
        if state != name:
            state = name
            report(name, error_type)

    report("starting", "")
    try:
        while True:
            instruction = command()
            if instruction.get("close"):
                break

            if session is None or not session.alive:
                if session is not None and phase == "login":
                    # The user closed the sign-in window themselves; treat it
                    # the same as pressing the button.
                    phase = "read" if marker.is_file() else "login"
                try:
                    session = open_session(phase)
                except BrowserError as error:
                    report("engine_error", str(error))
                    return
                state = ""
                challenge_since = revealed_at = untitled_since = None

            show = int(instruction.get("show") or 0)
            if show > seen_show:
                seen_show = show
                if phase == "read" and state == "login_required":
                    # "계정 연결" on a signed-out reader: sign in through the
                    # clean, port-less window rather than the scripted one.
                    session.close()
                    session = None
                    phase = "login"
                    continue
                if phase == "read":
                    reveal()
                else:
                    session.bring_to_front()

            # -- sign-in phase: no automation at all, just wait ------------
            if phase == "login":
                set_state("login_manual")
                done = int(instruction.get("done") or 0)
                if done > seen_done:
                    seen_done = done
                    with contextlib.suppress(OSError):
                        marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.write_text("1", encoding="ascii")
                    session.close()
                    session = None
                    phase = "read"
                    last_poll = 0.0
                time.sleep(0.5)
                continue

            refresh = int(instruction.get("refresh") or 0)
            force = refresh > seen_refresh
            if force:
                seen_refresh = refresh

            now = time.monotonic()
            # While a check is pending or a page is loading, look more often
            # so the card catches up soon after it settles.
            interval = 3 if (challenge_since is not None or untitled_since is not None) else 15
            if not force and now - last_poll < interval:
                session.keep_hidden()
                time.sleep(0.4)
                continue
            last_poll = now
            session.keep_hidden()

            # One look at the page, over HTTP only, decides what is safe.
            try:
                page = current_page(site, session)
            except Exception:  # noqa: BLE001 - treated like a failed read below
                page = None
            checking = page is not None and is_challenge(page)

            # A hidden tab gets its timers throttled, so the page may stop
            # refreshing itself. Reload on a slow cadence to be sure the
            # numbers are current - but never during a bot check: a reload
            # there restarts the check, and a debugger attached to it is what
            # makes the check come back forever.
            if not checking and page is not None and on_usage_page(site, page) and now - last_reload > RELOAD_SECONDS:
                last_reload = now
                with contextlib.suppress(Exception):
                    session.call("Page.reload", page=page)
                    time.sleep(2.5)

            if page is not None and is_signed_out(site, page):
                # Session expired: drop the marker and go back to a clean,
                # unautomated browser rather than looping on the login page.
                with contextlib.suppress(OSError):
                    marker.unlink(missing_ok=True)
                session.close()
                session = None
                phase = "login"
                continue

            # A page with no title yet is still loading - and on claude.ai that
            # is often a Cloudflare check about to appear. Attaching the
            # debugger at that moment is what we try hardest to avoid, so wait
            # for it to show what it is (but not forever).
            if page is not None and not (page.get("title") or "").strip():
                if untitled_since is None:
                    untitled_since = now
                if now - untitled_since < LOADING_GRACE_SECONDS:
                    continue
            else:
                untitled_since = None

            try:
                snapshot = extract_usage(site, session, expression)
            except ChallengePending:
                failures = 0
                if challenge_since is None:
                    challenge_since = now
                # Give the check a chance to clear by itself before involving
                # the user, then only say so on the card. The window stays
                # hidden: a browser that pops up uninvited is worse than a
                # card asking for one click on "창 열기".
                if now - challenge_since >= CHALLENGE_GRACE_SECONDS:
                    set_state("verify_required", "BotCheck")
                continue
            except BrowserError as error:
                failures += 1
                if failures >= 3:
                    set_state("page_error", str(error))
                time.sleep(1.0)
                continue
            except Exception as error:  # noqa: BLE001 - helper must stay alive
                failures += 1
                if failures >= 3:
                    set_state("page_error", type(error).__name__)
                time.sleep(1.0)
                continue

            failures = 0
            challenge_since = None
            if snapshot and snapshot.get("windows"):
                publish(snapshot)
                set_state("connected")
                with contextlib.suppress(OSError):
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text("1", encoding="ascii")
                # The window was only shown to get past a check or because
                # the user asked; with numbers flowing it goes back away.
                if revealed_at is not None and now - revealed_at >= REHIDE_AFTER_SECONDS:
                    session.hide(timeout=2.0)
                    revealed_at = None
            else:
                set_state("login_required")
                # Nudge a signed-in-but-wrong-page tab back to the usage view.
                # Only from a page it is safe to leave: navigating away from a
                # login form or a bot check throws away the user's work.
                if now - last_reload > RELOAD_SECONDS:
                    last_reload = now
                    _navigate_to_usage(site, session)
    finally:
        if session is not None:
            session.close()
        report("closed", "")


def _navigate_to_usage(site: Site, session: BrowserSession) -> None:
    with contextlib.suppress(Exception):
        page = current_page(site, session)
        if page is None or is_challenge(page):
            return
        parsed = urlparse(page.get("url") or "")
        if parsed.scheme != "https" or parsed.hostname != site.host:
            return
        if parsed.path.rstrip("/") not in site.safe_to_leave:
            return
        session.call("Page.navigate", {"url": site.usage_url}, page=page)
