"""Explicit CLI repair for this plugin's config; never runs during plugin import.

Only adds a missing/blank plugin.config_version. Preserve enabled, all custom
values and comments, back up existing bytes, then atomically replace the file.
"""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Dict

import argparse
import json
import os
import secrets
import stat
import tempfile
import time

import tomlkit


def repair_config(config_path: Path, defaults_path: Path, *, check: bool = False) -> Dict[str, Any]:
    if config_path.is_symlink():
        raise ValueError("配置是符号链接，请手工补齐版本号，工具不会修改链接目标。")
    default_bytes = defaults_path.read_bytes()
    defaults = tomlkit.parse(default_bytes.decode("utf-8"))
    version = defaults["plugin"]["config_version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("默认配置缺少有效的 plugin.config_version。")
    exists = config_path.exists()
    original = config_path.read_bytes() if exists else None
    document = tomlkit.parse(original.decode("utf-8")) if exists else defaults
    section = document.get("plugin")
    if section is not None and not isinstance(section, MutableMapping):
        raise ValueError("[plugin] 必须是 TOML 表；原配置未修改。")
    current = section.get("config_version") if section is not None else None
    if current is not None and not isinstance(current, str):
        raise ValueError("配置版本必须是字符串；原配置未修改。")
    if exists and current is not None and str(current).strip():
        return {"status": "unchanged", "message": "已有配置版本，未修改文件。"}
    if check:
        return {"status": "needs_repair", "message": "缺少配置文件或 plugin.config_version，尚未修改文件。"}
    if section is None:
        document["plugin"] = tomlkit.table()
        section = document["plugin"]
    section["config_version"] = version
    updated = tomlkit.dumps(document).encode("utf-8")
    # Validate the complete document before touching disk.
    assert tomlkit.parse(updated.decode("utf-8"))["plugin"]["config_version"] == version
    config_path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(config_path.stat().st_mode) if exists else 0o600
    backup = None
    if exists:
        backup = config_path.with_name(
            config_path.name + ".backup." + time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "." + secrets.token_hex(4)
        )
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix=".config.toml.", suffix=".tmp", dir=config_path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if exists:
            if config_path.is_symlink() or not config_path.exists() or config_path.read_bytes() != original:
                raise ValueError("配置在修复期间发生变化，未覆盖；请停止同时编辑后重试。")
            os.replace(temporary, config_path)
        else:
            # Exclusive link avoids replacing a configuration created concurrently.
            os.link(temporary, config_path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "status": "repaired" if exists else "created",
        "backup": str(backup) if backup else None,
        "message": "配置版本已补齐；原有启停状态和设置保持不变。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="修复 stable_identity 自身的配置版本，已有文件先备份。")
    parser.add_argument("--check", action="store_true", help="只检查，不修改文件")
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    try:
        result = repair_config(directory / "config.toml", directory / "config.example.toml", check=args.check)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["status"] == "needs_repair" else 0


if __name__ == "__main__":
    raise SystemExit(main())
