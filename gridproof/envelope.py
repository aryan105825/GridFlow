# @exports: EnvelopeError(RuntimeError)
# @exports: Envelope(asset_key: str, ws_edges: list[float], upper_cf: list[float], margin: float, quantile: float, train_range: list[int], n_train: int)   # frozen dataclass; ws_edges are LOWER bin edges, the last bin is open-ended
# @exports: fit_envelope(train: list[Interval], capacity_kw: int, quantile: float = 0.995, bin_width: float = 0.5, margin: float = 0.05, min_bin_n: int = 25, min_months: int = 11, holdout_days: int = 14, target_false_flag: float = 0.01) -> Envelope   # `margin` is the floor of the margin search
# @exports: upper_cf(env: Envelope, ws: float) -> float
# @exports: to_json(env: Envelope) -> dict ; from_json(d: dict) -> Envelope ; save(env: Envelope, path: str | Path) -> None ; load(path: str | Path) -> Envelope
# @exports: model_hash(env: Envelope) -> bytes   # sha256(canonical_json(to_json(env))); the on-chain modelHash is the sha256 of the data/models/{key}.json FILE bytes (envelope JSON + "rules"), see pipeline.py
# @exports: blind_regions(env: Envelope, train: list[Interval], capacity_kw: int) -> list[{"ws_bin": str, "headroom_kwh_per_period": float, "headroom_pct_capacity": float, "n": int}]   # only bins with training samples; capacity_kw added vs CONTRACTS because Envelope does not store it
# @imports: gridproof/schemas.py:Interval
# @imports: gridproof/dataset.py:canonical_json(obj), sha256(b), atomic_write(path, data)
# @env: none
# @schema: Envelope JSON = {"asset_key","ws_edges","upper_cf","margin","quantile","train_range":[first_day,last_day],"n_train"}; floats rounded to 6 places so the file is byte-stable
# @schema: bin i covers ws in [ws_edges[i], ws_edges[i+1]); the last bin covers every ws >= ws_edges[-1]; ws below ws_edges[0] uses bin 0; upper_cf has one value per bin, non-decreasing, within [0,1]
# @schema: per bin: base = quantile of cf = kwh / (capacity_kw * 0.5). A bin with fewer than min_bin_n samples inherits the bin below it; sparse bins at the LOW end (calm wind is rare offshore) take the value of the lowest well-populated bin above them (conservative, still monotone). Then np.maximum.accumulate, + margin, clip [0,1]
# @schema: Margin is chosen only on the last `holdout_days` TRAINING days (fit on earlier training days, smallest margin >= floor in 0.005 steps with holdout false-flag rate <= target_false_flag), then the envelope is refit on all training data. The issue month is never touched

from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from gridproof.dataset import atomic_write, canonical_json, sha256
from gridproof.schemas import Interval

MARGIN_STEP = 0.005
ROUND = 6


class EnvelopeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Envelope:
    asset_key: str
    ws_edges: list
    upper_cf: list
    margin: float
    quantile: float
    train_range: list
    n_train: int


def _usable(train: list[Interval]) -> list[Interval]:
    return [i for i in train if i.ws100 is not None]


def _bin_index(edges: np.ndarray, ws: np.ndarray) -> np.ndarray:
    return np.maximum(np.searchsorted(edges, ws, side="right") - 1, 0)


def _base_upper(ws: np.ndarray, cf: np.ndarray, edges: np.ndarray, quantile: float, min_bin_n: int) -> np.ndarray:
    idx = _bin_index(edges, ws)
    nb = len(edges)
    base = np.full(nb, np.nan)
    for b in range(nb):
        sel = cf[idx == b]
        if len(sel) >= min_bin_n:
            base[b] = float(np.quantile(sel, quantile))
    good = np.flatnonzero(~np.isnan(base))
    if len(good) == 0:
        raise EnvelopeError(f"no wind bin has {min_bin_n} samples or more. Fix: fetch more training data or lower min_bin_n.")
    first = good[0]
    base[:first] = base[first]
    for b in range(first + 1, nb):
        if np.isnan(base[b]):
            base[b] = base[b - 1]
    return np.maximum.accumulate(base)


def _finish(base: np.ndarray, margin: float) -> np.ndarray:
    return np.clip(base + margin, 0.0, 1.0)


def _flag_rate(upper: np.ndarray, edges: np.ndarray, ws: np.ndarray, cf: np.ndarray) -> float:
    return float(np.mean(cf > upper[_bin_index(edges, ws)]))


def fit_envelope(train: list[Interval], capacity_kw: int, quantile: float = 0.995, bin_width: float = 0.5,
                 margin: float = 0.05, min_bin_n: int = 25, min_months: int = 11, holdout_days: int = 14,
                 target_false_flag: float = 0.01) -> Envelope:
    use = _usable(train)
    if not use:
        raise EnvelopeError("no training intervals with wind data. Fix: re-run `fetch`.")
    keys = {i.asset_key for i in use}
    if len(keys) != 1:
        raise EnvelopeError(f"training data must hold exactly one asset, found {sorted(keys)}.")
    months = {(i.day // 10000, (i.day // 100) % 100) for i in use}
    if len(months) < min_months:
        raise EnvelopeError(f"{use[0].asset_key}: only {len(months)} months of training data (< {min_months}). Fix: widen TRAIN_START/TRAIN_END.")
    if capacity_kw <= 0:
        raise EnvelopeError("capacity_kw must be positive.")
    half = capacity_kw * 0.5
    ws = np.array([i.ws100 for i in use], dtype=float)
    cf = np.array([i.kwh / half for i in use], dtype=float)
    days = np.array([i.day for i in use])
    nbins = int(ws.max() // bin_width) + 1
    edges = np.array([round(b * bin_width, ROUND) for b in range(nbins)])

    udays = sorted(set(days.tolist()))
    if len(udays) <= holdout_days:
        raise EnvelopeError(f"need more than {holdout_days} training days for the margin holdout, have {len(udays)}.")
    hold = np.isin(days, udays[-holdout_days:])
    base_fit = _base_upper(ws[~hold], cf[~hold], edges, quantile, min_bin_n)
    chosen = None
    m = float(margin)
    while m <= 1.0 + 1e-9:
        if _flag_rate(_finish(base_fit, m), edges, ws[hold], cf[hold]) <= target_false_flag:
            chosen = round(m, ROUND)
            break
        m += MARGIN_STEP
    if chosen is None:
        raise EnvelopeError(f"{use[0].asset_key}: holdout false-flag rate cannot reach {target_false_flag} even at margin 1.0. "
                            "Fix: inspect the training data for over-capacity readings.")
    upper = _finish(_base_upper(ws, cf, edges, quantile, min_bin_n), chosen)
    return Envelope(asset_key=use[0].asset_key, ws_edges=[float(e) for e in edges],
                    upper_cf=[round(float(u), ROUND) for u in upper], margin=chosen, quantile=quantile,
                    train_range=[int(days.min()), int(days.max())], n_train=len(use))


def upper_cf(env: Envelope, ws: float) -> float:
    i = max(bisect.bisect_right(env.ws_edges, ws) - 1, 0)
    return env.upper_cf[i]


def to_json(env: Envelope) -> dict:
    return {"asset_key": env.asset_key, "ws_edges": [round(float(e), ROUND) for e in env.ws_edges],
            "upper_cf": [round(float(u), ROUND) for u in env.upper_cf], "margin": round(float(env.margin), ROUND),
            "quantile": round(float(env.quantile), ROUND), "train_range": [int(d) for d in env.train_range],
            "n_train": int(env.n_train)}


def from_json(d: dict) -> Envelope:
    need = ("asset_key", "ws_edges", "upper_cf", "margin", "quantile", "train_range", "n_train")
    missing = [k for k in need if k not in d]
    if missing:
        raise EnvelopeError(f"envelope JSON lacks fields {missing}.")
    if len(d["ws_edges"]) != len(d["upper_cf"]) or not d["ws_edges"]:
        raise EnvelopeError("envelope JSON has mismatched or empty ws_edges / upper_cf.")
    return Envelope(asset_key=d["asset_key"], ws_edges=list(d["ws_edges"]), upper_cf=list(d["upper_cf"]),
                    margin=float(d["margin"]), quantile=float(d["quantile"]),
                    train_range=[int(x) for x in d["train_range"]], n_train=int(d["n_train"]))


def save(env: Envelope, path: str | Path) -> None:
    atomic_write(path, canonical_json(to_json(env)) + b"\n")


def load(path: str | Path) -> Envelope:
    import json

    p = Path(path)
    if not p.exists():
        raise EnvelopeError(f"{p} is missing. Fix: run `python -m gridproof.cli train`.")
    return from_json(json.loads(p.read_text(encoding="utf-8")))


def model_hash(env: Envelope) -> bytes:
    return sha256(canonical_json(to_json(env)))


def blind_regions(env: Envelope, train: list[Interval], capacity_kw: int) -> list[dict]:
    use = _usable(train)
    if not use:
        raise EnvelopeError("no training intervals with wind data for blind_regions.")
    half = capacity_kw * 0.5
    edges = np.array(env.ws_edges)
    ws = np.array([i.ws100 for i in use], dtype=float)
    cf = np.array([i.kwh / half for i in use], dtype=float)
    idx = _bin_index(edges, ws)
    out = []
    for b in range(len(edges)):
        sel = cf[idx == b]
        if len(sel) == 0:
            continue
        head = max(env.upper_cf[b] - float(np.median(sel)), 0.0)
        label = f"{edges[b]:.1f}-{edges[b + 1]:.1f}" if b + 1 < len(edges) else f"{edges[b]:.1f}+"
        out.append({"ws_bin": label, "headroom_kwh_per_period": round(head * half, 3),
                    "headroom_pct_capacity": round(head * 100, 3), "n": int(len(sel))})
    return out
