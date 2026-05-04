"""SchedBot — AI-powered scheduling and appointment-management engine.

Public entry points:
    - schedbot.cli.main                 : Click CLI (the `nova` command)
    - schedbot.mcp_server.server.main   : MCP stdio server

Pluggable base classes (subclass these in a downstream persona repo):
    - schedbot.ai.classifier.RequestClassifier
    - schedbot.ai.drafter.MessageDrafter
    - schedbot.ai.scheduler.AvailabilityScheduler
"""

__version__ = "0.1.0"
