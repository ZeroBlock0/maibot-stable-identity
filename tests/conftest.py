"""Synthetic data only. Real SDK, in-memory capability transport substitute."""

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import ModuleType

import hashlib
import os
import sys

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOST_ROOT = Path(os.environ.get("MAIBOT_HOST_SOURCE_ROOT", str(PLUGIN_ROOT.parents[1]))).resolve()
SDK_ROOT = Path(os.environ.get("MAIBOT_PLUGIN_SDK_PATH", str(HOST_ROOT.parent / "maibot-plugin-sdk")))
sys.path[:0] = [str(PLUGIN_ROOT.parent), str(SDK_ROOT), str(HOST_ROOT)]
# Tests must also work when GitHub checks out the repository under another name.
package = ModuleType("stable_identity")
package.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault("stable_identity", package)

from maibot_sdk.context import PluginContext  # noqa: E402
from stable_identity.engine import TOOL_NAME, display_id  # noqa: E402
from stable_identity.plugin import StableIdentityPlugin  # noqa: E402


STAMP = datetime(2026, 10, 25, 12, 0, 0, 123456).timestamp()


def message(
    mid="101",
    uid="1001",
    name="小明",
    sid="group-a",
    timestamp=STAMP,
    text="我在杭州工作",
    account="9000",
    scope="adapter-a",
    quotes=(),
):
    return {
        "message_id": mid,
        "timestamp": str(timestamp),
        "platform": "qq",
        "account_id": account,
        "scope": scope,
        "session_id": sid,
        "is_notify": False,
        "message_info": {
            "user_info": {"user_id": uid, "user_nickname": name, "user_cardname": ""},
            "group_info": {"group_id": sid, "group_name": "合成测试群"},
        },
        "raw_message": [
            {
                "type": "reply",
                "data": {
                    "target_message_id": q,
                    "target_message_content": "仅测试用引用",
                    "target_message_sender_id": "8888",
                },
            }
            for q in quotes
        ]
        + [{"type": "text", "data": text}],
    }


def session(sid="group-a", account="9000", scope="adapter-a"):
    return {
        "session_id": sid,
        "platform": "qq",
        "account_id": account,
        "scope": scope,
        "group_id": sid,
        "user_id": None,
    }


def item_for(msg, *, include_session=False, text=None):
    from html import escape

    user = msg["message_info"]["user_info"]
    attrs = {"msg_id": display_id(msg["message_id"])}
    if include_session:
        attrs.update(session_id=msg["session_id"], chat_name="合成测试群")
    quote_ids = [part["data"]["target_message_id"] for part in msg["raw_message"] if part["type"] == "reply"]
    if quote_ids:
        attrs["quote"] = ",".join(dict.fromkeys(display_id(q) for q in quote_ids))
    stamp = datetime.fromtimestamp(float(msg["timestamp"]))
    attrs.update(time=stamp.strftime("%H:%M:%S"), user=user["user_nickname"] or user["user_id"])
    if user["user_cardname"]:
        attrs["group_card"] = user["user_cardname"]
    prefix = "<message " + " ".join(f'{k}="{escape(v, quote=True)}"' for k, v in attrs.items()) + ">\n"
    body = "".join(p["data"] for p in msg["raw_message"] if p["type"] == "text")
    return {
        "item_type": "UserMessageItem",
        "meta": {"item_id": "item:" + msg["message_id"], "logical_turn_id": None, "timestamp": stamp.isoformat()},
        "parts": [{"type": "text", "text": prefix + (text if text is not None else body)}],
    }


def system_item():
    return {
        "item_type": "SystemMessageItem",
        "meta": {"item_id": "system", "logical_turn_id": None, "timestamp": datetime.fromtimestamp(STAMP).isoformat()},
        "parts": [{"type": "text", "text": "合成人设：自然聊天，保留原有风格。"}],
    }


def tool_defs(plugin):
    defs = []
    for component in plugin.get_components():
        if component["type"] == "TOOL":
            defs.append(
                {
                    "type": "function",
                    "function": {
                        "name": component["name"],
                        "description": component["metadata"]["description"],
                        "parameters": deepcopy(component["metadata"]["parameters_raw"]),
                    },
                }
            )
    defs.append(
        {
            "type": "function",
            "function": {
                "name": "query_memory",
                "parameters": {"type": "object", "properties": {"person_name": {"type": "string"}}},
            },
        }
    )
    return defs


class SyntheticHost:
    def __init__(self):
        self.messages = [message()]
        self.sessions = [session(), session("group-b", "9001", "adapter-b")]
        self.calls = []
        self.fail = set()
        self.active = 0
        self.peak = 0
        self.delay = 0

    async def rpc(self, method, plugin_id, payload, **kwargs):
        import asyncio

        assert method == "cap.call"
        capability, args = payload["capability"], payload["args"]
        self.calls.append((capability, deepcopy(args)))
        if capability in self.fail:
            raise RuntimeError("synthetic_rpc_failure")
        if capability == "database.get":
            if args["model_name"] == "ChatSession":
                rows = self.sessions
            elif args["model_name"] == "Messages":
                rows = [{**m, "user_id": m["message_info"]["user_info"]["user_id"]} for m in self.messages]
            elif args["model_name"] == "BotPlatformAccount":
                rows = [{"platform": "qq", "account_id": "9000"}, {"platform": "qq", "account_id": "9001"}]
            else:
                raise AssertionError("unexpected model")
            rows = [m for m in rows if all(m.get(k) == v for k, v in args["filters"].items())]
            return {"success": True, "result": deepcopy(rows[: args["limit"]])}
        if capability == "message.get_by_time_in_chat":
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                rows = sorted(
                    [
                        m
                        for m in self.messages
                        if m["session_id"] == args["chat_id"]
                        and float(args["start_time"]) <= float(m["timestamp"]) <= float(args["end_time"])
                    ],
                    key=lambda m: float(m["timestamp"]),
                )
                # Host find_messages returns chronological order even when it
                # selects the latest rows. Stable ties follow insertion/row ID.
                limit = args["limit"]
                if limit > 0:
                    rows = rows[:limit] if args.get("limit_mode") == "earliest" else rows[-limit:]
                return {"success": True, "messages": deepcopy(rows)}
            finally:
                self.active -= 1
        if capability == "message.get_by_id":
            rows = [
                m for m in self.messages if m["session_id"] == args["chat_id"] and m["message_id"] == args["message_id"]
            ]
            return {"success": True, "message": deepcopy(rows[0] if rows else None)}
        if capability == "person.get_id":
            return {
                "success": True,
                "person_id": hashlib.md5(f"{args['platform']}_{args['user_id']}".encode()).hexdigest(),
            }
        if capability == "knowledge.search":
            return {"success": True, "content": "你知道这些知识: 1. 合成长期记忆：喜欢散步。"}
        raise AssertionError("unexpected capability " + capability)

    async def plugin(self):
        instance = StableIdentityPlugin()
        instance._set_context(PluginContext(plugin_id="local.stable-identity", rpc_call=self.rpc))
        instance.set_plugin_config({})
        await instance.on_load()
        return instance


@pytest.fixture
def host():
    return SyntheticHost()


async def grant(plugin, *, sid="group-a", query=None, tool=TOOL_NAME, items=None):
    before = await plugin.before_planner(
        items=items or [system_item(), item_for(message())],
        item_schema_version=1,
        session_id=sid,
        tool_definitions=tool_defs(plugin),
    )
    definitions = before["modified_kwargs"]["tool_definitions"]
    function = next(d["function"] for d in definitions if d["function"]["name"] == tool)
    ref = function["parameters"]["properties"]["context_ref"]["enum"][0]
    call = {
        "item_type": "FunctionCallItem",
        "meta": {
            "item_id": "call-item",
            "logical_turn_id": "turn",
            "timestamp": datetime.fromtimestamp(STAMP).isoformat(),
        },
        "tool_call": {
            "call_id": "call-a",
            "func_name": tool,
            "args": {"context_ref": ref, **(query or {"message_id": "101"})},
            "extra_content": None,
        },
    }
    after = await plugin.after_planner(output_items=[call], item_schema_version=1, session_id=sid)
    return after["modified_kwargs"]["output_items"][0]["tool_call"]["args"], before
