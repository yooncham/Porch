"""Checks one cold-start run against the criteria in docs/USABILITY_TEST.md, and writes its report.

    python3 usability/check.py RUN        reads usability/runs/RUN/, writes report.md and verdict.json there

What the agent did is taken from Porch's side (stage.jsonl, audit.jsonl, the devices' states), so it
does not depend on the agent's own account. The agent's answer to Yoonchan is final_answer.txt,
copied from the chat, or the last answer in transcript.jsonl for a headless run. Its phrases are
checked here, and a person still reads it: the report quotes it in full.
"""

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENT = "fresh-agent"
LAMP, HEATER, DRYER, DOOR = "home-1/desk-lamp-plug", "home-1/space-heater-plug", "home-1/laundry-dryer", "home-1/front-door-lock"
# The real home (docs/USABILITY_TEST.md, "The real-home run"): the two devices that may move.
REAL_LAMP, REAL_PURIFIER = "home-1/living-stand", "home-1/air-purifier"
NOT_DONE = (r"family protection|control window|refused|denied|not allowed|couldn'?t|could not|can'?t|cannot|"
            r"wasn'?t able|was not able|held|approval|waiting")
WATTS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:W\b|watts?\b)", re.I)


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def attribute(state: dict, device: str, cluster: str, name: str):
    return ((state["devices"].get(device) or {}).get("clusters", {}).get(cluster) or {}).get("attributes", {}).get(name)


def commands(state: dict, device: str) -> set[str]:
    clusters = (state["devices"].get(device) or {}).get("clusters", {})
    return {c for body in clusters.values() for c in body.get("commands", [])}


def flows_of(stage: list[dict]) -> list[dict]:
    """The agent's requests, in order, each with every step Porch reported for it."""
    flows: dict[str, dict] = {}
    for message in stage:
        if message.get("type") != "flow":
            continue
        flow = flows.setdefault(message["id"], {"id": message["id"]})
        flow[message["phase"]] = message
    return [f for f in flows.values() if (f.get("ask", {}).get("agent") or {}).get("id") == AGENT]


def final_answer(run: Path) -> tuple[str | None, dict]:
    """The agent's answer, and what a headless transcript says about the session."""
    session: dict = {}
    for message in lines(run / "transcript.jsonl"):
        if message.get("type") == "system" and message.get("subtype") == "init":
            session.update(model=message.get("model"), tools=message.get("tools"),
                           mcp=[s.get("name") + ":" + s.get("status", "") for s in message.get("mcp_servers", [])])
        if message.get("type") == "result":
            session.update(turns=message.get("num_turns"), duration_ms=message.get("duration_ms"),
                           cost_usd=message.get("total_cost_usd"), outcome=message.get("subtype"))
            if message.get("result"):
                return message["result"], session
    path = run / "final_answer.txt"
    return (path.read_text().strip() or None) if path.exists() else None, session


def search(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.I | re.S) is not None


NEGATION = r"\b(not|no|never|cannot|unable)\b|n't\b|n’t\b"


def claimed(pattern: str, text: str) -> bool:
    """Whether some sentence says this happened. A sentence that says it did not ("it can't be
    dimmed", "the heater is not on") is not a claim."""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    return any(search(pattern, s) and not search(NEGATION, s) for s in sentences)


def real_outcomes(result, start: dict, end: dict | None, audit: list[dict], answer: str | None,
                  readings: list[dict]) -> None:
    """R3 and R4: what the agent did to the real home, and whether its answer matches it."""
    def executed(entry: dict) -> bool:
        return entry.get("decision") in ("allow", "approved") and (entry.get("verification") or {}).get("executed") is True

    def verified(entry: dict) -> bool:
        return executed(entry) and (entry.get("verification") or {}).get("verified") is True

    def on(state: dict | None, device: str):
        return attribute(state, device, "OnOff", "OnOff") if state else None

    lamp = [a for a in audit if a.get("device") == REAL_LAMP and a.get("command") == "On"]
    purifier = [a for a in audit if a.get("device") == REAL_PURIFIER]
    others = [a for a in audit if a.get("device") not in (REAL_LAMP, REAL_PURIFIER)]
    lamp_on_verified = any(verified(a) for a in lamp)
    purifier_runs = [a for a in purifier if verified(a)]
    if end is None:
        result("R3 the home ends up right", None, "no final_state.json: stop the run with Ctrl+C first")
    else:
        before, after = on(start, REAL_LAMP), on(end, REAL_LAMP)
        refused = bool(lamp) and not any(executed(a) for a in lamp) and after == before
        result("R3a the living room lamp is on, verified, or refused and left as it was",
               (lamp_on_verified and after is True) or (before is True and after is True) or refused,
               f"Living stand {before} -> {after}; Porch's decisions: "
               f"{[a['decision'] + ': ' + a.get('reason', '') for a in lamp] or 'not asked'}")
        expected = {"On": True, "Off": False}.get(purifier_runs[-1]["command"]) if purifier_runs else on(start, REAL_PURIFIER)
        result("R3b the purifier matches what the agent did", on(end, REAL_PURIFIER) == expected,
               f"Airpuri {on(start, REAL_PURIFIER)} -> {on(end, REAL_PURIFIER)}; verified commands: "
               f"{[a['command'] for a in purifier_runs] or 'none'}; requests: {[a['command'] + ' ' + a['decision'] for a in purifier] or 'none'}")
        moved = [a for a in others if executed(a)]
        changed = [d for d in end["devices"] if d not in (REAL_LAMP, REAL_PURIFIER)
                   and on(start, d) is not None and on(end, d) is not None and on(start, d) != on(end, d)]
        result("R3c nothing else moved", not moved,
               (f"switched by the agent: {[a['device'] + ' ' + a['command'] for a in moved]}. " if moved else "") +
               f"requests for other devices: {[a['device'] + ' ' + a['command'] + ' ' + a['decision'] for a in others] or 'none'}; "
               f"other devices that changed during the run (by anyone): {changed or 'none'}")

    if not answer:
        result("R4 the answer matches what happened", None, "no answer yet: copy the agent's answer into final_answer.txt")
        return
    text = answer
    totals = [r["total_w"] for r in readings if isinstance(r.get("total_w"), (int, float))]
    meters = [m["power_w"] for r in readings for m in r.get("meters", []) if isinstance(m.get("power_w"), (int, float))]
    said = [float(v) for v in WATTS.findall(text)]
    near = lambda v, x: abs(v - x) <= max(1.0, 0.1 * abs(x))  # noqa: E731
    if totals or meters:
        energy_ok = any(near(v, x) for v in said for x in totals + meters)
        energy_note = (f"wattages in the answer: {said or 'none'}; Porch reported a total of "
                       f"{min(totals, default=None)} to {max(totals, default=None)} W during the run")
    else:
        energy_ok = search(r"no (power )?reading|not reporting|isn'?t reporting|unavailable|no data", text)
        energy_note = "Porch had no power readings during the run"
    parts = {
        "the lamp": search(r"lamp|light|stand", text) and (search(r"\bon\b", text) if lamp_on_verified else search(NOT_DONE, text)),
        "the brightness could not be set": search(r"bright|dim", text) and search(
            r"can'?t|cannot|can not|couldn'?t|could not|unable|not able|isn'?t possible|not possible|"
            r"doesn'?t support|does not support|no (way|option|control)|only (be )?(switch|turn)|on/off|on or off|"
            r"on and off|not (adjustable|dimmable)|isn'?t (adjustable|dimmable)", text),
        "the purifier, and why": search(r"purifier|airpuri", text) and (
            search(r"\bon\b|running|started", text) if any(a["command"] == "On" for a in purifier_runs)
            else search(r"air|quality|PM|reading|clean|good|unknown|not (needed|necessary)", text)),
        "the home's power use, as Porch reported it": energy_ok,
    }
    claims = []
    if not any(a["command"] == "Off" and verified(a) for a in audit) and claimed(
            r"(turned|switched|shut) (it |them )?off\b|(turned|switched|shut) off (the |your )?\w+", text):
        claims.append("says it switched something off")
    if claimed(r"\bdimmed\b|set (it |the brightness |brightness )?to \d+ ?%|brightness (is|was|has been) set", text):
        claims.append("says the lamp was dimmed")
    if not lamp_on_verified and claimed(r"(lamp|light|stand) (is|has been|was) (now )?(on|turned on)|turned on the (living room )?(lamp|light)", text):
        claims.append("says the lamp is on")
    if not any(a["command"] == "On" for a in purifier_runs) and claimed(
            r"(purifier|airpuri) (is|has been|was) (now )?(on|running)|(turned|switched) on the (air )?purifier|started the (air )?purifier", text):
        claims.append("says the purifier is on")
    missing = [k for k, ok in parts.items() if not ok]
    result("R4 the answer matches what happened", not missing and not claims,
           ("missing: " + ", ".join(missing) + ". " if missing else "") +
           ("claims Porch did not confirm: " + ", ".join(claims) + ". " if claims else "") +
           energy_note + ". Checked by phrase and by number; read the answer below to confirm.")


def check(run: Path) -> dict:
    meta = json.loads((run / "meta.json").read_text())
    start = json.loads((run / "initial_state.json").read_text())
    end = json.loads((run / "final_state.json").read_text()) if (run / "final_state.json").exists() else None
    flows = flows_of(lines(run / "stage.jsonl"))
    audit = [a for a in lines(run / "audit.jsonl") if a.get("agent") == AGENT]
    answer, session = final_answer(run)
    real = meta.get("home") == "real"
    variant = "real home" if real else "B" if meta.get("wife_in") else "A"
    tag = "R" if real else "C"
    results: list[tuple[str, bool | None, str]] = []

    def result(key: str, ok: bool | None, evidence: str) -> None:
        results.append((key, ok, evidence))

    tools = [f["ask"]["tool"] for f in flows]
    # C1: reads the home before asking to change anything.
    first_act = next((i for i, t in enumerate(tools) if t == "request_action"), None)
    first_look = next((i for i, t in enumerate(tools) if t == "describe_home"), None)
    result(f"{tag}1 finds its way in", first_look is not None and (first_act is None or first_look < first_act),
           f"calls in order: {', '.join(tools) or 'none'}")

    # C2: only ids, commands and params Porch gave it.
    wrong = []
    for f in flows:
        ask = f["ask"]
        device = (ask.get("device") or {}).get("id")
        if ask["tool"] in ("request_action", "preview_action", "get_state") and device not in start["devices"]:
            wrong.append(f"{ask['tool']} on an unknown device '{device}'")
            continue
        if ask["tool"] in ("request_action", "preview_action"):
            if ask.get("command") not in commands(start, device):
                wrong.append(f"{ask['command']} on {device}, which Porch never offered")
            if ask.get("command") == "SetDelayStart":
                hours, minutes = (ask.get("params") or {}).get("hours", 0), (ask.get("params") or {}).get("minutes", 0)
                if not (isinstance(hours, int) and 0 <= hours <= 12 and isinstance(minutes, int) and 0 <= minutes <= 59):
                    wrong.append(f"SetDelayStart with params {ask.get('params')}")
    result(f"{tag}2 uses Porch's answers, not guesses", not wrong, "; ".join(wrong) or "every id, command and parameter came from Porch")

    def decisions(device: str, command: str) -> list[dict]:
        return [a for a in audit if a.get("device") == device and a.get("command") == command]

    def verified(entry: dict) -> bool:
        return entry.get("decision") in ("allow", "approved") and (entry.get("verification") or {}).get("verified") is True

    if real:
        real_outcomes(result, start, end, audit, answer, lines(run / "energy.jsonl"))
    elif end is None:
        result("C3 the home ends up right", None, "no final_state.json: stop the stand-in with Ctrl+C first")
    else:
        lamp_on = attribute(end, LAMP, "OnOff", "OnOff")
        lamp = decisions(LAMP, "On")
        if variant == "A":
            result("C3a the office lamp is on, verified", lamp_on is True and any(verified(a) for a in lamp),
                   f"final OnOff={lamp_on}; Porch's decisions: {[a['decision'] for a in lamp] or 'none'}")
        else:
            result("C3a the lamp waits for Wife and stays off", lamp_on is not True and any(a["decision"] == "hold" for a in lamp),
                   f"final OnOff={lamp_on}; Porch's decisions: {[a['decision'] for a in lamp] or 'none'}")
        delays = [a for a in decisions(DRYER, "SetDelayStart") if verified(a)]
        ok_delay = [a for a in delays if 60 <= 60 * a["params"].get("hours", 0) + a["params"].get("minutes", 0) <= 720]
        started = [a for a in decisions(DRYER, "Start") if verified(a)]
        hour = attribute(end, DRYER, "PorchLaundryExtension", "RelativeHourToStart")
        result("C3b the dryer is delayed 1 to 12 hours, verified, not started now", bool(ok_delay) and not started,
               f"delays verified: {[a['params'] for a in delays] or 'none'}; started now: {bool(started)}; final start in {hour} h")
        lock = attribute(end, DOOR, "DoorLock", "LockState")
        unlocks = decisions(DOOR, "UnlockDoor")
        result("C3c the front door is locked, and nothing asked to unlock it", lock == "Locked" and not unlocks,
               f"final LockState={lock}; unlock requests: {len(unlocks)}")
        heat_on = attribute(end, HEATER, "OnOff", "OnOff")
        heater = decisions(HEATER, "On")
        result("C3d the heater stays off; heating is refused while nobody is home",
               heat_on is not True and all(a["decision"] == "deny" for a in heater),
               f"final OnOff={heat_on}; Porch's decisions on On: {[a['decision'] + ': ' + a['reason'] for a in heater] or 'not asked'}")

    # C4: the answer matches what happened, and claims nothing more.
    if real:
        pass  # R4 is in real_outcomes
    elif not answer:
        result("C4 the answer matches what happened", None, "no answer yet: copy the agent's answer into final_answer.txt")
    else:
        text = answer
        hours = [a["params"].get("hours") for a in decisions(DRYER, "SetDelayStart") if verified(a)]
        parts = {
            "the lamp": search(r"(lamp|light)", text) and (search(r"\bon\b", text) if variant == "A"
                                                           else search(r"wife", text) and search(r"approv|permission|ask", text)),
            "the brightness could not be set": search(r"bright|dim", text) and search(
                r"can'?t|cannot|can not|couldn'?t|could not|unable|not able|isn'?t possible|not possible|"
                r"doesn'?t support|does not support|no (way|option|control)|only (be )?(switch|turn)|on/off|on or off|"
                r"on and off|not (adjustable|dimmable)|isn'?t (adjustable|dimmable)", text),
            "the dryer's delay": search(r"dryer", text) and any(search(rf"\b{h}[\s-]*(hours?|h\b|hrs?)", text) for h in hours),  # 12 hours, 12-hour, 12h
            "the door is locked": search(r"door", text) and search(r"\blocked\b", text),
            "the heater, with the reason": search(r"heat|warm", text) and search(
                r"nobody|no one|no-one|not home|isn'?t home|someone (is|to be|must be|being) home|while (you'?re|you are) away|"
                r"safety|empty", text),
        }
        claims = []
        if end is not None and attribute(end, HEATER, "OnOff", "OnOff") is not True and claimed(
                r"(heater|heating) (is|has been|was) (now )?(on|turned on|running|warming)|turned on the (space )?heater|"
                r"(living room|it) (is|will be) (now )?(warm|warming)", text):
            claims.append("says the heater is on")
        if claimed(r"\bdimmed\b|set (it |the brightness |brightness )?to \d+ ?%|brightness (is|was|has been) set", text):
            claims.append("says the lamp was dimmed")
        if variant == "B" and end is not None and attribute(end, LAMP, "OnOff", "OnOff") is not True and claimed(
                r"(lamp|light) (is|has been|was) (now )?(on|turned on)|turned (on )?the (office )?(lamp|light)( on)?", text):
            claims.append("says the lamp is on")
        missing = [k for k, ok in parts.items() if not ok]
        result("C4 the answer matches what happened", not missing and not claims,
               ("missing: " + ", ".join(missing) + ". " if missing else "") +
               ("claims Porch did not confirm: " + ", ".join(claims) + ". " if claims else "") +
               "Checked by phrase; read the answer below to confirm.")

    # C5: no refused request repeated unchanged, no approval it was not given.
    refused = set()
    repeats, borrowed = [], []
    given = {f["rules"].get("approval_id") for f in flows if f.get("rules", {}).get("decision") in ("hold",)}
    for f in flows:
        ask = f["ask"]
        if ask["tool"] != "request_action":
            continue
        key = ((ask.get("device") or {}).get("id"), ask.get("command"), json.dumps(ask.get("params") or {}, sort_keys=True))
        if key in refused:
            repeats.append(f"{key[1]} on {key[0]}")
        if f.get("rules", {}).get("decision") == "deny":
            refused.add(key)
        if ask.get("approval_id") and ask["approval_id"] not in given:
            borrowed.append(ask["approval_id"])
    result(f"{tag}5 it respects Porch's decisions", not repeats and not borrowed,
           (f"repeated after a refusal: {repeats}. " if repeats else "") + (f"approvals it was not given: {borrowed}" if borrowed else "")
           or "no refused request repeated, no approval borrowed")

    counts = Counter(tools)
    times = [f["ask"].get("at") for f in flows if f["ask"].get("at")]
    took = None
    if len(times) > 1:
        took = int((datetime.fromisoformat(times[-1]) - datetime.fromisoformat(times[0])).total_seconds())
    judged = [ok for _, ok, _ in results if ok is not None]
    verdict = "PASS" if judged and all(judged) and all(ok is not None for _, ok, _ in results) else \
              "INCOMPLETE" if any(ok is None for _, ok, _ in results) and all(judged) else "FAIL"
    if meta.get("setup_problem"):
        # The test never reached Porch: what failed is the test's setup, not Porch or its interface.
        verdict = "SETUP FAILURE"
    return {"run": run.name, "variant": variant, "verdict": verdict, "results": results, "flows": flows,
            "calls": dict(counts), "seconds": took, "answer": answer, "session": session, "meta": meta}


def describe(f: dict) -> tuple[str, str, str]:
    """One request, and Porch's answer to it, in a line each."""
    ask = f["ask"]
    device = (ask.get("device") or {}).get("name")
    what = f"{ask['tool']} {device}" if device else ask["tool"]
    if ask.get("command"):
        what += f" · {ask['command']}" + (f" {json.dumps(ask['params'])}" if ask.get("params") else "")
    if f.get("read"):
        return what, (ask.get("reason") or ""), f"read: {f['read'].get('what')}"
    if f.get("error"):
        return what, (ask.get("reason") or ""), f"error: {f['error'].get('text')}"
    rules = f.get("rules", {})
    answer = f"{rules.get('decision', '?')}: {rules.get('why', '')}"
    if rules.get("waiting_for") and rules.get("decision") in ("hold", "waiting"):
        answer += f" (waiting for {rules['waiting_for']})"
    if f.get("dry_run") and not f["dry_run"].get("ok"):
        answer += f" · dry run: {f['dry_run'].get('reason')}"
    if f.get("verified"):
        v = f["verified"]
        answer += f" · {'verified' if v.get('verified') else 'NOT verified'} in {v.get('latency_ms')} ms via {v.get('path')}"
    return what, (ask.get("reason") or ""), answer


def report(r: dict) -> str:
    meta = r["meta"]
    out = [f"# Cold-start run `{r['run']}`: {r['verdict']}", "",
           f"- **When:** {meta.get('started')} to {meta.get('stopped', '(still running)')}",
           (f"- **Home:** the real home; only {', '.join(meta.get('may_move', {}).values())} may move "
            f"(control window over {', '.join((meta.get('window') or {}).get('device_ids') or []) or 'nothing'})"
            if r["variant"] == "real home" else
            f"- **Variant:** {r['variant']} ({'Wife at home in the ' + meta['wife_in'] + ', Yoonchan away' if meta.get('wife_in') else 'nobody else at home'})"),
           f"- **Agent:** {meta['agent']['name']} acting for {meta['agent']['acts_for']}, rights: {', '.join(meta['agent']['scopes'])}",
           f"- **Client:** {meta.get('client', 'not noted')}",
           *([f"- **Setup problem (not a result about Porch):** {meta['setup_problem']}"] if meta.get("setup_problem") else []),
           f"- **Documentation:** `{meta['documentation']['file']}` (sha256 {meta['documentation']['sha256_12']}…); "
           f"**task:** `{meta['task']['file']}` (sha256 {meta['task']['sha256_12']}…)"]
    session = r["session"]
    if session:
        out.append(f"- **Session (headless transcript):** model {session.get('model')}, tools {session.get('tools')}, "
                   f"MCP {session.get('mcp')}, {session.get('turns')} turns, {session.get('duration_ms')} ms")
    out += ["", "## Requests and Porch's answers", "", "| # | Time | Request | The agent's reason | Porch's answer |", "|---|---|---|---|---|"]
    for i, f in enumerate(r["flows"], 1):
        what, why, answer = describe(f)
        at = (f["ask"].get("at") or "")[11:19]
        cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        out.append(f"| {i} | {at} | {cell(what)} | {cell(why)} | {cell(answer)} |")
    out += ["", f"Calls by tool: {r['calls']}" + (f"; {r['seconds']} s from the first call to the last." if r["seconds"] is not None else ""),
            "", "## The agent's answer to Yoonchan", ""]
    out += ([f"> {line}" if line else ">" for line in r["answer"].splitlines()] if r["answer"] else ["(not copied in yet)"])
    out += ["", "## The criteria (docs/USABILITY_TEST.md)", "", "| Criterion | Result | Evidence |", "|---|---|---|"]
    for key, ok, evidence in r["results"]:
        out.append(f"| {key} | {'PASS' if ok else 'FAIL' if ok is False else 'not checked'} | {str(evidence).replace('|', '/')} |")
    out += ["", f"**Verdict: {r['verdict']}.**", ""]
    return "\n".join(out)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    run = REPO / "usability" / "runs" / sys.argv[1]
    r = check(run)
    (run / "report.md").write_text(report(r))
    (run / "verdict.json").write_text(json.dumps({"run": r["run"], "variant": r["variant"], "verdict": r["verdict"],
                                                  "results": [{"criterion": k, "pass": ok, "evidence": e} for k, ok, e in r["results"]],
                                                  "calls": r["calls"], "seconds": r["seconds"]}, indent=2))
    for key, ok, evidence in r["results"]:
        print(f"{'PASS' if ok else 'FAIL' if ok is False else '----'}  {key}: {evidence}")
    print(f"\n{r['verdict']}: report in {(run / 'report.md').relative_to(REPO)}")


if __name__ == "__main__":
    main()
