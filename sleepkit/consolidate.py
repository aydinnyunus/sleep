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


def buffer_projects(records: list) -> list[str]:
    """Canonical project addresses present in one consolidation batch."""
    projects = set()
    for rec in records:
        cwd = (rec.get("cwd") or "").strip()
        if cwd and os.path.abspath(cwd) != os.path.sep:
            projects.add(os.path.realpath(os.path.abspath(cwd)))
    return sorted(projects)


def buffer_cwd(records: list) -> str:
    """The only project represented by a batch, or empty when mixed/unknown.

    Canonical paths decide whether the batch is mixed, but the recorded spelling is
    preserved. On macOS, realpath adds a `/private` prefix to temporary directories;
    rewriting that address would make an otherwise matching project unreachable.
    """
    addresses = []
    for rec in records:
        cwd = (rec.get("cwd") or "").strip()
        if cwd and os.path.abspath(cwd) != os.path.sep:
            addresses.append((os.path.normpath(os.path.abspath(cwd)),
                              os.path.realpath(os.path.abspath(cwd))))
    if len({canonical for _original, canonical in addresses}) != 1:
        return ""
    return addresses[0][0] if addresses else ""


def _project_groups(records: list) -> tuple:
    """Split the buffer into (projects, unaddressed).

    Each project is (recorded_cwd, [records]): records are grouped by their
    canonical project address, but the recorded spelling is kept for filing,
    because on macOS realpath adds a `/private` prefix that would otherwise make
    the address unreachable. Records with no cwd (or a root cwd) carry no project
    address and are returned separately: they cannot be filed somewhere a session
    will look, so they are left in the buffer rather than dangerously filed.
    """
    groups = {}
    unaddressed = []
    for rec in records:
        cwd = (rec.get("cwd") or "").strip()
        if not cwd or os.path.abspath(cwd) == os.path.sep:
            unaddressed.append(rec)
            continue
        address = os.path.normpath(os.path.abspath(cwd))
        key = os.path.realpath(os.path.abspath(cwd))
        if key not in groups:
            groups[key] = (address, [])
        groups[key][1].append(rec)
    return list(groups.values()), unaddressed


def _consolidate_project(recs, origin, session, model, budget, prompt_file) -> dict:
    """One consolidation pass over a single project's records: the model proposes,
    Python gates, and candidates are filed under that project's recorded cwd."""
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
                         cwd=origin)
    ing["parse"] = {"mode": how, "blocks": len(_blocks)}
    if res["exit"] == 0 and not ing["proposals"] and how == "none":
        # The model answered and nothing in the answer matched the contract. Say so:
        # an empty night and a swallowed night are indistinguishable from the outside,
        # and only one of them is worth reading the log for.
        ing["warn"] = "the model answered, but no block matched the proposal contract"
    files = ingest_pending(session=session or "", cwd=origin)
    ing["proposals"] += files["proposals"]
    ing["accepted"] += files["accepted"]
    ing["refused"] += files["refused"]
    ing["accepted_ids"] += files["accepted_ids"]
    ing["llm_exit"] = res["exit"]
    # The tail lands in state.json, where it outlives the night. Redact on the way in:
    # a model that echoes back a credential must not have it filed in plain text.
    ing["llm_tail"] = safety.redact((res["stdout"] or "")[-400:])[0]
    ing["consumed"] = bool(res["exit"] == 0 and ing["proposals"])
    if res["exit"] != 0:
        ing["ok"] = False
    return ing


def distill(session: str | None = None, model: str | None = None,
            budget: float = 0.5, prompt_file: str | None = None,
            dry_run: bool = False) -> dict:
    model = llm.resolve_model(model)
    recs = store.read_buffer(session)
    if not recs:
        return {"ok": True, "reason": "buffer empty", "proposals": 0, "accepted": 0,
                "consumed": False}
    projects, unaddressed = _project_groups(recs)
    if dry_run:
        return {"ok": True, "dry_run": True, "buffer_records": len(recs),
                "projects": [o for o, _ in projects], "unaddressed": len(unaddressed),
                "would_send": _fmt_buffer(recs)[:2000]}
    if not projects:
        return {"ok": False, "reason": "no project address in buffer",
                "buffer_records": len(recs), "proposals": 0, "accepted": 0,
                "refused": [], "accepted_ids": [], "llm_exit": None, "consumed": False}
    # One consolidation pass per project, each filed under that project's own recorded
    # cwd. A mixed buffer used to fail closed because a single `origin` cannot be both
    # projects; running per-project lets each project's truths land where their own
    # sessions will look, and unaddressed records stay in the buffer (never pruned).
    if len(projects) == 1:
        ing = _consolidate_project(projects[0][1], projects[0][0], session,
                                   model, budget, prompt_file)
    else:
        ing = {"ok": True, "proposals": 0, "accepted": 0, "refused": [],
               "accepted_ids": [], "llm_exit": None, "consumed": False,
               "per_project": []}
        any_bad = False
        for origin, recs_proj in projects:
            sub = _consolidate_project(recs_proj, origin, session, model, budget,
                                       prompt_file)
            sub["origin"] = origin
            sub["buffer_records"] = len(recs_proj)
            ing["per_project"].append(sub)
            ing["proposals"] += sub["proposals"]
            ing["accepted"] += sub["accepted"]
            ing["refused"] += sub["refused"]
            ing["accepted_ids"] += sub["accepted_ids"]
            if sub.get("llm_exit") != 0:
                any_bad = True
            ing["consumed"] = ing["consumed"] or sub["consumed"]
        if any_bad:
            ing["ok"] = False
    ing["buffer_records"] = len(recs)
    ing["unaddressed"] = len(unaddressed)
    # "consumed" is what entitles a sleep to prune: the pass produced proposals.
    # Unaddressed records were never read by any pass, so they must not be pruned.
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
        parts = re.split(_SEP_LINE, text)
        blocks = [b for b in parts[1:] if b.strip()]
        # Some models use the marker as a closing fence only. Keep a leading
        # contract-shaped proposal, but still discard ordinary preamble.
        leading = parts[0]
        first_header = _HDR_LINE.search(leading)
        if first_header:
            blocks.insert(0, leading[first_header.start():])
        return blocks, "marker"

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


def ingest_records(parsed: list, session: str = "", cwd: str = "",
                   honor_provenance: bool = False) -> dict:
    """Run proposals through the gates. Model text cannot assign its own provenance.

    The default is the stdout channel, so Python stamps every proposal agent/observed.
    Only ingest_pending(), the explicit hand-authored file channel, may honor source and
    trust headers. Policy lives here rather than in a prompt a model can rewrite.
    """
    accepted, refused = [], []
    for item in parsed:
        f = item["fields"]
        source = f.get("source", "agent") if honor_provenance else "agent"
        trust = f.get("trust", "observed") if honor_provenance else "observed"
        ok, out = store.add_candidate(
            item["body"], kind=f.get("kind", "fact"), source=source, trust=trust,
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
    result = ingest_records(parsed, session=session, cwd=cwd, honor_provenance=True)
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
