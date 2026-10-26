"""MaiBot SDK 2.16.0 entry point. All Host access goes through self.ctx."""

from typing import Any, Dict, List, Optional, Tuple

import asyncio
import time

from maibot_sdk import API, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .diagnostics import ActivityReporter
from .engine import IdentityEngine, SCHEMA_VERSION, Settings, TOOL_NAME
from .memory_bridge import MemoryBridge


VERSION = "0.2.5"
REQUEST_BUDGET_SECONDS = 4.0


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="启用身份核验及原话查证")
    config_version: str = Field(default="0.1.0", description="配置版本")


class Limits(PluginConfigBase):
    cache_capacity: int = Field(default=4096, ge=64, le=16384, description="核验缓存容量")
    cache_ttl_seconds: float = Field(default=600, ge=1, le=3600, description="缓存有效期")
    concurrency: int = Field(default=8, ge=1, le=16, description="回查并发数")
    scan_limit: int = Field(default=1000, ge=10, le=2000, description="单次最多扫描消息数")
    result_limit: int = Field(default=20, ge=1, le=50, description="最多返回证据条数")


class IdentityConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    limits: Limits = Field(default_factory=Limits)


TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "context_ref": {"type": "string", "description": "填写本次请求工具定义给出的唯一上下文引用。"},
        "sender_user_id": {"type": "string", "description": "真实用户 ID；昵称不能代替 ID。"},
        "person_id": {
            "type": "string",
            "description": "宿主稳定人物 ID，反查当前平台账号；不能填昵称，与 sender_user_id 同填时必须一致。",
        },
        "keyword": {"type": "string", "description": "在本人原始文本中精确搜索，不搜索引用和摘要。"},
        "start_time": {"type": "string", "description": "起始时间，带时区 ISO 8601 或 Unix 秒。省略时最近七天。"},
        "end_time": {"type": "string", "description": "结束时间，带时区 ISO 8601 或 Unix 秒。"},
        "message_id": {
            "type": "string",
            "description": "原始 ID或本次上下文的 msg_id；不能填空白，冲突和未知映射不猜测。按 ID默认不限时间。",
        },
        "message_ids": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 12,
            "description": "批量核实记忆来源消息 ID，最多12个；不能与 message_id 同填，仍限当前会话。",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "返回上限，默认 20，仍受插件配置限制。",
        },
        "_identity_ticket": {"type": "string", "description": "插件内部执行凭据，由 Hook 注入；模型省略。"},
    },
    "required": ["context_ref"],
    "additionalProperties": False,
}


class StableIdentityPlugin(MaiBotPlugin):
    config_model = IdentityConfig

    def __init__(self) -> None:
        super().__init__()
        self.engine = None
        self.diagnostics = None
        self.memory = None

    async def on_load(self) -> None:
        limits = self.config.limits
        self.engine = IdentityEngine(
            self.ctx,
            Settings(
                capacity=limits.cache_capacity,
                ttl=limits.cache_ttl_seconds,
                concurrency=limits.concurrency,
                scan_limit=limits.scan_limit,
                result_limit=limits.result_limit,
            ),
        )
        self.diagnostics = ActivityReporter(self.ctx.logger)
        self.memory = MemoryBridge(self.engine)
        self.ctx.logger.info(
            "稳定身份插件已加载 v0.2.5；请求增强共用4秒预算；支持只读status及阶段耗时诊断。INFO摘要每阶段首次及每60秒记录。复古/压缩部分支持，Focus共享查证拒绝执行。"
        )

    async def on_unload(self) -> None:
        if self.engine is not None:
            self.memory.close()
            self.memory = None
            self.engine.close()
            self.engine = None
            self.diagnostics = None

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        del scope, config_data, version
        await self.on_unload()
        await self.on_load()

    def active(self) -> bool:
        return self.engine is not None and self.config.plugin.enabled

    @API("status", description="只读查看身份增强计数与耗时，不返回账号、正文或凭据", version="1", public=True)
    async def status(self) -> Dict[str, Any]:
        return {
            "version": VERSION,
            "enabled": self.active(),
            "request_budget_ms": int(REQUEST_BUDGET_SECONDS * 1000),
            "activity": self.diagnostics.snapshot() if self.diagnostics is not None else {},
        }

    async def _enhance(
        self,
        stage: str,
        items: List[Dict[str, Any]],
        session_id: str,
        *,
        reply_message_id: str = "",
        reply_args: Optional[Dict[str, Any]] = None,
        definitions: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]]:
        # Hold one load generation across awaits. Reload/unload must not publish
        # stale annotations, memory references or tickets into the next load.
        engine, memory, diagnostics = self.engine, self.memory, self.diagnostics
        started = time.monotonic()
        deadline = started + REQUEST_BUDGET_SECONDS
        phases = {}
        try:
            phase = time.monotonic()
            output, aliases = await engine.annotate(items, session_id, budget=max(0, deadline - phase))
            phases["identity_ms"] = (time.monotonic() - phase) * 1000
            if reply_message_id and not any(
                item.get("item_type") == "UserMessageItem"
                and item.get("parts")
                and item["parts"][0].get("text", "").startswith("<message ")
                for item in output
            ):
                phase = time.monotonic()
                await engine.annotate_reply_target(
                    output, session_id, reply_message_id, budget=max(0, deadline - phase)
                )
                phases["target_ms"] = (time.monotonic() - phase) * 1000
            phase = time.monotonic()
            memory_stats = await memory.enrich(
                output, session_id, reply_args, budget=max(0, min(0.6, deadline - phase))
            )
            phases["memory_ms"] = (time.monotonic() - phase) * 1000
            if self.engine is not engine or not self.active():
                return None
            diagnostics.memory(stage, memory_stats)
            diagnostics.report(stage, output, engine.stats)
            bound = engine.bind_definitions(definitions, session_id, aliases) if definitions is not None else None
            return output, bound
        except asyncio.CancelledError:
            if self.engine is not engine or engine.closed:
                return None
            raise
        finally:
            diagnostics.timing(stage, (time.monotonic() - started) * 1000, phases, REQUEST_BUDGET_SECONDS * 1000)

    @HookHandler(
        "chat.receive.before_process",
        name="identity_observe",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def observe_message(self, message=None, **kwargs):
        del kwargs
        if self.active():
            try:
                self.engine.observe(message)
                self.diagnostics.inbound()
            except Exception as exc:
                self.ctx.logger.warning("入站身份元数据无效: code=%s", type(exc).__name__)
        return {"action": "continue"}

    @HookHandler(
        "maisaka.planner.before_request",
        name="identity_planner",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def before_planner(self, items=None, item_schema_version=0, session_id="", tool_definitions=None, **kwargs):
        if not self.active() or item_schema_version != SCHEMA_VERSION or not isinstance(items, list):
            if self.active():
                self.diagnostics.skip("Planner", "不支持的item_schema_version或items类型")
            return {"action": "continue"}
        try:
            result = await self._enhance("Planner", items, session_id, definitions=tool_definitions or [])
            if result is None:
                return {"action": "continue"}
            output, definitions = result
            # Host REPLACES kwargs rather than merging. Preserve the entire
            # original envelope, including version and fields for other plugins.
            return {
                "action": "continue",
                "modified_kwargs": {
                    **kwargs,
                    "items": output,
                    "item_schema_version": item_schema_version,
                    "session_id": session_id,
                    "tool_definitions": definitions,
                },
            }
        except Exception as exc:
            self.ctx.logger.warning("Planner 身份标注失败，保留原请求: code=%s", type(exc).__name__)
            return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="identity_replyer",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=5500,
        error_policy=ErrorPolicy.SKIP,
    )
    async def before_replyer(self, items=None, item_schema_version=0, session_id="", reply_message_id="", **kwargs):
        if not self.active() or item_schema_version != SCHEMA_VERSION or not isinstance(items, list):
            if self.active():
                self.diagnostics.skip("Replyer", "不支持的item_schema_version或items类型")
            return {"action": "continue"}
        try:
            result = await self._enhance(
                "Replyer", items, session_id, reply_message_id=reply_message_id, reply_args=kwargs.get("reply_tool_args")
            )
            if result is None:
                return {"action": "continue"}
            output, _ = result
            return {
                "action": "continue",
                "modified_kwargs": {
                    **kwargs,
                    "items": output,
                    "item_schema_version": item_schema_version,
                    "session_id": session_id,
                    "reply_message_id": reply_message_id,
                },
            }
        except Exception as exc:
            self.ctx.logger.warning("Replyer 身份标注失败，保留原请求: code=%s", type(exc).__name__)
            return {"action": "continue"}

    @HookHandler(
        "maisaka.planner.after_response",
        name="identity_authorize_lookup",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def after_planner(self, output_items=None, item_schema_version=0, session_id="", **kwargs):
        if not self.active() or item_schema_version != SCHEMA_VERSION or not isinstance(output_items, list):
            if self.active():
                self.diagnostics.skip("Planner输出", "不支持的item_schema_version或output_items类型")
            return {"action": "continue"}
        started = time.monotonic()
        output = self.engine.authorize_calls(output_items, session_id)
        self.diagnostics.timing("Planner输出", (time.monotonic() - started) * 1000, {}, 1000)
        if output is output_items:
            return {"action": "continue"}
        return {
            "action": "continue",
            "modified_kwargs": {
                **kwargs,
                "output_items": output,
                "item_schema_version": item_schema_version,
                "session_id": session_id,
            },
        }

    @Tool(
        TOOL_NAME,
        description="在当前单会话查证谁说过什么。返回原始账号、昵称、时间、消息 ID与本人原文；不使用记忆摘要，不能跨会话查询。Focus 共享查证暂不支持。",
        parameters=TOOL_SCHEMA,
        visibility="visible",
        timeout_ms=15000,
    )
    async def lookup_original_messages(self, _identity_ticket="", **kwargs):
        del kwargs
        if not self.active():
            return IdentityEngine.result(False, "disabled", "身份查证插件未启用。")
        engine, diagnostics = self.engine, self.diagnostics
        try:
            result = await engine.lookup(_identity_ticket)
        except asyncio.CancelledError:
            if self.engine is engine and not engine.closed:
                raise
            result = IdentityEngine.result(False, "unauthorized", "插件已重载或停用，请使用新的请求上下文。")
        if self.engine is not engine or not self.active():
            return IdentityEngine.result(False, "unauthorized", "插件已重载或停用，请使用新的请求上下文。")
        if diagnostics is not None:
            diagnostics.lookup(result)
        return result


def create_plugin() -> StableIdentityPlugin:
    return StableIdentityPlugin()
