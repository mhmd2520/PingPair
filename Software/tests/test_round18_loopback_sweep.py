"""Round 18 (PP) — loopback multi-case sweep.

Loopback mode used to be a single-case runner. PP gives it the full
sweep via a socket-less mode of ``ControlClient.run_sweep``: each case
is a local ``CaseRunner(loopback=True)`` and the emitted event stream
matches the two-machine control-channel path, so the GUI sweep panel
is shared between the Client and Loopback roles.

These tests mock ``CaseRunner`` so no real iperf3 / fping is spawned.
"""

from types import SimpleNamespace

from pingpair.config import load_default_config
from pingpair.core.control import client as client_module
from pingpair.core.control.client import ControlClient


class _FakeCaseRunner:
    """Stand-in for CaseRunner — records how it was built, runs instantly."""

    instances: list["_FakeCaseRunner"] = []

    def __init__(self, cfg, case, *, loopback, on_line=None) -> None:
        self.cfg = cfg
        self.case = case
        self.loopback = loopback
        self.on_line = on_line
        _FakeCaseRunner.instances.append(self)

    def run(self):
        return SimpleNamespace(ok=True, error=None, case=self.case)

    def stop(self) -> None:
        pass


def test_loopback_sweep_runs_subset_with_no_control_channel(monkeypatch) -> None:
    _FakeCaseRunner.instances.clear()
    monkeypatch.setattr(client_module, "CaseRunner", _FakeCaseRunner)

    events: list[tuple[str, dict]] = []
    cc = ControlClient(load_default_config(), on_event=lambda n, d: events.append((n, d)))
    result = cc.run_sweep(loopback=True, selected_indexes=[1, 2, 3])

    names = [n for n, _ in events]
    assert names.count("case_starting") == 3
    assert names.count("case_done") == 3
    assert names.count("sweep_finished") == 1
    # Loopback skips the control channel entirely.
    assert "connecting" not in names
    assert "connected" not in names
    assert "error" not in names
    assert len(result.cases) == 3
    # Every case ran as a loopback CaseRunner.
    assert len(_FakeCaseRunner.instances) == 3
    assert all(r.loopback is True for r in _FakeCaseRunner.instances)


def test_loopback_sweep_full_plan_is_twenty_cases(monkeypatch) -> None:
    _FakeCaseRunner.instances.clear()
    monkeypatch.setattr(client_module, "CaseRunner", _FakeCaseRunner)

    cc = ControlClient(load_default_config())
    result = cc.run_sweep(loopback=True)
    assert len(result.cases) == 20


def test_loopback_sweep_stops_early_when_user_stops(monkeypatch) -> None:
    cfg = load_default_config()
    cc = ControlClient(cfg)

    class _StopAfterFirstCase:
        def __init__(self, cfg, case, *, loopback, on_line=None) -> None:
            self.case = case

        def run(self):
            cc.stop()  # simulate the user pressing Stop mid-case
            return SimpleNamespace(ok=True, error=None, case=self.case)

        def stop(self) -> None:
            pass

    monkeypatch.setattr(client_module, "CaseRunner", _StopAfterFirstCase)
    result = cc.run_sweep(loopback=True, selected_indexes=[1, 2, 3, 4, 5])
    # Case 1 runs and sets the stop flag; the loop breaks after it.
    assert len(result.cases) == 1
