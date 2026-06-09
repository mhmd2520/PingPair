"""Round-trip and framing tests for the control protocol."""

from __future__ import annotations

import socket

import pytest

from pingpair.core.control.protocol import (
    FramedSocket,
    Message,
    ProtocolError,
    decode,
    encode,
)


class _FakeSock:
    """Minimal stand-in for a connected socket — drives ``recv`` outcomes."""

    def __init__(self, *, recv_exc: BaseException | None = None,
                 recv_ret: bytes = b"") -> None:
        self._recv_exc = recv_exc
        self._recv_ret = recv_ret
        self.timeout_set: float | None = None

    def settimeout(self, t: float) -> None:
        self.timeout_set = t

    def recv(self, _n: int) -> bytes:
        if self._recv_exc is not None:
            raise self._recv_exc
        return self._recv_ret


def test_hello_round_trips() -> None:
    msg = Message.hello(client_version="0.1.0", plan_id="default-20")
    out = decode(encode(msg))
    assert out.type == "HELLO"
    assert out.payload["client_version"] == "0.1.0"
    assert out.payload["plan_id"] == "default-20"


def test_start_sweep_round_trips() -> None:
    """START_SWEEP carries the case count + opaque sweep_id."""
    msg = Message.start_sweep(total_cases=12, sweep_id="1778437006")
    out = decode(encode(msg))
    assert out.type == "START_SWEEP"
    assert out.payload["total_cases"] == 12
    assert out.payload["sweep_id"] == "1778437006"


def test_start_sweep_default_sweep_id_is_empty() -> None:
    """sweep_id is optional; defaults to empty string for callers that
    don't bother with log correlation."""
    msg = Message.start_sweep(total_cases=20)
    out = decode(encode(msg))
    assert out.payload["sweep_id"] == ""
    assert out.payload["total_cases"] == 20


def test_start_case_carries_full_case() -> None:
    msg = Message.start_case(
        case_idx=7,
        payload_bytes=600,
        bandwidth_mbps=70,
        duration_s=30,
        protocol="udp",
        server_ip="192.168.1.1",
        client_ip="192.168.1.2",
    )
    out = decode(encode(msg))
    assert out.payload["case_idx"] == 7
    assert out.payload["bandwidth_mbps"] == 70
    assert out.payload["protocol"] == "udp"


def test_server_result_carries_iperf_json_blob() -> None:
    msg = Message.server_result(case_idx=1, iperf3_json='{"end": {}}', returncode=0)
    out = decode(encode(msg))
    assert out.payload["iperf3_json"] == '{"end": {}}'
    assert out.payload["returncode"] == 0


def test_decode_rejects_empty_frame() -> None:
    with pytest.raises(ProtocolError, match="empty frame"):
        decode(b"")


def test_decode_rejects_invalid_json() -> None:
    with pytest.raises(ProtocolError, match="invalid JSON"):
        decode(b'{not json\n')


def test_decode_rejects_unknown_type() -> None:
    bad = b'{"type": "BANANA", "seq": 0, "payload": {}}\n'
    with pytest.raises(ProtocolError):
        decode(bad)


def test_encoded_frame_ends_with_newline() -> None:
    msg = Message.heartbeat(ts=123.45)
    raw = encode(msg)
    assert raw.endswith(b"\n")
    # Exactly one terminator — no trailing whitespace inside the JSON.
    assert raw.count(b"\n") == 1


# --- read_message transport-error normalisation (mid-sweep disconnect) -------


def test_read_message_clean_close_raises_protocol_error() -> None:
    """An empty recv (orderly FIN) surfaces as a ProtocolError, not EOF."""
    framed = FramedSocket(_FakeSock(recv_ret=b""))
    with pytest.raises(ProtocolError, match="peer closed"):
        framed.read_message(timeout_s=1.0)


def test_read_message_timeout_wraps_as_protocol_error() -> None:
    """socket.timeout is an OSError subclass — it MUST hit the timeout branch
    (re-checked by the polled read loops), never the abort branch below."""
    framed = FramedSocket(_FakeSock(recv_exc=socket.timeout("timed out")))
    with pytest.raises(ProtocolError, match="read timeout"):
        framed.read_message(timeout_s=1.0)


def test_read_message_wraps_peer_abort_oserror_as_protocol_error() -> None:
    """Regression: a Windows peer-abort (WinError 10053/10054) raises a raw
    OSError from recv(). It must be normalised to a ProtocolError carrying
    "peer closed" so callers' ``except ProtocolError`` disconnect handling
    softens the wording instead of leaking "[WinError 10053] …" into the
    cross-tab banner."""
    abort = OSError(10053, "An established connection was aborted")
    framed = FramedSocket(_FakeSock(recv_exc=abort))
    with pytest.raises(ProtocolError, match="peer closed") as excinfo:
        framed.read_message(timeout_s=1.0)
    # The raw OS detail is preserved (for logs) but chained, not surfaced bare.
    assert excinfo.value.__cause__ is abort


def test_read_message_wraps_connection_reset_as_protocol_error() -> None:
    """ConnectionResetError (WinError 10054 / errno ECONNRESET) is an OSError
    subclass and routes through the same disconnect normalisation."""
    framed = FramedSocket(_FakeSock(recv_exc=ConnectionResetError("reset")))
    with pytest.raises(ProtocolError, match="peer closed"):
        framed.read_message(timeout_s=1.0)
