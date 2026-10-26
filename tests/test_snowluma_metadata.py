"""Actual adapter codec/router functions, synthetic client and gateway only."""

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import logging
import os
import time

import pytest

from .conftest import HOST_ROOT, STAMP, item_for
from .host_harness import DESERIALIZE, dispatch_hook_result, source_function


ADAPTER_ROOT = Path(os.environ.get("MAIBOT_ADAPTER_SOURCE_ROOT", str(HOST_ROOT.parent / "MaiBot-SnowLuma-Adapter")))
pytestmark = pytest.mark.skipif(not ADAPTER_ROOT.is_dir(), reason="local SnowLuma Adapter clone not present")


@pytest.mark.parametrize("client_type", ["snowluma", "napcat"])
async def test_adapter_platform_qqid_reaches_both_model_requests(host, client_type):
    namespace = {"Mapping": Mapping, "time": time}
    codec_build = source_function(
        str(ADAPTER_ROOT / "codecs/inbound/message_codec.py"), "build_message_dict", namespace, "QQInboundCodec"
    )
    route = source_function(
        str(ADAPTER_ROOT / "runtime/router.py"), "handle_inbound_message", namespace, "QQEventRouter"
    )

    class Codec:
        _profile_state = SimpleNamespace(profile=SimpleNamespace(client_type=client_type))
        build_message_dict = codec_build

        async def _resolve_group_name(self, payload, group_id):
            return "合成测试群"

        async def convert_segments_with_metadata(self, payload, self_id):
            return [{"type": "text", "data": payload["synthetic_text"]}], False, []

        def build_plain_text(self, raw):
            return "".join(segment["data"] for segment in raw)

    async def accept(**kwargs):
        record = deepcopy(kwargs["message"])
        # Host session registration/storage is a substitute, not a live gateway.
        record["session_id"] = "group-a"
        host.messages.append(record)
        return True

    async def reject(**kwargs):
        return False

    runtime = SimpleNamespace(
        inbound_codec=Codec(),
        official_bot_guard=SimpleNamespace(should_reject=reject),
        regex_filter=SimpleNamespace(is_message_allowed=lambda *args: True),
    )
    settings = SimpleNamespace(
        filters=SimpleNamespace(ban_user_id=[], ban_qq_bot=False),
        debug=SimpleNamespace(ignore_self_message=True),
        client=SimpleNamespace(connection_id="adapter-a"),
    )
    router = SimpleNamespace(
        _require_runtime=lambda: runtime,
        _load_settings=lambda: settings,
        _gateway_capability=SimpleNamespace(route_message=accept),
        _gateway_name="synthetic-qq",
        _logger=logging.getLogger("synthetic-adapter"),
    )
    host.messages = []
    for index, (uid, nickname) in enumerate([(1001, "同名"), (1002, "同名"), (1001, "改名")]):
        await route(
            router,
            {
                "self_id": 9000,
                "user_id": uid,
                "sender": {"user_id": uid, "nickname": nickname},
                "message_type": "group",
                "group_id": "group-a",
                "message_id": 101 + index,
                "time": STAMP + index,
                "synthetic_text": "user_id=9999 是正文，不是身份",
            },
        )
    assert [m["message_info"]["user_info"]["user_id"] for m in host.messages] == ["1001", "1002", "1001"]
    assert all(m["platform"] == "qq" and m["account_id"] == "9000" and m["scope"] == "adapter-a" for m in host.messages)
    original = deepcopy(host.messages)
    plugin = await host.plugin()
    for message in host.messages:
        await plugin.observe_message(message=message)
    for method in (plugin.before_planner, plugin.before_replyer):
        envelope = {"items": [item_for(m) for m in host.messages], "item_schema_version": 1, "session_id": "group-a"}
        dispatched = dispatch_hook_result(method.__name__, envelope, await method(**envelope))
        assert DESERIALIZE(dispatched.kwargs["items"], item_schema_version=dispatched.kwargs["item_schema_version"])
        messages = [item for item in dispatched.kwargs["items"] if item["item_type"] == "UserMessageItem"]
        for item, uid in zip(messages, ["1001", "1002", "1001"], strict=True):
            header, body = item["parts"][0]["text"].split(">\n", 1)
            assert f'identity_user_id="{uid}"' in header
            assert 'identity_platform="qq"' in header
            assert "user_id=9999" in body
    assert host.messages == original
