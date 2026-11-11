"""Oral exam (SPEC §7.6): adaptive length, a light examiner, a pinned heavy grader, the pass rule and the remediation child.

Flow code (not the examiner model) owns the exam: which items are asked, in which order, at most 2 probes per item, and when it is
done. The examiner only supplies the wording of the next utterance; anything that breaks the flow is replaced by the key's own
question text (`source: "coerced"`), and a failed examiner call falls back the same way (`source: "fallback"`), so a model outage
never strands the learner. The examiner gets questions and probes but never anchors; anchors go only to the grader.

Exam state lives in data/sessions/<node dir>/oral.json; the transcript is also written to the node's oral.md at the end (§4.1).
"""

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from app.builder.service import BuildError, BuildService
from app.datastore.cache import rebuild_db
from app.datastore.misconceptions import add_misconception, fold, read_records
from app.lifecycle.debrief import GRADE_SCHEMA, refresh_provisional, tolerance_abs
from app.lifecycle.nodes import LifecycleError, NodeRef, NodeStore, append_jsonl, autocommit, node_lock
from app.lifecycle.tutor import public_checker
from app.llm.types import LLMError, Request
from app.planner.service import Planner, PlannerError
from app.voice.stt import SAMPLE_RATE, VoiceUnavailable, build_prompt

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
GRADE_TASK = "grade_oral"
EXAMINER_TASK = "oral_dialogue"
EXAMINER_SCHEMA = {
    "type": "object",
    "properties": {"say": {"type": "string"}, "item_id": {"type": "string"}, "kind": {"type": "string", "enum": ["question", "probe", "done"]}},
    "required": ["say", "item_id", "kind"],
}
MAX_PROBES = 2  # §7.6 examiner prompt: at most 2 follow-ups per item
N_CORE_CLEAN, N_CORE_MISSED = 2, 5  # §7.6: all numeric prelab items within tolerance -> 2 core questions, else 5
PASS_MEAN = 2.0  # §7.6: mean >= 2.0 over non-interleaved items and none scored 0
MAX_ANSWER_CHARS = 4_000
MAX_LEARNER_TURNS = 40  # hard stop against a runaway exam
CLOSING = "Thanks, that's all my questions."


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _qnorm(s: str) -> str:
    """Quote matching ignores case, punctuation and spacing: the grader sees speech-to-text, which has its own punctuation."""
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def oral_verdict(results: list[dict]) -> dict:
    """§7.6 pass rule over graded items: mean >= 2.0 over the non-interleaved ones and none of them scored 0.
    Interleaved items count toward scores elsewhere but never fail the node."""
    core = [r["score"] for r in results if not r["interleaved"]]
    mean = sum(core) / len(core) if core else None
    passed = not core or (sum(core) >= PASS_MEAN * len(core) and min(core) > 0)  # integer compare: no float slack needed
    return {"passed": passed, "mean": mean, "core_n": len(core)}


class OralExam:
    def __init__(self, store: NodeStore, router, domain, debrief, voice=None, jobs=None):
        self.store, self.router, self.domain, self.debrief = store, router, domain, debrief
        self.voice = voice
        self.jobs = jobs  # job manager: needed to start the remediation build (None -> remediation is recorded as "needed")

    # --- state ------------------------------------------------------------------------------------------------
    def _state(self, ref: NodeRef) -> dict:
        s = self.store.session(ref, "oral", {})
        s.setdefault("status", "none")
        s.setdefault("mode", "text")
        s.setdefault("tts", False)
        s.setdefault("plan", None)
        s.setdefault("cursor", {"i": -1, "probes": 0})
        s.setdefault("turns", [])
        s.setdefault("grading", {"status": "none", "results": [], "model": None, "passed": None, "mean": None})
        s.setdefault("remediation", None)
        return s

    def _save(self, ref: NodeRef, st: dict) -> None:
        self.store.save_session(ref, "oral", st)

    def _items(self, ref: NodeRef, st: dict) -> list[dict]:
        by_id = {it["id"]: it for it in self.store.oral_key(ref)}
        return [by_id[i] for i in st["plan"]["items"] if i in by_id]

    # --- plan (§7.6 length) ----------------------------------------------------------------------------------
    def plan(self, ref: NodeRef) -> dict:
        key = self.store.oral_key(ref)
        answers = self.debrief._answers(ref)
        missed = []
        for it in self.store.prelab_key(ref):
            if it.get("kind") != "numeric":
                continue
            ans = answers.get(it["id"])
            if ans is None or abs(float(ans) - float(it["value"])) > tolerance_abs(it):
                missed.append(it["subskill"])
        n_core = N_CORE_CLEAN if not missed else N_CORE_MISSED
        core = [it for it in key if not it.get("interleaved")]
        core.sort(key=lambda it: it.get("subskill") not in missed)  # stable: questions on missed prelab subskills first
        picked = core[:n_core]
        inter = [it for it in key if it.get("interleaved")][:1]  # one interleaved question when the key has one
        reason = ("all numeric prelab answers were within tolerance" if not missed
                  else f"{len(set(missed))} prelab subskill(s) were outside tolerance: " + ", ".join(sorted(set(missed))))  # fmt: skip
        return {"n_core": len(picked), "n_interleaved": len(inter), "items": [it["id"] for it in [*picked, *inter]], "reason": reason}

    # --- view -----------------------------------------------------------------------------------------------------
    def voice_status(self) -> dict:
        if self.voice is None:
            return {"stt": False, "tts": False, "microphone": False}
        return self.voice.status()

    def view(self, ref: NodeRef) -> dict:
        st = self._state(ref)
        self._refresh_remediation(ref, st)
        plan = st["plan"]
        n_items = len(plan["items"]) if plan else 0
        asked = sum(1 for t in st["turns"] if t["role"] == "examiner" and t["kind"] == "question")
        return {
            "implemented": True, "stage": self.store.status(ref), "status": st["status"], "mode": st["mode"], "tts": st["tts"],
            "voice": self.voice_status(), "recording": bool(self.voice and self.voice.recorder.recording),
            "plan": {"n_core": plan["n_core"], "n_interleaved": plan["n_interleaved"], "reason": plan["reason"]} if plan else None,
            "progress": {"asked": asked, "of": n_items}, "can_finish": st["status"] == "complete",
            "turns": [self._public_turn(ref, t) for t in st["turns"]],
            "latency": [{"n": t["n"], **t["latency"]} for t in st["turns"] if t["role"] == "examiner" and t.get("latency")],
        }  # fmt: skip

    @staticmethod
    def _public_turn(ref: NodeRef, t: dict) -> dict:
        out = {"n": t["n"], "role": t["role"], "text": t["text"], "kind": t.get("kind"), "source": t.get("source")}
        if t.get("audio"):
            out["audio_url"] = f"/api/nodes/{ref.id}/oral/audio/{t['n']}"
        return out

    # --- start ----------------------------------------------------------------------------------------------
    def start(self, ref: NodeRef, mode: str, tts: bool) -> dict:
        if mode not in ("voice", "text"):
            raise LifecycleError(422, "mode must be 'voice' or 'text'")
        with node_lock(ref.track, ref.id):
            self.store.require(ref, "oral")
            st = self._state(ref)
            if mode == "voice":
                vs = self.voice_status()
                missing = [name for name, ok in (("speech-to-text model", vs["stt"]), ("microphone", vs["microphone"])) if not ok]
                if missing:
                    raise LifecycleError(409, "voice mode needs a " + " and a ".join(missing) + "; use text mode (it is graded the same way)")
            if tts and not self.voice_status()["tts"]:
                tts = False  # spoken questions are optional: the text is always shown
            st["mode"], st["tts"] = mode, bool(tts)
            if self.voice is not None and (mode == "voice" or st["tts"]):
                threading.Thread(target=self.voice.warm_up, kwargs={"stt": mode == "voice", "tts": st["tts"]}, daemon=True).start()
            if st["status"] == "none":
                st["plan"] = self.plan(ref)
                if not st["plan"]["items"]:
                    raise LifecycleError(409, "this node has no oral questions")
                st["status"], st["started"] = "active", _now()
                self._examiner_turn(ref, st, {"t0": time.perf_counter()})
            self._save(ref, st)
        return self.view(ref)

    # --- turns ------------------------------------------------------------------------------------------------
    def answer(self, ref: NodeRef, text: str, source: str = "typed", timing: dict | None = None) -> dict:
        """One learner answer (typed, or the transcript of a recording) -> the examiner's next utterance."""
        text = text.strip()
        if not text:
            raise LifecycleError(422, "say or type an answer first")
        if len(text) > MAX_ANSWER_CHARS:
            raise LifecycleError(422, f"answers are limited to {MAX_ANSWER_CHARS} characters")
        timing = {"t0": time.perf_counter(), **(timing or {})}
        with node_lock(ref.track, ref.id):
            self.store.require(ref, "oral")
            st = self._state(ref)
            if st["status"] != "active":
                raise LifecycleError(409, "the oral exam is not running" if st["status"] == "none" else "the exam is complete: finish it to see the debrief")
            n = len(st["turns"]) + 1
            st["turns"].append({"n": n, "role": "learner", "text": text, "kind": "answer", "source": source, "item_id": self._current_item_id(ref, st),
                                "latency": {k: round(v, 3) for k, v in timing.items() if k in ("record_s", "stt_s")}})  # fmt: skip
            examiner = self._examiner_turn(ref, st, timing)
            self._save(ref, st)
        return {"learner": self._public_turn(ref, st["turns"][-2]), "examiner": self._public_turn(ref, examiner), "done": st["status"] == "complete",
                "view": self.view(ref)}  # fmt: skip

    def _current_item_id(self, ref: NodeRef, st: dict) -> str | None:
        items = self._items(ref, st)
        i = st["cursor"]["i"]
        return items[i]["id"] if 0 <= i < len(items) else None

    def _examiner_turn(self, ref: NodeRef, st: dict, timing: dict) -> dict:
        items = self._items(ref, st)
        learner_turns = sum(1 for t in st["turns"] if t["role"] == "learner")
        t_llm = time.perf_counter()
        raw = None if learner_turns >= MAX_LEARNER_TURNS else self._ask_examiner(ref, st, items)
        llm_s = time.perf_counter() - t_llm
        say, item_id, kind, source = self._enforce(st, items, raw, force_done=learner_turns >= MAX_LEARNER_TURNS)
        n = len(st["turns"]) + 1
        turn = {"n": n, "role": "examiner", "text": say, "kind": kind, "item_id": item_id, "source": source, "audio": False}
        tts_s = 0.0
        if st["tts"] and self.voice is not None:
            try:
                wav, tts_s = self.voice.speaker.synthesize(say)
                self.voice.put_audio(ref.track, ref.id, n, wav)
                turn["audio"] = True
            except (VoiceUnavailable, OSError, RuntimeError) as e:  # a TTS problem must not stop the exam: the text is shown anyway
                turn["tts_error"] = str(e)[:200]
        # record -> transcript -> question audio (M7 acceptance): per-stage seconds; total_s runs from the stop-recording request
        # (t0) to the audio being ready, so it already contains stt_s
        lat = {k: round(timing[k], 3) for k in ("record_s", "stt_s") if k in timing}
        lat.update(llm_s=round(llm_s, 3), tts_s=round(tts_s, 3), total_s=round(time.perf_counter() - timing["t0"], 3))
        turn["latency"] = lat
        st["turns"].append(turn)
        if kind == "done":
            st["status"] = "complete"
        return turn

    def _ask_examiner(self, ref: NodeRef, st: dict, items: list[dict]) -> dict | None:
        """The examiner's raw JSON, or None when the call failed (the caller then falls back to the key's own wording)."""
        fm = self.store.frontmatter(ref)
        system = (PROMPTS / "examiner.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain.name)
        payload = {  # anchors, subskills and tiers are deliberately absent (§7.6)
            "brief": self.store.brief(ref),
            "items": [{"id": it["id"], "question": it["question"], "probes": list(it.get("probes") or []), "interleaved": bool(it.get("interleaved"))} for it in items],
            "checker_result": public_checker(self.store.session(ref, "checker", None)),
            "transcript": [{"role": t["role"], "text": t["text"], **({"item_id": t["item_id"], "kind": t["kind"]} if t["role"] == "examiner" else {})} for t in st["turns"]],
            "title": fm.get("title"),
        }  # fmt: skip
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        try:
            res = self.router.complete(EXAMINER_TASK, msgs, schema=EXAMINER_SCHEMA, node_id=ref.id, track=ref.track)
        except LLMError:
            return None
        return res.json if isinstance(res.json, dict) else None

    @staticmethod
    def _enforce(st: dict, items: list[dict], raw: dict | None, force_done: bool = False) -> tuple[str, str, str, str]:
        """Apply the flow rules to the examiner's reply -> (say, item_id, kind, source). Mutates the cursor."""
        cur = st["cursor"]
        valid = (isinstance(raw, dict) and isinstance(raw.get("say"), str) and raw["say"].strip() != "" and raw.get("kind") in ("question", "probe", "done"))
        say = raw["say"].strip()[:600] if valid else None
        kind = raw["kind"] if valid else None
        item_id = raw.get("item_id") if valid else None
        i, last = cur["i"], len(items) - 1

        def done(text: str | None, source: str) -> tuple[str, str, str, str]:
            return text or CLOSING, items[-1]["id"], "done", source

        def advance(text_ok: bool, text: str | None) -> tuple[str, str, str, str]:
            if i >= last:
                return done(text if text_ok else None, "llm" if text_ok else ("fallback" if not valid else "coerced"))
            cur["i"], cur["probes"] = i + 1, 0
            nxt = items[i + 1]
            return (text if text_ok else nxt["question"]), nxt["id"], "question", ("llm" if text_ok else ("fallback" if not valid else "coerced"))

        if force_done:
            return done(None, "coerced")
        if i < 0:  # opening
            cur["i"], cur["probes"] = 0, 0
            ok = valid and kind == "question" and item_id == items[0]["id"]
            return (say if ok else items[0]["question"]), items[0]["id"], "question", ("llm" if ok else ("fallback" if not valid else "coerced"))
        if valid and kind == "probe" and cur["probes"] < MAX_PROBES:
            cur["probes"] += 1
            return say, items[i]["id"], "probe", "llm"
        if i >= last:
            return advance(valid and kind == "done", say)
        return advance(valid and kind == "question" and item_id == items[i + 1]["id"], say)

    # --- voice (push-to-talk) -------------------------------------------------------------------------------------
    def _need_voice(self, ref: NodeRef) -> dict:
        st = self._state(ref)
        if self.voice is None or st["mode"] != "voice" or st["status"] != "active":
            raise LifecycleError(409, "push-to-talk needs a running exam in voice mode")
        return st

    def record_start(self, ref: NodeRef) -> dict:
        self.store.require(ref, "oral")
        self._need_voice(ref)
        try:
            self.voice.recorder.start()
        except VoiceUnavailable as e:
            raise LifecycleError(503, str(e)) from None
        return {"recording": True}

    def record_cancel(self, ref: NodeRef) -> dict:
        if self.voice is not None:
            self.voice.recorder.cancel()
        return {"recording": False}

    def record_stop(self, ref: NodeRef) -> dict:
        """Stop recording -> transcribe -> examiner -> question audio, in one request. -> the turn, or {empty: True} when nothing was heard."""
        self.store.require(ref, "oral")
        st = self._need_voice(ref)
        t0 = time.perf_counter()
        try:
            audio = self.voice.recorder.stop()
        except VoiceUnavailable as e:
            raise LifecycleError(409, str(e)) from None
        try:
            tr = self.voice.transcriber.transcribe(audio, self.stt_prompt(ref))
        except VoiceUnavailable as e:
            raise LifecycleError(503, str(e)) from None
        if not tr.text:
            return {"empty": True, "heard_s": round(len(audio) / SAMPLE_RATE, 2), "view": self.view(ref)}
        out = self.answer(ref, tr.text, "voice", {"t0": t0, "record_s": len(audio) / SAMPLE_RATE, "stt_s": tr.seconds})
        return {**out, "empty": False, "transcript": tr.text}

    def stt_prompt(self, ref: NodeRef) -> str:
        """initial_prompt = node vocab + the domain's vocab.txt (§7.6)."""
        node_vocab = [str(v) for v in self.store.frontmatter(ref).get("vocab") or []]
        dom: list[str] = []
        p = getattr(self.domain, "vocab", None)
        if p and Path(p).is_file():
            dom = [ln.strip() for ln in Path(p).read_text(encoding="utf-8").splitlines() if ln.strip()]
        return build_prompt(node_vocab, dom)

    def audio(self, ref: NodeRef, n: int) -> bytes:
        wav = self.voice.audio(ref.track, ref.id, n) if self.voice is not None else None
        if wav is None:
            raise LifecycleError(404, "no audio for that turn (it is only kept for the last few questions)")
        return wav

    # --- end of exam -> oral.md, grading ------------------------------------------------------------------------------
    def require_complete(self, ref: NodeRef) -> None:
        if self._state(ref)["status"] != "complete":
            raise LifecycleError(409, "the oral exam is not finished yet")

    def write_md(self, ref: NodeRef) -> None:
        st = self._state(ref)
        title = self.store.frontmatter(ref).get("title")
        L = [f"# Oral exam: {title}", "", f"Mode: {st['mode']} · started {st.get('started', '?')}", ""]
        for t in st["turns"]:
            who = "Examiner" if t["role"] == "examiner" else "Learner"
            tag = f" ({t['item_id']}, {t['kind']})" if t["role"] == "examiner" else ""
            L += [f"**{who}{tag}:** {t['text']}", ""]
        (ref.path / "oral.md").write_text("\n".join(L), encoding="utf-8", newline="\n")

    def grade(self, ref: NodeRef) -> None:
        """Grade the finished exam (heavy, pinned, frozen prompt). Unavailable -> pending + queued (§3.1). Idempotent."""
        with node_lock(ref.track, ref.id):
            st = self._state(ref)
            g = st["grading"]
            if g["status"] in ("done", "pending") or st["status"] != "complete":
                return
            msgs = self._grade_messages(ref, st)
            try:
                res = self.router.complete(GRADE_TASK, msgs, schema=GRADE_SCHEMA, node_id=ref.id, track=ref.track)
                parsed, err = self._parse(ref, st, res.json)
                if parsed is not None and parsed["unverified"]:  # one repair attempt for quotes that are not verbatim
                    bad = ", ".join(f"{r['id']}: {r['quote'][:80]!r}" for r in parsed["unverified"])
                    fix = [*msgs, {"role": "assistant", "content": res.text},
                           {"role": "user", "content": f"These quotes are not verbatim from the learner's turns ({bad}). Return the same JSON with each quote copied exactly from a learner turn."}]  # fmt: skip
                    try:
                        res2 = self.router.complete(GRADE_TASK, fix, schema=GRADE_SCHEMA, node_id=ref.id, track=ref.track)
                        p2, e2 = self._parse(ref, st, res2.json)
                        if p2 is not None:
                            res, parsed, err = res2, p2, e2
                    except LLMError:
                        pass  # keep the first answer; the unverified quotes are blanked below
            except LLMError as e:  # GradingUnavailable: the router already queued the request
                g["status"], g["error"] = "pending", str(e)[:300]
                self._save(ref, st)
                refresh_provisional(self.store, ref)
                return
            if err or parsed is None:  # unusable grader output: queue it like an outage, the retry asks again
                g["status"], g["error"] = "pending", err or "unusable grader output"
                self.router.store.enqueue_grade(GRADE_TASK, _req_dict(msgs, ref.id, ref.track), ref.id, g["error"], track=ref.track)
            else:
                self._apply(ref, st, parsed, res.json, res.model)
            self._save(ref, st)
            refresh_provisional(self.store, ref)

    def _grade_messages(self, ref: NodeRef, st: dict) -> list[dict]:
        items = [{"id": it["id"], "subskill": it["subskill"], "question": it["question"], "anchors": {str(k): v for k, v in (it.get("anchors") or {}).items()},
                  "interleaved": bool(it.get("interleaved"))} for it in self._items(ref, st)]  # fmt: skip
        transcript = [{"role": t["role"], "text": t["text"], "item_id": t.get("item_id")} for t in st["turns"]]
        system = (PROMPTS / "oral_grader.md").read_text(encoding="utf-8").replace("{domain_pack}", self.domain.name)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps({"items": items, "transcript": transcript}, ensure_ascii=False)}]

    def _parse(self, ref: NodeRef, st: dict, out) -> tuple[dict | None, str | None]:
        """Validate the grader JSON -> ({results, unverified}, None) or (None, error). Nothing is written here."""
        if not isinstance(out, dict) or not isinstance(out.get("items"), list):
            return None, "grader output has no items list"
        by_id = {i.get("id"): i for i in out["items"] if isinstance(i, dict)}
        learner = _qnorm(" ".join(t["text"] for t in st["turns"] if t["role"] == "learner"))
        results, unverified = [], []
        for it in self._items(ref, st):
            got = by_id.get(it["id"])
            score = got.get("score") if got else None
            if got is None or isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 3:
                return None, f"grader gave no valid 0-3 score for item {it['id']}"
            quote = got.get("quote") if isinstance(got.get("quote"), str) else ""
            verified = quote.strip() == "" or _qnorm(quote) in learner
            r = {"id": it["id"], "subskill": it["subskill"], "interleaved": bool(it.get("interleaved")), "question": it["question"], "score": score,
                 "quote": quote.strip() if verified else "", "quote_verified": verified, "rationale": str(got.get("rationale", ""))}  # fmt: skip
            results.append(r)
            if not verified:
                unverified.append({**r, "quote": quote})
        return {"results": results, "unverified": unverified}, None

    def _apply(self, ref: NodeRef, st: dict, parsed: dict, out: dict, model: str) -> None:
        """Write the evidence: oral_item events, misconceptions, verdict, and (on a fail) the remediation child."""
        results = parsed["results"]
        ev = self.debrief.ev
        for r in results:
            ev.emit(ref, "oral_item", r["subskill"], score=r["score"], interleaved=r["interleaved"], grader_model=model)
        logged = ev.log_misconceptions(ref, out.get("misconceptions"), None)
        have = {m["subskill"] for m in out.get("misconceptions") or [] if isinstance(m, dict)}
        verdict = oral_verdict(results)
        for r in results:  # §7.6: an interleaved 0 logs a misconception; a failed oral logs the weak core answers so they are revisited
            weak_core = not verdict["passed"] and not r["interleaved"] and r["score"] < 2
            if (r["interleaved"] and r["score"] == 0 or weak_core) and r["subskill"] not in have:
                what = "Could not answer" if r["score"] == 0 else "Weak answer on"
                text = f"{what} an oral question on {r['subskill']}: {r['rationale']}".strip()
                if add_misconception(self.store.layout.misconceptions_path, track=ref.track, node_id=ref.id, subskill=r["subskill"], text=text) is not None:
                    logged += 1
                have.add(r["subskill"])
        g = st["grading"]
        g.update(status="done", results=results, model=model, passed=verdict["passed"], mean=verdict["mean"], misconceptions_logged=logged)
        g.pop("error", None)
        if not verdict["passed"]:
            self.spawn_remediation(ref, st)

    def apply_queued(self, row: dict, result) -> None:
        """Router.retry_pending_grades handler: a queued oral grading finally succeeded."""
        if row.get("task_type") != GRADE_TASK or row.get("node_id") is None:
            return
        try:
            ref = self.store.find(int(row["node_id"]))
        except LifecycleError:
            return
        with node_lock(ref.track, ref.id):
            st = self._state(ref)
            if st["grading"]["status"] != "pending":
                return  # already applied
            parsed, err = self._parse(ref, st, result.json)
            if err or parsed is None:
                self.router.store.enqueue_grade(GRADE_TASK, json.loads(row["request"]), ref.id, err or "unusable grader output", track=ref.track)
            else:
                self._apply(ref, st, parsed, result.json, result.model)
                self.debrief.write_md(ref)
                autocommit(self.store.root, f"node {ref.id}: oral graded")
            self._save(ref, st)
            refresh_provisional(self.store, ref)

    # --- remediation child (§7.6) -------------------------------------------------------------------------------------
    def spawn_remediation(self, ref: NodeRef, st: dict | None = None) -> dict:
        """Planner (kind=remediation) -> a build job for the child node. Idempotent; the result is kept in the oral state."""
        own = st is None
        with node_lock(ref.track, ref.id):
            st = st or self._state(ref)
            rem = st.get("remediation")
            if rem and rem.get("status") in ("building", "ready"):
                return rem
            if st["grading"]["status"] != "done" or st["grading"]["passed"]:
                raise LifecycleError(409, "remediation only follows a failed, graded oral exam")
            if self.jobs is None:
                st["remediation"] = {"status": "needed"}
            else:
                st["remediation"] = self._start_remediation(ref, st)
            if own:
                self._save(ref, st)
            return st["remediation"]

    def _start_remediation(self, ref: NodeRef, st: dict) -> dict:
        fm = self.store.frontmatter(ref)
        mine = [m for m in fold(read_records(self.store.layout.misconceptions_path, ref.track)) if m["node_id"] == ref.id]
        ctx = {
            "parent": {"id": ref.id, "title": fm.get("title"), "domain": fm.get("domain"), "subskills": [str(s) for s in fm.get("subskills") or []], "tier": fm.get("tier")},
            "failed_items": [{"subskill": r["subskill"], "score": r["score"], "why": r["rationale"]} for r in st["grading"]["results"] if not r["interleaved"] and r["score"] < 2],
            "misconceptions": [{"text": m["text"], "subskill": m["subskill"]} for m in mine],
        }  # fmt: skip
        try:
            option = Planner(self.store.root, ref.track, self.router, self.domain.name).remediation_option(ctx)
            builder = BuildService(self.store.root, ref.track, self.router, self.domain)
            parent_id = ref.id

            def done(rec: dict) -> None:  # the child is released: link it to its parent
                new_dir = self.store.layout.nodes_dir / rec["node_dir"]
                self.store.set_scalar(NodeRef(int(rec["node_dir"].split("-", 1)[0]), new_dir, ref.track), "parents", [parent_id])
                rebuild_db(self.store.root)
                with node_lock(ref.track, parent_id):
                    p = self.store.find(parent_id)
                    s2 = self._state(p)
                    s2["remediation"] = {**(s2.get("remediation") or {}), "status": "ready"}
                    self._save(p, s2)
                autocommit(self.store.root, f"node {parent_id}: remediation child {rec['node_dir']} released")

            job, plan = builder.start(self.jobs, option, on_success=done)
        except (PlannerError, BuildError, LLMError) as e:
            return {"status": "failed", "error": str(getattr(e, "message", e))[:300]}
        append_jsonl(self.store.layout.remediation_path, {"ts": _now(), "node_id": ref.id, "decision": "oral_fail", "child": plan.name})
        return {"status": "building", "job_id": job.id, "child_id": plan.node_id, "node_dir": plan.name, "title": option["title"], "option": option}

    def _refresh_remediation(self, ref: NodeRef, st: dict) -> None:
        """A build job that died leaves the state at "building": look the job up and surface the failure (the learner can retry)."""
        rem = st.get("remediation")
        if not rem or rem.get("status") != "building" or self.jobs is None:
            return
        store = getattr(self.jobs, "store", None)
        job = store.get(rem["job_id"]) if store is not None else None
        if job is not None and str(getattr(job, "status", "")) == "failed":
            rem.update(status="failed", error=str(getattr(job, "error", "") or "the build failed")[:300])
            self._save(ref, st)

    def retry_remediation(self, ref: NodeRef) -> dict:
        with node_lock(ref.track, ref.id):
            st = self._state(ref)
            if (st.get("remediation") or {}).get("status") not in ("failed", "needed"):
                raise LifecycleError(409, "there is no failed remediation to retry")
            st["remediation"] = None
            rem = self.spawn_remediation(ref, st)
            self._save(ref, st)
            return rem

    # --- for the debrief -------------------------------------------------------------------------------------------
    def debrief_view(self, ref: NodeRef) -> dict:
        st = self._state(ref)
        g = st["grading"]
        self._refresh_remediation(ref, st)
        if st["status"] == "none":
            return {"status": "not_taken", "note": "The oral exam was not taken."}
        status = "pending" if g["status"] == "pending" else "graded" if g["status"] == "done" else "not_graded"
        lat = [t["latency"] for t in st["turns"] if t["role"] == "examiner" and t.get("latency") and "stt_s" in t["latency"]]
        return {
            "status": status, "mode": st["mode"], "passed": g.get("passed"), "mean": g.get("mean"), "model": g.get("model"),
            "items": [{k: r[k] for k in ("id", "subskill", "interleaved", "question", "score", "quote", "rationale")} for r in g.get("results", [])],
            "pending_items": [{"id": i["id"]} for i in self._items(ref, st)] if status == "pending" else [],
            "remediation": st.get("remediation"),
            "latency": {"turns": len(lat), "median_total_s": sorted(x["total_s"] for x in lat)[len(lat) // 2] if lat else None},
            "note": "Oral exam graded." if status == "graded" else "Oral grades pending: they will appear when grading succeeds." if status == "pending" else "",
        }  # fmt: skip

    def outcome(self, ref: NodeRef) -> str | None:
        """'done-with-remediation' for a node whose oral failed (the status stays `done`; node.md's status enum is frozen)."""
        st = self.store.session(ref, "oral", {})
        return "done-with-remediation" if (st.get("remediation") or {}).get("status") in ("building", "ready", "needed", "failed") else None


def _req_dict(messages: list[dict], node_id: int, track: str) -> dict:
    from dataclasses import asdict

    return asdict(Request(GRADE_TASK, messages, GRADE_SCHEMA, None, None, node_id, None, light=False, track=track))
