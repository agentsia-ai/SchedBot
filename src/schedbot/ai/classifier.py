"""SchedBot Request Classifier.

Decides whether an inbound message is a new booking request, a reschedule,
a cancellation, a generic question, or something we can't classify
confidently. The output drives the rest of the pipeline — a misclassified
"cancel" that gets confirmed as a booking is the worst possible failure,
so the floor on confidence is high by default.

This is the generic engine implementation. To customize for a productized
agent (e.g. a named persona with a tuned rubric), either:
  1. Subclass `RequestClassifier` and override `SYSTEM_PROMPT` (and
     optionally `_build_user_prompt`), or
  2. Point `config.ai.classifier_prompt_path` at an external prompt file.

See CLAUDE.md → Customization Patterns for details.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import anthropic

from schedbot._time import now_utc
from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import ClassificationResult, RequestKind

logger = logging.getLogger(__name__)


DEFAULT_CLASSIFIER_PROMPT = """You are a scheduling-triage expert. Classify a single inbound message
into one of the following request kinds:

  - new_booking : the customer wants to book an appointment for the first
                  time in this thread (or with no thread context at all).
                  Includes "are you available?" / "can I come in?" / "I'd
                  like to schedule a service".
  - reschedule  : the customer wants to MOVE an existing appointment to a
                  different time. Words like "push", "move", "change",
                  "delay", "bring forward" with reference to an existing
                  appointment all map here.
  - cancel      : the customer wants to CANCEL — drop the appointment with
                  no replacement. "Cancel" / "I won't be able to make it"
                  / "Please remove me" / "we don't need it after all".
  - question    : a non-action question (pricing, directions, prep
                  instructions, "do you offer X?"). Should be escalated to
                  a human, not auto-handled.
  - uncertain   : you cannot confidently choose any of the above.

Decision rules:
  - If the customer says "cancel" but in the next sentence proposes a new
    time, prefer reschedule.
  - "Just confirming my appointment" with no change requested is a
    question (the customer wants reassurance; the engine doesn't need to
    re-book).
  - A short message with no scheduling words at all — but in a thread
    where we recently offered times — should usually be classified
    based on whether the message is accepting, declining, or asking about
    those times. When in doubt, prefer uncertain.

Return ONLY valid JSON — no preamble, no explanation outside the JSON —
in this exact shape:
{
  "kind": "<one of the values above>",
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<1–2 sentences explaining your choice>"
}

Be honest about confidence. Anything below the operator's confidence
floor is collapsed to 'uncertain' and routed to human review."""


class RequestClassifier:
    """Classifies inbound booking-related messages using Claude.

    Subclass this and override `SYSTEM_PROMPT` to define a tuned classifier
    with a custom rubric. Per-deployment overrides can also be supplied by
    setting `config.ai.classifier_prompt_path` to a text file.
    """

    SYSTEM_PROMPT: str = DEFAULT_CLASSIFIER_PROMPT

    def __init__(self, config: SchedBotConfig, keys: APIKeys) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.model = config.ai.model
        self.min_confidence = config.ai.min_request_confidence
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Resolution order:
          1. `config.ai.classifier_prompt_path` (if set and file exists)
          2. Class attribute `SYSTEM_PROMPT` (subclass-overridable)
        """
        override = self.config.ai.classifier_prompt_path
        if override:
            path = Path(override)
            if path.exists():
                logger.info(f"{type(self).__name__} using prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"classifier_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__}.SYSTEM_PROMPT"
            )
        return self.SYSTEM_PROMPT

    def _build_user_prompt(
        self, message_text: str, sender: str = "", subject: str = ""
    ) -> str:
        return f"""Classify this inbound scheduling-related message.

Sender: {sender or "(unknown)"}
Subject: {subject or "(none)"}
Message body:
---
{message_text.strip()}
---

Return only JSON."""

    async def classify(
        self, message_text: str, sender: str = "", subject: str = ""
    ) -> ClassificationResult:
        """Classify a single message. Never raises — parse failures fall
        through to RequestKind.UNCERTAIN."""
        prompt = self._build_user_prompt(message_text, sender=sender, subject=subject)

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=400,
                system=self._system_prompt,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text
            data = _parse_json_loosely(raw)

            kind = _coerce_kind(data.get("kind"))
            confidence = float(data.get("confidence", 0.0) or 0.0)
            if confidence < self.min_confidence and kind != RequestKind.UNCERTAIN:
                logger.info(
                    f"Classifier confidence {confidence:.2f} below floor "
                    f"{self.min_confidence} → forcing UNCERTAIN"
                )
                kind = RequestKind.UNCERTAIN

            return ClassificationResult(
                kind=kind,
                confidence=confidence,
                reasoning=str(data.get("reasoning") or ""),
                classified_at=now_utc(),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Classifier failed to parse response: {e}")
            return ClassificationResult(
                kind=RequestKind.UNCERTAIN,
                confidence=0.0,
                reasoning=f"Parse error: {e}",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Classifier API call failed: {e}")
            return ClassificationResult(
                kind=RequestKind.UNCERTAIN,
                confidence=0.0,
                reasoning=f"API error: {e}",
            )

    async def classify_batch(
        self, items: list[tuple[str, str, str]]
    ) -> list[ClassificationResult]:
        """Classify a batch of (message, sender, subject) tuples in batches
        of 5 to manage rate limits."""
        out: list[ClassificationResult] = []
        for i in range(0, len(items), 5):
            batch = items[i : i + 5]
            results = await asyncio.gather(
                *[self.classify(m, sender=s, subject=subj) for m, s, subj in batch]
            )
            out.extend(results)
        return out


# ── parsing helpers ──────────────────────────────────────────────────────────


def _parse_json_loosely(raw: str) -> dict:
    """Accept either a bare JSON object or a fenced ```json``` block."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def _coerce_kind(raw: object) -> RequestKind:
    if raw is None:
        return RequestKind.UNCERTAIN
    text = str(raw).strip().lower()
    try:
        return RequestKind(text)
    except ValueError:
        logger.warning(f"Classifier returned unknown kind {text!r} → UNCERTAIN")
        return RequestKind.UNCERTAIN
