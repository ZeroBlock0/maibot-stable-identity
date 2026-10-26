"""Regression: installed -> toggled -> missing version -> repaired -> activated."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zipfile import ZipFile

import ast
import shutil
import subprocess
import sys
import tomllib

from maibot_sdk.context import PluginContext
from pydantic import TypeAdapter, ValidationError
import pytest
import tomlkit

from stable_identity.build_release import FILES, build_release
from stable_identity.plugin import StableIdentityPlugin
from stable_identity.repair_config import repair_config
from .conftest import HOST_ROOT, PLUGIN_ROOT
from .host_harness import NS, source_function


def runner_config_harness():
    # Execute the Host's real configuration and activation functions without
    # starting networking, loading global bot config or modifying Host modules.
    namespace = dict(
        NS,
        Mapping=Mapping,
        Path=Path,
        cast=cast,
        tomlkit=tomlkit,
        tomllib=tomllib,
        dataclass=dataclass,
        Enum=Enum,
        Any=Any,
        _ConfigAwarePlugin=Any,
        _PLUGIN_ENABLED_VALIDATOR=TypeAdapter(bool),
        ValidationError=ValidationError,
    )
    path = HOST_ROOT / "src/plugin_runtime/runner/runner_main.py"
    tree = ast.parse(path.read_text())
    classes = [
        deepcopy(node)
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name
        in {
            "PluginConfigNormalizationResult",
            "PluginConfigVersionError",
            "PluginActivationStatus",
        }
    ]
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *classes],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)
    for name in (
        "_deep_copy_plugin_config_value",
        "_deep_copy_plugin_config_mapping",
        "_is_plugin_config_value_type_compatible",
        "_overlay_plugin_config_fields",
        "extract_plugin_config_version",
        "rebuild_plugin_config_data",
        "_restore_host_reserved_plugin_config",
    ):
        source_function("src/plugin_runtime/runner/runner_main.py", name, namespace)
    source_function("src/config/config_utils.py", "compare_versions", namespace)
    runner_type = type("ConfigurationSourceHarness", (), {})
    namespace["PluginRunner"] = runner_type
    static_names = (
        "_get_plugin_default_config",
        "_prepare_plugin_config_for_version_update",
        "_load_plugin_config",
        "_save_plugin_config",
        "_has_extra_config_keys",
        "_merge_plugin_config_document",
        "_is_plugin_enabled",
    )
    for name in (*static_names, "_normalize_plugin_config", "_apply_plugin_config", "_prepare_plugin_activation"):
        method = source_function("src/plugin_runtime/runner/runner_main.py", name, namespace, "PluginRunner")
        setattr(runner_type, name, staticmethod(method) if name in static_names else method)
    return runner_type()


def defaults():
    return PLUGIN_ROOT / "config.example.toml"


@pytest.mark.parametrize("enabled", [True, False])
def test_repair_preserves_enabled_limits_comments_and_original_backup(tmp_path, enabled):
    config = tmp_path / "config.toml"
    original = (
        f"# operator note\n[plugin]\nenabled = {str(enabled).lower()} # keep\n\n[limits]\ncache_capacity = 128\n"
    ).encode()
    config.write_bytes(original)
    config.chmod(0o640)
    result = repair_config(config, defaults())
    assert result["status"] == "repaired"
    assert Path(result["backup"]).read_bytes() == original
    after = tomllib.loads(config.read_text())
    assert after == {"plugin": {"enabled": enabled, "config_version": "0.1.0"}, "limits": {"cache_capacity": 128}}
    assert "# operator note" in config.read_text() and "# keep" in config.read_text()
    assert config.stat().st_mode & 0o777 == 0o640
    assert repair_config(config, defaults())["status"] == "unchanged"
    assert len(list(tmp_path.glob("config.toml.backup.*"))) == 1


@pytest.mark.parametrize(
    "text", ["plugin = {enabled = false}\n", '[plugin]\nenabled = true\nconfig_version = ""\n', 'title = "keep"\n']
)
def test_repair_other_valid_toml_layouts(tmp_path, text):
    config = tmp_path / "config.toml"
    config.write_text(text)
    assert repair_config(config, defaults())["status"] == "repaired"
    data = tomllib.loads(config.read_text())
    assert data["plugin"]["config_version"] == "0.1.0"
    before = tomllib.loads(text)
    for key, value in before.items():
        if key == "plugin":
            for field, setting in value.items():
                if field != "config_version":
                    assert data[key][field] == setting
        else:
            assert data[key] == value


def test_missing_config_creation_and_check_mode(tmp_path):
    config = tmp_path / "config.toml"
    assert repair_config(config, defaults(), check=True)["status"] == "needs_repair"
    assert not config.exists()
    assert repair_config(config, defaults())["status"] == "created"
    assert tomllib.loads(config.read_text())["plugin"]["config_version"] == "0.1.0"
    assert not list(tmp_path.glob("*.backup.*"))


def test_existing_bad_config_check_does_not_write(tmp_path):
    config = tmp_path / "config.toml"
    original = b"[plugin]\nenabled = true\n"
    config.write_bytes(original)
    assert repair_config(config, defaults(), check=True)["status"] == "needs_repair"
    assert config.read_bytes() == original
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.parametrize("text", ["[plugin\n", 'plugin = "wrong"\n', "[plugin]\nconfig_version = false\n"])
def test_invalid_configuration_is_not_overwritten(tmp_path, text):
    config = tmp_path / "config.toml"
    config.write_text(text)
    with pytest.raises(ValueError):
        repair_config(config, defaults())
    assert config.read_text() == text
    assert len(list(tmp_path.iterdir())) == 1


def test_symlink_target_is_not_modified(tmp_path):
    target = tmp_path / "other.toml"
    target.write_text("[plugin]\nenabled = false\n")
    config = tmp_path / "config.toml"
    config.symlink_to(target)
    with pytest.raises(ValueError):
        repair_config(config, defaults())
    assert "config_version" not in target.read_text()


async def test_real_runner_reproduces_config_error_and_repair_activates(host, tmp_path):
    runner = runner_config_harness()
    plugin = StableIdentityPlugin()
    ctx = PluginContext(plugin_id="local.stable-identity", rpc_call=host.rpc)
    runner._inject_context = lambda plugin_id, instance, directory: instance._set_context(ctx)
    config = tmp_path / "config.toml"
    # Same disk state produced by toggling a plugin before full initialization.
    config.write_text("[plugin]\nenabled = true\n")
    meta = SimpleNamespace(
        instance=plugin,
        plugin_id="local.stable-identity",
        plugin_dir=str(tmp_path),
        explicitly_disabled=False,
        config_error="",
    )
    assert runner._prepare_plugin_activation(meta) is None
    assert "plugin.config_version" in meta.config_error
    assert plugin.engine is None
    repair_config(config, defaults())
    assert runner._prepare_plugin_activation(meta) is None
    assert meta.config_error == ""
    await plugin.on_load()
    assert plugin.active()
    assert plugin.config.plugin.config_version == "0.1.0"
    await plugin.on_unload()


async def test_real_runner_initializes_an_absent_config(host, tmp_path):
    runner = runner_config_harness()
    plugin = StableIdentityPlugin()
    meta = SimpleNamespace(instance=plugin, plugin_id="local.stable-identity", plugin_dir=str(tmp_path))
    result = runner._apply_plugin_config(meta)
    assert result["plugin"]["config_version"] == "0.1.0"
    assert tomllib.loads((tmp_path / "config.toml").read_text())["plugin"]["config_version"] == "0.1.0"


def test_zip_contains_public_defaults_never_installed_config_or_backup(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in FILES:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLUGIN_ROOT / name, target)
    (source / "config.toml").write_text('private_test_sentinel = "must-not-ship"\n')
    (source / "config.toml.backup.test").write_text("must-not-ship")
    archive = tmp_path / "plugin.zip"
    build_release(source, archive)
    with ZipFile(archive) as package:
        assert package.testzip() is None
        assert (
            tomllib.loads(package.read("stable_identity/config.toml").decode())["plugin"]["config_version"] == "0.1.0"
        )
        assert not any("backup" in name or "tests/" in name or "/.git/" in name for name in package.namelist())
        assert all(b"must-not-ship" not in package.read(name) for name in package.namelist())


async def test_zip_defaults_survive_enable_disable_before_runner_load(host, tmp_path):
    archive = tmp_path / "plugin.zip"
    build_release(PLUGIN_ROOT, archive)
    with ZipFile(archive) as package:
        package.extractall(tmp_path / "extracted")
    directory = tmp_path / "extracted/stable_identity"
    config = directory / "config.toml"
    for enabled in (False, True):
        document = tomlkit.parse(config.read_text())
        document["plugin"]["enabled"] = enabled
        config.write_text(tomlkit.dumps(document))
        runner = runner_config_harness()
        plugin = StableIdentityPlugin()
        result = runner._apply_plugin_config(
            SimpleNamespace(instance=plugin, plugin_id="local.stable-identity", plugin_dir=str(directory))
        )
        assert result["plugin"]["config_version"] == "0.1.0"
        assert result["plugin"]["enabled"] is enabled


@pytest.mark.parametrize("explicit", [False, True])
def test_build_cli_is_safe_in_standalone_checkout(tmp_path, explicit):
    source = tmp_path / "arbitrary-checkout-name"
    source.mkdir()
    for name in (*FILES, "build_release.py"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLUGIN_ROOT / name, target)
    (source / "config.toml").write_text('private_test_sentinel = "must-not-ship"\n')
    arguments = [sys.executable, str(source / "build_release.py")]
    output = tmp_path / "explicit" / "plugin.zip"
    if explicit:
        arguments.extend(["--output", str(output)])
    result = subprocess.run(arguments, cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    if not explicit:
        archives = list((source / "dist").glob("*.zip"))
        assert len(archives) == 1
        output = archives[0]
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert b"must-not-ship" not in archive.read("stable_identity/config.toml")
    assert not list(tmp_path.glob("stable_identity-*.zip"))
