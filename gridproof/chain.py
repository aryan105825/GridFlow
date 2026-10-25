# @exports: ChainError(RuntimeError) ; ChainRevert(ChainError) with .reason: str
# @exports: beneficiary_ref(name: str) -> bytes   # keccak256(utf8(name)), the bytes32 passed to retire()
# @exports: Chain.from_settings(s: Settings) -> Chain   # read-only when no signer is available (blank ISSUER_PRIVATE_KEY on a node with no unlocked accounts)
# @exports: Chain.register_asset(asset: Asset) -> {"tx": str, "block": int, "gas_used": int}
# @exports: Chain.get_asset(asset_id: bytes) -> {"capacity_kw": int, "exists": bool}
# @exports: Chain.issue_batch(b: DayBatch, model_hash: bytes, holder: str | None = None) -> {"tx","block","gas_used"}   # holder defaults to the sender
# @exports: Chain.get_batch(asset_id: bytes, day: int) -> {"root": bytes, "model_hash": bytes, "snapshot_hash": bytes, "total_kwh": int, "intervals": int, "issued_at": int, "holder": str} | None   # None when not issued
# @exports: Chain.retire(asset_id: bytes, day: int, sp: int, kwh: int, g: int, proof: list[bytes], beneficiary_ref: bytes) -> {"tx","block","gas_used"}   # sent by the sender, who must be the batch holder
# @exports: Chain.verify_leaf(asset_id: bytes, day: int, sp: int, kwh: int, g: int, proof: list[bytes]) -> bool
# @exports: Chain.leaf_of(asset_id: bytes, day: int, sp: int, kwh: int, g: int) -> bytes
# @exports: Chain.retired_grams(beneficiary_ref: bytes) -> int ; Chain.retired_by(leaf: bytes) -> str
# @exports: Chain.expect_revert(fn: Callable, *args) -> str   # calls fn(*args); returns the revert reason string (e.g. "BATCH_EXISTS"); raises ChainError if the call succeeds
# @exports: Chain.address: str ; Chain.sender: str | None ; Chain.w3: Web3 ; Chain.chain_id: int
# @imports: gridproof/schemas.py:Asset, DayBatch, Settings
# @env: none (all settings arrive through Settings)
# @schema: reads deployments file {"network","chainId","address","issuer","txHash","blockNumber"} and ABI from artifacts/contracts/GridProofRegistry.sol/GridProofRegistry.json
# @schema: signing: ISSUER_PRIVATE_KEY set -> build, sign locally and send raw (accepts web3 6.20.3 .rawTransaction or newer .raw_transaction); blank -> node-unlocked accounts[0] via eth_sendTransaction
# @schema: all contract reverts surface as ChainRevert(reason); every other RPC failure surfaces as ChainError with a how-to-fix message
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from web3 import Web3
from web3.exceptions import ContractLogicError

from gridproof.schemas import Asset, DayBatch, Settings

ABI_PATH = Path("artifacts/contracts/GridProofRegistry.sol/GridProofRegistry.json")

KNOWN_REASONS = (
    "ASSET_EXISTS",
    "UNKNOWN_ASSET",
    "BATCH_EXISTS",
    "EMPTY_BATCH",
    "EXCEEDS_DAILY_CAPACITY",
    "BATCH_MISSING",
    "NOT_HOLDER",
    "EXCEEDS_INTERVAL_CAPACITY",
    "BAD_PROOF",
    "ALREADY_RETIRED",
    "OwnableUnauthorizedAccount",
)

_DEPLOYMENT_FIELDS = ("network", "chainId", "address", "issuer", "txHash", "blockNumber")


class ChainError(RuntimeError):
    """Any chain failure that is not a contract revert. The message says what failed and how to fix it."""


class ChainRevert(ChainError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"contract reverted: {reason}")
        self.reason = reason


def beneficiary_ref(name: str) -> bytes:
    if not name:
        raise ChainError("beneficiary name must be non-empty")
    return bytes(Web3.keccak(text=name))


def _hex(b: Any) -> str:
    return "0x" + bytes(b).hex()


def _b32(value: bytes, name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != 32:
        raise ChainError(f"{name} must be exactly 32 bytes, got {value!r}")
    return bytes(value)


def _revert_reason(exc: BaseException) -> Optional[str]:
    text = " ".join([str(exc)] + [repr(a) for a in getattr(exc, "args", ())])
    for reason in KNOWN_REASONS:
        if reason in text:
            return reason
    for pattern in (r"reason string '([^']+)'", r"execution reverted: ([A-Za-z0-9_]+)"):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    return None


class Chain:
    def __init__(self, w3: Web3, contract: Any, sender: Optional[str], key: Optional[str], chain_id: int) -> None:
        self.w3 = w3
        self.contract = contract
        self.sender = sender
        self._key = key
        self.chain_id = chain_id
        self._owner: Optional[str] = None

    @property
    def address(self) -> str:
        return self.contract.address

    @classmethod
    def from_settings(cls, s: Settings) -> "Chain":
        w3 = Web3(Web3.HTTPProvider(s.rpc_url, request_kwargs={"timeout": 60}))
        if not w3.is_connected():
            raise ChainError(
                f"cannot reach the RPC node at {s.rpc_url}. Start a local node with `npm run node`, "
                "or fix RPC_URL / TESTNET_RPC_URL in .env."
            )
        node_chain = w3.eth.chain_id
        if node_chain != s.chain_id:
            raise ChainError(
                f"node at {s.rpc_url} reports chain id {node_chain} but settings expect {s.chain_id}. "
                "Fix CHAIN_ID or RPC_URL in .env."
            )

        dep_path = Path(s.deployment_file)
        if not dep_path.is_file():
            raise ChainError(
                f"deployment file {dep_path} not found. Deploy first: `npm run deploy:local` (node must be running) "
                "or `npm run deploy:testnet`."
            )
        try:
            dep = json.loads(dep_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ChainError(f"{dep_path} is not valid JSON ({exc}). Redeploy to regenerate it.") from exc
        for field in _DEPLOYMENT_FIELDS:
            if field not in dep:
                raise ChainError(f"{dep_path} is missing field '{field}'. Redeploy to regenerate it.")
        if int(dep["chainId"]) != s.chain_id:
            raise ChainError(
                f"{dep_path} was written for chain {dep['chainId']} but settings expect {s.chain_id}. "
                "Point DEPLOYMENT_FILE at the matching deployment."
            )
        address = Web3.to_checksum_address(dep["address"])
        if not w3.eth.get_code(address):
            raise ChainError(
                f"no contract code at {address} on {s.rpc_url}. A restarted local node loses its state: "
                "rerun `npm run deploy:local`."
            )

        if not ABI_PATH.is_file():
            raise ChainError(f"ABI file {ABI_PATH} not found. Run `npm run compile` from the repo root.")
        abi = json.loads(ABI_PATH.read_text(encoding="utf-8"))["abi"]
        contract = w3.eth.contract(address=address, abi=abi)

        key: Optional[str] = None
        sender: Optional[str] = None
        if s.issuer_key:
            key = s.issuer_key
            sender = w3.eth.account.from_key(key).address
        else:
            try:
                accounts = w3.eth.accounts
            except Exception:  # noqa: BLE001 - public RPCs reject eth_accounts; that just means "read-only", writes will fail loudly
                accounts = []
            sender = Web3.to_checksum_address(accounts[0]) if accounts else None
        return cls(w3, contract, sender, key, s.chain_id)

    # ------------------------------------------------------------------ plumbing

    def _translate(self, exc: Exception, what: str) -> ChainError:
        reason = _revert_reason(exc)
        if reason is not None:
            return ChainRevert(reason)
        return ChainError(
            f"{what} failed: {type(exc).__name__}: {exc}. Check the RPC node is running and that the contract at "
            f"{self.address} matches the compiled ABI (`npm run compile`, then redeploy)."
        )

    def _call(self, fn: Any, what: str = "contract call") -> Any:
        try:
            return fn.call()
        except ChainError:
            raise
        except (ContractLogicError, ValueError) as exc:
            raise self._translate(exc, what) from exc
        except Exception as exc:  # noqa: BLE001 - re-raised with context
            raise self._translate(exc, what) from exc

    def _send(self, fn: Any, what: str) -> dict:
        if self.sender is None:
            raise ChainError(
                f"{what}: no signer available. Set ISSUER_PRIVATE_KEY in .env, or use a node with unlocked accounts "
                "(npm run node)."
            )
        try:
            if self._key:
                tx = fn.build_transaction(
                    {
                        "from": self.sender,
                        "nonce": self.w3.eth.get_transaction_count(self.sender, "pending"),
                        "chainId": self.chain_id,
                    }
                )
                signed = self.w3.eth.account.sign_transaction(tx, self._key)
                raw = getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction", None)
                if raw is None:
                    raise ChainError("signed transaction has neither rawTransaction nor raw_transaction; check the pinned web3/eth-account versions")
                tx_hash = self.w3.eth.send_raw_transaction(raw)
            else:
                tx_hash = fn.transact({"from": self.sender})
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        except ChainError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised with context
            raise self._translate(exc, what) from exc
        if receipt["status"] != 1:
            raise ChainError(f"{what}: transaction {_hex(tx_hash)} was mined but failed (status 0)")
        return {"tx": _hex(tx_hash), "block": int(receipt["blockNumber"]), "gas_used": int(receipt["gasUsed"])}

    def _require_owner(self, what: str) -> None:
        if self.sender is None:
            raise ChainError(f"{what}: no signer available. Set ISSUER_PRIVATE_KEY in .env or use an unlocked local node.")
        if self._owner is None:
            self._owner = Web3.to_checksum_address(self._call(self.contract.functions.owner(), "owner()"))
        if self.sender != self._owner:
            raise ChainError(
                f"{what}: sender {self.sender} is not the contract owner {self._owner}. "
                "Use the deployer key as ISSUER_PRIVATE_KEY (or deploy from this account)."
            )

    # ------------------------------------------------------------------ writes

    def register_asset(self, asset: Asset) -> dict:
        self._require_owner("registerAsset")
        fn = self.contract.functions.registerAsset(asset.asset_id, asset.bm_unit, asset.capacity_kw)
        return self._send(fn, "registerAsset")

    def issue_batch(self, b: DayBatch, model_hash: bytes, holder: Optional[str] = None) -> dict:
        self._require_owner("issueBatch")
        holder_addr = Web3.to_checksum_address(holder) if holder else self.sender
        fn = self.contract.functions.issueBatch(
            _b32(b.asset_id, "asset_id"),
            b.day,
            _b32(b.root, "root"),
            _b32(model_hash, "model_hash"),
            _b32(b.snapshot_hash, "snapshot_hash"),
            b.total_kwh,
            len(b.sps),
            holder_addr,
        )
        return self._send(fn, "issueBatch")

    def retire(
        self,
        asset_id: bytes,
        day: int,
        sp: int,
        kwh: int,
        g: int,
        proof: list[bytes],
        beneficiary_ref: bytes,
    ) -> dict:
        fn = self.contract.functions.retire(
            _b32(asset_id, "asset_id"),
            day,
            sp,
            kwh,
            g,
            [_b32(p, "proof element") for p in proof],
            _b32(beneficiary_ref, "beneficiary_ref"),
        )
        return self._send(fn, "retire")

    # ------------------------------------------------------------------ reads

    def get_asset(self, asset_id: bytes) -> dict:
        capacity_kw, exists = self._call(self.contract.functions.assets(_b32(asset_id, "asset_id")), "assets()")
        return {"capacity_kw": int(capacity_kw), "exists": bool(exists)}

    def get_batch(self, asset_id: bytes, day: int) -> Optional[dict]:
        root, model_hash, snapshot_hash, total_kwh, intervals, issued_at, holder = self._call(
            self.contract.functions.batches(_b32(asset_id, "asset_id"), day), "batches()"
        )
        if int(issued_at) == 0:
            return None
        return {
            "root": bytes(root),
            "model_hash": bytes(model_hash),
            "snapshot_hash": bytes(snapshot_hash),
            "total_kwh": int(total_kwh),
            "intervals": int(intervals),
            "issued_at": int(issued_at),
            "holder": Web3.to_checksum_address(holder),
        }

    def verify_leaf(self, asset_id: bytes, day: int, sp: int, kwh: int, g: int, proof: list[bytes]) -> bool:
        fn = self.contract.functions.verifyLeaf(
            _b32(asset_id, "asset_id"), day, sp, kwh, g, [_b32(p, "proof element") for p in proof]
        )
        return bool(self._call(fn, "verifyLeaf()"))

    def leaf_of(self, asset_id: bytes, day: int, sp: int, kwh: int, g: int) -> bytes:
        fn = self.contract.functions.leafOf(_b32(asset_id, "asset_id"), day, sp, kwh, g)
        return bytes(self._call(fn, "leafOf()"))

    def retired_grams(self, beneficiary_ref: bytes) -> int:
        return int(self._call(self.contract.functions.gramsBy(_b32(beneficiary_ref, "beneficiary_ref")), "gramsBy()"))

    def retired_by(self, leaf: bytes) -> str:
        return Web3.to_checksum_address(self._call(self.contract.functions.retiredBy(_b32(leaf, "leaf")), "retiredBy()"))

    # ------------------------------------------------------------------ testing helper

    def expect_revert(self, fn: Callable[..., Any], *args: Any) -> str:
        try:
            fn(*args)
        except ChainRevert as exc:
            return exc.reason
        raise ChainError(
            "expect_revert: the call succeeded but a revert was expected. State on chain may have changed; "
            "this means a guard in GridProofRegistry did not fire."
        )
