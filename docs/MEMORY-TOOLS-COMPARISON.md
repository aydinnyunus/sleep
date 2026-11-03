# Agent-memory landscape: what the other tools do that sleep does not

Compiled 2026-11-03 from depth-1 clones of ten agent-memory projects, read locally under
`~/Research/mem-tools/`. Every claim cites either a local `path:line` in that clone or an
upstream doc/README; nothing here is from memory. This is a survey note, not a product doc:
it exists so a future change can be checked against what the field already does.

## Per-tool analysis

Cloned into `~/Research/mem-tools/` (depth 1) and read locally. Every claim below is
tagged with the artifact it came from: a local path+line, or an upstream doc/README.

## 1. mem0 (mem0ai/mem0) - `753496c`, 1832 files
**What it is.** A memory *layer* between app and model. Loop: send turns to `add`, call
`search` before the next model call, put results into the prompt.
**Storage.** Vector store + optional graph; OSS SDK keeps SQLite/vector locally.
**How it works (local evidence).** `mem0/memory/main.py:20` imports
`ADDITIVE_EXTRACTION_PROMPT`; `main.py:458` `_PROJECT_UPDATE_UNSUPPORTED_ERROR` ("Project
updates are not supported by the OSS Memory SDK"). The current OSS algorithm is
**single-pass ADD-only** (one LLM call extracts facts; no UPDATE/DELETE step), retrieval is
**multi-signal hybrid** (semantic + BM25 + entity match), and graph memory moved to the paid
Platform (docs: migration/oss-v2-to-v3). Extraction is LLM-based; `infer=False` stores raw text.
**Conflicts.** Docs claim "latest truth wins"; a live upstream issue (#4896) shows the OSS code
does **exact MD5 dedup only** (`main.py:784-803`) with no semantic conflict resolution.
**Notable.** Token-cheap API surface (`add`/`search`), user/agent/run scoping,
`DELETE_ALL_BATCH_SIZE = 1000` (`main.py:136`).
**Gap vs sleep.** No human approval gate at all (memory is trusted as written); no provenance
class distinction enforced in code; the conflict story is documented-but-not-implemented.

## 2. Letta (letta-ai/letta) - `8816d3a`, 12 files
**Important local finding.** This repo is now a **landing page only**: `AGENTS.md` says
"Stop: do not use this repository as a working Letta implementation", and the README points
to `letta-ai/letta-code` for the real harness. So there is no server code here to read.
**How it works (upstream docs).** Memory = **memory blocks**: labelled, size-capped sections
of the context window that are *always visible* (prepended to the prompt in XML-like form, no
retrieval). Agents edit them with built-in system-call-style tools
(`memory_append`/`memory_replace`/`memory_rethink`). Newer work adds **MemFS**, a git-backed
memory filesystem, and **sleep-time compute**: the agent keeps running while idle, rewriting
its own memory state ("dreaming").
**Gap vs sleep.** (i) Agent self-editing memory vs sleep's human-promote-only T2. (ii) Always-on
blocks (deterministic context) vs sleep's retrieval-ranked injection. (iii) git-backed memory
history vs sleep's append-only archive. Sleep's advantage: a review gate and provenance
enforcement Letta lacks.

## 3. Cognee (topoteretes/cognee) - `525b1d1`, 4763 files
**What it is.** Graph-vector hybrid memory with an **ECL pipeline** (extract, cognify, load).
API: `add` -> `cognify` -> `memify` -> `search`.
**Storage (local evidence).** Three complementary stores; `cognee/infrastructure/databases/`
has a relational config (`relational/config.py:29,38,95`), plus vector and graph. Defaults are
embedded (SQLite + LanceDB + graph) so it runs with no infra.
**Retrieval (local evidence).** `cognee/modules/search/types/SearchType.py:5` enumerates
`RAG_COMPLETION`, `GRAPH_COMPLETION`, `GRAPH_COMPLETION_DECOMPOSITION`, `GRAPH_COMPLETION_COT`,
`GRAPH_COMPLETION_CONTEXT_EXTENSION`, and **`TEMPORAL`**; line 26 notes triplet embeddings were
removed (SDK-1115). So search is a *choice of strategy*, not one similarity call.
**Gap vs sleep.** Multiple retrieval strategies and a real graph; sleep has one overlap-ranked
injection. Cognee is heavier (needs stores + LLM pipeline). Sleep's advantage: zero-dependency,
file-first, human gate.

## 4. Graphiti (getzep/graphiti) - `16bf514`, 424 files
**What it is.** The strongest temporal model of the set.
**Storage (local evidence).** Neo4j/falkor graph driver:
`graphiti_core/driver/neo4j/operations/entity_edge_ops.py:53-54` writes `valid_at` and
`invalid_at` per edge; `graphiti_core/utils/maintenance/edge_operations.py:310-359` parses
`invalid_at` and normalizes the temporal bounds.
**How it works.** Bi-temporal: each edge carries when the fact was true (`valid_at`),
when it stopped being true (`invalid_at`), plus when the system learned each. New information
**invalidates** the old edge instead of deleting it; retrieval combines vector + full-text +
graph traversal in one call; no batch re-indexing.
**Gap vs sleep.** Sleep now has `valid_from`/`valid_until` (added 0.8.4), so the *concept*
matches - but sleep stamps only on an explicit `supersede`, has no automatic invalidation when a
contradicting capture arrives, and no graph/entity layer. Graphiti's "expired_at vs invalid_at"
split (system-time vs world-time) is the piece sleep lacks.

## 5. Zep (getzep/zep) - `d1cbb1c`, 1055 files
**Local finding.** README: "This repository is **not** Zep's product or service. It contains
example code, framework integrations, and tools for Zep Cloud." Product is managed; the engine
is Graphiti (`legacy/` holds the retired Go CE server).
**Gap vs sleep.** Nothing to adopt mechanically; it is the *productized* Graphiti. Its paper
(arXiv 2501.13956) is where the Graphiti temporal claims are benchmarked.

## 6. Basic Memory (basicmachines-co/basic-memory) - `574f372`, 1888 files
**What it is.** Closest in *philosophy* to sleep: file-first markdown.
**Storage (local evidence).** `src/basic_memory/` is a full package (api, cli, db, indexing,
importers, mcp, hooks). Markdown files are the source of truth; a DB is a *secondary index*
(file watcher re-indexes on change). Knowledge = `[[wikilinks]]` + frontmatter -> a graph.
**How it works.** Capture -> connect -> reference -> edit -> sync. Two-way: AI and human edit
the same files (Obsidian graph view works directly). MCP server + CLI + semantic search.
**Gap vs sleep.** Sleep's T2 is also markdown, but (i) Basic Memory exposes the whole store to the
agent as editable MCP tools; sleep deliberately keeps T2 human-write-only. (ii) It has explicit
entities/relations syntax; sleep has free-text records. (iii) Two-way sync vs sleep's one-way
capture + approve.

## 7. claude-mem (thedotmack/claude-mem) - `63efe31`, 2882 files
**What it is.** The closest *architectural* competitor to sleep - same hook-based capture.
**How it works (upstream docs + local).** Lifecycle hooks `SessionStart, UserPromptSubmit,
PreToolUse, PostToolUse, Stop, SessionEnd`, each one shell-free command `claude-mem hook
claude-code ...`. Pipeline: Hook(stdin) -> SQLite -> worker service -> AI (Claude Agent SDK)
compression -> SQLite -> next session's context hook. Reports a **10:1 to 100:1 compression**
ratio.
**Distinctive mechanism.** **Progressive disclosure**: Layer 1 index (titles/dates/types/token
counts, ~50-100 tokens per result), Layer 2 timeline context, Layer 3 full observations - the
agent chooses when to pay the token cost. Local UI data shows a rule with `timesSeen: 3`,
`source: 'you'`, `status: 'on'` (`src/ui/my-work/src/data-rules.ts:120`).
**Gap vs sleep.** (i) Progressive disclosure (index -> timeline -> detail) is a real capability
sleep lacks: sleep injects full bodies. (ii) A worker service + DB decouples capture from
processing; sleep is synchronous hooks. (iii) `timesSeen` counter per rule is a first-class
"was this repeated" field - sleep added a related signal but keeps it in `notes`.
Sleep's advantage: an explicit human approval gate (claude-mem auto-trusts), and provenance
stamping as a product rule.

## 8. memory-bank-mcp (alioshr/memory-bank-mcp) - `559e422`, 126 files
**What it is.** A file-based "Memory Bank" MCP server (Cline-style), not a similarity engine.
**How it works (upstream).** Tools(JSON-RPC)/Resources/Prompts -> `MemoryBankManager`. Modes:
architect, code, ask, debug, test - modes change *behavior guidelines*, not tool access.
Storage is markdown under `memory-bank/`; every call takes `project_id` (absolute project root)
for per-project isolation; supports a shared common store.
**Gap vs sleep.** (i) **Per-project partitioning is first-class via `project_id` on every call** -
sleep partitions by canonical `cwd` at consolidation but has no project-keyed memory API.
(ii) Mode-scoped instructions (architect vs code) is a notion sleep has no equivalent for.
(iii) No trust/provenance model, no conflict handling, no decay.

## 9. OpenMemory (mem0ai/openmemory) - `2df78cc`, 297 files
**Status (upstream).** **Deprecated/sunset** - "use the Mem0 self-hosted server instead"
(`server && make bootstrap`). Was a local-first MCP memory layer with a React dashboard
(build = FastAPI + MCP server + UI).
**Gap vs sleep.** Its differentiator was a **UI for memory visibility/auditing/control** - a
human surface over the store. Sleep's only human surface is the CLI (`status`/`show`/`promote`).
Its MCP-server-as-shared-memory-across-clients idea is also absent in sleep.

## 10. Supermemory (supermemoryai/supermemory) - `81350cb`, 939 files
**What it is.** Managed "Memory API" with an internal learning model + temporal vector-graph.
**Local evidence.** Monorepo `packages/`: `memory-graph`, `lib`, `hooks`, `tools`, `ui`,
`agent-framework-python`, plus framework SDKs (openai, pipecat, livekit, cartesia).
**How it works (upstream).** Six-stage ingest pipeline; **documents vs memories** are distinct
kinds (raw input vs extracted facts like "Alex is a PM at Stripe"); user profiles as a product
surface. Fact-based temporal graph.
**Gap vs sleep.** (i) Document/memory type separation (sleep has one record kind + `kind` field).
(ii) Per-user profile synthesis as a first-class output. (iii) A large SDK/hook surface for
frameworks. Sleep's advantage: local, free, no service.

---

## Cross-cutting summary: what the others have that sleep does not

| Capability | Present in | Sleep today |
|---|---|---|
| Automatic conflict invalidation on contradiction | Graphiti, Supermemory, Cognee(TEMPORAL) | report-only `contradicting-candidates`; no auto-invalidate |
| Bi-temporal split (world-time vs system-time) | Graphiti (`valid_at`/`invalid_at` + learned-at) | `valid_from`/`valid_until` only, set on explicit supersede |
| Graph / entity-relation layer | Graphiti, Cognee, mem0(graph, paid), basic-memory(wikilinks) | none (flat records) |
| Multi-strategy retrieval | Cognee (6 SearchTypes), mem0 (semantic+BM25+entity) | one overlap-ranked injection |
| Progressive disclosure (index -> detail) | claude-mem | injects full bodies |
| Always-visible deterministic memory | Letta memory blocks | retrieval-only |
| Agent self-editing memory | Letta (`memory_replace`/`rethink`), basic-memory | T2 human-write-only |
| Human approval gate | **none of them** (sleep only) | T1 -> human `promote` -> T2 |
| Provenance/trust enforcement | partial (claude-mem `source`, mem0 scoping) | source/trust fields + relay filter |
| Decay / forgetting | Supermemory ("when to forget"), Letta dreaming | `doctor` flags stale (report-only) |
| Per-project memory API | memory-bank-mcp (`project_id`) | cwd partition at consolidation |
| Human UI over the store | OpenMemory (deprecated), claude-mem UI | CLI only |
| Zero-dependency / file-first | basic-memory, sleep | yes |

**Highest-value additions that fit sleep without breaking it:** (1) progressive disclosure in
inject (index line first, body on demand); (2) automatic invalidation *proposal* (not
auto-supersede) when a contradicting capture arrives; (3) world-time vs system-time split on the
validity window. All three are report/propose-shaped and keep the human gate intact.
