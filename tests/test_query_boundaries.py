"""Query boundary regressions, including the current Host's actual order contract."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from .conftest import STAMP, SyntheticHost, grant, message
from .host_harness import source_function


async def test_time_query_fixture_matches_unmodified_host_latest_order():
    rows = [message(str(101 + index), timestamp=STAMP + index) for index in range(4)]

    class Column:
        def desc(self):
            return "descending"

    class Query:
        def where(self, *_args):
            return self

        def order_by(self, *terms):
            assert terms == ("descending", "descending")
            return self

        def limit(self, limit):
            self.count = limit
            return self

    @contextmanager
    def database(**_kwargs):
        # Substitute only SQL transport. The Host selects DESC LIMIT and its
        # unchanged find_messages body must reverse those rows itself.
        yield SimpleNamespace(exec=lambda query: SimpleNamespace(all=lambda: list(reversed(rows))[: query.count]))

    actual = source_function(
        "src/common/message_repository.py",
        "find_messages",
        {
            "select": lambda _model: Query(),
            "Messages": SimpleNamespace(id="id"),
            "col": lambda _field: Column(),
            "message_time_expression": lambda _field: Column(),
            "_build_message_conditions": lambda **_kwargs: [],
            "get_db_session": database,
            "_message_to_instance": lambda row: row,
        },
    )(session_id="group-a", limit=3, limit_mode="latest")
    host = SyntheticHost()
    host.messages = rows
    plugin = await host.plugin()
    assert [row["message_id"] for row in actual] == ["102", "103", "104"]
    assert await plugin.engine.in_range("group-a", STAMP, STAMP + 4) == rows
    response = await host.rpc(
        "cap.call",
        "local.stable-identity",
        {
            "capability": "message.get_by_time_in_chat",
            "args": {
                "chat_id": "group-a",
                "start_time": str(STAMP),
                "end_time": str(STAMP + 4),
                "limit": 3,
                "limit_mode": "latest",
            },
        },
    )
    assert response["messages"] == actual
    await plugin.on_unload()


async def test_scan_limit_keeps_newest_matching_message(host):
    host.messages = [
        message(str(101 + index), timestamp=STAMP + index, text="newest-only" if index == 2 else "other")
        for index in range(3)
    ]
    plugin = await host.plugin()
    plugin.engine.settings = replace(plugin.engine.settings, scan_limit=2)
    args, _ = await grant(plugin, query={"keyword": "newest-only"})
    result = await plugin.lookup_original_messages(**args)
    assert result["status"] == "found"
    assert [row["message_id"] for row in result["records"]] == ["103"]
    assert result["incomplete"]  # Older messages really were outside the scan.


async def test_range_query_returns_newest_matches_first(host):
    host.messages = [message(str(101 + index), timestamp=STAMP + index) for index in range(3)]
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"keyword": "我", "limit": 2})
    result = await plugin.lookup_original_messages(**args)
    assert [row["message_id"] for row in result["records"]] == ["103", "102"]
    assert result["incomplete"]


@pytest.mark.parametrize("value", ["", " ", "\t\n", "\u3000"])
async def test_explicit_blank_single_id_never_becomes_range_query(host, value):
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"message_id": value})
    host.calls.clear()
    result = await plugin.lookup_original_messages(**args)
    assert not result["success"] and result["status"] == "invalid_query"
    assert not result.get("records")
    assert not any(cap.startswith("message.") for cap, _ in host.calls)


@pytest.mark.parametrize("batch", [False, True])
async def test_exact_ids_at_result_limit_are_complete(host, batch):
    plugin = await host.plugin()
    query = {"message_ids": ["101"]} if batch else {"message_id": "101"}
    args, _ = await grant(plugin, query={**query, "limit": 1})
    result = await plugin.lookup_original_messages(**args)
    assert len(result["records"]) == 1
    assert not result["incomplete"] and not result["missing_message_ids"]


async def test_full_limit_with_only_nonmatching_remainder_is_complete(host):
    host.messages = [
        message("101", timestamp=STAMP, text="other"),
        message("102", timestamp=STAMP + 1, text="only-match"),
    ]
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"keyword": "only-match", "limit": 1})
    result = await plugin.lookup_original_messages(**args)
    assert [row["message_id"] for row in result["records"]] == ["102"]
    assert not result["incomplete"]


async def test_an_additional_matching_result_is_incomplete(host):
    host.messages = [message("101"), message("102", timestamp=STAMP + 1)]
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"message_ids": ["101", "102"], "limit": 1})
    result = await plugin.lookup_original_messages(**args)
    assert [row["message_id"] for row in result["records"]] == ["101"]
    assert result["incomplete"]


@pytest.mark.parametrize("count,incomplete", [(2, False), (3, True)])
async def test_exact_scan_and_result_limits_distinguish_real_overflow(host, count, incomplete):
    host.messages = [message(str(101 + index), timestamp=STAMP + index) for index in range(count)]
    plugin = await host.plugin()
    plugin.engine.settings = replace(plugin.engine.settings, scan_limit=2)
    args, _ = await grant(plugin, query={"keyword": "我", "limit": 2})
    result = await plugin.lookup_original_messages(**args)
    assert [row["message_id"] for row in result["records"]] == [str(100 + count), str(99 + count)]
    assert result["incomplete"] is incomplete


async def test_nonmatching_sender_after_full_limit_does_not_signal_overflow(host):
    host.messages = [message("101", uid="1002"), message("102", timestamp=STAMP + 1)]
    plugin = await host.plugin()
    args, _ = await grant(plugin, query={"sender_user_id": "1001", "limit": 1})
    result = await plugin.lookup_original_messages(**args)
    assert [row["message_id"] for row in result["records"]] == ["102"]
    assert not result["incomplete"]


async def test_output_budget_omissions_remain_explicit(host):
    host.messages = [message("101", text="合成短文本"), message("102", timestamp=STAMP + 1, text="合成文本" * 1000)]
    plugin = await host.plugin()
    plugin.engine.settings = replace(plugin.engine.settings, max_output_chars=800)
    args, _ = await grant(plugin, query={"keyword": "合成"})
    result = await plugin.lookup_original_messages(**args)
    assert [row["message_id"] for row in result["records"]] == ["101"]
    assert result["incomplete"]
