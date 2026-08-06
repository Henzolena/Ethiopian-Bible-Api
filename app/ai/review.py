"""Model-based review — the last gate before content reaches a user.

The deterministic validators in `validators.py` catch structural breakage and
blatant safety failures. They cannot judge whether a question is *answerable from
the passage*, whether a distractor is accidentally also correct, or whether a
devotional quietly asserts a contested doctrine. That needs a reader.

So each candidate gets a second model call scoring it against REVIEW_RUBRIC. It
roughly doubles generation cost, which is the correct trade: generation happens
in the background to warm a cache, while bad content reaches a person once and
cannot be recalled.

Reviewer failures never pass content through. If the reviewer is unreachable or
returns nonsense, the item is held rather than shipped — the whole point is that
nothing unreviewed reaches a user.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from app.config import settings

from .contract import REVIEW_RUBRIC, contract_block

Decision = Literal["accept", "revise", "reject"]


@dataclass
class Review:
    decision: Decision
    reasons: list[str]
    raw: str | None = None

    @property
    def accepted(self) -> bool:
        return self.decision == "accept"


_REVIEW_SYSTEM = f"""\
You are a careful reviewer for a Christian scripture app. You did not write the
content; your job is to decide whether it is fit to show a real person.

{contract_block()}

{'-' * 70}

{REVIEW_RUBRIC}

Respond with ONLY a JSON object:
{{"decision": "accept" | "revise" | "reject", "reasons": ["...", "..."]}}

Use "reject" when any of criteria 1-4 fails. Use "revise" when the content is
sound but weak on 5 or 6. Use "accept" only when you would be content for it to
be the first thing someone reads on waking. Keep reasons short and specific.
"""


def _parse(raw: str) -> Review:
    """Parse the reviewer's reply, treating anything unparseable as a hold."""
    text = raw.strip()
    # Models sometimes wrap JSON in a code fence despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return Review("reject", ["reviewer returned unparseable output"], raw=raw[:400])

    decision = str(data.get("decision", "")).strip().lower()
    if decision not in ("accept", "revise", "reject"):
        return Review("reject", [f"reviewer gave an unknown decision {decision!r}"], raw=raw[:400])

    reasons = data.get("reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    return Review(decision, [str(r) for r in reasons][:6], raw=None)


async def review_item(
    *,
    passage_ref: str,
    passage_text: str,
    candidate: dict[str, Any],
    kind: str,
    client: httpx.AsyncClient | None = None,
) -> Review:
    """Score one candidate item. Never raises — an error becomes a rejection."""
    if not settings.mistral_api_key:
        # Refusing to run without a reviewer is deliberate: silently skipping the
        # gate would reintroduce exactly the unvalidated path this replaces.
        return Review("reject", ["reviewer not configured (MISTRAL_API_KEY unset)"])

    user_msg = (
        f"CONTENT TYPE: {kind}\n"
        f"PASSAGE: {passage_ref}\n"
        f"PASSAGE TEXT:\n{passage_text}\n\n"
        f"CANDIDATE (JSON):\n{json.dumps(candidate, ensure_ascii=False, indent=2)}"
    )

    payload = {
        "model": settings.mistral_model,
        "messages": [
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        # Low temperature: we want a consistent judge, not a creative one.
        "temperature": 0.1,
        "max_tokens": 512,
    }

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=45)
    try:
        resp = await client.post(
            f"{settings.mistral_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.mistral_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code != 200:
            return Review("reject", [f"reviewer HTTP {resp.status_code}"])
        content = resp.json()["choices"][0]["message"]["content"]
        return _parse(content)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return Review("reject", [f"reviewer unavailable: {type(exc).__name__}"])
    finally:
        if owns_client:
            await client.aclose()
