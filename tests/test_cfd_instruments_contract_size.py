"""
Tests for broker-specific contract-size overrides (app.cfd_risk.instruments).

Contract size varies by broker/prop-firm (e.g. XAGUSD is 5000 oz on FundedNext
and the MT5 standard, but 1000 oz on some IC Markets entities). Overriding it
must rescale the USD-per-move values linearly, and position sizing must follow.
"""

from __future__ import annotations

import pytest

from app.cfd_risk import instruments
from app.cfd_risk.instruments import get_instrument, set_contract_size
from app.cfd_risk.position_sizing import calculate_lot_size


@pytest.fixture()
def restore_silver():
    """Snapshot XAGUSD and restore it after, so overrides don't leak."""
    original = get_instrument("XAGUSD")
    yield
    instruments._INSTRUMENTS["XAGUSD"] = original


def test_default_silver_matches_ic_markets_1000oz():
    # Default matches our live IC Markets feed (verified via symbol_info).
    s = get_instrument("XAGUSD")
    assert s.contract_size == 1000.0
    assert s.point_value_per_lot == 1000.0
    assert s.pip_value_per_lot == 1.0


def test_override_scales_point_and_pip_values(restore_silver):
    # Override to the FundedNext / MT5-standard 5000 oz.
    set_contract_size("XAGUSD", 5000.0)
    s = get_instrument("XAGUSD")
    assert s.contract_size == 5000.0
    # 5x larger contract -> 5x larger USD-per-move values.
    assert s.point_value_per_lot == 5000.0
    assert s.pip_value_per_lot == 5.0


def test_override_changes_position_sizing_5x(restore_silver):
    # $1000 risk, 0.1 price stop.
    at_1000 = calculate_lot_size("XAGUSD", 100_000.0, 1.0, 0.1)
    set_contract_size("XAGUSD", 5000.0)
    at_5000 = calculate_lot_size("XAGUSD", 100_000.0, 1.0, 0.1)
    # Larger contract -> each lot risks 5x as much -> 1/5 the lots for same risk.
    assert at_5000.lot_size == pytest.approx(at_1000.lot_size / 5.0, rel=1e-6)


def test_apply_broker_spec_derives_point_value_from_ticks(restore_silver):
    # IC Markets silver: contract 1000, tick_value 1.0, tick_size 0.001.
    from app.cfd_risk.instruments import apply_broker_spec
    apply_broker_spec("XAGUSD", contract_size=1000.0, tick_value=1.0, tick_size=0.001)
    s = get_instrument("XAGUSD")
    assert s.contract_size == 1000.0
    assert s.point_value_per_lot == pytest.approx(1000.0)   # 1.0 / 0.001
    assert s.pip_value_per_lot == pytest.approx(1.0)        # 1000 * 0.001

    # Now a 5000-oz broker (tick_value 5.0 for the same 0.001 tick).
    apply_broker_spec("XAGUSD", contract_size=5000.0, tick_value=5.0, tick_size=0.001)
    s = get_instrument("XAGUSD")
    assert s.point_value_per_lot == pytest.approx(5000.0)


def test_apply_broker_spec_rejects_bad_ticks(restore_silver):
    from app.cfd_risk.instruments import apply_broker_spec
    with pytest.raises(ValueError):
        apply_broker_spec("XAGUSD", contract_size=1000.0, tick_value=1.0, tick_size=0.0)


def test_override_no_change_is_noop(restore_silver):
    before = get_instrument("XAGUSD")
    same = set_contract_size("XAGUSD", 1000.0)
    assert same is before  # unchanged instance


def test_override_rejects_bad_value(restore_silver):
    with pytest.raises(ValueError):
        set_contract_size("XAGUSD", 0.0)


def test_override_unknown_symbol_raises():
    with pytest.raises(KeyError):
        set_contract_size("NOPE", 1000.0)


def test_env_override_applied(monkeypatch, restore_silver):
    monkeypatch.setenv("CFD_CONTRACT_SIZE_XAGUSD", "5000")
    instruments._apply_env_overrides()
    assert get_instrument("XAGUSD").contract_size == 5000.0


def test_env_override_invalid_ignored(monkeypatch, restore_silver):
    monkeypatch.setenv("CFD_CONTRACT_SIZE_XAGUSD", "notanumber")
    instruments._apply_env_overrides()          # must not raise
    assert get_instrument("XAGUSD").contract_size == 1000.0  # unchanged default
