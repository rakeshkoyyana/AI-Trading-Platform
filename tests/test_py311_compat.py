"""Guard: the project must stay parseable by Python 3.11 (the user's interpreter), even if developed on 3.12+.

The classic trap is a backslash or reused quote inside an f-string expression, which only works on 3.12+.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_all_sources_parse_as_python_311():
    bad = []
    for p in list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        try:
            ast.parse(p.read_text(), filename=str(p), feature_version=(3, 11))
        except SyntaxError as e:
            bad.append(f"{p.relative_to(ROOT)}:{e.lineno} {e.msg}")
    assert not bad, bad
