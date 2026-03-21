#!/usr/bin/env python3
"""На каком кошельке (EOA vs Proxy) лежат CTF outcome-токены для redeem.

Берёт кандидатов из CLOB (выигрышный нетто) и проверяет balanceOf для каждого
condition_id на EOA и Proxy. Если токены на Proxy — нужны POLYMARKET_SIG_TYPE=2
и POLYMARKET_PROXY_ADDRESS.

Запуск: python scripts/check_token_holder.py
Docker: docker compose -f docker-compose.dev.yml --profile dev --env-file .env run --rm dev
  python scripts/check_token_holder.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account
from web3 import Web3

from trading.clob_account import (
    EPS_SHARES,
    fetch_all_clob_trades,
    net_shares_by_token_id,
    token_to_condition_map,
    winning_token_id_from_market,
)
from trading.redeemer import CTF_EXCHANGE, CTF_REDEEM_ABI, USDC_E, ZERO_BYTES32
from utils.config import load_config
from utils.polygon_web3 import make_polygon_web3


def main() -> None:
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = load_config()
    pk = cfg.secrets.private_key
    if not pk:
        print("PRIVATE_KEY не задан")
        return

    eoa = Account.from_key(pk).address
    proxy_raw = (cfg.secrets.proxy_address or "").strip()
    proxy = proxy_raw if proxy_raw else None

    print("EOA (PRIVATE_KEY):  ", eoa)
    print("Proxy (POLYMARKET_PROXY_ADDRESS):", proxy or "(не задан)")
    print()

    if not proxy:
        print("POLYMARKET_PROXY_ADDRESS не задан — проверяем только EOA.")
        print("Добавь proxy в .env, чтобы понять, не там ли токены.")
        print()

    w3 = make_polygon_web3(cfg.secrets.polygon_rpc_url)
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_EXCHANGE),
        abi=CTF_REDEEM_ABI,
    )

    async def run() -> None:
        from database.db import Database
        from trading.executor import TradeExecutor

        db = Database()
        await db.connect()
        executor = TradeExecutor(cfg, db)
        try:
            fetched = await fetch_all_clob_trades(executor.client)
            if fetched.error:
                print("Ошибка CLOB get_trades:", fetched.error)
                return

            raw = fetched.trades
            net = net_shares_by_token_id(raw)
            t2m = token_to_condition_map(raw)

            candidates: list[tuple[str, str, float]] = []
            for token_id, sh in net.items():
                if sh <= EPS_SHARES:
                    continue
                cond = t2m.get(token_id)
                if not cond:
                    continue
                candidates.append((cond, token_id, sh))

            if not candidates:
                print("Нет кандидатов на redeem (выигрышный нетто по CLOB).")
                return

            # Проверяем только closed + winning
            checked = 0
            for condition_id, token_id, _sh in sorted(candidates, key=lambda x: x[0])[:10]:
                try:
                    market_data = await asyncio.to_thread(
                        executor.client.get_market, condition_id
                    )
                except Exception as e:
                    print(f"  {condition_id} get_market: {e}")
                    continue
                if not market_data:
                    continue
                win_tid = winning_token_id_from_market(market_data)
                if not win_tid or win_tid != token_id:
                    continue

                checked += 1
                cond = Web3.to_bytes(hexstr=condition_id)
                col_yes = ctf.functions.getCollectionId(ZERO_BYTES32, cond, 1).call()
                col_no = ctf.functions.getCollectionId(ZERO_BYTES32, cond, 2).call()
                pid_yes = ctf.functions.getPositionId(
                    Web3.to_checksum_address(USDC_E), col_yes
                ).call()
                pid_no = ctf.functions.getPositionId(
                    Web3.to_checksum_address(USDC_E), col_no
                ).call()

                try:
                    tid_int = int(str(token_id).strip())
                except (ValueError, TypeError):
                    tid_int = 0

                print(f"condition_id: {condition_id} token_id: {token_id[:24]}…")
                for label, addr in [("EOA", eoa), ("Proxy", proxy)]:
                    if not addr:
                        continue
                    a = Web3.to_checksum_address(addr)
                    b_api = int(ctf.functions.balanceOf(a, tid_int).call()) if tid_int else 0
                    b_yes = int(ctf.functions.balanceOf(a, pid_yes).call())
                    b_no = int(ctf.functions.balanceOf(a, pid_no).call())
                    total = b_yes + b_no
                    if b_api:
                        total = max(total, b_api)
                    tok = "да" if total else "нет"
                    print(f"  {label}: API_tid={b_api}  yes={b_yes}  no={b_no}  → токены={tok}")
                print()

            if checked == 0:
                print("Нет закрытых выигрышных рынков среди кандидатов.")
        finally:
            await db.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
