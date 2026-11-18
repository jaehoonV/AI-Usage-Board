"""A minimal RFC 6455 client, just enough to speak CDP over loopback.

Why not a library: the only WebSocket this app opens is a short-lived,
unencrypted connection to 127.0.0.1 carrying a handful of small JSON messages.
Pulling in a dependency for that would mean Claude and Gemini silently fail to
connect on any machine where the package was not installed.

Scope on purpose: no TLS, no extensions, no fragmentation on send, text frames
only. Anything outside that raises.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
from typing import Optional
from urllib.parse import urlparse

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

MAX_MESSAGE = 4 * 1024 * 1024


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """Blocking client for ws://127.0.0.1:<port>/<path>."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise WebSocketError("Only ws:// is supported")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        self._socket = socket.create_connection((host, port), timeout=timeout)
        self._socket.settimeout(timeout)
        self._buffer = b""
        self._handshake(host, port, path)

    # -- setup ------------------------------------------------------------
    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._socket.sendall(request.encode("ascii"))

        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise WebSocketError("Connection closed during handshake")
            header += chunk
            if len(header) > 65536:
                raise WebSocketError("Handshake response too large")
        head, _, rest = header.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise WebSocketError(f"Upgrade refused: {status.decode('latin-1')[:120]}")
        self._buffer = rest

    # -- io ---------------------------------------------------------------
    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise WebSocketError("Connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        header = bytearray([0x80 | OPCODE_TEXT])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)

    def _read_frame(self):
        first, second = self._read(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        if length > MAX_MESSAGE:
            raise WebSocketError("Frame too large")
        mask = self._read(4) if masked else b""
        payload = self._read(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self) -> str:
        """Return the next complete text message, answering pings on the way."""
        data = b""
        opcode: Optional[int] = None
        while True:
            fin, frame_opcode, payload = self._read_frame()
            if frame_opcode == OPCODE_PING:
                self._send_control(OPCODE_PONG, payload)
                continue
            if frame_opcode == OPCODE_PONG:
                continue
            if frame_opcode == OPCODE_CLOSE:
                raise WebSocketError("Connection closed by peer")
            if frame_opcode in (OPCODE_TEXT, OPCODE_BINARY):
                opcode = frame_opcode
            elif frame_opcode != OPCODE_CONTINUATION:
                raise WebSocketError(f"Unsupported opcode {frame_opcode}")
            data += payload
            if len(data) > MAX_MESSAGE:
                raise WebSocketError("Message too large")
            if fin:
                break
        if opcode == OPCODE_BINARY:
            raise WebSocketError("Binary frame received")
        return data.decode("utf-8", "replace")

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload[:125]))
        frame = bytes([0x80 | opcode, 0x80 | len(masked)]) + mask + masked
        self._socket.sendall(frame)

    # -- convenience ------------------------------------------------------
    def send_json(self, value) -> None:
        self.send_text(json.dumps(value))

    def recv_json(self):
        return json.loads(self.recv_text())

    def close(self) -> None:
        try:
            self._send_control(OPCODE_CLOSE)
        except OSError:
            pass
        finally:
            try:
                self._socket.close()
            except OSError:
                pass

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
