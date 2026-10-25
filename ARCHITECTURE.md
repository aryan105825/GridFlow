# GridProof architecture

## 1. Data flow

```
 Elexon B1610 (settled MWh)      Open-Meteo / ERA5 (100 m wind)      NESO (actual gCO2/kWh)
          |                                |                                   |
          +---------------+----------------+-----------------------------------+
                          v
                 sources.py  (cache, retries, loud field assertions)
                          v
                 dataset.py  (align to settlement periods; DST-aware expected_periods;
                              wind interpolated to half-hour midpoint; canonical JSON)
                          v
              data/snapshots/{asset}.json   <-- committed, hashed (snapshot_hash per asset-day)
                          |
          train window    |    issue window
   2024-04-01..2025-02-28 |  2025-03-01..2025-03-31
                          v
   envelope.py (per-wind-bin 99.5th-percentile capacity factor + margin)
        -> data/models/{asset}.json   <-- sha256 = model_hash, anchored on-chain
                          v
   oracle.py  judge(): OK | NEG_OR_NAN | MISSING_DATA | OVER_CAPACITY | ABOVE_ENVELOPE | FROZEN_METER
                          v   accepted intervals only
   merkle.py  leaf = keccak(keccak(abi.encode(assetId, day, sp, kwh, g)))
              one tree per asset-day, sorted pairs, odd nodes promoted
                          v
   chain.py -> GridProofRegistry.issueBatch(root, modelHash, snapshotHash, totalKwh, intervals, holder)
                          v
   retire(assetId, day, sp, kwh, g, proof, beneficiaryRef)
        retiredBy[leaf] set once; gramsBy[beneficiary] += kwh * g   (computed on-chain)
                          v
   recompute.py / web/verify.html
        snapshot mode : rebuild root from committed snapshot, compare to on-chain root
        live mode     : re-fetch public APIs, compare kWh and g per interval; envelope flips = drift
```

## 2. Trust table

| Threat | What stops it | Residual risk |
|---|---|---|
| Replay (issuing an asset-day twice) | `issueBatch` reverts `BATCH_EXISTS` | None at contract level; a compromised issuer can still issue a wrong first batch |
| Double claim of one half-hour | `retiredBy[leaf]` set once; `ALREADY_RETIRED` | None at contract level |
| Tampered kWh or intensity after issue | Root mismatch in snapshot mode; per-interval mismatch in live mode, localised to (asset, day, sp) | Tampering with the source APIs before issue is not detected |
| Bad or inflated data at issue time | Rule checks plus the statistical envelope; on-chain daily and per-interval capacity caps | Inflation inside the envelope headroom is undetectable; headroom is published as the blind-region chart |
| Rogue issuer | Anyone can recompute every root from public data; mismatches are visible | The issuer key is a single trusted party; no multi-sig or slashing |
| Model swap (judging with a different model than claimed) | `modelHash` committed on-chain per batch; equals sha256 of `data/models/{asset}.json` | The model is only as good as its training data; the hash proves which model, not that it is right |
| Source data revised after issue | Training and issue windows sit after final settlement | Later revisions to ERA5 or NESO are reported as drift, not failure |
| Wind-cell mismatch | Offshore sea cells; margin fixed before the issue month is touched | ERA5 is about 25 km resolution |

Flags mean "flagged for audit", not "fraud".

## 3. Conventions

- `asset_id` = keccak256(utf8(bm_unit)); `day` = YYYYMMDD GB settlement date; `sp` = 1..50
- `kwh` = MWh x 1000 rounded; `g_per_kwh` = NESO actual
- Leaf ABI types, identical in Python, Solidity and JS: `(bytes32 assetId, uint32 day, uint8 sp, uint64 kwh, uint32 g)`
- Settlement periods per day: 48 normal, 46 on spring-forward (2025-03-30), 50 on fall-back

## 4. Claims ledger

| ID | Claim | How it is checked |
|---|---|---|
| C01 | Every certified half-hour comes from public data (B1610, ERA5, NESO); nothing synthetic | Re-fetch one day with cache off; kWh and g equal the snapshot; meta holds URLs and timestamp |
| C02 | Every settlement period is accounted for, including the 46-period DST day | accepted + rejected + missing == `expected_periods(day)` for all days |
| C03 | Held-out false-flag rate is at most 3% per asset (assumed threshold) | `calibration.json` against the threshold |
| C04 | Detection vs inflation is a labelled sensitivity test on real data, and blind regions are published | `detection.json` kind label; recall non-decreasing in factor; `blind.json` exists |
| C05 | Python, Solidity and browser JS compute identical leaves and roots | 20 sampled leaves match `leaf_of`; `verify_leaf` true; includes an odd-sized tree; Node-run JS vector matches |
| C06 | Re-issuing an asset-day is impossible | `expect_revert("issueBatch")` returns `BATCH_EXISTS` |
| C07 | An interval retires exactly once; forged kWh or proof fails | Reverts `ALREADY_RETIRED`, `BAD_PROOF`, `NOT_HOLDER` |
| C08 | On-chain physical caps hold | `EXCEEDS_DAILY_CAPACITY` and `EXCEEDS_INTERVAL_CAPACITY` |
| C09 | On-chain retired grams equal the independent Python sum | Retire 5 real leaves; `retired_grams(ref)` equals the sum of kwh x g |
| C10 | All on-chain roots are reproduced from snapshots; live re-fetch reproduces kWh and g | `verify --mode snapshot` and `--mode live` exit 0; drift listed separately |
| C11 | A one-digit tamper is detected and localised | Flip one kWh in a temp copy; exit 2 and mismatch names (asset, day, sp) |
| C12 | The model hash on-chain equals the sha256 of the model file | Every batch `model_hash` equals sha256 of `data/models/{key}.json` |
| C13 | Gas per certified half-hour is measured | `chain.json` has `gas_per_interval`; issue gas below 300k |
| C14 | Half-hourly vs flat-average tCO2e is measured | `emissions.json` has both totals; kWh sum equals on-chain `totalKwh` sum |

## 5. Scope

In: GB offshore wind, Sepolia, one adapter per data source.
Out: marketplace, tokens, transfers, solar, non-GB grids, mobile app.
