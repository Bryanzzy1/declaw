#!/usr/bin/env python3
"""declaw: strip AI provenance from text you own, for privacy and hygiene.

Two layers, text only:
  Layer A (deterministic)  scrub invisible Unicode (zero-width, variation
                           selectors, tag chars, bidi, exotic spaces).
  Layer B (statistical)    the token-choice watermark lives in word choice, so
                           it only comes out with a real rewrite. declaw hands
                           you a Gemini rewrite prompt (or calls Gemini if you
                           set GEMINI_API_KEY), then scores candidates by how
                           much they diverge at the token level and picks the best.

Subcommands:
  scrub     Layer A on a file/stdin, writes cleaned text
  inspect   report hidden characters found, change nothing
  prompt    emit the Gemini rewrite prompt around your text
  rewrite   headless Layer B: scrub + Gemini rewrite a file/stdin
  score     rank rewrite candidates by lexical divergence, pick the best
  web       clipboard loop for the browser (claude.ai -> Gemini -> clean)
  doctor    check the Gemini key and which models are available
  selftest  run built-in asserts

The Gemini backend authenticates with the x-goog-api-key header (the key never
enters the URL), retries overloaded models (503/429) with backoff, and falls
back across flash models. stdlib only; the backend is optional and off by
default.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Layer A: deterministic invisible-character scrub
# --------------------------------------------------------------------------- #

# Explicit steganographic / invisible codepoints to delete outright.
_REMOVE: set[int] = set()
_REMOVE |= {0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x061C}  # zero-width, joiners, BOM, soft hyphen
_REMOVE |= {0x200E, 0x200F}                    # LRM, RLM
_REMOVE |= set(range(0x202A, 0x202F))          # bidi embeddings/overrides
_REMOVE |= set(range(0x2066, 0x206A))          # bidi isolates
_REMOVE |= set(range(0xFE00, 0xFE10))          # variation selectors
_REMOVE |= set(range(0xE0100, 0xE01F0))        # variation selectors supplement
_REMOVE |= set(range(0xE0000, 0xE0080))        # Unicode tag chars (hidden payloads)
# Blank/zero-width glyphs the Unicode-category pass below misses, because their
# category is So or Lo, not Cf/Cc/Zs. Steganography favors exactly these: they render
# as nothing yet survive a naive "strip the format chars" cleaner.
_REMOVE |= {0x2800}                            # braille pattern blank (renders empty, So)
_REMOVE |= {0x115F, 0x1160, 0x3164, 0xFFA0}    # Hangul fillers (zero-width, Lo)

_KEEP_CONTROLS = {"\n", "\r", "\t"}


def clean_text(
    text: str, *, nfkc: bool = False, normalize_spaces: bool = True
) -> tuple[str, dict[str, int]]:
    """Return (cleaned_text, stats). Deletes invisibles, normalizes odd spaces."""
    out: list[str] = []
    removed = 0
    replaced = 0
    for ch in text:
        cp = ord(ch)
        if cp in _REMOVE:
            removed += 1
            continue
        cat = unicodedata.category(ch)
        # Cf = format chars (most invisibles). Cc = control chars.
        # ponytail: this also strips ZWJ inside emoji/Indic clusters; fine for
        # English prose, revisit if non-Latin scripts matter.
        if cat == "Cf":
            removed += 1
            continue
        if cat == "Cc" and ch not in _KEEP_CONTROLS:
            removed += 1
            continue
        if normalize_spaces and cat == "Zs" and cp != 0x20:
            out.append(" ")
            replaced += 1
            continue
        out.append(ch)
    cleaned = "".join(out)
    if nfkc:
        cleaned = unicodedata.normalize("NFKC", cleaned)
    stats = {
        "removed": removed,
        "replaced": replaced,
        "in_len": len(text),
        "out_len": len(cleaned),
    }
    return cleaned, stats


def inspect_text(text: str) -> list[tuple[int, str, int]]:
    """Return [(codepoint, name, count)] for every hidden char present."""
    counts: dict[int, int] = {}
    for ch in text:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        hidden = (
            cp in _REMOVE
            or cat == "Cf"
            or (cat == "Cc" and ch not in _KEEP_CONTROLS)
            or (cat == "Zs" and cp != 0x20)
        )
        if hidden:
            counts[cp] = counts.get(cp, 0) + 1
    rows = []
    for cp, n in sorted(counts.items()):
        try:
            name = unicodedata.name(chr(cp))
        except ValueError:
            name = "<unnamed>"
        rows.append((cp, name, n))
    return rows


# ASCII smuggling: printable ASCII maps 1:1 into the Unicode Tag block, so an attacker can
# hide a whole instruction (a prompt injection, an exfil command, a fingerprint) in text that
# looks clean to a human but is fully readable to an LLM. See embracethered.com's ASCII
# Smuggler and promptfoo's ascii-smuggling plugin. scrub already deletes these chars; decoding
# them tells you WHAT was hidden, which is the difference between "cleaned it" and "caught it".
_TAG_BASE = 0xE0000


def decode_tag_payload(text: str) -> str:
    """Reconstruct any ASCII smuggled via the Unicode Tag block (U+E0020..U+E007E).

    Tag space through tag tilde map to ASCII 0x20..0x7E. Non-printable tag chars (the
    deprecated language tag and cancel tag) are skipped. Returns "" if nothing is hidden.
    """
    out = []
    for ch in text:
        off = ord(ch) - _TAG_BASE
        if 0x20 <= off <= 0x7E:
            out.append(chr(off))
    return "".join(out)


def _vs_byte(cp: int) -> int | None:
    """The byte a variation selector encodes under the Sneaky Bits scheme, or None."""
    if 0xFE00 <= cp <= 0xFE0F:
        return cp - 0xFE00              # VS1..VS16  -> bytes 0..15
    if 0xE0100 <= cp <= 0xE01EF:
        return cp - 0xE0100 + 16        # VS17..VS256 -> bytes 16..255
    return None


def decode_variation_bytes(text: str) -> str:
    """Reconstruct arbitrary bytes smuggled in variation selectors (Sneaky Bits).

    Any byte can be encoded as a variation selector, so a run of them carries a hidden
    payload of any content, not just ASCII. See embracethered.com's Sneaky Bits. Variation
    selectors also legitimately follow emoji, so this returns "" unless the decoded bytes
    form at least three printable characters, which keeps a lone emoji selector from looking
    like a payload.
    """
    raw = bytes(b for b in (_vs_byte(ord(ch)) for ch in text) if b is not None)
    if not raw:
        return ""
    try:
        msg = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    printable = sum(ch.isprintable() for ch in msg)
    return msg if printable >= 3 else ""


# --------------------------------------------------------------------------- #
# Homoglyph confusables (UTS #39): letters from other scripts that look like ASCII
# --------------------------------------------------------------------------- #

# Cyrillic and Greek letters that render identically to a Latin letter in common fonts.
# A word like "pаypal" (Cyrillic a) reads as Latin but is a different string, which is both
# a classic spoof and a cheap way to fingerprint a document. NFKC does NOT fix these: they
# are separate scripts with no compatibility decomposition, so they need an explicit map.
_CONFUSABLES: dict[str, str] = {}
_CONFUSABLES.update(dict(zip(
    "АВЕКМНОРСТУХаеорсухіѕј",   # Cyrillic uppercase + lowercase lookalikes
    "ABEKMHOPCTYXaeopcyxisj",
)))
_CONFUSABLES.update(dict(zip(
    "ΑΒΕΗΙΚΜΝΟΡΤΥΧαοντ",        # Greek lookalikes
    "ABEHIKMNOPTYXaovt",
)))
# Fullwidth Latin/digits (also caught by --nfkc, kept here so --homoglyphs stands alone).
for _cp in range(0xFF21, 0xFF3B):
    _CONFUSABLES[chr(_cp)] = chr(_cp - 0xFF21 + ord("A"))
for _cp in range(0xFF41, 0xFF5B):
    _CONFUSABLES[chr(_cp)] = chr(_cp - 0xFF41 + ord("a"))
for _cp in range(0xFF10, 0xFF1A):
    _CONFUSABLES[chr(_cp)] = chr(_cp - 0xFF10 + ord("0"))


def _script(ch: str) -> str:
    """Coarse script bucket for a letter: latin, cyrillic, greek, or other."""
    cp = ord(ch)
    if 0x0400 <= cp <= 0x04FF:
        return "cyrillic"
    if 0x0370 <= cp <= 0x03FF:
        return "greek"
    if ch.isascii() and ch.isalpha():
        return "latin"
    if 0x00C0 <= cp <= 0x024F:  # Latin-1 Supplement + Latin Extended-A/B
        return "latin"
    return "other"


def fold_confusables(text: str) -> tuple[str, int]:
    """Map confusable Cyrillic/Greek/fullwidth letters back to ASCII. Returns (text, count)."""
    out = []
    n = 0
    for ch in text:
        repl = _CONFUSABLES.get(ch)
        if repl is not None:
            out.append(repl)
            n += 1
        else:
            out.append(ch)
    return "".join(out), n


def find_confusables(text: str) -> list[tuple[int, str, str, int]]:
    """[(codepoint, name, ascii_target, count)] for each confusable present."""
    counts: dict[str, int] = {}
    for ch in text:
        if ch in _CONFUSABLES:
            counts[ch] = counts.get(ch, 0) + 1
    rows = []
    for ch, n in sorted(counts.items(), key=lambda kv: ord(kv[0])):
        try:
            name = unicodedata.name(ch)
        except ValueError:
            name = "<unnamed>"
        rows.append((ord(ch), name, _CONFUSABLES[ch], n))
    return rows


def mixed_script_words(text: str) -> list[str]:
    """Words that mix scripts (e.g. Latin + Cyrillic), a strong homoglyph-spoof tell."""
    flagged = []
    for word in re.findall(r"\w+", text):
        scripts = {_script(c) for c in word if c.isalpha()}
        scripts.discard("other")
        if len(scripts) > 1:
            flagged.append(word)
    return flagged


# --------------------------------------------------------------------------- #
# Layer B: rewrite prompt + divergence scoring
# --------------------------------------------------------------------------- #

PROMPTS = {
    "paraphrase": (
        "Rewrite the text below so it uses substantially different wording at the "
        "token level. Change clause order, connectors, and transitions; vary sentence "
        "boundaries and length; replace content and function words where meaning allows. "
        "Preserve every fact, number, name, and technical identifier. Do not add or "
        "remove claims. Output only the rewritten text.\n\n---\n{TEXT}"
    ),
    "humanize": (
        "Rewrite the text below so it reads as if a person wrote it from scratch. Vary "
        "sentence rhythm and length, cut formulaic AI transitions and filler, use plain "
        "varied wording, and avoid em dashes. Preserve every fact, number, name, and "
        "technical identifier. Do not add or remove claims. Output only the rewritten "
        "text.\n\n---\n{TEXT}"
    ),
    "backtranslate": (
        "Translate the text below into French, then translate that French back into "
        "English. Preserve every fact, number, and name; use natural phrasing. Output "
        "only the final English text.\n\n---\n{TEXT}"
    ),
}


def build_prompt(strength: str, text: str) -> str:
    return PROMPTS[strength].format(TEXT=text)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", text.lower())


def _bigrams(toks: list[str]) -> set[tuple[str, str]]:
    return set(zip(toks, toks[1:]))


def lexical_divergence(original: str, candidate: str) -> float:
    """Bigram Jaccard distance: 0.0 identical wording, 1.0 fully reworded.

    Higher means more token re-picking, which is what strips the statistical
    watermark, so higher is better for our purpose.
    """
    a, b = _tokens(original), _tokens(candidate)
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    ba, bb = _bigrams(a), _bigrams(b)
    union = ba | bb
    if not union:
        return 0.0
    return 1.0 - len(ba & bb) / len(union)


def select_candidate(original: str, candidates: list[str]) -> tuple[int, list[float]]:
    """Pick the most-diverged rewrite, lightly penalizing extreme length drift."""
    scores: list[float] = []
    for cand in candidates:
        s = lexical_divergence(original, cand)
        if original:
            ratio = len(cand) / max(1, len(original))
            if ratio > 2.0 or ratio < 0.5:
                s -= 0.15
        scores.append(s)
    best = max(range(len(candidates)), key=lambda i: scores[i])
    return best, scores


# --------------------------------------------------------------------------- #
# Optional Gemini backend (off by default; key read from env only)
# --------------------------------------------------------------------------- #

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_MODEL = "gemini-flash-latest"
# Tried in order after the primary model when it stays overloaded. Kept small and
# all flash-class so quality and latency stay comparable to the default.
FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-flash-lite-latest")

# Codes worth retrying: rate limit and the transient server-overload family.
# 503 UNAVAILABLE ("model experiencing high demand") is the one declaw hits most.
RETRYABLE_CODES = frozenset({429, 500, 503})


def _retry_delay(err: urllib.error.HTTPError | None, attempt: int) -> float:
    """Seconds to wait before the next attempt (0-indexed).

    Honor a numeric Retry-After header when present; otherwise exponential
    backoff (1, 2, 4, 8 ...) capped at 30s so a busy model does not stall long.
    """
    if err is not None and err.headers:
        after = err.headers.get("Retry-After")
        if after and after.strip().isdigit():
            return min(float(after), 30.0)
    return min(2.0 ** attempt, 30.0)


def _gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "error: --backend gemini needs GEMINI_API_KEY in your environment. "
            "Set it yourself; declaw never takes a key on the command line."
        )
    return key


def _http_error_message(err: urllib.error.HTTPError) -> tuple[int, str]:
    """Pull (code, human message) out of a Gemini HTTPError.

    Google returns a JSON body like {"error":{"code":503,"message":"...",
    "status":"UNAVAILABLE"}}. Reading it turns an opaque traceback into the
    reason the caller actually needs.
    """
    body = ""
    try:
        body = err.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - body is best-effort context only
        pass
    try:
        return err.code, str(json.loads(body)["error"]["message"])
    except Exception:  # noqa: BLE001 - fall back to the raw snippet
        return err.code, (body[:300] or err.reason or "no error body")


def _gemini_error_help(code: int, msg: str) -> str:
    """A non-retryable failure, phrased with the fix, not just the code."""
    if code in (400, 401, 403):
        return (f"error: Gemini rejected the key ({code}): {msg}\n"
                "Check GEMINI_API_KEY and any key restrictions at aistudio.google.com/apikey.")
    if code == 404:
        return (f"error: model not found ({code}): {msg}\n"
                "Set GEMINI_MODEL or --model to an available one; run `declaw doctor` to list them.")
    return f"error: Gemini call failed ({code}): {msg}"


def _gemini_post(prompt: str, model: str, key: str, timeout: float) -> str:
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    body = json.dumps(payload).encode("utf-8")
    # Auth via the x-goog-api-key header, not a ?key= query param: the key never
    # lands in the URL, so proxies and access logs cannot capture it.
    req = urllib.request.Request(
        GEMINI_ENDPOINT.format(model=model),
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        return resp.read().decode("utf-8")


def _model_chain(primary: str | None = None) -> list[str]:
    """Primary model (explicit arg, else env override, else default) then the
    flash fallbacks, deduped and order-preserving."""
    chain = [primary or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)]
    for m in FALLBACK_MODELS:
        if m not in chain:
            chain.append(m)
    return chain


def gemini_rewrite(prompt: str, *, model: str | None = None, retries: int = 3,
                   timeout: float = 60) -> str:
    key = _gemini_key()
    last = ""
    for model in _model_chain(model):
        for attempt in range(retries + 1):
            try:
                return _extract_gemini_text(_gemini_post(prompt, model, key, timeout=timeout))
            except urllib.error.HTTPError as err:
                code, msg = _http_error_message(err)
                last = f"{model}: {code} {msg}"
                if code in RETRYABLE_CODES and attempt < retries:
                    delay = _retry_delay(err, attempt)
                    eprint(f"gemini {model}: {code} (overloaded), retry {attempt + 1}/{retries} in {delay:.0f}s")
                    time.sleep(delay)
                    continue
                if code in RETRYABLE_CODES:
                    eprint(f"gemini {model}: {code} after {retries} retries, trying next model")
                    break  # move on to the next model in the chain
                raise SystemExit(_gemini_error_help(code, msg))
    raise SystemExit(
        f"error: every Gemini model is overloaded right now ({last}).\n"
        "Try again later, or run `declaw prompt` and paste into the Gemini web app."
    )


def gemini_list_models(key: str, timeout: float = 30) -> list[str]:
    """Available model ids for this key (bare names, no 'models/' prefix).

    A successful call is also the cheapest proof the key itself is valid, which
    is what `doctor` needs to separate a bad key from an overloaded model.
    """
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        data = json.loads(resp.read().decode("utf-8"))
    return [m["name"].removeprefix("models/") for m in data.get("models", [])]


def _extract_gemini_text(raw: str) -> str:
    data = json.loads(raw)
    # A safety filter can reject the prompt outright: no candidates, just a
    # blockReason. Surface that plainly instead of an index error.
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise SystemExit(f"error: Gemini blocked the prompt (blockReason={block}). Try --strength humanize.")
    candidates = data.get("candidates") or []
    if not candidates:
        raise SystemExit(f"error: Gemini returned no candidates.\n{raw[:400]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        # A candidate with no text usually carries a finishReason (SAFETY, MAX_TOKENS).
        reason = candidates[0].get("finishReason", "unknown")
        raise SystemExit(f"error: Gemini returned an empty rewrite (finishReason={reason}).")
    return text


# --------------------------------------------------------------------------- #
# Clipboard (Windows via PowerShell; graceful elsewhere)
# --------------------------------------------------------------------------- #

def clip_get() -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard", "-Raw"],
            capture_output=True, text=True, encoding="utf-8", timeout=15,
        )
        return r.stdout or ""
    except FileNotFoundError:
        raise SystemExit("error: clipboard needs PowerShell (Windows). Use file mode instead.")


def clip_set(text: str) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$in=[Console]::In.ReadToEnd(); Set-Clipboard -Value $in"],
        input=text, text=True, encoding="utf-8", timeout=15,
    )


_STATE = Path(tempfile.gettempdir()) / "declaw_original.txt"


# --------------------------------------------------------------------------- #
# IO helpers
# --------------------------------------------------------------------------- #

def read_input(path: str) -> str:
    if path in (None, "-"):
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def write_output(text: str, out: str | None) -> None:
    if out:
        Path(out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def eprint(*a) -> None:
    print(*a, file=sys.stderr)


def load_dotenv() -> None:
    """Load KEY=VALUE lines from .env (beside this script, then cwd).

    Real environment variables always win, so nothing here overrides an export.
    """
    for base in (Path(__file__).resolve().parent, Path.cwd()):
        f = base / ".env"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_scrub(args) -> int:
    text = read_input(args.path)
    cleaned, st = clean_text(text, nfkc=args.nfkc)
    if args.homoglyphs:
        cleaned, folded = fold_confusables(cleaned)
        st["folded"] = folded
    out = args.output
    if out is None and args.path not in (None, "-"):
        p = Path(args.path)
        out = str(p.with_suffix(".cleaned" + p.suffix))
    write_output(cleaned, out)
    tail = f" folded={st['folded']}" if "folded" in st else ""
    eprint(f"removed={st['removed']} replaced={st['replaced']}{tail} len {st['in_len']}->{st['out_len']}")
    return 0


def cmd_inspect(args) -> int:
    text = read_input(args.path)
    rows = inspect_text(text)
    tag_payload = decode_tag_payload(text)
    vs_payload = decode_variation_bytes(text)
    confusables = find_confusables(text)
    mixed = sorted(set(mixed_script_words(text)))
    clean = not (rows or confusables or mixed or tag_payload or vs_payload)

    if args.json:
        report = {
            "clean": clean,
            "hidden": [{"codepoint": f"U+{cp:04X}", "name": name, "count": n}
                       for cp, name, n in rows],
            "confusables": [{"codepoint": f"U+{cp:04X}", "name": name, "ascii": a, "count": n}
                            for cp, name, a, n in confusables],
            "mixed_script_words": mixed,
            "tag_payload": tag_payload or None,
            "variation_selector_payload": vs_payload or None,
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if (args.check and not clean) else 0

    if clean:
        eprint("clean: no hidden characters found")
        return 0
    for cp, name, n in rows:
        eprint(f"U+{cp:04X}  x{n:<5} {name}")
    if rows:
        eprint(f"total kinds: {len(rows)}")
    if confusables:
        eprint("\nhomoglyph confusables (look like ASCII, are not):")
        for cp, name, ascii_target, n in confusables:
            eprint(f"U+{cp:04X}  x{n:<5} {name} -> {ascii_target!r}")
    if mixed:
        eprint(f"\nmixed-script words (homoglyph spoof tell): {', '.join(mixed[:8])}")
    # A decoded payload is not just noise: it is a message someone hid in the text.
    if tag_payload:
        eprint(f"\nWARNING: decoded ASCII smuggled in the Unicode Tag block:\n  {tag_payload!r}")
    if vs_payload:
        eprint(f"\nWARNING: decoded bytes smuggled in variation selectors:\n  {vs_payload!r}")
    return 1 if args.check else 0


def cmd_prompt(args) -> int:
    print(build_prompt(args.strength, read_input(args.path)))
    return 0


def cmd_score(args) -> int:
    original = Path(args.original).read_text(encoding="utf-8")
    cands = [Path(p).read_text(encoding="utf-8") for p in args.candidates]
    best, scores = select_candidate(original, cands)
    for i, (p, s) in enumerate(zip(args.candidates, scores)):
        mark = " <- best" if i == best else ""
        eprint(f"{p}: divergence={s:.3f}{mark}")
    if args.output:
        Path(args.output).write_text(cands[best], encoding="utf-8")
        eprint(f"wrote best to {args.output}")
    return 0


def cmd_web(args) -> int:
    if not args.finish:
        # Step 1: claude text on clipboard -> scrub -> build prompt -> clipboard
        text = clip_get()
        if not text.strip():
            eprint("clipboard empty. Copy your Claude text first.")
            return 2
        cleaned, st = clean_text(text)
        _STATE.write_text(cleaned, encoding="utf-8")
        prompt = build_prompt(args.strength, cleaned)
        backend = args.backend
        if backend == "auto":
            backend = "gemini" if os.environ.get("GEMINI_API_KEY", "").strip() else "print"
        if backend == "gemini":
            rewrite = gemini_rewrite(prompt, model=args.model, retries=args.retries, timeout=args.timeout)
            final, _ = clean_text(rewrite)
            div = lexical_divergence(cleaned, final)
            clip_set(final)
            eprint(f"scrubbed {st['removed']} hidden chars, Gemini rewrote, divergence={div:.3f}")
            eprint("clean text is on your clipboard. Paste anywhere.")
            return 0
        clip_set(prompt)
        eprint(f"scrubbed {st['removed']} hidden chars.")
        eprint("Rewrite prompt is on your clipboard. Paste it into the Gemini web app,")
        eprint("copy Gemini's answer, then run:  declaw web --finish")
        return 0
    # Step 2: gemini output on clipboard -> scrub -> score vs original -> clipboard
    rewrite = clip_get()
    if not rewrite.strip():
        eprint("clipboard empty. Copy Gemini's rewrite first.")
        return 2
    final, st = clean_text(rewrite)
    if _STATE.exists():
        original = _STATE.read_text(encoding="utf-8")
        div = lexical_divergence(original, final)
        eprint(f"divergence from original={div:.3f} " +
               ("(strong scrub)" if div > 0.6 else "(weak, rewrite harder)"))
    clip_set(final)
    eprint(f"scrubbed {st['removed']} hidden chars. Clean text is on your clipboard.")
    return 0


def cmd_rewrite(args) -> int:
    """Headless Layer B: scrub -> Gemini rewrite -> scrub, straight on a file or
    stdin. The clipboard `web` loop is for the browser; this is for scripts."""
    scrubbed, _ = clean_text(read_input(args.path))
    prompt = build_prompt(args.strength, scrubbed)
    rewrite = gemini_rewrite(prompt, model=args.model, retries=args.retries, timeout=args.timeout)
    final, _ = clean_text(rewrite)
    div = lexical_divergence(scrubbed, final)
    write_output(final, args.output)
    eprint(f"divergence={div:.3f} " + ("(strong)" if div > 0.6 else "(weak, try --strength humanize)"))
    return 0


def cmd_doctor(_args) -> int:
    """Diagnose the Gemini backend: is the key set, valid, and is a usable model
    available. Separates 'bad key' from 'model overloaded' so you stop guessing."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        eprint("key: MISSING. Set GEMINI_API_KEY (declaw reads it from env or .env).")
        return 1
    eprint(f"key: set ({len(key)} chars, starts {key[:4]}...)")
    try:
        available = set(gemini_list_models(key))
    except urllib.error.HTTPError as err:
        code, msg = _http_error_message(err)
        eprint(f"key: REJECTED ({code}): {msg}")
        eprint("Check GEMINI_API_KEY and any key restrictions at aistudio.google.com/apikey.")
        return 1
    except urllib.error.URLError as err:
        eprint(f"network: cannot reach Gemini ({err.reason}).")
        return 1
    eprint(f"key: valid, {len(available)} models available")
    chain = _model_chain()
    for m in chain:
        eprint(f"  model {m}: {'available' if m in available else 'NOT available'}")
    usable = [m for m in chain if m in available]
    if not usable:
        eprint("no configured model is available; set GEMINI_MODEL or --model to one that is.")
        return 1
    eprint(f"ok: will use {usable[0]} (then {', '.join(usable[1:]) or 'no fallback'}).")
    return 0


def cmd_selftest(_args) -> int:
    # Layer A removes zero-width + variation selector, keeps real text.
    dirty = "Hel​lo️ wor⁣ld‍"
    cleaned, st = clean_text(dirty)
    assert cleaned == "Hello world", repr(cleaned)
    assert st["removed"] == 4, st
    # exotic space becomes normal space
    c2, _ = clean_text("a b")
    assert c2 == "a b", repr(c2)
    # inspect finds them
    assert len(inspect_text(dirty)) == 4
    # divergence: identical low, reworded high
    orig = "the cat sat on the warm mat by the door"
    assert lexical_divergence(orig, orig) == 0.0
    reworded = "a feline rested near the entrance on a heated rug"
    assert lexical_divergence(orig, reworded) > 0.9
    # selector picks the most diverged candidate
    best, _ = select_candidate(orig, [orig, reworded])
    assert best == 1
    # backoff: exponential from 1s, capped at 30s; None err = no Retry-After
    assert [_retry_delay(None, a) for a in (0, 1, 2, 3)] == [1.0, 2.0, 4.0, 8.0]
    assert _retry_delay(None, 20) == 30.0
    import types
    assert _retry_delay(types.SimpleNamespace(headers={"Retry-After": "5"}), 0) == 5.0
    # model chain: primary first, fallbacks appended, deduped
    assert _model_chain("gemini-flash-latest")[0] == "gemini-flash-latest"
    assert _model_chain("gemini-2.5-flash").count("gemini-2.5-flash") == 1
    # blocked / empty responses raise a clear SystemExit, not an IndexError
    for raw in ('{"promptFeedback":{"blockReason":"SAFETY"}}', '{"candidates":[]}'):
        try:
            _extract_gemini_text(raw)
            assert False, "expected SystemExit"
        except SystemExit:
            pass
    # non-retryable help names the fix
    assert "key" in _gemini_error_help(403, "x").lower()
    assert "model" in _gemini_error_help(404, "x").lower()
    # ASCII smuggling: hidden tag-block payload is decoded, and scrub removes it
    smuggled = "hi" + "".join(chr(_TAG_BASE + ord(c)) for c in "secret")
    assert decode_tag_payload(smuggled) == "secret"
    assert clean_text(smuggled)[0] == "hi"
    assert decode_tag_payload("plain text") == ""
    # Sneaky Bits: bytes smuggled in variation selectors decode back, emoji selector does not
    bits = "".join(chr(0xFE00 + b) if b < 16 else chr(0xE0100 + b - 16) for b in b"hey")
    assert decode_variation_bytes(bits) == "hey"
    assert decode_variation_bytes("ok" + chr(0xFE0F)) == ""
    # Homoglyphs: Cyrillic lookalikes fold to ASCII, mixed-script word is flagged
    spoof = "pаypal"  # Cyrillic a inside "paypal"
    assert fold_confusables(spoof)[0] == "paypal"
    assert fold_confusables("paypal") == ("paypal", 0)
    flagged = mixed_script_words(spoof)
    assert len(flagged) == 1 and fold_confusables(flagged[0])[0] == "paypal"
    assert mixed_script_words("plain english words") == []
    print("selftest ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="declaw", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrub", help="Layer A invisible-char scrub")
    s.add_argument("path", nargs="?", default="-")
    s.add_argument("-o", "--output")
    s.add_argument("--nfkc", action="store_true", help="apply NFKC normalization after scrub")
    s.add_argument("--homoglyphs", action="store_true",
                   help="fold confusable Cyrillic/Greek/fullwidth letters back to ASCII")
    s.set_defaults(func=cmd_scrub)

    i = sub.add_parser("inspect", help="report hidden chars, change nothing")
    i.add_argument("path", nargs="?", default="-")
    i.add_argument("--json", action="store_true", help="emit a machine-readable JSON report")
    i.add_argument("--check", action="store_true",
                   help="exit 1 if anything hidden is found (gate documents in CI)")
    i.set_defaults(func=cmd_inspect)

    pr = sub.add_parser("prompt", help="emit the Gemini rewrite prompt")
    pr.add_argument("path", nargs="?", default="-")
    pr.add_argument("--strength", choices=list(PROMPTS), default="paraphrase")
    pr.set_defaults(func=cmd_prompt)

    sc = sub.add_parser("score", help="rank rewrite candidates by divergence")
    sc.add_argument("original")
    sc.add_argument("candidates", nargs="+")
    sc.add_argument("-o", "--output", help="write the best candidate here")
    sc.set_defaults(func=cmd_score)

    w = sub.add_parser("web", help="clipboard loop for the browser")
    w.add_argument("--finish", action="store_true", help="second step: clean Gemini output")
    w.add_argument("--strength", choices=list(PROMPTS), default="paraphrase")
    w.add_argument("--backend", choices=["auto", "print", "gemini"], default="auto",
                   help="auto (gemini if GEMINI_API_KEY set, else print), print, or gemini")
    w.add_argument("--model", default=None, help="Gemini model (default gemini-flash-latest or $GEMINI_MODEL)")
    w.add_argument("--retries", type=int, default=3, help="retries per model on overload (default 3)")
    w.add_argument("--timeout", type=float, default=60, help="per-request timeout seconds (default 60)")
    w.set_defaults(func=cmd_web)

    rw = sub.add_parser("rewrite", help="headless Layer B: scrub + Gemini rewrite a file/stdin")
    rw.add_argument("path", nargs="?", default="-")
    rw.add_argument("-o", "--output")
    rw.add_argument("--strength", choices=list(PROMPTS), default="paraphrase")
    rw.add_argument("--model", default=None, help="Gemini model (default gemini-flash-latest or $GEMINI_MODEL)")
    rw.add_argument("--retries", type=int, default=3, help="retries per model on overload (default 3)")
    rw.add_argument("--timeout", type=float, default=60, help="per-request timeout seconds (default 60)")
    rw.set_defaults(func=cmd_rewrite)

    sub.add_parser("doctor", help="check the Gemini key and model availability").set_defaults(func=cmd_doctor)
    sub.add_parser("selftest", help="run built-in asserts").set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    # Text tools emit non-ASCII (accents, decoded payloads, other scripts). A Windows
    # console defaults to cp1252 and crashes on those, so force UTF-8 on our streams.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # already wrapped, or not a real stream
            pass
    load_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
