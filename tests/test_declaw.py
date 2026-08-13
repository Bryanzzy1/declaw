"""Runnable checks for declaw. Run: python tests/test_declaw.py  (or pytest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from declaw import clean_text, inspect_text, lexical_divergence, select_candidate


def test_scrub_removes_invisibles():
    dirty = "Hel​lo️ wor⁣ld‍"  # ZWSP, VS16, invisible sep, ZWJ
    cleaned, st = clean_text(dirty)
    assert cleaned == "Hello world"
    assert st["removed"] == 4


def test_scrub_normalizes_exotic_space():
    cleaned, st = clean_text("a b")  # no-break space
    assert cleaned == "a b"
    assert st["replaced"] == 1


def test_scrub_keeps_newlines_and_plain_text():
    text = "line one\nline two\tindented"
    cleaned, st = clean_text(text)
    assert cleaned == text
    assert st["removed"] == 0


def test_inspect_lists_hidden():
    rows = inspect_text("a​b‍b")
    assert len(rows) == 2


def test_divergence_bounds():
    orig = "the cat sat on the warm mat by the door"
    assert lexical_divergence(orig, orig) == 0.0
    reworded = "a feline rested near the entrance on a heated rug"
    assert lexical_divergence(orig, reworded) > 0.9


def test_selector_picks_most_diverged():
    orig = "the cat sat on the warm mat"
    reworded = "a feline rested near a heated rug indoors"
    best, scores = select_candidate(orig, [orig, reworded])
    assert best == 1
    assert scores[1] > scores[0]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all tests passed")
