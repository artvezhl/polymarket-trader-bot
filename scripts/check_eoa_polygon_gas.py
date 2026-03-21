"""Проверка: адрес EOA из PRIVATE_KEY и нативный баланс POL на Polygon (газ).

Запуск (из корня репо): python scripts/check_eoa_polygon_gas.py
В Docker: docker compose -f docker-compose.dev.yml --profile dev --env-file .env run --rm dev python scripts/check_eoa_polygon_gas.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.redeemer import Redeemer
from utils.config import load_config


def main() -> None:
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = load_config()
    if not cfg.secrets.private_key:
        print("PRIVATE_KEY не задан в .env")
        return
    r = Redeemer(cfg.secrets)
    addr = r.account.address
    bal = r.w3.eth.get_balance(addr)
    gp = r.w3.eth.gas_price
    est = 500_000 * gp
    print("RPC:", cfg.secrets.polygon_rpc_url[:48] + "…")
    print("EOA (адрес из PRIVATE_KEY):", addr)
    print("Нативный баланс (Polygon PoS):", r.w3.from_wei(bal, "ether"), "POL")
    print("Оценка max за 500k gas @ текущий gas_price:", r.w3.from_wei(est, "ether"), "POL")
    print("Polygonscan:", f"https://polygonscan.com/address/{addr}")
    err = r.check_eoa_pays_gas()
    print("---")
    if err:
        print("Бот отклонит redeem:", err[:300], "…" if len(err) > 300 else "")
    else:
        print("check_eoa_pays_gas: OK (для отправки tx средств достаточно по грубой оценке)")
    print(
        "\nЕсли на Polygonscan баланс 0, а вы «пополнили MATIC»: "
        "убедитесь, что перевод был в сеть Polygon (chain 137), "
        "а не Ethereum / Arbitrum / BSC, и на тот же адрес, что выше."
    )


if __name__ == "__main__":
    main()
