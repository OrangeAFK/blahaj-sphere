"""M8 live measurement: per-turn latency of the oral exam (stop recording -> next question's audio ready), real STT + Piper + examiner.

    BLAHAJ_DATA_DIR=<real data dir> uv run python -m scripts.m8_latency [--label after] [--thin-first] [--clips-dir DIR] [--out FILE]

Run it once against the code BEFORE the M8 oral change (a git worktree of the previous commit, same script) and once after: it only uses
APIs both versions have (Lifecycle.oral.start / record_start / record_stop and the per-turn `latency` record), and the same cached speech clips.
All learner state lives in a TEMP COPY of data/ (scripts/tempdata.py); the real data/ is only read. Node 0004 of track rfic-default is put at
stage oral with exactly right prelab answers (2 core questions + 1 interleaved). "Microphone" = SAPI speech replayed through the real STT.
Nothing is graded (no pinned-grader call): this measures the turn loop only.

Honest limits: synthetic speech, one machine, a handful of turns per run; the examiner is claude-haiku-5-5 through the CLI, whose latency varies.
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.voice.models import models_dir  # noqa: E402

os.environ["BLAHAJ_MODELS_DIR"] = str(models_dir())  # the real download, resolved before the temp copy takes over

from scripts.legacy.m7_acceptance import ANSWERS_A, PROBE_ANSWER, FileRecorder, sapi  # noqa: E402
from scripts.tempdata import temp_data  # noqa: E402

TRACK = "rfic-default"
NODE = 4
THIN = "I think it is the pad capacitance."  # fewer than 25 words: the new flow asks a live probe, the old one asks the examiner every turn


def say(m: str) -> None:
    print(m, flush=True)


def seed_oral(lc) -> None:
    import yaml

    from app.datastore.cache import rebuild_db

    ref = lc.store.find(NODE)
    answers = {it["id"]: float(it["value"]) if it["kind"] == "numeric" else "I do not know" for it in lc.store.prelab_key(ref)}
    (ref.path / "prelab_answers.yaml").write_text(yaml.safe_dump({"submitted": "2026-11-11T00:00:00+00:00", "answers": answers, "units": {}}), encoding="utf-8")
    lc.store.set_scalar(ref, "status", "oral")
    rebuild_db(lc.root)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--thin-first", action="store_true", help="answer the first question thinly (exercises the probe path)")
    ap.add_argument("--clips-dir", default=str(Path(os.environ.get("TEMP", ".")) / "m8clips"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    clips = Path(a.clips_dir)
    clips.mkdir(parents=True, exist_ok=True)

    from app.domains import load_domain
    from app.lifecycle.service import Lifecycle
    from app.llm.router import Router
    from app.voice.service import Voice

    with temp_data() as root:
        say(f"[{a.label}] temp data copy {root}")
        rec = FileRecorder()
        voice = Voice(recorder=rec)
        lc = Lifecycle(root, TRACK, Router(), load_domain(), voice=voice, jobs=None)
        seed_oral(lc)
        ref = lc.store.find(NODE)
        voice.warm_up()
        t0 = time.perf_counter()
        v = lc.oral.start(ref, "voice", True)
        start_s = time.perf_counter() - t0
        say(f"[{a.label}] exam started in {start_s:.1f} s (first question, incl. its audio); plan {v['plan']}")
        turns = []
        for _ in range(12):
            st = lc.oral._state(ref)
            if st["status"] == "complete":
                break
            last = [t for t in st["turns"] if t["role"] == "examiner"][-1]
            if last["kind"] == "question":
                text = THIN if (a.thin_first and not turns) else ANSWERS_A.get(last["item_id"], PROBE_ANSWER)
            else:
                text = PROBE_ANSWER
            audio = sapi(text, clips / f"ans_{hashlib.md5(text.encode()).hexdigest()[:10]}.wav", "Microsoft Zira Desktop", clips)
            rec.queue.append(audio)
            lc.oral.record_start(ref)
            t1 = time.perf_counter()
            out = lc.oral.record_stop(ref)
            wall = time.perf_counter() - t1
            lat = out["view"]["latency"][-1]
            turns.append({"after_item": last["item_id"], "answer_kind": "thin" if text == THIN else "full", "answer_s": round(len(audio) / 16000, 1), "request_wall_s": round(wall, 2),
                          "examiner_kind": out["examiner"]["kind"], "source": out["examiner"].get("source"), **{k: v for k, v in lat.items() if k != "n"}})  # fmt: skip
            say(f"  turn {len(turns)} after {last['item_id']} ({turns[-1]['answer_kind']}, {turns[-1]['answer_s']}s): stt {lat.get('stt_s')} | examiner {lat['llm_s']} | "
                f"tts {lat['tts_s']} | stop->audio {lat['total_s']} | {out['examiner']['kind']}/{out['examiner'].get('source')}: {out['examiner']['text'][:70]}")
        tot = [t["total_s"] for t in turns]
        summary = {"label": a.label, "turns": turns, "first_question_s": round(start_s, 2), "n_turns": len(turns),
                   "median_stop_to_audio_s": round(statistics.median(tot), 2), "max_stop_to_audio_s": max(tot),
                   "median_non_stt_s": round(statistics.median(t["total_s"] - t.get("stt_s", 0) for t in turns), 2)}  # fmt: skip
        say(f"[{a.label}] median stop->audio {summary['median_stop_to_audio_s']} s, max {summary['max_stop_to_audio_s']} s, median excluding STT {summary['median_non_stt_s']} s")
        out_path = Path(a.out) if a.out else clips / f"m8_latency_{a.label}.json"
        out_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
        say(f"[{a.label}] wrote {out_path}")


if __name__ == "__main__":
    main()
