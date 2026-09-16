"""The committed 2DPackLib conversions in examples/guillotine_cutting/parsed/ must
match a fresh parse of the raw files in data/, so neither can drift from the
other (as tests/examples/test_nurse_rostering.py does for its XML instances)."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "guillotine_cutting"


def _load_parser() -> Any:
    spec = importlib.util.spec_from_file_location(
        "guillotine_cutting_parse_instance", _EXAMPLE_DIR / "parse_instance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_parser = _load_parser()


@pytest.mark.parametrize(
    ("raw_name", "parsed_name"),
    [
        ("CGCUT1.ins2D", "CGCUT1.json"),
        ("OF1.ins2D", "OF1.json"),
        ("OF2.ins2D", "OF2.json"),
    ],
)
def test_committed_instance_matches_a_fresh_parse(raw_name: str, parsed_name: str) -> None:
    raw_text = (_EXAMPLE_DIR / "data" / raw_name).read_text(encoding="utf-8")
    committed = json.loads((_EXAMPLE_DIR / "parsed" / parsed_name).read_text(encoding="utf-8"))
    assert _parser.parse_ins2d(raw_text, raw_name) == committed


def test_minimum_demand_is_refused_rather_than_dropped() -> None:
    text = "1\n10 10\n1 2 2 1 3 5\n"
    with pytest.raises(ValueError, match="minimum demand 1"):
        _parser.parse_ins2d(text, "CGCUT1.ins2D")
