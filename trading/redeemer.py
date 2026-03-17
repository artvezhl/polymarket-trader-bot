from __future__ import annotations

import asyncio

from eth_account import Account
try:
    from eth_account.messages import encode_structured_data
except ImportError:
    from eth_account.messages import encode_typed_data as encode_structured_data
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

SAFE_EXEC_TX_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"},
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
POLYGON_CHAIN_ID = 137


class Redeemer:
    """Redeem winning positions on Polymarket via Safe proxy wallet."""

    def __init__(self, secrets: SecretsConfig):
        self._secrets = secrets
        self._w3: Web3 | None = None
        self._account: Account | None = None

    @property
    def w3(self) -> Web3:
        if self._w3 is None:
            self._w3 = Web3(
                Web3.HTTPProvider(self._secrets.polygon_rpc_url)
            )
        return self._w3

    @property
    def account(self) -> Account:
        if self._account is None:
            self._account = Account.from_key(self._secrets.private_key)
        return self._account

    def _build_redeem_calldata(
        self, condition_id: str, neg_risk: bool
    ) -> bytes:
        """Build redeemPositions calldata for CTF/NegRisk contract."""
        contract_addr = (
            NEG_RISK_ADAPTER if neg_risk else CTF_EXCHANGE
        )
        abi = NEG_RISK_ADAPTER_ABI if neg_risk else CTF_EXCHANGE_ABI
        contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(contract_addr),
            abi=abi,
        )
        cond_bytes = Web3.to_bytes(hexstr=condition_id)
        built = contract.functions.redeemPositions(
            cond_bytes, []
        ).build_transaction(
            {"from": self.account.address}
        )
        data_hex = built["data"]
        return bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)

    def _build_safe_tx_signature(
        self,
        safe_address: str,
        to: str,
        data: bytes,
        nonce: int,
        chain_id: int = POLYGON_CHAIN_ID,
    ) -> bytes:
        """Build EIP-712 signature for Safe execTransaction."""
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "SafeTx": [
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "data", "type": "bytes"},
                    {"name": "operation", "type": "uint8"},
                    {"name": "safeTxGas", "type": "uint256"},
                    {"name": "baseGas", "type": "uint256"},
                    {"name": "gasPrice", "type": "uint256"},
                    {"name": "gasToken", "type": "address"},
                    {"name": "refundReceiver", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                ],
            },
            "primaryType": "SafeTx",
            "domain": {
                "chainId": chain_id,
                "verifyingContract": Web3.to_checksum_address(safe_address),
            },
            "message": {
                "to": Web3.to_checksum_address(to),
                "value": 0,
                "data": data,
                "operation": 0,
                "safeTxGas": 0,
                "baseGas": 0,
                "gasPrice": 0,
                "gasToken": Web3.to_checksum_address(ZERO_ADDRESS),
                "refundReceiver": Web3.to_checksum_address(ZERO_ADDRESS),
                "nonce": nonce,
            },
        }
        signable = encode_structured_data(typed_data)
        signed = self.account.sign_message(signable)
        return signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([signed.v])

    async def redeem(
        self, condition_id: str, neg_risk: bool = False
    ) -> str | None:
        """Execute redeem via Safe proxy: Safe calls redeemPositions on CTF.

        Returns transaction hash on success, None on failure.
        """
        try:
            proxy = Web3.to_checksum_address(self._secrets.proxy_address)
            target_contract = (
                NEG_RISK_ADAPTER if neg_risk else CTF_EXCHANGE
            )

            redeem_data = self._build_redeem_calldata(condition_id, neg_risk)

            safe_contract = self.w3.eth.contract(
                address=proxy,
                abi=SAFE_EXEC_TX_ABI,
            )

            nonce = await asyncio.to_thread(
                safe_contract.functions.nonce().call
            )
            signatures = await asyncio.to_thread(
                self._build_safe_tx_signature,
                proxy,
                target_contract,
                redeem_data,
                nonce,
            )

            eoa = self.account.address
            exec_built = safe_contract.functions.execTransaction(
                Web3.to_checksum_address(target_contract),
                0,
                redeem_data,
                0,
                0,
                0,
                0,
                Web3.to_checksum_address(ZERO_ADDRESS),
                Web3.to_checksum_address(ZERO_ADDRESS),
                signatures,
            ).build_transaction({"from": eoa})

            tx = {
                "from": eoa,
                "to": proxy,
                "value": 0,
                "data": exec_built["data"],
                "nonce": self.w3.eth.get_transaction_count(eoa),
                "gas": 400000,
                "gasPrice": self.w3.eth.gas_price,
            }

            signed = self.w3.eth.account.sign_transaction(
                tx, self._secrets.private_key
            )
            tx_hash = await asyncio.to_thread(
                self.w3.eth.send_raw_transaction,
                signed.raw_transaction,
            )
            hex_hash = tx_hash.hex()
            logger.info("Redeem tx sent via Safe: %s", hex_hash)
            return hex_hash

        except Exception as e:
            logger.error(
                "Redeem failed for %s: %s", condition_id[:16], e
            )
            return None
