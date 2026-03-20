from __future__ import annotations

from trading.clob_account import (
    net_shares_by_token_id,
    outcome_label_for_token,
    token_to_condition_map,
    winning_token_id_from_market,
)


def test_net_shares_buy_sell():
    trades = [
        {
            "asset_id": "100",
            "market": "0xabc",
            "side": "BUY",
            "size": "10",
        },
        {
            "asset_id": "100",
            "market": "0xabc",
            "side": "SELL",
            "size": "3",
        },
    ]
    net = net_shares_by_token_id(trades)
    assert net["100"] == 7.0


def test_token_to_condition_map():
    trades = [
        {"asset_id": "t1", "market": "0xm1", "side": "BUY", "size": "1"},
        {"asset_id": "t2", "market": "0xm2", "side": "BUY", "size": "2"},
    ]
    m = token_to_condition_map(trades)
    assert m["t1"] == "0xm1"
    assert m["t2"] == "0xm2"


def test_winning_token_from_market():
    m = {
        "closed": True,
        "tokens": [
            {"token_id": "yes", "outcome": "Yes", "price": "0.99"},
            {"token_id": "no", "outcome": "No", "price": "0.01"},
        ],
    }
    assert winning_token_id_from_market(m) == "yes"


def test_winning_token_not_closed():
    m = {"closed": False, "tokens": []}
    assert winning_token_id_from_market(m) is None


def test_outcome_label_for_token():
    trades = [
        {"asset_id": "99", "market": "0x1", "side": "BUY", "size": "1", "outcome": "Yes"},
    ]
    assert outcome_label_for_token(trades, "99") == "Yes"
