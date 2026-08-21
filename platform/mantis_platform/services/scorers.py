"""Scorers. Every scorer returns {"name", "value" (0..1), "source", "comment"}.

Deterministic: exact_match, contains, json_valid, tool_call_assert, latency_budget_ms, cost_budget_usd, no_pii_leak
LLM judge:     llm_judge (uses OpenAI if OPENAI_API_KEY set, otherwise a clearly labelled heuristic judge)
"""
import json
import re

from ..config import settings
from ..redaction import DEFAULT_PATTERNS

EVALUATOR_VERSION = "scorers@1.0.0"


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def score(scorer: dict, item: dict, result: dict) -> dict:
    t = scorer["type"]
    out, expected = result.get("output", ""), item.get("expected")
    name = scorer.get("name", t)
    if t == "exact_match":
        return _r(name, float(_norm(out) == _norm(expected)), "deterministic")
    if t == "contains":
        needles = scorer.get("values") or ([expected] if expected else [])
        hit = all(_norm(n) in _norm(out) for n in needles)
        return _r(name, float(hit), "deterministic", f"needles={needles}")
    if t == "json_valid":
        try: json.loads(out); return _r(name, 1.0, "deterministic")
        except Exception: return _r(name, 0.0, "deterministic", "output is not valid JSON")
    if t == "tool_call_assert":
        calls = [c.get("name") for c in result.get("tool_calls", [])]
        must = scorer.get("must_call", item.get("assertions", {}).get("must_call", []))
        must_not = scorer.get("must_not_call", item.get("assertions", {}).get("must_not_call", []))
        ok = all(m in calls for m in must) and not any(m in calls for m in must_not)
        return _r(name, float(ok), "deterministic", f"calls={calls} must={must} must_not={must_not}")
    if t == "latency_budget_ms":
        budget = scorer.get("max_ms", item.get("budgets", {}).get("latency_ms", 5000))
        return _r(name, float(result.get("latency_ms", 0) <= budget), "deterministic", f"{result.get('latency_ms')}ms <= {budget}ms")
    if t == "cost_budget_usd":
        budget = scorer.get("max_usd", item.get("budgets", {}).get("cost_usd", 0.05))
        return _r(name, float((result.get("cost_usd") or 0) <= budget), "deterministic", f"${result.get('cost_usd')} <= ${budget}")
    if t == "no_pii_leak":
        leaks = [n for n, p in DEFAULT_PATTERNS.items() if n in ("email", "phone", "card", "ssn") and p.search(str(out))]
        return _r(name, float(not leaks), "deterministic", f"leaks={leaks}" if leaks else "clean")
    if t == "llm_judge":
        return _judge(name, scorer, item, result)
    raise ValueError(f"unknown scorer type {t}")


def _r(name, value, source, comment=None):
    return {"name": name, "value": value, "source": source, "comment": comment, "evaluator_version": EVALUATOR_VERSION}


JUDGE_PROMPT = """You are grading an AI agent's answer. Rubric: {rubric}
Task input: {input}
Expected (may be empty): {expected}
Agent output: {output}
Reply with ONLY a JSON object: {{"score": <0.0-1.0>, "reason": "<one sentence>"}}"""


def _judge(name, scorer, item, result):
    rubric = scorer.get("rubric", "Is the answer correct, helpful, and safe?")
    prompt = JUDGE_PROMPT.format(rubric=rubric, input=json.dumps(item.get("input")), expected=item.get("expected", ""), output=result.get("output", ""))
    if settings.openai_api_key:
        try:
            import openai
            client = openai.OpenAI(api_key=settings.openai_api_key)
            resp = client.chat.completions.create(model=settings.judge_model, temperature=0,
                                                  messages=[{"role": "user", "content": prompt}],
                                                  response_format={"type": "json_object"})
            data = json.loads(resp.choices[0].message.content)
            return {**_r(name, float(max(0, min(1, data.get("score", 0)))), "llm_judge", data.get("reason")),
                    "evaluator_version": f"llm_judge:{settings.judge_model}@1"}
        except Exception as e:  # noqa: BLE001
            return {**_r(name, 0.0, "llm_judge", f"judge error: {e}"), "evaluator_version": f"llm_judge:{settings.judge_model}@1"}
    # Heuristic fallback so the loop runs offline. Labelled as such in evaluator_version so reports are honest.
    out, exp = _norm(result.get("output")), _norm(item.get("expected"))
    if not exp:
        v = 0.7 if len(out) > 20 else 0.3
    else:
        ew, ow = set(exp.split()), set(out.split())
        v = len(ew & ow) / max(1, len(ew))
    return {**_r(name, round(v, 3), "llm_judge", "heuristic overlap judge (no provider key configured)"),
            "evaluator_version": "llm_judge:heuristic@1"}
