"""Runnable checks for declaw. Run: python tests/test_declaw.py  (or pytest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from declaw import (
    clean_text,
    decode_tag_payload,
    decode_variation_bytes,
    find_confusables,
    fold_confusables,
    inspect_text,
    lexical_divergence,
    mixed_script_words,
    select_candidate,
    verify_preservation,
)

_TAG = 0xE0000


def _tag_encode(s: str) -> str:
    return "".join(chr(_TAG + ord(c)) for c in s)


def _vs_encode(data: bytes) -> str:
    return "".join(chr(0xFE00 + b) if b < 16 else chr(0xE0100 + b - 16) for b in data)


def test_scrub_removes_invisibles():
    # Built from codepoints so the invisibles survive file round-trips: ZWSP, VS16,
    # invisible separator, ZWJ.
    dirty = "Hel" + chr(0x200B) + "lo" + chr(0xFE0F) + " wor" + chr(0x2063) + "ld" + chr(0x200D)
    cleaned, st = clean_text(dirty)
    assert cleaned == "Hello world"
    assert st["removed"] == 4


def test_scrub_normalizes_exotic_space():
    cleaned, st = clean_text("a" + chr(0x00A0) + "b")  # no-break space
    assert cleaned == "a b"
    assert st["replaced"] == 1


def test_scrub_keeps_newlines_and_plain_text():
    text = "line one\nline two\tindented"
    cleaned, st = clean_text(text)
    assert cleaned == text
    assert st["removed"] == 0


def test_scrub_strips_blank_glyphs():
    # Braille blank (So) and Hangul filler (Lo) render empty but dodge the Cf/Cc/Zs pass.
    dirty = "foo" + chr(0x2800) + "bar" + chr(0x3164) + "baz"
    cleaned, st = clean_text(dirty)
    assert cleaned == "foobarbaz"
    assert st["removed"] == 2


def test_scrub_is_idempotent():
    dirty = "Hel" + chr(0x200B) + "lo wor" + chr(0x2063) + "ld" + chr(0x2800) + " a" + chr(0x00A0) + "b"
    once, _ = clean_text(dirty)
    twice, st = clean_text(once)
    assert once == twice
    assert st["removed"] == 0 and st["replaced"] == 0


def test_inspect_lists_hidden():
    rows = inspect_text("a" + chr(0x200B) + "b" + chr(0x200D) + "b")  # ZWSP + ZWJ
    assert len(rows) == 2


def test_decode_tag_payload():
    carrier = "Looks normal." + _tag_encode("rm -rf /")
    assert decode_tag_payload(carrier) == "rm -rf /"
    assert decode_tag_payload("nothing hidden") == ""


def test_decode_variation_bytes():
    carrier = "clean" + _vs_encode("payload".encode())
    assert decode_variation_bytes(carrier) == "payload"
    # a lone emoji presentation selector is not a payload
    assert decode_variation_bytes("thumbs" + chr(0xFE0F)) == ""


def test_fold_confusables():
    spoof = "p" + chr(0x0430) + "ypal"  # Cyrillic a inside "paypal"
    folded, n = fold_confusables(spoof)
    assert folded == "paypal" and n == 1
    assert fold_confusables("paypal") == ("paypal", 0)
    assert find_confusables(spoof)[0][2] == "a"


def test_mixed_script_words():
    assert len(mixed_script_words("p" + chr(0x0430) + "ypal")) == 1  # Latin + Cyrillic
    assert mixed_script_words("plain english words") == []


def test_verify_preservation():
    orig = "Cut latency 12x to 207 us for Acme."
    good = "Acme saw latency fall 12x, to 207 us."
    bad = "Cut latency 10x to 210 us."
    assert verify_preservation(orig, good)["missing_numbers"] == []
    assert set(verify_preservation(orig, bad)["missing_numbers"]) == {"12", "207"}


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
