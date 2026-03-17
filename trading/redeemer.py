from __future__ import annotations

import asyncio

from web3 import Web3

from utils.config import SecretsConfig
from utils.logger import logger

CTF_EXCHANGE_ABI = [
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

NEG_RISK_ADAPTER_ABI = [
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_EXCHANGE = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


class Redeemer:
    """Redeem winning positions on Polymarket via smart contract."""

    def __init__(self, secrets: SecretsConfig):
        self._secrets = secrets
        self._w3: Web3 | None = None

    @property
    def w3(self) -> Web3:
        if self._w3 is None:
            self._w3 = Web3(
                Web3.HTTPProvider(self._secrets.polygon_rpc_url)
            )
        return self._w3

    async def redeem(
        self, condition_id: str, neg_risk: bool = False
    ) -> str | None:
        """Call redeemPositions on the CTF contract.

        Returns transaction hash on success, None on failure.
        """
        try:
            contract_addr = (
                NEG_RISK_ADAPTER if neg_risk else CTF_EXCHANGE
            )
            abi = (
                NEG_RISK_ADAPTER_ABI if neg_risk else CTF_EXCHANGE_ABI
            )

            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(contract_addr),
                abi=abi,
            )

            proxy = Web3.to_checksum_address(
                self._secrets.proxy_address
            )
            cond_bytes = Web3.to_bytes(hexstr=condition_id)

            tx = contract.functions.redeemPositions(
                cond_bytes, []
            ).build_transaction(
                {
                    "from": proxy,
                    "nonce": self.w3.eth.get_transaction_count(proxy),
                    "gas": 300000,
                    "gasPrice": self.w3.eth.gas_price,
                }
            )

            signed = self.w3.eth.account.sign_transaction(
                tx, self._secrets.private_key
            )
            tx_hash = await asyncio.to_thread(
                self.w3.eth.send_raw_transaction,
                signed.raw_transaction,
            )
            hex_hash = tx_hash.hex()
            logger.info("Redeem tx sent: %s", hex_hash)
            return hex_hash

        except Exception as e:
            logger.error(
                "Redeem failed for %s: %s", condition_id[:16], e
            )
            return None
