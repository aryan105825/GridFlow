# @exports: RecomputeError(RuntimeError)
# @exports: VerifyResult(asset_key: str, day: int, ok: bool, root_chain: bytes | None, root_local: bytes | None, root_published: bytes, intervals_chain: int, intervals_local: int, model_hash_ok: bool, mismatches: list[{"sp","field","published","local"}], drift: list[{"sp","published_accepted","local_reason"}])
# @exports: verify_day(chain: Chain, asset: Asset, day: int, mode: "snapshot"|"live", s: Settings, *, live_intervals: list[Interval] | None = None) -> VerifyResult
# @exports: verify_all(chain: Chain, assets: list[Asset], s: Settings, mode: "snapshot"|"live", days: list[int] | None = None) -> list[VerifyResult]   # days=None -> every published batch file; raises RecomputeError if nothing matches
# @imports: gridproof/pipeline.py:batch_path(s, key, day), snapshot_path(s, key), model_path(s, key), judge_config(s, asset)
# @imports: gridproof/dataset.py:build_intervals(asset, start, end, use_cache), read_snapshot(path), day_snapshot_hash(day_intervals), sha256(b)
# @imports: gridproof/merkle.py:leaf_hash(asset_id, day, sp, kwh, g), merkle_root(leaves), from_hex(s), to_hex(b)
# @imports: gridproof/oracle.py:judge(intervals, capacity_kw, upper_fn, capacity_tol, frozen_run)
# @imports: gridproof/chain.py:Chain.get_batch(asset_id, day)
# @imports: gridproof/schemas.py:Asset, Interval, Settings, date_from_day(day)
# @env: none
# @schema: hard mismatch fields: "kwh" and "g" (per sp, published batch file vs local data), "root" (sp 0: root rebuilt locally vs on-chain), "root_published" (sp 0), "intervals" (sp 0), "snapshot_hash" (sp 0, snapshot mode only), "model_hash" (sp 0), "batch_on_chain" (sp 0)
# @schema: snapshot mode: local = data/snapshots/{key}.json rows; root rebuilt for the PUBLISHED sps; drift is always []
# @schema: live mode: local = fresh fetch (use_cache=False); kWh and g compared first; the envelope is re-run only to list decision flips as drift ({"sp","published_accepted","local_reason"}), which never changes ok

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from gridproof.chain import Chain
from gridproof.dataset import build_intervals, day_snapshot_hash, read_snapshot, sha256
from gridproof.merkle import from_hex, leaf_hash, merkle_root, to_hex
from gridproof.oracle import judge
from gridproof.pipeline import batch_path, judge_config, model_path, snapshot_path
from gridproof.schemas import Asset, Interval, Settings, date_from_day


class RecomputeError(RuntimeError):
    pass


@dataclass
class VerifyResult:
    asset_key: str
    day: int
    ok: bool
    root_chain: bytes | None
    root_local: bytes | None
    root_published: bytes
    intervals_chain: int
    intervals_local: int
    model_hash_ok: bool
    mismatches: list[dict] = field(default_factory=list)
    drift: list[dict] = field(default_factory=list)


def _hex(b: bytes | None) -> str | None:
    return None if b is None else to_hex(b)


def verify_day(chain: Chain, asset: Asset, day: int, mode: Literal["snapshot", "live"], s: Settings,
               *, live_intervals: list[Interval] | None = None) -> VerifyResult:
    if mode not in ("snapshot", "live"):
        raise ValueError(f"verify_day: mode must be 'snapshot' or 'live', got {mode!r}")
    bpath = batch_path(s, asset.key, day)
    if not bpath.exists():
        raise RecomputeError(f"No published batch file {bpath}. Run `python -m gridproof.cli issue` first, "
                             "or restore data/batches from git.")
    pub = json.loads(bpath.read_text(encoding="utf-8"))
    sps, pkwh, pg = pub["sps"], pub["kwh"], pub["g"]
    root_published = from_hex(pub["root"])

    on_chain = chain.get_batch(asset.asset_id, day)
    if on_chain is None:
        return VerifyResult(asset.key, day, False, None, None, root_published, 0, 0, False,
                            [{"sp": 0, "field": "batch_on_chain", "published": to_hex(root_published),
                              "local": "no batch issued on-chain for this asset-day"}], [])

    mismatches: list[dict] = []
    snap_ints: list[Interval] = []
    if mode == "snapshot":
        spath = snapshot_path(s, asset.key)
        snap_ints = [i for i in read_snapshot(spath) if i.day == day]
        local_by_sp = {i.sp: i for i in snap_ints}
    else:
        ints = live_intervals
        if ints is None:
            d = date_from_day(day)
            ints = build_intervals(asset, d, d, use_cache=False)
        local_by_sp = {i.sp: i for i in ints if i.day == day}

    leaves: list[bytes] = []
    buildable = True
    found = 0
    for sp, k, gg in zip(sps, pkwh, pg):
        li = local_by_sp.get(sp)
        if li is None:
            mismatches.append({"sp": sp, "field": "kwh", "published": k, "local": None})
            mismatches.append({"sp": sp, "field": "g", "published": gg, "local": None})
            buildable = False
            continue
        found += 1
        if li.kwh != k:
            mismatches.append({"sp": sp, "field": "kwh", "published": k, "local": li.kwh})
        if li.g_per_kwh != gg:
            mismatches.append({"sp": sp, "field": "g", "published": gg, "local": li.g_per_kwh})
        if li.g_per_kwh is None or not (0 <= li.kwh < 2 ** 64 and 0 <= li.g_per_kwh < 2 ** 32):
            buildable = False
            continue
        leaves.append(leaf_hash(asset.asset_id, day, sp, li.kwh, li.g_per_kwh))

    root_local = merkle_root(leaves) if (buildable and leaves) else None
    if root_local is not None and root_local != on_chain["root"]:
        mismatches.append({"sp": 0, "field": "root", "published": to_hex(on_chain["root"]),
                           "local": to_hex(root_local)})
    if root_published != on_chain["root"]:
        mismatches.append({"sp": 0, "field": "root_published", "published": to_hex(on_chain["root"]),
                           "local": to_hex(root_published)})
    if len(sps) != on_chain["intervals"]:
        mismatches.append({"sp": 0, "field": "intervals", "published": on_chain["intervals"], "local": len(sps)})
    if mode == "snapshot" and snap_ints:
        local_snap_hash = day_snapshot_hash(snap_ints)
        if local_snap_hash != on_chain["snapshot_hash"]:
            mismatches.append({"sp": 0, "field": "snapshot_hash", "published": to_hex(on_chain["snapshot_hash"]),
                               "local": to_hex(local_snap_hash)})

    mpath = model_path(s, asset.key)
    if mpath.exists():
        model_ok = sha256(mpath.read_bytes()) == on_chain["model_hash"]
        local_model = to_hex(sha256(mpath.read_bytes()))
    else:
        model_ok = False
        local_model = f"model file {mpath} missing"
    if not model_ok:
        mismatches.append({"sp": 0, "field": "model_hash", "published": to_hex(on_chain["model_hash"]),
                           "local": local_model})

    drift: list[dict] = []
    if mode == "live" and local_by_sp:
        upper_fn, tol, run = judge_config(s, asset)
        day_ints = [local_by_sp[sp] for sp in sorted(local_by_sp)]
        published = set(sps)
        for v in judge(day_ints, asset.capacity_kw, upper_fn=upper_fn, capacity_tol=tol, frozen_run=run):
            sp = v.interval.sp
            if sp in published and not v.accepted:
                drift.append({"sp": sp, "published_accepted": True, "local_reason": v.reason})
            elif sp not in published and v.accepted:
                drift.append({"sp": sp, "published_accepted": False, "local_reason": v.reason})

    mismatches.sort(key=lambda m: (m["sp"], m["field"]))
    return VerifyResult(asset_key=asset.key, day=day, ok=not mismatches, root_chain=on_chain["root"],
                        root_local=root_local, root_published=root_published,
                        intervals_chain=on_chain["intervals"], intervals_local=found,
                        model_hash_ok=model_ok, mismatches=mismatches, drift=drift)


def _published_days(s: Settings, key: str) -> list[int]:
    folder = Path(s.data_dir) / "batches"
    days: list[int] = []
    for p in folder.glob(f"{key}_*.json"):
        tail = p.stem[len(key) + 1:]
        if len(tail) == 8 and tail.isdigit():
            days.append(int(tail))
    return sorted(days)


def verify_all(chain: Chain, assets: list[Asset], s: Settings, mode: Literal["snapshot", "live"],
               days: list[int] | None = None) -> list[VerifyResult]:
    results: list[VerifyResult] = []
    for a in assets:
        available = _published_days(s, a.key)
        selected = available if days is None else [d for d in days if d in set(available)]
        if not selected:
            continue
        live = None
        if mode == "live":
            live = build_intervals(a, date_from_day(min(selected)), date_from_day(max(selected)),
                                   use_cache=False)
        for d in selected:
            results.append(verify_day(chain, a, d, mode, s, live_intervals=live))
    if not results:
        raise RecomputeError(f"No published batches matched (assets {[a.key for a in assets]}, days {days}). "
                             f"Looked in {Path(s.data_dir) / 'batches'}; run `issue` first or fix --asset/--day.")
    return results
