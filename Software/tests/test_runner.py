"""Smoke tests for ProcRunner using harmless host commands.

We can't spawn iperf3/fping in CI (no Cygwin runtime, no NIC), but we can
still verify the streaming + capture machinery against ``cmd /c echo``
and ``python -c print_loop`` style processes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pingpair.core.runner import ProcRunner, ProcSpec


def _python_spec(*python_args: str, name: str = "py") -> ProcSpec:
    return ProcSpec(
        name=name,
        argv=[sys.executable, *python_args],
        cwd=Path("."),
    )


def test_proc_runner_captures_stdout() -> None:
    spec = _python_spec("-c", "print('hello'); print('world')")
    result = ProcRunner(spec).run_blocking()
    assert result.returncode == 0
    assert "hello" in result.stdout
    assert "world" in result.stdout


def test_proc_runner_invokes_line_callback_per_line() -> None:
    received: list[tuple[str, str]] = []
    spec = _python_spec(
        "-u", "-c", "import sys; [print(i) for i in range(3)]",
        name="counter",
    )
    runner = ProcRunner(spec, on_line=lambda src, ln: received.append((src, ln)))
    runner.run_blocking()
    # We should see lines '0', '1', '2' in order, all tagged with 'counter'.
    only_payload = [r[1] for r in received if r[0] == "counter"]
    assert "0" in only_payload
    assert "1" in only_payload
    assert "2" in only_payload


def test_proc_runner_records_nonzero_exit() -> None:
    spec = _python_spec("-c", "import sys; sys.exit(7)")
    result = ProcRunner(spec).run_blocking()
    assert result.returncode == 7


def test_proc_runner_handles_missing_executable() -> None:
    spec = ProcSpec(
        name="nope",
        argv=["definitely-not-a-real-binary-xyz"],
        cwd=Path("."),
    )
    result = ProcRunner(spec).run_blocking()
    assert result.returncode == -1
    assert result.stderr  # message captured


def test_proc_runner_stop_terminates_long_lived_proc() -> None:
    """Spawn a process that would run forever; stop() must end it."""
    spec = _python_spec(
        "-u", "-c",
        "import time\n"
        "while True:\n"
        "    print('tick', flush=True)\n"
        "    time.sleep(0.05)\n",
        name="ticker",
    )
    runner = ProcRunner(spec)
    runner.start()
    # let it produce a few lines
    import time as _t
    _t.sleep(0.3)
    runner.stop(timeout_s=2.0)
    result = runner.wait()
    # On Windows, terminate-by-Popen returns non-zero; on POSIX -SIGTERM (=15
    # which Python encodes as -15). The exact code is platform-specific.
    assert result.returncode != 0
    assert "tick" in result.stdout


def test_command_string_quotes_paths_with_spaces() -> None:
    spec = ProcSpec(
        name="x",
        argv=["C:\\Program Files\\fping.exe", "-v"],
        cwd=Path("."),
    )
    assert spec.command_string == '"C:\\Program Files\\fping.exe" -v'
