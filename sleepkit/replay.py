"""Layer 3: measured promotion, report-only.

A candidate is only worth persisting if it demonstrably helps. We cannot prove
that from a transcript, so we run the same task twice in clean contexts: once
with the candidate withheld, once with it injected as context, and report the
difference. Nothing here ever promotes automatically.

Read-only tool whitelist, per-task budget, and a quarantine pre-check on the
task prompt: a replay run is an unattended agent, so it gets the tightest leash.
"""
from __future__ import annotations

import difflib
import json
import os
import time

from . import llm, paths, safety

DEFAULT_TASKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "replay", "tasks.json")


def load_tasks(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    tasks = data.get("tasks", data if isinstance(data, list) else [])
    clean = []
    for t in tasks:
        prompt = t.get("prompt", "")
        reason = safety.quarantine_reason(prompt, "user")
        if reason:
            t = dict(t, skipped=reason)
        clean.append(t)
    return clean


def _sandbox(tid: str, condition: str) -> str:
    d = os.path.join(paths.home(), "replay", "runs",
                     time.strftime("%Y%m%d-%H%M%S"), "%s.%s" % (tid, condition))
    os.makedirs(os.path.join(d, "work"), exist_ok=True)
    return d


def _context_block(tasks_dir: str) -> str:
    """The treatment: provisional memory, framed as context, never as rules."""
    recs = []
    for c in store_candidates():
        recs.append(safety.render_for_context({
            "text": c.get("_body", ""), "source": c.get("source", "inferred"),
            "trust": c.get("trust", "speculative")}))
    if not recs:
        return ""
    return ("<provisional-memory unverified=\"true\">\n" + "\n".join(recs) +
            "\n</provisional-memory>\n")


def store_candidates():
    from . import store
    return store.list_candidates(status="provisional", with_body=True)


def answer_text(res: dict) -> str:
    """The answer inside whatever --output-format json returned.

    The envelope carries the counters. Diffing raw stdout diffed the envelope, so two
    runs that said opposite things could still score as similar: the JSON around them
    was nearly identical.
    """
    items = res.get("json")
    items = items if isinstance(items, list) else [items]
    for item in reversed(items):
        if isinstance(item, dict):
            for key in ("result", "text", "answer"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return (res.get("stdout") or "").strip()


def normalize_answer(text: str) -> str:
    """Case and layout are not signal. Collapse both before any comparison."""
    return " ".join((text or "").split()).lower()


def answer_drift(off_answer: str, on_answer: str) -> float:
    """How much the two answers differ, 0.0 identical to 1.0 unrelated.

    Descriptive only. Both runs can be right, both can be wrong, and a rephrasing of
    the same answer scores as no change. This replaced a similarity score taken from
    each side's last 400 characters, which reported agreement about a window neither
    run was asked to fill.
    """
    a, b = normalize_answer(off_answer), normalize_answer(on_answer)
    if not a and not b:
        return 0.0
    return round(1.0 - difflib.SequenceMatcher(None, a, b).ratio(), 3)


def keyword_hit(answer: str, groups: list) -> bool:
    """True when every group finds at least one of its spellings in the answer.

    One fact reaches the page as "idle timeout" or "idle_timeout", so a group lists
    alternatives and passes on any of them; the task passes only when all groups do.
    """
    text = normalize_answer(answer)
    for group in groups:
        alts = group if isinstance(group, list) else [group]
        if not any(n in text for n in (normalize_answer(alt) for alt in alts) if n):
            return False
    return True


def criteria_check(task: dict, answer: str) -> dict:
    """Score one answer against the machine-checkable part of its criteria.

    measurable=False is the honest result for a task that records prose only: it
    cannot lift or sink a claim, so it is excluded from the tally instead of scored
    on a guess. The keyword lists are hand-authored anchors, and a substring match is
    a proxy for the criteria, not the criteria.
    """
    need = task.get("criteria_keywords") or []
    forbid = task.get("criteria_forbid") or []
    if not need:
        return {"measurable": False, "met": None, "missing": [], "forbidden": []}
    text = normalize_answer(answer)
    missing = []
    for group in need:
        if not keyword_hit(answer, [group]):
            alts = group if isinstance(group, list) else [group]
            missing.append("|".join(alts))
    banned = [f for f in forbid if normalize_answer(f) in text]
    return {"measurable": True, "met": not missing and not banned,
            "missing": missing, "forbidden": banned}


def verdict(off: dict, on: dict, treatment_delivered: bool) -> str:
    """The weakest claim this pair supports, in words.

    Four of the six branches say nothing happened or nothing could be seen, and that
    is the usual result of a three-task suite against one memory entry. A verdict that
    reads like evidence only when there is evidence is the point.
    """
    if not treatment_delivered:
        return "not measured: no provisional memory reached the on-runs"
    if not off.get("measurable") or not on.get("measurable"):
        return "not measurable: criteria are prose only"
    if off.get("met") and on.get("met"):
        return "tie: both met"
    if off.get("met") is False and on.get("met") is False:
        return "tie: both missed"
    return "helped" if on.get("met") else "hurt"


def _tally(rows: list) -> dict:
    out = {}
    for r in rows:
        out[r["verdict"]] = out.get(r["verdict"], 0) + 1
    return out


def criteria_cell(row: dict) -> str:
    off, on = row["criteria_off"], row["criteria_on"]
    if not off.get("measurable") or not on.get("measurable"):
        return "n/a"
    def side(c):
        if c.get("met"):
            return "met"
        if c.get("forbidden"):
            return "tripped"
        return "miss"
    return "%s/%s" % (side(off), side(on))


def metrics_from_result(raw) -> dict:
    """Pull the counters out of whatever shape --output-format json produced.

    Observed in the wild: a single result object, and an array of per-turn objects
    where only the last one carries num_turns/total_cost_usd. Anything else yields
    None-valued metrics rather than a crash, because a missing number in a report is
    recoverable and an exception in an unattended pass is not.
    """
    items = raw if isinstance(raw, list) else [raw]
    best = {}
    for item in items:
        if isinstance(item, dict) and any(k in item for k in
                                           ("num_turns", "total_cost_usd", "usage")):
            best = item
    raw_usage = best.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    cost = best.get("total_cost_usd", best.get("cost_usd"))
    if cost is None:
        cost = usage.get("total_cost_usd")
    return {"num_turns": best.get("num_turns"), "total_cost_usd": cost,
            "duration_ms": best.get("duration_ms"), "session_id": best.get("session_id")}


def run_task(task: dict, condition: str, memory_text: str, model: str,
             budget: float, timeout_s: int) -> dict:
    sbx = _sandbox(task["id"], condition)
    prompt = task["prompt"]
    if condition == "on" and memory_text:
        prompt = memory_text + "\n\n" + prompt
    judge = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prompts",
                         "replay-task.md")
    res = llm.run(system_prompt_file=judge, user_prompt=prompt,
                  add_dirs=[os.path.join(sbx, "work")],
                  allowed_tools=llm.READONLY_TOOLS, model=model,
                  budget_usd=budget, timeout_s=timeout_s, output_format="json")
    j = metrics_from_result(res.get("json"))
    answer = answer_text(res)
    metrics = {
        "condition": condition,
        "exit": res["exit"],
        "sandbox": sbx,
        "num_turns": j["num_turns"],
        "total_cost_usd": j["total_cost_usd"],
        "duration_ms": j["duration_ms"],
        "session_id": j["session_id"],
        "answer": answer,
        "answer_chars": len(answer),
    }
    with open(os.path.join(sbx, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"task": task, "metrics": metrics}, fh, indent=2, sort_keys=True)
    return metrics


def run_suite(tasks_path: str = DEFAULT_TASKS, limit: int = 3, model: str | None = None,
              budget: float = 0.50, timeout_s: int = 600) -> dict:
    model = llm.resolve_model(model)
    tasks = [t for t in load_tasks(tasks_path) if "skipped" not in t][:limit]
    skipped = [t for t in load_tasks(tasks_path) if "skipped" in t]
    memory_text = _context_block("")
    treatment = bool(memory_text.strip())
    rows = []
    arm_budget = budget / 2.0
    for t in tasks:
        off = run_task(t, "off", "", model, arm_budget, timeout_s)
        on = run_task(t, "on", memory_text, model, arm_budget, timeout_s)
        delta = {}
        for k in ("num_turns", "total_cost_usd", "duration_ms"):
            a, b = off.get(k), on.get(k)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                delta[k] = round(b - a, 4)
        check_off = criteria_check(t, off["answer"])
        check_on = criteria_check(t, on["answer"])
        rows.append({"id": t["id"], "criteria": t.get("criteria", ""),
                     "off": off, "on": on, "delta": delta,
                     "criteria_off": check_off, "criteria_on": check_on,
                     "answer_drift": answer_drift(off["answer"], on["answer"]),
                     "verdict": verdict(check_off, check_on, treatment)})
    samples = [r[c] for r in rows for c in ("off", "on")]
    rep = {"ts": paths.now(), "model": model, "budget_per_task": budget,
           "answered": sum(1 for s in samples if s.get("exit") == 0),
           "samples": len(samples),
           "treatment_delivered": treatment,
           "provisional_candidates": len(store_candidates()),
           "verdict_tally": _tally(rows),
           "skipped": [{"id": s.get("id"), "reason": s["skipped"]} for s in skipped],
           "rows": rows}
    return rep


def replay_due(interval_hours: float) -> tuple:
    """(due, why) for an unattended replay pass.

    No clock means this has never run, so the first nightly pass measures instead of
    waiting out a week. An unreadable clock is treated the same way: a pass that
    cannot check its own history should err toward the measurement that costs money,
    not toward a silent month of skipping.
    """
    from . import consolidate  # lazy: consolidate owns hours_since, and it imports no sibling
    if interval_hours <= 0:
        return True, "interval is 0, every pass is due"
    last = paths.read_state().get("last_replay")
    if not last:
        return True, "no replay pass has run yet"
    elapsed = consolidate.hours_since(last)
    if elapsed is None:
        return True, "the last_replay clock is unreadable: %r" % (last,)
    if elapsed >= interval_hours:
        return True, "%.1f h since the last pass" % elapsed
    return False, "%.1f of %.1f h since the last pass" % (elapsed, interval_hours)


def record_quality(rep: dict) -> dict:
    """Persist the memory-quality summary of a completed replay pass.

    The dated report file holds the detail, but a skipped night leaves no trace at all,
    so "is my memory actually helping?" had no answer between runs. A small rolling
    summary in state.json keeps the last real measurement readable from `status` and
    the nightly report without parsing report files.
    """
    tally = rep.get("verdict_tally") or {}
    summary = {"ran": paths.now(), "tally": tally,
               "helpful": tally.get("helps", 0),
               "harmed": tally.get("hurts", 0),
               "tie": tally.get("tie", 0),
               "samples": rep.get("samples", 0),
               "answered": rep.get("answered", 0),
               "treatment_delivered": bool(rep.get("treatment_delivered"))}
    paths.write_state({"replay_quality": summary})
    return summary


def quality_line(state: dict | None = None) -> str:
    """One line for the nightly report: what the last real measurement said.

    A skip is not a quality signal, so the line states the last measurement that
    actually happened and how long ago, instead of pretending the skip measured
    anything. Never-run is reported as such rather than as a zero score.
    """
    from . import consolidate  # lazy: hours_since lives there
    st = state if state is not None else paths.read_state()
    q = st.get("replay_quality") or {}
    if not q or not q.get("ran"):
        return "memory quality: no replay measurement has run yet"
    age = consolidate.hours_since(q.get("ran"))
    when = "just now" if age is None or age < 1 else (
        "%.0fh ago" % age if age < 48 else "%.0fd ago" % (age / 24.0))
    if not q.get("treatment_delivered"):
        return ("memory quality: last pass (%s) delivered no treatment, so it measured "
                "nothing" % when)
    return ("memory quality: %d helps / %d hurts / %d ties from %d samples (last pass %s)"
            % (q.get("helpful", 0), q.get("harmed", 0), q.get("tie", 0),
               q.get("samples", 0), when))


def mark_replayed() -> str:
    """Stamp last_replay so the next nightly pass waits out its interval."""
    ts = paths.now()
    paths.write_state({"last_replay": ts})
    return ts


def nightly(tasks_path: str = DEFAULT_TASKS, model: str | None = None, budget: float = 0.50,
            limit: int = 3, interval_hours: float = 168.0,
            dry_run: bool = False) -> dict:
    """The replay half of an unattended night: run when due, report, never promote.

    Every failure comes back as a field rather than a traceback. At 03:41 with nobody
    watching, a stack trace in a log file is indistinguishable from a night that did
    nothing useful, while one JSON line naming what was attempted and what failed is
    readable the next morning.
    """
    due, why = replay_due(interval_hours)
    if dry_run:
        return {"skipped": True, "reason": "dry run", "due": due, "detail": why,
                "quality_line": quality_line()}
    if not due:
        return {"skipped": True, "reason": why, "quality_line": quality_line()}
    try:
        rep = run_suite(tasks_path=tasks_path, limit=limit, model=model, budget=budget)
        report = write_report(rep)
    except (OSError, ValueError) as exc:
        return {"failed": True, "attempted": paths.now(),
                "reason": "%s: %s" % (type(exc).__name__, exc)}
    if rep["samples"] and not rep["answered"]:
        # Every sample exited non-zero, so the CLI answered nothing and there is no
        # measurement to date. Stamping the clock anyway would hide that for a week and
        # leave a report full of "tie: both missed" that reads like a result.
        first = rep["rows"][0]["off"] if rep["rows"] else {}
        return {"failed": True, "attempted": paths.now(), "report": report,
                "reason": "no model call answered: 0/%d samples exited 0; first answer "
                          "began %r" % (rep["samples"], (first.get("answer") or "")[:80])}
    summary = record_quality(rep)
    return {"ran": True, "report": report, "clock": mark_replayed(),
            "treatment_delivered": rep.get("treatment_delivered"),
            "verdict_tally": rep.get("verdict_tally"),
            "quality": summary, "quality_line": quality_line()}


def write_report(rep: dict) -> str:
    p = os.path.join(paths.reports_dir(), time.strftime("%Y-%m-%d-%H%M%S") + "-replay.md")
    L = ["# Sleep replay report", "",
         "run: %s  model: %s  budget/task: $%s" % (rep["ts"], rep["model"], rep["budget_per_task"]),
         "provisional candidates at run time: %d" % rep["provisional_candidates"],
         "",
         "treatment delivered: %s" % (
             "yes" if rep.get("treatment_delivered")
             else "NO - both arms ran the same prompt, so nothing was measured"),
         "",
         "| task | verdict | criteria off/on | exit off/on | turns off/on | cost off/on | drift |",
         "|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        o, n = r["off"], r["on"]
        L.append("| %s | %s | %s | %s/%s | %s/%s | %s/%s | %.2f |" % (
            r["id"], r["verdict"], criteria_cell(r), o["exit"], n["exit"],
            o.get("num_turns"), n.get("num_turns"),
            o.get("total_cost_usd"), n.get("total_cost_usd"), r["answer_drift"]))
    tally = rep.get("verdict_tally") or {}
    if tally:
        L += ["", "tally: " + ", ".join("%s x%d" % (k, tally[k]) for k in sorted(tally))]
    L += ["", "## Criteria", ""]
    for r in rep["rows"]:
        L.append("- **%s**: %s" % (r["id"], r["criteria"] or "(none recorded)"))
        for side in ("off", "on"):
            c = r["criteria_" + side]
            if not c.get("measurable"):
                continue
            bits = []
            if c.get("missing"):
                bits.append("missing " + ", ".join(c["missing"]))
            if c.get("forbidden"):
                bits.append("tripped " + ", ".join(c["forbidden"]))
            L.append("  - %s: %s" % (side, "; ".join(bits) if bits else "all groups found"))
    if rep["skipped"]:
        L += ["", "## Skipped (quarantine pre-check)", ""]
        for s in rep["skipped"]:
            L.append("- %s: %s" % (s["id"], s["reason"]))
    L += ["", "## Reading this",
          "",
          "Report-only by design. The verdict column is the claim, and four of its",
          "six branches say nothing happened or nothing could be seen: no treatment",
          "delivered, prose-only criteria, or a tie. met means every keyword group in",
          "criteria_keywords was found in the answer; drift is descriptive and never",
          "a verdict, because a rephrasing of a wrong answer is still a wrong answer.",
          "Negative turns delta means the memory-bearing run took fewer turns. Promote",
          "with sleepctl promote <id> only on evidence you would defend in review.",
          ""]
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return p
