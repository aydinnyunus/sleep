"""Wake buffer -> provisional candidates, with the policy kept in Python.

The model's job is only to *propose* text. It writes proposals as plain files in
$SLEEP_HOME/pending/; Python then ingests them through the same gates as any
other write (redaction, quarantine, trust ceiling, dedupe). That way a bad or
injected proposal cannot skip a check, because the checks are not in the prompt.
"""
from __future__ import annotations

import os
import re
import time
import shutil
from datetime import datetime

from . import llm, paths, safety, store

_HDR = re.compile(r"^(source|kind|trust|evidence)\s*:\s*(.+)$", re.M)


def pending_dir() -> str:
    d = os.path.join(paths.home(), "pending")
    os.makedirs(d, exist_ok=True)
    return d


def _fmt_buffer(records: list[dict], limit: int = 120) -> str:
    lines = []
    for r in records[-limit:]:
        lines.append("- [%s|%s] %s" % (r.get("kind", "fact"), r.get("source", "?"),
                                       r.get("text", "").replace("\n", " ")))
    return "\n".join(lines)


def existing_pool() -> str:
    lines = []
    for c in store.list_candidates(with_body=True):
        lines.append("- id=%s status=%s :: %s" % (c.get("id"), c.get("status"),
                                                  c.get("_body", "").replace("\n", " ")[:160]))
    return "\n".join(lines)


PROPOSAL_SEP = "===PROPOSAL==="
MIN_BODY = 30


def distill(session: str | None = None, model: str = llm.DEFAULT_MODEL,
            budget: float = 0.5, prompt_file: str | None = None,
            dry_run: bool = False) -> dict:
    recs = store.read_buffer(session)
    if not recs:
        return {"ok": True, "reason": "buffer empty", "proposals": 0, "accepted": 0,
                "consumed": False}
    if dry_run:
        return {"ok": True, "dry_run": True, "buffer_records": len(recs),
                "would_send": _fmt_buffer(recs)[:2000]}
    pf = prompt_file or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "prompts", "consolidate.md")
    user = (
        "Wake-phase observations from this session (T0 buffer):\n\n%s\n\n"
        "Already-known memory (do NOT restate these; only supersede or add):\n\n%s\n\n"
        "Emit your proposals now, to stdout, in the format from the output contract."
        % (_fmt_buffer(recs), existing_pool() or "(none)"))
    # Read-only on purpose. Proposals arrive on stdout and become candidates only
    # after ingest_records() has applied the gates, so a run that goes wrong cannot
    # leave files behind that anything later trusts.
    res = llm.run(system_prompt_file=pf, user_prompt=user,
                  add_dirs=[paths.buffer_dir()],
                  allowed_tools="Read", model=model, budget_usd=budget)
    _blocks, how = split_blocks(res["stdout"] or "")
    ing = ingest_records(parse_stdout(res["stdout"] or ""), session=session or "",
                         cwd=os.getcwd())
    ing["parse"] = {"mode": how, "blocks": len(_blocks)}
    if res["exit"] == 0 and not ing["proposals"] and how == "none":
        # The model answered and nothing in the answer matched the contract. Say so:
        # an empty night and a swallowed night are indistinguishable from the outside,
        # and only one of them is worth reading the log for.
        ing["warn"] = "the model answered, but no block matched the proposal contract"
    files = ingest_pending(session=session or "")
    ing["proposals"] += files["proposals"]
    ing["accepted"] += files["accepted"]
    ing["refused"] += files["refused"]
    ing["accepted_ids"] += files["accepted_ids"]
    ing["llm_exit"] = res["exit"]
    # The tail lands in state.json, where it outlives the night. Redact on the way in:
    # a model that echoes back a credential must not have it filed in plain text.
    ing["llm_tail"] = safety.redact((res["stdout"] or "")[-400:])[0]
    ing["buffer_records"] = len(recs)
    # "consumed" is what entitles a sleep to prune: the pass produced proposals.
    ing["consumed"] = bool(res["exit"] == 0 and ing["proposals"])
    if res["exit"] != 0:
        ing["ok"] = False
    return ing


def parse_proposal(text: str) -> dict:
    """Split one proposal into header fields + body. Headers are optional."""
    fields = {"source": "agent", "kind": "fact", "trust": "observed", "evidence": ""}
    for m in _HDR.finditer(text):
        fields[m.group(1)] = m.group(2).strip()
    body = _HDR.sub("", text).strip()
    return {"fields": fields, "body": body}


# The contract asks for a line holding exactly the marker above. Real output arrives
# messier: padded with spaces, wrapped in a code fence, or missing while the blocks are
# still there. Splitting on one literal string turns a paid call into zero candidates
# with nothing to show for it, so the split is tolerant and a header-shaped block is a
# fallback. Neither decides what becomes memory: every block still goes through
# ingest_records(), which is where the gates live.
_SEP_LINE = re.compile(r"^[^A-Za-z0-9\n]*PROPOSAL[^A-Za-z0-9\n]*$", re.I | re.M)
_HDR_LINE = re.compile(r"^(source|kind|trust|evidence)\s*:.*$", re.M)


def split_blocks(text: str) -> tuple:
    """(blocks, how): chunks that could be proposals, and how they were located.

    "marker" when separator lines are present, "header" when a run of contract header
    lines is there but the marker was not printed, "none" when there is neither. The
    mode travels back because the difference matters at 03:41: a night where the model
    had nothing to say is fine, a night where a formatting slip swallowed the answer is
    a paid call wasted, and from the outside the two look identical.

    The header fallback is line-based rather than a split on blank lines, because a
    blank line is exactly where the contract puts the headers and the body: splitting
    there would separate a proposal from its own text. The body is the first paragraph
    after the header run, which keeps trailing commentary out of the candidate.
    """
    text = text or ""
    if _SEP_LINE.search(text):
        return [b for b in re.split(_SEP_LINE, text)[1:] if b.strip()], "marker"

    lines = text.split("\n")
    hdr = [bool(_HDR_LINE.match(line)) for line in lines]
    blocks, i = [], 0
    while i < len(lines):
        if not hdr[i]:
            i += 1
            continue
        k = i
        while k < len(lines) and hdr[k]:
            k += 1
        head = lines[i:k]
        j = k
        while j < len(lines) and not lines[j].strip():
            j += 1
        body = []
        while j < len(lines) and lines[j].strip():
            body.append(lines[j])
            j += 1
        if body:
            blocks.append("\n".join(head + [""] + body).strip())
            i = j
        else:
            i = k
    return (blocks, "header") if blocks else ([], "none")


def parse_stdout(text: str) -> list:
    """Turn model output into proposal blocks, marker-delimited or header-shaped.

    Two rules, both about not trusting prose as data: commentary before the first
    block is discarded, and a block with no real body is dropped rather than
    guessed at. Output with neither a marker nor a contract header yields nothing,
    which is the correct outcome for a session that had nothing worth keeping.
    """
    blocks, _how = split_blocks(text)
    out = []
    for chunk in blocks:
        chunk = chunk.strip()
        if chunk.lower().startswith("```"):
            chunk = chunk.split("\n", 1)[1] if "\n" in chunk else ""
        parsed = parse_proposal(chunk)
        body = parsed["body"].strip().strip("`").strip()
        # Models mirror the opening marker with a closing fence. That fence is
        # protocol, not memory: stored verbatim it becomes the last line of a
        # T1 record and then the last line an agent reads months later.
        lines = body.split("\n")
        while lines and lines[-1].strip() and set(lines[-1].strip()) <= {"="}:
            lines.pop()
        while lines and lines[0].strip() and set(lines[0].strip()) <= {"="}:
            lines.pop(0)
        body = "\n".join(lines).strip()
        if len(body) >= MIN_BODY:
            out.append({"fields": parsed["fields"], "body": body})
    return out


def ingest_records(parsed: list, session: str = "", cwd: str = "") -> dict:
    """Run proposals through the gates. Policy lives here, not in the prompt."""
    accepted, refused = [], []
    for item in parsed:
        f = item["fields"]
        ok, out = store.add_candidate(
            item["body"], kind=f.get("kind", "fact"), source=f.get("source", "agent"),
            trust=f.get("trust", "observed"),
            session=session or os.environ.get("SLEEP_SESSION", ""),
            cwd=cwd or os.getcwd(),
            evidence=[x for x in f.get("evidence", "").split(",") if x])
        (accepted if ok else refused).append({"body": item["body"][:60], "result": out})
    return {"proposals": len(parsed), "accepted": len(accepted), "refused": refused,
            "accepted_ids": [a["result"] for a in accepted]}


def ingest_pending(session: str = "", cwd: str = "") -> dict:
    """Secondary channel: proposal files dropped into pending/ by hand or by an
    older run. Same gates, same refusal, then archived either way."""
    d = pending_dir()
    names = sorted(n for n in os.listdir(d) if n.endswith((".txt", ".md")))
    parsed = []
    for name in names:
        p = os.path.join(d, name)
        try:
            with open(p, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            continue
        item = parse_proposal(raw)
        item["file"] = name
        parsed.append(item)
    result = ingest_records(parsed, session=session, cwd=cwd)
    for item, name in [(i, i["file"]) for i in parsed]:
        try:
            shutil.move(os.path.join(d, name),
                        os.path.join(paths.archive_dir(), "pending-" + name))
        except OSError:
            pass
    return result


# --- sleep policy: when is it worth running at all? ------------------------

LOCK_STALE_SECONDS = 1800


def _lock_path() -> str:
    return os.path.join(paths.home(), ".sleep.lock")


def acquire_lock() -> bool:
    """O_EXCL is the lock. A stale lock is broken rather than honored, because a
    consolidation killed mid-run must not disable memory forever."""
    lock = _lock_path()
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = time.time() - os.path.getmtime(lock)
        except OSError:
            return True
        if age < LOCK_STALE_SECONDS:
            return False
        os.unlink(lock)
        return acquire_lock()
    with os.fdopen(fd, "w") as handle:
        handle.write(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        os.unlink(_lock_path())
    except OSError:
        pass


def hours_since(ts):
    """Hours since an ISO timestamp written by paths.now(). Unparseable input
    returns None, which means 'no constraint' rather than 'block forever'."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600.0


def sleep_gate(records: int, min_records: int, min_interval_hours: float) -> tuple[bool, str]:
    state = paths.read_state()
    last = state.get("last_sleep")
    if records < min_records:
        return False, f"buffer has {records} record(s), needs {min_records}"
    elapsed = hours_since(last)
    if elapsed is not None and elapsed < min_interval_hours:
            return False, f"last sleep {elapsed:.1f}h ago, minimum interval {min_interval_hours:g}h"
    return True, "policy satisfied"


def run_sleep(session=None, model=None, budget_usd=1.0, min_records=3,
              min_interval_hours=6.0, force=False, dry_run=False):
    """The entry point a hook or a cron job calls. Cheap when there is nothing
    to consolidate: one stat and one JSON read, no model call."""
    records = store.buffer_counts()["records"]
    ok, reason = sleep_gate(records, min_records, min_interval_hours)
    if not ok and not force:
        return {"action": "skipped", "reason": reason, "buffer_records": records}
    if dry_run:
        return {"action": "would-run", "reason": reason if ok else reason + " (forced)",
                "buffer_records": records}
    if not acquire_lock():
        return {"action": "locked", "reason": "another consolidation is running",
                "buffer_records": records}
    try:
        result = distill(session=session, model=model, budget=budget_usd)
        state = paths.read_state()
        state["last_sleep"] = paths.now()
        state["sleep_count"] = int(state.get("sleep_count", 0)) + 1
        state["last_sleep_result"] = {k: result.get(k) for k in
                                      ("proposals", "accepted", "refused", "llm_exit")}
        paths.write_state(state)
        # Synaptic pruning only after a consolidation that actually produced
        # proposals. Pruning a buffer the model never read would silently discard
        # the session's observations.
        if result.get("consumed"):
            result["pruned"] = store.prune_buffer(older_than_hours=0.0)
        else:
            result["pruned"] = {"skipped": True, "reason": "no proposals consumed"}
        result["action"] = "ran"
        return result
    finally:
        release_lock()
