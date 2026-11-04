"""Run the Porch benchmark.

    python -m bench run                                  # every case, porch and baseline, 3 trials
    python -m bench run --split tune --trials 5
    python -m bench run --cases F4-lamp-toggle-slow --conditions porch
    python -m bench cases                                # list the cases and their checks

Results go to bench/results/<run id>/: cases.json (the cases as run, with their hash),
runs.jsonl (one record per run, with every tool call and the audit trail), summary.md.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .agent import ScriptedAgent
from .cases import CASES, select
from .conditions import CONDITIONS
from .report import markdown, summarize
from .runner import run_case

RESULTS = Path(__file__).parent / "results"


def _git() -> str:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                                cwd=Path(__file__).parent, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                               cwd=Path(__file__).parent, check=True).stdout.strip()
        return commit + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def run(args: argparse.Namespace) -> Path:
    cases = select(args.split, args.cases)
    conditions = args.conditions.split(",")
    for name in conditions:
        if name not in CONDITIONS:
            sys.exit(f"unknown condition {name!r}; choose from {', '.join(CONDITIONS)}")
    run_id = args.name or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else RESULTS / run_id
    out.mkdir(parents=True, exist_ok=True)
    cases_json = json.dumps([c.public() for c in cases], indent=2, sort_keys=True)
    (out / "cases.json").write_text(cases_json + "\n")
    meta = {"run_id": run_id, "agent": ScriptedAgent.name, "trials": args.trials, "cases": len(cases),
            "split": args.split or "all", "conditions": conditions, "simulate_latency": not args.fast,
            "cases_sha256": hashlib.sha256(cases_json.encode()).hexdigest()[:16], "git": _git(),
            "started_at": datetime.now(UTC).isoformat()}
    print(f"run {run_id}: {len(cases)} cases x {len(conditions)} conditions x {args.trials} trials -> {out}")
    runs = []
    with (out / "runs.jsonl").open("w") as f:
        for trial in range(1, args.trials + 1):
            for case in cases:
                for condition in conditions:
                    started = time.perf_counter()
                    record = await run_case(case, condition, trial, simulate_latency=not args.fast)
                    runs.append(record)
                    f.write(json.dumps(record, default=str) + "\n")
                    f.flush()
                    mark = "PASS" if record["checks"]["success"] else "fail"
                    print(f"  t{trial} {case.id:<26} {condition:<9} {mark}  {record['final_status']:<22} "
                          f"sends={record['sends']} {time.perf_counter() - started:5.1f}s", flush=True)
    meta["finished_at"] = datetime.now(UTC).isoformat()
    (out / "meta.json").write_text(json.dumps({**meta, "summary": summarize(runs)}, indent=2) + "\n")
    (out / "summary.md").write_text(markdown(runs, meta))
    print(f"\n{(out / 'summary.md').read_text()}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bench", description="Porch vs baseline benchmark (HW7).")
    sub = parser.add_subparsers(dest="action", required=True)
    r = sub.add_parser("run", help="run the benchmark")
    r.add_argument("--split", choices=["tune", "fresh", "all"], default="all")
    r.add_argument("--cases", nargs="*", help="only these case ids")
    r.add_argument("--conditions", default="porch,baseline")
    r.add_argument("--trials", type=int, default=3)
    r.add_argument("--fast", action="store_true", help="no simulated network latency (for quick checks)")
    r.add_argument("--name", help="run id (default: UTC timestamp)")
    r.add_argument("--out", help="output directory (default: bench/results/<run id>)")
    sub.add_parser("cases", help="list the cases")
    args = parser.parse_args()
    # The console shows one line per run; the hub's own log lines are in runs.jsonl as the audit trail.
    for name in ("porch_hub", "engine_lite", "httpx", "httpx2", "httpcore"):
        logging.getLogger(name).setLevel(logging.ERROR)
    if args.action == "cases":
        for c in CASES:
            print(f"{c.id:<26} {c.split:<6} {c.kind:<9} should_happen={c.should_happen!s:<5} {c.task}")
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
