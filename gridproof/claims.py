# @exports: ClaimsError(RuntimeError)
# @exports: Check(id: str, claim: str, ok: bool, detail: str, skipped: bool = False)   # frozen dataclass; skipped=True means not run (ok=True), never a PASS
# @exports: CLAIMS: list[(id, claim_text)]   # C01..C14 in ledger order
# @exports: run_checks(s: Settings, chain: Chain, live: bool = True) -> list[Check]   # also writes results/claims.json; a check that raises becomes FAIL with the error text
# @exports: format_check(c: Check) -> str   # "PASS <id> <claim> (<detail>)" | "FAIL <id> <claim>: <detail>" | "SKIP <id> <claim>: <detail>"
# @exports: main(argv: list[str] | None = None) -> int   # python -m gridproof.claims [--network localhost|testnet] [--live] ; 0 all ok, 1 any FAIL
# @imports: gridproof/schemas.py:Settings, load_settings(env, network), load_assets(path), date_from_day(day), day_from_date(d), SEPOLIA_CHAIN_ID
# @imports: gridproof/chain.py:Chain (from_settings, leaf_of, verify_leaf, get_batch, get_asset, retire, retired_by, retired_grams, issue_batch, expect_revert, w3, address, sender), beneficiary_ref(name)
# @imports: gridproof/dataset.py:build_intervals(asset, start, end, use_cache), expected_periods(day), canonical_json(obj), sha256(b), atomic_write(path, data), read_snapshot(path)
# @imports: gridproof/merkle.py:leaf_hash(asset_id, day, sp, kwh, g), merkle_root(leaves), merkle_proof(leaves, index), from_hex(s), to_hex(b)
# @imports: gridproof/pipeline.py:run_judge(s), run_retire(s, chain, asset_key, day, sp, beneficiary), snapshot_path(s, key), model_path(s, key), batch_path(s, key, day)
# @imports: gridproof/recompute.py:verify_all(chain, assets, s, mode, days), verify_day(chain, asset, day, mode, s)
# @imports: gridproof/evaluate.py:published_batches(s, assets), read_json(s, name), write_json(s, name, obj)
# @imports: gridproof/schemas.py:DayBatch
# @imports: web/verify.html: CORE block functions leafOf(assetId, day, sp, kwh, g) -> hex and merkleRoot(leavesHex[]) -> hex (call signatures ASSUMED, not in the header; a mismatch makes C05 FAIL loudly)
# @env: none (all settings arrive through Settings; subprocess checks pass DATA_DIR only)
# @schema: results/claims.json = [{"id","claim","ok","detail","skipped"}]
# @schema: C01 and the live half of C10 need network and run only with live=True (otherwise SKIP); every other check runs offline against the chain and the committed files
# @schema: state-changing checks: C09 retires 5 un-retired non-zero certified intervals to beneficiary "gridproof-claims" and updates results/chain.json gas_retire_mean; C06/C07/C08 use reverting calls only

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from web3 import Web3

from gridproof.chain import Chain, beneficiary_ref
from gridproof.dataset import atomic_write, build_intervals, canonical_json, expected_periods, read_snapshot, sha256
from gridproof.evaluate import published_batches, read_json, write_json
from gridproof.merkle import from_hex, leaf_hash, merkle_proof, merkle_root, to_hex
from gridproof.pipeline import batch_path, model_path, run_judge, run_retire, snapshot_path
from gridproof.recompute import verify_all, verify_day
from gridproof.schemas import SEPOLIA_CHAIN_ID, DayBatch, Settings, date_from_day, day_from_date, load_assets, load_settings

BENEFICIARY = "gridproof-claims"
FALSE_FLAG_MAX = 0.03
GAS_ISSUE_MAX = 1_000_000
STRANGER = Web3.to_checksum_address("0x000000000000000000000000000000000000dEaD")
ABI_PATH = Path("artifacts/contracts/GridProofRegistry.sol/GridProofRegistry.json")
REVERTS = ("ASSET_EXISTS", "UNKNOWN_ASSET", "BATCH_EXISTS", "EMPTY_BATCH", "EXCEEDS_DAILY_CAPACITY", "BATCH_MISSING",
           "NOT_HOLDER", "EXCEEDS_INTERVAL_CAPACITY", "BAD_PROOF", "ALREADY_RETIRED")

CLAIMS = [
    ("C01", "Every certified half-hour comes from public data (B1610, ERA5, NESO); nothing is synthetic"),
    ("C02", "Every settlement period is accounted for, including the 46-period DST day (30 Mar 2025)"),
    ("C03", "Held-out March false-flag rate is at most 3% per asset"),
    ("C04", "Detection vs inflation is a labelled sensitivity test on real data; blind regions are published"),
    ("C05", "Python, Solidity and browser JS compute identical leaves and roots"),
    ("C06", "Re-issuing an asset-day is impossible"),
    ("C07", "An interval retires exactly once; forged kWh or proof fails"),
    ("C08", "On-chain physical capacity caps hold"),
    ("C09", "On-chain retired grams equal the independent Python sum"),
    ("C10", "All on-chain roots are reproduced from snapshots; live re-fetch reproduces kWh and g"),
    ("C11", "A one-digit tamper is detected and localised"),
    ("C12", "The on-chain model hash equals the sha256 of the model file"),
    ("C13", "Gas per certified half-hour is measured (issue gas below 1M)"),
    ("C14", "Half-hourly vs flat-average tCO2e is measured and kWh matches the chain"),
]


class ClaimsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    id: str
    claim: str
    ok: bool
    detail: str
    skipped: bool = False


def format_check(c: Check) -> str:
    if c.skipped:
        return f"SKIP {c.id} {c.claim}: {c.detail}"
    if c.ok:
        return f"PASS {c.id} {c.claim} ({c.detail})"
    return f"FAIL {c.id} {c.claim}: {c.detail}"


def _leaves(doc: dict) -> list[bytes]:
    return [from_hex(x) for x in doc["leaves"]]


def _call_reason(fn, sender: str) -> str:
    try:
        fn.call({"from": sender})
    except Exception as e:  # the revert text is parsed, anything unrecognised is raised below
        text = " ".join([str(e)] + [str(a) for a in e.args])
        for r in REVERTS:
            if r in text:
                return r
        raise ClaimsError(f"eth_call reverted without a known reason string: {text[:300]}") from e
    raise ClaimsError("eth_call succeeded but a revert was expected")


def _flip(n: int) -> int:
    d = str(n)
    last = (int(d[-1]) + 1) % 10
    return int(d[:-1] + str(last)) if len(d) > 1 else last


class _Ctx:
    def __init__(self, s: Settings, chain: Chain):
        self.s, self.chain = s, chain
        self.assets = load_assets(s.assets_file)
        self.batches = published_batches(s, self.assets)
        self._verdicts = None
        self._retire = None
        self._retire_err: Exception | None = None

    def verdicts(self):
        if self._verdicts is None:
            self._verdicts = run_judge(self.s)
        return self._verdicts

    def contract(self):
        if not ABI_PATH.exists():
            raise ClaimsError(f"{ABI_PATH} is missing. Fix: run `npm run compile`.")
        abi = json.loads(ABI_PATH.read_text(encoding="utf-8"))["abi"]
        return self.chain.w3.eth.contract(address=Web3.to_checksum_address(self.chain.address), abi=abi)

    def day_batch(self, b: dict) -> DayBatch:
        d = b["doc"]
        return DayBatch(asset_key=b["key"], asset_id=from_hex(d["asset_id"]), day=b["day"], sps=list(d["sps"]),
                        kwh=list(d["kwh"]), g=list(d["g"]), leaves=_leaves(d), root=from_hex(d["root"]),
                        total_kwh=int(d["total_kwh"]), snapshot_hash=from_hex(d["snapshot_hash"]))

    def retire_demo(self) -> dict:
        if self._retire_err is not None:
            raise self._retire_err
        if self._retire is not None:
            return self._retire
        try:
            self._retire = self._do_retire()
        except Exception as e:
            self._retire_err = e
            raise
        return self._retire

    def _do_retire(self) -> dict:
        chain, s = self.chain, self.s
        picks = []
        for b in self.batches:
            leaves = _leaves(b["doc"])
            for i in range(len(b["doc"]["sps"])):
                if b["doc"]["kwh"][i] > 0 and int(chain.retired_by(leaves[i]), 16) == 0:
                    picks.append((b, i, leaves))
                    if len(picks) == 5:
                        break
            if len(picks) == 5:
                break
        if len(picks) < 5:
            raise ClaimsError("fewer than 5 un-retired non-zero certified intervals are left. Fix: run on a fresh chain (`issue` again).")
        for b, _, _ in picks:
            holder = chain.get_batch(from_hex(b["doc"]["asset_id"]), b["day"])
            if holder is None:
                raise ClaimsError(f"{b['key']} {b['day']} is not issued on this chain. Fix: run `issue`.")
            if chain.sender is None or holder["holder"].lower() != chain.sender.lower():
                raise ClaimsError("the signer is not the batch holder, so retire cannot be tested. Fix: use the issuer key.")
        ref = beneficiary_ref(BENEFICIARY)
        before = chain.retired_grams(ref)
        recs = [run_retire(s, chain, b["key"], b["day"], b["doc"]["sps"][i], BENEFICIARY) for b, i, _ in picks]
        after = chain.retired_grams(ref)
        expected = sum(b["doc"]["kwh"][i] * b["doc"]["g"][i] for b, i, _ in picks)
        gas = [int(r["gas_used"]) for r in recs]
        path = Path(s.results_dir) / "chain.json"
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            d["gas_retire_mean"] = round(sum(gas) / len(gas), 1)
            write_json(s, "chain.json", d)
        return {"picks": picks, "ref": ref, "delta": after - before, "expected": expected,
                "returned": sum(int(r["grams"]) for r in recs), "gas_mean": round(sum(gas) / len(gas), 1)}


def _c01(x: _Ctx, live: bool):
    s = x.s
    b = x.batches[0]
    doc = b["doc"]
    snap = json.loads(snapshot_path(s, b["key"]).read_text(encoding="utf-8"))
    srcs = snap.get("meta", {}).get("sources") or []
    if not srcs or not all(e.get("url") for e in srcs) or not snap["meta"].get("fetched_utc"):
        return False, "snapshot meta lacks source URLs or fetched_utc"
    rows = {(r[0], r[1]): r for r in snap["rows"]}
    for i, sp in enumerate(doc["sps"]):
        r = rows.get((b["day"], sp))
        if r is None or r[3] != doc["kwh"][i] or r[5] != doc["g"][i]:
            return False, f"published batch {b['key']} {b['day']} sp={sp} differs from the snapshot row"
    if not live:
        return None, "needs network; re-run with --live (offline part ok: meta URLs present, batch equals snapshot)"
    d = date_from_day(b["day"])
    fresh = {i.sp: i for i in build_intervals(b["asset"], d, d, use_cache=False)}
    bad = []
    for (day, sp), r in rows.items():
        if day != b["day"]:
            continue
        f = fresh.get(sp)
        if f is None or f.kwh != r[3] or f.g_per_kwh != r[5]:
            bad.append(sp)
    if bad:
        return False, f"{b['key']} {b['day']}: live re-fetch differs from snapshot at sp={bad[:5]}"
    return True, f"{b['key']} {b['day']}: {len(fresh)} live periods equal the snapshot kWh and g; {len(srcs)} source URLs recorded"


def _c02(x: _Ctx, live: bool):
    s = x.s
    by_day = {(b["key"], b["day"]): b for b in x.batches}
    totals = {"days": 0, "accepted": 0, "rejected": 0, "missing": 0}
    dst = ""
    for a in x.assets:
        per_day: dict[int, list] = {}
        for v in x.verdicts()[a.key]:
            per_day.setdefault(v.interval.day, []).append(v)
        d = s.issue[0]
        while d <= s.issue[1]:
            day = day_from_date(d)
            exp = expected_periods(day)
            vs = per_day.get(day, [])
            sps = [v.interval.sp for v in vs]
            if len(set(sps)) != len(sps) or any(not 1 <= sp <= exp for sp in sps):
                return False, f"{a.key} {day}: duplicate or out-of-range settlement periods (expected 1..{exp})"
            acc = sum(1 for v in vs if v.accepted)
            rej = len(vs) - acc
            miss = len(set(range(1, exp + 1)) - set(sps))
            if acc + rej + miss != exp:
                return False, f"{a.key} {day}: accepted {acc} + rejected {rej} + missing {miss} != {exp}"
            pub = by_day.get((a.key, day))
            if (pub["doc"]["intervals"] if pub else 0) != acc:
                return False, f"{a.key} {day}: {acc} accepted but {pub['doc']['intervals'] if pub else 0} certified. Fix: re-run `issue`."
            totals["days"] += 1
            totals["accepted"] += acc
            totals["rejected"] += rej
            totals["missing"] += miss
            if day == 20250330:
                dst = f"; DST day {a.key}: expected {exp}, accepted {acc}, rejected {rej}, missing {miss}"
                if exp != 46:
                    return False, f"expected_periods(20250330) is {exp}, not 46"
            d += timedelta(days=1)
    return True, f"{totals['days']} asset-days: accepted {totals['accepted']} + rejected {totals['rejected']} + missing {totals['missing']} == expected periods{dst}"


def _c03(x: _Ctx, live: bool):
    cal = read_json(x.s, "calibration.json")
    missing = [a.key for a in x.assets if a.key not in cal]
    if missing:
        return False, f"calibration.json lacks {missing}"
    bad = {k: v["false_flag_rate"] for k, v in cal.items() if v["false_flag_rate"] > FALSE_FLAG_MAX}
    rates = ", ".join(f"{k}={v['false_flag_rate']:.4f}" for k, v in cal.items())
    neg = ", ".join(f"{k}={v.get('n_negative_meter')} (min {v.get('min_negative_kwh')} kWh)" for k, v in cal.items())
    return (not bad), (f"above {FALSE_FLAG_MAX}: {bad}" if bad else f"rates {rates} (threshold {FALSE_FLAG_MAX}); negative import readings excluded and reported separately: {neg}")


def _c04(x: _Ctx, live: bool):
    det = read_json(x.s, "detection.json")
    if det.get("kind") != "sensitivity_test_perturbation_of_real_data":
        return False, f"detection.json kind is {det.get('kind')!r}"
    for key, res in det["per_asset"].items():
        rec = [res[k]["recall"] for k in sorted(res, key=float)]
        if any(b < a for a, b in zip(rec, rec[1:])):
            return False, f"{key}: recall decreases with factor: {rec}"
    blind = read_json(x.s, "blind.json")
    empty = [a.key for a in x.assets if not blind.get(a.key)]
    if empty:
        return False, f"blind.json has no regions for {empty}"
    last = {k: v[sorted(v, key=float)[-1]]["recall"] for k, v in det["per_asset"].items()}
    return True, f"kind label ok, recall non-decreasing, blind regions for {len(blind)} assets; recall at max factor {last}"


JS_HARNESS = r"""
const fs = require('fs');
const ethers = require('ethers');
const html = fs.readFileSync('web/verify.html', 'utf8');
const b = html.lastIndexOf('CORE-BEGIN'), e = html.lastIndexOf('CORE-END');
if (b < 0 || e < 0 || e < b) throw new Error('CORE-BEGIN/CORE-END markers not found in web/verify.html');
const code = html.slice(html.indexOf('\n', b) + 1, html.lastIndexOf('\n', e));
const api = new Function('ethers', code + '\n;return {leafOf: typeof leafOf === "function" ? leafOf : null, merkleRoot: typeof merkleRoot === "function" ? merkleRoot : null};')(ethers);
if (!api.leafOf || !api.merkleRoot) throw new Error('CORE block must define leafOf and merkleRoot');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
(async () => {
  const leaves = [];
  for (const v of input.vectors) leaves.push(String(await api.leafOf(v.asset_id, v.day, v.sp, v.kwh, v.g)));
  const roots = [];
  for (const t of input.trees) roots.push(String(await api.merkleRoot(t)));
  console.log(JSON.stringify({leaves, roots}));
})().catch(err => { console.error(String(err && err.stack || err)); process.exit(1); });
"""


def _c05(x: _Ctx, live: bool):
    odd = [b for b in x.batches if len(b["doc"]["leaves"]) % 2 == 1]
    if not odd:
        return False, "no odd-sized tree among the published batches; the ledger requires one"
    rng = random.Random(5)
    pool = [(b, i) for b in x.batches for i in range(len(b["doc"]["sps"]))]
    sel = [(odd[0], len(odd[0]["doc"]["leaves"]) - 1)]
    rest = [p for p in pool if p != sel[0]]
    sel += rng.sample(rest, min(19, len(rest)))
    vectors = []
    for b, i in sel:
        d = b["doc"]
        aid = from_hex(d["asset_id"])
        sp, kwh, g = d["sps"][i], d["kwh"][i], d["g"][i]
        leaf = leaf_hash(aid, b["day"], sp, kwh, g)
        leaves = _leaves(d)
        if leaf != leaves[i]:
            return False, f"{b['key']} {b['day']} sp={sp}: python leaf differs from published leaf"
        if x.chain.leaf_of(aid, b["day"], sp, kwh, g) != leaf:
            return False, f"{b['key']} {b['day']} sp={sp}: Solidity leafOf differs from python"
        if not x.chain.verify_leaf(aid, b["day"], sp, kwh, g, merkle_proof(leaves, i)):
            return False, f"{b['key']} {b['day']} sp={sp}: on-chain verifyLeaf is false"
        vectors.append({"asset_id": d["asset_id"], "day": b["day"], "sp": sp, "kwh": kwh, "g": g})
    trees = {}
    for b, _ in sel:
        trees[(b["key"], b["day"])] = b
        if merkle_root(_leaves(b["doc"])) != from_hex(b["doc"]["root"]):
            return False, f"{b['key']} {b['day']}: python root differs from published root"
    tlist = list(trees.values())
    cp = subprocess.run(["node", "-e", JS_HARNESS], input=json.dumps({"vectors": vectors, "trees": [b["doc"]["leaves"] for b in tlist]}),
                        capture_output=True, text=True)
    if cp.returncode != 0:
        return False, f"node harness failed (rc {cp.returncode}): {cp.stderr.strip()[:400]}"
    out = json.loads(cp.stdout)
    for v, got in zip(vectors, out["leaves"]):
        want = to_hex(leaf_hash(from_hex(v["asset_id"]), v["day"], v["sp"], v["kwh"], v["g"]))
        if got.lower() != want:
            return False, f"JS leaf differs at day {v['day']} sp={v['sp']}: {got} != {want}"
    for b, got in zip(tlist, out["roots"]):
        if got.lower() != b["doc"]["root"].lower():
            return False, f"JS root differs for {b['key']} {b['day']}: {got}"
    return True, f"{len(vectors)} leaves match python, Solidity and JS; {len(tlist)} roots match incl. odd tree of {len(odd[0]['doc']['leaves'])} leaves"


def _c06(x: _Ctx, live: bool):
    b = x.batches[0]
    reason = x.chain.expect_revert(x.chain.issue_batch, x.day_batch(b), from_hex(b["doc"]["model_hash"]))
    return reason == "BATCH_EXISTS", f"re-issue of {b['key']} {b['day']} reverted with {reason}"


def _proof_args(b: dict, i: int):
    d = b["doc"]
    return (from_hex(d["asset_id"]), b["day"], d["sps"][i], d["kwh"][i], d["g"][i], merkle_proof(_leaves(d), i))


def _c07(x: _Ctx, live: bool):
    demo = x.retire_demo()
    ref = demo["ref"]
    b0, i0, _ = demo["picks"][0]
    aid, day, sp, kwh, g, proof = _proof_args(b0, i0)
    again = x.chain.expect_revert(x.chain.retire, aid, day, sp, kwh, g, proof, ref)
    b1, i1, _ = demo["picks"][1]
    aid1, day1, sp1, kwh1, g1, proof1 = _proof_args(b1, i1)
    cap = x.chain.get_asset(aid1)["capacity_kw"]
    forged_g = x.chain.expect_revert(x.chain.retire, aid1, day1, sp1, kwh1, g1 + 1, proof1, ref)
    forged_k = "n/a (kwh+1 would hit the interval cap first)"
    if (kwh1 + 1) * 20000 <= cap * 10200:
        forged_k = x.chain.expect_revert(x.chain.retire, aid1, day1, sp1, kwh1 + 1, g1, proof1, ref)
    stranger = _call_reason(x.contract().functions.retire(aid1, day1, sp1, kwh1, g1, proof1, ref), STRANGER)
    ok = again == "ALREADY_RETIRED" and forged_g == "BAD_PROOF" and forged_k in ("BAD_PROOF", "n/a (kwh+1 would hit the interval cap first)") and stranger == "NOT_HOLDER"
    return ok, f"second retire: {again}; forged g: {forged_g}; forged kWh: {forged_k}; non-holder: {stranger}"


def _c08(x: _Ctx, live: bool):
    b = x.batches[0]
    d = b["doc"]
    aid = from_hex(d["asset_id"])
    cap = x.chain.get_asset(aid)["capacity_kw"]
    k = Web3.keccak(text="gridproof-claims-c08")
    if x.chain.sender is None:
        raise ClaimsError("no signer available to act as owner for the daily-cap call. Fix: set ISSUER_PRIVATE_KEY or use an unlocked node.")
    daily = _call_reason(x.contract().functions.issueBatch(aid, 20991231, k, k, k, cap * 25 + 1, 1, Web3.to_checksum_address(x.chain.sender)), x.chain.sender)
    holder = Web3.to_checksum_address(x.chain.get_batch(aid, b["day"])["holder"])
    over = cap * 10200 // 20000 + 1
    interval = _call_reason(x.contract().functions.retire(aid, b["day"], d["sps"][0], over, d["g"][0], [], beneficiary_ref(BENEFICIARY)), holder)
    return (daily == "EXCEEDS_DAILY_CAPACITY" and interval == "EXCEEDS_INTERVAL_CAPACITY"), f"daily cap {cap * 25 + 1} kWh: {daily}; interval {over} kWh: {interval}"


def _c09(x: _Ctx, live: bool):
    r = x.retire_demo()
    ok = r["delta"] == r["expected"] == r["returned"]
    return ok, f"5 real leaves: on-chain delta {r['delta']} g, python sum {r['expected']} g, receipts {r['returned']} g; retire gas mean {r['gas_mean']}"


def _c10(x: _Ctx, live: bool):
    res = verify_all(x.chain, x.assets, x.s, "snapshot")
    bad = [r for r in res if not r.ok]
    if bad:
        return False, f"snapshot: {len(bad)}/{len(res)} failed; first {bad[0].asset_key} {bad[0].day}: {bad[0].mismatches[:1]}"
    if not live:
        return None, f"snapshot {len(res)}/{len(res)} ok; live half needs network, re-run with --live"
    live_res = verify_all(x.chain, x.assets, x.s, "live")
    lbad = [r for r in live_res if not r.ok]
    drift = sum(len(r.drift) for r in live_res)
    if lbad:
        return False, f"live: {len(lbad)}/{len(live_res)} failed; first {lbad[0].asset_key} {lbad[0].day}: {lbad[0].mismatches[:1]}"
    return True, f"snapshot {len(res)}/{len(res)} and live {len(live_res)}/{len(live_res)} ok; envelope drift flips listed separately: {drift}"


def _c11(x: _Ctx, live: bool):
    s = x.s
    b = next((c for c in x.batches if any(k > 0 for k in c["doc"]["kwh"])), None)
    if b is None:
        return False, "no certified interval with kWh > 0 to tamper with"
    d = b["doc"]
    i = next(j for j, k in enumerate(d["kwh"]) if k > 0)
    sp, old = d["sps"][i], d["kwh"][i]
    new = _flip(old)
    root = Path(s.data_dir).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        for src in {snapshot_path(s, b["key"]).parent, batch_path(s, b["key"], 0).parent, model_path(s, b["key"]).parent}:
            shutil.copytree(src, Path(tmp) / src.resolve().relative_to(root), dirs_exist_ok=True)
        tsnap = Path(tmp) / snapshot_path(s, b["key"]).resolve().relative_to(root)
        doc = json.loads(tsnap.read_text(encoding="utf-8"))
        for row in doc["rows"]:
            if row[0] == b["day"] and row[1] == sp:
                row[3] = new
                break
        else:
            raise ClaimsError(f"row {b['day']} sp={sp} not found in the snapshot copy")
        atomic_write(tsnap, canonical_json(doc) + b"\n")
        res = verify_day(x.chain, b["asset"], b["day"], "snapshot", dataclasses.replace(s, data_dir=tmp))
        net = "testnet" if s.chain_id == SEPOLIA_CHAIN_ID else "localhost"
        cp = subprocess.run([sys.executable, "-m", "gridproof.cli", "verify", "--mode", "snapshot", "--asset", b["key"],
                             "--day", str(b["day"]), "--network", net], env={**os.environ, "DATA_DIR": tmp},
                            capture_output=True, text=True)
    hit = [m for m in res.mismatches if m["sp"] == sp and m["field"] == "kwh"]
    ok = (not res.ok) and bool(hit) and cp.returncode == 2
    return ok, f"tampered {b['key']} day {b['day']} sp={sp} kWh {old}->{new}: verify exit {cp.returncode}, mismatch localised to sp={[m['sp'] for m in res.mismatches[:3]]} fields={[m['field'] for m in res.mismatches[:3]]}"


def _c12(x: _Ctx, live: bool):
    n = 0
    for b in x.batches:
        want = to_hex(sha256(model_path(x.s, b["key"]).read_bytes()))
        on = x.chain.get_batch(from_hex(b["doc"]["asset_id"]), b["day"])
        if on is None:
            return False, f"{b['key']} {b['day']} is not on this chain"
        if b["doc"]["model_hash"].lower() != want or to_hex(on["model_hash"]) != want:
            return False, f"{b['key']} {b['day']}: model hash differs (file {want}, batch {b['doc']['model_hash']}, chain {to_hex(on['model_hash'])})"
        n += 1
    return True, f"{n} batches: batch file and on-chain model hash equal sha256 of data/models/{{key}}.json"


def _c13(x: _Ctx, live: bool):
    c = read_json(x.s, "chain.json")
    g, m = c.get("gas_per_interval"), c.get("gas_issue_mean")
    ok = isinstance(g, (int, float)) and g > 0 and isinstance(m, (int, float)) and m < GAS_ISSUE_MAX
    return ok, f"gas per certified half-hour {g}, mean issue gas {m} (limit {GAS_ISSUE_MAX}), mean retire gas {c.get('gas_retire_mean')}"


def _c14(x: _Ctx, live: bool):
    em = read_json(x.s, "emissions.json")
    on_chain = 0
    for b in x.batches:
        on = x.chain.get_batch(from_hex(b["doc"]["asset_id"]), b["day"])
        if on is None:
            return False, f"{b['key']} {b['day']} is not on this chain"
        on_chain += on["total_kwh"]
    files = sum(int(b["doc"]["total_kwh"]) for b in x.batches)
    ok = em["kwh_certified"] == on_chain == files and em["tco2e_halfhourly"] > 0 and em["tco2e_flat_mean"] > 0
    return ok, f"half-hourly {em['tco2e_halfhourly']} t, flat {em['tco2e_flat_mean']} t, overstatement {em['overstatement_pct']}%; kWh emissions {em['kwh_certified']} chain {on_chain} files {files}"


_FUNCS = {"C01": _c01, "C02": _c02, "C03": _c03, "C04": _c04, "C05": _c05, "C06": _c06, "C07": _c07, "C08": _c08,
          "C09": _c09, "C10": _c10, "C11": _c11, "C12": _c12, "C13": _c13, "C14": _c14}


def run_checks(s: Settings, chain: Chain, live: bool = True) -> list[Check]:
    ctx = _Ctx(s, chain)
    out: list[Check] = []
    for cid, text in CLAIMS:
        try:
            ok, detail = _FUNCS[cid](ctx, live)
            out.append(Check(cid, text, True, detail, True) if ok is None else Check(cid, text, bool(ok), detail))
        except Exception as e:  # reported as a FAIL with the full error text, never hidden
            out.append(Check(cid, text, False, f"{type(e).__name__}: {e}"))
    write_json(s, "claims.json", [dataclasses.asdict(c) for c in out])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gridproof.claims")
    ap.add_argument("--network", choices=["localhost", "testnet"], default="localhost")
    ap.add_argument("--live", action="store_true", help="also run the checks that re-fetch public APIs (C01, live half of C10)")
    args = ap.parse_args(argv)
    s = load_settings(os.environ, args.network)
    chain = Chain.from_settings(s)
    checks = run_checks(s, chain, live=args.live)
    for c in checks:
        print(format_check(c))
    return 1 if any(not c.ok for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
