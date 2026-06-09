"""Round-9 #2: Administrator is mandatory — refuse to run unelevated.

Almost everything PingPair does to set up a test (firewall rules, static IP,
toggling Wi-Fi via ``netsh``) needs Administrator. Before Round-9 the launcher
would *fall through and run unelevated* when the user denied UAC; now it simply
exits. (A follow-up dropped the explanatory message box — the UAC prompt the
user just dismissed is feedback enough, so denial just silently closes.) These
tests pin the decision logic in ``_ensure_admin_or_relaunch_on_windows`` via its
injectable callables so we never touch the real ctypes / ShellExecute / sys.exit
machinery.
"""

from __future__ import annotations

from pingpair.__main__ import _ensure_admin_or_relaunch_on_windows


def _run(
    *,
    platform: str = "win32",
    no_elevate: bool = False,
    admin,
    rc=None,
):
    """Drive the gate with fully injected dependencies and return a result dict.

    ``admin`` is the value ``is_admin()`` returns (True / False / None).
    ``rc`` is the value ``elevate()`` returns; only consulted when not admin.
    """
    state = {"exit_calls": [], "elevate_calls": 0}

    def _elevate():
        state["elevate_calls"] += 1
        return rc

    _ensure_admin_or_relaunch_on_windows(
        platform=platform,
        no_elevate=no_elevate,
        is_admin=lambda: admin,
        elevate=_elevate,
        exit_fn=lambda code=0: state["exit_calls"].append(code),
    )
    return state


# --- skip conditions: never touch elevation / exit -------------------------


def test_non_windows_is_a_noop():
    st = _run(platform="linux", admin=False, rc=5)
    assert st["exit_calls"] == []
    assert st["elevate_calls"] == 0


def test_env_opt_out_is_a_noop():
    st = _run(no_elevate=True, admin=False, rc=5)
    assert st["exit_calls"] == []
    assert st["elevate_calls"] == 0


def test_already_admin_runs_without_elevating():
    st = _run(admin=True, rc=5)
    assert st["exit_calls"] == []
    assert st["elevate_calls"] == 0


def test_cannot_determine_admin_falls_through_to_run():
    # IsUserAnAdmin raised -> admin is None -> run rather than risk a lockout.
    st = _run(admin=None, rc=5)
    assert st["exit_calls"] == []
    assert st["elevate_calls"] == 0


# --- not admin: elevate, and gate on the result ----------------------------


def test_uac_accepted_exits_zero():
    st = _run(admin=False, rc=42)  # rc > 32 == accepted
    assert st["elevate_calls"] == 1
    assert st["exit_calls"] == [0]


def test_uac_denied_exits_one_silently():
    st = _run(admin=False, rc=5)  # SE_ERR_ACCESSDENIED
    assert st["elevate_calls"] == 1
    assert st["exit_calls"] == [1]


def test_elevation_api_failure_exits_one():
    st = _run(admin=False, rc=None)  # ShellExecute itself failed
    assert st["elevate_calls"] == 1
    assert st["exit_calls"] == [1]


def test_rc_boundary_32_is_denied():
    st = _run(admin=False, rc=32)  # must be strictly > 32 to count as success
    assert st["exit_calls"] == [1]


def test_rc_boundary_33_is_accepted():
    st = _run(admin=False, rc=33)
    assert st["exit_calls"] == [0]


# --- the real default helpers are safe to import / call shapes -------------


def test_default_helpers_exist_and_are_callable():
    from pingpair.__main__ import _default_elevate, _default_is_admin

    assert callable(_default_is_admin)
    assert callable(_default_elevate)


def test_no_message_box_helper_remains():
    # The explanatory message box was removed; guard against it creeping back.
    import pingpair.__main__ as m

    assert not hasattr(m, "_show_admin_required_message")
