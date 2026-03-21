#!/usr/bin/env python3
"""Проверка EOA и proxy (Safe) адресов для Polymarket.

Запуск: python scripts/check_wallet_addresses.py
Docker: docker compose -f docker-compose.dev.yml --profile dev --env-file .env run --rm dev
  python scripts/check_wallet_addresses.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eth_account import Account
from web3 import Web3

from utils.config import load_config
from utils.polygon_web3 import make_polygon_web3


def main() -> None:
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = load_config()
    pk = cfg.secrets.private_key
    if not pk:
        print("PRIVATE_KEY не задан в .env")
        return

    eoa = Account.from_key(pk).address
    print("EOA (адрес из PRIVATE_KEY):", eoa)

    # py_clob_client: get_address возвращает signer = EOA, не proxy
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=POLYGON,
        )
        clob_addr = client.get_address()
        print("ClobClient.get_address():", clob_addr or "(None)")
        if clob_addr and clob_addr.lower() != eoa.lower():
            print("  ^ отличается от EOA — возможен funder в клиенте")
    except Exception as e:
        print("ClobClient:", e)

    proxy_env = (cfg.secrets.proxy_address or "").strip()
    print()
    if proxy_env:
        proxy = Web3.to_checksum_address(proxy_env)
        print("Proxy (POLYMARKET_PROXY_ADDRESS):", proxy)
        w3 = make_polygon_web3(cfg.secrets.polygon_rpc_url)
        code = w3.eth.get_code(proxy)
        if code and len(code) > 2:
            print("Тип proxy: Контракт (Safe)")
            try:
                req = urllib.request.Request(
                    f"https://relayer-v2.polymarket.com/deployed?address={proxy}",
                    headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read().decode())
                deployed = data.get("deployed", False)
                print("Relayer /deployed:", "да" if deployed else "нет")
            except Exception as e:
                print("Relayer check:", e)
        else:
            print("Тип proxy: EOA (не контракт)")
    else:
        print("POLYMARKET_PROXY_ADDRESS не задан")
        print(
            "  Укажи адрес Safe в .env — его можно взять в Polymarket: "
            "Settings → Wallet или при депозите."
        )

    print()
    print("Polygonscan EOA:", f"https://polygonscan.com/address/{eoa}")
    if proxy_env:
        print("Polygonscan Proxy:", f"https://polygonscan.com/address/{proxy_env}")


if __name__ == "__main__":
    main()
