"""One recorded answer through one tool, on every Claude model.

Claude Opus 5.5, Sonnet 5.5 and Fable 5.1 reject a forced tool choice (``tool_choice`` "tool" or "any" is a 400), so
the call offers the single tool with ``tool_choice: auto`` (at most one call) and the system prompt says to answer
only through it. Thinking is always on for those models and counts against ``max_tokens``, so the cap is generous
(non-streaming, under the SDK's request timeout); the answer itself stays short.
"""

from __future__ import annotations

from typing import Any

MAX_TOKENS = 16000


def tool_rule(name: str) -> str:
    return f"Answer only by calling the {name} tool exactly once; do not answer in plain text."


def one_tool_call(tool: dict[str, Any], system: str) -> dict[str, Any]:
    """Keyword arguments for ``client.messages.create``: the tool, an unforced single call and the system prompt
    with the answer-through-the-tool rule appended."""
    return {"tools": [tool], "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            "system": f"{system}\n\n{tool_rule(tool['name'])}", "max_tokens": MAX_TOKENS}
