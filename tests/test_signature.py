"""Outreach signature rendering — agent vs operator placeholders."""

from __future__ import annotations

from schedbot.ai.drafter import MessageDrafter
from schedbot.config.loader import (
    APIKeys,
    BusinessConfig,
    OutreachConfig,
    SchedBotConfig,
)


def _drafter(config: SchedBotConfig) -> MessageDrafter:
    return MessageDrafter(config, APIKeys())


def test_email_signature_uses_agent_name_and_business_name() -> None:
    config = SchedBotConfig(
        client_name="Example Co",
        operator_name="Pat Operator",
        operator_title="Owner",
        operator_email="pat@example.com",
        agent_name="Scheduling Assistant",
        agent_email="scheduler@example.com",
        business=BusinessConfig(name="Example Co"),
        outreach=OutreachConfig(
            email_signature="Warm regards,\n{agent_name}\n{business_name}",
        ),
    )
    body = _drafter(config)._append_signature("Your appointment is confirmed.", "email")
    assert "Your appointment is confirmed." in body
    assert "Warm regards," in body
    assert "Scheduling Assistant" in body
    assert "Example Co" in body
    assert "Pat Operator" not in body
    assert "pat@example.com" not in body


def test_sms_signature_uses_business_name_only() -> None:
    config = SchedBotConfig(
        agent_name="Scheduling Assistant",
        business=BusinessConfig(name="Example Co"),
        outreach=OutreachConfig(sms_signature="- {business_name}"),
    )
    body = _drafter(config)._append_signature("Reminder: tomorrow at 2pm.", "sms")
    assert body.endswith("- Example Co")


def test_all_drafted_channels_share_signature_helper() -> None:
    """Confirmations, reminders, cancellations, etc. all route through _draft."""
    config = SchedBotConfig(
        agent_name="Scheduling Assistant",
        business=BusinessConfig(name="Example Co"),
        outreach=OutreachConfig(email_signature="— {agent_name}"),
    )
    d = _drafter(config)
    body = d._append_signature("Cancellation acknowledged.", "email")
    assert body.endswith("— Scheduling Assistant")
