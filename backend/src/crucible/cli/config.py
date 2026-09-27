"""`crucible.yaml` — the file that makes a run reproducible from a repository.

Config lives in the repo next to the prompt it describes, so a pull request that changes a prompt
also changes the thing the eval will execute, and CI needs no arguments beyond the file.

Precedence is flag > environment > file, which is the order of how specific the intent is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAMES = ("crucible.yaml", "crucible.yml", ".crucible.yaml")
DEFAULT_API_URL = "http://localhost:8000"


class ConfigError(Exception):
    """Raised with a message meant to be read by a person in a terminal, not parsed."""


@dataclass
class TargetSpec:
    name: str
    config: dict[str, Any]
    kind: str = "prompt"
    description: str | None = None


@dataclass
class SuiteSpec:
    name: str
    dataset: str
    assertions: list[dict[str, Any]]
    judge_model: str | None = None
    pass_threshold: float = 1.0
    description: str | None = None


@dataclass
class DatasetSpec:
    name: str
    path: str
    description: str | None = None


@dataclass
class CliConfig:
    project: str | None = None
    api_url: str = DEFAULT_API_URL
    api_key: str | None = None
    targets: list[TargetSpec] = field(default_factory=list)
    datasets: list[DatasetSpec] = field(default_factory=list)
    suites: list[SuiteSpec] = field(default_factory=list)
    path: Path | None = None

    @property
    def root(self) -> Path:
        return self.path.parent if self.path else Path.cwd()


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from `start` the way git finds its root, so the CLI works from a subdirectory."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        for name in CONFIG_NAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_config(path: Path | None = None, *, api_url: str | None = None, api_key: str | None = None) -> CliConfig:
    found = path or find_config()
    data: dict[str, Any] = {}
    if found is not None:
        try:
            data = yaml.safe_load(found.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{found} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{found} must contain a mapping at the top level")

    config = CliConfig(
        project=data.get("project"),
        api_url=(api_url or os.getenv("CRUCIBLE_API_URL") or data.get("api_url") or DEFAULT_API_URL).rstrip("/"),
        # The key comes from the environment by default: a secret committed to crucible.yaml is
        # a secret published to everyone with read access to the repository.
        api_key=api_key or os.getenv("CRUCIBLE_API_KEY") or data.get("api_key"),
        path=found,
    )

    for raw in data.get("targets") or []:
        _require(raw, ("name", "config"), "targets")
        config.targets.append(
            TargetSpec(
                name=raw["name"],
                config=raw["config"],
                kind=raw.get("kind", "prompt"),
                description=raw.get("description"),
            )
        )

    for raw in data.get("datasets") or []:
        _require(raw, ("name", "path"), "datasets")
        config.datasets.append(DatasetSpec(name=raw["name"], path=raw["path"], description=raw.get("description")))

    for raw in data.get("suites") or []:
        _require(raw, ("name", "dataset", "assertions"), "suites")
        config.suites.append(
            SuiteSpec(
                name=raw["name"],
                dataset=raw["dataset"],
                assertions=raw["assertions"],
                judge_model=raw.get("judge_model"),
                pass_threshold=float(raw.get("pass_threshold", 1.0)),
                description=raw.get("description"),
            )
        )

    return config


def _require(raw: Any, keys: tuple[str, ...], section: str) -> None:
    if not isinstance(raw, dict):
        raise ConfigError(f"each entry under `{section}` must be a mapping, got {type(raw).__name__}")
    missing = [k for k in keys if k not in raw]
    if missing:
        raise ConfigError(f"an entry under `{section}` is missing: {', '.join(missing)}")


def load_cases(root: Path, relative: str) -> list[dict[str, Any]]:
    """Cases come from JSONL or YAML. JSONL is the default because it diffs one case per line."""
    path = (root / relative).resolve()
    if not path.is_file():
        raise ConfigError(f"dataset file not found: {path}")

    if path.suffix in (".yaml", ".yml"):
        loaded = yaml.safe_load(path.read_text()) or []
        cases = loaded.get("cases", loaded) if isinstance(loaded, dict) else loaded
    else:
        import json

        cases = []
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{path}:{number} is not valid JSON: {exc.msg}") from exc

    if not isinstance(cases, list) or not cases:
        raise ConfigError(f"{path} contains no cases")
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or "external_id" not in case or "input" not in case:
            raise ConfigError(f"{path}: case {index} needs at least `external_id` and `input`")
    return cases
