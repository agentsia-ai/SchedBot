"""SchedBot Availability Scheduler.

Two responsibilities:
  1. Reasoning over a set of candidate slots to pick the best 2–3 to offer
     a customer (deterministic generation lives in `scheduler/availability.py`;
     this module just *ranks and explains*).
  2. Suggesting alternatives when the customer's requested time isn't
     workable (slot unavailable, outside business hours, conflicts with
     buffer requirements).

Like the other AI seams, this is pluggable. A productized persona may
override `SYSTEM_PROMPT` to inject scheduling-bias preferences (e.g. "Nova
prefers morning slots when the customer didn't express a preference") and
tune the picker without rewriting the engine.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import anthropic

from schedbot._time import format_local
from schedbot.config.loader import APIKeys, SchedBotConfig
from schedbot.models import TimeSlot

logger = logging.getLogger(__name__)


DEFAULT_SCHEDULER_PROMPT = """You help a small business pick the best times to offer a customer for a
booking, given a list of candidate slots and what the customer said.

Hard rules — never violate any of these:
  - You may ONLY pick from the candidate slots provided in the user
    message. Inventing a time that isn't on the list is the worst possible
    failure — the engine cannot guarantee anything you invent is actually
    free.
  - Pick AT MOST 3 slots. Two is usually better than three; one is fine
    when the customer was specific.
  - Prefer slots that match what the customer asked for. If they said
    "afternoon", drop morning slots even if they're earlier on the list.
    If they said "this week", drop next-week slots.
  - When the customer didn't express a preference, prefer slots that are
    spread out (different days / different parts of the day) so the
    customer has a real choice.
  - Order your picks earliest-first.

Return ONLY valid JSON — no preamble, no commentary outside the JSON —
in this exact shape:
{
  "chosen_slot_indexes": [<integer indexes into the provided slots, 0-based>],
  "reasoning": "<1 short sentence explaining the picks>"
}"""


class AvailabilityScheduler:
    """Reasons about candidate slots and picks the best ones to offer.

    Subclass this and override `SYSTEM_PROMPT` to define a tuned
    availability picker (e.g. preference for morning slots, batching in
    blocks). Per-deployment overrides can also be supplied via
    `config.ai.scheduler_prompt_path`.
    """

    SYSTEM_PROMPT: str = DEFAULT_SCHEDULER_PROMPT

    def __init__(self, config: SchedBotConfig, keys: APIKeys) -> None:
        self.config = config
        self.client = anthropic.AsyncAnthropic(api_key=keys.anthropic)
        self.model = config.ai.model
        self.max_proposed = config.scheduler.max_proposed_slots
        self._system_prompt = self._load_system_prompt()

    def _load_system_prompt(self) -> str:
        """Resolution order:
          1. `config.ai.scheduler_prompt_path` (if set and file exists)
          2. Class attribute `SYSTEM_PROMPT` (subclass-overridable)
        """
        override = self.config.ai.scheduler_prompt_path
        if override:
            path = Path(override)
            if path.exists():
                logger.info(f"{type(self).__name__} using prompt override: {path}")
                return path.read_text(encoding="utf-8")
            logger.warning(
                f"scheduler_prompt_path points at missing file: {path} — "
                f"falling back to {type(self).__name__}.SYSTEM_PROMPT"
            )
        return self.SYSTEM_PROMPT

    async def pick_slots(
        self,
        candidate_slots: list[TimeSlot],
        customer_request_text: str = "",
        service_name: str = "",
        requested_at: Optional[datetime] = None,
    ) -> tuple[list[TimeSlot], str]:
        """Pick up to `max_proposed_slots` from `candidate_slots`.

        Returns (chosen_slots, reasoning). Falls back to "first N earliest
        candidates" on any AI failure — the engine never needs to block on
        the model being available.
        """
        if not candidate_slots:
            return [], "No candidate slots available."

        # Trim to a reasonable working set so the model isn't reasoning
        # over hundreds of options. Earliest 20 is plenty for a 60-day window.
        working = candidate_slots[:20]

        slots_rendered = "\n".join(
            f"  [{i}] {format_local(s.start_at, s.timezone)} – "
            f"{format_local(s.end_at, s.timezone)}"
            for i, s in enumerate(working)
        )

        request_block = ""
        if requested_at:
            tz = working[0].timezone if working else "UTC"
            request_block = (
                f"Customer originally asked for: {format_local(requested_at, tz)}\n"
            )
        if customer_request_text:
            request_block += (
                f"Customer's note:\n---\n{customer_request_text.strip()}\n---\n"
            )

        user_prompt = f"""Pick the best slots to offer the customer.

Service: {service_name or "(unspecified)"}
Maximum slots to pick: {self.max_proposed}
{request_block}
Candidate slots (use only these):
{slots_rendered}

Return JSON."""

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=300,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            data = _parse_json_loosely(response.content[0].text)
            raw_idxs = data.get("chosen_slot_indexes") or []
            chosen: list[TimeSlot] = []
            for i in raw_idxs:
                try:
                    chosen.append(working[int(i)])
                except (ValueError, IndexError):
                    logger.warning(f"Scheduler returned invalid slot index {i}")
            chosen = chosen[: self.max_proposed]
            reasoning = str(data.get("reasoning") or "")
            if not chosen:
                # Defensive fallback — never return empty when we had candidates.
                chosen = working[: self.max_proposed]
                reasoning = "Fell back to earliest candidates (model returned no picks)."
            return chosen, reasoning
        except Exception as e:  # noqa: BLE001
            logger.error(f"AvailabilityScheduler call failed: {e}")
            return working[: self.max_proposed], (
                f"Fell back to earliest candidates ({type(e).__name__})."
            )


def _parse_json_loosely(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())
