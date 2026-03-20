from __future__ import annotations

import asyncio

from web3 import Web3

from utils.config import DEFAULT_POLYGON_RPC_URL
from utils.logger import logger
from utils.polygon_web3 import make_polygon_web3

USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]


class WalletManager:
    def __init__(
        self, private_key: str, rpc_url: str = DEFAULT_POLYGON_RPC_URL
    ):
        self._private_key = private_key
        self._rpc_url = rpc_url
        self._w3: Web3 | None = None
        self._address: str | None = None

    @property
    def w3(self) -> Web3:
        if self._w3 is None:
            self._w3 = make_polygon_web3(self._rpc_url)
        return self._w3

    @property
    def address(self) -> str:
        if self._address is None:
            account = self.w3.eth.account.from_key(self._private_key)
            self._address = account.address
        return self._address

    async def get_matic_balance(self) -> float:
        try:
            balance_wei = await asyncio.to_thread(
                self.w3.eth.get_balance, self.address
            )
            return float(self.w3.from_wei(balance_wei, "ether"))
        except Exception as e:
            logger.error("Failed to get MATIC balance: %s", e)
            return 0.0

    async def get_usdc_balance(self) -> float:
        try:
            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=USDC_ABI,
            )
            balance = await asyncio.to_thread(
                contract.functions.balanceOf(self.address).call
            )
            return balance / 1e6
        except Exception as e:
            logger.error("Failed to get USDC balance: %s", e)
            return 0.0
