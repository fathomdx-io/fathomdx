"""Harness — agentic tool-calling loop that subsumes the convener +
parliament + witness pipeline.

Phase 1: drop-in replacement for `witness.run_witness` with the same
signature. Tools are `search`, `expand`, `ascend`, `deliberate`. The
loop exits when the model emits a final card (no tool call). Hard cap
on turns is a safety, not a typical exit.

Phase 2 (separate work): multi-vector provenance facets in delta-store,
which makes `expand`/`ascend` semantically richer without changing the
harness API.
"""

from __future__ import annotations

from .loop import run_dialogue, run_harness, run_introspection

__all__ = ["run_dialogue", "run_harness", "run_introspection"]
