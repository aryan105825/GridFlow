# @exports: PipelineError(RuntimeError)
# @exports: DEFAULT_CAPACITY_TOL: float = 0.02 ; DEFAULT_FROZEN_RUN: int = 6
# @exports: snapshot_path(s, key) -> Path ; model_path(s, key) -> Path ; batch_path(s, key, day: int) -> Path ; receipts_path(s) -> Path
# @exports: ensure_model(s, asset) -> (doc: dict, raw: bytes)   # reads data/models/{key}.json; if absent writes a rules-only model
# @exports: judge_config(s, asset) -> (upper_fn: Callable[[float], float] | None, capacity_tol: float, frozen_run: int)
# @exports: run_fetch(s, use_cache: bool = True) -> dict[key, {"rows": int, "days": int, "written": bool, "path": str}]
# @exports: run_train(s) -> dict[key, {"n_train": int, "model_sha256": str, "path": str}]   # needs gridproof/envelope.py (milestone M2)
# @exports: run_judge(s) -> dict[key, list[Verdict]]   # issue window only
# @exports: make_batches(asset: Asset, verdicts: list[Verdict]) -> list[DayBatch]   # accepted only, leaves sorted by sp, sorted by day
# @exports: run_issue(s, chain: Chain, max_days: int | None = None) -> list[{"kind","asset_key","day","tx","block","gas_used"}]   # max_days applies per asset; also appends to data/receipts.json
# @exports: run_retire(s, chain: Chain, asset_key: str, day: int, sp: int, beneficiary: str) -> {"kind","asset_key","day","tx","block","gas_used","sp","kwh","g_per_kwh","grams","beneficiary","beneficiary_ref"}
# @imports: gridproof/sources.py:set_cache_dir(path), source_descriptors(asset, start, end), weather_model()
# @imports: gridproof/dataset.py:build_intervals(asset, start, end, use_cache), canonical_json(obj), sha256(b), day_snapshot_hash(day_intervals), atomic_write(path, data), write_snapshot(path, asset, intervals, meta), read_snapshot(path)
# @imports: gridproof/merkle.py:leaf_hash(asset_id, day, sp, kwh, g), merkle_root(leaves), merkle_proof(leaves, index), verify_proof(leaf, proof, root), to_hex(b), from_hex(s)
# @imports: gridproof/oracle.py:judge(intervals, capacity_kw, upper_fn, capacity_tol, frozen_run)
# @imports: gridproof/chain.py:Chain.get_asset(asset_id), Chain.register_asset(asset), Chain.get_batch(asset_id, day), Chain.issue_batch(b, model_hash, holder), Chain.retire(asset_id, day, sp, kwh, g, proof, beneficiary_ref), beneficiary_ref(name)
# @imports: gridproof/schemas.py:Asset, DayBatch, Interval, Settings, Verdict, load_assets(path), day_from_date(d)
# @imports: gridproof/envelope.py (lazy, only when a model file contains "ws_edges"): fit_envelope(train: list[Interval], capacity_kw: int) -> Envelope, to_json(env) -> dict, from_json(d: dict) -> Envelope, upper_cf(env, ws: float) -> float
# @env: none (all settings arrive through Settings)
# @schema: data/models/{key}.json = canonical JSON of {"asset_key", "rules": {"capacity_tol", "frozen_run"}} (rules-only, milestone M1) or envelope.to_json(env) plus "rules" (after `train`). The on-chain modelHash is sha256 of these exact file bytes (claim C12)
# @schema: data/batches/{key}_{day}.json = {"asset_id","day","root","model_hash","snapshot_hash","total_kwh","intervals","sps","kwh","g","leaves"} (hex strings with 0x; canonical JSON)
# @schema: data/receipts.json = [{"kind": "register"|"issue"|"retire", "asset_key", "day" (0 for register), "tx", "block", "gas_used"}]
# @schema: snapshots hold train + issue window in one file (train start .. issue end); the issue window is the only part judged and certified

from __future__ import annotations

import importlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from gridproof import sources
from gridproof.chain import Chain, beneficiary_ref
from gridproof.dataset import (atomic_write, build_intervals, canonical_json, day_snapshot_hash,
                               read_snapshot, sha256, write_snapshot)
from gridproof.merkle import from_hex, leaf_hash, merkle_proof, merkle_root, to_hex, verify_proof
from gridproof.oracle import judge
from gridproof.schemas import Asset, DayBatch, Interval, Settings, Verdict, day_from_date, load_assets

DEFAULT_CAPACITY_TOL = 0.02
DEFAULT_FROZEN_RUN = 6


class PipelineError(RuntimeError):
    pass


def snapshot_path(s: Settings, key: str) -> Path:
    return Path(s.data_dir) / "snapshots" / f"{key}.json"


def model_path(s: Settings, key: str) -> Path:
    return Path(s.data_dir) / "models" / f"{key}.json"


def batch_path(s: Settings, key: str, day: int) -> Path:
    return Path(s.data_dir) / "batches" / f"{key}_{day}.json"


def receipts_path(s: Settings) -> Path:
    return Path(s.data_dir) / "receipts.json"


def _assets(s: Settings) -> list[Asset]:
    return load_assets(s.assets_file)


def _asset(s: Settings, key: str) -> Asset:
    for a in _assets(s):
        if a.key == key:
            return a
    raise PipelineError(f"Unknown asset key {key!r}. Keys in {s.assets_file}: {[a.key for a in _assets(s)]}.")


def _read_snapshot_for(s: Settings, asset: Asset) -> list[Interval]:
    path = snapshot_path(s, asset.key)
    if not path.exists():
        raise PipelineError(f"No snapshot at {path}. Run `python -m gridproof.cli fetch` first.")
    return read_snapshot(path)


def _issue_days(s: Settings) -> tuple[int, int]:
    return day_from_date(s.issue[0]), day_from_date(s.issue[1])


def _envelope_module():
    try:
        return importlib.import_module("gridproof.envelope")
    except ModuleNotFoundError as exc:
        if exc.name == "gridproof.envelope":
            raise PipelineError("gridproof/envelope.py is not in the repo yet (it is written in milestone M2), "
                                "but this step needs it. Add the file, or delete data/models/*.json to run "
                                "with the rules-only oracle.") from exc
        raise


def ensure_model(s: Settings, asset: Asset) -> tuple[dict, bytes]:
    path = model_path(s, asset.key)
    if not path.exists():
        doc = {"asset_key": asset.key,
               "rules": {"capacity_tol": DEFAULT_CAPACITY_TOL, "frozen_run": DEFAULT_FROZEN_RUN}}
        atomic_write(path, canonical_json(doc))
    raw = path.read_bytes()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{path} is not valid JSON ({exc}). Delete it and re-run.") from exc
    rules = doc.get("rules") if isinstance(doc, dict) else None
    if not isinstance(rules, dict) or "capacity_tol" not in rules or "frozen_run" not in rules:
        raise PipelineError(f"{path} must contain rules.capacity_tol and rules.frozen_run. "
                            "Delete it and re-run `train` or `judge`.")
    return doc, raw


def judge_config(s: Settings, asset: Asset) -> tuple[Callable[[float], float] | None, float, int]:
    doc, _ = ensure_model(s, asset)
    rules = doc["rules"]
    upper_fn = None
    if "ws_edges" in doc:
        mod = _envelope_module()
        env = mod.from_json({k: v for k, v in doc.items() if k != "rules"})
        upper_fn = lambda ws, _env=env, _mod=mod: _mod.upper_cf(_env, ws)  # noqa: E731
    return upper_fn, float(rules["capacity_tol"]), int(rules["frozen_run"])


def run_fetch(s: Settings, use_cache: bool = True) -> dict:
    sources.set_cache_dir(Path(s.data_dir) / "cache")
    start, end = s.train[0], s.issue[1]
    result: dict = {}
    for a in _assets(s):
        intervals = build_intervals(a, start, end, use_cache=use_cache)
        if not intervals:
            raise PipelineError(f"{a.key}: no intervals between {start} and {end}.")
        path = snapshot_path(s, a.key)
        written = True
        if path.exists() and read_snapshot(path) == intervals:
            written = False  # identical rows: keep the committed file (and its fetched_utc) untouched
        if written:
            meta = {"sources": sources.source_descriptors(a, start, end),
                    "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "weather_model": sources.weather_model()}
            write_snapshot(path, a, intervals, meta)
        result[a.key] = {"rows": len(intervals), "days": len({i.day for i in intervals}),
                         "written": written, "path": str(path)}
    return result


def run_train(s: Settings) -> dict:
    mod = _envelope_module()
    d0, d1 = day_from_date(s.train[0]), day_from_date(s.train[1])
    out: dict = {}
    for a in _assets(s):
        train = [i for i in _read_snapshot_for(s, a)
                 if d0 <= i.day <= d1 and i.ws100 is not None and i.kwh >= 0]
        if not train:
            raise PipelineError(f"{a.key}: no usable training intervals between {s.train[0]} and {s.train[1]}.")
        env = mod.fit_envelope(train, a.capacity_kw)
        doc = dict(mod.to_json(env))
        if "rules" in doc:
            raise PipelineError("envelope.to_json() must not use the key 'rules'; pipeline owns that key.")
        doc["rules"] = {"capacity_tol": DEFAULT_CAPACITY_TOL, "frozen_run": DEFAULT_FROZEN_RUN}
        raw = canonical_json(doc)
        path = model_path(s, a.key)
        atomic_write(path, raw)
        out[a.key] = {"n_train": len(train), "model_sha256": to_hex(sha256(raw)), "path": str(path)}
    return out


def run_judge(s: Settings) -> dict:
    d0, d1 = _issue_days(s)
    out: dict = {}
    for a in _assets(s):
        window = [i for i in _read_snapshot_for(s, a) if d0 <= i.day <= d1]
        if not window:
            raise PipelineError(f"{a.key}: snapshot has no intervals in the issue window "
                                f"{s.issue[0]}..{s.issue[1]}. Re-run fetch.")
        upper_fn, tol, run = judge_config(s, a)
        out[a.key] = judge(window, a.capacity_kw, upper_fn=upper_fn, capacity_tol=tol, frozen_run=run)
    return out


def make_batches(asset: Asset, verdicts: list[Verdict]) -> list[DayBatch]:
    by_day: dict[int, list[Verdict]] = defaultdict(list)
    for v in verdicts:
        if v.interval.asset_key != asset.key:
            raise PipelineError(f"make_batches: verdict for {v.interval.asset_key} given with asset {asset.key}.")
        by_day[v.interval.day].append(v)
    batches: list[DayBatch] = []
    for day in sorted(by_day):
        day_v = sorted(by_day[day], key=lambda v: v.interval.sp)
        accepted = [v for v in day_v if v.accepted]
        if not accepted:
            continue
        sps, kwh, g = [], [], []
        for v in accepted:
            if v.interval.g_per_kwh is None:
                raise PipelineError(f"{asset.key} day {day} sp {v.interval.sp}: accepted without grid "
                                    "intensity; the oracle must reject it as MISSING_DATA.")
            sps.append(v.interval.sp)
            kwh.append(v.interval.kwh)
            g.append(v.interval.g_per_kwh)
        leaves = [leaf_hash(asset.asset_id, day, sp, k, gg) for sp, k, gg in zip(sps, kwh, g)]
        batches.append(DayBatch(asset_key=asset.key, asset_id=asset.asset_id, day=day, sps=sps, kwh=kwh,
                                g=g, leaves=leaves, root=merkle_root(leaves), total_kwh=sum(kwh),
                                snapshot_hash=day_snapshot_hash([v.interval for v in day_v])))
    return batches


def _load_receipts(s: Settings) -> list[dict]:
    path = receipts_path(s)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{path} is not valid JSON ({exc}). Restore it from git or delete it.") from exc
    if not isinstance(data, list):
        raise PipelineError(f"{path} must hold a JSON array.")
    return data


def _append_receipt(s: Settings, receipt: dict) -> None:
    atomic_write(receipts_path(s), json.dumps(_load_receipts(s) + [receipt], indent=1).encode("utf-8") + b"\n")


def _receipt(kind: str, asset_key: str, day: int, rc: dict) -> dict:
    return {"kind": kind, "asset_key": asset_key, "day": day,
            "tx": rc["tx"], "block": rc["block"], "gas_used": rc["gas_used"]}


def _batch_doc(b: DayBatch, model_hash: bytes) -> dict:
    return {"asset_id": to_hex(b.asset_id), "day": b.day, "root": to_hex(b.root),
            "model_hash": to_hex(model_hash), "snapshot_hash": to_hex(b.snapshot_hash),
            "total_kwh": b.total_kwh, "intervals": len(b.sps), "sps": list(b.sps),
            "kwh": list(b.kwh), "g": list(b.g), "leaves": [to_hex(x) for x in b.leaves]}


def run_issue(s: Settings, chain: Chain, max_days: int | None = None) -> list[dict]:
    if max_days is not None and max_days < 1:
        raise PipelineError("--max-days must be >= 1.")
    verdicts = run_judge(s)
    new: list[dict] = []
    for a in _assets(s):
        _, raw = ensure_model(s, a)
        model_hash = sha256(raw)
        state = chain.get_asset(a.asset_id)
        if not state["exists"]:
            rc = _receipt("register", a.key, 0, chain.register_asset(a))
            _append_receipt(s, rc)
            new.append(rc)
        elif state["capacity_kw"] != a.capacity_kw:
            raise PipelineError(f"{a.key} is registered on-chain with capacity {state['capacity_kw']} kW but "
                                f"assets.yaml says {a.capacity_kw} kW. Use a fresh deployment or fix assets.yaml.")
        batches = make_batches(a, verdicts[a.key])
        if max_days is not None:
            batches = batches[:max_days]
        for b in batches:
            existing = chain.get_batch(a.asset_id, b.day)
            doc = _batch_doc(b, model_hash)
            if existing is not None:
                if existing["root"] != b.root or existing["model_hash"] != model_hash:
                    raise PipelineError(
                        f"{a.key} day {b.day} is already issued on-chain with a different root or model hash "
                        f"(chain root {to_hex(existing['root'])}, local {to_hex(b.root)}). The contract forbids "
                        "re-issuing; restart the local node and redeploy, or restore the original data/models "
                        "and snapshots from git.")
                atomic_write(batch_path(s, a.key, b.day), canonical_json(doc))
                continue
            rc = _receipt("issue", a.key, b.day, chain.issue_batch(b, model_hash))
            atomic_write(batch_path(s, a.key, b.day), canonical_json(doc))
            _append_receipt(s, rc)
            new.append(rc)
    return new


def run_retire(s: Settings, chain: Chain, asset_key: str, day: int, sp: int, beneficiary: str) -> dict:
    if not beneficiary.strip():
        raise PipelineError("beneficiary must be a non-empty name.")
    asset = _asset(s, asset_key)
    path = batch_path(s, asset.key, day)
    if not path.exists():
        raise PipelineError(f"No published batch at {path}. Run `issue` for that day first.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    sps = doc["sps"]
    if sp not in sps:
        raise PipelineError(f"Period {sp} is not certified for {asset.key} on {day}. Certified periods: {sps}.")
    i = sps.index(sp)
    kwh, g = doc["kwh"][i], doc["g"][i]
    leaves = [from_hex(x) for x in doc["leaves"]]
    if leaves[i] != leaf_hash(asset.asset_id, day, sp, kwh, g):
        raise PipelineError(f"{path}: stored leaf for sp {sp} does not match its kwh/g. The batch file was altered.")
    proof = merkle_proof(leaves, i)
    if not verify_proof(leaves[i], proof, from_hex(doc["root"])):
        raise PipelineError(f"{path}: Merkle proof for sp {sp} does not verify against the stored root.")
    ref = beneficiary_ref(beneficiary)
    rc = _receipt("retire", asset.key, day, chain.retire(asset.asset_id, day, sp, kwh, g, proof, ref))
    _append_receipt(s, rc)
    return {**rc, "sp": sp, "kwh": kwh, "g_per_kwh": g, "grams": kwh * g,
            "beneficiary": beneficiary, "beneficiary_ref": to_hex(ref)}
