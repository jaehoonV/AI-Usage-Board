"""Entry point for both the widget and its helper processes."""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import sys
import threading
import time
import traceback
from typing import List, Optional

from . import APP_NAME, BRIDGE_PORT
from .config import CACHE_FILE, LOG_FILE, ensure_dirs, prepare_user_files, single_instance


def _parse(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai_usage_board", add_help=False)
    parser.add_argument("--account-window", dest="account_window", default=None)
    parser.add_argument("--hidden", action="store_true")
    parser.add_argument("--setup-guide", action="store_true")
    parser.add_argument("--quit", action="store_true", help="실행 중인 인스턴스를 정상 종료")
    parser.add_argument("--help", "-h", action="help")
    return parser.parse_args(argv)


def _alert(message: str) -> None:
    if os.name == "nt":
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x40)
    else:
        print(message, file=sys.stderr)


def _rotate_log() -> None:
    with contextlib.suppress(OSError):
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 256 * 1024:
            LOG_FILE.replace(LOG_FILE.with_suffix(".previous.log"))


# ----------------------------------------------------------------------
# helper process
# ----------------------------------------------------------------------
def _run_account_window(provider: str, hidden: bool) -> int:
    from .bridge.client import BridgeClient

    client = BridgeClient()
    if not client.token:
        return 2

    def report(state: str, error_type: str = "") -> None:
        client.report_status(provider, state, error_type)

    def command() -> dict:
        return client.command(provider)

    try:
        # Every account runs in a dedicated Chrome/Edge profile.
        from .providers import browser_account

        browser_account.run(
            provider,
            report=report,
            publish=client.publish,
            command=command,
            login_only=not hidden,
        )
    except Exception:  # noqa: BLE001 - never pop a traceback at the user
        report("engine_error", "HelperCrashed")
        _rotate_log()
        with contextlib.suppress(OSError):
            with LOG_FILE.open("a", encoding="utf-8") as stream:
                stream.write(f"\n--- {provider} helper ---\n")
                traceback.print_exc(file=stream)
        return 1
    return 0


# ----------------------------------------------------------------------
# widget process
# ----------------------------------------------------------------------
def _run_widget() -> int:
    from .bridge.client import ping, write_token
    from .bridge.server import BridgeServer
    from .core.store import UsageStore
    from .providers.manager import AccountManager
    from .ui.widget import Widget

    token = write_token()
    if ping(token, BRIDGE_PORT, timeout=1.5):
        # Another instance answered: it raised its own window, we are done.
        return 0

    store = UsageStore(CACHE_FILE)
    accounts = AccountManager()
    try:
        server = BridgeServer(store, accounts, token, BRIDGE_PORT)
    except OSError:
        _alert("이미 실행 중이거나 연결 포트를 사용할 수 없습니다.")
        return 1

    threading.Thread(target=server.serve_forever, daemon=True, name="ai_usage_board-bridge").start()
    accounts.restore()

    widget = Widget(store, accounts)
    try:
        widget.run()
    finally:
        widget.closing.set()
        accounts.wait_closed()
        with contextlib.suppress(Exception):
            server.shutdown()
            server.server_close()
    return 0


def _bridge_failure(what: str, error: OSError) -> str:
    """Turn a bridge error into something the user can act on.

    A 403 almost always means the port is held by an instance started from a
    different build, whose token no longer matches the one on disk. "HTTP
    Error 403" tells the user nothing; this does.
    """
    code = getattr(error, "code", None)
    if code == 403:
        return (
            f"{what}: 포트를 쥐고 있는 인스턴스가 다른 토큰을 쓰고 있습니다.\n"
            "예전 버전이 아직 실행 중일 때 생깁니다. 알림 영역 아이콘에서 종료하거나,\n"
            "아래 명령으로 정리한 뒤 다시 실행하세요.\n"
            '  Get-CimInstance Win32_Process -Filter "Name=\'python.exe\' OR Name=\'pythonw.exe\'" |'
            " Where-Object { $_.CommandLine -match 'run\\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
    return f"{what} ({error})."


def _quit_running(timeout: float = 20.0) -> int:
    """Ask a running instance to stop, then wait until it really has.

    Goes through the widget rather than killing the process, because only the
    widget can close the account browsers it started. A forced kill leaves
    them on the user's machine with nothing owning them.
    """
    import socket

    from .bridge.client import BridgeClient

    def port_open() -> bool:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            return probe.connect_ex(("127.0.0.1", BRIDGE_PORT)) == 0

    if not port_open():
        print(f"{APP_NAME}이(가) 실행 중이지 않습니다.")
        return 0

    try:
        BridgeClient().quit()
    except OSError as error:
        print(_bridge_failure("종료 요청을 보내지 못했습니다", error), file=sys.stderr)
        return 1

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open():
            print(f"{APP_NAME}을(를) 종료했습니다.")
            return 0
        time.sleep(0.4)

    print("종료 요청은 보냈지만 아직 완전히 닫히지 않았습니다.", file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    options = _parse(argv if argv is not None else sys.argv[1:])
    ensure_dirs()
    prepare_user_files()

    if options.setup_guide:
        from .config import GUIDE_FILE

        if GUIDE_FILE.is_file() and os.name == "nt":
            os.startfile(str(GUIDE_FILE))  # noqa: S606
        return 0

    if options.quit:
        return _quit_running()

    if options.account_window:
        return _run_account_window(options.account_window, options.hidden)

    try:
        with single_instance() as owner:
            if not owner:
                # Ask the running instance to come forward. Say so out loud:
                # exiting silently looks exactly like failing to start.
                from .bridge.client import BridgeClient

                try:
                    BridgeClient().activate()
                except OSError as error:
                    print(
                        _bridge_failure(f"{APP_NAME}이(가) 이미 실행 중이지만 기존 창을 부르지 못했습니다", error),
                        file=sys.stderr,
                    )
                    return 1
                print(
                    f"{APP_NAME}이(가) 이미 실행 중입니다. 기존 창을 마우스가 있는 화면으로 가져왔습니다.\n"
                    "종료하려면 알림 영역 아이콘을 우클릭한 뒤 '종료'를 누르세요."
                )
                return 0
            return _run_widget()
    except Exception:  # noqa: BLE001
        _rotate_log()
        with contextlib.suppress(OSError):
            with LOG_FILE.open("a", encoding="utf-8") as stream:
                traceback.print_exc(file=stream)
        _alert(f"앱을 시작하지 못했습니다.\n오류 기록: {LOG_FILE}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
