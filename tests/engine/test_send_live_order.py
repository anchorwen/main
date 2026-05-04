import pytest

from scripts.send_live_order import _validate_sl_tp


def test_validate_sl_tp_accepts_valid_long():
    _validate_sl_tp(side="long", stop_loss=4500.0, take_profit=4700.0, reference_price=4600.0)


def test_validate_sl_tp_rejects_invalid_long():
    with pytest.raises(ValueError):
        _validate_sl_tp(side="long", stop_loss=4700.0, take_profit=4500.0, reference_price=4600.0)


def test_validate_sl_tp_accepts_valid_short():
    _validate_sl_tp(side="short", stop_loss=4700.0, take_profit=4500.0, reference_price=4600.0)


def test_validate_sl_tp_rejects_invalid_short():
    with pytest.raises(ValueError):
        _validate_sl_tp(side="short", stop_loss=4500.0, take_profit=4700.0, reference_price=4600.0)
