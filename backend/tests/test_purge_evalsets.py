from pathlib import Path

from scripts.purge_orphan_evalsets import find_orphans


def _p(name: str) -> Path:
    return Path("eval_sets") / name


def test_find_orphans_by_kb_ids():
    files = [_p("5.json"), _p("6.json"), _p("9.json")]
    assert find_orphans({9}, files) == [_p("5.json"), _p("6.json")]


def test_find_orphans_ignores_non_numeric_and_readme():
    files = [_p("README.md"), _p("abc.json"), _p("9.json")]
    assert find_orphans({9}, files) == []
    assert find_orphans(set(), files) == [_p("9.json")]
