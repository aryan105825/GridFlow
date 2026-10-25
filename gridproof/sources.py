# @exports: SourceError(RuntimeError) with .status: int | None
# @exports: set_cache_dir(path: str | Path) -> None   # default data/cache; pipeline.run_fetch sets <DATA_DIR>/cache
# @exports: get_json(url: str, params: dict | None = None, *, use_cache: bool = True, retries: int = 4, timeout: int = 60) -> Any
# @exports: fetch_bmu_reference(use_cache: bool = True) -> list[dict]
# @exports: fetch_b1610(bm_unit: str, start: date, end: date, use_cache: bool = True) -> list[{"settlementDate": "YYYY-MM-DD", "settlementPeriod": int, "halfHourEndTime": str, "quantity": float (MWh), "bmUnit": str}]   # start/end are inclusive GB settlement dates; sorted by (date, sp)
# @exports: fetch_wind100(lat: float, lon: float, start: date, end: date, offshore: bool = True, use_cache: bool = True) -> dict[str, float]   # 'YYYY-MM-DDTHH:00Z' -> m/s; requests end+1 day; null hours are omitted
# @exports: fetch_intensity(start: datetime, end: datetime, use_cache: bool = True) -> dict[str, int]   # 'YYYY-MM-DDTHH:MMZ' -> actual gCO2/kWh for start <= t < end; null actuals are omitted
# @exports: wind_window(start: date, end: date) -> tuple[date, date]   # the wind request range dataset.build_intervals uses
# @exports: intensity_window(start: date, end: date) -> tuple[datetime, datetime]   # the intensity request range dataset.build_intervals uses (UTC, end exclusive)
# @exports: weather_model() -> str   # "era5" or a note that models=era5 was rejected and the API default was used
# @exports: source_descriptors(asset: Asset, start: date, end: date) -> list[{"url": str, "params": dict}]   # for snapshot meta.sources
# @imports: gridproof/schemas.py:Asset
# @env: none
# @schema: B1610 request params are the constants B1610_PARAM_FROM / B1610_PARAM_TO / B1610_PARAM_BMU / B1610_DATE_FORMAT below; confirm them against data/probe/b1610_params.txt (scripts/probe.sh) and edit the constants if the probe shows different names
# @schema: B1610 is requested in chunks of B1610_CHUNK_DAYS days; `to` is requested one day past the chunk and rows are filtered back to the chunk, so both inclusive and exclusive `to` semantics work
# @schema: required fields asserted loudly: B1610 bmUnit/settlementDate/settlementPeriod/halfHourEndTime/quantity; Open-Meteo hourly.time/hourly.wind_speed_100m; NESO data[].from, data[].intensity.actual
# @schema: cache = <cache_dir>/<sha256(url+sorted params)>.json; use_cache=False neither reads nor writes the cache

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from gridproof.schemas import Asset

BMRS_BASE = "https://data.elexon.co.uk/bmrs/api/v1"
B1610_URL = f"{BMRS_BASE}/datasets/B1610/stream"
BMU_URL = f"{BMRS_BASE}/reference/bmunits/all"
OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
NESO_URL = "https://api.carbonintensity.org.uk/intensity"

# Confirm in milestone M0 against data/probe/b1610_params.txt.
B1610_PARAM_FROM = "from"
B1610_PARAM_TO = "to"
B1610_PARAM_BMU = "bmUnit"
B1610_DATE_FORMAT = "%Y-%m-%d"
B1610_CHUNK_DAYS = 5  # one day shorter: `from` is padded back one day (BST: sp 1-2 of a day end before 00:00Z), request span unchanged
INTENSITY_CHUNK_DAYS = 13
WIND_PAD_BEFORE_DAYS = 1

_UTC = timezone.utc
_TS_FORMAT = "%Y-%m-%dT%H:%MZ"

_cache_dir: Path = Path("data") / "cache"
_wind_model_param: str | None = "era5"
_wind_model_note: str = "era5"


class SourceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def set_cache_dir(path: str | Path) -> None:
    global _cache_dir
    _cache_dir = Path(path)


def _cache_path(url: str, params: dict | None) -> Path:
    key = json.dumps({"url": url, "params": params or {}}, sort_keys=True, default=str)
    return _cache_dir / (hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json")


def get_json(url: str, params: dict | None = None, *, use_cache: bool = True,
             retries: int = 4, timeout: int = 60) -> Any:
    if retries < 1:
        raise ValueError("get_json: retries must be >= 1")
    path = _cache_path(url, params)
    if use_cache and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SourceError(f"Corrupt cache file {path} ({exc}). Delete it and re-run.") from exc

    last: SourceError | None = None
    for attempt in range(retries):
        if attempt:
            time.sleep(2 ** attempt)  # 2s, 4s, 8s: deterministic back-off
        try:
            resp = requests.get(url, params=params, timeout=timeout,
                                headers={"Accept": "application/json"})
        except requests.RequestException as exc:
            last = SourceError(f"GET {url} failed ({type(exc).__name__}: {exc}). "
                               "Check your internet connection and proxy settings, then re-run.")
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last = SourceError(f"GET {resp.url} returned HTTP {resp.status_code}: {resp.text[:200]!r}. "
                               "The API is rate-limiting or down; wait a minute and re-run "
                               "(already cached responses are reused).", status=resp.status_code)
            continue
        if resp.status_code >= 400:
            raise SourceError(f"GET {resp.url} returned HTTP {resp.status_code}: {resp.text[:300]!r}. "
                              "Compare the request parameter names with the payloads saved by "
                              "scripts/probe.sh in data/probe/ and fix the constants in gridproof/sources.py.",
                              status=resp.status_code)
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError(f"GET {resp.url} returned HTTP {resp.status_code} but the body is not JSON "
                              f"(starts with {resp.text[:120]!r}).") from exc
        if use_cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, path)
        return data
    assert last is not None
    raise last


def _require(obj: Any, field: str, where: str) -> Any:
    if not isinstance(obj, dict):
        raise SourceError(f"{where}: expected a JSON object holding '{field}' but got {type(obj).__name__}. "
                          "The API schema may have changed; compare with data/probe/*.json.")
    if field not in obj:
        raise SourceError(f"{where}: required field '{field}' is missing (fields present: {sorted(obj)[:15]}). "
                          "The API schema may have changed; compare with data/probe/*.json and update the parser.")
    return obj[field]


def _rows(data: Any, where: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    raise SourceError(f"{where}: expected a JSON array (or an object with a 'data' array) but got "
                      f"{type(data).__name__}. Compare with data/probe/*.json.")


def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def fetch_bmu_reference(use_cache: bool = True) -> list[dict]:
    data = get_json(BMU_URL, use_cache=use_cache)
    rows = _rows(data, "BMU reference")
    if not rows:
        raise SourceError("BMU reference returned zero rows. Check data/probe/bmu_reference.json.")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SourceError(f"BMU reference row {i} is {type(row).__name__}, expected an object.")
    return rows


def fetch_b1610(bm_unit: str, start: date, end: date, use_cache: bool = True) -> list[dict]:
    if end < start:
        raise ValueError(f"fetch_b1610: end {end} is before start {start}")
    seen: dict[tuple[str, int], dict] = {}
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=B1610_CHUNK_DAYS - 1), end)
        params = {
            B1610_PARAM_FROM: (cur - timedelta(days=1)).strftime(B1610_DATE_FORMAT),
            B1610_PARAM_TO: (chunk_end + timedelta(days=1)).strftime(B1610_DATE_FORMAT),
            B1610_PARAM_BMU: bm_unit,
        }
        where = f"B1610 {bm_unit} {cur}..{chunk_end}"
        rows = _rows(get_json(B1610_URL, params, use_cache=use_cache), where)
        for raw in rows:
            unit = _require(raw, "bmUnit", where)
            if unit != bm_unit:
                continue
            sdate = str(_require(raw, "settlementDate", where))[:10]
            sp = _require(raw, "settlementPeriod", where)
            hh_end = _require(raw, "halfHourEndTime", where)
            qty = _require(raw, "quantity", where)
            try:
                d = date.fromisoformat(sdate)
            except ValueError as exc:
                raise SourceError(f"{where}: settlementDate {sdate!r} is not YYYY-MM-DD.") from exc
            if not (cur <= d <= chunk_end):
                continue
            if not _is_number(sp) or int(sp) != sp:
                raise SourceError(f"{where}: settlementPeriod {sp!r} on {sdate} is not an integer.")
            if not _is_number(qty) or not math.isfinite(qty):
                raise SourceError(f"{where}: quantity {qty!r} for {sdate} period {sp} is not a finite number "
                                  "(missing meter value). Inspect data/probe/b1610.json.")
            row = {"settlementDate": sdate, "settlementPeriod": int(sp),
                   "halfHourEndTime": str(hh_end), "quantity": float(qty), "bmUnit": bm_unit}
            key = (sdate, int(sp))
            prev = seen.get(key)
            if prev is not None and (prev["quantity"] != row["quantity"]
                                     or prev["halfHourEndTime"] != row["halfHourEndTime"]):
                raise SourceError(f"{where}: conflicting duplicate rows for {sdate} period {sp} "
                                  f"({prev['quantity']} vs {row['quantity']} MWh). B1610 returned several "
                                  "settlement runs; restrict the request to the final run.")
            seen[key] = row
        cur = chunk_end + timedelta(days=1)
    if not seen:
        raise SourceError(f"B1610 returned no rows for bmUnit {bm_unit!r} between {start} and {end}. "
                          "Check the bm_unit in assets.yaml and the parameter constants in gridproof/sources.py.")
    return [seen[k] for k in sorted(seen)]


def _wind_params(lat: float, lon: float, start: date, end: date, offshore: bool,
                 model: str | None) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": (end + timedelta(days=1)).isoformat(),
        "hourly": "wind_speed_100m",
        "wind_speed_unit": "ms",
        "timezone": "GMT",
        "cell_selection": "sea" if offshore else "land",
    }
    if model:
        params["models"] = model
    return params


def fetch_wind100(lat: float, lon: float, start: date, end: date, offshore: bool = True,
                  use_cache: bool = True) -> dict[str, float]:
    global _wind_model_param, _wind_model_note
    try:
        data = get_json(OPEN_METEO_URL, _wind_params(lat, lon, start, end, offshore, "era5"),
                        use_cache=use_cache)
        _wind_model_param, _wind_model_note = "era5", "era5"
    except SourceError as exc:
        if exc.status != 400:
            raise
        data = get_json(OPEN_METEO_URL, _wind_params(lat, lon, start, end, offshore, None),
                        use_cache=use_cache)
        _wind_model_param = None
        _wind_model_note = f"open-meteo default model (models=era5 was rejected with HTTP 400: {exc})"
    where = f"Open-Meteo wind {lat},{lon} {start}..{end}"
    hourly = _require(data, "hourly", where)
    times = _require(hourly, "time", where + " hourly")
    vals = _require(hourly, "wind_speed_100m", where + " hourly")
    if not isinstance(times, list) or not isinstance(vals, list) or len(times) != len(vals):
        raise SourceError(f"{where}: hourly.time and hourly.wind_speed_100m must be arrays of equal length "
                          f"(got {len(times) if isinstance(times, list) else type(times).__name__} vs "
                          f"{len(vals) if isinstance(vals, list) else type(vals).__name__}).")
    out: dict[str, float] = {}
    for t, v in zip(times, vals):
        stamp = str(t)[:16]
        if len(stamp) != 16 or not stamp.endswith(":00"):
            raise SourceError(f"{where}: unexpected hourly timestamp {t!r}; expected YYYY-MM-DDTHH:00 in GMT.")
        if v is None:
            continue
        if not _is_number(v) or not math.isfinite(v):
            raise SourceError(f"{where}: wind value {v!r} at {t} is not a number.")
        out[stamp + "Z"] = float(v)
    if not out:
        raise SourceError(f"{where}: no wind values returned. Check lat/lon in assets.yaml.")
    return out


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_UTC)
    return dt.astimezone(_UTC)


def fetch_intensity(start: datetime, end: datetime, use_cache: bool = True) -> dict[str, int]:
    s, e = _to_utc(start), _to_utc(end)
    if e <= s:
        raise ValueError(f"fetch_intensity: end {e} must be after start {s}")
    out: dict[str, int] = {}
    cur = s
    while cur < e:
        chunk_end = min(cur + timedelta(days=INTENSITY_CHUNK_DAYS), e)
        url = f"{NESO_URL}/{cur.strftime(_TS_FORMAT)}/{chunk_end.strftime(_TS_FORMAT)}"
        where = f"NESO intensity {cur.strftime(_TS_FORMAT)}..{chunk_end.strftime(_TS_FORMAT)}"
        rows = _rows(get_json(url, use_cache=use_cache), where)
        for row in rows:
            frm = _require(row, "from", where)
            inten = _require(row, "intensity", where)
            actual = _require(inten, "actual", where + " intensity")
            if actual is None:
                continue
            if not _is_number(actual) or not math.isfinite(actual):
                raise SourceError(f"{where}: intensity.actual {actual!r} at {frm} is not a number.")
            try:
                stamp = _to_utc(datetime.fromisoformat(str(frm).replace("Z", "+00:00")))
            except ValueError as exc:
                raise SourceError(f"{where}: 'from' value {frm!r} is not an ISO timestamp.") from exc
            if s <= stamp < e:
                out[stamp.strftime(_TS_FORMAT)] = int(round(actual))
        cur = chunk_end
    return {k: out[k] for k in sorted(out)}


def wind_window(start: date, end: date) -> tuple[date, date]:
    return start - timedelta(days=WIND_PAD_BEFORE_DAYS), end


def intensity_window(start: date, end: date) -> tuple[datetime, datetime]:
    s = datetime(start.year, start.month, start.day, tzinfo=_UTC) - timedelta(days=1)
    e = datetime(end.year, end.month, end.day, tzinfo=_UTC) + timedelta(days=1)
    return s, e


def weather_model() -> str:
    return _wind_model_note


def source_descriptors(asset: Asset, start: date, end: date) -> list[dict]:
    ws, we = wind_window(start, end)
    gs, ge = intensity_window(start, end)
    return [
        {"url": B1610_URL,
         "params": {B1610_PARAM_FROM: start.strftime(B1610_DATE_FORMAT),
                    B1610_PARAM_TO: end.strftime(B1610_DATE_FORMAT),
                    B1610_PARAM_BMU: asset.bm_unit}},
        {"url": OPEN_METEO_URL,
         "params": _wind_params(asset.lat, asset.lon, ws, we, asset.offshore, _wind_model_param)},
        {"url": f"{NESO_URL}/{gs.strftime(_TS_FORMAT)}/{ge.strftime(_TS_FORMAT)}", "params": {}},
    ]
