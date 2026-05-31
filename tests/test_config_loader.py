"""Config loader tests — identity fields and safety defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from schedbot.config.loader import (
    APIKeys,
    SchedBotConfig,
    display_agent_name,
    load_api_keys,
    load_config,
)


def test_load_config_reads_identity_fields(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "client_name": "Example Co",
                "operator_name": "Pat Operator",
                "operator_title": "Owner",
                "operator_email": "pat@example.com",
                "agent_name": "Scheduling Assistant",
                "agent_email": "scheduler@example.com",
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.client_name == "Example Co"
    assert cfg.operator_name == "Pat Operator"
    assert cfg.operator_title == "Owner"
    assert cfg.operator_email == "pat@example.com"
    assert cfg.agent_name == "Scheduling Assistant"
    assert cfg.agent_email == "scheduler@example.com"
    assert cfg.outreach.require_approval is True
    assert cfg.outreach.auto_send is False


def test_display_agent_name_falls_back_to_engine_name() -> None:
    assert display_agent_name(SchedBotConfig()) == "schedbot"
    assert display_agent_name(SchedBotConfig(agent_name="Scheduling Assistant")) == (
        "Scheduling Assistant"
    )


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.yaml")


def test_api_keys_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("CALCOM_API_KEY", raising=False)

    keys = load_api_keys()
    assert keys.anthropic == "sk-ant-test"
    assert keys.calcom_api_key == ""
