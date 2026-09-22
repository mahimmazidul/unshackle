from __future__ import annotations

import ast
import io
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from rich.padding import Padding
from rich.segment import Segment
from rich.spinner import Spinner

CONSOLE_PY = Path(__file__).resolve().parents[2] / "unshackle" / "core" / "console.py"


def _helpers():
    import shutil

    tree = ast.parse(CONSOLE_PY.read_text(encoding="utf-8"))
    keep = ast.Module(
        body=[
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in {"terminal_columns", "console_width"}
        ],
        type_ignores=[],
    )
    ns: dict = {"shutil": shutil}
    exec(compile(keep, str(CONSOLE_PY), "exec"), ns)
    return ns["terminal_columns"], ns["console_width"]


def test_console_width_never_exceeds_the_terminal() -> None:
    terminal_columns, console_width = _helpers()
    with patch("shutil.get_terminal_size") as size:
        size.return_value = type("TS", (), {"columns": 40, "lines": 20})()
        assert terminal_columns() == 40
        assert console_width() == 40

    with patch("shutil.get_terminal_size") as size:
        size.return_value = type("TS", (), {"columns": 200, "lines": 50})()
        assert console_width() == 80


def test_status_padding_must_not_fill_80_columns() -> None:
    """An expanded 80-cell status wraps on a portrait screen; Live only erases one row."""
    console = Console(width=80, file=io.StringIO())
    spinner = Spinner("dots", text="Preparing Service Session…")
    filled = Padding(spinner, (0, 0, 0, 5), expand=True)
    cropped = Padding(spinner, (0, 0, 0, 5), expand=False)
    filled_width, _ = Segment.get_shape(console.render_lines(filled, console.options, pad=False))
    cropped_width, _ = Segment.get_shape(console.render_lines(cropped, console.options, pad=False))
    assert filled_width == 80
    assert cropped_width < 60
    source = CONSOLE_PY.read_text(encoding="utf-8")
    assert "expand=False" in source
    assert "width=console_width()" in source
