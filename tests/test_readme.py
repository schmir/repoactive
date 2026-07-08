"""Validate TOML blocks embedded in README.md."""

import itertools
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

from repoactive.config import _built_in_defaults, _ConfigSource, _merge_config

_README = Path(__file__).parent.parent / "README.md"


@dataclass(frozen=True)
class TomlBlock:
    line: int
    content: str


def _readme_toml_blocks() -> list[TomlBlock]:
    text = _README.read_text()
    return [
        TomlBlock(line=text.count(chr(10), 0, m.start()) + 1, content=m.group(1))
        for m in re.finditer(r"```toml\n(.*?)```", text, re.DOTALL)
    ]


def _validate(label: str, data: dict) -> None:
    _merge_config(itertools.chain(_built_in_defaults(), [_ConfigSource(label, data)]))


@pytest.mark.parametrize("block", _readme_toml_blocks(), ids=lambda b: f"README.md:{b.line}")
def test_readme_toml_block_is_valid(block: TomlBlock) -> None:
    print(f"README.md:{block.line}:\n---\n{block.content}---\n")
    data = tomllib.loads(block.content)
    if set(data.keys()) & {"platform", "job", "job-defaults"}:
        _validate(f"README.md:{block.line}", data)
