# BlahajSphere — Implementation Spec v1.1

Agent-driven, gamified desktop tutor for simulation-heavy engineering. v1 domain: **RFICs**. Domain-pluggable architecture (frontier ML, topological CM later).

**Core idea:** not a chat tutor. An agent picks the next challenge from the user's goals + an archive of past challenges, then *builds a ready-to-use environment* for it (files, cached Docker toolchain, sim tools, a deterministic checker). The user always sees one clear next task.

## 0. Rules for implementing agents
- Build milestones in order (§14). Each must pass its acceptance criteria before the next starts.
- **Context economy:** per session, load only §0, §5 (schemas), and the sections listed for your milestone in §14. Do not load the whole spec.
- Items marked **[VERIFY]** are assumptions to confirm at M0. If false, choose the closest equivalent and record it in `DECISIONS.md`.
- All automated tests use the `mock` LLM adapter (§3). Each milestone gets exactly one manual live smoke test.
- Domain-specific content lives only in `domains/<domain>/` (§2).

---

## 1. Hard constraints

| # | Constraint |
|---|---|
| C1 | Host: Windows 11 laptop (ThinkPad T14s Gen 4). No GPU assumed. |
| C2 | **$0 marginal cost**: existing subscriptions (Claude, Cursor) + free models only. Router refuses paid endpoints. |
| C3 | All EDA/sim tools run in **Docker** (Docker Desktop, WSL2 backend). |
| C4 | Learning efficiency first: maximize competency gained per hour. No calendar-based gates. |
| C5 | Competency scores computed by **code**, never by an LLM directly (§8). |
| C6 | Tutor is **Socratic by default** (§7.4). |
| C7 | Local-first: all user data on disk; works offline except LLM calls. |

## 2. Stack & layout

| Layer | Choice |
|---|---|
| Shell | `pywebview` window (Edge WebView2) |
| Backend | Python 3.11+, FastAPI, uvicorn (127.0.0.1 only), `uv` |
| API contract | FastAPI OpenAPI → generated TS client (`openapi-typescript`); SSE for streams |
| Frontend | React + TypeScript + Vite + Tailwind; graph via `react-flow` |
| DB | SQLite (`sqlmodel`) as a **rebuildable cache** (§4.3) |
| Containers | Docker SDK for Python |
| Audio | `sounddevice` (capture in backend), `faster-whisper` (`small.en`, int8, CPU), `piper-tts` |
| Doc ingest | PyMuPDF; `ocrmypdf`+Tesseract fallback; `fastembed` (`BAAI/bge-small-en-v1.5`); brute-force cosine in numpy |
| Tests | pytest, vitest |

```
blahajsphere/
  app/                 # FastAPI, router, agents, jobs, competency engine
  app/prompts/         # domain-agnostic prompt packs
  ui/                  # React
  domains/rfic/        # EVERYTHING RFIC-specific:
    framework_template.yaml   # starter domains + tier text
    docker/Dockerfile         # rfic image
    tools_mcp/                # MCP server: sim tools
    prompts/builder.md        # domain builder pack
    vocab.txt                 # STT vocabulary seed
  data/                # user data root (own git repo; gitignored here)
  SPEC.md DECISIONS.md
```
Domain plugin = the files in `domains/<name>/`. Core code must not import anything RFIC-specific.

## 3. LLM backend router

Interface: `complete(task_type, messages, schema=None, cwd=None, tools=None) -> Result{text, json, usage, backend, model}`.

**Adapters**
| Adapter | Mechanism | Tier |
|---|---|---|
| `claude_code` | subprocess `claude -p --output-format json --model <id> --mcp-config <file> --allowedTools <explicit list> --permission-mode <non-interactive mode>` **[VERIFY flags; must never block on a permission prompt]**. cwd = node dir. **Strip `ANTHROPIC_API_KEY` and similar API-key vars from the subprocess env** so it uses the subscription login **[VERIFY billing behavior]**. Timeout per call (default 15 min). | heavy |
| `cursor_cli` | subprocess `cursor-agent -p ...` **[VERIFY name/flags]**. Optional fallback. | heavy |
| `openrouter` | OpenAI-compatible HTTPS; **only model IDs ending `:free`**; key in settings | light |
| `mock` | Returns canned fixtures from `tests/fixtures/llm/<task_type>/*.json` | tests |

**Routing** (`config/routing.yaml`, user-editable)
| task_type | primary | fallback |
|---|---|---|
| plan_branches, build_challenge, draft_framework, rescope | claude_code | cursor_cli |
| grade_oral, grade_prelab_explain, grade_diagnostic, grade_tier_exam | claude_code, **pinned model ID** (§8.5) | none → grading queue (§3.1) |
| interview, tutor_chat, oral_dialogue, diagnostic_dialogue, summarize | openrouter free list (ordered) | claude_code |

- Auto-failover across the free list on 429/5xx/timeout.
- **Daily request counter** per free model; skip models at their cap **[VERIFY current OpenRouter free limits at M0]**.
- Log every call to `llm_calls`: task_type, backend, model, tokens (if reported), latency, node_id. Settings shows daily usage.
- Grading never fails over to a different model.
- **Privacy:** library chunks (§10) are sent only to `claude_code` tasks unless the user enables "allow library text to free models" (free providers may log prompts).

### 3.1 Grading queue
If grading is unavailable (e.g., subscription limit), the node advances to Debrief as **provisional**: proof results and calibration are shown, grades shown as "pending". The Planner may still propose branches. When grading succeeds, events are written and the debrief updates. Pending grades retry with backoff and on app start.

### 3.2 Token economy
Agents never receive the raw archive. They get `data/archive/SUMMARY.md` (≤ 1,500 tokens, regenerated after each Debrief), the specific node files needed, and a scores JSON.

## 4. Data model

### 4.1 Filesystem (`data/`, git repo; auto-commit at each stage transition)
```
data/
  .gitignore                     # ignores workspace sim outputs (*.raw, *.s*p over 1 MB, openEMS dirs, __pycache__)
  profile/goals.md
  framework/competency-v1.yaml   # frozen ruler (§8.1)
  events.jsonl                   # APPEND-ONLY source of truth for evidence (§5.8)
  archive/SUMMARY.md
  archive/nodes/0007-l-match-60ghz/
    node.md        # frontmatter + brief (user-visible)
    prelab.md      # questions (user-visible)
    prelab_answers.yaml   # user's locked answers
    hints.yaml     # hint ladder (tutor-visible, user-visible on request)
    _hidden/       # NEVER sent to tutor/examiner models or shown in UI
      reference_solution/   # files mirroring workspace/
      prelab_key.yaml
      oral_key.yaml
      ref_result.json       # checker output on reference solution (gate step 1)
    workspace/     # user works here
    checker.py
    proof/         # copies of declared proof artifacts only
    oral.md        # transcript + grades
    debrief.md
  library/<book-id>/
```

### 4.2 Container paths
One long-lived container mounts `data/archive/nodes` → `/work/nodes` once. Per-node commands use `workdir=/work/nodes/<dir>/workspace`. All host↔container path translation lives in one function `to_container(path)`/`to_host(path)`; MCP tools accept host-relative node paths and translate.

### 4.3 SQLite (cache, rebuildable from files + events.jsonl via `rebuild_db` command)
`nodes`, `subskills`, `scores`, `tiers`, `misconceptions`, `jobs`, `llm_calls`, `grading_queue`, `library_chunks`(book_id, page, text, embedding blob). `llm_calls`, `jobs`, and `grading_queue` are the only tables not rebuildable (operational).

## 5. Schemas (authoritative)

### 5.1 `node.md` frontmatter
```yaml
id: 7
slug: l-match-60ghz
title: "Hand-design an L-match at 60 GHz"
parents: [5]
kind: challenge | remediation | tier_exam | diagnostic
domain: matching
subskills: [matching.l_network, smith_chart.navigation]
tier: 2
size_est_min: 40
sim_runtime_s: 95            # measured from reference run at gate (§7.1)
image: rfic:1
pdk: ihp-sg13g2 | sky130 | none
run: ["python run_sim.py"]   # commands the checker executes inside workspace
proof_artifacts: ["match.sch", "run_sim.py"]   # copied to proof/
vocab: ["S11", "gm", "Smith chart", "L-match"] # STT initial prompt
status: prelab | lab | proof | oral | debrief | done | rescoped | abandoned
provisional: false
created: 2026-11-10T05:00:00-06:00
references: [{title: "...", url: "..."}, {book_id: "razavi-rf", page: 112}]
```

### 5.2 Checker contract
`python checker.py --work <workspace_dir> --out <result.json>`, run inside container.
- Must **re-run the simulation** via the node's `run` commands from user source files; never trust pre-existing output files.
- Exit codes: 0 pass, 1 fail, 2 error (env/crash).
- Output:
```json
{"pass": true,
 "tests": [{"name": "S11 at 60GHz", "pass": true, "measured": -14.2, "spec": "<= -10", "unit": "dB"}],
 "metrics": {"Q_L": 3.1, "S11_60G_dB": -14.2},
 "runtime_s": 95.3}
```
`metrics` feed prelab numeric keys (§5.3).

### 5.3 `prelab.md` + `_hidden/prelab_key.yaml`
`prelab.md`: numbered items with id, prompt, and for numeric items a **fixed unit** (input field locked to it).
```yaml
items:
  - id: p1
    kind: numeric
    subskill: matching.l_network
    unit: ohm
    metric: Q_L                  # value extracted by code from ref_result.json.metrics
    tolerance: {rel: 0.15}       # or {abs: 2.0}
  - id: p2
    kind: explain
    subskill: smith_chart.navigation
    anchors: {0: "...", 1: "...", 2: "...", 3: "..."}
```
Key values are **never** LLM-asserted: the gate fills them from `ref_result.json`; a missing metric fails the gate.

### 5.4 `_hidden/oral_key.yaml`
```yaml
items:
  - id: o1
    subskill: matching.l_network
    tier: 2
    question: "..."
    anchors: {0: "...", 1: "...", 2: "...", 3: "..."}
    probes: ["...", "..."]
    interleaved: false
```
At least one item with `interleaved: true` when eligible (§7.1 gate 4).

### 5.5 `hints.yaml`
```yaml
ladders:
  - subskill: matching.l_network
    hints: ["conceptual nudge", "narrower nudge", "near-explicit method step"]   # no target numbers
```

### 5.6 Branch options (Planner output)
```json
{"options": [{"title": "...", "why_now": "...", "kind": "challenge", "domain": "noise",
              "subskills": ["noise.figure_cascade"], "tier": 2, "size_est_min": 30}]}
```
2–4 options.

### 5.7 Grading output
```json
{"items": [{"id": "o1", "score": 2, "quote": "verbatim from transcript", "rationale": "..."}],
 "misconceptions": [{"text": "...", "subskill": "..."}]}
```

### 5.8 Event (`events.jsonl`, one JSON per line)
```json
{"id": "uuid", "ts": "...", "node_id": 7, "domain": "matching", "subskill": "matching.l_network",
 "tier": 2, "type": "proof_pass|oral_item|prelab_numeric|prelab_explain|tier_exam_item|diagnostic_item",
 "value": 0.67, "weight": 1.0, "interleaved": false, "assist_reveal": false,
 "grader_model": "model-id-or-null", "framework_version": 1}
```

## 6. Docker toolchain & jobs

### 6.1 Image
- `rfic:1` built **FROM IIC-OSIC-TOOLS** (`hpretl/iic-osic-tools`) **[VERIFY includes ngspice, Xschem, KLayout, openEMS, Qucs-S, sky130 + IHP SG13G2 PDKs; add missing tools in Dockerfile; record image size and disk free]**. Add: scikit-rf, numpy, matplotlib, jupyterlab.
- GUI tools launched **externally**: default the image's noVNC in the system browser **[VERIFY port/flag]**; alternative X11 via WSLg. Pick what works at M0.
- JupyterLab in container, opened in system browser at the node workspace.
- **[VERIFY at M0]** openEMS performance on a Windows-path bind mount vs. a WSL2-native path; if > 2× slower, place `data/` inside the WSL2 filesystem and record it.

### 6.2 MCP server (`domains/rfic/tools_mcp/`, stdio, heavy agents only)
`run_ngspice(netlist)`, `run_openems(script, timeout_min)`, `run_python(path)`, `run_checker(node, target: workspace|reference)`, `parse_touchstone(path) -> {f, S}`, `list_pdk_models(pdk)`, `write_file`, `read_file` (sandboxed to the node dir).

### 6.3 Job system
All long work (build, sim, checker, grading, ingest, image pull, summarize) runs as **jobs**: in-process asyncio worker, `jobs` table (id, type, node_id, status queued|running|done|failed, progress 0–1, log tail). UI subscribes via SSE `/api/jobs/stream`; any screen shows active jobs in a tray.

### 6.4 Preflight (first run + Settings)
Checks with fix hints: Docker running; image present (pull job if not); `claude` CLI installed + logged in; OpenRouter key valid; whisper + piper models downloaded; disk free ≥ image size + 10 GB; microphone available.

## 7. Node lifecycle

```
[Planner] ─► 2–4 options ─► pick ─► [Builder + gate] ─► PRELAB ─► LAB ─► PROOF ─► ORAL ─► DEBRIEF ─► [Planner]
                                          ▲               │                         │ fail
                                          └─── RESCOPE ───┘                         └─► remediation child + misconceptions
```
Every screen shows one primary next-action button.

### 7.1 Builder (heavy, with MCP) + release gate
Input: chosen option, goals, SUMMARY.md, scores, open misconceptions, top-k library chunks, `domains/<d>/prompts/builder.md`.
Produces the full node directory. **Gate (enforced by code):**
1. `run_checker(reference)` → exit 0; save output as `_hidden/ref_result.json`; set `sim_runtime_s`.
2. `run_checker(workspace)` on unmodified starter → exit 1.
3. Every numeric prelab item's `metric` exists in `ref_result.json.metrics`; code writes values into `prelab_key.yaml`. ≥ 2 numeric items.
4. If any open misconception or any subskill not evidenced in the last 3 nodes exists: ≥ 1 prelab or oral item targets one (interleaving).
5. `hints.yaml` covers every listed subskill; `vocab` non-empty.
6. If `sim_runtime_s` > 600: brief must contain a long-run warning (UI shows it before Run).
Failure → Builder retries with the gate error (max 2), then shows the error to the user.

### 7.2 Prelab
User answers before workspace unlocks. Numeric answers use the fixed unit. Locked on submit. No feedback until Debrief.

### 7.3 Lab
- Buttons: Open folder, Launch JupyterLab, Launch Xschem/KLayout/Qucs-S, **Run checker** (unlimited; shows per-test pass/fail and measured values, never spec-hidden values), Rescope.
- **Rescope** (heavy): user gives a reason (too hard / wrong direction / objection / broken env). Agent edits the node (re-gated) or creates a replacement and marks this one `rescoped`. Reason logged; "too hard" counts as a failure for difficulty control (§7.7).

### 7.4 Tutor (light tier)
- Context injected by backend: brief, prelab questions, user's prelab answers, `hints.yaml`, current workspace text files (size-capped), last checker result, relevant library chunks (if allowed). **Never** `_hidden/`.
- Behavior: Socratic questions; gives hints from the ladder in order.
- **"Show me"** button → heavy model call with `_hidden/` access produces a direct explanation; sets `assist_reveal: true` on all of this node's events for the affected subskill (§8.2).

### 7.5 Proof
Submit → copy `proof_artifacts` to `proof/` → checker job → pass/fail. **No retry penalty.** Pass → Oral, logs `proof_pass` (value 1.0). After 3 failed submissions, offer a remediation node (user may decline).

### 7.6 Oral (push-to-talk voice, text fallback)
- Hold Space or on-screen button; UI sends start/stop; backend records via `sounddevice`, transcribes with faster-whisper using `initial_prompt` = node `vocab` + domain `vocab.txt`. Piper reads questions (toggle).
- Length: all numeric prelab items within tolerance → **2 core questions**; else **5**. Plus **1 interleaved** question when one exists in the key.
- Examiner (light) asks key questions + live probes; it receives `oral_key` questions and probes but **not anchors**.
- Grading (heavy, pinned) over full transcript → §5.7. **Pass** = mean ≥ 2.0 over non-interleaved items and no non-interleaved item scored 0. Interleaved items count toward scores but never fail the node; a 0 logs a misconception.
- Fail → remediation child node (Planner, kind=remediation) + misconceptions logged; parent marked done-with-remediation.

### 7.7 Debrief & Planner
Debrief shows prediction vs. actual, oral grades with quotes, misconceptions logged/resolved, mastery deltas, then Planner options.
Planner input: goals, SUMMARY.md, scores, misconceptions, last 10 nodes' outcomes.
**Difficulty control:** success = proof passed and oral passed without remediation and not rescoped as "too hard". Rolling rate over the last 8 nodes of kind=challenge (exclude remediation/diagnostic/tier_exam). Fewer than 3 such nodes → use diagnostic tier at default sizes. Keep rate in **[0.70, 0.85]**: above → raise tier/complexity; below → lower or add foundations.
**Sizing:** unfamiliar/foundational subskills 15–45 min; integrative builds 1–3 h.
No full tree; only next options. Misconceptions resolve when an interleaved item on them scores ≥ 2.

## 8. Competency system

### 8.1 Framework (ruler)
- Onboarding: heavy agent drafts `competency-v1.yaml` from goals + `framework_template.yaml`: **5–8 domains**, tier descriptors T1–T5 (one observable-behavior sentence each). User edits/approves → **frozen**.
- RFIC template domains: passives & transmission lines, impedance matching, active devices & biasing, noise, linearity & stability, EM & layout, measurement/characterization.
- **Subskills emergent**: Builder may add `domain.subskill` (with tier). Domains/tier text change only via user-approved version bump (`competency-v2.yaml`); old events kept with their `framework_version`.

### 8.2 Event values & weights (code)
| type | value v | weight w |
|---|---|---|
| proof_pass | 1.0 | 1.0 |
| oral_item, tier_exam_item, prelab_explain | score/3 | 1.0 |
| prelab_numeric | 1 if within tol, else max(0, 1 − |err|/(3·tol)) | 0.5 |
| diagnostic_item | score/3 | 1.0 (only sets initial tier; excluded from mastery) |
Modifiers: `interleaved: true` → w × 1.5. `assist_reveal: true` → v × 0.7.

### 8.3 Mastery formula (code, unit-tested)
For domain d at current tier T, take the **most recent 20 events** with domain d and event tier ≤ T, recorded since reaching T:
```
mastery(d,T) = 100 · Σ(w·v) / (Σw + K),   K = 3
```
XP bar = mastery. Reference: 13 events at v=0.8, w=1 → 65.

### 8.4 Tier exams
Unlock when mastery ≥ **65** AND ≥ 3 distinct subskills evidenced at tier T. Exam = node kind=tier_exam spanning the domain's subskills, oral of 6 questions, pass mean ≥ 2.3 and no item 0. Pass → tier T+1, mastery starts fresh. Fail → misconceptions logged, unlock condition re-applies. Diagnostic sets the initial tier per domain. **No time-based conditions anywhere.**

### 8.5 Grading consistency
- `grading_model` = an explicit model ID (setting), used for all grading tasks; lowest available temperature; strict JSON schema; anchored 0–3 descriptors written at build time.
- Changing it is logged; events carry `grader_model`.

## 9. Onboarding
1. **Interview** (light, ≤ 10 questions) → `goals.md` (target capability, motivation, hours/week, background).
2. **Framework draft** (heavy) → user approves (§8.1).
3. **Diagnostic** (light dialogue, heavy grading): ~2 probes per domain, climb tiers until first miss → initial tier per domain; stored as a diagnostic node.
4. Planner proposes first options. Goals editable anytime.

## 10. Library
Upload PDF → job: PyMuPDF text per page (OCR if < 50 chars/page) → ~500-token chunks → fastembed → `library_chunks`. Retrieval: numpy cosine top-k (k=5). Cite `{book_id, page}`. Known limit: equations extract poorly. Free online references (OCW, PDK docs, papers) cited by URL.

## 11. UI screens
| Screen | Contents |
|---|---|
| Preflight | §6.4 checklist with fix actions |
| Home | Current node card + one next-action button; mastery bars; job tray |
| Node | Stage stepper; stage panel; tutor panel; Rescope; provisional badge if grades pending |
| Graph | react-flow DAG (color = status, badge = domain/tier); click → read-only node |
| Competency | Domains × tiers with descriptors; mastery %; tier-exam availability; subskills |
| Oral | Push-to-talk, live transcript, question audio |
| Library | Upload, list, ingest progress |
| Settings | Backends, OpenRouter key, routing, grading model, free-model counters, privacy toggle, usage log, Docker status |
Visual tone: playful shark motif, information-dense.

## 12. Non-goals (v1)
Multi-user, cloud sync, mobile, paid APIs, tapeout, non-RFIC domains (interface only).

## 13. Known limitations (accepted)
Free models may log prompts; textbook equations degrade in extraction; Builder quality bounds the product (validated by M2.5); openEMS speed on laptop limits EM challenge size.

## 14. Milestones

| M | Deliverable | Load sections | Accept when |
|---|---|---|---|
| M0 | Skeleton (pywebview+FastAPI+React, OpenAPI client gen), settings, all [VERIFY] → DECISIONS.md | 0–3, 6.1, 6.4 | Window opens on Windows; every VERIFY recorded |
| M1 | Router: 4 adapters, routing.yaml, counters, logging, failover, env stripping | 0, 3, 5 | Mock tests pass; live smoke on claude_code + one free model; forced 429 fails over; paid ID rejected |
| M2 | `rfic:1`, container manager (§4.2), GUI/Jupyter launch, MCP server, job system, preflight | 0, 4.2, 6 | `claude -p` runs ngspice via MCP; openEMS sample runs; Xschem visible on host; jobs stream to UI |
| **M2.5** | **Builder spike (go/no-go)**: draft `builder.md`; generate 3 nodes (SKY130 circuit, IHP circuit, openEMS passive) with gate script | 0, 4.1, 5, 6.2, 7.1 | ≥ 2 of 3 pass gate within 2 retries; record wall time + usage per node in DECISIONS.md; if failed, stop and report to user before further work |
| M3 | FS + events.jsonl + SQLite cache + `rebuild_db` + competency engine | 0, 4, 5, 8 | Unit tests cover every row of §8.2, §8.3 window, §8.4 unlock; rebuild reproduces scores |
| M4 | Onboarding | 0, 5, 8.1, 9 | Fresh data dir → goals, approved framework, tiers (mock + one live run) |
| M5 | Builder productionized + full gate + interleaving | 0, 4.1, 5, 7.1 | 3 nodes generated end-to-end from Planner options |
| M6 | Lifecycle UI: prelab, lab, tutor, show-me, proof, debrief, rescope, grading queue | 0, 3.1, 5, 7.2–7.5, 7.7 | Node completable in text mode; tutor leaks no `_hidden` value in 5 adversarial prompts |
| M7 | Voice oral + grading | 0, 5.4, 5.7, 7.6 | Transcribes 15 s utterance in < 5 s on CPU; RFIC terms in vocab transcribed correctly in 8/10 test clips; grades valid JSON with quotes |
| M8 | Planner, difficulty control, remediation, misconception resolution, graph | 0, 5.6, 7.7, 11 | Oral fail spawns remediation child; misconception reappears in a later node and resolves; graph renders |
| M9 | Library | 0, 10 | 300-page PDF ingests locally; Builder cites pages |
| M10 | Tier exams + polish | 0, 8.4, 11 | Tier-up from seeded events works end-to-end |

Orchestration: Claude Code owns M0–M3, M5, M2.5 review, and all prompt packs; Cursor does bulk UI (M6, M8 graph, M10) and boilerplate.

## 15. Prompt packs (`app/prompts/`, plus `domains/rfic/prompts/builder.md`)
interviewer, framework_drafter, diagnostician, diagnostic_grader, planner, builder (domain), tutor, show_me, examiner, oral_grader, prelab_explain_grader, rescoper, summarizer. Each ≤ 800 tokens: role, exact inputs, output schema from §5, forbidden behaviors (tutor/examiner: never state hidden values or anchors).