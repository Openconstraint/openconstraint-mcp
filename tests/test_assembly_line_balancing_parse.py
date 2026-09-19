"""The committed Scholl conversion in examples/assembly_line_balancing/parsed/ must
match a fresh parse of the raw file in data/, so neither can drift from the other
(as tests/test_guillotine_cutting_parse.py does for 2DPackLib)."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "assembly_line_balancing"


def _load_parser() -> Any:
    spec = importlib.util.spec_from_file_location(
        "assembly_line_balancing_parse_instance", _EXAMPLE_DIR / "parse_instance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_parser = _load_parser()


def test_committed_instance_matches_a_fresh_parse() -> None:
    raw_text = (_EXAMPLE_DIR / "data" / "JACKSON.IN2").read_text(encoding="utf-8")
    committed = json.loads((_EXAMPLE_DIR / "parsed" / "JACKSON_c10.json").read_text())
    assert _parser.parse_in2(raw_text, "JACKSON_c10") == committed


def test_free_text_note_after_the_end_mark_is_not_data() -> None:
    text = "2\r\n3\r\n4\r\n1,2\r\n-1,-1\r\n\r\nKleinbildkamera\r\nLutz (1974, S. 16)\r\n"
    instance = _parser.parse_in2(text, "JACKSON_c10")
    assert (instance["tasks"], instance["precedences"]) == (
        [{"id": 1, "time": 3}, {"id": 2, "time": 4}],
        [[1, 2]],
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("3\n3\n4\n1,2\n-1,-1\n", "header says 3 tasks"),
        ("2\n3\n4\n1;2\n-1,-1\n", "expected a precedence 'i,j'"),
        ("2\n3\n4\n1,3\n-1,-1\n", "names an unknown task"),
    ],
)
def test_malformed_file_is_refused_rather_than_dropped(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parser.parse_in2(text, "JACKSON_c10")
