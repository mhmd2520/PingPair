"""Wire format for the Server↔Client control channel.

Newline-delimited JSON over a single TCP socket on
``cfg.network.control_port`` (default 5202 — separate from iperf3's 5201).
Each frame is one UTF-8 ``Message`` JSON object terminated by ``\\n``.

See also ``../../../../plan.md`` §"TCP control protocol" for the message
catalogue + sequence diagram.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

PROTOCOL_VERSION: int = 1

MessageType = Literal[
    "HELLO",
    "HELLO_OK",
    "START_SWEEP",
    "START_CASE",
    "SERVER_READY",
    "CASE_DONE",
    "SERVER_RESULT",
    "FINISH",
    "HEARTBEAT",
    "ERROR",
]


class Message(BaseModel):
    """A single control-channel message."""

    type: MessageType
    seq: int = Field(default=0, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)

    # ---- conveniences -------------------------------------------------

    @classmethod
    def hello(cls, *, client_version: str, plan_id: str = "default-20") -> Message:
        return cls(
            type="HELLO",
            payload={
                "protocol_version": PROTOCOL_VERSION,
                "client_version": client_version,
                "plan_id": plan_id,
            },
        )

    @classmethod
    def hello_ok(cls, *, server_version: str) -> Message:
        return cls(
            type="HELLO_OK",
            payload={
                "protocol_version": PROTOCOL_VERSION,
                "server_version": server_version,
            },
        )

    @classmethod
    def start_sweep(cls, *, total_cases: int, sweep_id: str = "") -> Message:
        """One-way notification from Client → Server announcing a new sweep.

        ``total_cases`` is the number of START_CASE messages the Client
        will send before FINISH. The Server uses it to (a) reset its
        per-sweep counter — without this, the counter accumulates across
        every sweep in the listener's lifetime and the GUI title shows
        misleading numbers — and (b) display the correct denominator in
        "case M of N" style status strings.

        ``sweep_id`` is opaque (typically the Client's wall-clock start
        timestamp) and only used for log correlation.

        Sent immediately after HELLO_OK and before the first START_CASE.
        Pre-protocol-START_SWEEP servers will log it as an unexpected
        message but stay alive — back-compat is best-effort.
        """
        return cls(
            type="START_SWEEP",
            payload={
                "total_cases": total_cases,
                "sweep_id": sweep_id,
            },
        )

    @classmethod
    def start_case(
        cls,
        *,
        case_idx: int,
        payload_bytes: int,
        bandwidth_mbps: int,
        duration_s: int,
        protocol: Literal["udp", "tcp"],
        server_ip: str,
        client_ip: str,
    ) -> Message:
        return cls(
            type="START_CASE",
            payload={
                "case_idx": case_idx,
                "payload_bytes": payload_bytes,
                "bandwidth_mbps": bandwidth_mbps,
                "duration_s": duration_s,
                "protocol": protocol,
                "server_ip": server_ip,
                "client_ip": client_ip,
            },
        )

    @classmethod
    def server_ready(cls, *, case_idx: int, pid: int) -> Message:
        return cls(
            type="SERVER_READY",
            payload={"case_idx": case_idx, "pid": pid},
        )

    @classmethod
    def case_done(cls, *, case_idx: int) -> Message:
        return cls(type="CASE_DONE", payload={"case_idx": case_idx})

    @classmethod
    def server_result(cls, *, case_idx: int, iperf3_json: str, returncode: int) -> Message:
        return cls(
            type="SERVER_RESULT",
            payload={
                "case_idx": case_idx,
                "iperf3_json": iperf3_json,
                "returncode": returncode,
            },
        )

    @classmethod
    def finish(cls) -> Message:
        return cls(type="FINISH")

    @classmethod
    def heartbeat(cls, *, ts: float) -> Message:
        return cls(type="HEARTBEAT", payload={"ts": ts})

    @classmethod
    def error(cls, *, code: str, message: str, case_idx: int | None = None) -> Message:
        payload: dict[str, Any] = {"code": code, "message": message}
        if case_idx is not None:
            payload["case_idx"] = case_idx
        return cls(type="ERROR", payload=payload)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


class ProtocolError(RuntimeError):
    """Raised on malformed frames or disconnect."""


# Ceiling on a single newline-delimited frame. The largest legitimate
# message is a SERVER_RESULT carrying one case's iperf3 --json blob
# (tens of KB); 16 MiB is a generous headroom that still bounds the
# read buffer so a peer that never sends a newline can't OOM the app.
_MAX_FRAME_BYTES = 16 * 1024 * 1024


def encode(message: Message) -> bytes:
    """Serialise a Message to a single newline-terminated UTF-8 line."""
    return (message.model_dump_json() + "\n").encode("utf-8")


def decode(line: bytes | str) -> Message:
    """Deserialise a single frame back to a Message; raises ProtocolError."""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"non-UTF8 frame: {exc}") from exc
    line = line.strip()
    if not line:
        raise ProtocolError("empty frame")
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc
    try:
        return Message.model_validate(data)
    except ValidationError as exc:
        raise ProtocolError(f"schema mismatch: {exc}") from exc


# ---------------------------------------------------------------------------
# Stream helpers — wrap a TCP socket for line-oriented reads/writes
# ---------------------------------------------------------------------------


class FramedSocket:
    """Buffered line reader/writer over a connected TCP socket.

    Exists because :func:`socket.socket.makefile` doesn't expose flush in a
    way that plays well with Cygwin sockets, and we want explicit control
    over partial-read buffering.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = bytearray()

    def read_message(self, *, timeout_s: float | None = None) -> Message:
        if timeout_s is not None:
            self.sock.settimeout(timeout_s)
        while b"\n" not in self._buf:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout as exc:
                raise ProtocolError(f"read timeout after {timeout_s}s") from exc
            except OSError as exc:
                # A peer *abort* (Windows WinError 10053/10054 — connection
                # aborted / reset, the common case when the other side's
                # process dies or its control socket is closed under a Stop)
                # raises a raw OSError from recv() rather than the clean
                # empty-chunk close handled just below. Normalise it to
                # ProtocolError like the timeout and clean-close cases, so it
                # routes through every caller's ``except ProtocolError``
                # disconnect handling (which softens "peer closed" into
                # "client disconnected") instead of escaping raw and leaking a
                # "[WinError 10053] …" string into a user-facing banner.
                raise ProtocolError(f"peer closed connection: {exc}") from exc
            if not chunk:
                raise ProtocolError("peer closed connection")
            self._buf.extend(chunk)
            if len(self._buf) > _MAX_FRAME_BYTES:
                raise ProtocolError(
                    f"control frame exceeded {_MAX_FRAME_BYTES} bytes "
                    "with no newline — peer misbehaving"
                )
        line, _, rest = self._buf.partition(b"\n")
        self._buf = bytearray(rest)
        return decode(line)

    def write_message(self, msg: Message) -> None:
        self.sock.sendall(encode(msg))

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
