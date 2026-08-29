from __future__ import annotations

import tomllib
from pathlib import Path


def test_package_supports_python_311_without_forbidden_runtime_dependencies() -> None:
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())

    assert config["project"]["requires-python"] == ">=3.11"
    forbidden = {"scipy", "httpx", "requests", "fastapi", "mcp", "playwright"}
    dependencies = {
        item.split(">", 1)[0].split("=", 1)[0].lower() for item in config["project"]["dependencies"]
    }
    assert dependencies.isdisjoint(forbidden)
