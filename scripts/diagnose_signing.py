"""Run this ON THE SERVER to diagnose order signing issues.

Usage: docker compose exec bot python scripts/diagnose_signing.py
   or: cd ~/polymarket-bot && python scripts/diagnose_signing.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)
from py_clob_client.order_builder.constants import BUY


def main():
    pk = os.environ.get("PRIVATE_KEY", "")
    if not pk:
        print("ERROR: PRIVATE_KEY not set")
        return

    eoa = Account.from_key(pk).address
    print(f"EOA address: {eoa}")

    creds = ApiCreds(
        api_key=os.environ.get("POLYMARKET_API_KEY", ""),
        api_secret=os.environ.get("POLYMARKET_API_SECRET", ""),
        api_passphrase=os.environ.get("POLYMARKET_API_PASSPHRASE", ""),
    )

    # Step 1: find a real tradeable market
    print("\n=== Step 1: Finding a test market ===")
    c0 = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds)
    markets = c0.get_sampling_simplified_markets()
    test_market = None
    if isinstance(markets, dict):
        for m in markets.get("data", []):
            tokens = m.get("tokens", [])
            for t in tokens:
                price = float(t.get("price", 0))
                if 0.01 <= price <= 0.05:
                    test_market = {
                        "token_id": t["token_id"],
                        "price": price,
                        "outcome": t.get("outcome", ""),
                        "tick_size": m.get("min_tick_size", "0.01"),
                        "neg_risk": m.get("neg_risk", False),
                        "question": m.get("question", "")[:50],
                    }
                    break
            if test_market:
                break

    if not test_market:
        print("No suitable test market found, using first available")
        for m in markets.get("data", [])[:1]:
            tokens = m.get("tokens", [])
            if tokens:
                t = tokens[0]
                test_market = {
                    "token_id": t["token_id"],
                    "price": max(float(t.get("price", 0.01)), 0.01),
                    "outcome": t.get("outcome", ""),
                    "tick_size": m.get("min_tick_size", "0.01"),
                    "neg_risk": m.get("neg_risk", False),
                    "question": m.get("question", "")[:50],
                }

    print(f"Market: {test_market['question']}")
    print(f"Token: {test_market['token_id'][:30]}...")
    print(f"Price: {test_market['price']}, tick: {test_market['tick_size']}, neg_risk: {test_market['neg_risk']}")

    # Step 2: test all combinations
    print("\n=== Step 2: Testing signature combinations ===")
    size = round(1.0 / test_market["price"], 2)
    order_args = OrderArgs(
        price=test_market["price"],
        size=size,
        side=BUY,
        token_id=test_market["token_id"],
    )
    options = PartialCreateOrderOptions(
        tick_size=test_market["tick_size"],
        neg_risk=test_market["neg_risk"],
    )

    results = []
    for sig in [0, 1, 2]:
        for funder_val in [None, eoa]:
            label = f"sig={sig}, funder={'EOA' if funder_val else 'None'}"
            try:
                kwargs = dict(
                    host="https://clob.polymarket.com",
                    key=pk, chain_id=137, creds=creds,
                    signature_type=sig,
                )
                if funder_val:
                    kwargs["funder"] = funder_val
                c = ClobClient(**kwargs)

                signed = c.create_order(order_args, options)
                resp = c.post_order(signed, OrderType.FOK)
                status = resp.get("status", "") if isinstance(resp, dict) else str(resp)
                print(f"  {label}: ✅ POSTED! status={status}")
                results.append((label, "SUCCESS", status))
            except Exception as e:
                err = str(e)
                if "invalid signature" in err.lower():
                    print(f"  {label}: ❌ invalid signature")
                    results.append((label, "INVALID_SIG", ""))
                elif "restricted" in err.lower() or "geoblock" in err.lower():
                    print(f"  {label}: ⚠️  GEO-BLOCKED (sig might be valid)")
                    results.append((label, "GEO_BLOCK", ""))
                elif "not enough balance" in err.lower() or "balance" in err.lower():
                    print(f"  {label}: ✅ SIGNATURE VALID (insufficient balance)")
                    results.append((label, "VALID_NO_BALANCE", ""))
                else:
                    print(f"  {label}: ⚠️  {err[:120]}")
                    results.append((label, "OTHER", err[:80]))

    # Step 3: try re-deriving API keys
    print("\n=== Step 3: Try deriving fresh API keys ===")
    for sig in [0, 1, 2]:
        for funder_val in [None, eoa]:
            label = f"sig={sig}, funder={'EOA' if funder_val else 'None'}"
            try:
                kwargs = dict(
                    host="https://clob.polymarket.com",
                    key=pk, chain_id=137,
                    signature_type=sig,
                )
                if funder_val:
                    kwargs["funder"] = funder_val
                c = ClobClient(**kwargs)
                new_creds = c.derive_api_key()
                print(f"  {label}: ✅ derived key={new_creds.api_key[:16]}...")

                # Now test order with these new creds
                c2 = ClobClient(**{**kwargs, "creds": new_creds})
                signed = c2.create_order(order_args, options)
                resp = c2.post_order(signed, OrderType.FOK)
                print(f"    → ORDER POSTED: {resp}")
            except Exception as e:
                err = str(e)
                if "invalid signature" in err.lower():
                    print(f"  {label}: ❌ invalid signature")
                elif "not enough balance" in err.lower() or "balance" in err.lower():
                    print(f"  {label}: ✅ WORKS! (just need balance)")
                elif "restricted" in err.lower():
                    print(f"  {label}: ⚠️  geo-blocked")
                else:
                    print(f"  {label}: {err[:100]}")

    print("\n=== Summary ===")
    working = [r for r in results if r[1] in ("SUCCESS", "VALID_NO_BALANCE")]
    if working:
        print(f"Working combination: {working[0][0]}")
    else:
        print("No working combination found with current API keys.")
        print("Try running with freshly derived keys (Step 3 results above).")


if __name__ == "__main__":
    main()
