"""Centralised AI content generation: one contract, one validation gate.

Every AI-generated surface (quiz questions, study guides, daily devotionals) goes
through `pipeline.run`, so the doctrinal, pastoral and grounding rules in
`contract.py` apply everywhere instead of being restated per router.
"""

from .contract import CONTRACT_VERSION, contract_block, system_prompt
from .pipeline import PipelineStats, run
from .validators import (
    Verdict,
    check_grounding,
    check_mcq,
    grounding_overlap,
    merge,
    normalise_answer_letter,
    screen_safety,
)

__all__ = [
    "CONTRACT_VERSION",
    "PipelineStats",
    "Verdict",
    "check_grounding",
    "check_mcq",
    "contract_block",
    "grounding_overlap",
    "merge",
    "normalise_answer_letter",
    "run",
    "screen_safety",
    "system_prompt",
]
