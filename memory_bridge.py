"""Read-only native tool provenance through the documented database capability.

Native calls/results remain byte-for-byte intact. Only independent System parts
are added. Missing structured records never authenticate a summary or a name.
"""

from collections import Counter
from typing import Any, Dict, List, Optional

import asyncio
import hashlib
import hmac
import json
import re
import secrets

from .engine import BoundedTTL, IdentityEngine, metadata, same_route


MARKER = "[stable_identity.memory:v1] "
REF_RE = re.compile(r"\[stable_identity\.memory_ref:v1:([a-f0-9]{32})\]")
NATIVE_TOOLS = {"query_memory", "query_person_profile"}
RULE = (
    "[stable_identity.memory_rules:v1] 记忆核验说明只认证记录的主体映射和原始发言来源身份，不认证事实真假。"
    "画像查询优先使用已核验的 identity_person_id，不按同名推断。原生记忆可能退回其他人的候选；"
    "人物查询目标不等于每条命中的实际主体。摘要、画像和机器人回复都不是用户原话证据。"
    "将检索材料用于 reply_reference 时，保留该条核验说明给出的 [stable_identity.memory_ref:v1:...] 引用，"
    "Replyer 才能重新回查同一工具记录；引用丢失、过期或 structured_omitted 时归属仍未知。"
    "subject_unknown 的 reason 说明缺失层；missing_subject_ids 常见于未保存人物归属的群聊摘要，"
    "不能把它认作当前提问者的经历。reference_missing 只表示本次补充层未核验记忆来源，"
    "不表示没有调用原生记忆工具；请区分已回查的原话与未核验的摘要。"
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class MemoryBridge:
    def __init__(self, engine: IdentityEngine):
        self.engine, self.ctx = engine, engine.ctx
        self.refs = BoundedTTL(256, 120)
        self.secret = secrets.token_bytes(32)
        self.semaphore = asyncio.Semaphore(4)

    def close(self) -> None:
        self.refs.clear()

    async def enrich(
        self,
        items: List[Dict[str, Any]],
        sid: str,
        reply_args: Optional[Dict[str, Any]] = None,
        *,
        budget: float = 0.7,
    ) -> Dict[str, int]:
        """A separate 700 ms budget prevents memory diagnostics delaying chat."""
        for item in items:
            if item.get("item_type") == "SystemMessageItem":
                item["parts"] = [
                    part
                    for part in item.get("parts", [])
                    if not (part.get("type") == "text" and part.get("text", "").startswith(MARKER))
                ]
        candidates = [
            item
            for item in items
            if item.get("item_type") == "FunctionCallOutputItem" and item.get("tool_name") in NATIVE_TOOLS
        ]
        reference_text = (reply_args if isinstance(reply_args, dict) else {}).get("reply_reference", "")
        tokens = list(dict.fromkeys(REF_RE.findall(reference_text[:32000]))) if isinstance(reference_text, str) else []
        # Ordinary Replyer drops native tool results. A missing explicit link
        # must be visible even after a successful Planner inspection; never
        # attach another concurrent request's last result by session ID.
        missing_reference = isinstance(reply_args, dict) and not candidates and not tokens
        if not candidates and not tokens and not missing_reference:
            return {}
        stats: Counter = Counter()
        notes: List[Dict[str, Any]] = []
        if not sid:
            notes.append({"status": "unknown_scope"})
        elif missing_reference:
            notes.append(
                {
                    "status": "reference_missing",
                    "reason": "no_verification_reference_in_reply_reference",
                    "evidence_status": "not_checked",
                    "semantic_status": "not_evaluated",
                }
            )
        else:

            async def run() -> None:
                # Never reuse a previous route after an account/connection change.
                session = dict(await self.engine.session(sid, fresh=True))
                accounts = await self.ctx.db.get(
                    "BotPlatformAccount", filters={"platform": session["platform"]}, limit=129
                )
                if not isinstance(accounts, list) or len(accounts) > 128:
                    raise ValueError("unknown_bot_accounts")
                session["_identity_bot_ids"] = {row["account_id"] for row in accounts}
                if session.get("account_id"):
                    session["_identity_bot_ids"].add(session["account_id"])

                async def inspect(item: Dict[str, Any]) -> None:
                    async with self.semaphore:
                        try:
                            note = await self.inspect_output(item, sid, session)
                        except Exception:
                            note = {"call_id": item.get("call_id", ""), "status": "lookup_error"}
                        notes.append(note)

                async def relay(token: str) -> None:
                    ref = self.refs.get(token)
                    if not ref or ref["sid"] != sid or ref["route"] != self.route(session):
                        notes.append({"status": "reference_unavailable"})
                        return
                    async with self.semaphore:
                        try:
                            note = await self.inspect_record(
                                sid, ref["call_id"], ref["tool_name"], ref["digest"], session
                            )
                        except Exception:
                            note = {"status": "lookup_error"}
                        notes.append(note)

                await asyncio.gather(
                    *(inspect(item) for item in candidates[-8:]), *(relay(token) for token in tokens[:8])
                )

            try:
                if budget <= 0:
                    raise TimeoutError
                async with asyncio.timeout(min(0.7, budget)):
                    await run()
            except TimeoutError:
                notes.append({"status": "budget_exhausted"})
            except Exception:
                notes.append({"status": "lookup_error"})
            if len(candidates) > 8 or len(tokens) > 8:
                notes.append({"status": "truncated"})
        system = next((i for i in items if i.get("item_type") == "SystemMessageItem"), None)
        for note in notes:
            stats[note["status"]] += 1
            for hit in note.get("hits", []):
                stats[hit["status"]] += 1
                if hit.get("reason"):
                    stats["reason_" + hit["reason"]] += 1
                if hit.get("matches_query_subject") is False:
                    stats["query_subject_mismatch"] += 1
        if system is not None and notes:
            if not any(p.get("text") == RULE for p in system["parts"]):
                system["parts"].append({"type": "text", "text": RULE})
            encoded = json.dumps(sorted(notes, key=lambda n: (n.get("call_id", ""), n["status"])), ensure_ascii=False)
            if len(encoded) > 16000:
                encoded = json.dumps([{"status": "output_budget_exhausted"}])
                stats["output_budget_exhausted"] += 1
            system["parts"].append({"type": "text", "text": MARKER + encoded})
        return dict(stats)

    @staticmethod
    def route(session: Dict[str, Any]) -> tuple:
        return tuple(session.get(k) or "" for k in ("platform", "account_id", "scope", "group_id"))

    async def inspect_output(self, item: Dict[str, Any], sid: str, session: Dict[str, Any]) -> Dict[str, Any]:
        content = item.get("output")
        call_id = item.get("call_id")
        if not isinstance(content, str) or not isinstance(call_id, str) or not 0 < len(call_id) <= 255:
            return {"status": "invalid_output"}
        if item.get("success") is not True:
            return {"call_id": call_id, "status": "tool_failed"}
        return await self.inspect_record(sid, call_id, item["tool_name"], digest(content), session)

    async def inspect_record(
        self, sid: str, call_id: str, tool_name: str, history_digest: str, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        base = {"call_id": call_id, "tool_name": tool_name, "status": "record_unavailable"}
        rows = await self.ctx.db.get("ToolRecord", filters={"session_id": sid, "tool_id": call_id}, limit=2)
        if not isinstance(rows, list) or len(rows) != 1:
            return base
        row = rows[0]
        raw = row.get("tool_data")
        if not isinstance(raw, str) or len(raw) > 262144:
            return {**base, "status": "record_unreadable"}
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or row.get("session_id") != sid
            or row.get("tool_id") != call_id
            or row.get("tool_name") != tool_name
            or data.get("call_id") != call_id
            or data.get("session_id") != sid
            or data.get("stream_id") != sid
            or data.get("provider_type") != "builtin"
            or data.get("provider_name") != "maisaka_builtin"
            or not isinstance(data.get("history_content"), str)
            or digest(data["history_content"]) != history_digest
        ):
            return {**base, "status": "record_mismatch"}
        if data.get("success") is not True:
            return {**base, "status": "tool_failed"}
        reference_key = json.dumps([sid, self.route(session), call_id, history_digest], ensure_ascii=False).encode()
        token = hmac.new(self.secret, reference_key, hashlib.sha256).hexdigest()[:32]
        self.refs.put(
            token,
            {
                "sid": sid,
                "call_id": call_id,
                "tool_name": tool_name,
                "digest": history_digest,
                "route": self.route(session),
            },
        )
        base["reference"] = f"[stable_identity.memory_ref:v1:{token}]"
        structured = data.get("structured_content")
        if not isinstance(structured, dict):
            return {**base, "status": "structured_unavailable"}
        if structured.get("structured_content_omitted"):
            return {**base, "status": "structured_omitted"}
        if structured.get("success") is not True:
            return {**base, "status": "tool_failed"}
        if tool_name == "query_person_profile":
            return await self.profile(base, structured, sid, session)
        if structured.get("chat_id") != sid:
            return {**base, "status": "scope_mismatch"}
        hits = structured.get("hits")
        if not isinstance(hits, list) or len(hits) > 20:
            return {**base, "status": "invalid_hits"}
        # Query target is a request parameter, NEVER the identity of each hit.
        checked = []
        for index, hit in enumerate(hits):
            if index >= 8:
                checked.append({"index": index + 1, "status": "not_checked"})
                break
            checked.append(await self.hit(hit, index + 1, structured.get("person_id", ""), sid, session))
        return {
            **base,
            "status": "structured_checked",
            "fallback_applied": structured.get("fallback_applied") is True,
            "hits": checked,
            "semantic_status": "not_evaluated",
        }

    async def profile(
        self, base: Dict[str, Any], data: Dict[str, Any], sid: str, session: Dict[str, Any]
    ) -> Dict[str, Any]:
        pid = data.get("person_id")
        if not isinstance(pid, str) or not 0 < len(pid) <= 128:
            return {**base, "status": "profile_subject_unknown", "reason": "invalid_subject_id"}
        subject = await self.engine.subject(sid, pid)
        if subject is None:
            return {**base, "status": "profile_subject_unknown", "reason": "subject_mapping_unavailable"}
        if not await self.present(subject, sid, session):
            return {**base, "status": "profile_subject_unknown", "reason": "no_current_session_presence"}
        return {
            **base,
            "status": "profile_subject_mapped",
            "subject": subject,
            "evidence_status": "not_exposed_by_profile_tool",
            "semantic_status": "not_evaluated",
        }

    async def present(self, subject: Dict[str, str], sid: str, session: Dict[str, Any]) -> bool:
        rows = await self.ctx.db.get(
            "Messages", filters={"session_id": sid, "user_id": subject["user_id"], "is_notify": False}, limit=2
        )
        return isinstance(rows, list) and any(
            same_route(row, session) and row.get("user_id") == subject["user_id"] and not row.get("is_notify")
            for row in rows
        )

    async def hit(self, hit: Any, index: int, requested_pid: Any, sid: str, session: Dict[str, Any]) -> Dict[str, Any]:
        base = {"index": index, "status": "subject_unknown"}
        if not isinstance(hit, dict) or not isinstance(hit.get("metadata"), dict):
            return {**base, "reason": "invalid_hit_metadata"}
        meta = hit["metadata"]
        chat_ids = meta.get("chat_ids", [])
        if meta.get("chat_id") != sid or not isinstance(chat_ids, list) or any(s != sid for s in chat_ids):
            return {**base, "status": "scope_mismatch"}
        pids = meta.get("person_ids")
        if pids is None or pids == []:
            return {**base, "reason": "missing_subject_ids"}
        if not isinstance(pids, list):
            return {**base, "reason": "invalid_subject_ids"}
        if len(pids) != 1:
            return {**base, "reason": "multiple_subject_ids"}
        if not isinstance(pids[0], str) or not 0 < len(pids[0]) <= 128:
            return {**base, "reason": "invalid_subject_id"}
        pid = pids[0]
        if meta.get("person_id", pid) != pid:
            return {**base, "status": "subject_conflict"}
        subject = await self.engine.subject(sid, pid)
        if subject is None:
            return {**base, "reason": "subject_mapping_unavailable"}
        if not await self.present(subject, sid, session):
            return {**base, "reason": "no_current_session_presence"}
        ids = meta.get("evidence_message_ids", [])
        if (
            not isinstance(ids, list)
            or len(ids) > 12
            or any(not isinstance(i, str) or not 0 < len(i) <= 255 for i in ids)
        ):
            return {**base, "status": "invalid_evidence_ids"}
        ids = list(dict.fromkeys(ids))
        sources = []
        # Source IDs here come from the stored structured record. They are raw
        # IDs; do not expand them with a request's potentially colliding aliases.
        for raw_id in ids:
            message, reason = await self.engine.original(sid, raw_id, session)
            source = {"message_id": raw_id, "status": reason}
            if message is not None:
                record = metadata(message)
                source.update(platform=record["platform"], user_id=record["user_id"], timestamp=record["timestamp"])
                source["speaker_kind"] = "bot" if record["user_id"] in session["_identity_bot_ids"] else "user"
                if record["user_id"] != subject["user_id"]:
                    source["status"] = "different_speaker"
            sources.append(source)
        if not sources and not await self.present(subject, sid, session):
            return {**base, "reason": "no_current_session_presence"}
        status = (
            "source_identity_verified"
            if sources and all(s["status"] == "found" for s in sources)
            else "evidence_incomplete"
        )
        return {
            "index": index,
            "status": status,
            "subject": subject,
            "matches_query_subject": (pid == requested_pid) if requested_pid else None,
            "sources": sources,
            "evidence_status": "not_recorded" if not sources else status,
            "semantic_status": "not_evaluated",
        }
