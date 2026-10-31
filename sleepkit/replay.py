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
import re
import shutil
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
    usage = best.get("usage") if isinstance(best.get("usage"), dict) else {}
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
    metrics = {
        "condition": condition,
        "exit": res["exit"],
        "sandbox": sbx,
        "num_turns": j["num_turns"],
        "total_cost_usd": j["total_cost_usd"],
        "duration_ms": j["duration_ms"],
        "session_id": j["session_id"],
        "answer_chars": len(res["stdout"] or ""),
        "stdout_tail": (res["stdout"] or "")[-400:],
    }
    with open(os.path.join(sbx, "result.json"), "w", encoding="utf-8") as fh:
        json.dump({"task": task, "metrics": metrics}, fh, indent=2, sort_keys=True)
    return metrics


def run_suite(tasks_path: str = DEFAULT_TASKS, limit: int = 3, model: str = llm.DEFAULT_MODEL,
              budget: float = 0.50, timeout_s: int = 600) -> dict:
    tasks = [t for t in load_tasks(tasks_path) if "skipped" not in t][:limit]
    skipped = [t for t in load_tasks(tasks_path) if "skipped" in t]
    memory_text = _context_block("")
    rows = []
    for t in tasks:
        off = run_task(t, "off", "", model, budget, timeout_s)
        on = run_task(t, "on", memory_text, model, budget, timeout_s)
        delta = {}
        for k in ("num_turns", "total_cost_usd", "duration_ms"):
            a, b = off.get(k), on.get(k)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                delta[k] = round(b - a, 4)
        similarity = difflib.SequenceMatcher(
            None, off["stdout_tail"], on["stdout_tail"]).ratio()
        rows.append({"id": t["id"], "criteria": t.get("criteria", ""),
                     "off": off, "on": on, "delta": delta,
                     "output_similarity": round(similarity, 3)})
    rep = {"ts": paths.now(), "model": model, "budget_per_task": budget,
           "provisional_candidates": len(store_candidates()),
           "skipped": [{"id": s.get("id"), "reason": s["skipped"]} for s in skipped],
           "rows": rows}
    return rep


def write_report(rep: dict) -> str:
    p = os.path.join(paths.reports_dir(), time.strftime("%Y-%m-%d-%H%M%S") + "-replay.md")
    L = ["# Sleep replay report", "",
         "run: %s  model: %s  budget/task: $%s" % (rep["ts"], rep["model"], rep["budget_per_task"]),
         "provisional candidates at run time: %d" % rep["provisional_candidates"],
         "",
         "| task | exit off/on | turns off/on | cost off/on | delta turns | out sim |",
         "|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        o, n, d = r["off"], r["on"], r["delta"]
        L.append("| %s | %s/%s | %s/%s | %s/%s | %s | %.2f |" % (
            r["id"], o["exit"], n["exit"], o.get("num_turns"), n.get("num_turns"),
            o.get("total_cost_usd"), n.get("total_cost_usd"),
            d.get("num_turns", "n/a"), r["output_similarity"]))
    L += ["", "## Criteria", ""]
    for r in rep["rows"]:
        L.append("- **%s**: %s" % (r["id"], r["criteria"] or "(none recorded)"))
    if rep["skipped"]:
        L += ["", "## Skipped (quarantine pre-check)", ""]
        for s in rep["skipped"]:
            L.append("- %s: %s" % (s["id"], s["reason"]))
    L += ["", "## Reading this",
          "",
          "Report-only by design. Negative `delta turns` means the memory-bearing",
          "run took fewer turns; a positive or noisy delta is not evidence that the",
          "memory is wrong, only that this suite cannot show it helping. Promote with",
          "`sleepctl promote <id>` only on evidence you would defend in review.",
          ""]
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return p
