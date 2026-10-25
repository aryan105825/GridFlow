#!/usr/bin/env bash
# @exports: none (executable script, no arguments)
# @imports: none
# @env: none
# @schema: CLI `bash scripts/probe.sh` -> writes data/probe/{b1610,bmu_reference,open_meteo,neso}.json and data/probe/b1610_params.txt; prints the head of each payload; exit 0 = all four sources returned data, 1 = any failure
set -u

OUT="data/probe"
UA="gridproof-probe/1.0"
FAILS=0
mkdir -p "$OUT"

# Fixed probe window: the DST day (46 periods) and the day after.
DAY_FROM="2025-03-30"
DAY_TO="2025-03-31"

# One site per source; must match assets.yaml (first asset).
BMU="T_DDGNO-1"
LAT="53.25"
LON="1.38"

B1610_URL="https://data.elexon.co.uk/bmrs/api/v1/datasets/B1610/stream"
BMU_REF_URL="https://data.elexon.co.uk/bmrs/api/v1/reference/bmunits/all"
METEO_URL="https://archive-api.open-meteo.com/v1/archive"
NESO_URL="https://api.carbonintensity.org.uk/intensity/${DAY_FROM}T00:00Z/${DAY_TO}T00:00Z"

# get <name> <url> [curl args]: GET with -G so --data-urlencode becomes query params.
# Saves body to $OUT/<name>.json and the curl error text to $OUT/<name>.err. Prints the HTTP code ("000" if curl failed).
get() {
  local name="$1" url="$2"
  shift 2
  local code
  code=$(curl -sS -G -A "$UA" --max-time 120 -o "$OUT/$name.json" -w '%{http_code}' "$url" "$@" 2>"$OUT/$name.err")
  if [ -z "$code" ]; then code="000"; fi
  echo "$code"
}

body_ok() {
  # body present and bigger than an empty JSON array/object
  [ -f "$1" ] && [ "$(wc -c < "$1")" -gt 20 ]
}

fail() {
  echo "FAIL $1" >&2
  FAILS=$((FAILS + 1))
}

# ---------------------------------------------------------------- B1610 (try parameter variants)
: > "$OUT/b1610_params.txt"
WINNER=""
try_b1610() {
  local label="$1"
  shift
  local code
  code=$(get "b1610_try" "$B1610_URL" "$@")
  if [ "$code" = "200" ] && body_ok "$OUT/b1610_try.json"; then
    echo "OK   variant=$label http=$code bytes=$(wc -c < "$OUT/b1610_try.json")" >> "$OUT/b1610_params.txt"
    if [ -z "$WINNER" ]; then
      WINNER="$label"
      cp "$OUT/b1610_try.json" "$OUT/b1610.json"
    fi
  else
    echo "FAIL variant=$label http=$code bytes=$(wc -c < "$OUT/b1610_try.json" 2>/dev/null || echo 0)" >> "$OUT/b1610_params.txt"
  fi
}

try_b1610 "A: from/to ISO datetime + bmUnit" \
  --data-urlencode "from=${DAY_FROM}T00:00Z" --data-urlencode "to=${DAY_TO}T00:00Z" --data-urlencode "bmUnit=${BMU}"
try_b1610 "B: from/to date only + bmUnit" \
  --data-urlencode "from=${DAY_FROM}" --data-urlencode "to=${DAY_TO}" --data-urlencode "bmUnit=${BMU}"
try_b1610 "C: settlementDateFrom/To + bmUnit" \
  --data-urlencode "settlementDateFrom=${DAY_FROM}" --data-urlencode "settlementDateTo=${DAY_TO}" --data-urlencode "bmUnit=${BMU}"
try_b1610 "D: from/to ISO + settlementPeriodFrom/To + bmUnit" \
  --data-urlencode "from=${DAY_FROM}T00:00Z" --data-urlencode "to=${DAY_TO}T00:00Z" \
  --data-urlencode "settlementPeriodFrom=1" --data-urlencode "settlementPeriodTo=50" --data-urlencode "bmUnit=${BMU}"
rm -f "$OUT/b1610_try.json" "$OUT/b1610_try.err"

if [ -z "$WINNER" ]; then
  fail "B1610: no parameter variant returned data (see $OUT/b1610_params.txt). Check the current /datasets/B1610/stream parameter names in the Elexon BMRS API docs."
fi

# ---------------------------------------------------------------- BMU reference
code=$(get "bmu_reference" "$BMU_REF_URL")
if [ "$code" != "200" ] || ! body_ok "$OUT/bmu_reference.json"; then
  fail "BMU reference: http=$code from $BMU_REF_URL (see $OUT/bmu_reference.err)"
fi

# ---------------------------------------------------------------- Open-Meteo (ERA5 100 m wind)
METEO_NOTE="models=era5 accepted"
code=$(get "open_meteo" "$METEO_URL" \
  --data-urlencode "latitude=${LAT}" --data-urlencode "longitude=${LON}" \
  --data-urlencode "start_date=${DAY_FROM}" --data-urlencode "end_date=${DAY_TO}" \
  --data-urlencode "hourly=wind_speed_100m" --data-urlencode "wind_speed_unit=ms" \
  --data-urlencode "timezone=GMT" --data-urlencode "cell_selection=sea" --data-urlencode "models=era5")
if [ "$code" != "200" ]; then
  METEO_NOTE="models=era5 REJECTED (http=$code); retried without models, record the actual model in snapshot meta"
  code=$(get "open_meteo" "$METEO_URL" \
    --data-urlencode "latitude=${LAT}" --data-urlencode "longitude=${LON}" \
    --data-urlencode "start_date=${DAY_FROM}" --data-urlencode "end_date=${DAY_TO}" \
    --data-urlencode "hourly=wind_speed_100m" --data-urlencode "wind_speed_unit=ms" \
    --data-urlencode "timezone=GMT" --data-urlencode "cell_selection=sea")
fi
if [ "$code" != "200" ] || ! body_ok "$OUT/open_meteo.json"; then
  fail "Open-Meteo: http=$code from $METEO_URL (see $OUT/open_meteo.err)"
fi

# ---------------------------------------------------------------- NESO carbon intensity
code=$(get "neso" "$NESO_URL")
if [ "$code" != "200" ] || ! body_ok "$OUT/neso.json"; then
  fail "NESO: http=$code from $NESO_URL (see $OUT/neso.err)"
fi

# ---------------------------------------------------------------- Report (paste this output into the coding chat)
echo
echo "===== B1610 parameter variants ====="
cat "$OUT/b1610_params.txt"
echo "winner: ${WINNER:-none}"
echo
echo "===== B1610 head ====="
head -c 800 "$OUT/b1610.json" 2>/dev/null
echo
echo
echo "===== BMU reference: records for assets.yaml units ====="
for u in T_DDGNO-1 T_BRBEO-1 T_RBWNO-1; do
  echo "--- $u"
  snippet=$(grep -o ".\{0,200\}\"${u}\".\{0,400\}" "$OUT/bmu_reference.json" 2>/dev/null | head -n 1)
  if [ -n "$snippet" ]; then
    echo "$snippet"
  else
    echo "NOT FOUND in reference payload: confirm bm_unit in assets.yaml"
  fi
done
echo
echo "===== BMU reference head (field names) ====="
head -c 800 "$OUT/bmu_reference.json" 2>/dev/null
echo
echo
echo "===== Open-Meteo ($METEO_NOTE) head ====="
head -c 800 "$OUT/open_meteo.json" 2>/dev/null
echo
echo
echo "===== NESO head ====="
head -c 800 "$OUT/neso.json" 2>/dev/null
echo
echo

if [ "$FAILS" -gt 0 ]; then
  echo "PROBE FAILED: $FAILS source(s) failed" >&2
  exit 1
fi
echo "PROBE OK: all four sources returned data"
exit 0
