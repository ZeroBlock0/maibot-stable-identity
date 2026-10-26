"""Build an installation ZIP from an explicit, non-private allowlist."""

from pathlib import Path
from typing import Any, Dict
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import argparse
import hashlib
import json


FILES = (
    "README.md",
    "LICENSE",
    "_manifest.json",
    "plugin.py",
    "engine.py",
    "diagnostics.py",
    "memory_bridge.py",
    "requirements.txt",
    "pyproject.toml",
    "config.example.toml",
    "repair_config.py",
    "INSTALL_ZIP.md",
    "CHANGELOG.md",
    "docs/OPERATIONS.md",
    "docs/VERIFICATION.md",
    "docs/REQUEST_EXAMPLES.md",
    "docs/model_requests.synthetic.json",
    "docs/SNOWLUMA_AND_MEMORY.md",
    "docs/MEMORY_BRIDGE.md",
    "docs/MEMORY_REQUEST_EXAMPLES.md",
    "docs/memory_requests.synthetic.json",
)


def build_release(source: Path, output: Path) -> Dict[str, Any]:
    # Intentionally NEVER read source/config.toml: it may contain private settings.
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w") as archive:
        for name in (*FILES, "config.toml"):
            original = source / ("config.example.toml" if name == "config.toml" else name)
            entry = ZipInfo("stable_identity/" + name, date_time=(2026, 10, 26, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, original.read_bytes())
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        assert archive.read("stable_identity/config.toml") == (source / "config.example.toml").read_bytes()
        assert len(archive.namelist()) == len(FILES) + 1
    return {
        "archive": str(output),
        "bytes": output.stat().st_size,
        "files": len(FILES) + 1,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    directory = Path(__file__).resolve().parent
    version = json.loads((directory / "_manifest.json").read_text())["version"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=directory / "dist" / f"stable_identity-{version}.zip")
    args = parser.parse_args()
    result = build_release(directory, args.output)
    print(json.dumps(result, ensure_ascii=False))
