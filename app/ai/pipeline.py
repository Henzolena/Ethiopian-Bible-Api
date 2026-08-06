"""Generate → validate → review → retry, for one item type.

This is the single path every AI surface goes through, so "is this fit to show
someone" is answered in one place rather than four.

Reject-and-retry rather than flag-and-hold: a rejected item is dropped, the reason
is logged, and generation is retried up to a bounded number of rounds. Users never
see rejected content and never wait on a review queue. The bound matters — an
unbounded retry against a model that keeps failing the same way burns quota and
never converges.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .review import Review, review_item
from .validators import Verdict

log = logging.getLogger("app.ai.pipeline")

# A generator produces raw candidate dicts; how it does so is the caller's business.
Generator = Callable[[int], Awaitable[list[dict[str, Any]]]]
# A validator returns a Verdict for one candidate.
Validator = Callable[[dict[str, Any]], Verdict]


@dataclass
class PipelineStats:
    """What happened, so rejections are visible instead of silent."""

    requested: int = 0
    generated: int = 0
    failed_validation: int = 0
    rejected_by_review: int = 0
    revise_accepted: int = 0
    accepted: int = 0
    rounds: int = 0
    reasons: list[str] = field(default_factory=list)

    def note(self, reason: str) -> None:
        # Cap so a pathological run cannot balloon the log line.
        if len(self.reasons) < 25:
            self.reasons.append(reason)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "generated": self.generated,
            "failed_validation": self.failed_validation,
            "rejected_by_review": self.rejected_by_review,
            "revise_accepted": self.revise_accepted,
            "accepted": self.accepted,
            "rounds": self.rounds,
            "reasons": self.reasons,
        }


async def run(
    *,
    kind: str,
    passage_ref: str,
    passage_text: str,
    want: int,
    generate: Generator,
    validate: Validator,
    max_rounds: int = 3,
    accept_revise: bool = True,
) -> tuple[list[dict[str, Any]], PipelineStats]:
    """Produce up to `want` items that pass validation and review.

    `accept_revise` keeps items the reviewer marked "revise" — sound content that
    is merely weak on formation or clarity. Rejections (criteria 1-4: grounding,
    answerability, doctrine, safety) are never kept.

    Returns whatever survived, which may be fewer than `want`. Callers must handle
    a short result: returning three good questions beats padding with a bad one.
    """
    stats = PipelineStats(requested=want)
    kept: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=45) as client:
        for round_no in range(1, max_rounds + 1):
            if len(kept) >= want:
                break
            stats.rounds = round_no

            shortfall = want - len(kept)
            # Over-ask slightly: some will fail, and a second round costs a full
            # round-trip. Capped so a large request does not balloon the call.
            ask = min(shortfall + 2, shortfall * 2 + 1)

            try:
                candidates = await generate(ask)
            except Exception as exc:  # noqa: BLE001 - generation is caller code
                log.warning("[%s] generation failed on round %d: %s", kind, round_no, exc)
                stats.note(f"generation error: {type(exc).__name__}")
                break

            if not candidates:
                stats.note("generator returned nothing")
                break
            stats.generated += len(candidates)

            for candidate in candidates:
                if len(kept) >= want:
                    break

                verdict = validate(candidate)
                if not verdict.ok:
                    stats.failed_validation += 1
                    stats.note(f"validation: {verdict.summary}")
                    continue

                review: Review = await review_item(
                    passage_ref=passage_ref,
                    passage_text=passage_text,
                    candidate=candidate,
                    kind=kind,
                    client=client,
                )

                if review.decision == "reject":
                    stats.rejected_by_review += 1
                    stats.note(f"review reject: {'; '.join(review.reasons) or 'no reason given'}")
                    continue

                if review.decision == "revise":
                    if not accept_revise:
                        stats.rejected_by_review += 1
                        stats.note(f"review revise (dropped): {'; '.join(review.reasons)}")
                        continue
                    stats.revise_accepted += 1

                candidate["_review"] = {
                    "decision": review.decision,
                    "reasons": review.reasons,
                }
                kept.append(candidate)
                stats.accepted += 1

    if stats.accepted < want:
        log.info(
            "[%s] %s: kept %d/%d after %d round(s) — %s",
            kind, passage_ref, stats.accepted, want, stats.rounds,
            "; ".join(stats.reasons[:5]) or "no reasons recorded",
        )
    return kept, stats
