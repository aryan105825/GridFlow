# @exports: ReportError(RuntimeError)
# @exports: publish_web_data(s: Settings, assets: list[Asset], batches: list[dict]) -> Path   # writes web/data/{index.json,deployment.json,snapshots/,batches/,models/} for web/verify.html
# @exports: build_report(s: Settings) -> Path   # results/report.html, PNGs base64-embedded; FIRST figure = flat vs half-hourly tCO2e; also refreshes web/data
# @imports: gridproof/schemas.py:Settings, Asset, load_assets(path), date_from_day(day)
# @imports: gridproof/dataset.py:atomic_write(path, data)
# @imports: gridproof/merkle.py:to_hex(b)
# @imports: gridproof/pipeline.py:run_judge(s), judge_config(s, asset), snapshot_path(s, key), model_path(s, key), receipts_path(s)
# @imports: gridproof/evaluate.py:published_batches(s, assets), read_json(s, name)
# @env: none (all settings arrive through Settings)
# @schema: needs results/{emissions,calibration,blind,chain}.json (run `evaluate` first); data/receipts.json is optional (tx table)
# @schema: web/data/deployment.json = deployments/sepolia.json when it exists (the browser verifier reads Sepolia), otherwise the active deployment file; paths are relative to the repo root

from __future__ import annotations

import base64
import html as htmllib
import io
import json
import shutil
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from gridproof.dataset import atomic_write  # noqa: E402
from gridproof.evaluate import published_batches, read_json  # noqa: E402
from gridproof.merkle import to_hex  # noqa: E402
from gridproof.pipeline import judge_config, model_path, receipts_path, run_judge, snapshot_path  # noqa: E402
from gridproof.schemas import Asset, Settings, date_from_day, load_assets  # noqa: E402


class ReportError(RuntimeError):
    pass


def publish_web_data(s: Settings, assets: list[Asset], batches: list[dict]) -> Path:
    root = Path("web") / "data"
    for sub in ("snapshots", "batches", "models"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    days: dict[str, list[int]] = {}
    for b in batches:
        shutil.copyfile(b["path"], root / "batches" / Path(b["path"]).name)
        days.setdefault(b["key"], []).append(b["day"])
    for a in assets:
        if a.key in days:
            shutil.copyfile(snapshot_path(s, a.key), root / "snapshots" / f"{a.key}.json")
            shutil.copyfile(model_path(s, a.key), root / "models" / f"{a.key}.json")
    index = {"assets": [{"key": a.key, "bm_unit": a.bm_unit, "asset_id": to_hex(a.asset_id), "days": sorted(days[a.key])}
                        for a in assets if a.key in days]}
    atomic_write(root / "index.json", (json.dumps(index, indent=2) + "\n").encode("utf-8"))
    sepolia = Path("deployments") / "sepolia.json"
    src = sepolia if sepolia.exists() else Path(s.deployment_file)
    if not src.exists():
        raise ReportError(f"no deployment file found ({sepolia} or {s.deployment_file}). Fix: run `npm run deploy:local` or deploy to Sepolia.")
    shutil.copyfile(src, root / "deployment.json")
    return root


def _uri(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _fig_emissions(em: dict) -> str:
    keys = list(em["by_asset"])
    labels = ["ALL"] + keys
    hh = [em["tco2e_halfhourly"]] + [em["by_asset"][k]["tco2e_halfhourly"] for k in keys]
    fl = [em["tco2e_flat_mean"]] + [em["by_asset"][k]["tco2e_flat_mean"] for k in keys]
    x = np.arange(len(labels))
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.bar(x - 0.2, hh, 0.4, label="Half-hourly (NESO actual per period)")
    ax.bar(x + 0.2, fl, 0.4, label="Flat average (window mean)")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("tCO2e attributed to certified kWh")
    pct = em["overstatement_pct"]
    ax.set_title(f"Flat-average accounting {'overstates' if pct >= 0 else 'understates'} tCO2e by {abs(pct):.1f}%")
    ax.legend()
    return _uri(fig)


def _fig_scatter(s: Settings, assets: list[Asset], verdicts: dict) -> str:
    fig, axes = plt.subplots(1, len(assets), figsize=(5.2 * len(assets), 4.2), squeeze=False)
    for ax, a in zip(axes[0], assets):
        vs = verdicts[a.key]
        acc = [(v.interval.ws100, v.cf) for v in vs if v.accepted and v.interval.ws100 is not None]
        rej = [(v.interval.ws100, v.cf) for v in vs if not v.accepted and v.interval.ws100 is not None]
        if acc:
            ax.scatter(*zip(*acc), s=6, alpha=0.4, label=f"accepted ({len(acc)})")
        if rej:
            ax.scatter(*zip(*rej), s=14, c="red", label=f"flagged for audit ({len(rej)})")
        upper_fn = judge_config(s, a)[0]
        if upper_fn is not None:
            top = max([w for w, _ in acc + rej] + [20.0])
            xs = np.linspace(0, top, 300)
            ax.plot(xs, [upper_fn(float(w)) for w in xs], "k-", lw=1.2, label="envelope")
        ax.set_title(a.key)
        ax.set_xlabel("ERA5 100 m wind (m/s)")
        ax.set_ylabel("capacity factor")
        ax.legend(fontsize=7)
    return _uri(fig)


def _fig_blind(blind: dict) -> str:
    fig, axes = plt.subplots(1, len(blind), figsize=(5.2 * len(blind), 3.8), squeeze=False)
    for ax, (key, rows) in zip(axes[0], blind.items()):
        ax.bar(range(len(rows)), [r["headroom_pct_capacity"] for r in rows])
        ax.set_xticks(range(len(rows)))
        ax.set_xticklabels([r["ws_bin"] for r in rows], rotation=90, fontsize=6)
        ax.set_title(f"{key}: undetectable inflation headroom")
        ax.set_ylabel("% of capacity per half-hour")
    return _uri(fig)


def _fig_timeline(batches: list[dict]) -> str:
    fig, ax = plt.subplots(figsize=(8, 3.8))
    for key in sorted({b["key"] for b in batches}):
        mine = sorted((b for b in batches if b["key"] == key), key=lambda b: b["day"])
        ax.plot([date_from_day(b["day"]) for b in mine], [b["doc"]["total_kwh"] / 1000 for b in mine], label=key)
    ax.set_ylabel("certified MWh per day")
    ax.legend(fontsize=7)
    fig.autofmt_xdate()
    return _uri(fig)


def _table(headers: list[str], rows: list[list]) -> str:
    e = htmllib.escape
    head = "".join(f"<th>{e(str(h))}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{e(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def build_report(s: Settings) -> Path:
    assets = load_assets(s.assets_file)
    batches = published_batches(s, assets)
    publish_web_data(s, assets, batches)
    em, cal = read_json(s, "emissions.json"), read_json(s, "calibration.json")
    blind, chain = read_json(s, "blind.json"), read_json(s, "chain.json")
    verdicts = run_judge(s)
    figs = [
        ("Figure 1: flat-average vs half-hourly tCO2e", _fig_emissions(em)),
        ("Figure 2: wind vs capacity factor, envelope and flags", _fig_scatter(s, assets, verdicts)),
        ("Figure 3: blind regions (inflation the envelope cannot detect)", _fig_blind(blind)),
        ("Figure 4: certified energy per day", _fig_timeline(batches)),
    ]
    cal_rows = [[k, v["n_train"], v["n_test"], f"{v['false_flag_rate'] * 100:.2f}%"] for k, v in cal.items()]
    chain_rows = [[k, v] for k, v in chain.items()]
    rp = Path(receipts_path(s))
    tx_rows = []
    if rp.exists():
        tx_rows = [[r["kind"], r["asset_key"], r["day"], r["tx"], r["block"], r["gas_used"]] for r in json.loads(rp.read_text(encoding="utf-8"))]
    pct = em["overstatement_pct"]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>GridProof report</title>",
        "<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;line-height:1.45}"
        "img{max-width:100%}table{border-collapse:collapse;margin:1rem 0;font-size:.85rem}"
        "td,th{border:1px solid #8886;padding:.25rem .5rem;text-align:left;word-break:break-all}</style></head><body>",
        "<h1>GridProof report</h1>",
        f"<p><b>Flat-average accounting {'overstates' if pct >= 0 else 'understates'} tCO2e by {abs(pct):.1f}%</b> "
        f"on {em['kwh_certified']:,} certified kWh ({em['tco2e_halfhourly']} t half-hourly vs {em['tco2e_flat_mean']} t flat).</p>",
        f"<p>{htmllib.escape(em['flat_basis'])}. Flags mean flagged for audit, not fraud. Oracle, data sources and issuer key remain trusted.</p>",
    ]
    for title, uri in figs:
        parts.append(f"<h2>{htmllib.escape(title)}</h2><img alt='{htmllib.escape(title)}' src='{uri}'>")
    parts += ["<h2>Calibration (held-out issue month)</h2>", _table(["asset", "n_train", "n_test", "false-flag rate"], cal_rows),
              "<h2>Chain</h2>", _table(["metric", "value"], chain_rows)]
    if tx_rows:
        parts += ["<h2>Transactions</h2>", _table(["kind", "asset", "day", "tx", "block", "gas"], tx_rows)]
    parts.append("</body></html>")
    out = Path(s.results_dir) / "report.html"
    atomic_write(out, "\n".join(parts).encode("utf-8"))
    return out
