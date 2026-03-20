#!/usr/bin/env python3
"""Реальные позиции кошелька из Data API Polymarket.

Запуск: python scripts/check_positions.py [адрес]
  Без аргумента — берёт POLYMARKET_PROXY_ADDRESS из .env

Docker: docker compose -f docker-compose.dev.yml --profile dev --env-file .env run --rm dev python scripts/check_positions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.data_api import fetch_positions
from utils.config import load_config


def main() -> None:
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    addr = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    if not addr:
        cfg = load_config()
        addr = (cfg.secrets.proxy_address or "").strip()
    if not addr:
        print("Укажи адрес: python scripts/check_positions.py 0x...")
        print("Или задай POLYMARKET_PROXY_ADDRESS в .env")
        return

    r = fetch_positions(addr)

    print(f"Адрес: {addr}")
    print(f"Найдено позиций: {len(r)}")
    print()
    for p in r:
        print(f"  title:        {p.get('title')}")
        print(f"  conditionId:  {p.get('conditionId')}")
        print(f"  asset:        {p.get('asset')}")
        print(f"  size:         {p.get('size')}")
        print(f"  outcome:      {p.get('outcome')}")
        print()


if __name__ == "__main__":
    main()
