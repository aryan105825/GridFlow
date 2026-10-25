# @exports: ConfigError(RuntimeError)
# @exports: SEPOLIA_CHAIN_ID: int
# @exports: Reason = Literal["OK","NEG_OR_NAN","MISSING_DATA","OVER_CAPACITY","ABOVE_ENVELOPE","FROZEN_METER"]
# @exports: REASONS: tuple[str, ...]  (same order as Reason)
# @exports: Asset(key: str, bm_unit: str, lat: float, lon: float, capacity_kw: int, source_url: str, offshore: bool = True); Asset.asset_id -> bytes (keccak256(utf8(bm_unit)))
# @exports: Interval(asset_key: str, day: int, sp: int, start_utc: str, kwh: int, ws100: float | None, g_per_kwh: int | None)
# @exports: Verdict(interval: Interval, accepted: bool, reason: Reason, cf: float, upper_cf: float | None)
# @exports: DayBatch(asset_key: str, asset_id: bytes, day: int, sps: list[int], kwh: list[int], g: list[int], leaves: list[bytes], root: bytes, total_kwh: int, snapshot_hash: bytes)
# @exports: Settings(rpc_url: str, chain_id: int, deployment_file: str, issuer_key: str | None, data_dir: str, results_dir: str, assets_file: str, train: tuple[date, date], issue: tuple[date, date])
# @exports: load_settings(env: Mapping[str, str] = os.environ, network: str = "localhost") -> Settings   # network: "localhost" (uses RPC_URL/CHAIN_ID/DEPLOYMENT_FILE) | "testnet" (TESTNET_RPC_URL, chain 11155111, deployments/sepolia.json, ISSUER_PRIVATE_KEY required)
# @exports: load_assets(path: str | Path) -> list[Asset]
# @exports: day_from_date(d: date) -> int   # 2025-03-30 -> 20250330
# @exports: date_from_day(day: int) -> date
# @imports: none
# @env: RPC_URL
# @env: CHAIN_ID
# @env: DEPLOYMENT_FILE
# @env: ISSUER_PRIVATE_KEY
# @env: TESTNET_RPC_URL
# @env: ASSETS_FILE
# @env: DATA_DIR
# @env: RESULTS_DIR
# @env: TRAIN_START
# @env: TRAIN_END
# @env: ISSUE_START
# @env: ISSUE_END
# @schema: assets.yaml -> {"assets": [{"key","bm_unit","lat","lon","capacity_kw":int>0,"source_url","offshore":bool=true}]}
# @schema: precedence for settings: process env > ./.env (only when env is os.environ) > built-in defaults identical to .env.example
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Mapping, Optional, get_args

import yaml
from web3 import Web3

SEPOLIA_CHAIN_ID = 11155111

Reason = Literal["OK", "NEG_OR_NAN", "MISSING_DATA", "OVER_CAPACITY", "ABOVE_ENVELOPE", "FROZEN_METER"]
REASONS: tuple[str, ...] = get_args(Reason)

DEFAULTS: dict[str, str] = {
    "RPC_URL": "http://127.0.0.1:8545",
    "CHAIN_ID": "31337",
    "DEPLOYMENT_FILE": "deployments/localhost.json",
    "ISSUER_PRIVATE_KEY": "",
    "TESTNET_RPC_URL": "",
    "ASSETS_FILE": "assets.yaml",
    "DATA_DIR": "data",
    "RESULTS_DIR": "results",
    "TRAIN_START": "2024-04-01",
    "TRAIN_END": "2025-02-28",
    "ISSUE_START": "2025-03-01",
    "ISSUE_END": "2025-03-31",
}

_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class ConfigError(RuntimeError):
    """Raised at startup when settings or assets.yaml are missing or invalid. The message says how to fix it."""


@dataclass(frozen=True)
class Asset:
    key: str
    bm_unit: str
    lat: float
    lon: float
    capacity_kw: int
    source_url: str
    offshore: bool = True

    @property
    def asset_id(self) -> bytes:
        return bytes(Web3.keccak(text=self.bm_unit))


@dataclass(frozen=True)
class Interval:
    asset_key: str
    day: int
    sp: int
    start_utc: str
    kwh: int
    ws100: Optional[float]
    g_per_kwh: Optional[int]


@dataclass(frozen=True)
class Verdict:
    interval: Interval
    accepted: bool
    reason: Reason
    cf: float
    upper_cf: Optional[float]


@dataclass(frozen=True)
class DayBatch:
    asset_key: str
    asset_id: bytes
    day: int
    sps: list[int]
    kwh: list[int]
    g: list[int]
    leaves: list[bytes]
    root: bytes
    total_kwh: int
    snapshot_hash: bytes


@dataclass(frozen=True)
class Settings:
    rpc_url: str
    chain_id: int
    deployment_file: str
    issuer_key: Optional[str]
    data_dir: str
    results_dir: str
    assets_file: str
    train: tuple[date, date]
    issue: tuple[date, date]


def day_from_date(d: date) -> int:
    return d.year * 10000 + d.month * 100 + d.day


def date_from_day(day: int) -> date:
    year, rest = divmod(int(day), 10000)
    month, dom = divmod(rest, 100)
    try:
        return date(year, month, dom)
    except ValueError as exc:
        raise ValueError(f"day={day} is not a valid YYYYMMDD date") from exc


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{n}: expected KEY=VALUE, got {raw!r}. Fix .env (see .env.example).")
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _need(values: Mapping[str, str], name: str) -> str:
    v = values.get(name, "").strip()
    if not v:
        raise ConfigError(f"{name} is empty. Set it in .env (see .env.example).")
    return v


def _parse_date(name: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name}={value!r} is not a YYYY-MM-DD date. Fix it in .env.") from exc


def load_settings(env: Mapping[str, str] = os.environ, network: str = "localhost") -> Settings:
    values: dict[str, str] = dict(DEFAULTS)
    if env is os.environ:
        values.update({k: v for k, v in _read_dotenv(Path(".env")).items() if k in DEFAULTS})
    values.update({k: v for k, v in env.items() if k in DEFAULTS and v != ""})

    if network == "localhost":
        rpc_url = _need(values, "RPC_URL")
        raw_chain = _need(values, "CHAIN_ID")
        try:
            chain_id = int(raw_chain)
        except ValueError as exc:
            raise ConfigError(f"CHAIN_ID={raw_chain!r} is not an integer. Fix it in .env.") from exc
        deployment_file = _need(values, "DEPLOYMENT_FILE")
    elif network == "testnet":
        rpc_url = _need(values, "TESTNET_RPC_URL")
        chain_id = SEPOLIA_CHAIN_ID
        deployment_file = "deployments/sepolia.json"
        if not values.get("ISSUER_PRIVATE_KEY", "").strip():
            raise ConfigError("ISSUER_PRIVATE_KEY is required for --network testnet. Set it in .env (0x + 64 hex).")
    else:
        raise ConfigError(f"unknown network {network!r}; expected 'localhost' or 'testnet'.")

    if not re.match(r"^https?://", rpc_url):
        raise ConfigError(f"RPC URL {rpc_url!r} must start with http:// or https://. Fix it in .env.")

    key = values.get("ISSUER_PRIVATE_KEY", "").strip()
    if key and not _KEY_RE.match(key):
        raise ConfigError("ISSUER_PRIVATE_KEY must be 0x followed by 64 hex characters (or blank for an unlocked node account).")

    train = (_parse_date("TRAIN_START", _need(values, "TRAIN_START")), _parse_date("TRAIN_END", _need(values, "TRAIN_END")))
    issue = (_parse_date("ISSUE_START", _need(values, "ISSUE_START")), _parse_date("ISSUE_END", _need(values, "ISSUE_END")))
    if train[0] > train[1]:
        raise ConfigError(f"TRAIN_START {train[0]} is after TRAIN_END {train[1]}.")
    if issue[0] > issue[1]:
        raise ConfigError(f"ISSUE_START {issue[0]} is after ISSUE_END {issue[1]}.")
    if train[1] >= issue[0]:
        raise ConfigError(
            f"TRAIN_END {train[1]} must be before ISSUE_START {issue[0]}: the model must never see the month it judges."
        )

    return Settings(
        rpc_url=rpc_url,
        chain_id=chain_id,
        deployment_file=deployment_file,
        issuer_key=key or None,
        data_dir=_need(values, "DATA_DIR"),
        results_dir=_need(values, "RESULTS_DIR"),
        assets_file=_need(values, "ASSETS_FILE"),
        train=train,
        issue=issue,
    )


_ASSET_REQUIRED = ("key", "bm_unit", "lat", "lon", "capacity_kw", "source_url")


def _is_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def load_assets(path: str | Path) -> list[Asset]:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"assets file {p} not found. Set ASSETS_FILE in .env or create it (see assets.yaml).")
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p} is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("assets"), list) or not doc["assets"]:
        raise ConfigError(f"{p} must contain a non-empty top-level 'assets:' list.")

    assets: list[Asset] = []
    seen_keys: set[str] = set()
    seen_units: set[str] = set()
    for i, row in enumerate(doc["assets"]):
        if not isinstance(row, dict):
            raise ConfigError(f"{p}: assets[{i}] must be a mapping.")
        for field in _ASSET_REQUIRED:
            if field not in row:
                raise ConfigError(f"{p}: assets[{i}] is missing required field '{field}'.")
        key, bm_unit, source_url = row["key"], row["bm_unit"], row["source_url"]
        for name, val in (("key", key), ("bm_unit", bm_unit), ("source_url", source_url)):
            if not isinstance(val, str) or not val.strip():
                raise ConfigError(f"{p}: assets[{i}].{name} must be a non-empty string.")
        if not (_is_number(row["lat"]) and -90 <= row["lat"] <= 90):
            raise ConfigError(f"{p}: assets[{i}].lat must be a number in [-90, 90].")
        if not (_is_number(row["lon"]) and -180 <= row["lon"] <= 180):
            raise ConfigError(f"{p}: assets[{i}].lon must be a number in [-180, 180].")
        cap = row["capacity_kw"]
        if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
            raise ConfigError(f"{p}: assets[{i}].capacity_kw must be a positive integer (kW).")
        offshore = row.get("offshore", True)
        if not isinstance(offshore, bool):
            raise ConfigError(f"{p}: assets[{i}].offshore must be true or false.")
        if key in seen_keys:
            raise ConfigError(f"{p}: duplicate asset key {key!r}.")
        if bm_unit in seen_units:
            raise ConfigError(f"{p}: duplicate bm_unit {bm_unit!r}.")
        seen_keys.add(key)
        seen_units.add(bm_unit)
        assets.append(
            Asset(
                key=key,
                bm_unit=bm_unit,
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                capacity_kw=cap,
                source_url=source_url,
                offshore=offshore,
            )
        )
    return assets
