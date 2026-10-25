# @exports: EvaluateError(RuntimeError)
# @exports: write_json(s: Settings, name: str, obj: Any) -> Path ; read_json(s: Settings, name: str) -> Any   # results/<name>, raises EvaluateError if missing
# @exports: published_batches(s: Settings, assets: list[Asset]) -> list[{"key","asset","day","doc","path"}]   # parsed data/batches/{key}_{day}.json sorted by (key, day); raises if none
# @exports: load_envelope(s: Settings, asset: Asset) -> Envelope ; split_snapshot(s: Settings, asset: Asset) -> (train: list[Interval], issue: list[Interval])
# @exports: eval_calibration(s: Settings, verdicts: dict | None = None) -> dict ; eval_detection(s: Settings, seed: int = 7, factors: tuple = (1.1, 1.25, 1.5, 2.0), verdicts: dict | None = None, max_samples: int = 2000) -> dict
# @exports: eval_blind(s: Settings) -> dict ; eval_chain(s: Settings, chain: Chain, receipts: list[dict]) -> dict ; eval_emissions(s: Settings) -> dict   # eval_chain takes s and chain (differs from CONTRACTS)
# @exports: run_evaluate(s: Settings, chain: Chain) -> dict   # writes results/{calibration,detection,blind,chain,emissions}.json
# @imports: gridproof/schemas.py:Asset, Interval, Settings, load_assets(path), day_from_date(d), date_from_day(day)
# @imports: gridproof/dataset.py:atomic_write(path, data), read_snapshot(path)
# @imports: gridproof/envelope.py:from_json(d), blind_regions(env, train, capacity_kw)
# @imports: gridproof/oracle.py:judge(intervals, capacity_kw, upper_fn, capacity_tol, frozen_run), summary(verdicts)
# @imports: gridproof/pipeline.py:run_judge(s), judge_config(s, asset), snapshot_path(s, key), model_path(s, key), batch_path(s, key, day), receipts_path(s)
# @imports: gridproof/chain.py:Chain.chain_id, Chain.address
# @env: none (all settings arrive through Settings)
# @schema: calibration.json {key:{"n_train","n_test","n_missing_data","n_negative_meter","min_negative_kwh","false_flag_rate","by_reason"}}; false_flag_rate = rejected / judged, on the held-out issue month of real data, excluding MISSING_DATA and NEG_OR_NAN. Negative B1610 readings are station import during calm wind: real meter values that earn no certificate, counted separately in n_negative_meter / min_negative_kwh, not as model false flags
# @schema: detection.json {"kind":"sensitivity_test_perturbation_of_real_data","seed","factors","max_samples","per_asset":{key:{"1.1":{"n","detected","recall"}}}}; the same seeded sample of accepted, non-zero real intervals is inflated by each factor and re-judged
# @schema: blind.json {key:[{"ws_bin","headroom_kwh_per_period","headroom_pct_capacity","n"}]} ; chain.json {"chain_id","contract","batches","gas_issue_mean","gas_per_interval","gas_retire_mean"|null}
# @schema: emissions.json {"kwh_certified","tco2e_halfhourly","tco2e_flat_mean","overstatement_pct","flat_basis","by_asset":{key:{...}}}; half-hourly = sum(kwh*g) over certified leaves; flat = certified kWh x unweighted mean NESO actual over all issue-window periods of the asset

from __future__ import annotations

import dataclasses
import json
import random
import re
from pathlib import Path
from typing import Any

from gridproof.chain import Chain
from gridproof.dataset import atomic_write, read_snapshot
from gridproof.envelope import blind_regions, from_json
from gridproof.oracle import judge, summary
from gridproof.pipeline import batch_path, judge_config, model_path, receipts_path, run_judge, snapshot_path
from gridproof.schemas import Asset, Interval, Settings, date_from_day, day_from_date, load_assets

DETECTION_KIND = "sensitivity_test_perturbation_of_real_data"


class EvaluateError(RuntimeError):
    pass


def write_json(s: Settings, name: str, obj: Any) -> Path:
    folder = Path(s.results_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    atomic_write(path, (json.dumps(obj, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))
    return path


def read_json(s: Settings, name: str) -> Any:
    path = Path(s.results_dir) / name
    if not path.exists():
        raise EvaluateError(f"{path} is missing. Fix: run `python -m gridproof.cli evaluate` first.")
    return json.loads(path.read_text(encoding="utf-8"))


def published_batches(s: Settings, assets: list[Asset]) -> list[dict]:
    out: list[dict] = []
    for a in assets:
        folder = batch_path(s, a.key, 0).parent
        if not folder.is_dir():
            continue
        pat = re.compile(rf"^{re.escape(a.key)}_(\d{{8}})\.json$")
        for f in sorted(folder.iterdir()):
            m = pat.match(f.name)
            if m:
                out.append({"key": a.key, "asset": a, "day": int(m.group(1)),
                            "doc": json.loads(f.read_text(encoding="utf-8")), "path": f})
    if not out:
        raise EvaluateError(f"no published batch files found under {Path(s.data_dir) / 'batches'}. "
                            "Fix: run `python -m gridproof.cli issue` first.")
    return sorted(out, key=lambda b: (b["key"], b["day"]))


def load_envelope(s: Settings, asset: Asset):
    path = model_path(s, asset.key)
    if not path.exists():
        raise EvaluateError(f"{path} is missing. Fix: run `python -m gridproof.cli train`.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if "ws_edges" not in doc:
        raise EvaluateError(f"{path} holds a rules-only model, not an envelope. Fix: run `python -m gridproof.cli train`.")
    doc.pop("rules", None)
    return from_json(doc)


def split_snapshot(s: Settings, asset: Asset) -> tuple[list[Interval], list[Interval]]:
    ints = read_snapshot(snapshot_path(s, asset.key))
    t0, t1 = day_from_date(s.train[0]), day_from_date(s.train[1])
    i0, i1 = day_from_date(s.issue[0]), day_from_date(s.issue[1])
    return ([i for i in ints if t0 <= i.day <= t1], [i for i in ints if i0 <= i.day <= i1])


def eval_calibration(s: Settings, verdicts: dict | None = None) -> dict:
    verdicts = verdicts if verdicts is not None else run_judge(s)
    out: dict[str, Any] = {}
    for a in load_assets(s.assets_file):
        vs = verdicts[a.key]
        sm = summary(vs)
        missing = sm["by_reason"].get("MISSING_DATA", 0)
        neg_vals = [v.interval.kwh for v in vs if v.reason == "NEG_OR_NAN"]
        neg = len(neg_vals)
        n_test = len(vs) - missing - neg
        if n_test <= 0:
            raise EvaluateError(f"{a.key}: no judgeable intervals in the issue window. Fix: re-run `fetch` and check the issue dates.")
        flagged = sm["rejected"] - missing - neg
        out[a.key] = {"n_train": load_envelope(s, a).n_train, "n_test": n_test, "n_missing_data": missing,
                      "n_negative_meter": neg, "min_negative_kwh": min(neg_vals) if neg_vals else None,
                      "false_flag_rate": round(flagged / n_test, 6), "by_reason": sm["by_reason"]}
    return out


def eval_detection(s: Settings, seed: int = 7, factors: tuple = (1.1, 1.25, 1.5, 2.0),
                   verdicts: dict | None = None, max_samples: int = 2000) -> dict:
    verdicts = verdicts if verdicts is not None else run_judge(s)
    per_asset: dict[str, Any] = {}
    for a in load_assets(s.assets_file):
        vs = verdicts[a.key]
        ints = [v.interval for v in vs]
        cand = [i for i, v in enumerate(vs) if v.accepted and v.interval.kwh > 0]
        if not cand:
            raise EvaluateError(f"{a.key}: no accepted non-zero intervals to perturb.")
        rng = random.Random(f"{seed}:{a.key}")
        chosen = sorted(rng.sample(cand, min(len(cand), max_samples)))
        upper_fn, tol, frozen = judge_config(s, a)
        res: dict[str, Any] = {}
        for f in factors:
            perturbed = list(ints)
            for i in chosen:
                perturbed[i] = dataclasses.replace(ints[i], kwh=int(round(ints[i].kwh * f)))
            vv = judge(perturbed, a.capacity_kw, upper_fn, tol, frozen)
            detected = sum(1 for i in chosen if not vv[i].accepted)
            res[str(f)] = {"n": len(chosen), "detected": detected, "recall": round(detected / len(chosen), 6)}
        per_asset[a.key] = res
    return {"kind": DETECTION_KIND, "seed": seed, "factors": [float(f) for f in factors],
            "max_samples": max_samples, "per_asset": per_asset}


def eval_blind(s: Settings) -> dict:
    out: dict[str, Any] = {}
    for a in load_assets(s.assets_file):
        train, _ = split_snapshot(s, a)
        out[a.key] = blind_regions(load_envelope(s, a), train, a.capacity_kw)
    return out


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 1)


def eval_chain(s: Settings, chain: Chain, receipts: list[dict]) -> dict:
    assets = load_assets(s.assets_file)
    batches = {(b["key"], b["day"]): b for b in published_batches(s, assets)}
    latest: dict[tuple[str, int], dict] = {}
    for r in receipts:
        if r.get("kind") == "issue":
            latest[(r["asset_key"], int(r["day"]))] = r
    if not latest:
        raise EvaluateError("no issue receipts in data/receipts.json. Fix: run `python -m gridproof.cli issue`.")
    gas: list[float] = []
    intervals = 0
    for (key, day), r in latest.items():
        b = batches.get((key, day))
        if b is None:
            raise EvaluateError(f"receipt for {key} {day} has no data/batches file. Fix: re-run `issue` on a clean chain.")
        gas.append(int(r["gas_used"]))
        intervals += int(b["doc"]["intervals"])
    retire = [int(r["gas_used"]) for r in receipts if r.get("kind") == "retire"]
    return {"chain_id": chain.chain_id, "contract": chain.address, "batches": len(latest),
            "gas_issue_mean": _mean(gas), "gas_per_interval": round(sum(gas) / intervals, 1),
            "gas_retire_mean": _mean(retire) if retire else None}


def eval_emissions(s: Settings) -> dict:
    assets = load_assets(s.assets_file)
    batches = published_batches(s, assets)
    by_asset: dict[str, Any] = {}
    for a in assets:
        mine = [b for b in batches if b["key"] == a.key]
        if not mine:
            continue
        kwh = sum(sum(b["doc"]["kwh"]) for b in mine)
        grams = sum(k * g for b in mine for k, g in zip(b["doc"]["kwh"], b["doc"]["g"]))
        _, issue = split_snapshot(s, a)
        gs = [i.g_per_kwh for i in issue if i.g_per_kwh is not None]
        if not gs or grams <= 0:
            raise EvaluateError(f"{a.key}: cannot compute emissions (no intensity data or zero certified energy).")
        flat_g = sum(gs) / len(gs)
        hh_t = grams / 1e6
        flat_t = kwh * flat_g / 1e6
        by_asset[a.key] = {"kwh_certified": kwh, "days": len(mine), "flat_mean_g_per_kwh": round(flat_g, 3),
                           "tco2e_halfhourly": round(hh_t, 6), "tco2e_flat_mean": round(flat_t, 6),
                           "overstatement_pct": round((flat_t / hh_t - 1) * 100, 3)}
    if not by_asset:
        raise EvaluateError("no certified energy found. Fix: run `python -m gridproof.cli issue`.")
    kwh_all = sum(v["kwh_certified"] for v in by_asset.values())
    hh_all = sum(v["tco2e_halfhourly"] for v in by_asset.values())
    flat_all = sum(v["tco2e_flat_mean"] for v in by_asset.values())
    return {"kwh_certified": kwh_all, "tco2e_halfhourly": round(hh_all, 6), "tco2e_flat_mean": round(flat_all, 6),
            "overstatement_pct": round((flat_all / hh_all - 1) * 100, 3),
            "flat_basis": "unweighted mean of NESO actual gCO2/kWh over every issue-window half-hour of the asset, applied to certified kWh",
            "by_asset": by_asset}


def run_evaluate(s: Settings, chain: Chain) -> dict:
    rpath = receipts_path(s)
    if not Path(rpath).exists():
        raise EvaluateError(f"{rpath} is missing. Fix: run `python -m gridproof.cli issue` first.")
    receipts = json.loads(Path(rpath).read_text(encoding="utf-8"))
    verdicts = run_judge(s)
    out = {
        "calibration": eval_calibration(s, verdicts),
        "detection": eval_detection(s, verdicts=verdicts),
        "blind": eval_blind(s),
        "chain": eval_chain(s, chain, receipts),
        "emissions": eval_emissions(s),
    }
    for name, obj in out.items():
        write_json(s, f"{name}.json", obj)
    return out
