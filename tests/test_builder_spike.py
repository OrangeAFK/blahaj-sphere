from app.builder.gate import GateResult
from scripts.m25_builder_spike import SPECS, Attempt, build_prompt, run_with_retries, sum_usage


def _fake(results):
    seen, it = [], iter(results)

    def invoke(errors):
        seen.append(errors)
        return {"num_turns": 3}, 1.0

    return seen, invoke, lambda: next(it)


def test_stops_on_first_pass():
    seen, invoke, gate = _fake([GateResult(passed=False, errors=["gate 2: x"]), GateResult(passed=True)])
    ok, hist = run_with_retries(invoke, gate)
    assert ok and len(hist) == 2 and seen == [None, ["gate 2: x"]]


def test_at_most_two_retries_and_errors_fed_back():
    seen, invoke, gate = _fake([GateResult(errors=[f"e{i}"]) for i in range(5)])
    ok, hist = run_with_retries(invoke, gate)
    assert not ok and len(hist) == 3 and seen == [None, ["e0"], ["e1"]]


def test_prompt_has_builder_md_input_and_gate_errors(tmp_path):
    p = build_prompt(SPECS["b"], tmp_path, "2026-11-10T05:00:00-06:00", ["gate 1: boom"])
    assert p.startswith("# RFIC challenge Builder")
    assert '"dir": "0902-ihp-hbt-ce-gain"' in p and "matching.l_network" in p
    assert "Previous attempt FAILED the gate" in p and "- gate 1: boom" in p
    assert "FAILED" not in build_prompt(SPECS["a"], tmp_path, "t", None)


def test_usage_is_summed_across_attempts():
    a = [Attempt(1, "m", claude={"usage": {"input_tokens": 5, "output_tokens": 7}, "num_turns": 2, "total_cost_usd": 0.5, "duration_ms": 1500}),
         Attempt(2, "m", claude={"usage": {"cache_read_input_tokens": 100}, "num_turns": 3})]  # fmt: skip
    u = sum_usage(a)
    assert (u["input_tokens"], u["output_tokens"], u["cache_read_input_tokens"], u["num_turns"]) == (5, 7, 100, 5)
    assert u["notional_cost_usd"] == 0.5 and u["claude_duration_s"] == 1.5
