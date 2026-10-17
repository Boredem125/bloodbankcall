"""Decision layer: the LLM talks, the decision model decides.

Every engine speaks the System One payload shared by TypeSafe Jev and Cloudflare Clef:
    state + {question_id: {type: choice|score|noul, instructions, criteria}}
and returns {question_id: answer} where answer carries the typed value plus probabilities.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Protocol

log = logging.getLogger(__name__)

Questions = dict[str, dict[str, Any]]
Answers = dict[str, dict[str, Any]]


class DecisionError(RuntimeError):
    pass


class DecisionEngine(Protocol):
    name: str

    async def decide(self, state: Any, questions: Questions) -> Answers: ...


class FallbackEngine:
    def __init__(self, chain: list[DecisionEngine]) -> None:
        self.chain = chain

    @property
    def primary(self) -> str:
        return self.chain[0].name

    async def decide(self, state: Any, questions: Questions) -> tuple[Answers, str, float]:
        """Returns (answers, backend that answered, latency in ms)."""
        last: Exception | None = None
        for engine in self.chain:
            start = time.perf_counter()
            try:
                answers = await engine.decide(state, questions)
                missing = set(questions) - set(answers)
                if missing:
                    raise DecisionError(f"{engine.name} skipped questions {sorted(missing)}")
                return answers, engine.name, (time.perf_counter() - start) * 1000
            except Exception as exc:  # noqa: BLE001 - any backend failure falls through
                log.warning("decision backend %s failed: %s", engine.name, exc)
                last = exc
        raise DecisionError(f"all decision backends failed: {last}")


# Accessors that tolerate small shape differences between backends.
def noul(answers: Answers, key: str, default: float = 0.5) -> float:
    a = answers.get(key) or {}
    return float(a.get("noul", default))


def choice(answers: Answers, key: str) -> tuple[str | None, float]:
    a = answers.get(key) or {}
    conf = a.get("confidence")
    if conf is None:
        conf = max((a.get("probabilities") or {}).values(), default=0.0)
    return a.get("choice"), float(conf)


def score(answers: Answers, key: str, levels: int) -> float:
    """Score normalised to 0..1 (Jev returns a 0..levels-1 position)."""
    a = answers.get(key) or {}
    raw = float(a.get("score", (levels - 1) / 2))
    return raw / (levels - 1) if levels > 1 else raw
