"""Between capture and consolidation: what did the human say more than once?

Deterministic on purpose. A sentence that comes back in a second, unrelated session is
the cheapest available evidence that it is a standing preference and not one-off context,
and that test needs no model, no budget and no network - so it still runs on a night when
the model is unreachable, and its output is the person's own words rather than a paraphrase.

Two outputs, both provisional:
  - a T1 candidate per repeated instruction (trust=observed, never verified: repetition is
    not confirmation, and `sleepctl promote` stays the human act that makes it one)
  - a replay task proposal, written aside from replay/tasks.json and never merged into it,
    so the measured-promotion harness can be fed by use instead of by hand-written guesses
"""
from __future__ import annotations

import json
import os
import re

from . import paths, safety, store

MIN_CHARS = 12
MAX_CHARS = 2000
# One instruction repeated inside a single session is a repeat, not a pattern: it is the
# cross-session requirement that makes the signal worth a candidate.
MIN_SESSIONS = 2

_PUNCT = re.compile(r"[\s\u00a0]+")


def corpus() -> list:
    """Every T0 record still on disk: the live buffer plus every archived generation."""
    out = []
    sources = [paths.buffer_dir()]
    arch = paths.archive_dir("buffer")
    if os.path.isdir(arch):
        sources.append(arch)
    for d in sources:
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            p = os.path.join(d, name)
            try:
                with open(p, encoding="utf-8") as fh:
                    lines = fh.readlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    rec["_file"] = p
                    out.append(rec)
    return out


def normalize(text: str) -> str:
    """Form used for equality: case and spacing folded, trailing punctuation dropped.

    Deliberately literal. Stemming or fuzzy grouping would let two different instructions
    collapse into one, and a wrong memory is worse than a missed one.
    """
    t = _PUNCT.sub(" ", (text or "").strip()).lower()
    return t.strip(" .!?:;\u2014-")


def instructions(records=None, min_sessions: int = MIN_SESSIONS,
                 min_chars: int = MIN_CHARS, max_chars: int = MAX_CHARS) -> list:
    """Instructions the human's own hand said in more than one session.

    `source=user` is the transport's own claim (hookio.provenance demotes anything that
    arrived by rpc or extension delivery), and `relayed` is store.capture's shape stamp for
    the case the transport cannot see: a delegate brief or a probe handed to the CLI, which
    reaches the hook as a typed prompt. Both must pass, or a machine instruction becomes a
    preference the agent keeps quoting back.
    """
    recs = corpus() if records is None else records
    groups = {}
    for rec in recs:
        if rec.get("source") != "user" or rec.get("relayed"):
            continue
        text = (rec.get("text") or "").strip()
        if safety.relay_reason(text):
            # Records captured before store.capture stamped them carry no flag, so the
            # shape test runs again over history: the archive is mostly probe traffic.
            continue
        if len(text) < min_chars or len(text) > max_chars:
            continue
        normalized = normalize(text)
        if not normalized:
            continue
        cwd = (rec.get("cwd") or "").strip()
        project = os.path.realpath(os.path.abspath(cwd)) if cwd else ""
        key = (normalized, project)
        g = groups.setdefault(key, {"text": text, "ids": [], "sessions": [], "cwds": [], "ts": []})
        g["ids"].append(rec.get("id", ""))
        g["sessions"].append(rec.get("session") or rec.get("_file", ""))
        g["cwds"].append(rec.get("cwd", ""))
        g["ts"].append(rec.get("ts", ""))
    rows = []
    for key, g in groups.items():
        sessions = [s for s in dict.fromkeys(g["sessions"]) if s]
        if len(sessions) < min_sessions:
            continue
        rows.append({
            "key": key[0],
            "text": g["text"],
            "count": len(g["ids"]),
            "sessions": sessions,
            "cwd": _dominant(g["cwds"]),
            "ids": [i for i in g["ids"] if i][:8],
            "first": min(t for t in g["ts"] if t) if any(g["ts"]) else "",
            "last": max(t for t in g["ts"] if t) if any(g["ts"]) else "",
        })
    rows.sort(key=lambda r: (-r["count"], r["key"]))
    return rows


def _dominant(values: list) -> str:
    """The project this instruction was said in; empty when it was said in several."""
    vals = [v.rstrip("/") for v in values if v and v.rstrip("/") != ""]
    if not vals:
        return ""
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    top = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))
    return top[0] if top[1] > 0 else ""


def task_id(text: str) -> str:
    return "mined-" + re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def task_proposals(rows: list) -> list:
    """Replay tasks that would ask the model to honour each mined instruction.

    `criteria_keywords` is left out on purpose: a keyword set written by the same process
    that proposes the task would grade its own homework. Such a task scores `not measurable`
    until a human adds the keywords, which is the honest state of the claim.
    """
    out = []
    for r in rows:
        out.append({
            "id": task_id(r["text"]),
            "prompt": "Recall what I have told you about this, and answer without asking "
                      "a clarifying question. My standing instruction, said in %d sessions: %s"
                      % (len(r["sessions"]), r["text"]),
            "criteria": "The answer follows the stated instruction: %s" % r["text"],
            "source": "mine",
            "said_in": len(r["sessions"]),
            "cwd": r["cwd"],
        })
    return out


def write_task_proposals(tasks: list) -> str:
    """Park proposals beside the task file. Never merged into replay/tasks.json by code."""
    d = os.path.join(paths.home(), "replay")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "task-proposals.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"generated": paths.now(),
                   "note": "written by `sleepctl mine`; promote a task into replay/tasks.json "
                           "by hand, and add criteria_keywords there or it scores not measurable",
                   "tasks": tasks}, fh, indent=2, sort_keys=True)
    return p


def mine(min_sessions: int = MIN_SESSIONS, dry_run: bool = False) -> dict:
    """Emit mined instructions as T1 candidates + replay task proposals."""
    rows = instructions(min_sessions=min_sessions)
    rep = {"records": len(corpus()), "instructions": len(rows), "emitted": [],
           "already_there": [], "dry_run": dry_run, "task_proposals": 0,
           "proposal_file": ""}
    tasks = task_proposals(rows)
    rep["task_proposals"] = len(tasks)
    if rows and not dry_run:
        rep["proposal_file"] = write_task_proposals(tasks)
    for r in rows:
        if dry_run:
            rep["emitted"].append("(would add) " + r["text"][:70])
            continue
        ok, detail = store.add_candidate(
            text=r["text"], kind="preference", source="user", trust="observed",
            cwd=r["cwd"], evidence=r["ids"],
            notes="mined: said verbatim in %d sessions" % len(r["sessions"]))
        if ok:
            rep["emitted"].append(detail)
        else:
            rep["already_there"].append(detail)
    return rep
