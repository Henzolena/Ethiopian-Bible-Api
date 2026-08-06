"""Tests for the AI content gate.

These pin down the specific failures that previously reached users: an empty
`correct_answer` crashing the endpoint, "Option B" becoming the letter "O" and
producing an unanswerable question, and content generated about a different
passage entirely.
"""

from app.ai.validators import (
    check_mcq,
    check_mcq_grounding,
    grounding_overlap,
    merge,
    normalise_answer_letter,
    screen_safety,
)

OPTIONS = ["Abraham", "Noah", "Moses", "Enoch"]
PASSAGE = (
    "Make yourself an ark of gopher wood. Make rooms in the ark, "
    "and cover it inside and out with pitch."
)


class TestAnswerLetterNormalisation:
    """The old code did str(raw).strip().upper()[0], which was wrong twice over."""

    def test_plain_letters(self):
        assert normalise_answer_letter("A", OPTIONS) == "A"
        assert normalise_answer_letter("b", OPTIONS) == "B"
        assert normalise_answer_letter("C)", OPTIONS) == "C"

    def test_prose_answer_is_not_truncated_to_first_character(self):
        # "Option B"[0] == "O" — not a valid answer, so the question became
        # impossible to answer correctly. It must resolve to B.
        assert normalise_answer_letter("Option B", OPTIONS) == "B"
        assert normalise_answer_letter("answer: c", OPTIONS) == "C"

    def test_one_based_index(self):
        assert normalise_answer_letter("2", OPTIONS) == "B"

    def test_full_option_text(self):
        assert normalise_answer_letter("Noah", OPTIONS) == "B"

    def test_empty_returns_none_instead_of_raising(self):
        # Previously an IndexError, surfacing as HTTP 500.
        assert normalise_answer_letter("", OPTIONS) is None
        assert normalise_answer_letter(None, OPTIONS) is None

    def test_unrecognisable_returns_none_rather_than_guessing(self):
        # Guessing would yield a question the user can never get right.
        assert normalise_answer_letter("Yes", OPTIONS) is None
        assert normalise_answer_letter("E", OPTIONS) is None


class TestMCQStructure:
    def test_valid_question_passes(self):
        v = check_mcq(
            "Who was told to build the ark?", OPTIONS, "B", "Genesis names Noah."
        )
        assert v.ok, v.summary

    def test_rejects_duplicate_options(self):
        v = check_mcq("Who built the ark?", ["Noah", "noah", "Moses", "Enoch"], "A")
        assert not v.ok
        assert "distinct" in v.summary

    def test_rejects_empty_option(self):
        v = check_mcq("Who built the ark?", ["Noah", "", "Moses", "Enoch"], "A")
        assert not v.ok

    def test_rejects_missing_answer_letter(self):
        v = check_mcq("Who built the ark?", OPTIONS, None)
        assert not v.ok

    def test_rejects_answer_pointing_at_empty_option(self):
        v = check_mcq("Who built the ark?", ["Noah", "", "Moses", "Enoch"], "B")
        assert not v.ok

    def test_rejects_leaked_answer(self):
        v = check_mcq("Who built it? The answer is B", OPTIONS, "B")
        assert not v.ok
        assert "leaks" in v.summary

    def test_rejects_trivially_short_question(self):
        assert not check_mcq("Who?", OPTIONS, "B").ok

    def test_rejects_wrong_option_count(self):
        assert not check_mcq("Who built the ark?", ["Noah", "Moses"], "A").ok


class TestGrounding:
    def test_legitimate_question_about_the_passage_is_kept(self):
        # Regression: judging grounding on the question's own wording rejected
        # this, because "according", "passage" and "material" are not in the text.
        v = check_mcq_grounding(
            correct_option="Gopher wood",
            explanation="The passage says to make the ark of gopher wood.",
            passage=PASSAGE,
            question="What material was the ark made from according to the passage?",
        )
        assert v.ok, v.summary

    def test_fabricated_answer_is_rejected(self):
        v = check_mcq_grounding(
            correct_option="Cedar from Lebanon",
            explanation="Solomon imported cedar for the temple.",
            passage=PASSAGE,
            question="What material was the ark made from?",
        )
        assert not v.ok

    def test_content_about_a_different_passage_is_rejected(self):
        v = check_mcq_grounding(
            correct_option="Four hundred horses",
            explanation="Pharaoh kept horses in his chariot stables.",
            passage=PASSAGE,
            question="How many horses did Pharaoh keep?",
        )
        assert not v.ok

    def test_missing_answer_text_is_rejected(self):
        v = check_mcq_grounding(
            correct_option="", explanation=None, passage=PASSAGE, question="Anything?"
        )
        assert not v.ok

    def test_overlap_is_a_fraction(self):
        assert grounding_overlap("gopher wood ark pitch", PASSAGE) == 1.0
        assert grounding_overlap("chariots horses Pharaoh", PASSAGE) == 0.0


class TestSafetyScreening:
    def test_ordinary_devotional_language_passes(self):
        assert screen_safety("God loves you and invites you to rest in him today.").ok

    def test_blocks_medical_directive(self):
        assert not screen_safety("Stop taking your medication and trust God.").ok

    def test_blocks_prayer_instead_of_care(self):
        assert not screen_safety("Pray instead of seeing a doctor about it.").ok

    def test_blocks_hardship_as_punishment(self):
        assert not screen_safety("Your illness is because of your sin.").ok

    def test_blocks_disparagement_of_a_tradition(self):
        assert not screen_safety("Catholics are wrong and worship idols.").ok

    def test_screens_across_multiple_fields(self):
        # Options and explanations are screened too, not just the question.
        assert not screen_safety("Which is true?", None, "God is punishing you").ok


class TestMerge:
    def test_collects_every_reason(self):
        a = check_mcq("Who?", OPTIONS, None)
        b = check_mcq_grounding(
            correct_option="Cedar", explanation="Unrelated.", passage=PASSAGE
        )
        combined = merge(a, b)
        assert not combined.ok
        # Reasons from both verdicts survive, so logs explain the rejection.
        assert len(combined.reasons) >= 2

    def test_all_passing_merges_to_ok(self):
        good = check_mcq(
            "Who was told to build the ark?", OPTIONS, "B", "Genesis names Noah."
        )
        assert merge(good, screen_safety("gentle text")).ok
