"""`crucible.yaml` parsing, and the case loader that reads a dataset off disk.

These are the parts of the CLI that fail on a developer's machine before anything reaches the
network, so the thing worth testing is the error messages: a missing key or a bad line should say
which file, which section, and which line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crucible.cli.config import ConfigError, find_config, load_cases, load_config

CONFIG = """\
project: demo
targets:
  - name: agent
    config: {model: claude-sonnet-5, architecture: single_shot}
datasets:
  - name: golden
    path: evals/golden.jsonl
suites:
  - name: default
    dataset: golden
    pass_threshold: 0.9
    assertions:
      - {type: equals, value_from: expected.answer}
"""


def write(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "crucible.yaml"
    path.write_text(text)
    return path


def test_a_complete_config_parses_into_specs(tmp_path: Path) -> None:
    config = load_config(write(tmp_path))
    assert config.project == "demo"
    assert config.targets[0].config["model"] == "claude-sonnet-5"
    assert config.targets[0].kind == "prompt"  # defaulted, not required in the file
    assert config.datasets[0].path == "evals/golden.jsonl"
    assert config.suites[0].pass_threshold == 0.9
    assert config.root == tmp_path


def test_the_api_key_comes_from_the_environment_before_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path, CONFIG + "api_key: committed-by-mistake\napi_url: http://from-file/\n")
    monkeypatch.setenv("CRUCIBLE_API_KEY", "crk_from_env")

    assert load_config(path).api_key == "crk_from_env"
    # A flag is more specific than either, and the trailing slash is normalised away so that
    # every path we join onto it does not end up with a double slash.
    explicit = load_config(path, api_url="http://from-flag/", api_key="crk_from_flag")
    assert (explicit.api_key, explicit.api_url) == ("crk_from_flag", "http://from-flag")


def test_config_is_found_from_a_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path)
    nested = tmp_path / "services" / "api"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    found = find_config()
    assert found is not None and found.parent == tmp_path.resolve()


def test_a_missing_key_names_the_section_and_the_key(tmp_path: Path) -> None:
    path = write(tmp_path, "suites:\n  - name: default\n    dataset: golden\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "suites" in str(excinfo.value) and "assertions" in str(excinfo.value)


def test_invalid_yaml_is_reported_as_config_not_as_a_traceback(tmp_path: Path) -> None:
    path = write(tmp_path, "project: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_no_config_file_is_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands that only need `--api-url` and a key must work in a directory with no config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CRUCIBLE_API_KEY", raising=False)
    config = load_config()
    assert config.path is None and config.api_url == "http://localhost:8000" and config.targets == []


def test_jsonl_cases_load_skipping_blanks_and_comments(tmp_path: Path) -> None:
    (tmp_path / "evals").mkdir()
    lines = [
        json.dumps({"external_id": "a", "input": {"q": "1"}, "expected": {"answer": "1"}}),
        "",
        "# the second case covers the tool path",
        json.dumps({"external_id": "b", "input": {"q": "2"}, "tool_fixtures": {"calculator": 42}}),
    ]
    (tmp_path / "evals/golden.jsonl").write_text("\n".join(lines))

    cases = load_cases(tmp_path, "evals/golden.jsonl")
    assert [c["external_id"] for c in cases] == ["a", "b"]
    assert cases[1]["tool_fixtures"] == {"calculator": 42}


def test_a_bad_jsonl_line_is_reported_with_its_line_number(tmp_path: Path) -> None:
    (tmp_path / "golden.jsonl").write_text('{"external_id": "a", "input": {}}\n{not json}\n')
    with pytest.raises(ConfigError, match=r"golden\.jsonl:2"):
        load_cases(tmp_path, "golden.jsonl")


def test_yaml_cases_load_from_either_a_list_or_a_cases_key(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("- {external_id: a, input: {q: 1}}\n")
    (tmp_path / "b.yaml").write_text("cases:\n  - {external_id: b, input: {q: 2}}\n")
    assert load_cases(tmp_path, "a.yaml")[0]["external_id"] == "a"
    assert load_cases(tmp_path, "b.yaml")[0]["external_id"] == "b"


def test_a_case_without_the_required_keys_is_rejected_before_upload(tmp_path: Path) -> None:
    (tmp_path / "golden.jsonl").write_text('{"external_id": "a"}\n')
    with pytest.raises(ConfigError, match="needs at least"):
        load_cases(tmp_path, "golden.jsonl")


def test_a_missing_dataset_file_names_the_resolved_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        load_cases(tmp_path, "evals/nope.jsonl")
    assert str(tmp_path / "evals/nope.jsonl") in str(excinfo.value)
