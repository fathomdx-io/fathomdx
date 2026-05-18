"""Harness — the agentic tool-calling loop that drives Fathom's thinking.

`run_threaded_fire` (in `threaded.py`) is the single entry point. It
reads a work-set (the live thread, or a scoped substrate when given a
`work_set` override), assembles a chat-completions request with the
shared tool surface (`tool_schemas.chat_tools()`), and loops until the
model emits `respond` or hits the turn cap. Tool handlers live in
`tools.py`, registered via `TOOL_HANDLERS`; the dispatcher in
`tool_schemas.py` bridges native tool calls to the handlers.

The `introspect` tool re-enters `run_threaded_fire` with a scoped
work-set and `disabled_tools` set, giving any caller a way to ask
Fathom a question and get a full Fathom answer back.
"""

from __future__ import annotations

from .threaded import run_threaded_fire

__all__ = ["run_threaded_fire"]
