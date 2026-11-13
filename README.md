# BlahajSphere

BlahajSphere is a desktop app that teaches simulation-heavy engineering by giving you one hands-on task at a time. An AI agent picks the next challenge based on your goals and what you have already done, then builds a ready-to-use workspace for it: starter files, a simulation toolchain in Docker, and a checker that tells you whether your result is right. The first supported domain is RFIC design.

It is not a chat tutor. You do the work, the checker verifies it, and then you explain your reasoning out loud in an oral exam. Your competency scores come from that evidence, calculated by code, never by asking a model how you are doing.

This is a personal project that runs on Windows. It is built to cost nothing beyond subscriptions you already have.

## How it works

Each task is called a node, and every node goes through the same stages:

1. **Pick.** The Planner offers two to four options for what to work on next. You choose one.
2. **Build.** A builder agent writes the node: a brief, starter files, a hidden reference solution, a checker, and the hint ladder. A release gate verifies it before you see it. For example, the checker must pass on the reference solution and fail on the starter files.
3. **Prelab.** You answer a few questions and predict some numbers before the workspace unlocks.
4. **Lab.** You work in the workspace with JupyterLab and the simulation tools (ngspice, openEMS, Xschem, KLayout, Qucs-S). A Socratic tutor gives hints, not answers. You can ask it to "show me", which reveals the answer but lowers how much that node counts toward your score.
5. **Proof.** You submit your result and the checker runs. You can submit as many times as you like.
6. **Oral.** You answer questions by voice (hold Space to talk), or by text if you prefer. A grader scores the transcript against a hidden answer key.
7. **Debrief.** You see your prediction against the real result, your oral grades with quotes, and your mastery change. Then the Planner offers the next options.

If you fail the oral, the app creates a short remediation node aimed at the specific misunderstanding. If a task is too hard or broken, you can rescope it.

Mastery is tracked per domain and per tier (T1 to T5). When a domain reaches the unlock threshold, the Planner offers a tier exam: a six-question oral across the domain. Passing it moves you up a tier.

You can keep several independent tracks, each with its own goals, competency framework and progress. One track is active at a time.

## What you need

- Windows 11 (the voice and window code is written for it)
- Docker Desktop with the WSL2 backend, and about 10 GB of free disk space beyond the toolchain image
- Python 3.11 to 3.13 and [uv](https://docs.astral.sh/uv/)
- Node.js and npm, to build the interface
- The [Claude Code](https://claude.com/claude-code) CLI, installed and logged in. The builder, grader and tutor call it, so a Claude subscription is required.
- A microphone, if you want to do orals by voice
- Optional: an [OpenRouter](https://openrouter.ai) API key. It is only used for free models (the app refuses paid ones), as a first choice for light tasks like the intake interview and as a fallback for tutoring.

## Setup

From the project folder:

```
uv sync
cd ui
npm install
npm run build
cd ..
uv run python -m app.voice download
```

The last command downloads the speech-to-text and text-to-speech models (roughly 500 MB). They are stored in `data/models/`.

Then start the app:

```
uv run python -m app
```

On first launch, the Setup screen runs a preflight check. It looks at Docker, the toolchain image, the Claude CLI login, the voice models, disk space and your microphone, and shows a fix for anything that fails. If the toolchain image is missing, it can build it for you. That downloads a large base image and takes a while.

After preflight, onboarding asks about your goals, drafts a competency framework for you to edit and approve, and runs a short diagnostic to set your starting tiers.

## Using your own books

The Library screen accepts PDFs. Text is extracted (with OCR for scanned pages), split into chunks, and embedded locally. When the builder creates a node, the most relevant passages are included and cited by page number. Equations extract poorly from PDFs, so expect better results on prose than on math.

## Where your data lives

Everything is stored on disk in `data/`, which is ignored by this repository. The folder is its own git repo, and the app commits to it at stage transitions so you have a history of your progress.

- `tracks/` holds each track's goals, framework, and archived nodes
- `events.jsonl` and `misconceptions.jsonl` are the append-only record that all scores are computed from
- `cache.db` is a SQLite cache that can be rebuilt from those logs
- `library/` holds ingested books
- `models/` holds the voice models
- `settings.json` holds your settings, including any API key, and is not committed even in the inner repo

Nothing leaves your machine except the calls to the language models. Library text is sent only to Claude unless you turn on the setting that allows free models to see it.

To start over, use the Tracks screen to reset or delete a track. Deleted tracks go to `data/_trash/` until you purge them.

## Models and cost

Which model handles which job is set in `config/routing.yaml`, and you can edit it. By default:

- Planning, building, rescoping and grading use Claude Sonnet through the Claude CLI. The grader is pinned to one model so scores stay consistent.
- Tutoring and the live oral examiner use Claude Haiku, with free OpenRouter models as a fallback.
- The intake interview and diagnostic dialogue try free OpenRouter models first.

The Settings screen shows daily usage per model.

## Development

```
uv run pytest            # backend tests, all on a mock LLM adapter
cd ui && npm test        # interface tests
cd ui && npm run dev     # Vite dev server
```

After changing the API, regenerate the TypeScript client:

```
uv run python -m scripts.gen_client
cd ui && npm run gen:client
```

Domain-specific code lives only in `domains/rfic/`: the Docker image, the simulation tool server, the builder prompt and the starter competency framework. Adding a new domain means adding another folder there.

```
app/          backend: API, LLM router, planner, builder, lifecycle, competency engine
ui/           React interface
domains/rfic/ everything specific to RFIC
config/       model routing
docs/         SPEC.md (the design), DECISIONS.md (running log of choices), AGENTS.md
scripts/      acceptance and smoke scripts
tests/        backend tests
```

## Status

All ten milestones in the spec are implemented and tested. It has not yet been used for real learning, and some parts have only been exercised in automated tests: most screens have not been checked in a real browser, tier exams have never run with the real model, and your own PDFs have not been ingested. The full list of known gaps is at the end of `docs/DECISIONS.md`.

## Documentation

- [docs/SPEC.md](docs/SPEC.md) is the full design, including data schemas, the competency math and the node lifecycle.
- [docs/DECISIONS.md](docs/DECISIONS.md) records every deviation and non-obvious choice, with measurements.
