"""M7 acceptance (manual, live): voice oral exam. Run: uv run python -m scripts.m7_acceptance [--clips-dir DIR] [--skip-exam]

Everything that touches learner state runs on a TEMP COPY of data/ (scripts/tempdata.py); the real data/ is only read to make the copy.
The voice models are read from the real download via BLAHAJ_MODELS_DIR.

Parts
 1. microphone: the real sounddevice input opens and delivers samples (a silent room is fine; no one speaks in this run)
 2. STT speed: 15 s windows of speech, faster-whisper small.en int8 CPU, with the node+domain vocab prompt      (accept: < 5 s each)
 3. STT accuracy: 10 clips of RFIC sentences, with and without the vocab prompt                              (accept: >= 8/10 correct)
 4. voice exam A: real STT + real examiner (oral_dialogue) + real Piper + real grader; the "microphone" replays speech clips
    synthesized by Windows SAPI; per-turn latency record -> transcript -> question audio
 5. fail exam B (typed): weak answers -> fail, misconceptions, live Planner remediation option (the Builder is stubbed: no build runs)

HONEST LIMITS: the test speech is synthetic (Windows SAPI voices David/Zira, an engine independent of Piper), not a human voice with
accent, hesitation or room noise, so accuracy on real speech can be lower. The microphone capture itself is only checked for
"opens and delivers samples".
"""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.voice.models import models_dir  # noqa: E402

REAL_MODELS = models_dir()  # resolved against the real data/ before the temp copy takes over
os.environ["BLAHAJ_MODELS_DIR"] = str(REAL_MODELS)

from scripts.tempdata import temp_data  # noqa: E402

SR = 16_000
# (spoken text as a person would say it, terms that must appear in the transcript)
CLIPS = [
    ("The S eleven of the L-match drops below minus ten dee bee near two point four gigahertz.", ["S11", "L-match"]),
    ("On the Smith chart, a series inductor moves the reflection coefficient clockwise toward the inductive half.", ["Smith chart", "reflection coefficient"]),
    ("The Miller effect multiplies C G D, and the cascode stage reduces it, so the bandwidth improves.", ["Miller effect", "Cgd", "cascode"]),
    ("We de-embedded the G S G pad parasitics using an open-short structure on the same wafer.", ["GSG pad", "open-short"]),
    ("The noise figure of the L N A follows the Friis formula for a cascade of stages.", ["noise figure", "LNA", "Friis"]),
    ("The S G thirteen G two H B T has an f T above two hundred gigahertz.", ["SG13G2", "HBT", "fT"]),
    ("Third order intercept, I I P three, is measured with a two-tone test.", ["IIP3", "two-tone"]),
    ("The spiral inductor quality factor collapses past its self-resonance because of substrate loss.", ["quality factor", "self-resonance", "substrate loss"]),
    ("Phase noise in the V C O limits the P L L jitter.", ["phase noise", "VCO", "PLL"]),
    ("ngspice and openEMS both give the transmission line S twenty one and the characteristic impedance.", ["ngspice", "openEMS", "S21", "characteristic impedance"]),
]
ANSWERS_A = {  # node 0004 oral items (core o1, o2, interleaved o3): what a learner who knows the material would say
    "o1": "The residual comes from the feed inductance and the pad capacitance in the frame. The open short model assumes the shunt parasitics sit in front of "
          "the series ones, so it does not capture their interaction, and the error grows with frequency, roughly with omega squared times L F times C F, "
          "so it is small at thirty gigahertz and larger at one hundred.",
    "o2": "Y does not depend on the reference impedance, it is a property of the network. S depends on Z zero, so I convert to S at fifty ohm just to match the "
          "instruments and the reporting convention. Changing Z zero changes S but not the de-embedded Y.",
    "o3": "I would run an EM simulation of the open and short pad structures with proper ports referenced to the ground of the G S G pad, and extract the capacitance "
          "from the imaginary part of Y divided by omega and the inductance from the imaginary part of Z divided by omega.",
}
PROBE_ANSWER = ("Because the shunt parasitic sits in front of the series elements, so the open-short model misses their interaction, and the error scales with "
                "frequency squared, so a more detailed dummy structure would capture it.")
WEAK = "I am not sure. I think it is mostly numerical noise in the simulator, and the pad does not really matter."


def say(msg: str) -> None:
    print(msg, flush=True)


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


PS1 = """param($Voice,$TextFile,$Out)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice($Voice)
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$s.SetOutputToWaveFile($Out, $fmt)
$s.Speak((Get-Content -Raw -Encoding UTF8 $TextFile))
$s.Dispose()
"""


def sapi(text: str, out: Path, voice: str, clips: Path) -> np.ndarray:
    """Synthesize `text` with a Windows SAPI voice (cached) -> float32 mono 16 kHz."""
    if not out.is_file():
        ps1, txt = clips / "sapi.ps1", out.with_suffix(".txt")
        ps1.write_text(PS1, encoding="utf-8")
        txt.write_text(text, encoding="utf-8")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Voice", voice, "-TextFile", str(txt), "-Out", str(out)],
                       check=True, capture_output=True)
    with wave.open(str(out)) as w:  # SAPI is asked for 16 kHz 16-bit mono, exactly what the recorder delivers
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SR, 1, 2)
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


class FileRecorder:
    """Stands in for the microphone: replays queued clips as 'recordings' (the rest of the pipeline is real)."""

    def __init__(self):
        self.queue: list[np.ndarray] = []
        self.recording = False

    def start(self):
        self.recording = True

    def stop(self):
        self.recording = False
        return self.queue.pop(0)

    def cancel(self):
        self.recording = False


def part1_mic() -> dict:
    import sounddevice as sd

    dev = sd.query_devices(kind="input")
    t0 = time.perf_counter()
    rec = sd.rec(int(1.5 * SR), samplerate=SR, channels=1, dtype="float32")
    sd.wait()
    rms = float(np.sqrt(np.mean(rec**2)))
    return {"device": dev["name"], "samples": int(len(rec)), "rms": round(rms, 5), "seconds": round(time.perf_counter() - t0, 2)}


def stt_prompt() -> str:
    from app.voice.stt import build_prompt

    vocab = [ln.strip() for ln in (Path(__file__).resolve().parents[1] / "domains/rfic/vocab.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
    return build_prompt([], vocab)


def part2_3(clips: Path) -> tuple[dict, dict]:
    from app.voice.service import Voice

    tr = Voice().transcriber
    tr.load()
    audio = []
    for i, (text, _terms) in enumerate(CLIPS):
        voice = "Microsoft David Desktop" if i % 2 == 0 else "Microsoft Zira Desktop"
        audio.append(sapi(text, clips / f"vocab{i + 1:02d}.wav", voice, clips))
    prompt = stt_prompt()
    tr.transcribe(audio[0][: 3 * SR], prompt)  # warm-up (thread pools, first-use allocations)

    # part 2: 15 s windows cut from the concatenated speech
    gap = np.zeros(int(0.4 * SR), dtype=np.float32)
    long = np.concatenate([np.concatenate([a, gap]) for a in audio])
    say(f"speech pool: {len(long) / SR:.0f} s")
    times = []
    for k in range(5):
        start = k * 12 * SR
        win = long[start : start + 15 * SR]
        if len(win) < 15 * SR:
            win = np.pad(win, (0, 15 * SR - len(win)))
        r = tr.transcribe(win, prompt)
        times.append(r.seconds)
        say(f"  15 s window {k + 1}: {r.seconds:.2f} s  -> {r.text[:90]}")
    speed = {"seconds": [round(t, 2) for t in times], "median": round(statistics.median(times), 2), "max": round(max(times), 2), "pass": max(times) < 5.0,
             "threads": tr.cpu_threads, "beam": tr.beam_size}  # fmt: skip

    # part 3: accuracy with and without the prompt
    acc = {"with_prompt": [], "without_prompt": []}
    for label, pr in (("with_prompt", prompt), ("without_prompt", "")):
        for i, ((text, terms), a) in enumerate(zip(CLIPS, audio)):
            r = tr.transcribe(a, pr)
            n = norm(r.text)
            missing = [t for t in terms if norm(t) not in n]
            acc[label].append({"clip": i + 1, "ok": not missing, "missing": missing, "heard": r.text, "audio_s": round(r.audio_s, 1), "stt_s": round(r.seconds, 2)})
            say(f"  [{label}] clip {i + 1:2d} {'OK ' if not missing else 'BAD'} {r.seconds:4.1f}s  {r.text}" + (f"   MISSING {missing}" if missing else ""))
    summary = {k: sum(1 for c in v if c["ok"]) for k, v in acc.items()}
    return speed, {"correct": summary, "pass": summary["with_prompt"] >= 8, "clips": acc}


def seed_oral(lc, node_id: int, wrong: bool) -> None:
    """Put a node at stage oral with locked prelab answers (acceptance harness: the lab/proof stages were exercised in M6c)."""
    ref = lc.store.find(node_id)
    answers = {}
    for it in lc.store.prelab_key(ref):
        if it["kind"] == "numeric":
            answers[it["id"]] = float(it["value"]) * (3.0 if wrong else 1.0)
        else:
            answers[it["id"]] = "I do not know"
    (ref.path / "prelab_answers.yaml").write_text(yaml.safe_dump({"submitted": "2026-11-11T00:00:00+00:00", "answers": answers, "units": {}}), encoding="utf-8")
    lc.store.set_scalar(ref, "status", "oral")
    from app.datastore.cache import rebuild_db

    rebuild_db(lc.root)


def part4_voice_exam(clips: Path) -> dict:
    from app.domains import load_domain
    from app.llm.router import Router
    from app.lifecycle.service import Lifecycle
    from app.voice.service import Voice

    with temp_data() as root:
        say(f"temp data copy: {root}")
        rec = FileRecorder()
        voice = Voice(recorder=rec)
        lc = Lifecycle(root, Router(), load_domain(), voice=voice, jobs=None)
        seed_oral(lc, 4, wrong=False)
        ref = lc.store.find(4)
        voice.warm_up()  # models loaded once at app start in real use; the first-turn load cost is reported separately below
        t0 = time.perf_counter()
        v = lc.oral.start(ref, "voice", True)
        start_s = time.perf_counter() - t0
        say(f"exam started in {start_s:.1f} s: plan {v['plan']}")
        say(f"  EXAMINER: {v['turns'][0]['text']}")
        turns, qn = [], 0
        for _ in range(12):
            st = lc.oral._state(ref)
            if st["status"] == "complete":
                break
            last = [t for t in st["turns"] if t["role"] == "examiner"][-1]
            if last["kind"] == "question":
                item = last["item_id"]
                text = ANSWERS_A.get(item, PROBE_ANSWER)
                qn += 1
            else:
                text = PROBE_ANSWER
            audio = sapi(text, clips / f"ans_{len(turns) + 1:02d}.wav", "Microsoft Zira Desktop", clips)
            rec.queue.append(audio)
            lc.oral.record_start(ref)
            t1 = time.perf_counter()
            out = lc.oral.record_stop(ref)
            wall = time.perf_counter() - t1
            lat = out["view"]["latency"][-1]
            turns.append({"item": last["item_id"], "answer_s": round(len(audio) / SR, 1), "wall_s": round(wall, 2), **{k: lat[k] for k in lat if k != "n"},
                          "heard": out["transcript"], "examiner_kind": out["examiner"]["kind"], "source": out["examiner"]["source"]})  # fmt: skip
            say(f"  turn {len(turns)} ({last['item_id']}): record {lat['record_s']}s | stt {lat['stt_s']}s | examiner {lat['llm_s']}s | tts {lat['tts_s']}s "
                f"| stop->audio {lat['total_s']}s (request wall {wall:.2f}s)")
            say(f"    HEARD:    {out['transcript'][:140]}")
            say(f"    EXAMINER: [{out['examiner']['kind']}/{out['examiner']['source']}] {out['examiner']['text']}")
        say("finishing + grading (real pinned grader)...")
        t2 = time.perf_counter()
        d = lc.oral_finish(4)
        grade_s = time.perf_counter() - t2
        o = d["oral"]
        say(f"graded in {grade_s:.1f} s: status {o['status']} passed {o['passed']} mean {o['mean']} model {o['model']}")
        for it in o["items"]:
            say(f"  {it['id']} score {it['score']} interleaved {it['interleaved']} quote {it['quote']!r}")
        st = lc.oral._state(ref)
        learner = re.sub(r"[^a-z0-9]+", " ", " ".join(t["text"] for t in st["turns"] if t["role"] == "learner").lower())
        quotes_ok = all(not it["quote"] or re.sub(r"[^a-z0-9]+", " ", it["quote"].lower()).strip() in learner for it in o["items"])
        ev = [e for e in __import__("app.datastore.cache", fromlist=["DataStore"]).DataStore(root).events() if e.node_id == 4 and e.type == "oral_item"]
        walls = [t["wall_s"] for t in turns]
        sources = {t["source"] for t in turns}
        return {"start_s": round(start_s, 1), "turns": turns, "median_stop_to_audio_s": round(statistics.median(t["total_s"] for t in turns), 2),
                "max_stop_to_audio_s": max(t["total_s"] for t in turns), "median_wall_s": round(statistics.median(walls), 2), "grade_s": round(grade_s, 1),
                "oral": {k: o[k] for k in ("status", "passed", "mean", "model", "items", "remediation")}, "quotes_verbatim": quotes_ok,
                "events_written": len(ev), "examiner_sources": sorted(s for s in sources if s), "grader_json_valid": o["status"] == "graded" and len(o["items"]) == len(ev)}  # fmt: skip


def part5_fail_exam() -> dict:
    from app.domains import load_domain
    from app.llm.router import Router
    from app.lifecycle.service import Lifecycle
    from app.voice.service import Voice

    stub: dict = {}

    class StubBuild:  # the live Planner call is exercised; the heavy Builder is not started
        def __init__(self, *a):
            pass

        def start(self, jobs, option, on_success=None):
            stub["option"] = option
            return SimpleNamespace(id="stub-job"), SimpleNamespace(name="0007-stub", node_id=7)

    import app.lifecycle.oral as oral_mod

    oral_mod.BuildService = StubBuild  # type: ignore[misc]
    with temp_data() as root:
        lc = Lifecycle(root, Router(), load_domain(), voice=Voice(), jobs=SimpleNamespace(store=SimpleNamespace(get=lambda _i: None)))
        seed_oral(lc, 6, wrong=True)
        ref = lc.store.find(6)
        v = lc.oral.start(ref, "text", False)
        say(f"fail exam plan: {v['plan']}")
        n = 0
        while lc.oral.view(ref)["status"] == "active" and n < 20:
            lc.oral.answer(ref, WEAK)
            n += 1
        d = lc.oral_finish(6)
        o = d["oral"]
        from app.datastore.misconceptions import fold, read_records

        mis = [m for m in fold(read_records(lc.store.layout.misconceptions_path)) if m["node_id"] == 6]
        say(f"graded: passed {o['passed']} mean {o['mean']}; remediation {o['remediation'] and o['remediation']['status']}; misconceptions {len(mis)}")
        for it in o["items"]:
            say(f"  {it['id']} score {it['score']} {'(interleaved)' if it['interleaved'] else ''} quote {it['quote']!r}")
        return {"answers": n, "oral": {k: o[k] for k in ("status", "passed", "mean", "model", "items")}, "remediation": o["remediation"],
                "remediation_option": stub.get("option"), "misconceptions": [{"subskill": m["subskill"], "text": m["text"][:160]} for m in mis]}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-dir", default=str(Path(os.environ.get("TEMP", ".")) / "m7clips"))
    ap.add_argument("--skip-exam", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    clips = Path(a.clips_dir)
    clips.mkdir(parents=True, exist_ok=True)
    res: dict = {"models_dir": str(REAL_MODELS)}
    say("== 1. microphone")
    try:
        res["microphone"] = part1_mic()
        say(f"  {res['microphone']}")
    except Exception as e:  # noqa: BLE001
        res["microphone"] = {"error": str(e)}
        say(f"  FAILED: {e}")
    say("== 2/3. speech-to-text speed and accuracy")
    res["stt_speed"], res["stt_accuracy"] = part2_3(clips)
    say(f"  speed: {res['stt_speed']}")
    say(f"  accuracy: {res['stt_accuracy']['correct']} of {len(CLIPS)} (accept >= 8 with the prompt)")
    if not a.skip_exam:
        say("== 4. voice exam A (real STT, examiner, Piper, grader)")
        res["voice_exam"] = part4_voice_exam(clips)
        say("== 5. fail exam B (typed)")
        res["fail_exam"] = part5_fail_exam()
    out = Path(a.out) if a.out else clips / "m7_results.json"
    out.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    say(f"results: {out}")


if __name__ == "__main__":
    main()
