from copy import deepcopy
from datetime import datetime

import logging

import pytest

from stable_identity.diagnostics import ActivityReporter
from .conftest import STAMP, item_for, message, system_item, tool_defs
from .host_harness import DESERIALIZE, dispatch_hook_result
from src.llm_models.payload_content.context_protocol import ContextProtocolMode
from src.plugin_runtime.runner.log_handler import RunnerIPCLogHandler


def test_old_partial_hook_response_reproduces_schema_none_rejection():
    original = {"items": [system_item(), item_for(message())], "item_schema_version": 1, "session_id": "group-a"}
    legacy = {"action": "continue", "modified_kwargs": {"items": deepcopy(original["items"])}}
    dispatched = dispatch_hook_result("maisaka.replyer.before_model_request", original, legacy)
    assert dispatched.kwargs.get("item_schema_version") is None
    with pytest.raises(ValueError, match="item_schema_version"):
        DESERIALIZE(dispatched.kwargs["items"], item_schema_version=dispatched.kwargs.get("item_schema_version"))


@pytest.mark.parametrize("stage", ["Planner", "Replyer", "Planner输出"])
async def test_complete_hook_envelope_preserves_schema_and_other_plugin_parameters(host, stage):
    plugin = await host.plugin()
    common = {
        "item_schema_version": 1,
        "session_id": "group-a",
        "another_plugin_context": {"keep": [1, 2]},
        "selected_history_count": 1,
        "selection_reason": "synthetic",
        "attempt": 2,
    }
    if stage == "Planner输出":
        method = plugin.after_planner
        original = {
            **common,
            "output_items": [
                {
                    "item_type": "AssistantMessageItem",
                    "meta": {
                        "item_id": "native-output",
                        "logical_turn_id": None,
                        "timestamp": datetime.fromtimestamp(STAMP).isoformat(),
                    },
                    "parts": [{"type": "text", "text": "合成原生输出"}],
                }
            ],
        }
        field, mode = "output_items", ContextProtocolMode.MODEL_OUTPUT
    else:
        method = plugin.before_planner if stage == "Planner" else plugin.before_replyer
        original = {
            **common,
            "items": [system_item(), item_for(message())],
            "tool_definitions": tool_defs(plugin),
            "reply_message_id": "101",
        }
        field, mode = "items", ContextProtocolMode.REQUEST_CONTEXT
    response = await method(**original)
    dispatched = dispatch_hook_result(stage, original, response)
    assert set(original) <= set(dispatched.kwargs)
    for key in original:
        if key not in {"items", "output_items", "tool_definitions"}:
            assert dispatched.kwargs[key] == original[key]
    restored = DESERIALIZE(
        dispatched.kwargs[field], item_schema_version=dispatched.kwargs.get("item_schema_version"), mode=mode
    )
    assert restored
    if stage != "Planner输出":
        assert 'identity_user_id="1001"' in str(dispatched.kwargs["items"])


async def test_info_activity_reaches_real_runner_ipc_log_handler_without_private_text(host):
    private = "SYNTHETIC_PRIVATE_TEXT_MUST_NOT_BE_LOGGED"
    host.messages = [message(text=private)]
    plugin = await host.plugin()
    logger = plugin.ctx.logger
    previous_level = logger.level
    handler = RunnerIPCLogHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        await plugin.observe_message(message=host.messages[0])
        args = dict(items=[system_item(), item_for(host.messages[0])], item_schema_version=1, session_id="group-a")
        await plugin.before_planner(**args, tool_definitions=tool_defs(plugin))
        await plugin.before_replyer(**args)
        entries = list(handler._buffer)
        assert len(entries) >= 3
        text = "\n".join(entry.message for entry in entries)
        assert "入站 Hook 已调用" in text and "Planner 请求标注完成" in text and "Replyer 请求标注完成" in text
        assert "已核验=1" in text
        assert private not in text and "1001" not in text and "identity-call:" not in text
        assert all(entry.logger_name == "plugin.local.stable-identity" for entry in entries)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


async def test_normal_info_activity_is_rate_limited_but_first_unknown_is_visible(host, caplog):
    plugin = await host.plugin()
    clock = [0.0]
    plugin.diagnostics = ActivityReporter(plugin.ctx.logger, lambda: clock[0], interval=60)
    caplog.set_level(logging.INFO, logger=plugin.ctx.logger.name)
    args = dict(items=[system_item(), item_for(message())], item_schema_version=1, session_id="group-a")
    for _ in range(3):
        await plugin.before_replyer(**args)
    assert sum("Replyer 请求标注完成" in record.message for record in caplog.records) == 1
    unknown = item_for(message("unknown-id"))
    await plugin.before_replyer(items=[unknown], item_schema_version=1, session_id="group-a")
    assert any("未知=1" in record.message and record.levelno == logging.WARNING for record in caplog.records)
    clock[0] = 61
    await plugin.before_replyer(**args)
    assert sum("Replyer 请求标注完成" in record.message for record in caplog.records) == 3


async def test_unsupported_schema_skip_is_visible_without_rewriting_request(host, caplog):
    plugin = await host.plugin()
    caplog.set_level(logging.INFO, logger=plugin.ctx.logger.name)
    result = await plugin.before_planner(items=[item_for(message())], item_schema_version=None, session_id="group-a")
    assert result == {"action": "continue"}
    assert any("Planner Hook 已跳过" in record.message for record in caplog.records)
