"""Turn run records into the tables for the write-up."""

import statistics
from collections import defaultdict
from typing import Any

METRICS = [
    ("success", "Correct outcome (all three checks)"),
    ("unsafe_executed", "Unsafe action carried out"),
    ("false_done", "Told 'done' but the device did not change"),
    ("missed_done", "Told 'not done' but the device changed"),
    ("duplicate", "Device received the command twice"),
]


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        by_condition[run["condition"]].append(run)
    summary = {}
    for condition, items in by_condition.items():
        unsafe = [r for r in items if r["kind"] == "unsafe"]
        legit = [r for r in items if r["should_happen"]]
        row = {
            "runs": len(items),
            "unsafe_runs": len(unsafe),
            "legit_runs": len(legit),
            "blocked_legit": sum(1 for r in legit if not r["checks"]["reached_target"]),
            "median_agent_ms": round(statistics.median(r["agent_ms"] for r in items)),
            "p90_agent_ms": round(_percentile([r["agent_ms"] for r in items], 0.9)),
            "mean_tool_calls": round(statistics.mean(r["tool_calls"] for r in items), 2),
            "owner_prompts": sum(r["owner_prompts"] for r in items),
            "tokens": sum(r["tokens"] for r in items),
        }
        for key, _ in METRICS:
            row[key] = sum(1 for r in items if r["checks"][key])
        summary[condition] = row
    return summary


def markdown(runs: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    summary = summarize(runs)
    conditions = list(summary)
    lines = [f"# Benchmark results: {meta['run_id']}", "",
             f"- Agent: {meta['agent']} · trials per case: {meta['trials']} · cases: {meta['cases']} "
             f"({meta['split']}) · simulated latency: {meta['simulate_latency']}",
             f"- Cases file sha256: `{meta['cases_sha256']}` · code: `{meta['git']}`", "",
             "## Summary", "", "| Metric | " + " | ".join(conditions) + " |",
             "|---|" + "---|" * len(conditions)]

    def cell(condition: str, key: str, of: str = "runs") -> str:
        row = summary[condition]
        return f"{row[key]} / {row[of]}"

    for key, label in METRICS:
        of = "unsafe_runs" if key == "unsafe_executed" else "runs"
        lines.append(f"| {label} | " + " | ".join(cell(c, key, of) for c in conditions) + " |")
    lines.append("| Legitimate request not carried out | " + " | ".join(cell(c, "blocked_legit", "legit_runs") for c in conditions) + " |")
    for key, label in [("median_agent_ms", "Median agent time (ms)"), ("p90_agent_ms", "90th percentile agent time (ms)"),
                       ("mean_tool_calls", "Mean tool calls per task"), ("owner_prompts", "Owner prompts"),
                       ("tokens", "Model tokens")]:
        lines.append(f"| {label} | " + " | ".join(str(summary[c][key]) for c in conditions) + " |")

    lines += ["", "## Per case", "", "| Case | Kind | " + " | ".join(conditions) + " |",
              "|---|---|" + "---|" * len(conditions)]
    by_case: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    kinds = {}
    for run in runs:
        by_case[run["case"]][run["condition"]].append(run)
        kinds[run["case"]] = f"{run['kind']}, {run['split']}"
    for case, per in by_case.items():
        cells = []
        for condition in conditions:
            items = per.get(condition, [])
            passed = sum(1 for r in items if r["checks"]["success"])
            last = items[-1] if items else None
            detail = f"{last['final_status']}; device {_short(last['after'])}; sends {last['sends']}" if last else ""
            cells.append(f"{passed}/{len(items)} pass ({detail})")
        lines.append(f"| {case} | {kinds[case]} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _short(state: dict[str, Any]) -> str:
    return ", ".join(f"{k.split('.')[-1]}={v}" for k, v in state.items())


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]
