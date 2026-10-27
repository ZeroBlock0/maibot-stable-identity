# 合成记忆核验请求

全部为合成数据。原生工具记录由当前宿主 build_tool_record_payload 实际生成；Planner Hook 经过真实参数替换和协议反序列化。Replyer 使用真实请求构造、before_model_request Hook 入口及同一参数/协议校验；能力传输为测试替身，没有调用模型。引用仅供测试，生成后插件已卸载失效。

下面是额外 System 核验说明；原生工具输出和原话正文保留原样。source_identity_verified 只证明来源身份与记录主体一致，不证明事实真假。

## Planner 的原生记忆结果核验

```json
[
  {
    "call_id": "native-memory",
    "tool_name": "query_memory",
    "status": "structured_checked",
    "reference": "[stable_identity.memory_ref:v1:98b3c85c3570d780cce048eba9428f46]",
    "fallback_applied": false,
    "hits": [
      {
        "index": 1,
        "status": "source_identity_verified",
        "subject": {
          "platform": "qq",
          "user_id": "1001",
          "person_id": "1bc8b8291fd69e22a81527169ba22c7d"
        },
        "matches_query_subject": true,
        "sources": [
          {
            "message_id": "101",
            "status": "found",
            "platform": "qq",
            "user_id": "1001",
            "timestamp": 1792900800.123456,
            "speaker_kind": "user"
          }
        ],
        "evidence_status": "source_identity_verified",
        "semantic_status": "not_evaluated"
      }
    ],
    "semantic_status": "not_evaluated"
  }
]
```

## Replyer 的明确引用核验

```json
[
  {
    "call_id": "native-memory",
    "tool_name": "query_memory",
    "status": "structured_checked",
    "reference": "[stable_identity.memory_ref:v1:98b3c85c3570d780cce048eba9428f46]",
    "fallback_applied": false,
    "hits": [
      {
        "index": 1,
        "status": "source_identity_verified",
        "subject": {
          "platform": "qq",
          "user_id": "1001",
          "person_id": "1bc8b8291fd69e22a81527169ba22c7d"
        },
        "matches_query_subject": true,
        "sources": [
          {
            "message_id": "101",
            "status": "found",
            "platform": "qq",
            "user_id": "1001",
            "timestamp": 1792900800.123456,
            "speaker_kind": "user"
          }
        ],
        "evidence_status": "source_identity_verified",
        "semantic_status": "not_evaluated"
      }
    ],
    "semantic_status": "not_evaluated"
  }
]
```

## Planner：四条摘要缺失人物 ID

以下为失败场景的合成复现；原始参考中的账号及消息 ID仍保留，但不能据此认证摘要归属。

```json
[
  {
    "call_id": "native-memory",
    "tool_name": "query_memory",
    "status": "structured_checked",
    "reference": "[stable_identity.memory_ref:v1:766e722d13f2ab50044383d58e5fbea9]",
    "fallback_applied": false,
    "hits": [
      {
        "index": 1,
        "status": "subject_unknown",
        "reason": "missing_subject_ids"
      },
      {
        "index": 2,
        "status": "subject_unknown",
        "reason": "missing_subject_ids"
      },
      {
        "index": 3,
        "status": "subject_unknown",
        "reason": "missing_subject_ids"
      },
      {
        "index": 4,
        "status": "subject_unknown",
        "reason": "missing_subject_ids"
      }
    ],
    "semantic_status": "not_evaluated"
  }
]
```

## Replyer：模型漏传核验引用

以下为失败场景的合成复现；原始参考中的账号及消息 ID仍保留，但不能据此认证摘要归属。

```json
[]
```

0.2.6 的漏传引用场景中 Replyer 输出 []，表示没有向模型追加记忆核验文本；reference_missing 仅作日志诊断。原生参考未改写，也没有据此认证人物归属。

完整 Context Items 见 `memory_requests.synthetic.json`；不开结构化工具记录时返回 structured_omitted，不能得到这里的完整来源。
