"""Working-hours parsing and Sunday=0 weekday matching in availability."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
import yaml

from schedbot._time import get_tz, weekday_sunday0
from schedbot.config.loader import (
    BusinessConfig,
    SchedBotConfig,
    SchedulerConfig,
    ServiceConfig,
    WorkingWindow,
    load_config,
)
from schedbot.scheduler.availability import AvailabilityEngine

SAMPLE_WORKING_HOURS = [
    {"weekday": 0, "open_at": "09:00", "close_at": "21:00"},
    {"weekday": 1, "open_at": "17:00", "close_at": "21:00"},
    {"weekday": 2, "open_at": "17:00", "close_at": "21:00"},
    {"weekday": 3, "open_at": "17:00", "close_at": "21:00"},
    {"weekday": 4, "open_at": "17:00", "close_at": "21:00"},
    {"weekday": 5, "open_at": "17:00", "close_at": "21:00"},
    {"weekday": 6, "open_at": "09:00", "close_at": "21:00"},
]


def test_weekday_sunday0_convention() -> None:
    assert weekday_sunday0(date(2025, 5, 18)) == 0  # Sunday
    assert weekday_sunday0(date(2025, 5, 19)) == 1  # Monday


def test_load_config_parses_numeric_working_hours(tmp_path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "client_name": "Example Co",
                "operator_name": "Pat Operator",
                "operator_email": "pat@example.com",
                "agent_name": "Scheduling Assistant",
                "agent_email": "scheduler@example.com",
                "business": {
                    "name": "Example Co",
                    "timezone": "America/Chicago",
                    "working_hours": SAMPLE_WORKING_HOURS,
                    "services": [
                        {
                            "slug": "discovery-call",
                            "name": "Discovery Call",
                            "duration_minutes": 30,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.business.timezone == "America/Chicago"
    monday = next(w for w in cfg.business.working_hours if w.weekday == 1)
    saturday = next(w for w in cfg.business.working_hours if w.weekday == 6)
    assert monday.open_at == "17:00"
    assert saturday.open_at == "09:00"


@pytest.fixture
def sample_config() -> SchedBotConfig:
    return SchedBotConfig(
        agent_name="Scheduling Assistant",
        business=BusinessConfig(
            name="Example Co",
            timezone="America/Chicago",
            working_hours=[WorkingWindow(**w) for w in SAMPLE_WORKING_HOURS],
            services=[
                ServiceConfig(
                    slug="discovery-call",
                    name="Discovery Call",
                    duration_minutes=30,
                    buffer_after_minutes=0,
                )
            ],
        ),
        scheduler=SchedulerConfig(min_lead_minutes=0, booking_window_days=2),
    )


def test_availability_uses_sunday0_weekday(sample_config: SchedBotConfig) -> None:
    """Sunday morning and Monday evening slots match the numeric weekday list."""
    chicago = get_tz("America/Chicago")
    service = sample_config.business.services[0]
    engine = AvailabilityEngine(sample_config)

    sunday_from = datetime(2025, 5, 18, 10, 0, tzinfo=chicago).astimezone(timezone.utc)
    sunday_slots = engine.find_candidate_slots(
        service, from_dt=sunday_from, days=1, max_slots=3
    )
    assert sunday_slots, "expected Sunday slots within 09:00–21:00"

    monday_from = datetime(2025, 5, 19, 10, 0, tzinfo=chicago).astimezone(timezone.utc)
    monday_slots = engine.find_candidate_slots(
        service, from_dt=monday_from, days=1, max_slots=3
    )
    assert monday_slots, "expected Monday slots within 17:00–21:00"
    first_local = monday_slots[0].start_at.astimezone(chicago)
    assert first_local.hour >= 17
