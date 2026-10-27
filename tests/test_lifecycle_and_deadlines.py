"""Synthetic lifecycle, deadline, protocol and diagnostics acceptance checks."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from stable_identity.engine import TOOL_NAME
from .conftest import HOST_ROOT, PLUGIN_ROOT, SDK_ROOT, grant, item_for, message, system_item
from .test_memory_bridge import MemoryHost, native_record, notes


class UnrelatedPayload(dict):
    def __deepcopy__(self, memo):
        raise AssertionError("Unrelated native/media payload must not be deep-copied")


async def test_annotation_preserves_native_payloads_and_input_media(host):
    plugin = await host.plugin()
    media = UnrelatedPayload(type="image", image={"url": "synthetic://image", "metadata": [1, 2]})
    user = item_for(message())
    user["parts"].append(media)
    native = UnrelatedPayload(item_type="FunctionCallOutputItem", call_id="native", output="synthetic", success=True)
    source = [system_item(), user, native]
    before_text = user["parts"][0]["text"]
    try:
        output = await plugin.before_replyer(items=source, item_schema_version=1, session_id="group-a")
        actual = output["modified_kwargs"]["items"]
        assert 'sender_id="qq:1001"' in actual[1]["parts"][0]["text"]
        assert actual[1]["parts"][1] == media and actual[2] is native
        assert user["parts"][0]["text"] == before_text and len(source[0]["parts"]) == 1
    finally:
        await plugin.on_unload()


async def test_foreign_definitions_and_output_take_no_copy_fast_path(host):
    plugin = await host.plugin()
    native = UnrelatedPayload(item_type="FunctionCallItem", tool_call={"func_name": "reply", "args": {"text": "x"}})
    definition = UnrelatedPayload(type="function", function={"name": "query_memory"})
    try:
        assert plugin.engine.bind_definitions([definition], "group-a", {})[0] is definition
        output = await plugin.after_planner(output_items=[native], item_schema_version=1, session_id="group-a")
        assert output == {"action": "continue"}
        assert not plugin.engine.tickets.values
    finally:
        await plugin.on_unload()


@pytest.mark.parametrize(
    "query",
    [
        {"keyword": "x" * 40000},
        {"message_ids": ["101"] * 1000},
        {"keyword": {"nested": ["do-not-copy"] * 1000}},
        {"start_time": 10**1000},
        {"limit": float("inf")},
    ],
)
async def test_oversize_or_nested_arguments_are_rejected_before_execution(host, query):
    plugin = await host.plugin()
    try:
        args, _ = await grant(plugin, query=query)
        assert (await plugin.lookup_original_messages(**args))["status"] == "invalid_query"
    finally:
        await plugin.on_unload()


async def test_mixed_authorization_preserves_foreign_call_and_freezes_own_arguments(host):
    plugin = await host.plugin()
    try:
        args, _ = await grant(plugin)
        ref = args["context_ref"]
        native = UnrelatedPayload(item_type="FunctionCallItem", tool_call={"func_name": "query_memory", "args": {}})
        own = {"item_type": "FunctionCallItem", "tool_call": {"call_id": "new", "func_name": TOOL_NAME,
              "args": {"context_ref": ref, "message_ids": ["101"], "stream_id": "group-b"}}}
        before = deepcopy(own)
        result = plugin.engine.authorize_calls([native, own], "group-a")
        assert result[0] is native and own == before
        assert result[1]["tool_call"]["call_id"] == "new"
        own["tool_call"]["args"]["message_ids"][0] = "wrong-id"
        found = await plugin.lookup_original_messages(**result[1]["tool_call"]["args"])
        assert found["records"][0]["message_id"] == "101"
    finally:
        await plugin.on_unload()


async def test_identity_and_memory_share_remaining_request_budget(monkeypatch):
    import stable_identity.plugin as module

    monkeypatch.setattr(module, "REQUEST_BUDGET_SECONDS", 0.05)
    host = MemoryHost()
    native = native_record(host)
    host.delay = host.record_delay = 0.3
    plugin = await host.plugin()
    plugin.engine.settings = replace(plugin.engine.settings, hook_budget=0.03)
    original = [system_item(), item_for(message()), *native]
    started = time.monotonic()
    try:
        result = await plugin.before_planner(items=original, item_schema_version=1, session_id="group-a")
        assert time.monotonic() - started < 0.25
        actual = result["modified_kwargs"]["items"]
        assert actual[-2:] == native and original[1]["parts"][0]["text"] == item_for(message())["parts"][0]["text"]
        assert 'identity_status="unknown"' in actual[1]["parts"][0]["text"]
        assert "sender_id=" not in actual[1]["parts"][0]["text"]
        assert any(note["status"] == "budget_exhausted" for note in notes(actual))
    finally:
        await plugin.on_unload()


async def test_zero_budget_removes_unverified_old_markers_without_rpc(host, monkeypatch):
    import stable_identity.plugin as module

    monkeypatch.setattr(module, "REQUEST_BUDGET_SECONDS", 0)
    plugin = await host.plugin()
    source = item_for(message())
    source["parts"][0]["text"] = source["parts"][0]["text"].replace(
        'user="小明"', 'user="小明" identity_status="verified" identity_user_id="9999"'
    )
    result = await plugin.before_replyer(items=[system_item(), source], item_schema_version=1, session_id="group-a")
    assert 'identity_status="unknown"' in str(result) and 'sender_id="' not in str(result)
    assert host.calls == []
    await plugin.on_unload()


async def test_retro_target_obeys_remaining_budget(host, monkeypatch):
    import stable_identity.plugin as module

    monkeypatch.setattr(module, "REQUEST_BUDGET_SECONDS", 0.02)
    original_rpc = host.rpc

    async def delayed(method, plugin_id, payload, **kwargs):
        if payload["capability"] == "message.get_by_id":
            await asyncio.sleep(0.3)
        return await original_rpc(method, plugin_id, payload, **kwargs)

    host.rpc = delayed
    plugin = await host.plugin()
    started = time.monotonic()
    try:
        result = await plugin.before_replyer(items=[system_item()], item_schema_version=1, session_id="group-a", reply_message_id="101")
        assert time.monotonic() - started < 0.2
        assert '"identity_status": "unknown"' in str(result) and '"user_id"' not in str(result)
    finally:
        await plugin.on_unload()


async def test_fresh_route_does_not_join_older_inflight_session_read(host):
    started, release = asyncio.Event(), asyncio.Event()
    original_rpc = host.rpc
    first = True

    async def delayed(method, plugin_id, payload, **kwargs):
        nonlocal first
        result = await original_rpc(method, plugin_id, payload, **kwargs)
        if payload["capability"] == "database.get" and payload["args"]["model_name"] == "ChatSession" and first:
            first = False
            started.set()
            await release.wait()
        return result

    host.rpc = delayed
    plugin = await host.plugin()
    old = asyncio.create_task(plugin.engine.session("group-a"))
    await asyncio.wait_for(started.wait(), 1)
    host.sessions[0]["account_id"] = "9002"
    try:
        fresh = await asyncio.wait_for(plugin.engine.session("group-a", fresh=True), 0.5)
        assert fresh["account_id"] == "9002"
    finally:
        release.set()
        await old
        await plugin.on_unload()


@pytest.mark.parametrize("lifecycle", ["unload", "reload", "config_update"])
async def test_pending_identity_hook_cannot_publish_after_load_generation_changes(host, lifecycle):
    host.delay = 0.3
    plugin = await host.plugin()
    old = plugin.engine
    pending = asyncio.create_task(plugin.before_replyer(items=[item_for(message())], item_schema_version=1, session_id="group-a"))
    while not host.active:
        await asyncio.sleep(0)
    if lifecycle == "config_update":
        await plugin.on_config_update("self", {}, "0.1.0")
    else:
        await plugin.on_unload()
        if lifecycle == "reload":
            await plugin.on_load()
    assert await asyncio.wait_for(pending, 0.5) == {"action": "continue"}
    assert old.closed and not old.records.values and not old.tickets.values
    if plugin.active():
        host.delay = 0
        result = await plugin.before_replyer(items=[item_for(message())], item_schema_version=1, session_id="group-a")
        assert 'sender_id="qq:1001"' in str(result)
    await plugin.on_unload()


async def test_external_cancellation_is_not_swallowed(host):
    plugin = await host.plugin()
    host.delay = 0.3
    pending = asyncio.create_task(plugin.before_replyer(items=[item_for(message())], item_schema_version=1, session_id="group-a"))
    while not host.active:
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await plugin.on_unload()


async def test_reloaded_memory_hook_does_not_publish_old_reference():
    host = MemoryHost()
    native = native_record(host)
    started, release = asyncio.Event(), asyncio.Event()
    original_rpc = host.rpc

    async def delayed(method, plugin_id, payload, **kwargs):
        if payload["capability"] == "database.get" and payload["args"]["model_name"] == "ToolRecord":
            started.set()
            await release.wait()
        return await original_rpc(method, plugin_id, payload, **kwargs)

    host.rpc = delayed
    plugin = await host.plugin()
    pending = asyncio.create_task(plugin.before_planner(items=[system_item(), *native], item_schema_version=1, session_id="group-a"))
    await asyncio.wait_for(started.wait(), 1)
    await plugin.on_config_update("self", {}, "0.1.0")
    release.set()
    assert await pending == {"action": "continue"}
    assert not plugin.memory.refs.values
    assert (await plugin.status())["activity"]["requests"] == {}
    await plugin.on_unload()


async def test_inflight_lookup_after_reload_does_not_return_old_evidence(host):
    started, release = asyncio.Event(), asyncio.Event()
    original_rpc = host.rpc

    async def delayed(method, plugin_id, payload, **kwargs):
        if payload["capability"] == "message.get_by_id":
            started.set()
            await release.wait()
        return await original_rpc(method, plugin_id, payload, **kwargs)

    host.rpc = delayed
    plugin = await host.plugin()
    args, _ = await grant(plugin)
    pending = asyncio.create_task(plugin.lookup_original_messages(**args))
    await asyncio.wait_for(started.wait(), 1)
    await plugin.on_config_update("self", {}, "0.1.0")
    release.set()
    result = await pending
    assert result["status"] == "unauthorized" and "records" not in result
    await plugin.on_unload()


async def test_status_has_only_aggregate_diagnostics_and_is_detached(host):
    plugin = await host.plugin()
    private = "SYNTHETIC_PRIVATE_BODY_DO_NOT_LOG"
    await plugin.before_replyer(items=[system_item(), item_for(message(), text=private)], item_schema_version=1, session_id="group-a")
    calls_before = len(host.calls)
    status = await plugin.status()
    assert len(host.calls) == calls_before
    assert status["enabled"] and status["activity"]["requests"]["Replyer"] == 1
    encoded = json.dumps(status)
    assert all(value not in encoded for value in [private, "1001", "identity-context:", "identity-call:", "group-a"])
    status["activity"]["requests"]["Replyer"] = -1
    assert (await plugin.status())["activity"]["requests"]["Replyer"] == 1
    await plugin.on_unload()
    assert not (await plugin.status())["enabled"]


def test_two_process_restart_restores_history_but_rejects_old_ticket(tmp_path):
    code = r'''
import asyncio, json, sys
from pathlib import Path
from tests.conftest import SyntheticHost, grant, item_for, message, system_item

async def main():
    state = Path(sys.argv[2])
    host = SyntheticHost()
    host.messages = [message("101", "1001"), message("101", "1002", sid="group-b", account="9001", scope="adapter-b")]
    plugin = await host.plugin()
    try:
        if sys.argv[1] == "save":
            args, _ = await grant(plugin)
            state.write_text(json.dumps({"messages": host.messages, "old_args": args}))
        else:
            stored = json.loads(state.read_text())
            host.messages = stored["messages"]
            assert not plugin.engine.observed.values
            assert (await plugin.lookup_original_messages(**stored["old_args"]))["status"] == "unauthorized"
            for msg, sid, uid in [(host.messages[0], "group-a", "1001"), (host.messages[1], "group-b", "1002")]:
                result = await plugin.before_replyer(items=[system_item(), item_for(msg)], item_schema_version=1, session_id=sid)
                assert 'sender_id="qq:' + uid + '"' in str(result)
                args, _ = await grant(plugin, sid=sid, items=[system_item(), item_for(msg)])
                assert (await plugin.lookup_original_messages(**args))["records"][0]["user_id"] == uid
        print("SYNTHETIC_RESTART_OK")
    finally:
        await plugin.on_unload()

asyncio.run(main())
'''
    env = {
        **os.environ,
        "MAIBOT_PLUGIN_SDK_PATH": str(SDK_ROOT),
        "MAIBOT_INSTANCE_DIR": str(tmp_path / "instance"),
        "PYTHONPATH": os.pathsep.join([str(PLUGIN_ROOT), str(PLUGIN_ROOT.parent), str(SDK_ROOT), str(HOST_ROOT)]),
    }
    for phase in ["save", "recover"]:
        result = subprocess.run([sys.executable, "-c", code, phase, str(Path(tmp_path) / "synthetic-state.json")],
                                cwd=tmp_path, env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "SYNTHETIC_RESTART_OK" in result.stdout
