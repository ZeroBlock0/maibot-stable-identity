"""Execute selected, UNEDITED Host functions without booting a configured bot.

AST selection removes only module imports/class wrappers/decorators, not function
bodies. This is source-function integration, NOT a running production Host/Runner.
The caller supplies synthetic history, personality/template fragments and RPC.
"""

# Imported names form the globals of unmodified source functions; lambdas below
# deliberately stand in for persona/template providers, not identity logic.
# ruff: noqa: F401, E731

from copy import deepcopy
from datetime import datetime
from html import escape
from types import SimpleNamespace

import ast
import logging

from src.llm_models.payload_content.context_item import (
    CONTEXT_ITEM_SCHEMA_VERSION,
    ContextItemBuilder,
    ContextItemMeta,
    FunctionCallItem,
    FunctionCallOutputItem,
    RoleType,
    UserMessageItem,
)
from src.llm_models.payload_content.context_protocol import ContextProtocolMode, validate_context_items
from src.llm_models.payload_content.context_snapshot import (
    deserialize_context_item_snapshot,
    serialize_context_item_snapshot,
)
from src.maisaka.context.message_id_alias import to_display_message_id, expand_message_id_aliases
from .conftest import HOST_ROOT


def source_function(relative_path, name, namespace, class_name=None):
    path = HOST_ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    scope = (
        next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name).body
        if class_name
        else tree.body
    )
    node = deepcopy(
        next(node for node in scope if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
    )
    node.decorator_list = []
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


NS = dict(globals(), logger=logging.getLogger("synthetic-host-functions"))
SERIALIZE = source_function("src/plugin_runtime/hook_payloads.py", "serialize_prompt_items", NS)
DESERIALIZE = source_function("src/plugin_runtime/hook_payloads.py", "deserialize_prompt_items", NS)
source_function("src/maisaka/context/planner_messages.py", "_format_quote_ids", NS)
BUILD_PREFIX = source_function("src/maisaka/context/planner_messages.py", "build_planner_prefix", NS)


class History:
    def __init__(self, msg, body):
        self.timestamp = datetime.fromtimestamp(float(msg["timestamp"]))
        user = msg["message_info"]["user_info"]
        text = BUILD_PREFIX(
            timestamp=self.timestamp,
            user_name=user["user_nickname"],
            group_card=user["user_cardname"],
            message_id=msg["message_id"],
        )
        self.item = (
            ContextItemBuilder()
            .set_meta(ContextItemMeta.create(item_id="history:" + msg["message_id"], timestamp=self.timestamp))
            .set_role(RoleType.User)
            .add_text_content(text + body)
            .build()
        )  # noqa: F405


NS.update(
    REPLYER_MAX_HOOK_RETRIES=3,
    build_context_items_from_history_entry=lambda entry, **kwargs: [entry.item],
    ContentContextMessage=History,
    SessionBackedMessage=History,
    ModelOutputContextMessage=type("SyntheticModelOutput", (), {}),
    EmojiCandidateMessage=type("SyntheticEmojiCandidate", (), {}),
    IMAGE_QUALITY_MAX_SIDE={"normal": 512},
    global_config=SimpleNamespace(visual=SimpleNamespace(image_quality="normal", max_image_num=8)),
    limit_latest_images_in_messages=lambda items, **kwargs: items,
)


class Planner:
    _custom_chat_system_prompt = "合成人设：自然聊天，保留原有风格。"
    _get_chat_prompt_name = lambda self: "synthetic-template"
    _build_bot_nickname_notice = lambda self, *args, **kwargs: ""
    _build_current_chat_attention_tail_message = lambda self: ""
    _build_current_time_user_message = lambda self: "合成当前时间提示"
    _validate_function_call_context_anchors = staticmethod(
        source_function(
            "src/maisaka/chat_loop_service.py", "_validate_function_call_context_anchors", NS, "MaisakaChatLoopService"
        )
    )
    build = source_function("src/maisaka/chat_loop_service.py", "_build_request_messages", NS, "MaisakaChatLoopService")


class Replyer:
    _uses_retro_prompt = lambda self: False
    _is_replyer_filtered_history_message = lambda self, msg: False
    _extract_guided_bot_reply = lambda self, msg: ""
    _select_temporary_reply_style = lambda self: ""
    _build_temporary_reply_style_message = lambda self, style: ""
    _build_reply_reference_message = lambda self, *args: ""
    _build_requested_reply_style_message = lambda self, *args: ""
    _build_keyword_reaction_prompt = lambda self, **kwargs: ""
    _build_system_prompt = lambda self, **kwargs: "合成人设：自然聊天，保留原有风格。"
    _build_final_user_message = lambda self, **kwargs: "请自然回复。"
    _build_history_messages = source_function(
        "src/chat/replyer/maisaka_generator_base.py", "_build_history_messages", NS, "BaseMaisakaReplyGenerator"
    )
    build = source_function(
        "src/chat/replyer/maisaka_generator_base.py", "_build_request_messages", NS, "BaseMaisakaReplyGenerator"
    )
    hook = source_function(
        "src/chat/replyer/maisaka_generator_base.py",
        "_invoke_before_model_request_hook",
        NS,
        "BaseMaisakaReplyGenerator",
    )


BUILD_TOOL_PAYLOAD = source_function(
    "src/plugin_runtime/component_query.py", "_build_tool_invocation_payload", NS, "ComponentQueryService"
)
BUILD_CONTEXT_PAYLOAD = source_function(
    "src/plugin_runtime/component_query.py", "_build_tool_context_payload", NS, "ComponentQueryService"
)
NS["ComponentQueryService"] = SimpleNamespace(_build_tool_context_payload=BUILD_CONTEXT_PAYLOAD)

MERGE_BLOCKING_RESULT = source_function(
    "src/plugin_runtime/host/hook_dispatcher.py", "_merge_blocking_result", NS, "HookDispatcher"
)


def dispatch_hook_result(name, original_kwargs, response):
    """Execute actual Host REPLACEMENT semantics, not a synthetic kwargs merge."""
    result = SimpleNamespace(
        hook_name=name, kwargs=dict(original_kwargs), custom_results=[], errors=[], aborted=False, stopped_by=None
    )
    execution = SimpleNamespace(
        custom_result=response.get("custom_result"),
        success=True,
        modified_kwargs=response.get("modified_kwargs"),
        action=response.get("action", "continue"),
    )
    spec = SimpleNamespace(allow_kwargs_mutation=True, allow_abort=False)
    target = SimpleNamespace(entry=SimpleNamespace(full_name="local.stable-identity.test"))
    MERGE_BLOCKING_RESULT(SimpleNamespace(), spec, target, execution, result)
    return result
