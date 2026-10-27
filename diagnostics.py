"""Rate-limited activity logs; never include IDs, message text or credentials."""

from typing import Any, Callable, Dict, List

import json
import time

from .engine import ATTR_RE, parse_header


class ActivityReporter:
    def __init__(self, logger: Any, clock: Callable[[], float] = time.monotonic, interval: float = 60):
        self.logger, self.clock, self.interval = logger, clock, interval
        self.last: Dict[tuple, float] = {}
        self.requests: Dict[str, int] = {}
        self.inbound_count = 0
        self.timings: Dict[str, Dict[str, Any]] = {}
        self.last_annotations: Dict[str, Dict[str, int]] = {}

    def snapshot(self) -> Dict[str, Any]:
        return {
            "inbound_count": self.inbound_count,
            "requests": dict(self.requests),
            "timings": {stage: dict(values) for stage, values in self.timings.items()},
            "last_annotations": {stage: dict(values) for stage, values in self.last_annotations.items()},
        }

    def timing(self, stage: str, elapsed_ms: float, phases: Dict[str, float], budget_ms: float) -> None:
        elapsed_ms = round(elapsed_ms, 2)
        previous = self.timings.get(stage, {})
        values = {
            "calls": previous.get("calls", 0) + 1,
            "last_ms": elapsed_ms,
            "max_ms": max(elapsed_ms, previous.get("max_ms", 0)),
            "budget_ms": budget_ms,
            "over_budget": previous.get("over_budget", 0) + int(elapsed_ms > budget_ms),
            **{key: round(value, 2) for key, value in phases.items()},
        }
        self.timings[stage] = values
        category = "over_budget" if elapsed_ms > budget_ms else "normal"
        if self.due(stage, "timing_" + category):
            log = self.logger.warning if category == "over_budget" else self.logger.info
            log("身份增强 %s 阶段耗时: %s；不含Host排队和RPC序列化", stage, json.dumps(values, sort_keys=True))

    def due(self, stage: str, category: str) -> bool:
        key = (stage, category)
        now = self.clock()
        if key in self.last and now - self.last[key] < self.interval:
            return False
        self.last[key] = now
        return True

    def inbound(self) -> None:
        self.inbound_count += 1
        if self.due("inbound", "normal"):
            self.logger.info("身份增强 入站 Hook 已调用: 本次加载累计接收=%d", self.inbound_count)

    def skip(self, stage: str, reason: str) -> None:
        if self.due(stage, "skipped"):
            self.logger.warning("身份增强 %s Hook 已跳过: 原因=%s；保留当前请求", stage, reason)

    def report(self, stage: str, items: List[Dict[str, Any]], stats: Dict[str, int]) -> Dict[str, int]:
        counts = {
            "messages": 0,
            "verified": 0,
            "unknown": 0,
            "person_linked": 0,
            "target_verified": 0,
            "target_unknown": 0,
        }
        linked_senders = set()
        for item in items:
            parts = item.get("parts", [])
            if item.get("item_type") == "UserMessageItem" and parts and parts[0].get("type") == "text":
                parsed = parse_header(parts[0].get("text", ""))
                if parsed is not None:
                    prefix = parts[0]["text"][: parsed[1]]
                    counts["messages"] += 1
                    attrs = dict(ATTR_RE.findall(prefix))
                    key = "verified" if attrs.get("sender_id") else "unknown"
                    counts[key] += 1
                    if attrs.get("person_id"):
                        linked_senders.add(attrs["sender_id"])
                    counts["person_linked"] += int(attrs.get("sender_id") in linked_senders)
            if item.get("item_type") == "SystemMessageItem":
                for part in parts:
                    text = part.get("text", "")
                    if part.get("type") == "text" and text.startswith("[stable_identity.target:v1] "):
                        try:
                            target = json.loads(text.split(" ", 1)[1])
                            counts[
                                "target_verified" if target.get("identity_status") == "verified" else "target_unknown"
                            ] += 1
                        except (ValueError, AttributeError):
                            counts["target_unknown"] += 1
        self.requests[stage] = self.requests.get(stage, 0) + 1
        self.last_annotations[stage] = dict(counts)
        category = (
            "unknown"
            if counts["unknown"] or counts["target_unknown"]
            else ("normal" if counts["messages"] else "no_boundary")
        )
        if self.due(stage, category):
            log = self.logger.warning if category == "unknown" else self.logger.info
            # Stats keys are local diagnostic codes; no exception messages/user data.
            reasons = {
                key: value
                for key, value in stats.items()
                if key
                in {
                    "ambiguous_id",
                    "record_missing",
                    "metadata_mismatch",
                    "scope_mismatch",
                    "lookup_truncated",
                    "lookup_error",
                    "unknown_source",
                    "annotation_timeout",
                    "background_lookup_failed",
                }
            }
            log(
                "身份增强 %s 请求标注完成: 本次消息=%d 已核验=%d 未知=%d 人物关联=%d 目标已核验=%d 目标未知=%d 累计请求=%d 原因累计=%s",
                stage,
                counts["messages"],
                counts["verified"],
                counts["unknown"],
                counts["person_linked"],
                counts["target_verified"],
                counts["target_unknown"],
                self.requests[stage],
                json.dumps(reasons, sort_keys=True),
            )
        return counts

    def lookup(self, result: Dict[str, Any]) -> None:
        status = result.get("status", "unknown")
        # Restrict grouping to bounded result categories, never any query argument.
        category = (
            status
            if status
            in {
                "found",
                "not_found",
                "incomplete",
                "ambiguous_id",
                "unknown_alias",
                "unauthorized",
                "query_failed",
                "invalid_query",
                "disabled",
                "unknown_person",
            }
            else "unknown"
        )
        if self.due("lookup", category):
            log = self.logger.info if result.get("success") else self.logger.warning
            self.logger.debug("原话查证诊断状态类别=%s", category)
            log(
                "身份增强 原话查证: 状态=%s 证据条数=%d 查询不完整=%s",
                category,
                len(result.get("records", [])),
                bool(result.get("incomplete", False)),
            )

    def memory(self, stage: str, counts: Dict[str, int]) -> None:
        if not counts:
            return
        # Native ordinary replies also carry reference text. Keep missing-link
        # diagnostics distinct from an actual memory verification result.
        if counts == {"reference_missing": 1}:
            if self.due("memory_" + stage, "reference_missing"):
                self.logger.info(
                    "身份增强 %s 记忆引用诊断: reference_missing（仅日志，不追加模型提示）；不代表原生记忆失败",
                    stage,
                )
            return
        known = {"structured_checked", "source_identity_verified", "profile_subject_mapped", "reference_missing"}
        incomplete = any(key not in known for key in counts)
        if self.due("memory_" + stage, "incomplete" if incomplete else "normal"):
            log = self.logger.warning if incomplete else self.logger.info
            log(
                "身份增强 %s 记忆来源核验: 状态计数=%s；只核验来源身份，不证明事实真假",
                stage,
                json.dumps(counts, sort_keys=True),
            )
