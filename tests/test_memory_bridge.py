"""Synthetic native records built by the actual Host serialization functions."""

from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import asyncio
import hashlib
import json
import logging

import pytest

from src.core.tooling import ToolExecutionResult, ToolInvocation, ToolSpec
from stable_identity.memory_bridge import MARKER, REF_RE
from .conftest import STAMP, SyntheticHost, grant, item_for, message, system_item
from .host_harness import (
    DESERIALIZE,
    NS,
    History,
    Replyer,
    SERIALIZE,
    dispatch_hook_result,
    expand_message_id_aliases,
    source_function,
)


def pid(uid="1001"):
    return hashlib.md5(f"qq_{uid}".encode()).hexdigest()


class MemoryHost(SyntheticHost):
    def __init__(self):
        super().__init__()
        self.records = []
        self.people = [{"person_id": pid(uid), "platform": "qq", "user_id": uid} for uid in ("1001", "1002", "9000")]
        self.record_delay = 0

    async def rpc(self, method, plugin_id, payload, **kwargs):
        args = payload["args"]
        if payload["capability"] == "database.get" and args["model_name"] == "Messages":
            # database.get returns FLAT SQLModel fields, unlike message.* which
            # returns a nested MessageDict. Preserve the actual boundary.
            self.calls.append((payload["capability"], deepcopy(args)))
            rows = [
                {
                    **m,
                    **m["message_info"]["user_info"],
                    "group_id": (m["message_info"].get("group_info") or {}).get("group_id"),
                }
                for m in self.messages
            ]
            rows = [row for row in rows if all(row.get(k) == v for k, v in args["filters"].items())]
            return {"success": True, "result": deepcopy(rows[: args["limit"]])}
        if payload["capability"] == "database.get" and args["model_name"] in {"ToolRecord", "PersonInfo"}:
            if args["model_name"] == "ToolRecord" and self.record_delay:
                await asyncio.sleep(self.record_delay)
            self.calls.append((payload["capability"], deepcopy(args)))
            rows = self.records if args["model_name"] == "ToolRecord" else self.people
            rows = [row for row in rows if all(row.get(k) == v for k, v in args["filters"].items())]
            return {"success": True, "result": deepcopy(rows[: args["limit"]])}
        return await super().rpc(method, plugin_id, payload, **kwargs)


def hit(uid="1001", ids=None, sid="group-a"):
    return {
        "content": "合成人物事实，不能把此句当原始发言。",
        "type": "factual",
        "metadata": {
            "chat_id": sid,
            "chat_ids": [sid],
            "person_ids": [pid(uid)],
            "evidence_message_ids": ["101"] if ids is None else ids,
        },
    }


def native_record(
    host,
    *,
    tool="query_memory",
    structured=None,
    enabled=True,
    sid="group-a",
    call="native-memory",
    content="合成原生检索结果",
):
    namespace = dict(NS, global_config=SimpleNamespace(debug=SimpleNamespace(record_tool_structured_content=enabled)))
    for name in (
        "normalize_tool_record_value",
        "_is_large_inline_media_text",
        "_build_omitted_media_marker",
        "_omit_tool_record_large_media",
        "_build_omitted_structured_content_marker",
        "build_tool_record_structured_content",
        "build_tool_record_payload",
    ):
        source_function("src/maisaka/utils/tool_record_payload.py", name, namespace)
    structured = (
        structured if structured is not None else {"success": True, "chat_id": sid, "person_id": pid(), "hits": [hit()]}
    )
    invocation = ToolInvocation(
        tool_name=tool, call_id=call, session_id=sid, stream_id=sid, arguments={"query": "合成查询"}
    )
    result = ToolExecutionResult(tool_name=tool, success=True, content=content, structured_content=structured)
    spec = ToolSpec(name=tool, provider_name="maisaka_builtin", provider_type="builtin")
    data = namespace["build_tool_record_payload"](invocation, result, spec)
    row = {"tool_id": call, "session_id": sid, "tool_name": tool, "tool_data": json.dumps(data, ensure_ascii=False)}
    host.records.append(row)
    meta = {
        "item_id": "synthetic-native",
        "logical_turn_id": "native-turn",
        "timestamp": datetime.fromtimestamp(STAMP).isoformat(),
    }
    return [
        {
            "item_type": "FunctionCallItem",
            "meta": {**meta, "item_id": "synthetic-call"},
            "tool_call": {"call_id": call, "func_name": tool, "args": invocation.arguments, "extra_content": None},
        },
        {
            "item_type": "FunctionCallOutputItem",
            "meta": meta,
            "call_id": call,
            "tool_name": tool,
            "success": True,
            "output": content,
        },
    ]


def notes(items):
    text = next((
        p["text"]
        for i in items
        if i["item_type"] == "SystemMessageItem"
        for p in i["parts"]
        if p.get("text", "").startswith(MARKER)
    ), None)
    return json.loads(text[len(MARKER) :]) if text is not None else []


async def planner(plugin, native):
    original = {
        "items": [system_item(), item_for(message()), *native],
        "item_schema_version": 1,
        "session_id": "group-a",
    }
    response = await plugin.before_planner(**original)
    dispatched = dispatch_hook_result("maisaka.planner.before_request", original, response)
    DESERIALIZE(dispatched.kwargs["items"], item_schema_version=dispatched.kwargs.get("item_schema_version"))
    assert dispatched.kwargs["items"][-len(native) :] == native
    return dispatched.kwargs["items"]


async def test_complete_native_memory_record_maps_subject_and_fresh_original_sources():
    host = MemoryHost()
    native = native_record(host)
    plugin = await host.plugin()
    items = await planner(plugin, native)
    note = notes(items)[0]
    assert note["status"] == "structured_checked"
    checked = note["hits"][0]
    assert checked["status"] == "source_identity_verified"
    assert checked["subject"] == {"platform": "qq", "user_id": "1001", "person_id": pid()}
    assert checked["sources"][0]["message_id"] == "101"
    assert checked["sources"][0]["speaker_kind"] == "user"
    assert checked["semantic_status"] == "not_evaluated"
    # No memory-content copy or extra retrieval in the independent note.
    assert "合成人物事实" not in json.dumps(note, ensure_ascii=False)
    assert REF_RE.fullmatch(note["reference"])
    assert all(cap not in {"knowledge.search", "database.save", "database.query"} for cap, _ in host.calls)


@pytest.mark.parametrize("tool", ["query_memory", "query_person_profile"])
async def test_host_omission_marker_never_authenticates_memory_and_is_logged(tool, caplog):
    host = MemoryHost()
    native = native_record(host, tool=tool, enabled=False)
    plugin = await host.plugin()
    caplog.set_level(logging.INFO, logger=plugin.ctx.logger.name)
    note = notes(await planner(plugin, native))[0]
    assert note["status"] == "structured_omitted" and "subject" not in note and "hits" not in note
    assert any("structured_omitted" in r.message for r in caplog.records)
    assert all("1001" not in r.message and "native-memory" not in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "change,expected",
    [
        ("missing", "record_unavailable"),
        ("duplicate", "record_unavailable"),
        ("provider", "record_mismatch"),
        ("session", "record_mismatch"),
        ("call", "record_mismatch"),
        ("output", "record_mismatch"),
        ("malformed", "lookup_error"),
        ("huge", "record_unreadable"),
    ],
)
async def test_native_record_requires_unique_scoped_builtin_exact_result(change, expected):
    host = MemoryHost()
    native = native_record(host)
    row = host.records[0]
    data = json.loads(row["tool_data"])
    if change == "missing":
        host.records.clear()
    elif change == "duplicate":
        host.records.append(deepcopy(row))
    elif change == "provider":
        data["provider_type"] = "plugin"
    elif change == "session":
        data["stream_id"] = "group-b"
    elif change == "call":
        data["call_id"] = "different-call"
    elif change == "output":
        data["history_content"] = "不同原始结果"
    if change in {"provider", "session", "call", "output"}:
        row["tool_data"] = json.dumps(data)
    elif change == "malformed":
        row["tool_data"] = "{"
    elif change == "huge":
        row["tool_data"] = "x" * 262145
    plugin = await host.plugin()
    note = notes(await planner(plugin, native))[0]
    assert note["status"] == expected and "hits" not in note


async def test_fallback_hit_is_mapped_to_actual_other_subject_not_requested_subject(caplog):
    host = MemoryHost()
    host.messages.append(message("102", "1002"))
    native = native_record(
        host,
        structured={
            "success": True,
            "chat_id": "group-a",
            "person_id": pid(),
            "fallback_applied": True,
            "hits": [hit("1002", ["102"])],
        },
    )
    plugin = await host.plugin()
    caplog.set_level(logging.INFO, logger=plugin.ctx.logger.name)
    checked = notes(await planner(plugin, native))[0]["hits"][0]
    assert checked["subject"]["user_id"] == "1002"
    assert checked["matches_query_subject"] is False
    assert any("query_subject_mismatch" in r.message for r in caplog.records)


@pytest.mark.parametrize(
    "change,status",
    [
        ("no_owner", "subject_unknown"),
        ("multi_owner", "subject_unknown"),
        ("conflict", "subject_conflict"),
        ("foreign_scope", "scope_mismatch"),
        ("multi_scope", "scope_mismatch"),
        ("missing_source", "evidence_incomplete"),
        ("wrong_sender", "evidence_incomplete"),
        ("no_source", "evidence_incomplete"),
        ("duplicate_source", "evidence_incomplete"),
        ("route_changed", "evidence_incomplete"),
    ],
)
async def test_missing_or_conflicting_evidence_is_not_promoted_to_verified(change, status):
    host = MemoryHost()
    memory = hit()
    if change == "no_owner":
        memory["metadata"]["person_ids"] = []
    elif change == "multi_owner":
        memory["metadata"]["person_ids"] = [pid(), pid("1002")]
    elif change == "conflict":
        memory["metadata"]["person_id"] = pid("1002")
    elif change == "foreign_scope":
        memory["metadata"]["chat_id"] = "group-b"
    elif change == "multi_scope":
        memory["metadata"]["chat_ids"].append("group-b")
    elif change == "missing_source":
        memory["metadata"]["evidence_message_ids"] = ["missing"]
    elif change == "wrong_sender":
        host.messages.append(message("102", "1002"))
        memory["metadata"]["evidence_message_ids"] = ["102"]
    elif change == "no_source":
        memory["metadata"]["evidence_message_ids"] = []
    elif change == "duplicate_source":
        host.messages.append(deepcopy(host.messages[0]))
    elif change == "route_changed":
        host.messages.append(message("102", account="9001"))
        memory["metadata"]["evidence_message_ids"] = ["102"]
    native = native_record(host, structured={"success": True, "chat_id": "group-a", "hits": [memory]})
    plugin = await host.plugin()
    checked = notes(await planner(plugin, native))[0]["hits"][0]
    assert checked["status"] == status


async def test_foreign_person_mapping_is_not_exposed_without_current_session_presence():
    host = MemoryHost()
    native = native_record(host, structured={"success": True, "chat_id": "group-a", "hits": [hit("1002", ["missing"])]})
    plugin = await host.plugin()
    checked = notes(await planner(plugin, native))[0]["hits"][0]
    assert checked == {"index": 1, "status": "subject_unknown", "reason": "no_current_session_presence"}


async def test_profile_mapping_does_not_claim_profile_is_raw_evidence():
    host = MemoryHost()
    native = native_record(
        host, tool="query_person_profile", structured={"success": True, "person_id": pid(), "summary": "合成画像"}
    )
    plugin = await host.plugin()
    note = notes(await planner(plugin, native))[0]
    assert note["subject"]["user_id"] == "1001"
    assert note["evidence_status"] == "not_exposed_by_profile_tool"
    assert note["semantic_status"] == "not_evaluated"


async def test_replyer_rechecks_explicit_reference_without_modifying_native_reply_arguments():
    host = MemoryHost()
    plugin = await host.plugin()
    first = notes(await planner(plugin, native_record(host)))[0]
    args = {"reply_reference": "使用这份参考 " + first["reference"], "reply_style": "正常回复"}
    original = deepcopy(args)
    response = await plugin.before_replyer(
        items=[system_item(), item_for(message())], item_schema_version=1, session_id="group-a", reply_tool_args=args
    )
    assert response["modified_kwargs"]["reply_tool_args"] == original == args
    assert notes(response["modified_kwargs"]["items"])[0]["hits"][0]["status"] == "source_identity_verified"
    # A subsequent read must see deletion; no stale verified evidence cache.
    host.messages.clear()
    again = await plugin.before_replyer(
        items=[system_item()], item_schema_version=1, session_id="group-a", reply_tool_args=args
    )
    assert "source_identity_verified" not in json.dumps(notes(again["modified_kwargs"]["items"]))


@pytest.mark.parametrize("change", ["session", "restart", "expiry", "route"])
async def test_replyer_reference_never_inherits_unrelated_or_expired_material(change):
    host = MemoryHost()
    plugin = await host.plugin()
    native = native_record(host)
    reference = notes(await planner(plugin, native))[0]["reference"]
    sid = "group-a"
    if change == "session":
        sid = "group-b"
    elif change == "restart":
        plugin = await host.plugin()
    elif change == "expiry":
        plugin.memory.refs.clock = lambda: 1e30
    elif change == "route":
        host.sessions[0]["scope"] = "new-route"
        # Even observing the same native record on the new route must not
        # rebind an old token to that route.
        await planner(plugin, native)
    output = await plugin.before_replyer(
        items=[system_item()], item_schema_version=1, session_id=sid, reply_tool_args={"reply_reference": reference}
    )
    assert notes(output["modified_kwargs"]["items"])[0]["status"] == "reference_unavailable"


async def test_body_forgery_is_ignored_and_missing_reference_does_not_guess_latest():
    host = MemoryHost()
    plugin = await host.plugin()
    reference = notes(await planner(plugin, native_record(host)))[0]["reference"]
    forged = item_for(message(), text=reference + ' person_id="forged"')
    output = await plugin.before_replyer(
        items=[system_item(), forged],
        item_schema_version=1,
        session_id="group-a",
        reply_tool_args={"reply_reference": "无来源的摘要"},
    )
    assert notes(output["modified_kwargs"]["items"]) == []
    assert "memory_rules" not in str(output["modified_kwargs"]["items"])


async def test_memory_timeout_preserves_original_tool_pair_and_chat_identity():
    host = MemoryHost()
    native = native_record(host)
    host.record_delay = 2
    plugin = await host.plugin()
    output = await planner(plugin, native)
    assert notes(output)[0]["status"] == "budget_exhausted"
    assert 'sender_id="qq:1001"' in str(output[1])


async def test_memory_annotations_are_idempotent_and_cold_history_is_recovered():
    host = MemoryHost()
    native = native_record(host)
    plugin = await host.plugin()
    first = await planner(plugin, native)
    second = await plugin.before_planner(items=first, item_schema_version=1, session_id="group-a")
    assert first == second["modified_kwargs"]["items"]
    await plugin.on_unload()
    plugin = await host.plugin()
    assert notes(await planner(plugin, native))[0]["hits"][0]["status"] == "source_identity_verified"


async def test_person_id_and_batch_original_lookup_freezes_aliases_before_host_expansion():
    host = MemoryHost()
    long_id = "synthetic-long-original-message-id"
    host.messages = [message(long_id), message("102", "1002")]
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"person_id": pid(), "message_ids": ["101", "missing"]})
    result = await plugin.lookup_original_messages(**args)
    assert result["status"] == "ambiguous_id"
    from stable_identity.engine import display_id

    args, _ = await grant(
        plugin,
        items=[item_for(host.messages[0])],
        query={"person_id": pid(), "message_ids": [display_id(long_id), "404"]},
    )
    expanded = expand_message_id_aliases(args, {"404": "102", pid(): pid("1002"), display_id(long_id): "102"})
    result = await plugin.lookup_original_messages(**expanded)
    assert result["records"][0]["message_id"] == long_id
    assert result["records"][0]["person_id"] == pid()
    assert result["missing_message_ids"] == ["404"] and result["incomplete"]


@pytest.mark.parametrize(
    "query,status",
    [
        ({"person_id": "unknown", "message_id": "101"}, "unknown_person"),
        ({"person_id": pid(), "sender_user_id": "1002"}, "invalid_query"),
        ({"message_id": "101", "message_ids": ["101"]}, "invalid_query"),
        ({"message_ids": ["101"] * 13}, "invalid_query"),
        ({"message_ids": [123]}, "invalid_query"),
        ({"message_ids": []}, "invalid_query"),
    ],
)
async def test_person_and_bulk_input_never_guess(query, status):
    host = MemoryHost()
    plugin = await host.plugin()
    args, _ = await grant(plugin, query=query)
    result = await plugin.lookup_original_messages(**args)
    assert result["status"] == status


async def build_memory_requests(*, unattributed=False, drop_reference=False):
    host = MemoryHost()
    plugin = await host.plugin()
    structured = None
    if unattributed:
        summaries = [hit() for _ in range(4)]
        for summary in summaries:
            summary["type"] = "chat_summary"
            summary["metadata"]["person_ids"] = []
            summary["metadata"]["evidence_message_ids"] = []
        structured = {
            "success": True,
            "chat_id": "group-a",
            "person_id": pid(),
            "fallback_applied": False,
            "hits": summaries,
        }
    planner_items = await planner(plugin, native_record(host, structured=structured))
    reference = notes(planner_items)[0]["reference"]
    reply_args = {
        "reply_reference": "合成参考：原话账号 1001，来源消息 101。" if drop_reference else "合成参考 " + reference
    }
    replyer = Replyer()
    replyer.request_type = "synthetic.replyer"
    replyer._build_reply_reference_message = source_function(
        "src/chat/replyer/maisaka_generator_base.py",
        "_build_reply_reference_message",
        dict(NS),
        "BaseMaisakaReplyGenerator",
    )
    original = replyer.build([History(message(), "合成原始发言")], None, "合成理由", reply_tool_args=reply_args)

    async def invoke_hook(name, **kwargs):
        return dispatch_hook_result(name, kwargs, await plugin.before_replyer(**kwargs))

    replyer._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=invoke_hook)
    restored = await replyer.hook(
        request_messages=original,
        session_id="group-a",
        active_task_name="replyer",
        active_model_name=None,
        model_info=None,
        attempt=1,
        retry_count=0,
        reply_message=None,
        reply_reason="合成理由",
        selected_expression_ids=[],
        reply_tool_args=reply_args,
    )
    await plugin.on_unload()
    return {"planner": planner_items, "replyer_before": SERIALIZE(original), "replyer_after": SERIALIZE(restored)}


async def test_actual_replyer_builder_hook_and_protocol_accept_referenced_evidence():
    requests = await build_memory_requests()
    assert notes(requests["replyer_after"])[0]["hits"][0]["status"] == "source_identity_verified"
    reference = notes(requests["planner"])[0]["reference"]
    assert reference in str(requests["replyer_after"])


@pytest.mark.parametrize("uid,kind", [("1001", "user"), ("9000", "bot")])
async def test_source_account_kind_is_explicit(uid, kind):
    host = MemoryHost()
    host.messages = [message(uid=uid)]
    plugin = await host.plugin()
    native = native_record(host, structured={"success": True, "chat_id": "group-a", "hits": [hit(uid)]})
    checked = notes(await planner(plugin, native))[0]["hits"][0]
    assert checked["sources"][0]["speaker_kind"] == kind


async def test_orphan_profile_person_and_inconsistent_account_mapping_stay_unknown():
    host = MemoryHost()
    host.people[0]["user_id"] = "1002"
    plugin = await host.plugin()
    native = native_record(host, tool="query_person_profile", structured={"success": True, "person_id": pid()})
    assert notes(await planner(plugin, native))[0]["status"] == "profile_subject_unknown"
    args, _ = await grant(plugin, query={"person_id": pid(), "message_id": "101"})
    assert (await plugin.lookup_original_messages(**args))["status"] == "unknown_person"


async def test_cross_session_bulk_query_does_not_fetch_foreign_original():
    host = MemoryHost()
    host.messages.append(
        message("foreign", sid="group-b", account="9001", scope="adapter-b", text="SYNTHETIC_OTHER_GROUP_SECRET")
    )
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"message_ids": ["101", "foreign"]})
    result = await plugin.lookup_original_messages(**args)
    assert len(result["records"]) == 1 and result["records"][0]["message_id"] == "101"
    assert result["missing_message_ids"] == ["foreign"]
    assert "SYNTHETIC_OTHER_GROUP_SECRET" not in json.dumps(result)


async def test_oversized_query_does_not_fill_ticket_cache_with_unbounded_values():
    host = MemoryHost()
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"keyword": "x" * 100000})
    assert all(len(str(entry)) < 40000 for entry in plugin.engine.tickets.values.values())
    assert (await plugin.lookup_original_messages(**args))["status"] == "invalid_query"


async def test_focus_memory_scope_is_unknown_without_any_cross_session_query():
    host = MemoryHost()
    plugin = await host.plugin()
    native = native_record(host)
    result = await plugin.before_planner(items=[system_item(), *native], item_schema_version=1, session_id="")
    assert notes(result["modified_kwargs"]["items"]) == [{"status": "unknown_scope"}]
    assert not host.calls


async def test_failed_context_result_is_not_authenticated_by_successful_old_record():
    host = MemoryHost()
    plugin = await host.plugin()
    native = native_record(host)
    native[-1]["success"] = False
    assert notes(await planner(plugin, native))[0]["status"] == "tool_failed"


@pytest.mark.parametrize(
    "pids,reason",
    [
        (None, "missing_subject_ids"),
        ([], "missing_subject_ids"),
        ("not-a-list", "invalid_subject_ids"),
        ([pid(), pid("1002")], "multiple_subject_ids"),
        ([""], "invalid_subject_id"),
        ([123], "invalid_subject_id"),
        (["x" * 129], "invalid_subject_id"),
        (["unknown-person"], "subject_mapping_unavailable"),
    ],
)
async def test_unknown_subject_has_specific_safe_reason_without_guessing(pids, reason, caplog):
    host = MemoryHost()
    memory = hit()
    memory["metadata"]["person_ids"] = pids
    plugin = await host.plugin()
    caplog.set_level(logging.INFO, logger=plugin.ctx.logger.name)
    native = native_record(host, structured={"success": True, "chat_id": "group-a", "hits": [memory]})
    checked = notes(await planner(plugin, native))[0]["hits"][0]
    assert checked == {"index": 1, "status": "subject_unknown", "reason": reason}
    assert any("reason_" + reason in r.message for r in caplog.records)
    assert all("not-a-list" not in r.message and "unknown-person" not in r.message for r in caplog.records)


async def test_real_replyer_path_exposes_four_unattributed_hits_and_dropped_reference():
    requests = await build_memory_requests(unattributed=True, drop_reference=True)
    inspected = notes(requests["planner"])[0]
    assert inspected["fallback_applied"] is False
    assert len(inspected["hits"]) == 4
    assert all(h["reason"] == "missing_subject_ids" and "subject" not in h for h in inspected["hits"])
    assert notes(requests["replyer_after"]) == []
    assert "memory_rules" not in str(requests["replyer_after"])
    # The model-provided source IDs remain ordinary input, never authority to
    # authenticate a memory hit or to fetch arbitrary records.
    assert "来源消息 101" in str(requests["replyer_after"])


async def test_nonempty_reference_missing_is_diagnostic_only_idempotent_and_read_free():
    host = MemoryHost()
    plugin = await host.plugin()
    items = [system_item()]
    args = {"reply_reference": "合成参考：账号 1001，消息 101", "reply_style": "原生风格"}
    original = deepcopy(args)
    for _ in range(2):
        stats = await plugin.memory.enrich(items, "group-a", args)
        assert stats == {"reference_missing": 1}
        assert notes(items) == []
        assert "memory_rules" not in str(items)
    assert args == original and not host.calls


async def test_malformed_hit_metadata_is_diagnosed_and_focus_keeps_unknown_scope():
    host = MemoryHost()
    plugin = await host.plugin()
    native = native_record(host, structured={"success": True, "chat_id": "group-a", "hits": ["bad-shape"]})
    assert notes(await planner(plugin, native))[0]["hits"][0]["reason"] == "invalid_hit_metadata"
    items = [system_item()]
    host.calls.clear()
    stats = await plugin.memory.enrich(items, "", {"reply_reference": "合成参考"})
    assert stats == {"unknown_scope": 1} and notes(items) == [] and not host.calls
