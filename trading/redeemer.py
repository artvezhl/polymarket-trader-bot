from __future__ import annotations

import asyncio
import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from database.db import Database
from database.models import Trade
from trading.clob_account import winning_token_id_from_market
from trading.data_api import fetch_positions_async
from trading.executor import TradeExecutor
from utils.config import SecretsConfig
from utils.logger import logger
from utils.polygon_web3 import make_polygon_web3

# Polymarket Conditional Tokens (CTF) on Polygon
CTF_EXCHANGE = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
# USDC.e (bridged) — collateral for standard markets
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

USDC_TRANSFER_EVENT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "src", "type": "address"},
            {"indexed": True, "name": "dst", "type": "address"},
            {"indexed": False, "name": "wad", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]

# CTF ConditionalTokens — после redeemPositions (см. Polygonscan → Event Logs)
CTF_PAYOUT_REDEMPTION_EVENT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "redeemer", "type": "address"},
            {"indexed": True, "name": "collateralToken", "type": "address"},
            {"indexed": True, "name": "parentCollectionId", "type": "bytes32"},
            {"indexed": False, "name": "conditionId", "type": "bytes32"},
            {"indexed": False, "name": "indexSets", "type": "uint256[]"},
            {"indexed": False, "name": "payout", "type": "uint256"},
        ],
        "name": "PayoutRedemption",
        "type": "event",
    },
]

CTF_REDEEM_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSet", "type": "uint256"},
        ],
        "name": "getCollectionId",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "name": "getPositionId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "pure",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

NEG_RISK_ADAPTER_ABI = [
    {
        "inputs": [],
        "name": "wcol",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amounts", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

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
ZERO_BYTES32 = bytes(32)
POLYGON_CHAIN_ID = 137
# Binary market: both outcome index sets (Polymarket / Gnosis CTF)
BINARY_INDEX_SETS = [1, 2]


@dataclass
class RedeemResult:
    tx_hash: str | None
    success: bool
    error: str = ""


@dataclass
class RedeemBatchSummary:
    total: int
    succeeded: int
    errors: list[str]


class Redeemer:
    """Redeem выигрышных позиций: с EOA (SIG_TYPE=0) или через Gnosis Safe (SIG_TYPE=2 + proxy)."""

    def __init__(self, secrets: SecretsConfig):
        self._secrets = secrets
        self._w3: Web3 | None = None
        self._account: Account | None = None

    @property
    def w3(self) -> Web3:
        if self._w3 is None:
            self._w3 = make_polygon_web3(self._secrets.polygon_rpc_url)
        return self._w3

    @property
    def account(self) -> Account:
        if self._account is None:
            self._account = Account.from_key(self._secrets.private_key)
        return self._account

    def _uses_relayer(self) -> bool:
        """True если Safe + Relayer keys — газ не нужен (gasless)."""
        return (
            self._secrets.signature_type != 0
            and bool(self._secrets.relayer_api_key)
            and bool(self._secrets.relayer_api_key_address)
        )

    def _usdc_e_payout_to_in_receipt(self, receipt: Any, recipient: str) -> int:
        """Сумма USDC.e (raw, 6 decimals) по логам Transfer на адрес recipient."""
        rcp = Web3.to_checksum_address(recipient)
        usdc = Web3.to_checksum_address(USDC_E)
        c = self.w3.eth.contract(address=usdc, abi=USDC_TRANSFER_EVENT_ABI)
        total = 0
        for log in receipt.get("logs", []):
            try:
                if Web3.to_checksum_address(log["address"]) != usdc:
                    continue
                decoded = c.events.Transfer().process_log(log)
                args = decoded["args"]
                if Web3.to_checksum_address(args["dst"]) == rcp:
                    amt = args.get("wad") or args.get("value")
                    total += int(amt or 0)
            except Exception:
                continue
        return total

    def _ctf_payout_redemptions_in_receipt(self, receipt: Any) -> list[tuple[str, int]]:
        """Пары (condition_id 0x…, payout raw USDC.e 6 decimals) из CTF."""
        ctf = Web3.to_checksum_address(CTF_EXCHANGE)
        c = self.w3.eth.contract(
            address=ctf, abi=CTF_PAYOUT_REDEMPTION_EVENT_ABI
        )
        out: list[tuple[str, int]] = []
        for log in receipt.get("logs", []):
            try:
                if Web3.to_checksum_address(log["address"]) != ctf:
                    continue
                decoded = c.events.PayoutRedemption().process_log(log)
                args = decoded["args"]
                cid = args["conditionId"]
                h = cid.hex() if hasattr(cid, "hex") else bytes(cid).hex()
                if not h.startswith("0x"):
                    h = "0x" + h
                out.append((h, int(args["payout"])))
            except Exception:
                continue
        return out

    def _log_redeem_settlement(self, receipt: Any, recipient: str, ctx: str) -> None:
        """Объясняет успешный tx: CTF payout и/или USDC Transfer (payout=0 → без Transfer — норма)."""
        payouts = self._ctf_payout_redemptions_in_receipt(receipt)
        usdc_raw = self._usdc_e_payout_to_in_receipt(receipt, recipient)
        for cid_hex, pay in payouts:
            if pay > 0:
                logger.info(
                    "Redeem %s: CTF PayoutRedemption %s payout_raw=%s (~$%.2f USDC.e)",
                    ctx,
                    cid_hex[:18] + "…",
                    pay,
                    pay / 1e6,
                )
            else:
                logger.warning(
                    "Redeem %s: CTF PayoutRedemption %s payout=0 — on-chain redeem "
                    "выполнен, USDC не начислялся (нет выигрышных токенов к выкупу / "
                    "уже выкуплено ранее / нетто в CLOB не отражает баланс CTF).",
                    ctx,
                    cid_hex[:18] + "…",
                )
        if usdc_raw > 0:
            logger.info(
                "Redeem %s: USDC.e Transfer +%s raw (~$%.2f) → %s",
                ctx,
                usdc_raw,
                usdc_raw / 1e6,
                recipient,
            )
        elif not payouts:
            logger.warning(
                "Redeem %s: нет CTF PayoutRedemption (возможно neg-risk adapter); "
                "нет USDC.e Transfer на %s — см. Polygonscan → Logs.",
                ctx,
                recipient,
            )

    def _should_skip_standard_redeem(
        self,
        holder: str,
        condition_id: str,
        outcome_token_id: str | None,
        *,
        holder_is_eoa: bool,
    ) -> str | None:
        """Если на holder нет CTF-токенов к выкупу — текст; иначе None."""
        ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_EXCHANGE),
            abi=CTF_REDEEM_ABI,
        )
        h = Web3.to_checksum_address(holder)
        who = "EOA (PRIVATE_KEY)" if holder_is_eoa else "Safe (POLYMARKET_PROXY_ADDRESS)"
        eoa_hint = (
            " Торгуете через Polymarket Safe — верните POLYMARKET_SIG_TYPE=2 и "
            "POLYMARKET_PROXY_ADDRESS=адрес Safe; иначе проверяется не тот кошелёк."
            if holder_is_eoa
            else ""
        )
        bal_api: int | None = None
        if outcome_token_id and str(outcome_token_id).strip():
            try:
                tid = int(str(outcome_token_id).strip())
            except (ValueError, TypeError):
                tid = 0
            if tid > 0:
                bal_api = int(ctf.functions.balanceOf(h, tid).call())
                if bal_api > 0:
                    return None
        cond = Web3.to_bytes(hexstr=condition_id)
        try:
            col_yes = ctf.functions.getCollectionId(ZERO_BYTES32, cond, 1).call()
            col_no = ctf.functions.getCollectionId(ZERO_BYTES32, cond, 2).call()
            pid_yes = ctf.functions.getPositionId(
                Web3.to_checksum_address(USDC_E), col_yes
            ).call()
            pid_no = ctf.functions.getPositionId(
                Web3.to_checksum_address(USDC_E), col_no
            ).call()
            b_yes = int(ctf.functions.balanceOf(h, pid_yes).call())
            b_no = int(ctf.functions.balanceOf(h, pid_no).call())
        except Exception as e:
            logger.warning(
                "CTF precheck balances failed %s: %s", condition_id[:16], e
            )
            return None
        # #region agent log
        try:
            _log_dir = Path(__file__).resolve().parent.parent / ".cursor"
            _log_dir.mkdir(parents=True, exist_ok=True)
            with open(
                _log_dir / "debug-e205d8.log",
                "a",
                encoding="utf-8",
            ) as _df:
                _df.write(
                    json.dumps(
                        {
                            "sessionId": "e205d8",
                            "timestamp": int(time.time() * 1000),
                            "location": "redeemer.py:_should_skip_standard_redeem",
                            "message": "precheck_balances",
                            "data": {
                                "cid16": condition_id[:16],
                                "holder": h[:10] + "…" if len(h) > 10 else h,
                                "holder_is_eoa": holder_is_eoa,
                                "bal_api": bal_api,
                                "b_yes": b_yes,
                                "b_no": b_no,
                                "outcome_tid_raw": (
                                    str(outcome_token_id)[:50]
                                    if outcome_token_id
                                    else None
                                ),
                            },
                            "hypothesisId": "H1-H4",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception:
            pass
        # #endregion
        if b_yes > 0 or b_no > 0:
            if outcome_token_id and str(outcome_token_id).strip():
                try:
                    tid_chk = int(str(outcome_token_id).strip())
                except (ValueError, TypeError):
                    tid_chk = 0
                if tid_chk > 0 and bal_api == 0:
                    logger.info(
                        "Redeem precheck %s: balanceOf(API token_id)=0, но USDC "
                        "outcome yes=%s no=%s — отправляем redeem (id из API ≠ positionId)",
                        condition_id[:16],
                        b_yes,
                        b_no,
                    )
            return None
        if b_yes == 0 and b_no == 0:
            # #region agent log
            try:
                _log_dir = Path(__file__).resolve().parent.parent / ".cursor"
                _log_dir.mkdir(parents=True, exist_ok=True)
                with open(
                    _log_dir / "debug-e205d8.log",
                    "a",
                    encoding="utf-8",
                ) as _df:
                    _df.write(
                        json.dumps(
                            {
                                "sessionId": "e205d8",
                                "timestamp": int(time.time() * 1000),
                                "location": "redeemer.py:_should_skip_standard_redeem",
                                "message": "skip_no_usdc_positions",
                                "data": {
                                    "cid16": condition_id[:16],
                                    "holder": h,
                                    "who": who,
                                    "bal_api": bal_api,
                                    "b_yes": b_yes,
                                    "b_no": b_no,
                                },
                                "hypothesisId": "H1",
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except Exception:
                pass
            # #endregion
            return (
                f"пропуск: на {who} нет USDC-collateral outcome tokens "
                f"(API token_id и yes/no CTF позиции = 0) — redeemPositions дал бы payout=0."
                + eoa_hint
            )

    def _standard_redeem_tx_outcome(
        self,
        receipt: Any,
        hex_hash: str,
        recipient: str,
        ctx: str,
    ) -> RedeemResult:
        """После status=1: если есть CTF PayoutRedemption и сумма payout=0 — не success."""
        self._log_redeem_settlement(receipt, recipient, ctx)
        payouts = self._ctf_payout_redemptions_in_receipt(receipt)
        if not payouts:
            return RedeemResult(hex_hash, True, "")
        tot = sum(p[1] for p in payouts)
        if tot == 0:
            return RedeemResult(
                hex_hash,
                False,
                "CTF PayoutRedemption payout=0 — USDC не зачислен; не считать выкуп успешным",
            )
        return RedeemResult(hex_hash, True, "")

    def check_eoa_pays_gas(self) -> str | None:
        """Газ за raw tx платит только EOA из PRIVATE_KEY; баланс Safe/proxy для этого не используется."""
        addr = self.account.address
        bal = self.w3.eth.get_balance(addr)
        _ui_short = (
            " Веб Polymarket = gasless relayer. Бот шлёт tx в Polygon RPC сам — "
            "см. gasless в docs.polymarket.com."
        )
        if bal == 0:
            logger.warning(
                "Gas check: EOA %s native balance 0 on Polygon (RPC gas payer is PRIVATE_KEY, not Safe)",
                addr,
            )
            return (
                f"На адресе подписанта {addr} сеть Polygon показывает 0 POL для газа. "
                "Пополните именно этот адрес (из PRIVATE_KEY), не только Safe/proxy: "
                "газ платит EOA, не кошелёк Polymarket."
                + _ui_short
            )
        try:
            gp = self.w3.eth.gas_price
            est_wei = 500_000 * gp
            if bal < est_wei:
                need = float(self.w3.from_wei(est_wei, "ether"))
                have = float(self.w3.from_wei(bal, "ether"))
                return (
                    f"Мало POL для газа на {addr}: нужно ~{need:.4f} за типичную tx, есть {have:.6f}."
                    + _ui_short
                )
        except Exception:
            pass
        return None

    def _build_standard_redeem_data(self, condition_id: str) -> bytes:
        ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_EXCHANGE),
            abi=CTF_REDEEM_ABI,
        )
        cond = Web3.to_bytes(hexstr=condition_id)
        built = ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC_E),
            ZERO_BYTES32,
            cond,
            BINARY_INDEX_SETS,
        ).build_transaction({"from": self.account.address})
        data_hex = built["data"]
        return bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)

    def _neg_risk_balances(self, proxy: str, condition_id: str) -> tuple[int, int] | None:
        adapter = self.w3.eth.contract(
            address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
            abi=NEG_RISK_ADAPTER_ABI,
        )
        wcol = adapter.functions.wcol().call()
        ctf = self.w3.eth.contract(
            address=Web3.to_checksum_address(CTF_EXCHANGE),
            abi=CTF_REDEEM_ABI,
        )
        cond = Web3.to_bytes(hexstr=condition_id)
        col_yes = ctf.functions.getCollectionId(ZERO_BYTES32, cond, 1).call()
        col_no = ctf.functions.getCollectionId(ZERO_BYTES32, cond, 2).call()
        pid_yes = ctf.functions.getPositionId(
            Web3.to_checksum_address(wcol), col_yes
        ).call()
        pid_no = ctf.functions.getPositionId(
            Web3.to_checksum_address(wcol), col_no
        ).call()
        proxy_cs = Web3.to_checksum_address(proxy)
        b_yes = ctf.functions.balanceOf(proxy_cs, pid_yes).call()
        b_no = ctf.functions.balanceOf(proxy_cs, pid_no).call()
        return (int(b_yes), int(b_no))

    def _build_neg_risk_redeem_data(self, condition_id: str, proxy: str) -> tuple[bytes, str]:
        balances = self._neg_risk_balances(proxy, condition_id)
        if balances is None:
            return b"", "balance lookup failed"
        b_yes, b_no = balances
        if b_yes == 0 and b_no == 0:
            return b"", "no conditional tokens for this holder on this condition"

        adapter = self.w3.eth.contract(
            address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
            abi=NEG_RISK_ADAPTER_ABI,
        )
        cond = Web3.to_bytes(hexstr=condition_id)

        # ✅ 1. encode_abi — только кодируем calldata, без симуляции
        data = adapter.encode_abi(
            "redeemPositions",
            args=[cond, [b_yes, b_no]]
        )

        return bytes.fromhex(data[2:] if data.startswith("0x") else data), ""

    def _build_safe_tx_signature(
        self,
        safe_address: str,
        to: str,
        data: bytes,
        nonce: int,
        chain_id: int = POLYGON_CHAIN_ID,
    ) -> bytes:
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
        signable = encode_typed_data(full_message=typed_data)
        signed = self.account.sign_message(signable)
        v = signed.v
        if v < 27:
            v += 27

        logger.debug(
            "Safe sig: r=%s s=%s v=%s",
            signed.r, signed.s, v
        )

        return (
            signed.r.to_bytes(32, "big")
            + signed.s.to_bytes(32, "big")
            + bytes([v])  # ← не signed.v напрямую
        )

    RELAYER_BASE = "https://relayer-v2.polymarket.com"

    def _submit_via_relayer(
        self,
        proxy: str,
        target: str,
        redeem_data: bytes,
        nonce: int,
        neg_risk: bool,
        log_label: str,
    ) -> RedeemResult:
        """Отправляет Safe tx через Polymarket Relayer (gasless)."""
        key = (self._secrets.relayer_api_key or "").strip()
        key_addr = (self._secrets.relayer_api_key_address or "").strip()
        if not key or not key_addr:
            return RedeemResult(
                None,
                False,
                "RELAYER_API_KEY и RELAYER_API_KEY_ADDRESS обязательны для gasless",
            )

        # eoa = self.account.address
        sig = self._build_safe_tx_signature(
            proxy, target, redeem_data, nonce
        )
        sig_hex = "0x" + sig.hex()
        data_hex = "0x" + redeem_data.hex()

        payload = {
            "from": Web3.to_checksum_address(proxy),
            "to": Web3.to_checksum_address(target),
            "proxyWallet": Web3.to_checksum_address(proxy),
            "data": data_hex,
            "nonce": str(nonce),
            "signature": sig_hex,
            "signatureParams": {
                "gasPrice": "0",
                "operation": "0",
                "safeTxnGas": "0",
                "baseGas": "0",
                "gasToken": ZERO_ADDRESS,
                "refundReceiver": ZERO_ADDRESS,
            },
            "type": "SAFE",
        }

        logger.debug("Relayer payload: %s", json.dumps(payload, indent=2))

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.RELAYER_BASE}/submit",
            data=body,
            headers={
                "Content-Type": "application/json",
                "RELAYER_API_KEY": key,
                "RELAYER_API_KEY_ADDRESS": Web3.to_checksum_address(key_addr),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            try:
                err = json.loads(body)
                msg = err.get("error", body) or str(e)
            except Exception:
                msg = body or str(e)
            logger.warning("Relayer submit failed (%s): %s", log_label, msg)
            return RedeemResult(None, False, f"Relayer: {msg}")
        except Exception as e:
            logger.warning("Relayer submit error (%s): %s", log_label, e)
            return RedeemResult(None, False, f"Relayer: {e}")

        tx_id = data.get("transactionID") or data.get("transactionId")
        if not tx_id:
            return RedeemResult(
                None, False, "Relayer: нет transactionID в ответе"
            )

        logger.info(
            "Redeem submitted via Relayer (%s): id=%s",
            log_label,
            tx_id,
        )

        for _ in range(90):
            time.sleep(2)
            status_req = urllib.request.Request(
                f"{self.RELAYER_BASE}/transaction?id={tx_id}",
                headers={
                    "RELAYER_API_KEY": key,
                    "RELAYER_API_KEY_ADDRESS": Web3.to_checksum_address(key_addr),
                },
            )
            try:
                with urllib.request.urlopen(status_req, timeout=15) as r:
                    items = json.loads(r.read().decode())
            except Exception as e:
                logger.warning("Relayer status poll failed: %s", e)
                continue

            tx_list = items if isinstance(items, list) else [items]
            for t in tx_list:
                state = t.get("state", "")
                if state == "STATE_CONFIRMED":
                    tx_hash = t.get("transactionHash") or ""
                    logger.info(
                        "Redeem tx OK via Relayer (%s): %s",
                        log_label,
                        tx_hash or tx_id,
                    )
                    if neg_risk:
                        if tx_hash:
                            try:
                                receipt = self.w3.eth.get_transaction_receipt(
                                    tx_hash
                                )
                                if receipt:
                                    self._log_redeem_settlement(
                                        receipt, proxy, log_label
                                    )
                            except Exception:
                                pass
                        return RedeemResult(tx_hash or "", True, "")
                    if tx_hash:
                        try:
                            receipt = self.w3.eth.get_transaction_receipt(
                                tx_hash
                            )
                            if receipt:
                                return self._standard_redeem_tx_outcome(
                                    receipt, tx_hash, proxy, log_label
                                )
                        except Exception:
                            pass
                    return RedeemResult(tx_hash or "", True, "")
                if state in ("STATE_FAILED", "STATE_INVALID"):
                    err = t.get("error") or t.get("metadata") or state
                    logger.error(
                        "Redeem via Relayer FAILED (%s): %s",
                        log_label,
                        err,
                    )
                    return RedeemResult(
                        None, False, f"Relayer {state}: {err}"
                    )

        return RedeemResult(
            None,
            False,
            "Relayer: таймаут ожидания подтверждения (3 мин)",
        )

    def _send_raw_polygon_tx(
        self, to: str, data: bytes, log_label: str, neg_risk: bool
    ) -> RedeemResult:
        eoa = self.account.address
        tx: dict = {
            "from": eoa,
            "to": Web3.to_checksum_address(to),
            "value": 0,
            "data": data,
            "nonce": self.w3.eth.get_transaction_count(eoa),
            "gas": 500_000,
            "gasPrice": self.w3.eth.gas_price,
            "chainId": POLYGON_CHAIN_ID,
        }
        signed = self.w3.eth.account.sign_transaction(
            tx, self._secrets.private_key
        )
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self.w3.eth.send_raw_transaction(raw)
        hex_hash = tx_hash.hex()
        logger.info("Redeem tx submitted (%s): %s", log_label, hex_hash)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt["status"] != 1:
            logger.error(
                "Redeem tx REVERTED (%s): %s — см. polygonscan.com/tx/0x%s",
                log_label,
                hex_hash,
                hex_hash,
            )
            return RedeemResult(
                hex_hash, False, "transaction reverted (status=0)"
            )
        logger.info(
            "Redeem tx OK (%s): %s gas_used=%s",
            log_label,
            hex_hash,
            receipt.get("gasUsed"),
        )
        if neg_risk:
            self._log_redeem_settlement(receipt, eoa, log_label)
            return RedeemResult(hex_hash, True, "")
        return self._standard_redeem_tx_outcome(receipt, hex_hash, eoa, log_label)

    def _redeem_eoa_sync(
        self,
        condition_id: str,
        neg_risk: bool,
        outcome_token_id: str | None,
    ) -> RedeemResult:
        try:
            holder = self.account.address
            if neg_risk:
                redeem_data, err = self._build_neg_risk_redeem_data(
                    condition_id, holder
                )
                if err:
                    return RedeemResult(None, False, err)
                target = NEG_RISK_ADAPTER
            else:
                skip = self._should_skip_standard_redeem(
                    holder,
                    condition_id,
                    outcome_token_id,
                    holder_is_eoa=True,
                )
                if skip:
                    logger.info(
                        "Skip standard redeem EOA (tx не отправлялась) %s: %s",
                        condition_id[:16],
                        skip[:120],
                    )
                    return RedeemResult(None, False, skip)
                redeem_data = self._build_standard_redeem_data(condition_id)
                target = CTF_EXCHANGE
            return self._send_raw_polygon_tx(
                target, redeem_data, "EOA", neg_risk
            )
        except Exception as e:
            err = str(e)
            logger.error(
                "Redeem failed for %s: %s", condition_id[:16], err
            )
            return RedeemResult(None, False, err[:500])

    def _redeem_safe_sync(
        self,
        condition_id: str,
        neg_risk: bool,
        outcome_token_id: str | None,
    ) -> RedeemResult:
        try:
            proxy = Web3.to_checksum_address(self._secrets.proxy_address)
            code = self.w3.eth.get_code(proxy)
            if not code or len(code) < 2:
                return RedeemResult(
                    None,
                    False,
                    "no contract at POLYMARKET_PROXY_ADDRESS — check Polygonscan; "
                    "for EOA trading set POLYMARKET_SIG_TYPE=0 and redeem without Safe",
                )
            if not neg_risk:
                skip = self._should_skip_standard_redeem(
                    proxy,
                    condition_id,
                    outcome_token_id,
                    holder_is_eoa=False,
                )
                if skip:
                    logger.info(
                        "Skip standard redeem Safe (tx не отправлялась) %s: %s",
                        condition_id[:16],
                        skip[:120],
                    )
                    return RedeemResult(None, False, skip)
            if neg_risk:
                redeem_data, err = self._build_neg_risk_redeem_data(
                    condition_id, proxy
                )
                if err:
                    return RedeemResult(None, False, err)
                target = NEG_RISK_ADAPTER
            else:
                redeem_data = self._build_standard_redeem_data(condition_id)
                target = CTF_EXCHANGE

            safe_contract = self.w3.eth.contract(
                address=proxy,
                abi=SAFE_EXEC_TX_ABI,
            )
            try:
                nonce = safe_contract.functions.nonce().call()
            except Exception as e:
                return RedeemResult(
                    None,
                    False,
                    f"Safe nonce() failed ({e!s}): POLYMARKET_PROXY_ADDRESS must be a "
                    "Gnosis Safe; for EOA use POLYMARKET_SIG_TYPE=0",
                )

            if self._secrets.relayer_api_key and self._secrets.relayer_api_key_address:
                relayer_result = self._submit_via_relayer(
                    proxy, target, redeem_data, nonce, neg_risk, "Safe"
                )
                if relayer_result.success:
                    return relayer_result
                logger.info(
                    "Relayer failed, fallback to raw RPC: %s",
                    relayer_result.error[:80] if relayer_result.error else "",
                )

            signatures = self._build_safe_tx_signature(
                proxy, target, redeem_data, nonce
            )

            eoa = self.account.address
            exec_built = safe_contract.functions.execTransaction(
                Web3.to_checksum_address(target),
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
                "gas": 500_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": POLYGON_CHAIN_ID,
            }

            signed = self.w3.eth.account.sign_transaction(
                tx, self._secrets.private_key
            )
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            tx_hash = self.w3.eth.send_raw_transaction(raw)
            hex_hash = tx_hash.hex()
            logger.info("Redeem tx submitted (Safe→proxy): %s", hex_hash)

            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, timeout=180
            )
            if receipt["status"] != 1:
                logger.error(
                    "Redeem tx REVERTED (Safe): %s — polygonscan.com/tx/0x%s",
                    hex_hash,
                    hex_hash,
                )
                return RedeemResult(
                    hex_hash,
                    False,
                    "transaction reverted (status=0); проверьте tx на Polygonscan",
                )
            logger.info(
                "Redeem tx OK (Safe): %s gas_used=%s — USDC.e на Safe (proxy), не на EOA",
                hex_hash,
                receipt.get("gasUsed"),
            )
            if neg_risk:
                self._log_redeem_settlement(receipt, proxy, "Safe")
                return RedeemResult(hex_hash, True, "")
            return self._standard_redeem_tx_outcome(
                receipt, hex_hash, proxy, "Safe"
            )
        except Exception as e:
            err = str(e)
            logger.error(
                "Redeem failed for %s: %s", condition_id[:16], err
            )
            return RedeemResult(None, False, err[:500])

    def _redeem_sync(
        self,
        condition_id: str,
        neg_risk: bool,
        outcome_token_id: str | None = None,
    ) -> RedeemResult:
        if not self._uses_relayer():
            gas_err = self.check_eoa_pays_gas()
            if gas_err:
                return RedeemResult(None, False, gas_err)
        if self._secrets.signature_type == 0:
            return self._redeem_eoa_sync(
                condition_id, neg_risk, outcome_token_id
            )
        if not self._secrets.proxy_address:
            return RedeemResult(
                None,
                False,
                "POLYMARKET_PROXY_ADDRESS required for Safe (POLYMARKET_SIG_TYPE 1 or 2)",
            )
        return self._redeem_safe_sync(
            condition_id, neg_risk, outcome_token_id
        )

    async def redeem(
        self,
        condition_id: str,
        neg_risk: bool = False,
        outcome_token_id: str | None = None,
    ) -> RedeemResult:
        return await asyncio.to_thread(
            self._redeem_sync, condition_id, neg_risk, outcome_token_id
        )


def _redeem_addr(redeemer: Redeemer) -> str:
    """Адрес для Data API (proxy или EOA)."""
    addr = (redeemer._secrets.proxy_address or "").strip()
    if addr:
        return addr
    if redeemer._secrets.private_key:
        return redeemer.account.address
    return ""


async def redeem_pending_from_data_api(
    db: Database,
    redeemer: Redeemer,
    executor: TradeExecutor,
    max_redeems: int = 50,
) -> RedeemBatchSummary:
    """Redeem по Data API (как check_positions.py): позиции кошелька по адресу."""
    addr = _redeem_addr(redeemer)
    if not addr:
        return RedeemBatchSummary(
            total=0,
            succeeded=0,
            errors=["Нет POLYMARKET_PROXY_ADDRESS и PRIVATE_KEY для Data API"],
        )
    if not redeemer._uses_relayer():
        gas_err = await asyncio.to_thread(redeemer.check_eoa_pays_gas)
        if gas_err:
            return RedeemBatchSummary(total=0, succeeded=0, errors=[gas_err])
    try:
        positions = await fetch_positions_async(addr, size_threshold=0)
    except Exception as e:
        return RedeemBatchSummary(
            total=0,
            succeeded=0,
            errors=[f"Data API positions: {e}"],
        )
    errors: list[str] = []
    succeeded = 0
    attempted = 0

    for p in positions:
        if attempted >= max_redeems:
            break
        condition_id = (p.get("conditionId") or p.get("condition_id") or "").strip()
        asset = (p.get("asset") or p.get("token_id") or "").strip()
        if not condition_id or not asset:
            continue
        if await db.clob_redeem_already_done(condition_id):
            continue
        try:
            market_data = await asyncio.to_thread(
                executor.client.get_market, condition_id
            )
        except Exception as e:
            errors.append(f"Data API {condition_id[:12]}… get_market: {e}")
            continue
        if not market_data:
            continue
        win_tid = winning_token_id_from_market(market_data)
        if not win_tid or win_tid != asset:
            continue

        attempted += 1
        try:
            neg_risk = await asyncio.to_thread(
                executor.client.get_neg_risk, asset
            )
        except Exception:
            neg_risk = False

        result = await redeemer.redeem(
            condition_id,
            neg_risk=neg_risk,
            outcome_token_id=asset,
        )
        await db.mark_clob_redeem_result(
            condition_id,
            result.tx_hash or "",
            result.success,
            result.error if not result.success else None,
        )
        if result.success:
            succeeded += 1
            await db.mark_trades_redeemed_by_condition(
                condition_id, result.tx_hash or ""
            )
        elif result.error:
            errors.append(f"Data API {condition_id[:16]}…: {result.error}")

    return RedeemBatchSummary(
        total=attempted, succeeded=succeeded, errors=errors
    )


async def redeem_unredeemed_won_trades(
    db: Database,
    redeemer: Redeemer,
    executor: TradeExecutor | None,
    max_trades: int = 100,
) -> RedeemBatchSummary:
    if not redeemer._uses_relayer():
        gas_err = await asyncio.to_thread(redeemer.check_eoa_pays_gas)
        if gas_err:
            return RedeemBatchSummary(total=0, succeeded=0, errors=[gas_err])
    won_trades = await db.get_unredeemed_won_trades(max_trades)
    errors: list[str] = []
    succeeded = 0
    attempted = 0

    for trade in won_trades:
        if await db.clob_redeem_already_done(trade.market_id):
            await db.sync_trades_redeem_from_clob_log(trade.market_id)
            continue

        attempted += 1
        neg_risk = False
        if executor:
            neg_risk = await resolve_neg_risk_for_trade(executor, trade)
        else:
            neg_risk = False

        result = await redeemer.redeem(
            trade.market_id,
            neg_risk=neg_risk,
            outcome_token_id=trade.token_id or None,
        )
        await db.mark_redeem_result(
            trade.id,  # type: ignore[arg-type]
            result.tx_hash or "",
            result.success,
            result.error if not result.success else None,
        )
        if result.success:
            succeeded += 1
            await db.mark_clob_redeem_result(
                trade.market_id,
                result.tx_hash or "",
                True,
                None,
            )
        elif result.error:
            short_q = (trade.question[:40] + "…") if len(trade.question) > 40 else trade.question
            errors.append(f"#{trade.id} {short_q}: {result.error}")

    return RedeemBatchSummary(
        total=attempted, succeeded=succeeded, errors=errors
    )


async def redeem_all_pending(
    db: Database,
    redeemer: Redeemer,
    executor: TradeExecutor | None,
    max_trades: int = 100,
) -> RedeemBatchSummary:
    """Сначала очередь по Data API (позиции кошелька), затем WON из БД."""
    all_errors: list[str] = []
    total_ok = 0
    total_n = 0

    if executor and _redeem_addr(redeemer):
        s_data = await redeem_pending_from_data_api(
            db, redeemer, executor, max_redeems=max_trades
        )
        total_n += s_data.total
        total_ok += s_data.succeeded
        all_errors.extend(s_data.errors)

    s_db = await redeem_unredeemed_won_trades(
        db, redeemer, executor, max_trades=max_trades
    )
    total_n += s_db.total
    total_ok += s_db.succeeded
    all_errors.extend(s_db.errors)

    return RedeemBatchSummary(
        total=total_n,
        succeeded=total_ok,
        errors=list(dict.fromkeys(all_errors)),
    )


async def resolve_neg_risk_for_trade(
    executor: TradeExecutor, trade: Trade
) -> bool:
    if trade.token_id:
        try:
            return await asyncio.to_thread(
                executor.client.get_neg_risk, trade.token_id
            )
        except Exception:
            return False
    try:
        market_data = await asyncio.to_thread(
            executor.client.get_market, trade.market_id
        )
    except Exception:
        return False
    if not market_data:
        return False
    for tok in market_data.get("tokens") or []:
        tid = tok.get("token_id")
        if tid:
            try:
                return await asyncio.to_thread(
                    executor.client.get_neg_risk, tid
                )
            except Exception:
                return False
    return False
