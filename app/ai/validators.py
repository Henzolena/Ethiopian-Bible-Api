"""Deterministic checks that run before the model-based review.

Order matters and is deliberate: cheap, certain checks first, so an obviously
broken item never costs a second model call. Only items that pass everything here
are worth asking a critic about.

The structural checks exist because `quiz.py` previously did this:

    correct_answer=str(rq.get("correct_answer", "A")).strip().upper()[0]

which crashes with IndexError on an empty string, and silently produces an
unanswerable question when the model replies "Option B" (it keeps "O"). Neither
was caught, and the rows were written with is_verified=False that nothing ever
checked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    """Outcome of validating one generated item."""

    ok: bool
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> "Verdict":
        self.ok = False
        self.reasons.append(reason)
        return self

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "ok"


# ---------------------------------------------------------------------------
# Safety screening
#
# A blocklist is a blunt instrument and cannot be the only defence — the model
# review covers nuance. What this catches is the unambiguous cases, cheaply, and
# it never has a false negative that the critic would also miss silently.
# ---------------------------------------------------------------------------

_MEDICAL_DIRECTIVE = re.compile(
    r"\b(stop|quit|discontinue|throw away|do not (take|use))\b[^.]{0,40}\b"
    r"(medication|medicine|pills?|treatment|therapy|insulin|antidepressant)",
    re.I,
)

_PRAYER_INSTEAD_OF_CARE = re.compile(
    r"\b(pray|faith|belief)\b[^.]{0,60}\b(instead of|rather than|not need)\b"
    r"[^.]{0,40}\b(doctor|medicine|medication|treatment|therapy|hospital)",
    re.I,
)

_SHAMING = re.compile(
    r"\b(you (are|must be) (a )?(failure|worthless|unworthy|disgusting))\b"
    r"|\b(god (is )?punishing you)\b"
    r"|\b(your (illness|suffering|poverty|depression) is (because of|due to) your sin)\b",
    re.I,
)

_SELF_HARM_INSTRUCTIONAL = re.compile(
    r"\b(how to|way to|method of)\b[^.]{0,30}\b(kill yourself|end your life|harm yourself)\b",
    re.I,
)

_DISPARAGEMENT = re.compile(
    r"\b(catholics?|protestants?|orthodox|baptists?|pentecostals?|muslims?|jews?|hindus?)\b"
    r"[^.]{0,40}\b(are (wrong|false|damned|heretics?|deceived)|worship (falsely|idols))",
    re.I,
)

_SAFETY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_MEDICAL_DIRECTIVE, "contains a medical directive"),
    (_PRAYER_INSTEAD_OF_CARE, "suggests prayer in place of medical care"),
    (_SHAMING, "shames the reader or frames hardship as punishment"),
    (_SELF_HARM_INSTRUCTIONAL, "contains instructional self-harm content"),
    (_DISPARAGEMENT, "disparages a tradition or faith"),
)


def screen_safety(*texts: str | None) -> Verdict:
    """Screen free text for the unambiguous safety failures."""
    verdict = Verdict(ok=True)
    blob = " ".join(t for t in texts if t)
    for pattern, reason in _SAFETY_PATTERNS:
        if pattern.search(blob):
            verdict.fail(reason)
    return verdict


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Function words carry no grounding signal; counting them inflates overlap and
# would let an ungrounded item pass.
_STOPWORDS = frozenset("""
a an and are as at be been but by for from had has have he her him his i in is it
its me my no not of on or our she that the their them then there they this to us
was we were what when which who will with you your

according passage verse verses text chapter following mentioned describes describe
does did says said tell tells according-to what which whom whose why how many
question answer answers correct according reader
""".split())


def _content_words(text: str) -> set[str]:
    return {
        w.lower()
        for w in _WORD.findall(text or "")
        if len(w) > 2 and w.lower() not in _STOPWORDS
    }


def grounding_overlap(candidate: str, passage: str) -> float:
    """Fraction of the candidate's content words that appear in the passage.

    Generalises the 40%-overlap heuristic that already works in the iOS
    generator. It is a floor, not a proof: it reliably catches content about a
    different passage entirely, which is the common failure.
    """
    cand = _content_words(candidate)
    if not cand:
        return 0.0
    return len(cand & _content_words(passage)) / len(cand)


def check_grounding(candidate: str, passage: str, threshold: float = 0.25) -> Verdict:
    """Reject content that shares almost no vocabulary with the passage.

    The threshold is deliberately lower than the iOS verse-text check (0.40).
    There it compared two renderings of the same verse; here a question legitimately
    introduces its own words, so demanding high overlap would reject good items.
    """
    verdict = Verdict(ok=True)
    overlap = grounding_overlap(candidate, passage)
    if overlap < threshold:
        verdict.fail(f"weak grounding in passage (overlap {overlap:.0%} < {threshold:.0%})")
    return verdict


# ---------------------------------------------------------------------------
# Multiple-choice structure
# ---------------------------------------------------------------------------

_LETTERS = ("A", "B", "C", "D")


def normalise_answer_letter(raw: Any, options: list[str]) -> str | None:
    """Coerce a model's answer field into a valid letter, or None.

    Handles what models actually return: "A", "a", "A)", "Option B", "2",
    "answer: c", or the full text of the correct option. Returns None rather than
    guessing — a wrong guess yields a question the user can never answer
    correctly, which is worse than discarding the item.
    """
    if raw is None:
        return None

    text = str(raw).strip()
    if not text:
        return None

    # A bare or decorated letter.
    m = re.search(r"\b([ABCD])\b", text.upper())
    if m:
        return m.group(1)

    # A 1-based index.
    m = re.fullmatch(r"([1-4])", text)
    if m:
        return _LETTERS[int(m.group(1)) - 1]

    # The option's own text.
    for letter, option in zip(_LETTERS, options):
        if option and option.strip().lower() == text.lower():
            return letter

    return None


def check_mcq(
    question: str,
    options: list[str],
    answer_letter: str | None,
    explanation: str | None = None,
) -> Verdict:
    """Structural validation for a 4-option multiple-choice question."""
    verdict = Verdict(ok=True)

    if not (question or "").strip():
        verdict.fail("empty question")
    elif len(question.strip()) < 10:
        verdict.fail("question is too short to be meaningful")

    cleaned = [(o or "").strip() for o in options]
    if len(cleaned) != 4:
        verdict.fail(f"expected 4 options, got {len(cleaned)}")
    if any(not o for o in cleaned):
        verdict.fail("one or more options are empty")
    if len({o.lower() for o in cleaned if o}) != len([o for o in cleaned if o]):
        verdict.fail("options are not distinct")

    if answer_letter not in _LETTERS:
        verdict.fail(f"answer letter is not A–D (got {answer_letter!r})")
    elif len(cleaned) == 4:
        idx = _LETTERS.index(answer_letter)
        if not cleaned[idx]:
            verdict.fail("answer points at an empty option")

    # A leaked answer letter makes the question trivially guessable.
    if re.search(r"\b(answer|correct) (is|:)\s*[ABCD]\b", question, re.I):
        verdict.fail("question text leaks the answer")

    if explanation is not None and explanation.strip() and len(explanation.strip()) < 10:
        verdict.fail("explanation is present but too short to be useful")

    return verdict


def check_mcq_grounding(
    *,
    correct_option: str,
    explanation: str | None,
    passage: str,
    question: str = "",
    threshold: float = 0.30,
) -> Verdict:
    """Grounding check aimed at the parts of an MCQ that assert fact.

    Judging the *question* text is the wrong test: an interrogative legitimately
    introduces vocabulary the passage does not contain ("which material", "what
    happened next"), so a perfectly grounded question scores low and gets thrown
    away. Fabrication shows up in the answer and the explanation — a hallucinated
    name, number or event — so those are what get measured.

    The question is still passed in and included, weighted only by being part of
    the blob, because a question about an entirely different passage should fail.
    """
    verdict = Verdict(ok=True)
    asserted = " ".join(p for p in (correct_option, explanation) if p)
    if not asserted.strip():
        return verdict.fail("no answer or explanation text to check grounding against")

    overlap = grounding_overlap(asserted, passage)
    if overlap < threshold:
        # Fall back to including the question: some correct answers are a single
        # proper noun that legitimately appears once, which alone can dip below
        # the threshold.
        combined = grounding_overlap(f"{asserted} {question}", passage)
        if combined < threshold:
            verdict.fail(
                f"answer/explanation weakly grounded in passage "
                f"(overlap {max(overlap, combined):.0%} < {threshold:.0%})"
            )
    return verdict


def merge(*verdicts: Verdict) -> Verdict:
    """Combine verdicts, preserving every reason so logs explain the rejection."""
    out = Verdict(ok=True)
    for v in verdicts:
        if not v.ok:
            out.ok = False
            out.reasons.extend(v.reasons)
    return out


def all_ok(verdicts: Iterable[Verdict]) -> bool:
    return all(v.ok for v in verdicts)
