**Track 2: Renewable Energy & Energy Trading (crossing Track 1: Carbon Markets & Emissions Transparency, and Track 4: Climate Data & Environmental Monitoring).**

# GridProof

Physics-checked, replay-proof, browser-re-verifiable half-hourly renewable certificates, built on real GB offshore-wind data.

> A renewable certificate is only as good as the meter number behind it and the guarantee that it is issued and claimed once. GridProof lets anyone re-check both.

## Try it in 30 seconds

| | |
|---|---|
| **Live verifier** | https://YOUR-USERNAME.github.io/YOUR-REPO/  (pick an asset and a day: green = the browser rebuilt the Merkle root and it equals the on-chain root) |
| **Contract (Sepolia)** | [`0x40CC75e65b87F1c5746486dD7Adecd71a68da83a`](https://sepolia.etherscan.io/address/0x40CC75e65b87F1c5746486dD7Adecd71a68da83a) |
| **Demo video** | YOUR-VIDEO-LINK |
| **Try the hard day** | `race_bank_1`, **2025-03-30**: the 46-period daylight-saving day, still green |

## Headline result

Flat-average carbon accounting **overstates emissions by 22.7%** compared with half-hourly accounting, measured on 159.7 GWh of real certified offshore-wind output (March 2025): 23,371 tCO2e flat versus 19,054 tCO2e half-hourly. Wind blows when the grid is cleaner, so averaging hides that.

## How it works

fetch → align → learn envelope → judge → Merkle → issue → retire → verify

1. Pull settled half-hourly MWh (Elexon B1610), ERA5 100 m wind (Open-Meteo) and grid carbon intensity (NESO) for three real GB offshore-wind BM units: Dudgeon, Burbo Bank Extension, Race Bank.
2. Learn a statistical physics envelope from 11 months of data (per-wind-bin 99.5th-percentile capacity factor). It is a plain statistical model, not deep learning.
3. Judge March 2025 against the envelope and physical limits. A flag means "flagged for audit", not "fraud".
4. Merkle-batch accepted intervals per asset-day. One transaction per asset-day stores root, model hash and snapshot hash on Sepolia (93 asset-days).
5. Retire a half-hour with its Merkle proof: it can only be retired once, and gCO2e is computed on-chain from half-hourly intensity.
6. Verify from a committed snapshot (browser or CLI) or live by re-fetching the public APIs. Python, Solidity and browser JS compute identical leaves and roots.

Details, trust table and claims ledger: [ARCHITECTURE.md](ARCHITECTURE.md).

## Quickstart

Requires Python 3.11, Node 20+ (tested on 22), Ubuntu or macOS. No paid keys, no hardware.

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && npm ci
bash scripts/verify.sh                                  # compile, 10 contract tests, local deploy, full pipeline, 14 claim checks
python -m gridproof.cli verify --mode snapshot --network testnet   # re-check all 93 roots against Sepolia (needs TESTNET_RPC_URL in .env)
python -m gridproof.cli report                          # writes results/report.html
```

Add `--live` to `scripts/verify.sh` to also re-fetch the public APIs. To see the verifier locally: `cd web && python3 -m http.server 8000`.

`scripts/verify.sh` always runs on its own local node and never touches Sepolia. Copy `.env.example` to `.env` only if you want to use the testnet.

## Demo: tamper and replay

```bash
# Flip one kWh in a copy of a snapshot and re-verify: exit code 2, mismatch names asset, day and sp
python -m gridproof.cli verify --mode snapshot --asset burbo_ext --day 20250301
# Re-issue an existing day: reverts with BATCH_EXISTS (claim C06)
# Retire the same half-hour twice: reverts with ALREADY_RETIRED (claim C07)
```

## Measured results

All values come from committed files in `results/`; `bash scripts/verify.sh` reproduces them.

| Metric | Result | Source |
|---|---|---|
| Flat vs half-hourly overstatement | **22.66%** (21.4% to 24.7% by asset) | `emissions.json` |
| Held-out false-flag rate (threshold 3%) | 0.31% / 0% / 0% (Burbo / Dudgeon / Race Bank) | `calibration.json` |
| Detection recall, inflated real data (sensitivity test) | 30% to 36% at 2.0x; lower at smaller factors | `detection.json` |
| Blind regions (inflation that cannot be detected) | published per wind bin | `blind.json`, `report.html` |
| Asset-days verified (snapshot and live) | 93 / 93 | `claims.json` C10 |
| Gas per certified half-hour (local node) | ~3.3k; ~144k per asset-day | `chain.json`, C13 |
| Gas on Sepolia | ~578k per issue transaction; ~157k per retirement | `receipts.json`, C09 |
| Claims checks | 14 / 14 pass | `claims.json` |

Negative import readings (9% to 13% of intervals) are excluded and reported separately; they are not counted as false flags.

## Honest limits

- The oracle, the data sources and the issuer key remain trusted. GridProof makes their output re-checkable; it does not remove them.
- ERA5 has about 25 km resolution, so local wind can differ from the grid cell.
- The envelope has blind regions where inflated readings stay under the limit. They are published, not hidden.
- Detection results are a sensitivity test that perturbs real data. They are not evidence of fraud found in the wild.
- This is a proof of concept on a testnet with three assets and one month. A production system needs an audited contract, a decentralised or multi-party issuer, and one data adapter per grid.
- Out of scope: marketplace, tokens, transfers, solar, non-GB grids, mobile app.

## Who it is for and how it scales

REGO-style certificate issuers, auditors and corporate buyers moving towards hourly matching. It needs no new hardware: only public data. Each new grid needs one adapter for its meter and intensity sources. Cost is one transaction per asset-day.

## Layout

```
contracts/   Solidity registry (OpenZeppelin 5.0.2)
gridproof/   Python pipeline, oracle, chain client, CLI
web/         single-file browser verifier (published with GitHub Pages)
scripts/     probe.sh, deploy.js, verify.sh
data/        snapshots, models, batches, receipts (committed)
results/     measured evaluation output (committed)
```

## Hackathon

IEEE ClimateChain Global Hackathon 2026.