from __future__ import annotations

from web3 import Web3

from utils.logger import logger


class WalletManager:
    def __init__(self, private_key: str, rpc_url: str = "https://polygon-rpc.com"):
        self._private_key = private_key
        self._rpc_url = rpc_url
        self._w3: Web3 | None = None
        self._address: str | None = None

    @property
    def w3(self) -> Web3:
        if self._w3 is None:
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
        return self._w3

    @property
    def address(self) -> str:
        if self._address is None:
            account = self.w3.eth.account.from_key(self._private_key)
            self._address = account.address
        return self._address

    async def get_matic_balance(self) -> float:
        try:
            balance_wei = self.w3.eth.get_balance(self.address)
            return float(self.w3.from_wei(balance_wei, "ether"))
        except Exception as e:
            logger.error("Failed to get MATIC balance: %s", e)
            return 0.0
