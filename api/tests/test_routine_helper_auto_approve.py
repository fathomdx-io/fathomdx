"""Tests for routine-driven helper-dispatch auto-approve.

When `tool_dispatch_helper` writes a proposal mid-routine and the
routine's spec has `helper_auto_approve: true`, the proposal must be
auto-approved silently (no model-facing change to the dispatch tool's
return string). When the flag is false (or the routine doesn't exist,
or the dispatch isn't from a routine), the proposal stays pending.

The model must NEVER see the auto-approve outcome — same return string
either way. This pins both halves: the auto-approve fires when it
should, and stays silent when it shouldn't, AND the tool's return
string is invariant.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.loop.harness import tools as harness_tools


def _spec(routine_id: str, *, auto: bool, deleted: bool = False) -> dict:
    """Build a fake routine spec dict as `get_latest_spec` would return it."""
    return {
        "meta": {
            "id": routine_id,
            "name": routine_id,
            "schedule": "0 14 * * 5",
            "enabled": True,
            "helper_auto_approve": auto,
            "deleted": deleted,
        },
        "body": "# Steps\n1. Do the thing.\n",
        "workspace": "",
        "delta": {},
    }


async def _run_dispatch(routine_id: str | None) -> tuple[str, list]:
    """Drive tool_dispatch_helper with mocked I/O. Returns (return_value,
    list of (function_name, args) tuples for the approve-related calls)."""
    calls: list = []

    if routine_id:
        token = harness_tools.FIRE_ROUTINE_ID.set(routine_id)
    else:
        token = harness_tools.FIRE_ROUTINE_ID.set("")

    try:
        mock_available = AsyncMock(
            return_value=[
                {
                    "host": "myras-fedora-laptop",
                    "role": "claude-code",
                    "description": "",
                }
            ]
        )
        mock_lake_write = AsyncMock(return_value={"id": "prop_id_1234"})
        mock_lake_query = AsyncMock(return_value=[])
        mock_puddle_write = AsyncMock()

        async def fake_get_spec(rid: str):
            calls.append(("get_latest_spec", rid))
            return _get_spec_for(rid)

        async def fake_approve(**kwargs):
            calls.append(("approve_helper_dispatch", kwargs))
            return {
                "dispatched": True,
                "host": kwargs["host"],
                "role": kwargs["role"],
                "task_corr": "abcd1234",
                "task_chars": len(kwargs["task"]),
                "dispatch_delta_id": "dispatch_id_5678",
            }

        async def fake_decision(**kwargs):
            calls.append(("write_decision", kwargs))
            return {"id": "decision_id_9999"}

        with (
            patch(
                "api.loop.witness._available_helpers",
                mock_available,
            ),
            patch("api.delta_client.write", mock_lake_write),
            patch("api.delta_client.query", mock_lake_query),
            patch.object(harness_tools.puddle, "write", mock_puddle_write),
            patch("api.routines.get_latest_spec", fake_get_spec),
            patch(
                "api.routes.proposals.approve_helper_dispatch",
                fake_approve,
            ),
            patch("api.routes.proposals._write_decision", fake_decision),
        ):
            ret = await harness_tools.tool_dispatch_helper(
                host="myras-fedora-laptop",
                role="claude-code",
                task="say hi",
                title="Test dispatch",
            )
    finally:
        harness_tools.FIRE_ROUTINE_ID.reset(token)

    return ret, calls


# Per-test spec setter — each test plugs the routine-id → spec map it wants.
_spec_map: dict = {}


def _get_spec_for(rid: str):
    return _spec_map.get(rid)


@pytest.mark.asyncio
async def test_auto_approve_fires_when_routine_flag_true():
    """A dispatch mid-routine with helper_auto_approve=true must
    silently approve and write a proposal-decision under the
    auto-policy lineage."""
    _spec_map.clear()
    _spec_map["auto-routine"] = _spec("auto-routine", auto=True)

    _ret, calls = await _run_dispatch(routine_id="auto-routine")

    fn_names = [c[0] for c in calls]
    assert "approve_helper_dispatch" in fn_names
    assert "write_decision" in fn_names
    # The decision is tagged with the auto-policy lineage so the audit
    # trail distinguishes it from operator approvals.
    decision_call = next(c for c in calls if c[0] == "write_decision")
    assert decision_call[1]["decided_by"] == "auto-policy:routine:auto-routine"


@pytest.mark.asyncio
async def test_auto_approve_skipped_when_routine_flag_false():
    """A dispatch mid-routine where the flag is false stays pending
    for operator review. No approve call, no decision write."""
    _spec_map.clear()
    _spec_map["manual-routine"] = _spec("manual-routine", auto=False)

    _ret, calls = await _run_dispatch(routine_id="manual-routine")

    fn_names = [c[0] for c in calls]
    assert "approve_helper_dispatch" not in fn_names
    assert "write_decision" not in fn_names


@pytest.mark.asyncio
async def test_auto_approve_skipped_when_no_routine_context():
    """Chat-driven dispatches (no routine in this fire) must never
    auto-approve, regardless of any routine specs in the lake."""
    _spec_map.clear()
    _spec_map["auto-routine"] = _spec("auto-routine", auto=True)

    _ret, calls = await _run_dispatch(routine_id=None)

    fn_names = [c[0] for c in calls]
    # Routine spec was never even fetched — no routine context, no path
    # to auto-approve.
    assert "get_latest_spec" not in fn_names
    assert "approve_helper_dispatch" not in fn_names


@pytest.mark.asyncio
async def test_auto_approve_skipped_when_routine_deleted():
    """A deleted (tombstoned) routine spec must not trigger auto-approve
    even if its flag is true — soft-deletion is the operator's signal
    that the routine should no longer act."""
    _spec_map.clear()
    _spec_map["dead-routine"] = _spec("dead-routine", auto=True, deleted=True)

    _ret, calls = await _run_dispatch(routine_id="dead-routine")

    fn_names = [c[0] for c in calls]
    assert "approve_helper_dispatch" not in fn_names


@pytest.mark.asyncio
async def test_return_string_invariant_regardless_of_auto_approve():
    """The model must NEVER see whether its dispatch was auto-approved
    or not. tool_dispatch_helper's return string says "Pending operator
    approval" in both branches so the model's behavior doesn't depend
    on a hidden policy."""
    _spec_map.clear()
    _spec_map["auto-routine"] = _spec("auto-routine", auto=True)
    _spec_map["manual-routine"] = _spec("manual-routine", auto=False)

    ret_auto, _ = await _run_dispatch(routine_id="auto-routine")
    ret_manual, _ = await _run_dispatch(routine_id="manual-routine")
    ret_chat, _ = await _run_dispatch(routine_id=None)

    for ret in (ret_auto, ret_manual, ret_chat):
        assert "Pending operator approval" in ret
