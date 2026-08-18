"""The in-app assistant: a local model with tools over the library.

The assistant exists because the filter-pack layer made it safe to have one.
Packs are validated declarative data, so "add a way to sort my photos by French
landmark" is a document a model can write and a human can read — not code, and
not a database migration.

Its write tools stage changes rather than applying them. Approval happens in
the chat window, against a preview of the exact document that will be saved.
"""

from __future__ import annotations

from .agent import MAX_STEPS, SYSTEM_PROMPT, Assistant, AssistantEvent, suggestion_prompts
from .tools import PendingChange, ToolRegistry, ToolResult, ToolSpec

__all__ = [
    "MAX_STEPS",
    "SYSTEM_PROMPT",
    "Assistant",
    "AssistantEvent",
    "PendingChange",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "suggestion_prompts",
]
