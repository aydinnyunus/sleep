"""Tiered memory store with an expansion-only write policy.

Policy (the part that keeps this from becoming a self-poisoning loop):

  * Nothing is ever edited in place in the canonical store. New knowledge opens a
    new file; a superseded record keeps its text and gets a pointer.
  * The derived index is only mutated inside a managed marker block; every byte
    outside that block is preserved verbatim.
  * Buffer -> candidate -> canonical is a one-way ratchet, and the last step is
    only reachable by an explicit human command (`sleepctl promote`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time

from . import paths, safety

KINDS = ("fact", "preference", "procedure", "warning")

IDX_BEGIN = "<!-- sleep:begin (managed by sleepctl; lines here are appended, never rewritten) -->"
IDX_END = "<!-- sleep:end -->"


# ------------------------------------------------------------- frontmatter --
def emit(record: dict, body: str = "") -> str:
    order = ["id", "ts", "source", "trust", "kind", "status", "session", "cwd",
             "tags", "evidence", "supersedes", "superseded_by", "redacted", "notes"]
    # Known fields first in a stable order, then anything else the record carries.
    # Dropping the remainder silently used to eat the review audit trail: `reviewed`
    # and `promoted_to`/`promoted_at` are written by set_status and are not in `order`,
    # so a verdict looked like it had never been made.
    extra = sorted(k for k in record if k not in order and not k.startswith("_"))
    lines = ["---"]
    for k in order + extra:
        if k in record:
            v = record[k]
            if isinstance(v, (list, tuple)):
                v = "|".join(str(x) for x in v)
            lines.append("%s: %s" % (k, v))
    lines.append("---")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def parse(text: str) -> tuple[dict, str]:
    meta: dict = {}
    rest = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            block = text[4:end]
            rest = text[end + 4:].lstrip("\n")
            for line in block.splitlines():
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                k, v = k.strip(), v.strip()
                if k in ("tags", "evidence", "supersedes", "superseded_by"):
                    meta[k] = [x for x in v.split("|") if x]
                elif k == "redacted":
                    meta[k] = v == "true"
                else:
                    meta[k] = v
    return meta, rest.strip()


# --------------------------------------------------------------- capture ----
def capture(text: str, kind: str = "fact", source: str = "agent", session: str = "",
            cwd: str = "", evidence: list[str] | None = None,
            tags: list[str] | None = None) -> tuple[bool, str]:
    """T0: write a wake-time observation. Cheap, no LLM, disposable.

    Returns (accepted, id_or_reason). Refusals are the point: a secret or an
    injection-shaped string must not enter the pipeline at all.
    """
    text = (text or "").strip()
    if not text:
        return False, "empty"
    if kind not in KINDS:
        return False, "unknown kind %r" % kind
    red, was_red = safety.redact(text)
    reason = safety.quarantine_reason(red, source)
    rec = {
        "id": paths.new_id("b"),
        "ts": paths.now(),
        "kind": kind,
        "source": source if source in safety.SOURCES else "inferred",
        "session": session or os.environ.get("SLEEP_SESSION", ""),
        "cwd": cwd or os.getcwd(),
        "tags": "|".join(tags or []),
        "evidence": "|".join(evidence or []),
        "redacted": "true" if was_red else "false",
        "text": red,
    }
    if reason:
        rec["reason"] = reason
        with open(os.path.join(paths.quarantine_dir(), rec["id"] + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
        return False, "quarantined: " + reason
    with open(os.path.join(paths.buffer_dir(), (rec["session"] or "anon") + ".jsonl"), "a",
              encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    return True, rec["id"]


def read_buffer(session: str | None = None) -> list[dict]:
    out = []
    d = paths.buffer_dir()
    names = [session + ".jsonl"] if session else sorted(os.listdir(d))
    for name in names:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return out


def buffer_counts() -> dict:
    d = paths.buffer_dir()
    files = [f for f in os.listdir(d) if f.endswith(".jsonl")]
    return {"files": len(files),
            "records": len(read_buffer()),
            "bytes": sum(os.path.getsize(os.path.join(d, f)) for f in files)}


# ------------------------------------------------------- similarity/dedup ---
_WORD = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def nearest(text: str, pool: list[tuple[str, str]], threshold: float = 0.6) -> tuple[str, float]:
    best, score = "", 0.0
    for ident, other in pool:
        s = similarity(text, other)
        if s > score:
            best, score = ident, s
    return (best, score) if score >= threshold else ("", score)


# ------------------------------------------------------------- candidates ---
def add_candidate(text: str, kind: str = "fact", source: str = "agent",
                  trust: str = "observed", session: str = "", cwd: str = "",
                  evidence: list[str] | None = None, supersedes: list[str] | None = None,
                  notes: str = "") -> tuple[bool, str]:
    """T1: distilled, provisional. Never injected as a directive."""
    text = (text or "").strip()
    if not text:
        return False, "empty"
    red, was_red = safety.redact(text)
    reason = safety.quarantine_reason(red, source)
    if reason:
        rec = {"id": paths.new_id("q"), "ts": paths.now(), "reason": reason,
               "text": red, "source": source}
        with open(os.path.join(paths.quarantine_dir(), rec["id"] + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
        return False, "quarantined: " + reason

    pool = [(m["id"], m.get("_body", "")) for m in list_candidates(with_body=True)]
    dup, score = nearest(red, pool)
    if dup:
        return False, "duplicate of %s (%.2f); use `sleepctl supersede %s %s`" % (
            dup, score, dup, "new-text")

    rec = {
        "id": paths.new_id("c"),
        "ts": paths.now(),
        "source": source if source in safety.SOURCES else "inferred",
        "trust": safety.cap_trust(source, trust),
        "kind": kind if kind in KINDS else "fact",
        "status": "provisional",
        "session": session or os.environ.get("SLEEP_SESSION", ""),
        "cwd": cwd or os.getcwd(),
        "tags": "|".join([]),
        "evidence": "|".join(evidence or []),
        "supersedes": "|".join(supersedes or []),
        "superseded_by": "",
        "redacted": "true" if was_red else "false",
        "notes": notes.replace("\n", " "),
    }
    p = os.path.join(paths.candidates_dir(), rec["id"] + ".md")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(emit(rec, red))
    return True, rec["id"]


def list_candidates(status: str | None = None, with_body: bool = False) -> list[dict]:
    out = []
    d = paths.candidates_dir()
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        p = os.path.join(d, name)
        try:
            with open(p, encoding="utf-8") as fh:
                meta, body = parse(fh.read())
        except OSError:
            continue
        meta["_path"] = p
        if with_body:
            meta["_body"] = body
        if status and meta.get("status") != status:
            continue
        out.append(meta)
    return out


def get_candidate(cid: str) -> tuple[dict, str] | None:
    p = os.path.join(paths.candidates_dir(), cid + ".md")
    if not os.path.isfile(p):
        # Promoted records live in the archive; look there too so provenance
        # survives and a second promote attempt gets a real answer, not "missing".
        p = os.path.join(paths.archive_dir("candidates"), cid + ".md")
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return parse(fh.read())


def set_status(cid: str, status: str, extra: dict | None = None) -> bool:
    got = get_candidate(cid)
    if not got:
        return False
    meta, body = got
    meta["status"] = status
    meta["reviewed"] = paths.now()
    for k, v in (extra or {}).items():
        meta[k] = v
    with open(os.path.join(paths.candidates_dir(), cid + ".md"), "w", encoding="utf-8") as fh:
        fh.write(emit(meta, body))
    return True


# ------------------------------------------------------------ promotion -----
def slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "note"


def promote(cid: str, memory_dir: str | None = None, dry_run: bool = False) -> tuple[bool, str]:
    """T1 -> T2. Explicit human act. Creates a NEW file, never edits one."""
    got = get_candidate(cid)
    if not got:
        return False, "no such candidate: " + cid
    meta, body = got
    if meta.get("status") == "promoted":
        return False, "already promoted: " + cid
    src = meta.get("source", "inferred")
    if src != "user" and meta.get("trust") == "speculative":
        return False, ("refusing to persist speculative memory from source=%s; "
                       "re-record with the user's own words or run a replay first" % src)
    mdir = memory_dir or paths.memory_dir()
    name = "%s.md" % slugify(body.split(".")[0][:60] or cid)
    dest = os.path.join(mdir, name)
    # Expansion-only: if the name is taken, open a new file rather than merging.
    if os.path.exists(dest):
        dest = os.path.join(mdir, "%s-%s.md" % (slugify(body.split(".")[0][:60]), cid[-4:]))
    line = "- **%s**: [detail](%s). %s" % (
        os.path.basename(dest)[:-3], os.path.basename(dest),
        body.replace("\n", " ")[:220])
    if dry_run:
        return True, "would write %s (+ index line)" % dest
    os.makedirs(mdir, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(emit({k: v for k, v in meta.items() if not k.startswith("_")}))
        fh.write("\n")
    ok, why = append_index_line(mdir, line)
    set_status(cid, "promoted", {"promoted_to": dest, "promoted_at": paths.now()})
    shutil.move(os.path.join(paths.candidates_dir(), cid + ".md"),
                os.path.join(paths.archive_dir("candidates"), cid + ".md"))
    return True, "wrote %s; index %s" % (dest, why)


def append_index_line(memory_dir: str, line: str) -> tuple[bool, str]:
    """Append inside a managed block. Idempotent; never rewrites outside it."""
    idx = os.path.join(memory_dir, "MEMORY.md")
    existing = ""
    if os.path.isfile(idx):
        with open(idx, encoding="utf-8") as fh:
            existing = fh.read()
    key = re.sub(r"\s+", " ", line).strip()
    if key in re.sub(r"\s+", " ", existing):
        return True, "skipped (already indexed)"
    block_re = re.compile(re.escape(IDX_BEGIN) + r".*?" + re.escape(IDX_END), re.S)
    m = block_re.search(existing)
    if m:
        block = m.group(0)
        if not block.rstrip().endswith(IDX_END):
            new_block = block.rstrip() + "\n" + line
        else:
            new_block = block.replace(IDX_END, line + "\n" + IDX_END)
        updated = existing[:m.start()] + new_block + existing[m.end():]
    else:
        add = ("\n%s\n%s\n%s\n" % (IDX_BEGIN, line, IDX_END))
        updated = existing.rstrip("\n") + add
    tmp = idx + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(updated)
    os.replace(tmp, idx)
    return True, "appended"


def supersede(old_id: str, new_id: str) -> bool:
    return (set_status(old_id, "superseded", {"superseded_by": new_id})
            and set_status(new_id, "provisional", {"supersedes": old_id}))


# ------------------------------------------------------------ pruning -------
def prune_buffer(older_than_hours: float = 0.0, session: str | None = None) -> dict:
    """T0 reset: consumed scratch moves to archive. Nothing is deleted."""
    moved = []
    d = paths.buffer_dir()
    cutoff = time.time() - older_than_hours * 3600
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        if session and name != session + ".jsonl":
            continue
        p = os.path.join(d, name)
        if older_than_hours and os.path.getmtime(p) > cutoff:
            continue
        dest = os.path.join(paths.archive_dir("buffer"),
                            time.strftime("%Y%m%d-") + name)
        shutil.move(p, dest)
        moved.append(dest)
    return {"moved": moved, "remaining": buffer_counts()}


def doctor(memory_dir: str | None = None, targets: list[str] | None = None) -> list[dict]:
    """Read-only hygiene report.

    Two checks, both about content rather than formatting:
      - duplicated headings *inside one file* (a real structural smell; the same
        section name appearing in different files is just this repo's convention,
        so reporting that was noise that hid the findings worth reading)
      - a candidate that restates an existing memory file, which means the
        consolidation pass missed the already-known pool it was shown
    """
    mdir = memory_dir or paths.memory_dir()
    files = list(targets or [])
    if os.path.isdir(mdir):
        files += [os.path.join(mdir, f) for f in sorted(os.listdir(mdir)) if f.endswith(".md")]
    out = []
    bodies = {}
    for p in files:
        try:
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        bodies[p] = text
        seen = {}
        for m in re.finditer(r"^(#{1,3})\s+(.+?)\s*$", text, re.M):
            seen[m.group(2).strip()] = seen.get(m.group(2).strip(), 0) + 1
        for h, n in sorted(seen.items()):
            if n > 1:
                out.append({"issue": "duplicate-heading-in-file", "heading": h,
                            "count": n, "file": p})
    pool = [(c["id"], c.get("_body", "")) for c in list_candidates(with_body=True)]
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            sim = similarity(pool[i][1], pool[j][1])
            if sim >= 0.75:
                out.append({"issue": "near-duplicate-candidates",
                            "pair": [pool[i][0], pool[j][0]], "similarity": round(sim, 2)})
    for cid, body in pool:
        for path, text in bodies.items():
            # MEMORY.md is the index: it restates every entry by design, so scoring
            # a candidate against it would flag every promotion.
            if os.path.basename(path) == "MEMORY.md" or not text.strip():
                continue
            plain = re.sub(r"^#{1,6}\s+.*$", " ", text, flags=re.M)
            plain = re.sub(r"^---.*?^---\s*$", " ", plain, flags=re.S | re.M)
            sim = similarity(body[:600], plain[:1200])
            if sim >= 0.7:
                out.append({"issue": "candidate-duplicates-memory", "id": cid, "file": path,
                            "similarity": round(sim, 2)})
    return out
