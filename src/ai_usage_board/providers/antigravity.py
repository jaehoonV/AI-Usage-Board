"""Antigravity quota through the IDE's own loopback API.

Antigravity runs ``language_server.exe`` beside the editor and the editor asks
it for the account's plan and per-model quota. We ask the same server the same
question, so nothing is scraped and no credential is read: the server was
started by the user's own signed-in app and authenticates us with the CSRF
token printed in its command line.

    POST https://127.0.0.1:<port>/exa.language_server_pb.LanguageServerService/GetUserStatus
    x-codeium-csrf-token: <token from --csrf_token>
    {}

Neither half of that address is fixed. The server is started with
``--https_server_port 0``, so Windows hands it a different port every launch,
and the token is new each time too. Both are therefore discovered from the
running process and cached only until a call fails.

While the IDE is open we are a guest of its server. When it is closed there
is nothing to ask, so :func:`launch_and_read` starts the same binary the IDE
would, against the same account folder, asks once and shuts it down again.
"""
from __future__ import annotations

import contextlib
import ctypes
import http.client
import json
import math
import os
import re
import ssl
import subprocess
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROVIDER = "antigravity"

#: Connect-RPC method the editor itself calls for plan and quota.
RPC_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"
#: Antigravity grew out of Codeium and still answers to that header name.
CSRF_HEADER = "x-codeium-csrf-token"

#: Tells the Antigravity language server apart from the one Windsurf and the
#: JetBrains Codeium plugin ship, which answer the same RPC for another account.
APP_DATA_MARKER = "antigravity"

MAX_WINDOWS = 8
LABEL_LIMIT = 60

#: The only addresses this client may talk to. Not "localhost": that is a name,
#: and a name can be pointed somewhere else.
LOOPBACK_HOSTS = ("127.0.0.1", "::1")

#: How long one port may take to answer, and how long the whole hunt may take.
PROBE_SECONDS = 4.0
DISCOVERY_BUDGET = 20.0

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: The server presents a self-signed certificate for 127.0.0.1. Verification
#: has nothing to check against and would only refuse every call, so it is off
#: here and nowhere else: this client talks to loopback or not at all, which
#: :func:`request` enforces rather than merely intending.
_LOOPBACK_TLS = ssl.create_default_context()
_LOOPBACK_TLS.check_hostname = False
_LOOPBACK_TLS.verify_mode = ssl.CERT_NONE

_cached: Optional["Endpoint"] = None


class AntigravityError(RuntimeError):
    """Any failure to obtain Antigravity numbers."""


class AntigravityNotRunning(AntigravityError):
    """No Antigravity language server on this machine right now.

    Unlike a missing CLI this fixes itself the moment the user opens the app,
    so the widget keeps polling instead of standing down.
    """


class AntigravityLoginRequired(AntigravityError):
    """The server answered but has no account behind it."""


class Endpoint:
    """Where to reach a running language server, and how to authenticate."""

    __slots__ = ("url", "token", "pid")

    def __init__(self, url: str, token: str, pid: int) -> None:
        self.url = url
        self.token = token
        self.pid = pid

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"Endpoint({self.url!r}, pid={self.pid})"


# --------------------------------------------------------------------------
# finding the server
# --------------------------------------------------------------------------
_TOKEN = re.compile(r"--csrf_token[= ]\s*([A-Za-z0-9._~-]{8,128})")
#: The value may be a bare name or a whole path, quoted or not, so it is taken
#: verbatim up to the next space and picked apart afterwards.
_APP_DATA = re.compile(r'--app_data_dir[= ]\s*("[^"]*"|\S+)')

#: One PowerShell round trip for both halves of the answer: which language
#: servers run, and which loopback ports each of them listens on.
_SURVEY = (
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new(); "
    "$found = @(); "
    "foreach ($p in Get-CimInstance Win32_Process -Filter \"Name LIKE 'language_server%'\") { "
    "  $ports = @(Get-NetTCPConnection -State Listen -OwningProcess $p.ProcessId "
    "    -ErrorAction SilentlyContinue | ForEach-Object { $_.LocalPort }); "
    "  $found += [pscustomobject]@{ pid = $p.ProcessId; cmd = $p.CommandLine; ports = $ports } "
    "}; "
    "ConvertTo-Json -Compress -Depth 3 -InputObject @($found)"
)


def _halt(cancel: Optional[threading.Event]) -> None:
    """Give up at once when the widget is shutting down."""
    if cancel is not None and cancel.is_set():
        raise InterruptedError("closing")


def _powershell() -> Optional[Path]:
    if os.name != "nt":
        return None
    path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return path if path.is_file() else None


def survey(cancel: Optional[threading.Event] = None) -> List[Dict[str, Any]]:
    """Every running language server, as ``{pid, cmd, ports}`` records."""
    _halt(cancel)
    shell = _powershell()
    if shell is None:
        return []
    try:
        completed = subprocess.run(
            [str(shell), "-NoProfile", "-NonInteractive", "-Command", _SURVEY],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_survey(completed.stdout or "")


def parse_survey(text: str) -> List[Dict[str, Any]]:
    """Normalise the survey's JSON into records, tolerating a single object."""
    try:
        payload = json.loads(text.strip() or "[]")
    except ValueError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []

    records: List[Dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        command = item.get("cmd")
        if not isinstance(command, str):
            continue
        raw_ports = item.get("ports")
        if isinstance(raw_ports, (int, float)) and not isinstance(raw_ports, bool):
            raw_ports = [raw_ports]
        ports = []
        for port in raw_ports if isinstance(raw_ports, list) else ():
            if isinstance(port, bool) or not isinstance(port, (int, float)):
                continue
            if 0 < int(port) < 65536:
                ports.append(int(port))
        pid = item.get("pid")
        records.append(
            {
                "pid": int(pid) if isinstance(pid, (int, float)) and not isinstance(pid, bool) else 0,
                "cmd": command,
                "ports": sorted(dict.fromkeys(ports)),
            }
        )
    return records


def is_antigravity(command: str) -> bool:
    """True for the Antigravity server, false for Windsurf's and Codeium's.

    They are the same program under different names, so the only honest
    marker is the data directory the app told it to use. That argument is a
    bare name here and a full path elsewhere, so only its last component is
    compared - matching the string alone would miss every path spelling.
    """
    match = _APP_DATA.search(command or "")
    if not match:
        return False
    value = match.group(1).strip('"').rstrip("\\/")
    return re.split(r"[\\/]", value)[-1].lower() == APP_DATA_MARKER


def csrf_token(command: str) -> str:
    match = _TOKEN.search(command or "")
    return match.group(1) if match else ""


def discover(
    cancel: Optional[threading.Event] = None,
    budget: float = DISCOVERY_BUDGET,
) -> Tuple[Endpoint, Dict[str, Any]]:
    """Find a reachable endpoint, and hand back the reply that proved it.

    The probe is a full ``GetUserStatus`` call, so returning its payload saves
    the caller from asking the same question again a millisecond later.

    Probing is bounded as a whole, not per port: a machine with many listening
    ports would otherwise multiply four seconds by however many there are.
    """
    _halt(cancel)
    records = [item for item in survey(cancel) if is_antigravity(item["cmd"])]
    if not records:
        raise AntigravityNotRunning("Antigravity가 실행 중이 아닙니다 · 앱을 켜면 표시됩니다")

    deadline = time.monotonic() + budget
    for record in records:
        token = csrf_token(record["cmd"])
        if not token:
            continue
        for port in record["ports"]:
            # The server speaks the same RPC on an https and an http port and
            # does not say which is which; the one that answers is the one.
            for scheme in ("https", "http"):
                _halt(cancel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AntigravityError("Antigravity 사용량 서버를 찾는 데 너무 오래 걸립니다")
                url = f"{scheme}://127.0.0.1:{port}{RPC_PATH}"
                try:
                    payload = request(url, token, timeout=min(PROBE_SECONDS, remaining))
                except AntigravityLoginRequired:
                    raise
                except AntigravityError:
                    continue
                return Endpoint(url, token, record["pid"]), payload
    raise AntigravityError("Antigravity 사용량 서버에 연결하지 못했습니다")


# --------------------------------------------------------------------------
# the call
# --------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect.

    urllib carries custom headers onto the new request, so a server that
    answered 302 with an address of its own choosing would be handed the CSRF
    token - over a connection whose certificate we do not check. Returning None
    turns the 3xx into an HTTPError instead of a second request.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_LOOPBACK_TLS),
    _NoRedirect,
)


def _check_loopback(url: str) -> None:
    """Refuse any address that is not this machine, before a byte is sent."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in LOOPBACK_HOSTS:
        raise AntigravityError("Antigravity 주소가 루프백이 아닙니다")


def request(url: str, token: str, timeout: float = 8.0) -> Dict[str, Any]:
    """POST the RPC and return the decoded reply."""
    _check_loopback(url)
    message = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", CSRF_HEADER: token},
    )
    try:
        with _OPENER.open(message, timeout=timeout) as response:
            body = response.read(1 << 20)
    except urllib.error.HTTPError as error:
        raise AntigravityError(f"Antigravity 응답 오류 ({error.code})") from error
    # http.client raises its own family - a mangled status line, a bad chunk -
    # and those are not OSError, so they would otherwise leave this module.
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        raise AntigravityError("Antigravity 사용량 서버에 연결하지 못했습니다") from error

    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError as error:
        raise AntigravityError("Antigravity 응답을 읽을 수 없습니다") from error
    if not isinstance(payload, dict):
        raise AntigravityError("Antigravity 응답을 읽을 수 없습니다")
    if not isinstance(payload.get("userStatus"), dict):
        raise AntigravityLoginRequired("Antigravity 로그인이 필요합니다")
    return payload


# --------------------------------------------------------------------------
# normalising the reply
# --------------------------------------------------------------------------
def _family(label: str) -> str:
    """The model's house name: 'Gemini 3.8 Flash (High)' -> 'Gemini'."""
    return (label or "").strip().split(" ")[0] or "모델"


def _base(label: str) -> str:
    """Drop the effort suffix: 'Gemini 3.8 Flash (High)' -> 'Gemini 3.8 Flash'."""
    return (label or "").split("(")[0].strip() or "모델"


def _join(names: List[str]) -> str:
    unique = list(dict.fromkeys(names))
    text = " · ".join(unique)
    if len(text) <= LABEL_LIMIT:
        return text
    return f"{unique[0]} 외 {len(unique) - 1}"


def _group_label(labels: List[str]) -> str:
    return _join([_family(label) for label in labels])


def _detailed_label(labels: List[str]) -> str:
    return _join([_base(label) for label in labels])


def _reset_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%m/%d %H:%M")


def _used_percent(quota: Dict[str, Any]) -> Optional[float]:
    """Remaining fraction -> used percent.

    The reply is protobuf-shaped JSON, so a field that is zero is left out
    entirely. A model group with no ``remainingFraction`` but a ``resetTime``
    is therefore spent, not unknown - reading the absence as "no data" would
    paint an exhausted quota as a full one.

    An entirely empty ``quotaInfo`` is the other case and means the opposite:
    the server is tracking nothing for this model, so reading it as zero would
    invent an exhausted quota that does not exist. Those models are dropped.
    """
    if not any(key in quota for key in ("remainingFraction", "resetTime")):
        return None
    remaining = quota.get("remainingFraction", 0)
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        return None
    # NaN would survive the clamp below as a full quota - min() keeps its first
    # argument when the comparison is false, and every comparison with NaN is.
    # A number we cannot read is not a number we may draw.
    if not math.isfinite(remaining):
        return None
    remaining = max(0.0, min(1.0, float(remaining)))
    return round(100.0 * (1.0 - remaining), 1)


def _dig(source: Any, *keys: str) -> Any:
    """Walk nested keys, giving up the moment a level is not a mapping.

    ``(value or {}).get(...)`` looks equivalent and is not: a string is truthy,
    so it survives the ``or`` and then raises AttributeError on ``.get``.
    """
    for key in keys:
        if not isinstance(source, dict):
            return None
        source = source.get(key)
    return source


def normalize(payload: Any) -> Dict[str, Any]:
    """Turn a GetUserStatus reply into our snapshot shape."""
    if not isinstance(payload, dict):
        raise AntigravityError("Antigravity 응답을 읽을 수 없습니다")
    status = payload.get("userStatus")
    if not isinstance(status, dict):
        raise AntigravityLoginRequired("Antigravity 로그인이 필요합니다")

    configs = _dig(status, "cascadeModelConfigData", "clientModelConfigs")
    if not isinstance(configs, list):
        configs = []

    # Models share a quota in groups, and the reply says so only by repeating
    # the same numbers. Fold identical (remaining, reset) pairs back into the
    # one bucket they came from, so the card shows two meters and not six.
    buckets: Dict[Tuple[float, str], List[str]] = {}
    for config in configs:
        if not isinstance(config, dict):
            continue
        quota = config.get("quotaInfo")
        if not isinstance(quota, dict):
            continue
        used = _used_percent(quota)
        if used is None:
            continue
        reset = quota.get("resetTime") if isinstance(quota.get("resetTime"), str) else ""
        label = config.get("label") if isinstance(config.get("label"), str) else ""
        buckets.setdefault((used, reset), []).append(label)

    groups = list(buckets.items())[:MAX_WINDOWS]
    names = [_group_label(labels) for _, labels in groups]
    # Two buckets from the same house - say, a Gemini group that ran out and
    # one that did not - would both read "Gemini". Name those by model instead,
    # so every meter on the card says what it measures.
    collisions = {name for name in names if names.count(name) > 1}
    windows = [
        {
            "label": _detailed_label(labels) if name in collisions else name,
            "used": used,
            "resetText": _reset_text(reset),
        }
        for name, ((used, reset), labels) in zip(names, groups)
    ]

    # The plan lives in two places and only one of them is about Antigravity.
    # `planStatus.planInfo` is the Windsurf/Codeium plan this server grew up
    # with - it speaks of Cascade, Flow credits and tab-to-jump, and it reads
    # "Pro" for an account on the free Google tier. `userTier` is the one the
    # IDE itself shows.
    tier = _dig(status, "userTier", "name")
    note = tier.strip() if isinstance(tier, str) and tier.strip() else "Antigravity 로컬 연동"
    if not windows:
        note = "조회 가능한 사용 한도가 없습니다"

    return {
        "provider": PROVIDER,
        "windows": windows,
        "note": note[:120],
        "updatedAt": time.time(),
        "source": "local",
        "error": None,
    }


# --------------------------------------------------------------------------
# what the widget calls
# --------------------------------------------------------------------------
def read_antigravity(
    timeout: float = 15.0,
    cancel: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Return an Antigravity snapshot, rediscovering the server when needed.

    The cached endpoint is tried first because discovery costs a PowerShell
    process. Every restart of the app invalidates it - new port, new token -
    so a failed call drops the cache and looks again exactly once.
    """
    global _cached

    _halt(cancel)
    endpoint = _cached
    if endpoint is not None:
        try:
            return normalize(request(endpoint.url, endpoint.token, timeout=timeout))
        except AntigravityLoginRequired:
            # The address and the token are fine; it is the account that is
            # missing. Throwing the endpoint away here would mean hunting the
            # server down again on every retry for a problem out on the web.
            raise
        except AntigravityError:
            _cached = None

    endpoint, payload = discover(cancel=cancel)
    snapshot = normalize(payload)
    _cached = endpoint
    return snapshot


def forget() -> None:
    """Drop the cached endpoint; the next read discovers from scratch."""
    global _cached
    _cached = None


# --------------------------------------------------------------------------
# reading with the IDE closed
# --------------------------------------------------------------------------
#: Flags copied from the command line the app itself uses, minus the two that
#: tie the server to the Electron process (``--host_bridge_*``).
#:
#: ``--enable_sidecars`` and ``--override_ide_version`` are not decoration:
#: drop either and the backend answers without the Gemini group's quota at
#: all - measured, 9 models and 6 figures with them, 6 and 3 without. A card
#: that quietly loses a limit reads as "no limit", so the version is taken
#: from the installed app, which moves with every auto-update.
BASE_ARGS = (
    "--standalone",
    "--override_ide_name", "antigravity",
    "--subclient_type", "hub",
    "--override_user_agent_name", "antigravity",
    "--app_data_dir", APP_DATA_MARKER,
    "--api_server_url", "https://generativelanguage.googleapis.com",
    "--cloud_code_endpoint", "https://daily-cloudcode-pa.googleapis.com",
    "--enable_sidecars",
)

#: How long to wait for a server we started to answer with real figures.
LAUNCH_TIMEOUT = 25.0


def server_binary() -> Optional[Path]:
    """The language server shipped with the installed app, if it is there."""
    roots = []
    for variable in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(variable)
        if base:
            roots.append(Path(base) / "Programs" / "antigravity")
            roots.append(Path(base) / "antigravity")
    for root in roots:
        candidate = root / "resources" / "bin" / "language_server.exe"
        if candidate.is_file():
            return candidate
    return None


class _FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def file_version(path: Path) -> str:
    """``2.32.1`` from a Windows executable's version resource, or ""."""
    if os.name != "nt" or not path.is_file():
        return ""
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        block = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, block):
            return ""
        pointer, length = ctypes.c_void_p(), ctypes.c_uint()
        if not version.VerQueryValueW(block, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return ""
        info = ctypes.cast(pointer, ctypes.POINTER(_FIXEDFILEINFO)).contents
        parts = (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
        )
        return ".".join(str(p) for p in parts)
    except Exception:  # noqa: BLE001
        return ""


def app_version(binary: Path) -> str:
    """Version of the installed app that owns this language server."""
    # <root>/resources/bin/language_server.exe -> <root>/Antigravity.exe
    return file_version(binary.parent.parent.parent / "Antigravity.exe")


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _quota_count(payload: Dict[str, Any]) -> int:
    """How many models carry a real quota figure in this reply."""
    configs = _dig(payload.get("userStatus"), "cascadeModelConfigData", "clientModelConfigs")
    if not isinstance(configs, list):
        return 0
    return sum(
        1 for c in configs
        if isinstance(c, dict) and isinstance(c.get("quotaInfo"), dict) and c["quotaInfo"]
    )


#: Polls with no new figures before the reply counts as complete.
SETTLE_POLLS = 3


def launch_and_read(
    timeout: float = LAUNCH_TIMEOUT,
    cancel: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Start the IDE's own server just long enough to ask it one question.

    Antigravity publishes no usage page, so with the app closed there is
    nothing to read - except the very program the app would have started. It
    is run the way the app runs it, against the same account directory, asked
    once, and shut down again: alive for about three seconds.

    Only reached when no Antigravity server is already listening, so this
    never puts a second one beside the app's.
    """
    binary = server_binary()
    version = app_version(binary) if binary is not None else ""
    if binary is None or not version:
        # No version means a reply with the Gemini group missing, and a card
        # that silently drops a limit is worse than one that says it cannot
        # read. Report it the same way a closed app is reported.
        raise AntigravityNotRunning("Antigravity가 실행 중이 아닙니다 · 앱을 켜면 표시됩니다")

    _halt(cancel)
    port = _free_port()
    token = uuid.uuid4().hex
    command = [
        str(binary), *BASE_ARGS,
        "--override_ide_version", version,
        "--https_server_port", str(port),
        "--csrf_token", token,
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            # The server logs steadily; a pipe nobody drains fills up and
            # blocks it before it ever answers.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    except OSError as error:
        raise AntigravityError("Antigravity 사용량 서버를 띄우지 못했습니다") from error

    url = f"https://127.0.0.1:{port}{RPC_PATH}"
    deadline = time.monotonic() + timeout
    best: Optional[Dict[str, Any]] = None
    best_count, settled = -1, 0
    try:
        while time.monotonic() < deadline:
            _halt(cancel)
            if process.poll() is not None:
                break
            try:
                payload = request(url, token, timeout=3)
            except AntigravityLoginRequired:
                raise
            except AntigravityError:
                time.sleep(0.4)
                continue
            # The groups do not all arrive together: the Claude bucket answers
            # first and Gemini's lands a moment later. Taking the first reply
            # that has any figure at all drops whichever group was still in
            # flight, so wait until the count stops growing.
            count = _quota_count(payload)
            if count > best_count:
                best, best_count, settled = payload, count, 0
            else:
                settled += 1
            if best_count > 0 and settled >= SETTLE_POLLS:
                return normalize(best)
            time.sleep(0.5)
        if best is not None:
            return normalize(best)
        raise AntigravityError("Antigravity 사용량 서버가 응답하지 않습니다")
    finally:
        _shutdown(process)


def _shutdown(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=3)
