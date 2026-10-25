# @exports: judge(intervals: list[Interval], capacity_kw: int, upper_fn: Callable[[float], float] | None = None, capacity_tol: float = 0.02, frozen_run: int = 6) -> list[Verdict]   # one Verdict per input interval, same order; intervals must be ONE asset, sorted by (day, sp)
# @exports: summary(verdicts: list[Verdict]) -> {"n": int, "accepted": int, "rejected": int, "rejected_rate": float, "by_reason": {Reason: int}, "accepted_kwh": int, "rejected_kwh": int}
# @imports: gridproof/schemas.py:Interval, Verdict, REASONS
# @env: none
# @schema: rule order per interval: NEG_OR_NAN, MISSING_DATA (ws100 or g_per_kwh is None), OVER_CAPACITY (kwh > capacity_kw*0.5*(1+tol), exact rational arithmetic), ABOVE_ENVELOPE (cf > upper_fn(ws100)), then FROZEN_METER (>= frozen_run consecutive 30-minute intervals with identical kwh > 0) applied to otherwise-accepted intervals
# @schema: cf = kwh / (capacity_kw * 0.5); 0.0 when kwh is not a finite number. upper_fn is duck-typed: any callable ws100 -> upper capacity factor; None disables the envelope rule
# @schema: "MISSING_DATA" covers missing inputs on a present interval; settlement periods absent from B1610 altogether never reach the oracle (counted as missing by claims C02 via expected_periods)
from __future__ import annotations

import math
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Callable, Optional, Sequence

from gridproof.schemas import REASONS, Interval, Verdict

_TIME_FMT = "%Y-%m-%dT%H:%MZ"
_STEP = timedelta(minutes=30)


def _start_time(iv: Interval) -> datetime:
    try:
        return datetime.strptime(iv.start_utc, _TIME_FMT)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"interval {iv.asset_key} day={iv.day} sp={iv.sp} has start_utc={iv.start_utc!r}; expected YYYY-MM-DDTHH:MMZ"
        ) from exc


def _finite_nonneg(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def judge(
    intervals: Sequence[Interval],
    capacity_kw: int,
    upper_fn: Optional[Callable[[float], float]] = None,
    capacity_tol: float = 0.02,
    frozen_run: int = 6,
) -> list[Verdict]:
    if capacity_kw <= 0:
        raise ValueError(f"capacity_kw must be positive, got {capacity_kw}")
    if capacity_tol < 0:
        raise ValueError(f"capacity_tol must be >= 0, got {capacity_tol}")
    if frozen_run < 2:
        raise ValueError(f"frozen_run must be >= 2, got {frozen_run}")

    # Exact rational limit: kwh * 20000 > capacity_kw * 10200 on-chain is the same rule for tol = 0.02.
    limit = Fraction(capacity_kw, 2) * (1 + Fraction(str(capacity_tol)))
    half_hour_capacity = capacity_kw * 0.5

    n = len(intervals)
    reasons: list[str] = []
    cfs: list[float] = []
    uppers: list[Optional[float]] = []
    valid = [False] * n
    starts = [_start_time(iv) for iv in intervals]

    for i, iv in enumerate(intervals):
        kwh = iv.kwh
        valid[i] = _finite_nonneg(kwh)
        finite = isinstance(kwh, (int, float)) and not isinstance(kwh, bool) and math.isfinite(kwh)
        cf = kwh / half_hour_capacity if finite else 0.0
        ws = iv.ws100
        has_ws = ws is not None and isinstance(ws, (int, float)) and math.isfinite(ws)
        upper = float(upper_fn(ws)) if (upper_fn is not None and has_ws) else None

        if not valid[i]:
            reason = "NEG_OR_NAN"
        elif not has_ws or iv.g_per_kwh is None:
            reason = "MISSING_DATA"
        elif Fraction(kwh) > limit:
            reason = "OVER_CAPACITY"
        elif upper is not None and cf > upper:
            reason = "ABOVE_ENVELOPE"
        else:
            reason = "OK"
        reasons.append(reason)
        cfs.append(cf)
        uppers.append(upper)

    frozen: set[int] = set()
    run: list[int] = []
    for i, iv in enumerate(intervals):
        usable = valid[i] and iv.kwh > 0
        if usable and run and iv.kwh == intervals[run[-1]].kwh and starts[i] - starts[run[-1]] == _STEP:
            run.append(i)
            continue
        if len(run) >= frozen_run:
            frozen.update(run)
        run = [i] if usable else []
    if len(run) >= frozen_run:
        frozen.update(run)

    verdicts: list[Verdict] = []
    for i, iv in enumerate(intervals):
        reason = reasons[i]
        if reason == "OK" and i in frozen:
            reason = "FROZEN_METER"
        verdicts.append(
            Verdict(interval=iv, accepted=(reason == "OK"), reason=reason, cf=cfs[i], upper_cf=uppers[i])  # type: ignore[arg-type]
        )
    return verdicts


def summary(verdicts: Sequence[Verdict]) -> dict:
    by_reason = {r: 0 for r in REASONS}
    accepted_kwh = 0
    rejected_kwh = 0
    accepted = 0
    for v in verdicts:
        by_reason[v.reason] += 1
        kwh = v.interval.kwh
        counted = int(kwh) if _finite_nonneg(kwh) else 0
        if v.accepted:
            accepted += 1
            accepted_kwh += counted
        else:
            rejected_kwh += counted
    n = len(verdicts)
    return {
        "n": n,
        "accepted": accepted,
        "rejected": n - accepted,
        "rejected_rate": ((n - accepted) / n) if n else 0.0,
        "by_reason": by_reason,
        "accepted_kwh": accepted_kwh,
        "rejected_kwh": rejected_kwh,
    }
