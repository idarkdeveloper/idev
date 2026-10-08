"""Claude-powered trading agent.

Watches a chosen investor's publicly disclosed trades (via QuiverQuant),
compares new activity against a paper-trading portfolio, and sends a
recommendation when something changes.
"""

__all__ = ["config", "quiver", "broker", "notify", "state", "agent"]
