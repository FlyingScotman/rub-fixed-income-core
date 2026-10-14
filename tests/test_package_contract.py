from __future__ import annotations

import tomllib
from pathlib import Path

import rub_fixed_income


def test_package_supports_python_311_without_forbidden_runtime_dependencies() -> None:
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())

    assert config["project"]["requires-python"] == ">=3.11"
    forbidden = {"scipy", "httpx", "requests", "fastapi", "mcp", "playwright"}
    dependencies = {
        item.split(">", 1)[0].split("=", 1)[0].lower() for item in config["project"]["dependencies"]
    }
    assert dependencies.isdisjoint(forbidden)


def test_runtime_version_matches_project_metadata() -> None:
    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())

    assert rub_fixed_income.__version__ == config["project"]["version"]
