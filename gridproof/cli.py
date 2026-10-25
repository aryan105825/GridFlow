# @exports: main(argv: list[str] | None = None) -> int   # 0 ok, 1 error/failed check, 2 verify mismatch (drift never changes the code)
# @exports: CliError(RuntimeError)
# @imports: gridproof/schemas.py:load_settings(env, network), load_assets(path), day_from_date(d)
# @imports: gridproof/pipeline.py:run_fetch(s, use_cache), run_train(s), run_judge(s), run_issue(s, chain, max_days), run_retire(s, chain, asset_key, day, sp, beneficiary)
# @imports: gridproof/oracle.py:summary(verdicts)
# @imports: gridproof/chain.py:Chain.from_settings(s)
# @imports: gridproof/recompute.py:verify_all(chain, assets, s, mode, days), VerifyResult
# @imports: gridproof/evaluate.py:run_evaluate(s, chain) (lazy, milestone M3)
# @imports: gridproof/claims.py:run_checks(s, chain) -> list[Check{id, claim, ok, detail}] (lazy, milestone M3)
# @imports: gridproof/report.py:build_report(s) -> Path (lazy, milestone M3)
# @env: none (all environment variables are read by schemas.load_settings)
# @schema: python -m gridproof.cli fetch [--no-cache] | train | judge | evaluate [--network N] | check [--network N] | report | all [--network N]
# @schema: python -m gridproof.cli issue [--network localhost|testnet] [--max-days N]
# @schema: python -m gridproof.cli retire --asset KEY --day YYYYMMDD --sp N --beneficiary NAME [--network N]
# @schema: python -m gridproof.cli verify --mode snapshot|live [--asset KEY] [--day YYYYMMDD] [--json] [--network N]   # exit 0 ok, 2 mismatch
# @schema: `all` = fetch, train, judge, issue, verify (snapshot), evaluate, check, report; stops at the first non-zero step

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import sys

from gridproof.chain import Chain
from gridproof.oracle import summary
from gridproof.pipeline import run_fetch, run_issue, run_judge, run_retire, run_train
from gridproof.recompute import verify_all
from gridproof.schemas import load_assets, load_settings


class CliError(RuntimeError):
    pass


def _optional(name: str):
    try:
        return importlib.import_module(f"gridproof.{name}")
    except ModuleNotFoundError as exc:
        if exc.name == f"gridproof.{name}":
            raise CliError(f"gridproof/{name}.py is not in the repo yet (it is written in a later milestone), "
                           "so this command is not available.") from exc
        raise


def _json_default(o):
    if isinstance(o, (bytes, bytearray)):
        return "0x" + bytes(o).hex()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def _cmd_fetch(args) -> int:
    s = load_settings()
    for key, v in run_fetch(s, use_cache=not args.no_cache).items():
        print(f"fetched {key}: rows={v['rows']} days={v['days']} "
              f"{'written' if v['written'] else 'unchanged'} {v['path']}")
    return 0


def _cmd_train(args) -> int:
    s = load_settings()
    for key, v in run_train(s).items():
        print(f"trained {key}: n_train={v['n_train']} model_sha256={v['model_sha256']} {v['path']}")
    return 0


def _cmd_judge(args) -> int:
    s = load_settings()
    for key, verdicts in run_judge(s).items():
        print(f"judged {key}: {json.dumps(summary(verdicts), sort_keys=True)}")
    return 0


def _cmd_issue(args) -> int:
    s = load_settings(network=args.network)
    chain = Chain.from_settings(s)
    receipts = run_issue(s, chain, max_days=args.max_days)
    for r in receipts:
        print(f"{r['kind']} {r['asset_key']} day={r['day']} tx={r['tx']} block={r['block']} gas={r['gas_used']}")
    print(f"issued/registered {len(receipts)} new transactions (already-issued days were skipped)")
    return 0


def _cmd_retire(args) -> int:
    s = load_settings(network=args.network)
    chain = Chain.from_settings(s)
    print(json.dumps(run_retire(s, chain, args.asset, args.day, args.sp, args.beneficiary), indent=1))
    return 0


def _short(b: bytes | None) -> str:
    return "none" if b is None else "0x" + b.hex()[:10] + "..."


def _cmd_verify(args) -> int:
    s = load_settings(network=args.network)
    chain = Chain.from_settings(s)
    assets = load_assets(s.assets_file)
    if args.asset:
        assets = [a for a in assets if a.key == args.asset]
        if not assets:
            raise CliError(f"Unknown asset {args.asset!r}; keys in {s.assets_file}: "
                           f"{[a.key for a in load_assets(s.assets_file)]}.")
    days = [args.day] if args.day else None
    results = verify_all(chain, assets, s, args.mode, days)
    if args.json:
        print(json.dumps([dataclasses.asdict(r) for r in results], default=_json_default, indent=1))
    else:
        for r in results:
            print(f"{'PASS' if r.ok else 'FAIL'} verify[{args.mode}] asset={r.asset_key} day={r.day} "
                  f"intervals={r.intervals_local}/{r.intervals_chain} root_chain={_short(r.root_chain)} "
                  f"root_local={_short(r.root_local)} model_hash_ok={r.model_hash_ok}")
            for m in r.mismatches:
                print(f"  MISMATCH asset={r.asset_key} day={r.day} sp={m['sp']} {m['field']} "
                      f"published={m['published']} != local={m['local']}")
            for d in r.drift:
                print(f"  DRIFT asset={r.asset_key} day={r.day} sp={d['sp']} "
                      f"published_accepted={d['published_accepted']} local_reason={d['local_reason']}")
        ok = sum(1 for r in results if r.ok)
        print(f"{ok}/{len(results)} asset-days verified in {args.mode} mode; "
              f"{sum(len(r.drift) for r in results)} drift notes (informational)")
    return 0 if all(r.ok for r in results) else 2


def _cmd_evaluate(args) -> int:
    mod = _optional("evaluate")
    s = load_settings(network=args.network)
    mod.run_evaluate(s, Chain.from_settings(s))
    print(f"evaluation written to {s.results_dir}/")
    return 0


def _cmd_check(args) -> int:
    mod = _optional("claims")
    s = load_settings(network=args.network)
    checks = mod.run_checks(s, Chain.from_settings(s))
    for c in checks:
        print(f"{'PASS' if c.ok else 'FAIL'} {c.id}: {c.detail}")
    failed = [c.id for c in checks if not c.ok]
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


def _cmd_report(args) -> int:
    mod = _optional("report")
    print(mod.build_report(load_settings()))
    return 0


def _cmd_all(args) -> int:
    steps = [("fetch", _cmd_fetch), ("train", _cmd_train), ("judge", _cmd_judge), ("issue", _cmd_issue),
             ("verify", _cmd_verify), ("evaluate", _cmd_evaluate), ("check", _cmd_check),
             ("report", _cmd_report)]
    args.max_days = None
    args.no_cache = False
    args.mode = "snapshot"
    args.asset = None
    args.day = None
    args.json = False
    for name, fn in steps:
        print(f"== {name} ==")
        code = fn(args)
        if code != 0:
            print(f"all: step '{name}' exited with {code}; stopping.", file=sys.stderr)
            return code
    return 0


def _build_parser() -> argparse.ArgumentParser:
    net = argparse.ArgumentParser(add_help=False)
    net.add_argument("--network", choices=["localhost", "testnet"], default="localhost")
    p = argparse.ArgumentParser(prog="python -m gridproof.cli", description="GridProof pipeline")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="download B1610, ERA5 wind and NESO intensity into data/snapshots")
    f.add_argument("--no-cache", action="store_true")
    f.set_defaults(fn=_cmd_fetch)
    sub.add_parser("train", help="fit the physics envelope").set_defaults(fn=_cmd_train)
    sub.add_parser("judge", help="judge the issue window and print a summary").set_defaults(fn=_cmd_judge)

    i = sub.add_parser("issue", parents=[net], help="register assets and issue day batches")
    i.add_argument("--max-days", type=int, default=None)
    i.set_defaults(fn=_cmd_issue)

    r = sub.add_parser("retire", parents=[net], help="retire one certified half-hour")
    r.add_argument("--asset", required=True)
    r.add_argument("--day", type=int, required=True)
    r.add_argument("--sp", type=int, required=True)
    r.add_argument("--beneficiary", required=True)
    r.set_defaults(fn=_cmd_retire)

    v = sub.add_parser("verify", parents=[net], help="recompute roots and compare with the chain")
    v.add_argument("--mode", choices=["snapshot", "live"], required=True)
    v.add_argument("--asset")
    v.add_argument("--day", type=int)
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=_cmd_verify)

    sub.add_parser("evaluate", parents=[net]).set_defaults(fn=_cmd_evaluate)
    sub.add_parser("check", parents=[net]).set_defaults(fn=_cmd_check)
    sub.add_parser("report").set_defaults(fn=_cmd_report)
    sub.add_parser("all", parents=[net]).set_defaults(fn=_cmd_all)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except RuntimeError as exc:  # SourceError, ConfigError, ChainError, PipelineError, RecomputeError, CliError
        print(f"ERROR ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
