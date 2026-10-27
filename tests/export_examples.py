"""Export reproducible synthetic request evidence; never reads a live chat DB."""

import asyncio
import json
import subprocess
import tomllib

from .conftest import HOST_ROOT, PLUGIN_ROOT, SDK_ROOT, SyntheticHost
from .test_host_integration import build_requests
from .test_memory_bridge import build_memory_requests, notes


async def main():
    requests = await build_requests(SyntheticHost())
    metadata = {
        "verification_kind": "offline_source_function_integration",
        "live_model_called": False,
        "live_chat_database_used": False,
        "host_hook_kwargs_replacement_executed": True,
        "plugin_version": json.loads((PLUGIN_ROOT / "_manifest.json").read_text())["version"],
        "host_version": tomllib.loads((HOST_ROOT / "pyproject.toml").read_text())["project"]["version"],
        "host_commit": subprocess.check_output(["git", "-C", str(HOST_ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "sdk_version": tomllib.loads((SDK_ROOT / "pyproject.toml").read_text())["project"]["version"],
        "sdk_commit": subprocess.check_output(["git", "-C", str(SDK_ROOT), "rev-parse", "HEAD"], text=True).strip(),
    }
    target = PLUGIN_ROOT / "docs"
    target.mkdir(exist_ok=True)
    (target / "model_requests.synthetic.json").write_text(
        json.dumps({"metadata": metadata, **requests}, ensure_ascii=False, indent=2) + "\n"
    )
    sections = [
        "# 合成请求处理前后",
        "",
        "以下由当前宿主原始请求构造、Hook 参数整体替换及序列化/反序列化函数实际执行生成，数据全部为合成数据。使用 Hook 实际返回的 item_schema_version 校验，不使用默认值补齐。模型调用与聊天数据库为测试替身，未连接真实群聊。",
        "",
    ]
    for key, title in (
        ("planner_before", "Planner 处理前"),
        ("planner_after", "Planner 处理后"),
        ("replyer_before", "Replyer 处理前"),
        ("replyer_after", "Replyer 处理后"),
    ):
        texts = [
            part["text"]
            for item in requests[key]
            if item["item_type"] == "UserMessageItem"
            for part in item["parts"]
            if part.get("type") == "text" and part["text"].startswith("<message ")
        ]
        sections.extend([f"## {title}", "", "```text", "\n".join(texts), "```", ""])
    sections.extend(
        ["完整 Context Items 见同目录 `model_requests.synthetic.json`；身份只加入临时请求，原始合成消息未改写。", ""]
    )
    (target / "REQUEST_EXAMPLES.md").write_text("\n".join(sections))
    memory_requests = await build_memory_requests()
    dropped_requests = await build_memory_requests(unattributed=True, drop_reference=True)
    (target / "memory_requests.synthetic.json").write_text(
        json.dumps(
            {"metadata": metadata, **memory_requests, "unattributed_and_dropped_reference": dropped_requests},
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    memory_sections = [
        "# 合成记忆核验请求",
        "",
        "全部为合成数据。原生工具记录由当前宿主 build_tool_record_payload 实际生成；Planner Hook 经过真实参数替换和协议反序列化。Replyer 使用真实请求构造、before_model_request Hook 入口及同一参数/协议校验；能力传输为测试替身，没有调用模型。引用仅供测试，生成后插件已卸载失效。",
        "",
        "下面是额外 System 核验说明；原生工具输出和原话正文保留原样。source_identity_verified 只证明来源身份与记录主体一致，不证明事实真假。",
        "",
    ]
    for key, title in (("planner", "Planner 的原生记忆结果核验"), ("replyer_after", "Replyer 的明确引用核验")):
        memory_sections.extend(
            [
                f"## {title}",
                "",
                "```json",
                json.dumps(notes(memory_requests[key]), ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    for key, title in (("planner", "Planner：四条摘要缺失人物 ID"), ("replyer_after", "Replyer：模型漏传核验引用")):
        memory_sections.extend(
            [
                f"## {title}",
                "",
                "以下为失败场景的合成复现；原始参考中的账号及消息 ID仍保留，但不能据此认证摘要归属。",
                "",
                "```json",
                json.dumps(notes(dropped_requests[key]), ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    memory_sections.extend(
        [
            "0.2.6 的漏传引用场景中 Replyer 输出 []，表示没有向模型追加记忆核验文本；reference_missing 仅作日志诊断。原生参考未改写，也没有据此认证人物归属。",
            "",
            "完整 Context Items 见 `memory_requests.synthetic.json`；不开结构化工具记录时返回 structured_omitted，不能得到这里的完整来源。",
            "",
        ]
    )
    (target / "MEMORY_REQUEST_EXAMPLES.md").write_text("\n".join(memory_sections))
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
