"""Supervises the helper processes that own browser sessions.

Each account connection runs in its own process. The widget never imports
browser automation in-process, so a hung page or a browser crash can only
take down a helper. Communication is one-way-ish: helpers POST state and
snapshots to the bridge, and poll a tiny command counter for 'show' / 'refresh'
/ 'close'.
"""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict

from ..config import ACCOUNTS_DIR
from ..core.models import SERVICES

STATES = (
    "closed",
    "starting",
    "connected",
    "page_error",
    "engine_error",
    "login_required",
    "verify_required",
    #: Sign-in browser is open with no DevTools port; waiting for the user to
    #: say they are done.
    "login_manual",
)
MANAGED = ("claude", "gemini")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AccountManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: Dict[str, subprocess.Popen] = {}
        self._commands: Dict[str, Dict[str, object]] = {
            key: {"show": 0, "refresh": 0, "done": 0, "close": False} for key in MANAGED
        }
        self._states: Dict[str, str] = {key: "login_required" for key in MANAGED}
        self._errors: Dict[str, str] = {key: "" for key in MANAGED}

    # -- lifecycle --------------------------------------------------------
    def connect(self, provider: str, *, hidden: bool = False) -> None:
        if provider not in MANAGED:
            raise ValueError("Unknown account")

        with self._lock:
            running = self._processes.get(provider)
            if running is not None and running.poll() is None:
                self._commands[provider]["show"] = int(self._commands[provider]["show"]) + 1
                return
            self._commands[provider] = {"show": 1 if not hidden else 0, "refresh": 0, "done": 0, "close": False}
            self._states[provider] = "starting"
            self._errors[provider] = ""

        command = _helper_command(provider, hidden=hidden)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        except OSError as error:
            self.set_status(provider, "engine_error", type(error).__name__)
            return

        with self._lock:
            self._processes[provider] = process

    def restore(self) -> None:
        """Reconnect providers that were connected the last time we ran."""
        for provider in MANAGED:
            if (ACCOUNTS_DIR / provider / "connected").is_file():
                self.connect(provider, hidden=True)

    def close_one(self, provider: str, timeout: float = 8.0) -> None:
        with self._lock:
            if provider in self._commands:
                self._commands[provider]["close"] = True
            process = self._processes.get(provider)
        if process is None:
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)

    def reset(self, provider: str) -> bool:
        """Throw away a provider's browser profile and start over.

        Needed because a site's bot protection can decide it does not trust
        this particular browser profile any more and then challenge it on
        every visit, with no way for the user out of the loop. Deleting the
        profile is the only reliable cure; it costs the user one sign-in.
        """
        if provider not in MANAGED:
            raise ValueError("Unknown account")
        self.close_one(provider)
        with self._lock:
            self._processes.pop(provider, None)
            self._states[provider] = "login_required"
            self._errors[provider] = ""
            self._commands[provider] = {"show": 0, "refresh": 0, "done": 0, "close": False}
        profile = ACCOUNTS_DIR / provider / "browser"
        removed = profile.is_dir()
        if removed:
            shutil.rmtree(profile, ignore_errors=True)
        with contextlib.suppress(OSError):
            (ACCOUNTS_DIR / provider / "connected").unlink(missing_ok=True)
        return removed

    def stop(self) -> None:
        with self._lock:
            for command in self._commands.values():
                command["close"] = True

    def wait_closed(self, timeout: float = 12.0) -> None:
        self.stop()
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)

    # -- command channel --------------------------------------------------
    def command(self, provider: str) -> Dict[str, object]:
        with self._lock:
            return dict(self._commands.get(provider, {}))

    def request_refresh(self) -> None:
        with self._lock:
            for command in self._commands.values():
                command["refresh"] = int(command["refresh"]) + 1

    def request_show(self, provider: str) -> None:
        with self._lock:
            if provider in self._commands:
                self._commands[provider]["show"] = int(self._commands[provider]["show"]) + 1

    def confirm_login(self, provider: str) -> None:
        """The user says they finished signing in: switch to the read phase."""
        with self._lock:
            if provider in self._commands:
                self._commands[provider]["done"] = int(self._commands[provider]["done"]) + 1

    # -- state ------------------------------------------------------------
    def set_status(self, provider: str, state: str, error_type: str = "") -> None:
        if provider not in MANAGED or state not in STATES:
            raise ValueError("Invalid account state")
        safe = "".join(ch for ch in str(error_type) if ch.isalnum() or ch in "._-")[:80]
        with self._lock:
            self._states[provider] = state
            self._errors[provider] = safe

    def status(self) -> Dict[str, str]:
        with self._lock:
            result = dict(self._states)
            for provider, process in self._processes.items():
                if process.poll() is not None and result.get(provider) not in ("engine_error",):
                    result[provider] = "closed"
            return result

    def errors(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._errors)


def _helper_command(provider: str, *, hidden: bool) -> list:
    """Build the command line for a helper process (frozen or source tree)."""
    arguments = ["--account-window", provider]
    if hidden:
        arguments.append("--hidden")

    if getattr(sys, "frozen", False):
        return [sys.executable, *arguments]

    # Prefer pythonw.exe so no console flashes during development.
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    if windowless.is_file():
        interpreter = windowless
    root = Path(__file__).resolve().parents[3]
    return [str(interpreter), str(root / "run.py"), *arguments]


__all__ = ["AccountManager", "MANAGED", "STATES", "SERVICES"]
