"""Offline, read-only export replay; prints aggregates, never private payloads.

Usage: python -m tests.replay_export /path/to/maibot-data.zip
No archive plugin code is executed. The temporary database copies are deleted.
"""

from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import argparse
import ast
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time

import msgpack


def load_messages(connection):
    from .conftest import HOST_ROOT
    from .host_harness import source_function

    model_path = HOST_ROOT / "src/common/data_models/message_component_data_model.py"
    tree = ast.parse(model_path.read_text(encoding="utf-8-sig"))
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *[n for n in tree.body if isinstance(n, ast.ClassDef)],
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "ABC": ABC,
        "abstractmethod": abstractmethod,
        "deepcopy": deepcopy,
        "hashlib": hashlib,
        "logger": logging.getLogger("offline-components"),
    }
    exec(compile(module, str(model_path), "exec"), namespace)
    utils = SimpleNamespace()
    namespace["PluginMessageUtils"] = utils
    for name in ("_message_sequence_to_dict", "_component_to_dict", "_forward_component_to_dict"):
        setattr(
            utils,
            name,
            source_function("src/plugin_runtime/host/message_utils.py", name, namespace, "PluginMessageUtils"),
        )
    result = []
    for row in connection.execute("SELECT * FROM mai_messages ORDER BY timestamp, id"):
        source = dict(row)
        message = {k: source[k] for k in ("session_id", "platform", "account_id", "scope", "message_id", "is_notify")}
        message.update(
            timestamp=str(datetime.fromisoformat(source["timestamp"]).timestamp()),
            message_info={
                "user_info": {k: source[k] for k in ("user_id", "user_nickname", "user_cardname")},
                "group_info": {"group_id": source["group_id"], "group_name": source["group_name"]}
                if source["group_id"]
                else None,
            },
            raw_message=utils._message_sequence_to_dict(
                namespace["MessageSequence"].from_dict(msgpack.unpackb(source["raw_content"], raw=False)),
                include_binary_data=False,
            ),
        )
        result.append((source, message))
    return result


async def replay(connection):
    from .conftest import item_for, grant, system_item
    from .test_memory_bridge import MemoryHost, notes
    from .host_harness import BUILD_PREFIX, DESERIALIZE, dispatch_hook_result

    host = MemoryHost()
    host.sessions = [dict(r) for r in connection.execute("SELECT * FROM chat_sessions")]
    host.people = [dict(r) for r in connection.execute("SELECT * FROM person_info")]
    host.records = [dict(r) for r in connection.execute("SELECT * FROM tool_records")]
    accounts = [dict(r) for r in connection.execute("SELECT * FROM bot_platform_accounts")]
    original_rpc = host.rpc

    async def rpc(method, plugin_id, payload, **kwargs):
        args = payload["args"]
        if payload["capability"] == "database.get" and args["model_name"] == "BotPlatformAccount":
            rows = [deepcopy(r) for r in accounts if all(r.get(k) == v for k, v in args["filters"].items())]
            return {"success": True, "result": rows[: args["limit"]]}
        return await original_rpc(method, plugin_id, payload, **kwargs)

    host.rpc = rpc
    loaded = load_messages(connection)
    host.messages = [m for _, m in loaded]
    grouped = defaultdict(list)
    for source, message in loaded:
        if message["is_notify"]:
            continue
        item = item_for(message, text=source["processed_plain_text"] or "")
        user = message["message_info"]["user_info"]
        quotes = [p["data"]["target_message_id"] for p in message["raw_message"] if p.get("type") == "reply"]
        prefix = BUILD_PREFIX(
            timestamp=datetime.fromtimestamp(float(message["timestamp"])),
            user_name=user["user_nickname"] or user["user_id"],
            group_card=user["user_cardname"] or "",
            message_id=message["message_id"],
            quote_ids=quotes,
        )
        item["parts"][0]["text"] = prefix + (source["processed_plain_text"] or "")
        grouped[message["session_id"]].append((message, item))
    plugin = await host.plugin()
    report = {
        "verification_kind": "offline_export_record_replay",
        "live_host": False,
        "live_model": False,
        "source_records": len(loaded),
        "stages": {},
        "native_record_statuses": {},
    }
    try:
        for stage, method in (("Planner", plugin.before_planner), ("Replyer", plugin.before_replyer)):
            counts = Counter()
            for sid, entries in grouped.items():
                for offset in range(0, len(entries), 100):
                    batch = entries[offset : offset + 100]
                    original = {
                        "items": [system_item(), *[i for _, i in batch]],
                        "item_schema_version": 1,
                        "session_id": sid,
                    }
                    result = dispatch_hook_result(stage, original, await method(**original))
                    assert DESERIALIZE(
                        result.kwargs["items"], item_schema_version=result.kwargs.get("item_schema_version")
                    )
                    actual = [i for i in result.kwargs["items"] if i["item_type"] == "UserMessageItem"]
                    for (message, before), after in zip(batch, actual, strict=True):
                        header = after["parts"][0]["text"].splitlines()[0]
                        verified = 'sender_id="' in header
                        counts["verified" if verified else "unknown"] += 1
                        if verified:
                            from stable_identity.engine import sender_key
                            expected = sender_key(message["platform"], message["message_info"]["user_info"]["user_id"])
                            assert f'sender_id="{expected}"' in header
                        assert before["parts"][0]["text"].split(">", 1)[1] == after["parts"][0]["text"].split(">", 1)[1]
                    counts["protocol_accepted_batches"] += 1
            report["stages"][stage] = dict(counts)
        statuses = Counter()
        for row in host.records:
            if row["tool_name"] not in {"query_memory", "query_person_profile"}:
                continue
            data = json.loads(row["tool_data"])
            meta = {"item_id": "offline-record", "logical_turn_id": "offline-turn", "timestamp": row["timestamp"]}
            native = [
                {
                    "item_type": "FunctionCallItem",
                    "meta": {**meta, "item_id": "offline-call"},
                    "tool_call": {
                        "call_id": row["tool_id"],
                        "func_name": row["tool_name"],
                        "args": data.get("arguments", {}),
                        "extra_content": None,
                    },
                },
                {
                    "item_type": "FunctionCallOutputItem",
                    "meta": meta,
                    "call_id": row["tool_id"],
                    "tool_name": row["tool_name"],
                    "success": data.get("success", False),
                    "output": data.get("history_content", ""),
                },
            ]
            original = {"items": [system_item(), *native], "item_schema_version": 1, "session_id": row["session_id"]}
            result = dispatch_hook_result("Planner", original, await plugin.before_planner(**original))
            assert DESERIALIZE(result.kwargs["items"], item_schema_version=result.kwargs.get("item_schema_version"))
            assert result.kwargs["items"][-2:] == native
            statuses[row["tool_name"] + ":" + notes(result.kwargs["items"])[0]["status"]] += 1
        report["native_record_statuses"] = dict(statuses)
        lookups = 0
        for sid, entries in grouped.items():
            message, item = entries[0]
            user = message["message_info"]["user_info"]
            person_id = hashlib.md5(f"{message['platform']}_{user['user_id']}".encode()).hexdigest()
            args, _ = await grant(
                plugin, sid=sid, items=[item], query={"person_id": person_id, "message_ids": [message["message_id"]]}
            )
            result = await plugin.lookup_original_messages(**args)
            assert len(result["records"]) == 1 and result["records"][0]["user_id"] == user["user_id"]
            lookups += 1
        report["scoped_person_id_batch_lookups"] = lookups
        return report
    finally:
        await plugin.on_unload()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    archive_path = args.archive.expanduser().resolve()
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    with tempfile.TemporaryDirectory(prefix="identity-offline-replay-", dir="/tmp") as temporary:
        directory = Path(temporary)
        os.environ["MAIBOT_INSTANCE_DIR"] = str(directory / "instance")
        # Host logger.py creates logs/ relative to cwd on import. Import Host
        # source helpers only after entering the private temporary directory.
        os.chdir(directory)
        with ZipFile(archive_path) as archive:
            for name in ("MaiBot.db", "MaiBot.db-wal", "MaiBot.db-shm"):
                entry = "data/" + name
                if entry in archive.namelist():
                    (directory / name).write_bytes(archive.read(entry))
        connection = sqlite3.connect((directory / "MaiBot.db").as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        try:
            report = asyncio.run(replay(connection))
        finally:
            connection.close()
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
