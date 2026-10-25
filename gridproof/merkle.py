# @exports: leaf_hash(asset_id: bytes, day: int, sp: int, kwh: int, g: int) -> bytes   # keccak256(keccak256(abi.encode(bytes32,uint32,uint8,uint64,uint32)))
# @exports: merkle_root(leaves: list[bytes]) -> bytes
# @exports: merkle_proof(leaves: list[bytes], index: int) -> list[bytes]
# @exports: verify_proof(leaf: bytes, proof: list[bytes], root: bytes) -> bool
# @exports: to_hex(b: bytes) -> str   # "0x" + lowercase hex
# @exports: from_hex(s: str) -> bytes   # accepts with or without "0x"
# @imports: none
# @env: none
# @schema: leaf ABI types (bytes32 assetId, uint32 day, uint8 sp, uint64 kwh, uint32 g), identical to GridProofRegistry.leafOf and web/verify.html
# @schema: tree rule: sorted-pair hashing keccak256(min(a,b) || max(a,b)); an odd node is promoted unchanged (never duplicated); leaf order is the caller's order (pipeline sorts by sp)
from __future__ import annotations

from eth_abi import encode
from web3 import Web3

_LEAF_TYPES = ["bytes32", "uint32", "uint8", "uint64", "uint32"]


def _keccak(data: bytes) -> bytes:
    return bytes(Web3.keccak(data))


def to_hex(b: bytes) -> str:
    return "0x" + bytes(b).hex()


def from_hex(s: str) -> bytes:
    text = s[2:] if s.startswith(("0x", "0X")) else s
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise ValueError(f"not a hex string: {s!r}") from exc


def leaf_hash(asset_id: bytes, day: int, sp: int, kwh: int, g: int) -> bytes:
    if len(asset_id) != 32:
        raise ValueError(f"asset_id must be 32 bytes, got {len(asset_id)}")
    for name, value, bits in (("day", day, 32), ("sp", sp, 8), ("kwh", kwh, 64), ("g", g, 32)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0 or value >= 2**bits:
            raise ValueError(f"{name}={value!r} does not fit uint{bits}: leaf fields must be non-negative integers")
    inner = _keccak(encode(_LEAF_TYPES, [bytes(asset_id), day, sp, kwh, g]))
    return _keccak(inner)


def _pair(a: bytes, b: bytes) -> bytes:
    lo, hi = (a, b) if a < b else (b, a)
    return _keccak(lo + hi)


def _check_leaves(leaves: list[bytes]) -> None:
    if not leaves:
        raise ValueError("a Merkle tree needs at least one leaf")
    for i, leaf in enumerate(leaves):
        if len(leaf) != 32:
            raise ValueError(f"leaf {i} must be 32 bytes, got {len(leaf)}")


def _next_level(level: list[bytes]) -> list[bytes]:
    nxt: list[bytes] = []
    for i in range(0, len(level), 2):
        nxt.append(_pair(level[i], level[i + 1]) if i + 1 < len(level) else level[i])
    return nxt


def merkle_root(leaves: list[bytes]) -> bytes:
    _check_leaves(leaves)
    level = [bytes(x) for x in leaves]
    while len(level) > 1:
        level = _next_level(level)
    return level[0]


def merkle_proof(leaves: list[bytes], index: int) -> list[bytes]:
    _check_leaves(leaves)
    if index < 0 or index >= len(leaves):
        raise IndexError(f"leaf index {index} out of range for {len(leaves)} leaves")
    level = [bytes(x) for x in leaves]
    idx = index
    proof: list[bytes] = []
    while len(level) > 1:
        sibling = idx ^ 1
        if sibling < len(level):
            proof.append(level[sibling])
        level = _next_level(level)
        idx >>= 1
    return proof


def verify_proof(leaf: bytes, proof: list[bytes], root: bytes) -> bool:
    node = bytes(leaf)
    for p in proof:
        node = _pair(node, bytes(p))
    return node == bytes(root)
