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

import itertools
import json
import os
import re
import shutil
import time
from datetime import datetime

from . import paths, safety

KINDS = ("fact", "preference", "procedure", "warning")

# Doctor flags a provisional candidate as stale when its last activity (creation
# or the most recent rediscovery) is older than this.
STALE_CANDIDATE_WEEKS = 4.0
# A provisional record nobody has asked for and nobody has re-learned is dead weight:
# it sits in the injected context of every session that shares its project. Named
# separately from STALE_CANDIDATE_WEEKS because "nobody re-learned it" and "nobody ever
# looked at it" are different facts and only the second one means the queue is filling
# with things that were never useful. Report only, like every other doctor line.
UNUSED_CANDIDATE_WEEKS = 8.0

# What the two constants above are a default FOR. A record's kind is not decoration:
# a warning that nobody re-learned in a month is not the same debt as a preference
# nobody re-learned in a month, and the flat constants made the four kinds behave
# identically. Each row is (stale_weeks, unused_weeks) and the rationale travels with
# the number, because a bare constant invites the next reader to "tidy" it back.
#
#   fact       4 / 8   the old defaults; a fact is cheap to re-learn and cheap to drop
#   preference 13 / 26 a preference is a statement about a person, and a person is
#                      slow. Expiring one after four quiet weeks deletes a true thing
#   procedure  8 / 12  durable but testable: a procedure that stops being exercised is
#                      suspect sooner than a stated preference, later than a fact
#   warning    52 / 52 an environment warning ("X does not fire on this engine") stays
#                      true long after nobody has touched it, and the cost of
#                      re-deriving it is paid in failed attempts
#
# Both remain REPORT-ONLY. Nothing here drops, demotes or expires a record; it decides
# when doctor names one. Use lifetime_for() rather than reading the table directly.
KIND_LIFETIME_WEEKS = {
    "fact": (4.0, 8.0),
    "preference": (13.0, 26.0),
    "procedure": (8.0, 12.0),
    "warning": (52.0, 52.0),
}


def lifetime_for(kind: str, default_weeks: float = 0.0) -> tuple[float, float, str]:
    """(stale_weeks, unused_weeks, kind) for one record's kind.

    An unknown or missing kind gets the historical flat default, so a record written
    by an older version, or by hand, is judged exactly as it was before this table
    existed. The third element is the kind actually used, so a caller can print what
    it judged against without re-deriving the fallback.
    """
    row = KIND_LIFETIME_WEEKS.get(str(kind or "").strip().lower())
    if row:
        return row[0], row[1], str(kind)
    return STALE_CANDIDATE_WEEKS, UNUSED_CANDIDATE_WEEKS, str(kind or "")


def _hours_since(ts: str) -> float | None:
    """Hours since an ISO timestamp written by paths.now(). Unparseable input
    returns None, meaning 'no constraint' rather than 'flag everything'."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600.0

IDX_BEGIN = "<!-- sleep:begin (managed by sleepctl; lines here are appended, never rewritten) -->"
IDX_END = "<!-- sleep:end -->"


# ------------------------------------------------------------- frontmatter --
def emit(record: dict, body: str = "") -> str:
    order = ["id", "ts", "event_at", "source", "trust", "kind", "status", "session", "cwd",
             "tags", "evidence", "supersedes", "superseded_by",
             "valid_from", "valid_until", "redacted", "notes"]
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
    relay = safety.relay_reason(red) if source == "user" else None
    machine = safety.machine_driven() if source == "user" else ""
    if relay or machine:
        # A host-spawned session is machine-authored even when its wording looks human.
        # It loses the user exemption and mine() will not count it as a preference.
        relay = relay or "machine-driven session (%s)" % machine
        source = "inferred"
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
    if relay:
        rec["relayed"] = relay
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


def buffer_session_counts() -> dict:
    """{session: record count} for the T0 buffer, one entry per file.

    A pass consolidates ONE session, so this is the count a window means anything
    against: `buffer_counts()` totals every file in the directory and answers a
    different question ("how much scratch is on disk").
    """
    d = paths.buffer_dir()
    out = {}
    for name in sorted(os.listdir(d)):
        if not name.endswith(".jsonl"):
            continue
        out[name[:-6]] = len(read_buffer(name[:-6]))
    return out


# ------------------------------------------------------- similarity/dedup ---

# A proposal that paraphrases an already-refused candidate must not reborn as a
# fresh provisional idea the reviewer then judges all over again. Token-set
# Jaccard (the general duplicate bar) falls off too fast to catch even a close
# restate, so against a *refused* idea we compare by character bigrams, which
# tolerate synonym swaps and reordering, and block on a lower bar. The idea was
# already judged and rejected, so colliding with it should refuse the write.
REFUSED_DUP_THRESHOLD = 0.42

# A contradiction is not a synonym for "similar": near-duplicates restate the same
# fact, contradicting records assert opposite things about the same subject. Only
# pairs that share most of their wording AND carry an explicit correction marker on
# one side are reported, so the doctor stays a signal rather than a guess.
CONTRADICTION_OVERLAP = 0.5
_CORRECTION = re.compile(
    r"\b(?:no longer|not any ?more|instead of|rather than|replaced|deprecated|"
    r"obsolete|stopped? (?:using|working)|used to|previously|but now|do(?:es)? not|"
    r"don't|doesn't|isn't|aren't|wasn't|weren't)\b", re.I)

_WORD = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def _bigrams(text: str) -> set:
    """Character bigrams of the lowercased text, for paraphrase-resistant
    similarity. A pair of observations that differ only in wording shares most
    bigrams, where token Jaccard already diverged."""
    t = (text or "").lower()
    return set(t[i:i + 2] for i in range(len(t) - 1))


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


def ngram_similarity(a: str, b: str) -> float:
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga | gb))


def refused_collision(text: str, threshold: float = REFUSED_DUP_THRESHOLD) -> tuple[str, float]:
    """Best matching already-refused candidate body, or ("", 0.0) under threshold.

    Refusals are the one class we hard-block aggressively: the idea was judged and
    rejected, so coming back paraphrased must not become a new provisional review.
    Bigram Jaccard separates a restate of the same fact from a different fact about
    the same subject far better than token Jaccard on short observations, which is
    why this gate uses it instead of similarity().
    """
    best, score = "", 0.0
    for m in list_candidates(with_body=True):
        if m.get("status") != "refused":
            continue
        s = ngram_similarity(text, m.get("_body", ""))
        if s > score:
            best, score = m["id"], s
    return (best, score) if score >= threshold else ("", score)


# ------------------------------------------------------------- candidates ---
def _record_rediscovery(cid: str, session: str) -> str | None:
    """Record that an agent re-learned an existing candidate in another session.

    Returns a human message when the session differs from the ones already recorded
    (a genuine cross-session rediscovery), or None when it is the same session again
    (that is a repeat, not a pattern, and the plain duplicate message applies).
    """
    got = get_candidate(cid)
    if not got:
        return None
    meta, body = got
    # The record's own session is the baseline: a repeat from the same session the
    # fact was first filed in is a repeat, not a rediscovery. Only a *different*
    # session counts.
    sessions = [s for s in meta.get("rediscovered_in", "").split("|") if s]
    own = (meta.get("session", "") or "").strip()
    if own and own not in sessions:
        sessions.append(own)
    this = (session or "").strip()
    if not this:
        return None
    if this in sessions:
        return None
    sessions.append(this)
    n = int(meta.get("rediscovered", 0) or 0) + 1
    meta["rediscovered"] = str(n)
    meta["rediscovered_in"] = "|".join(sessions)
    meta["rediscovered_at"] = paths.now()
    loc = paths.candidates_dir()
    if not os.path.isfile(os.path.join(loc, cid + ".md")):
        p = os.path.join(paths.archive_dir("candidates"), cid + ".md")
        if os.path.isfile(p):
            loc = paths.archive_dir("candidates")
    with open(os.path.join(loc, cid + ".md"), "w", encoding="utf-8") as fh:
        fh.write(emit(meta, body))
    return "duplicate of %s (rediscovered in a later session: %d distinct sessions now; use `sleepctl show %s` to review)" % (
        cid, n, cid)


def record_injection(ids: list[str]) -> int:
    """Stamp `injected`/`injected_at` on the records a session's context just carried.

    A record that no session has ever been served is a different thing from one that
    was served and ignored, and only the record itself can tell them apart. Writing the
    counter here (not in the reporter) keeps the evidence next to the fact it is about:
    the injected line a session saw is the same event the doctor later reads.
    Ids that no longer resolve are skipped; a failed stamp never breaks an injection
    that already printed, so the caller may ignore the return value.
    """
    n = 0
    for cid in ids:
        got = get_candidate(cid)
        if not got:
            continue
        meta, body = got
        meta["injected"] = str(int(meta.get("injected", 0) or 0) + 1)
        meta["injected_at"] = paths.now()
        loc = paths.candidates_dir()
        if not os.path.isfile(os.path.join(loc, cid + ".md")):
            p = os.path.join(paths.archive_dir("candidates"), cid + ".md")
            if os.path.isfile(p):
                loc = paths.archive_dir("candidates")
        try:
            with open(os.path.join(loc, cid + ".md"), "w", encoding="utf-8") as fh:
                fh.write(emit(meta, body))
        except OSError:
            continue
        n += 1
    return n


def add_candidate(text: str, kind: str = "fact", source: str = "agent",
                  trust: str = "observed", session: str = "", cwd: str = "",
                  evidence: list[str] | None = None, supersedes: list[str] | None = None,
                  notes: str = "", tags: list[str] | None = None,
                  event_at: str = "") -> tuple[bool, str]:
    """T1: distilled, provisional. Never injected as a directive."""
    text = (text or "").strip()
    if not text:
        return False, "empty"
    red, was_red = safety.redact(text)
    relay = safety.relay_reason(red) if source == "user" else None
    if relay:
        source = "inferred"
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
        if source == "agent":
            note = _record_rediscovery(dup, session)
            if note:
                # The agent independently re-learned a fact it already holds as a
                # provisional candidate. It stays provisional (not a directive) and
                # is not auto-kept; this just marks the cross-session rediscovery so
                # the reviewer can weigh it. trust stays cap_trust(source) unchanged.
                return False, note
        return False, "duplicate of %s (%.2f); use `sleepctl supersede %s %s`" % (
            dup, score, dup, "new-text")
    ref_id, ref_score = refused_collision(red)
    if ref_id:
        return False, "duplicate of earlier refusal %s (%.2f); this idea was judged and rejected" % (
            ref_id, ref_score)

    ts = paths.now()
    # Trust the caller's own statement when it gives one; otherwise read a date out of
    # the record's own wording. Never invent: an empty event_at means the record does
    # not say when the described thing happened, which is a different fact from ts.
    event_at = _valid_event_at(event_at, ts) or stated_event_at(red, ts)
    rec = {
        "id": paths.new_id("c"),
        "ts": ts,
        "source": source if source in safety.SOURCES else "inferred",
        "trust": safety.cap_trust(source, trust),
        "kind": kind if kind in KINDS else "fact",
        "status": "provisional",
        "event_at": event_at,
        "session": session or os.environ.get("SLEEP_SESSION", ""),
        "cwd": cwd or os.getcwd(),
        "tags": "|".join(_clean_tags(tags)),
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
    # Write back where the record actually lives: promoted candidates are in the
    # archive, pending ones in candidates. Writing always to candidates used to
    # leave a promoted record's status change stranded in a stale archive copy.
    loc = paths.candidates_dir()
    if meta.get("promoted_to") or not os.path.isfile(os.path.join(loc, cid + ".md")):
        if os.path.isfile(os.path.join(paths.archive_dir("candidates"), cid + ".md")):
            loc = paths.archive_dir("candidates")
    with open(os.path.join(loc, cid + ".md"), "w", encoding="utf-8") as fh:
        fh.write(emit(meta, body))
    return True


# ------------------------------------------------------------ promotion -----
def slugify(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "note"


# What a kind is allowed to become permanent on. This is not the same question as
# "is this record true": a directive-shaped sentence about how the agent should
# behave is a statement about the PERSON, and only the person can confirm one, so a
# preference keeps the existing user-provenance requirement on top of the trust
# check. The other three kinds describe the world, and a human running `promote` is
# the confirmation: they read the body and chose it.
#
# A warning is the case worth naming, because it is the kind most likely to be
# captured from an agent's own failed attempt with no user in the loop. That is
# still legitimate - the human reading it is the check - but the refusal message
# below says which rule stopped it, so "why can I not promote this" never needs
# source reading.
KIND_PROMOTE_NEEDS_USER = ("preference",)


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
    kind = str(meta.get("kind", "") or "")
    if kind in KIND_PROMOTE_NEEDS_USER and src != "user":
        return False, ("refusing to persist a %s from source=%s: a statement about what "
                       "the user wants needs the user's own words, not an agent's reading "
                       "of them" % (kind, src))
    mdir = memory_dir or paths.memory_dir()
    name = "%s.md" % slugify(body.split(".")[0][:60] or cid)
    dest = os.path.join(mdir, name)
    # Expansion-only: if the name is taken, open a new file rather than merging.
    if os.path.exists(dest):
        dest = os.path.join(mdir, "%s-%s.md" % (slugify(body.split(".")[0][:60]), cid[-4:]))
    when = paths.now()
    src = meta.get("source", "?")
    line = "- **%s**: [detail](%s). %s _(promoted %s, source=%s)_" % (
        os.path.basename(dest)[:-3], os.path.basename(dest),
        body.replace("\n", " ")[:160], when[:10], src)
    if dry_run:
        return True, "would write %s (+ index line)" % dest
    os.makedirs(mdir, exist_ok=True)
    # The T2 copy must carry the memory text and its own verdict. `emit` takes the
    # body as an argument and this call used to omit it, which wrote a 245-byte
    # frontmatter husk into the memory store: the only surviving copy of the fact
    # was then the 220-char index line. The copy is stamped promoted as well, so a
    # reader inside the memory store never sees a record claiming to be provisional.
    out_meta = {k: v for k, v in meta.items() if not k.startswith("_")}
    out_meta["status"] = "promoted"
    out_meta["promoted_at"] = when
    # The permanent copy's validity starts when the human approved it: a memory that
    # outlives the record it came from needs its own start date, not the capture ts.
    out_meta.setdefault("valid_from", when)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(emit(out_meta, body))
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
    # Undo has to be possible without git: the memory store is a plain directory
    # on this machine (no .git, no parent repo), so the index keeps its own copy.
    if existing:
        try:
            with open(os.path.join(paths.archive_dir("memory"),
                                   "MEMORY.md.bak-" + paths.now().replace(":", "").replace("+", "_")),
                      "w", encoding="utf-8") as fh:
                fh.write(existing)
        except OSError:
            pass
    tmp = idx + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(updated)
    os.replace(tmp, idx)
    return True, "appended"


def remove_index_line(memory_dir: str, line: str, slug: str) -> bool:
    """Drop one managed index line by slug. Never touches content outside the block."""
    idx = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.isfile(idx):
        return False
    with open(idx, encoding="utf-8") as fh:
        existing = fh.read()
    block_re = re.compile(re.escape(IDX_BEGIN) + r".*?" + re.escape(IDX_END), re.S)
    m = block_re.search(existing)
    if not m:
        return False
    block = m.group(0)
    # Match any line whose "**<slug>**" heading equals ours, regardless of suffix.
    target = "**%s**" % slug
    kept, found = [], False
    for ln in block.split("\n"):
        if re.match(r"\s*- \*\*[^*]+\*\*:", ln) and target in ln:
            found = True
            continue
        kept.append(ln)
    if not found:
        return False
    new_block = "\n".join(kept)
    updated = existing[:m.start()] + new_block + existing[m.end():]
    with open(idx, "w", encoding="utf-8") as fh:
        fh.write(updated)
    return True


def _t2_by_slug_or_id(ref: str, memory_dir: str | None = None) -> tuple[str, str]:
    """Find a permanent (T2) record by filename slug, or by the id inside it.

    `demote` used to accept only a candidate id, so a memory whose candidate record is
    gone (promoted before the archive existed, or written straight into the store) was
    unreachable: `demote <slug>` answered "no such candidate" while the file sat in T2.
    Returns (path, "") or ("", why).
    """
    mdir = memory_dir or paths.memory_dir()
    if not os.path.isdir(mdir):
        return "", "no memory dir: " + mdir
    ref = (ref or "").strip()
    if not ref:
        return "", "empty reference"
    for name in sorted(os.listdir(mdir)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(mdir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                meta, _body = parse(fh.read())
        except (OSError, ValueError):
            continue
        if meta.get("status") == "retracted":
            continue
        if name[:-3] == ref or meta.get("id") == ref:
            return path, ""
    return "", "no permanent record matching %r" % ref


def demote_by_t2(ref: str, memory_dir: str | None = None, dry_run: bool = False,
                 note: str = "") -> tuple[bool, str]:
    """Retract a permanent record that has no reachable candidate behind it.

    Same contract as demote: the copy is MARKED retracted (never deleted), its index
    line is dropped, and the verdict becomes visible in `status`. What differs is the
    lookup - a T2 file is found by slug or by the id it carries, so a memory that was
    promoted outside the normal path is still reversible.
    """
    path, why = _t2_by_slug_or_id(ref, memory_dir)
    if not path:
        return False, why
    slug = os.path.basename(path)[:-3]
    mdir = memory_dir or paths.memory_dir()
    if dry_run:
        return True, "would retract %s (drop index line, mark T2 retracted)" % slug
    stamp = paths.now()
    try:
        with open(path, encoding="utf-8") as fh:
            m2, b2 = parse(fh.read())
    except (OSError, ValueError) as exc:
        return False, "unreadable T2 record %s: %s" % (path, exc)
    m2["status"] = "retracted"
    m2["retracted_at"] = stamp
    if note:
        m2["note"] = note
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(emit(m2, b2))
    dropped = remove_index_line(mdir, "", slug) if mdir else False
    # If a candidate is still reachable under the id the T2 copy carries, keep the two
    # in step so a later `status` does not show a promoted candidate for a retracted file.
    cid = m2.get("id") or ""
    if cid and get_candidate(cid):
        set_status(cid, "refused", {"notes": note or "retracted from T2", "reviewed": stamp})
    return True, "retracted %s%s" % (slug, "" if dropped else " (index line not found)")


def demote(cid: str, memory_dir: str | None = None, dry_run: bool = False,
           note: str = "") -> tuple[bool, str]:
    """T2 -> refused. Explicit human act: an approved memory turned out wrong.

    Promotion was "create a new file, never edit one"; reversal keeps that spirit.
    The canonical T2 copy is *marked* retracted, not deleted, so the audit trail of
    what was once approved survives; the index line is dropped so the retired value
    no longer reads as active memory; and the archived candidate is re-opened as a
    refused record the reviewer can see in `status`. Nothing on this path edits or
    deletes a past verdict.
    """
    got = get_candidate(cid)
    if not got:
        # Not every permanent memory still has a candidate record (promoted before the
        # archive existed, or written into the store directly). Fall back to the T2 file
        # so "the permanent copy is wrong" is always actionable.
        if _t2_by_slug_or_id(cid, memory_dir)[0]:
            return demote_by_t2(cid, memory_dir=memory_dir, dry_run=dry_run, note=note)
        return False, "no such candidate: " + cid
    meta, body = got
    if meta.get("status") != "promoted":
        return False, "not promoted (nothing to demote): " + cid
    dest = meta.get("promoted_to") or ""
    mdir = memory_dir or paths.memory_dir()
    slug = os.path.basename(dest)[:-3] if dest else ""
    if dry_run:
        return True, "would retract %s (drop index line, mark candidate refused)" % (slug or cid)
    stamp = paths.now()
    # 1) mark the canonical T2 copy retracted
    t2_ok = True
    if dest and os.path.isfile(dest):
        try:
            with open(dest, encoding="utf-8") as fh:
                m2, b2 = parse(fh.read())
            m2["status"] = "retracted"
            m2["retracted_at"] = stamp
            if note:
                m2["note"] = note
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(emit(m2, b2))
        except (OSError, ValueError):
            t2_ok = False
    # 2) drop the index line so the retired value no longer reads as active
    if slug and mdir:
        remove_index_line(mdir, "", slug)
    # 3) re-open the archived candidate as refused with a note the reviewer can see
    if note:
        set_status(cid, "refused", {"notes": note, "reviewed": stamp})
    else:
        set_status(cid, "refused", {"reviewed": stamp})
    return True, ("retracted %s" % (slug or cid)
                  + (" (T2 default not found)" if not t2_ok else ""))

def _first_ref(value) -> str:
    """One id out of a supersede field, which parse() returns as a LIST.

    store.parse() turns tags/evidence/supersedes/superseded_by into lists, so treating
    the value as a string produced repr text like "['c1']" and every chain walk missed.
    Accepts either shape and takes the first entry.
    """
    if isinstance(value, (list, tuple)):
        return str(value[0]).strip() if value else ""
    return str(value or "").split("|")[0].strip()


def permanent_records(memory_dir: str | None = None) -> list:
    """(id, body, ts) for every ACTIVE permanent (T2) record, the T2 analogue of the
    provisional shortlist input.

    Retracted files are excluded: a memory a person already un-approved is a decision,
    not an inconsistency to re-report. `id` prefers the frontmatter id and falls back to
    the filename slug, so a hand-written file (no id in frontmatter) is still nameable
    in a finding and actionable with `demote <slug>`.
    """
    mdir = memory_dir or paths.memory_dir()
    out = []
    if not os.path.isdir(mdir):
        return out
    for name in sorted(os.listdir(mdir)):
        if not name.endswith(".md") or name == "MEMORY.md":
            continue
        path = os.path.join(mdir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                meta, body = parse(fh.read())
        except (OSError, ValueError):
            continue
        if meta.get("status") == "retracted":
            continue
        ref = str(meta.get("id") or name[:-3]).strip()
        # The recorded claim time, not the promotion date: a memory written down long after
        # the fact is a late note about an old state. Carried as (claim_time, event_at) so
        # _older_newer can read the same two clocks the T1 shortlist carries.
        out.append((ref, body or "",
                    (str(meta.get("ts") or meta.get("promoted_at") or ""),
                     str(meta.get("event_at") or ""))))
    return out


def _archived_candidates() -> list:
    """Parsed frontmatter of archived candidates (promoted/superseded records live there)."""
    out = []
    d = paths.archive_dir("candidates")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                meta, _body = parse(fh.read())
        except (OSError, ValueError):
            continue
        out.append(meta)
    return out


def supersession_chain(cid: str, max_hops: int = 20) -> dict:
    """Walk a record's supersede links in both directions.

    A supersede writes one pointer on each side, so the *chain* is only visible by
    following them: after three changes, "which record is current?" costs reading files.
    Returns {"order": [oldest..newest], "current": id, "broken": [ids]} - `broken` names
    ids that are referenced but missing from the store, which is how a hand-edited or
    partially archived chain shows up instead of silently reading as a two-record story.
    """
    seen_up, chain = set(), []
    node = cid
    while node and node not in seen_up and len(chain) < max_hops:
        seen_up.add(node)
        chain.append(node)
        got = get_candidate(node)
        if not got:
            break
        node = _first_ref(got[0].get("supersedes"))
    order = list(reversed(chain))          # oldest -> newest
    missing = []
    if order and not get_candidate(order[0]):
        missing.append(order[0])
    # Walk forward from the OLDEST end, not from the requested id: asking about the
    # replaced record must still report what replaced it.
    # Forward walk: follow superseded_by from whatever end we know, always extending the
    # NEWEST side. It is not gated on the seed id (the oldest member has no supersedes and
    # would otherwise stop the walk before it ever looked forward).
    node = order[-1] if order else cid
    seen_down = set(order)
    while node and len(order) < max_hops:
        got = get_candidate(node)
        if not got:
            break
        nxt = _first_ref(got[0].get("superseded_by"))
        if not nxt or nxt in seen_down:
            break
        seen_down.add(nxt)
        if get_candidate(nxt):
            order.append(nxt)
            node = nxt
        else:
            missing.append(nxt)
            break
    current = order[-1] if order else cid
    return {"order": order, "current": current, "broken": missing}


def supersede(old_id: str, new_id: str) -> bool:
    """Mark old_id invalid as of now and new_id valid as of now.

    A bare superseded_by pointer says *which* record replaced this one; it does not
    say *when* the old value stopped being true. That window is what a temporal
    question actually asks ("what did we believe before the change?"), so both ends
    are written here. The stamp is the review clock set_status already writes, so a
    supersede and a refusal carry the same time vocabulary.
    """
    now = paths.now()
    return (set_status(old_id, "superseded", {"superseded_by": new_id, "valid_until": now})
            and set_status(new_id, "provisional", {"supersedes": old_id, "valid_from": now}))


# ------------------------------------------------------------ pruning -------
def prune_buffer(older_than_hours: float = 0.0, session: str | None = None) -> dict:
    """T0 reset: consumed scratch moves to archive. Nothing is deleted.

    File-level and kind-blind by design. It answers "has this session's scratch been
    dealt with?", which is the right question when the caller is an explicit human
    command. It is the WRONG tool for a record-level decision, because a file holds
    records of every kind and moving it takes them all: use prune_session() when a
    record that no pass read must survive.
    """
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


# A kind whose records are dropped before consolidation has lost work that cannot be
# recovered by reading the archive later: the raw text survives, but no pass and no
# session will look at it again. These are the kinds where that loss is expensive -
# re-deriving a preference means asking the person again, and re-deriving a warning
# means paying for the failed attempt a second time. A fact is the one kind whose
# loss is cheap (it can be re-learned by looking), which is why it is deliberately
# NOT in this set: the default path must not be the expensive one.
KEEP_WHEN_UNREAD = ("preference", "procedure", "warning")


def prune_session(session: str, keep_kinds=KEEP_WHEN_UNREAD) -> dict:
    """Prune ONE session's file, holding back records no pass will read.

    The pass is shown the newest `consolidate.BUFFER_WINDOW` records, so anything older
    than that is read by no pass - and a record no pass reads is also never pruned
    (pruning follows consumption). What a file-level prune does anyway is move the whole
    file, oldest records included, which turns "fell outside the window" from a visible
    backlog into a silent one. This function moves the records the pass could have read
    and keeps the rest, so a withheld record stays where the next pass can still reach it.

    Imports `consolidate` lazily on purpose: consolidate imports this module at its top,
    so a module-level import here would be circular. The one constant borrowed back is
    the window, kept single-sourced rather than copied.
    """
    from . import consolidate as _consolidate
    d = paths.buffer_dir()
    src = os.path.join(d, session + ".jsonl")
    if not os.path.isfile(src):
        return {"skipped": True, "reason": "no such session", "kept": [], "moved": ""}
    records = []
    with open(src, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # The window decides what a pass READS, and a pass having read a record is what
    # licenses pruning it. It says nothing about whether the record was worth keeping:
    # a preference inside the window is still a preference, and a prune has no business
    # dropping it because the pass happened to look at it. A record outside the window
    # is the one case where nothing will ever read it again. So the kind filter runs
    # over the WHOLE file rather than over the tail alone, and what survives stays.
    _window = _consolidate.BUFFER_WINDOW
    in_window = records[max(0, len(records) - _window):]
    held = [r for r in records if str(r.get("kind", "")) in keep_kinds]
    held_outside = [r for r in held if r not in in_window]
    if not held:
        return prune_buffer(older_than_hours=0.0, session=session)
    dropped = [r for r in records if str(r.get("kind", "")) not in keep_kinds]
    # The keep file holds ONLY the records whose kind earned survival; the rest go to
    # the archive. An earlier draft appended the in-window records here as well, which
    # silently rewrote the file with its own contents and reported a "kept" count while
    # nothing changed - the exact shape of bug this function exists to prevent, so the
    # rewrite is asserted below rather than assumed.
    keep_sorted = sorted(held, key=lambda r: str(r.get("ts", "")))
    tmp = src + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in keep_sorted:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    os.replace(tmp, src)
    after = read_buffer(session)
    if len(after) != len(keep_sorted):
        raise RuntimeError("prune_session rewrite failed: %d kept, %d on disk"
                           % (len(keep_sorted), len(after)))
    # The records the pass COULD read, plus the withheld ones whose kind is safe to
    # drop, go to the archive rather than the keep file; nothing is deleted and the
    # archive path is the one prune_buffer already uses.
    dest = os.path.join(paths.archive_dir("buffer"),
                        time.strftime("%Y%m%d-") + session + ".jsonl")
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(paths.archive_dir("buffer"),
                            time.strftime("%Y%m%d-") + "%s-%d.jsonl" % (session, n))
        n += 1
    with open(dest, "w", encoding="utf-8") as fh:
        for r in dropped:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return {"moved": dest, "archived_records": len(dropped),
            "kept": [r["id"] for r in held], "kept_count": len(held),
            "kept_outside_window": len(held_outside), "read_window": len(in_window),
            "remaining": buffer_counts()}


# Tags are the only index a record carries besides its project, so they are kept
# machine-shaped: lowercase, no spaces, bounded in number and length. A model may
# propose them, which is exactly why they are normalized here rather than trusted.
MAX_TAGS = 6
MAX_TAG_CHARS = 32


# The two clocks are separate: `ts` is when the record was written (always set), and
# `event_at` is when the thing being described actually happened (only set when the
# record states it). Backfilling event_at from ts would manufacture a fact nobody
# recorded, and every later "where was X in October" question would answer with the
# capture date. Empty means unknown, and unknown is reported as unknown.
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _valid_event_at(value: str, ts: str = "") -> str:
    """Accept a caller-stated event date only in YYYY-MM-DD form, never the capture day."""
    m = _DATE_RE.fullmatch(str(value or "").strip())
    if not m:
        return ""
    if ts and m.group(1) in ts[:10]:
        return ""
    return m.group(1)


def stated_event_at(text: str, fallback_ts: str = "") -> str:
    """Return a date the record's own wording states, else "".

    Deliberately a literal match, not an inference: a relative phrasing ("last week")
    is left to the model that reads the record, and this only accepts a date the text
    spells out. A date equal to the capture day is not an event time either - it is
    the same clock, and writing it twice would invent a distinction.
    """
    m = _DATE_RE.search(text or "")
    if not m:
        return ""
    value = m.group(1)
    if fallback_ts and value in fallback_ts[:10]:
        return ""
    return value


def _clean_tags(tags) -> list:
    out, seen = [], set()
    for raw in tags or []:
        tag = re.sub(r"[^a-z0-9._:+-]+", "-", str(raw).strip().lower()).strip("-")
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:MAX_TAG_CHARS])
        if len(out) >= MAX_TAGS:
            break
    return out


def matches_topic(meta: dict, topic: str) -> bool:
    """True when a record carries the topic as one of its tags (substring, either way).

    Deliberately tag-only: matching the body would turn a topic filter into a keyword
    search, which is what --query already does. Substring in both directions so
    `deploy` selects `deploy` and `deployment`, not `predeployed`."""
    want = str(topic or "").strip().lower()
    if not want:
        return True
    for tag in meta.get("tags") or []:
        tag = str(tag).strip().lower()
        if want == tag or want in tag or tag in want:
            return True
    return False


# A cheap pre-filter for the LLM adjudicator. Token Jaccard is a poor *decider* on
# short observations (measured: no real pair in a live 11-record pool cleared 0.5),
# but it is a fine *shortlist*: pairs that share at least a handful of content words
# are the only ones worth spending a model call on. The model decides; this decides
# what the model is asked about, so the cost stays bounded by the record count.
CONTRADICTION_SHORTLIST = 0.05
CONTRADICTION_MAX_PAIRS = 12


def candidate_pairs(recs: list, limit: int = CONTRADICTION_MAX_PAIRS) -> list:
    """Pairs of provisional records worth asking a model about, newest-signal first.

    recs: [(id, body, ts)] in any order. Returns [(older_id, newer_id, ts_map)] with
    at most `limit` pairs, ordered by shared-wording score so a bounded number of
    model calls covers the most likely conflicts first.
    """
    scored = []
    for (ia, ba, ta), (ib, bb, tb) in itertools.combinations(recs, 2):
        s = similarity(ba, bb)
        if s >= CONTRADICTION_SHORTLIST:
            older, newer = (ia, ib) if (ta or "") <= (tb or "") else (ib, ia)
            scored.append((s, older, newer))
    scored.sort(reverse=True)
    return [(o, n) for _s, o, n in scored[:limit]]


def _clock(entry) -> tuple[str, str]:
    """Accept either the old bare-ts map value or this module's (ts, event_at) pair."""
    if isinstance(entry, (tuple, list)):
        ts = str(entry[0] or "") if len(entry) > 0 else ""
        ev = str(entry[1] or "") if len(entry) > 1 else ""
        return ts, ev
    return str(entry or ""), ""


def _older_newer(a: str, b: str, ts_by_id: dict) -> tuple[str, str]:
    """Order two candidate ids by the claim's own time, oldest first.

    Which side is the claim and which is the correction is a statement about the world,
    not about this store, so the ordering reads both clocks the record carries:

      - ts is when the store first held the claim, event_at is when the record says the
        described thing was true. A session can file an old fact today, so ts alone
        would name a document quote recorded this morning as "the newer claim" and hand
        back a supersede command pointing the wrong way.
      - event_at wins whenever the pair can be compared on it at all, for BOTH sides.
        Mixing the clocks (one side's ts against the other's event_at) compares two
        different axes and just relocates the bug: the documented late note carries both,
        so its ts would still be what the comparison read.
      - With no stated date on either side, or on only one, the capture ts pair decides
        and, when those tie or are missing, the id does. A pair is always returned, so
        `supersede <older> <newer>` is always a runnable command.

    Values may be a bare timestamp (older callers) or a (ts, event_at) pair.
    """
    ta, ea = _clock(ts_by_id.get(a, ""))
    tb, eb = _clock(ts_by_id.get(b, ""))
    # One axis per comparison: event_at when both records state one, capture ts otherwise.
    ca, cb = (ea, eb) if (ea and eb) else (ta, tb)
    if ca and cb and ca != cb:
        return (a, b) if ca < cb else (b, a)
    if ca != cb:
        return (a, b) if not ca else (b, a)
    return (a, b) if a <= b else (b, a)


def doctor(memory_dir: str | None = None, targets: list[str] | None = None,
          needle: str | None = None) -> list[dict]:
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
    if needle:
        nlow = needle.strip().lower()
        if nlow:
            for p, text in bodies.items():
                if os.path.basename(p) == "MEMORY.md":
                    # The index only points at detail files; flagging it re-reports
                    # every hit once for the detail file it names, so skip it.
                    continue
                if nlow in text.lower():
                    out.append({"issue": "stale-value-in-memory", "file": p,
                                "value": needle})
    pool_records = list_candidates(with_body=True)
    pool = [(c["id"], c.get("_body", "")) for c in pool_records]
    # Two clocks per record: ts is when this store first held the claim, event_at is when
    # the record itself says the described thing was true. A session can learn an old fact
    # today (ts new, event_at old), so "which side is the older claim" is a world-time
    # question and cannot be answered from ts alone. The pair is (ts, event_at) and the
    # ordering rule lives in _older_newer.
    pool_ts = {c["id"]: (str(c.get("ts", "")), str(c.get("event_at", ""))) for c in pool_records}
    for i in range(len(pool)):
        for j in range(i + 1, len(pool)):
            sim = similarity(pool[i][1], pool[j][1])
            if sim >= 0.75:
                out.append({"issue": "near-duplicate-candidates",
                            "pair": [pool[i][0], pool[j][0]], "similarity": round(sim, 2)})
            # Overlapping wording with a correction marker on exactly one side: the two
            # records are about the same thing but disagree, which is the state a person
            # has to resolve (refuse one, or supersede). Report-only; nothing is merged,
            # reordered or dropped, and a marker on BOTH sides is only a shared theme.
            elif sim >= CONTRADICTION_OVERLAP:
                mi, mj = bool(_CORRECTION.search(pool[i][1])), bool(_CORRECTION.search(pool[j][1]))
                if mi != mj:
                    # Name the older/newer side so a person does not diff two timestamps by
                    # eye: the older record is the claim, the newer one is the correction.
                    # "Older" is world time (_older_newer), so a record learned late about an
                    # old state is still named as the claim and not as the fix.
                    # The suggestion is a command to RUN, never one this code runs -
                    # superseding on a keyword match could kill a true memory, so the gate
                    # stays human.
                    older, newer = _older_newer(pool[i][0], pool[j][0], pool_ts)
                    out.append({"issue": "contradicting-candidates",
                                "pair": [pool[i][0], pool[j][0]], "similarity": round(sim, 2),
                                "older": older, "newer": newer,
                                "suggest": "sleep.py supersede %s %s" % (older, newer)})
    for c in list_candidates():
        if c.get("status") != "provisional":
            continue
        # Last activity = the most recent rediscovery if there was one, else the
        # record's own creation ts. A provisional that is neither being promoted
        # nor re-learned for weeks is noise the queue keeps injecting into every
        # session's context, so the doctor surfaces it (report only, nothing drops).
        # "Weeks" is per kind: the same quiet month means different things for a
        # preference and for an environment warning (see KIND_LIFETIME_WEEKS).
        stale_weeks, _unused_weeks, kind = lifetime_for(c.get("kind", ""))
        last = c.get("rediscovered_at") or c.get("ts")
        age_h = _hours_since(str(last)) if last else None
        if age_h is not None and age_h >= stale_weeks * 7 * 24:
            out.append({"issue": "stale-candidate-unrediscovered", "id": c["id"],
                        "kind": kind, "threshold_weeks": stale_weeks,
                        "age_days": int(age_h // 24),
                        "last_seen": str(last)[:10], "rediscovered": int(c.get("rediscovered", 0) or 0)})
    # The sibling of the check above, measuring a different emptiness. That one says
    # "nobody re-learned this"; this one says "nobody ever asked for this": no inject
    # ever carried it and no consolidation ever saw it come up again. A record can be
    # perfectly true and still be worth naming here, because the cost it carries is
    # paid by every session that shares the project and the evidence it is worth
    # paying is published in the record itself.
    for c in list_candidates():
        if c.get("status") != "provisional":
            continue
        served = int(c.get("injected", 0) or 0) + int(c.get("rediscovered", 0) or 0)
        if served:
            continue
        _stale_weeks, unused_weeks, kind = lifetime_for(c.get("kind", ""))
        age_h = _hours_since(str(c.get("ts", ""))) if c.get("ts") else None
        if age_h is not None and age_h >= unused_weeks * 7 * 24:
            out.append({"issue": "unused-candidate-never-served", "id": c["id"],
                        "kind": kind, "threshold_weeks": unused_weeks,
                        "age_days": int(age_h // 24),
                        "injected": 0, "rediscovered": 0})
    # The one input limit that is not a content judgement: a pass is shown only the
    # newest WINDOW records OF THE SESSION IT CONSOLIDATES, so a session larger than
    # that carries records no pass will read. Counted per session, on the honest
    # definition of a pass - `run_sleep`/`distill` hand over one session file - because
    # the total across sessions compares a number to a limit it was never measured
    # against: fifty sessions of ten records each are fifty complete passes, not one
    # buffer of five hundred with four hundred and eighty unread. Reported rather than
    # raised to a failure, because a large session may be a real backlog.
    from . import consolidate as _consolidate
    _window = _consolidate.BUFFER_WINDOW
    for _session, _count in sorted(buffer_session_counts().items()):
        if _count > _window:
            out.append({"issue": "buffer-exceeds-consolidation-window",
                        "session": _session,
                        "buffer_records": _count,
                        "window": _window,
                        "outside_window": _count - _window})
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
    # Quarantine is a wait-for-a-human state, not a resting state: entries parked there
    # are held out of every future consolidation until someone reads them. `doctor`
    # reporting "clean" while the queue holds parked records is the failure a user hits
    # by asking exactly the question doctor answers ("is anything waiting on me?").
    # Report the count and the newest ids; nothing is dropped or changed here.
    # Path assembled without paths.home(): home() creates the whole tier skeleton as a
    # side effect, and a read-only report that lays down directories is not read-only.
    # A supersede writes a pointer on each side, so a link can dangle: the file was
    # pruned, the archive lost it, or a hand edit dropped it. A dangling link reads as
    # "this record replaced something" while the something is unrecoverable - worth one
    # line, since it is invisible in every other view.
    for c in list_candidates(with_body=False) + _archived_candidates():
        cid = c.get("id")
        if not cid:
            continue
        for field in ("supersedes", "superseded_by"):
            target = _first_ref(c.get(field))
            if target and not get_candidate(target):
                out.append({"issue": "broken-supersede-link", "id": cid,
                            "field": field, "missing": target})
    qdir = os.path.join(paths.SLEEP_HOME, ".quarantine")
    if os.path.isdir(qdir):
        parked = sorted(f for f in os.listdir(qdir) if f.endswith(".json"))
        if parked:
            out.append({"issue": "quarantine-awaiting-review", "count": len(parked),
                        "newest": [f[:-5] for f in parked[-3:]]})
    return out
