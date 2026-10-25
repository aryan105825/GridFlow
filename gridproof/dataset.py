# @exports: expected_periods(day: int) -> int   # 48 / 46 / 50 via zoneinfo Europe/London
# @exports: build_intervals(asset: Asset, start: date, end: date, use_cache: bool = True) -> list[Interval]   # sorted (day, sp); start/end inclusive GB settlement dates
# @exports: canonical_json(obj: Any) -> bytes   # sort_keys, separators (",", ":"), utf-8, no NaN
# @exports: sha256(b: bytes) -> bytes
# @exports: day_snapshot_hash(day_intervals: list[Interval]) -> bytes   # sha256(canonical [[sp,kwh,ws100,g],...]) ordered by sp; one asset, one day
# @exports: atomic_write(path: str | Path, data: bytes) -> None
# @exports: write_snapshot(path: str | Path, asset: Asset, intervals: list[Interval], meta: dict) -> None
# @exports: read_snapshot_doc(path: str | Path) -> dict   # validated {"asset_id","bm_unit","meta","rows"}
# @exports: read_snapshot(path: str | Path) -> list[Interval]   # asset_key = file stem
# @imports: gridproof/sources.py:fetch_b1610(bm_unit, start, end, use_cache), fetch_wind100(lat, lon, start, end, offshore, use_cache), fetch_intensity(start, end, use_cache), wind_window(start, end), intensity_window(start, end), SourceError
# @imports: gridproof/schemas.py:Asset, Interval, day_from_date, date_from_day
# @imports: gridproof/merkle.py:to_hex
# @env: none
# @schema: ws100 = round(w[h]*(1-f)+w[h+1]*f, 3) with h = UTC hour of the period start and f = (minute+15)/60; None if either hour is missing. kwh = round(MWh*1000). g = NESO actual at start_utc, None if missing
# @schema: start_utc = halfHourEndTime - 30 min, 'YYYY-MM-DDTHH:MMZ'; it must equal London-midnight(day) + (sp-1)*30 min, otherwise SourceError (catches DST/period mislabelling)
# @schema: snapshot file = {"asset_id": "0x..", "bm_unit": str, "meta": {"sources": [{"url","params"}], "fetched_utc", "weather_model"}, "rows": [[day, sp, start_utc, kwh, ws100, g], ...]} written as canonical JSON plus newline

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from gridproof import sources
from gridproof.merkle import to_hex
from gridproof.schemas import Asset, Interval, date_from_day, day_from_date
from gridproof.sources import SourceError

_UTC = timezone.utc
_LONDON = ZoneInfo("Europe/London")
_TS_FORMAT = "%Y-%m-%dT%H:%MZ"
_HOUR_FORMAT = "%Y-%m-%dT%H:00Z"


def _london_midnight_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=_LONDON).astimezone(_UTC)


def expected_periods(day: int) -> int:
    d = date_from_day(day)
    seconds = (_london_midnight_utc(d + timedelta(days=1)) - _london_midnight_utc(d)).total_seconds()
    n = seconds / 1800
    if n not in (46.0, 48.0, 50.0):
        raise ValueError(f"expected_periods({day}): computed {n} half-hours, which is not 46/48/50; "
                         "check the tzdata package version.")
    return int(n)


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def atomic_write(path: str | Path, data: bytes) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def _parse_utc(text: Any, where: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceError(f"{where}: halfHourEndTime {text!r} is not an ISO timestamp.") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def build_intervals(asset: Asset, start: date, end: date, use_cache: bool = True) -> list[Interval]:
    rows = sources.fetch_b1610(asset.bm_unit, start, end, use_cache=use_cache)
    ws, we = sources.wind_window(start, end)
    wind = sources.fetch_wind100(asset.lat, asset.lon, ws, we, offshore=asset.offshore,
                                 use_cache=use_cache)
    gs, ge = sources.intensity_window(start, end)
    intensity = sources.fetch_intensity(gs, ge, use_cache=use_cache)

    out: list[Interval] = []
    for r in rows:
        where = f"{asset.key} {r['settlementDate']} sp {r['settlementPeriod']}"
        day = day_from_date(date.fromisoformat(r["settlementDate"]))
        sp = int(r["settlementPeriod"])
        n = expected_periods(day)
        if not 1 <= sp <= n:
            raise SourceError(f"{where}: settlement period {sp} is outside 1..{n} for that day.")
        start_t = _parse_utc(r["halfHourEndTime"], where) - timedelta(minutes=30)
        want = _london_midnight_utc(date_from_day(day)) + timedelta(minutes=30 * (sp - 1))
        if start_t != want:
            raise SourceError(f"{where}: halfHourEndTime {r['halfHourEndTime']} implies period start "
                              f"{start_t.strftime(_TS_FORMAT)} but GB settlement date + period imply "
                              f"{want.strftime(_TS_FORMAT)}. Check whether halfHourEndTime is the period "
                              "END in the probe payload; fix the parser, do not patch the data.")
        start_utc = start_t.strftime(_TS_FORMAT)
        hour0 = start_t.replace(minute=0)
        f = (start_t.minute + 15) / 60
        w0 = wind.get(hour0.strftime(_HOUR_FORMAT))
        w1 = wind.get((hour0 + timedelta(hours=1)).strftime(_HOUR_FORMAT))
        ws100 = None if (w0 is None or w1 is None) else round(w0 * (1 - f) + w1 * f, 3)
        g = intensity.get(start_utc)
        qty = r["quantity"]
        if not math.isfinite(qty):
            raise SourceError(f"{where}: quantity {qty} is not finite.")
        out.append(Interval(asset_key=asset.key, day=day, sp=sp, start_utc=start_utc,
                            kwh=int(round(qty * 1000)), ws100=ws100, g_per_kwh=g))
    out.sort(key=lambda i: (i.day, i.sp))
    for a, b in zip(out, out[1:]):
        if (a.day, a.sp) == (b.day, b.sp):
            raise SourceError(f"{asset.key}: duplicate interval day={a.day} sp={a.sp}.")
    return out


def day_snapshot_hash(day_intervals: list[Interval]) -> bytes:
    if not day_intervals:
        raise ValueError("day_snapshot_hash: empty interval list")
    keys = {(i.asset_key, i.day) for i in day_intervals}
    if len(keys) != 1:
        raise ValueError(f"day_snapshot_hash: intervals must belong to one asset and one day, got {sorted(keys)}")
    rows = [[i.sp, i.kwh, i.ws100, i.g_per_kwh] for i in sorted(day_intervals, key=lambda i: i.sp)]
    return sha256(canonical_json(rows))


def write_snapshot(path: str | Path, asset: Asset, intervals: list[Interval], meta: dict) -> None:
    for i in intervals:
        if i.asset_key != asset.key:
            raise ValueError(f"write_snapshot: interval for {i.asset_key} given with asset {asset.key}")
    doc = {
        "asset_id": to_hex(asset.asset_id),
        "bm_unit": asset.bm_unit,
        "meta": meta,
        "rows": [[i.day, i.sp, i.start_utc, i.kwh, i.ws100, i.g_per_kwh] for i in intervals],
    }
    atomic_write(path, canonical_json(doc) + b"\n")


def read_snapshot_doc(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SourceError(f"Snapshot {p} not found. Run `python -m gridproof.cli fetch` first.")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SourceError(f"Snapshot {p} is not valid JSON ({exc}). Re-run fetch or restore from git.") from exc
    for field in ("asset_id", "bm_unit", "meta", "rows"):
        if not isinstance(doc, dict) or field not in doc:
            raise SourceError(f"Snapshot {p} is missing field '{field}'. Re-run fetch or restore from git.")
    for n, row in enumerate(doc["rows"]):
        if not isinstance(row, list) or len(row) != 6:
            raise SourceError(f"Snapshot {p} row {n} must be [day, sp, start_utc, kwh, ws100, g], got {row!r}.")
    return doc


def read_snapshot(path: str | Path) -> list[Interval]:
    doc = read_snapshot_doc(path)
    key = Path(path).stem
    return [Interval(asset_key=key, day=int(d), sp=int(sp), start_utc=str(t), kwh=int(k),
                     ws100=None if w is None else float(w), g_per_kwh=None if g is None else int(g))
            for d, sp, t, k, w, g in doc["rows"]]
