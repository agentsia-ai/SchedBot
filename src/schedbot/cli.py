"""SchedBot CLI — the `nova` command entry point.

Mirrors LeadGen and CustComm's CLI shape: a Click group with verb-commands
that each dispatch into an async service call. Rich is used for pretty
human-readable output.

Note: the CLI is for human operators. The MCP server (`nova mcp`) is the
machine-readable surface that Claude Desktop talks to.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from schedbot import __version__
from schedbot._time import format_local
from schedbot.config.loader import load_api_keys, load_config
from schedbot.crm.database import AppointmentDatabase
from schedbot.models import AppointmentStatus, ReminderStatus, WaitlistStatus
from schedbot.service import (
    cancel_appointment,
    confirm_appointment,
    daily_digest,
    draft_pending_reminders,
    find_available_slots,
    mark_no_show,
    reschedule_appointment,
)

console = Console()


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _boot() -> tuple:
    config = load_config()
    keys = load_api_keys()
    db = AppointmentDatabase(config.database.sqlite_path)
    await db.init()
    return config, keys, db


@click.group()
@click.version_option(__version__, prog_name="nova")
@click.option("--debug", is_flag=True, help="Enable verbose logging.")
def main(debug: bool) -> None:
    """Nova — SchedBot's CLI. Scheduling, confirmations, reminders, waitlist."""
    _configure_logging(debug)


# ── schedule ──────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--status",
    type=click.Choice([s.value for s in AppointmentStatus], case_sensitive=False),
    default=None,
    help="Filter by status.",
)
@click.option("--service", "service_slug", default=None, help="Filter by service slug.")
@click.option("--limit", default=25, type=int)
def schedule(status: Optional[str], service_slug: Optional[str], limit: int) -> None:
    """List appointments, optionally filtered by status or service."""

    async def _run() -> None:
        config, _, db = await _boot()
        appts = await db.list_appointments(
            status=AppointmentStatus(status) if status else None,
            service_slug=service_slug,
            limit=limit,
        )
        if not appts:
            console.print("[yellow]No appointments match.[/yellow]")
            return
        table = Table(title=f"Appointments ({status or 'all'})")
        table.add_column("ID", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Service", style="green")
        table.add_column("Client", style="white")
        table.add_column("When", style="white")
        for a in appts:
            when = (
                format_local(a.start_at, config.business.timezone)
                if a.start_at else "-"
            )
            table.add_row(
                a.id[:8],
                a.status.value,
                a.service_name or a.service_slug,
                a.client.email or a.client.name or "-",
                when,
            )
        console.print(table)

    asyncio.run(_run())


# ── digest ────────────────────────────────────────────────────────────────────


@main.command()
def digest() -> None:
    """Today and tomorrow's appointments, grouped — the morning briefing."""

    async def _run() -> None:
        config, _, db = await _boot()
        result = await daily_digest(config, db)
        for label, group in (("Today", result["today"]), ("Tomorrow", result["tomorrow"])):
            if not group:
                console.print(f"[dim]{label}: no appointments[/dim]")
                continue
            table = Table(title=f"{label} ({len(group)})")
            table.add_column("Time", style="cyan")
            table.add_column("Status", style="yellow")
            table.add_column("Service", style="green")
            table.add_column("Client", style="white")
            table.add_column("Notes", style="dim")
            for a in group:
                when = (
                    format_local(a.start_at, config.business.timezone, fmt="%H:%M")
                    if a.start_at else "-"
                )
                table.add_row(
                    when,
                    a.status.value,
                    a.service_name or a.service_slug,
                    a.client.email or a.client.name or "-",
                    (a.intake_notes or a.notes or "")[:60],
                )
            console.print(table)

    asyncio.run(_run())


# ── availability ──────────────────────────────────────────────────────────────


@main.command()
@click.argument("service_slug")
@click.option("--days", default=None, type=int, help="Lookahead window (days).")
@click.option("--limit", default=10, type=int, help="Max slots to show.")
def availability(service_slug: str, days: Optional[int], limit: int) -> None:
    """Show open slots for SERVICE_SLUG."""

    async def _run() -> None:
        config, _, db = await _boot()
        slots = await find_available_slots(
            config, db, service_slug=service_slug, days=days, max_slots=limit
        )
        if not slots:
            console.print("[yellow]No open slots in window.[/yellow]")
            return
        table = Table(title=f"Open slots — {service_slug}")
        table.add_column("Start", style="cyan")
        table.add_column("End", style="cyan")
        table.add_column("Duration", style="green")
        for s in slots:
            table.add_row(
                format_local(s.start_at, s.timezone),
                format_local(s.end_at, s.timezone),
                f"{s.duration_minutes} min",
            )
        console.print(table)

    asyncio.run(_run())


# ── show ──────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("appointment_id")
def show(appointment_id: str) -> None:
    """Show a single appointment + its reminder history."""

    async def _run() -> None:
        config, _, db = await _boot()
        appt = await _resolve_appt(db, appointment_id)
        if not appt:
            console.print(f"[red]No appointment matching {appointment_id!r}[/red]")
            return
        console.print(
            f"[bold cyan]{appt.service_name or appt.service_slug}[/bold cyan]  "
            f"[dim]{appt.id}[/dim]"
        )
        console.print(
            f"  status={appt.status.value}  "
            f"client={appt.client.name or '-'} <{appt.client.email or '-'}>"
        )
        if appt.start_at:
            console.print(
                f"  when={format_local(appt.start_at, config.business.timezone)} "
                f"({appt.duration_minutes} min)"
            )
        console.print(f"  location={appt.location.kind.value}")
        if appt.intake_notes:
            console.print(f"\n[dim]Intake notes:[/dim] {appt.intake_notes}")
        if appt.confirmation_body:
            console.print("\n[bold green]Confirmation draft:[/bold green]")
            console.print(f"  Subject: {appt.confirmation_subject}")
            console.print(appt.confirmation_body)

        reminders = await db.list_reminders_for_appointment(appt.id)
        if reminders:
            console.print(f"\n[bold]Reminders ({len(reminders)})[/bold]")
            for r in reminders:
                console.print(
                    f"  [{r.status.value}] {r.channel.value} "
                    f"-{r.offset_minutes_before}min  {r.subject[:60]}"
                )

    asyncio.run(_run())


# ── confirm ───────────────────────────────────────────────────────────────────


@main.command()
@click.argument("appointment_id")
@click.option("--channel", default="email", type=click.Choice(["email", "sms"]))
@click.option("--guidance", default="", help="Per-run guidance for the drafter.")
def confirm(appointment_id: str, channel: str, guidance: str) -> None:
    """Confirm an appointment: reserve the slot, draft confirmation, queue reminders."""

    async def _run() -> None:
        config, keys, db = await _boot()
        appt = await _resolve_appt(db, appointment_id)
        if not appt:
            console.print(f"[red]No appointment matching {appointment_id!r}[/red]")
            return
        result = await confirm_appointment(
            config, keys, db,
            appointment_id=appt.id,
            channel=channel,
            guidance=guidance,
        )
        if result is None:
            console.print("[red]Could not confirm (status mismatch or conflict).[/red]")
            return
        console.print(
            f"OK Confirmed {result.id[:8]} — status={result.status.value}"
        )
        if result.confirmation_subject:
            console.print(f"  draft subject: {result.confirmation_subject}")

    asyncio.run(_run())


# ── reschedule ────────────────────────────────────────────────────────────────


@main.command()
@click.argument("appointment_id")
@click.option("--message", default="", help="What the customer said in their request.")
@click.option("--channel", default="email", type=click.Choice(["email", "sms"]))
@click.option("--guidance", default="")
def reschedule(appointment_id: str, message: str, channel: str, guidance: str) -> None:
    """Generate alternative slots and a draft reply for a reschedule request."""

    async def _run() -> None:
        config, keys, db = await _boot()
        appt = await _resolve_appt(db, appointment_id)
        if not appt:
            console.print(f"[red]No appointment matching {appointment_id!r}[/red]")
            return
        result, slots = await reschedule_appointment(
            config, keys, db,
            appointment_id=appt.id,
            customer_request_text=message,
            channel=channel,
            guidance=guidance,
        )
        if result is None:
            console.print("[red]Could not draft reschedule.[/red]")
            return
        console.print(
            f"OK Drafted {len(slots)} alternative(s) for {appt.id[:8]}"
        )
        for s in slots:
            console.print(
                f"  - {format_local(s.start_at, s.timezone)} "
                f"({s.duration_minutes} min)"
            )

    asyncio.run(_run())


# ── cancel ────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("appointment_id")
@click.option("--reason", default="", help="Cancellation reason (free-form).")
@click.option("--channel", default="email", type=click.Choice(["email", "sms"]))
@click.option("--guidance", default="")
def cancel(appointment_id: str, reason: str, channel: str, guidance: str) -> None:
    """Cancel an appointment and draft the acknowledgement."""

    async def _run() -> None:
        config, keys, db = await _boot()
        appt = await _resolve_appt(db, appointment_id)
        if not appt:
            console.print(f"[red]No appointment matching {appointment_id!r}[/red]")
            return
        result = await cancel_appointment(
            config, keys, db,
            appointment_id=appt.id,
            reason=reason,
            channel=channel,
            guidance=guidance,
        )
        if result is None:
            console.print("[red]Cancel failed.[/red]")
            return
        console.print(f"OK Cancelled {result.id[:8]}")

    asyncio.run(_run())


# ── no-show ───────────────────────────────────────────────────────────────────


@main.command(name="no-show")
@click.argument("appointment_id")
@click.option("--channel", default="email", type=click.Choice(["email", "sms"]))
def no_show(appointment_id: str, channel: str) -> None:
    """Mark an appointment as no-show and draft the follow-up."""

    async def _run() -> None:
        config, keys, db = await _boot()
        appt = await _resolve_appt(db, appointment_id)
        if not appt:
            console.print(f"[red]No appointment matching {appointment_id!r}[/red]")
            return
        result = await mark_no_show(
            config, keys, db, appointment_id=appt.id, channel=channel
        )
        if result is None:
            console.print("[red]Mark failed.[/red]")
            return
        console.print(f"OK Marked no-show: {result.id[:8]}")

    asyncio.run(_run())


# ── remind ────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--limit", default=50, type=int)
@click.option("--guidance", default="")
def remind(limit: int, guidance: str) -> None:
    """Draft pending reminders that don't yet have a body."""

    async def _run() -> None:
        config, keys, db = await _boot()
        out = await draft_pending_reminders(
            config, keys, db, limit=limit, guidance=guidance
        )
        console.print(f"OK Drafted [green]{len(out)}[/green] reminder(s).")

    asyncio.run(_run())


# ── review ────────────────────────────────────────────────────────────────────


@main.command()
@click.option("--limit", default=20, type=int)
def review(limit: int) -> None:
    """List reminders + confirmations sitting in DRAFTED awaiting approval."""

    async def _run() -> None:
        config, _, db = await _boot()
        pending = await db.list_pending_reminders(limit=limit)
        if not pending:
            console.print("[yellow]No drafted reminders awaiting approval.[/yellow]")
        else:
            table = Table(title=f"Drafted reminders ({len(pending)})")
            table.add_column("Reminder", style="cyan")
            table.add_column("Appt", style="cyan")
            table.add_column("Channel", style="yellow")
            table.add_column("Offset", style="green")
            table.add_column("Subject", style="white")
            for appt_id, r in pending:
                table.add_row(
                    r.id[:8],
                    appt_id[:8],
                    r.channel.value,
                    f"-{r.offset_minutes_before}m",
                    (r.subject or "")[:60],
                )
            console.print(table)

        # Also show appointments whose confirmation_status is DRAFTED.
        appts = await db.list_appointments(limit=200)
        drafted_appts = [
            a for a in appts
            if a.confirmation_status == ReminderStatus.DRAFTED and a.confirmation_body
        ]
        if drafted_appts:
            ctable = Table(title=f"Drafted confirmations ({len(drafted_appts)})")
            ctable.add_column("Appt", style="cyan")
            ctable.add_column("Status", style="yellow")
            ctable.add_column("Client", style="white")
            ctable.add_column("Subject", style="white")
            for a in drafted_appts:
                ctable.add_row(
                    a.id[:8],
                    a.status.value,
                    a.client.email or "-",
                    a.confirmation_subject[:60],
                )
            console.print(ctable)

    asyncio.run(_run())


# ── waitlist ──────────────────────────────────────────────────────────────────


@main.command()
@click.option("--service", "service_slug", default=None)
@click.option(
    "--status",
    type=click.Choice([s.value for s in WaitlistStatus], case_sensitive=False),
    default=None,
)
@click.option("--limit", default=50, type=int)
def waitlist(service_slug: Optional[str], status: Optional[str], limit: int) -> None:
    """Show waitlist entries."""

    async def _run() -> None:
        _, _, db = await _boot()
        entries = await db.list_waitlist(
            status=WaitlistStatus(status) if status else None,
            service_slug=service_slug,
            limit=limit,
        )
        if not entries:
            console.print("[yellow]Waitlist is empty.[/yellow]")
            return
        table = Table(title=f"Waitlist ({len(entries)})")
        table.add_column("ID", style="cyan")
        table.add_column("Status", style="yellow")
        table.add_column("Service", style="green")
        table.add_column("Client", style="white")
        table.add_column("Added", style="dim")
        for e in entries:
            table.add_row(
                e.id[:8],
                e.status.value,
                e.service_slug,
                e.client.email or e.client.name or "-",
                e.created_at.isoformat(timespec="minutes"),
            )
        console.print(table)

    asyncio.run(_run())


# ── pipeline ──────────────────────────────────────────────────────────────────


@main.command()
def pipeline() -> None:
    """Show pipeline status (appointment counts by status)."""

    async def _run() -> None:
        _, _, db = await _boot()
        counts = await db.count_appointments_by_status()
        table = Table(title="SchedBot Pipeline")
        table.add_column("Status", style="cyan")
        table.add_column("Count", justify="right", style="magenta")
        total = 0
        for s in AppointmentStatus:
            c = counts.get(s.value, 0)
            if c:
                table.add_row(s.value, str(c))
                total += c
        table.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]")
        console.print(table)

    asyncio.run(_run())


# ── mcp ───────────────────────────────────────────────────────────────────────


@main.command()
def mcp() -> None:
    """Start the MCP stdio server (for Claude Desktop)."""
    from schedbot.mcp_server.server import main as mcp_main

    asyncio.run(mcp_main())


# ── internal helpers ──────────────────────────────────────────────────────────


async def _resolve_appt(db: AppointmentDatabase, prefix_or_id: str):
    """Accept either a full appointment UUID or a short prefix (first 8 chars)."""
    appt = await db.get_appointment(prefix_or_id)
    if appt:
        return appt
    candidates = await db.list_appointments(limit=500)
    matches = [a for a in candidates if a.id.startswith(prefix_or_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        console.print(
            f"[yellow]Ambiguous prefix {prefix_or_id!r} — "
            f"{len(matches)} appointments match.[/yellow]"
        )
    return None


# Suppress unused-import lint for typing-only / future use imports.
_ = (datetime, timedelta)


if __name__ == "__main__":  # pragma: no cover
    main()
    sys.exit(0)
