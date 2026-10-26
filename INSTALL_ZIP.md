# ZIP 安装与配置错误修复

适用 MaiBot 1.3.15、SDK 2.16.0。本包内 `config.toml` 来自公开默认示例，不含任何用户数据。

## 首次安装

将 `stable_identity` 文件夹解压到实例实际使用的 `plugins/` 下，最终路径是 `plugins/stable_identity/plugin.py`，同目录应有 `config.toml` 和 `_manifest.json`。不要多套一层目录。Docker 要使用实际映射到容器 `/MaiMBot/plugins` 的目录。

安装包自带含 `plugin.config_version` 的默认配置，避免首次启用前被宿主开关生成只有 `enabled` 的不完整配置。使用原有运行环境重载或启用插件；SDK 必须已安装在该环境中，或通过正式 `MAIBOT_PLUGIN_SDK_PATH` 指向 SDK 克隆。

## 已有安装出现 config_error

出现“插件配置文件缺少 plugin.config_version”时，只需在现有 `config.toml` 的 `[plugin]` 表中添加：

```toml
config_version = "0.1.0"
```

这是配置结构版本；即使安装包是 0.2.5，这里仍填 0.1.0。保留原来的 `enabled` 和其他设置，保存后重载插件，必要时再启用。

也可复制新版 `repair_config.py` 与 `config.example.toml` 到已安装插件目录，然后使用 MaiBot 正在使用的 Python 执行（从实例根目录运行）：

```sh
python plugins/stable_identity/repair_config.py
```

工具会先备份原配置，仅补缺失/空白的版本号，保留启停状态、其他配置和注释。`--check` 只检查。它不会在插件导入时自动运行，也不修改任何宿主配置、聊天或记忆。

**升级已有安装时，不要用包内默认 config.toml 覆盖个人配置。** 先备份现有文件，只替换代码和文档，再按上面的方式补版本；若现有配置已有版本，则保持原样。

WebUI 安装的现有目录可能叫 `plugins/local_stable-identity/`。升级应更新这一个现有插件目录，或使用宿主已有更新入口；不要再同时创建 `plugins/stable_identity/` 留下两个相同 manifest ID的副本。

## 效果与限制

0.2.5 升级请完整更新包内代码和文档（包括 engine.py、memory_bridge.py、diagnostics.py 和 plugin.py），保留个人 config.toml。重载后确认 `稳定身份插件已加载 v0.2.5`。本版包含统一请求预算、减少无关复制、重载隔离和状态/耗时诊断，并完善公开发布元数据与构建方式。配置结构仍为 0.1.0，无数据库迁移，作者为 Zero。

保留旧版 ZIP 以便回退。要回退时停用插件，恢复旧版代码并保留个人配置，再重载；缓存和临时凭据会重建，不需要操作宿主数据库。新版诊断字段及验收步骤见 [运行说明](docs/OPERATIONS.md)。

原话工具支持 person_id 和批量消息来源 ID；原生记忆工具结果有完整结构时可回查身份与来源。未知主体显示具体 reason；Replyer 缺少引用明确显示 reference_missing，本次未核验。若出现 structured_omitted，表示宿主此前没有保存完整结构，不能从旧摘要恢复它；可选设置及影响见 [记忆增强说明](docs/MEMORY_BRIDGE.md)，插件不自动修改宿主配置。

0.2.0 同时保留了 0.1.2 对 Hook 丢失 `item_schema_version` 的修复。重载后查看新版加载日志，以及 `身份增强 Planner 请求标注完成` 和 `身份增强 Replyer 请求标注完成`。摘要首次立即输出，后续每阶段每 60 秒限频。未知身份为 WARNING；摘要不打印聊天正文、用户 ID或凭据。

不要仅以 `verified` 累计数或“标注完成”判断模型已收到标记。还要检查宿主没有 `item_schema_version 不受支持: None`、`items 无效/无法反序列化，已忽略`，并在最终模型请求中核对外层 `identity_user_id`。其他插件仍可能在本插件之后修改请求，不能仅凭本插件日志证明最终供应商请求正确。

身份标记和记忆核验说明在模型临时上下文中，不会由插件主动发到群里。原生工具和记忆写入保持原有行为。Focus 新增查证暂不支持，复古回复和压缩部分覆盖，自动画像/摘要等无结构来源路径仍受宿主接口限制；旧错误记忆不会自动修复。

源码离线测试结果、真实运行验证范围和合成请求示例见 README 与 docs。安装包不包含开发测试文件。
