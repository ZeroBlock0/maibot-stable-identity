from types import SimpleNamespace

import json
import os
import subprocess
import sys

import pytest

from src.llm_models.payload_content.context_item import (
    ContextItemMeta,
    ContextToolCall,
    FunctionCallItem,
    FunctionCallOutputItem,
)
from src.llm_models.payload_content.context_protocol import ContextProtocolMode
from .conftest import HOST_ROOT, PLUGIN_ROOT, SDK_ROOT, grant, message, tool_defs
from .host_harness import (
    BUILD_TOOL_PAYLOAD,
    DESERIALIZE,
    History,
    Planner,
    Replyer,
    SERIALIZE,
    expand_message_id_aliases,
)
from .host_harness import NS, source_function, dispatch_hook_result


@pytest.mark.parametrize("value", ["", "101", " 101 ", "ROBOT1.0_" + "synthetic" * 20, "SYNTHETIC_LONG_MESSAGE_053992"])
def test_alias_algorithm_matches_current_host(value):
    from stable_identity.engine import display_id
    from src.maisaka.context.message_id_alias import to_display_message_id

    assert display_id(value) == to_display_message_id(value)


async def build_requests(host):
    host.messages = [
        message("101", "1001", "小明"),
        message("102", "1002", "小明", text="我在成都工作"),
        message("103", "1001", "改过的昵称", text="我还是之前那个人"),
    ]
    plugin = await host.plugin()
    history = [History(m, m["raw_message"][-1]["data"]) for m in host.messages]
    planner, count = Planner().build(history, enable_visual_message=False)
    assert count == 3
    planner_kwargs = dict(
        items=SERIALIZE(planner), item_schema_version=1, session_id="group-a", tool_definitions=tool_defs(plugin)
    )
    result = await plugin.before_planner(**planner_kwargs)
    dispatched = dispatch_hook_result("maisaka.planner.before_request", planner_kwargs, result)
    processed_planner = DESERIALIZE(
        dispatched.kwargs["items"],
        item_schema_version=dispatched.kwargs.get("item_schema_version"),
        original_items=planner,
    )
    replyer = Replyer()
    replyer.request_type = "synthetic.replyer"
    replyer_items = replyer.build(history, reply_message=None, reply_reason="合成回复理由")

    async def invoke_hook(name, **kwargs):
        assert name == "maisaka.replyer.before_model_request"
        response = await plugin.before_replyer(**kwargs)
        return dispatch_hook_result(name, kwargs, response)

    replyer._get_runtime_manager = lambda: SimpleNamespace(invoke_hook=invoke_hook)
    processed_replyer = await replyer.hook(
        request_messages=replyer_items,
        session_id="group-a",
        active_task_name="replyer",
        active_model_name=None,
        model_info=None,
        attempt=1,
        retry_count=0,
        reply_message=None,
        reply_reason="合成回复理由",
        selected_expression_ids=[],
        reply_tool_args={},
    )
    return {
        "planner_before": SERIALIZE(planner),
        "planner_after": SERIALIZE(processed_planner),
        "replyer_before": SERIALIZE(replyer_items),
        "replyer_after": SERIALIZE(processed_replyer),
    }


async def test_real_host_request_builders_and_replyer_hook(host):
    requests = await build_requests(host)
    for name in ("planner_after", "replyer_after"):
        text = json.dumps(requests[name], ensure_ascii=False)
        assert text.count('identity_user_id=\\"1001\\"') == 2
        assert text.count('identity_user_id=\\"1002\\"') == 1
    assert "identity_user_id" not in json.dumps(requests["planner_before"])


async def test_host_tool_expansion_cannot_change_frozen_query(host):
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"message_id": "101", "keyword": "我在杭州工作", "stream_id": "group-b"})
    expanded = expand_message_id_aliases(args, {"101": "evil-other-id", "我在杭州工作": "evil-keyword"})
    invocation = SimpleNamespace(arguments=expanded, stream_id="group-a", reasoning="")
    context = SimpleNamespace(
        stream_id="group-a", session_id="group-a", group_id="group-a", user_id="", platform="qq", metadata={}
    )
    payload = BUILD_TOOL_PAYLOAD(SimpleNamespace(invoke_method="plugin.invoke_tool"), invocation, context)
    result = await plugin.lookup_original_messages(**payload)
    assert result["records"][0]["message_id"] == "101"


async def test_host_protocol_keeps_native_tool_pairs_and_replay_objects(host):
    plugin = await host.plugin()
    history = History(message(), "合成正文")
    call = FunctionCallItem(
        meta=ContextItemMeta.create(logical_turn_id="native-turn"),
        tool_call=ContextToolCall.create(call_id="native-call", func_name="query_memory", args={"person_name": "小明"}),
    )
    output = FunctionCallOutputItem(
        meta=ContextItemMeta.create(logical_turn_id="native-turn"),
        call_id="native-call",
        output="合成记忆结果",
        tool_name="query_memory",
        success=True,
    )
    items = [history.item, call, output]
    result = await plugin.before_planner(
        items=SERIALIZE(items), item_schema_version=1, session_id="group-a", tool_definitions=tool_defs(plugin)
    )
    restored = DESERIALIZE(
        result["modified_kwargs"]["items"], original_items=items, mode=ContextProtocolMode.REQUEST_CONTEXT
    )
    assert restored[1] is call and restored[2] is output
    assert SERIALIZE(items)[1:] == SERIALIZE(restored)[1:]


async def test_native_compaction_inherits_annotated_snapshot_without_rewriting_delta(host):
    requests = await build_requests(host)
    import copy
    from src.llm_models.payload_content.context_snapshot import deserialize_context_item_snapshot

    class Request:
        def __init__(self, items):
            self.context_items = items

        def detached_snapshot(self):
            return copy.deepcopy(self)

    covered, delta = object(), object()
    namespace = dict(NS)
    namespace.update(
        load_prompt=lambda *args, **kwargs: "合成压缩指令",
        message_text_for_compaction=lambda message: "合成新增资料：无身份标记",
    )
    build = source_function("src/maisaka/context/compaction.py", "_build_compaction_request", namespace)
    snapshot = SimpleNamespace(
        history=(covered,),
        request=Request([deserialize_context_item_snapshot(item) for item in requests["planner_after"]]),
    )
    result = build(
        [covered, delta], planner_snapshot=snapshot, previous_summary=None, recent_messages=[], max_summary_chars=1000
    )
    assert SERIALIZE(result.context_items[:-1]) == requests["planner_after"]
    assert "合成新增资料：无身份标记" in str(SERIALIZE(result.context_items[-1:]))
    assert "identity_user_id" not in str(SERIALIZE(result.context_items[-1:]))


async def test_native_memory_filter_fallback_is_real_and_not_exposed_as_verified(host):
    # Demonstrate the actual Host limitation with synthetic hits, not a guess.
    function = source_function(
        "src/A_memorix/core/runtime/services/search_hit_processing_service.py",
        "_filter_hits",
        dict(NS),
        "MemorySearchHitProcessingService",
    )
    other_person_hits = [{"content": "合成乙的记忆", "metadata": {"person_ids": ["synthetic-person-b"]}}]
    assert function(other_person_hits, "synthetic-person-a") == other_person_hits
    plugin = await host.plugin()
    assert not any("lookup_memory" in c["name"] for c in plugin.get_components())


def test_actual_runner_loader_manifest_and_sdk_contract(tmp_path):
    # Separate interpreter: Host logging and legacy import hooks stay in temp space.
    env = {
        **os.environ,
        "MAIBOT_PLUGIN_SDK_PATH": str(SDK_ROOT),
        "MAIBOT_INSTANCE_DIR": str(tmp_path),
        "PYTHONPATH": os.pathsep.join([str(HOST_ROOT), str(SDK_ROOT)]),
    }
    code = """
from pathlib import Path
from src.plugin_runtime.runner.plugin_loader import PluginLoader
import sys
sys.path.insert(0, sys.argv[1])
from tests.host_harness import source_function, NS
from src.plugin_runtime.hook_schema_utils import build_object_schema
from src.plugin_runtime.host.hook_spec_registry import HookSpec, HookSpecRegistry
from src.plugin_runtime.host.component_registry import ComponentRegistry
from src.plugin_runtime.host.api_registry import APIRegistry
from src.plugin_runtime.protocol.envelope import ComponentDeclaration
loader = PluginLoader(host_version='1.3.15')
candidate = loader._discover_single_candidate(Path(__import__('sys').argv[1]))
assert candidate is not None, loader.failed_plugins
plugin_id, values = candidate
meta = loader.load_candidate(plugin_id, values)
assert meta is not None, loader.failed_plugins
assert len(meta.instance.get_components()) == 6
NS.update(HookSpec=HookSpec, build_object_schema=build_object_schema)
specs = HookSpecRegistry()
source_function('src/chat/message_receive/bot.py', 'register_chat_hook_specs', NS)(specs)
source_function('src/maisaka/chat_loop_service.py', 'register_maisaka_hook_specs', NS)(specs)
registry = ComponentRegistry(hook_spec_registry=specs)
declarations = [ComponentDeclaration(name=c['name'], component_type=c['type'], plugin_id=plugin_id, metadata=c['metadata']).model_dump() for c in meta.instance.get_components()]
assert registry.register_plugin_components(plugin_id, [c for c in declarations if c['component_type'] != 'API']) == 5
assert APIRegistry().register_plugin_apis(plugin_id, declarations) == 1
assert len(registry.get_tools()) == 1
print('RUNNER_LOADER_OK')
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(PLUGIN_ROOT)], env=env, cwd=tmp_path, text=True, capture_output=True
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert "RUNNER_LOADER_OK" in process.stdout
