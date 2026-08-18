# declaw

Strip AI fingerprints from text you wrote with an assistant. Pure Python, no dependencies, text only.

AI and adversarial text carry marks in three places, and each needs a different fix:

| Layer | Mark | What declaw does |
| --- | --- | --- |
| **A1** | Invisible Unicode: zero-width chars, variation selectors, tag chars, bidi, blank glyphs | Delete them |
| **A2** | Homoglyph confusables: Cyrillic/Greek letters that look like ASCII | Fold to ASCII, flag mixed-script words |
| **A3** | Smuggled payloads: ASCII hidden in the Tag block, bytes hidden in variation selectors | Decode and show what was hidden |
| **B** | Statistical token watermark (SynthID-Text, Kirchenbauer family) | Rewrite through another model |

Layers A1-A3 are deterministic: delete, fold, or decode. Layer B gets baked in while the model writes, so the only way out is a full rewrite by a model that does not share the secret key. declaw does the A layers itself and runs Layer B through Gemini.

## Why each layer exists

- **Invisible Unicode** is the easy fingerprint: characters that render as nothing but survive copy-paste. declaw removes the format-category ones and the sneakier blank glyphs (braille blank, Hangul fillers) that a naive "strip the format chars" pass misses.
- **Homoglyphs** are letters from another script that look identical, like the Cyrillic `а` in `pаypal`. NFKC does not fix them because they are separate scripts, so declaw carries an explicit confusables map ([UTS #39](https://www.unicode.org/reports/tr39/)).
- **Smuggled payloads** hide a whole message in text that looks clean. The Unicode Tag block maps every printable ASCII character to an invisible twin, and variation selectors can encode arbitrary bytes. declaw does not just delete these, it decodes them, so `inspect` tells you a document was carrying `ignore prior instructions` before you trust it. See [ASCII Smuggler](https://embracethered.com/blog/posts/2024/hiding-and-finding-text-with-unicode-tags/) and [Sneaky Bits](https://embracethered.com/blog/posts/2025/sneaky-bits-and-ascii-smuggler/).
- **The token watermark** ([SynthID-Text](https://deepmind.google/blog/watermarking-ai-generated-text-and-video-with-synthid/)) biases which tokens the model picks. Each word looks natural; the bias only shows across a passage. It weakens against paraphrase and translation, which is exactly Layer B.

## Setup

```bash
git clone https://github.com/Bryanzzy1/declaw && cd declaw
python declaw.py selftest        # should print: selftest ok
```

Or install it as a command:

```bash
pip install .
declaw selftest
```

Want the automated rewrite? Copy `.env.example` to `.env` and drop in a [Gemini key](https://aistudio.google.com/apikey). declaw reads it from `.env` only, and `.env` stays out of git. The key travels in the `x-goog-api-key` header, never in the URL.

Not sure the key or model is good? Run `declaw doctor`. It reports whether the key is valid and which models are available, so you can tell a rejected key from an overloaded model. When Gemini answers `503` ("high demand"), declaw retries with backoff and falls back across flash models on its own.

## Use

```bash
declaw inspect draft.md                 # report hidden chars, homoglyphs, and decode any smuggled payload
declaw inspect --json draft.md          # the same, machine-readable
declaw inspect --check draft.md         # exit 1 if anything hidden is found (gate docs in CI)
declaw scrub draft.md -o clean.md       # Layer A1: delete invisibles
declaw scrub --homoglyphs draft.md      # also fold Cyrillic/Greek/fullwidth lookalikes to ASCII
declaw prompt draft.md                  # Layer B: a rewrite prompt for Gemini
declaw rewrite draft.md -o clean.md     # Layer B end to end, no clipboard (needs a key)
declaw score draft.md a.txt b.txt       # keep the most-reworded rewrite
declaw verify draft.md clean.md         # check the rewrite kept every number and name
```

`rewrite` and `web` run the fact check automatically and warn if the rewrite changed a number, the one drift a paraphrase must never make.

Working in the browser (claude.ai plus Gemini)? Drive it from the clipboard:

```bash
declaw web            # copy Claude's output first. Scrubs, then rewrites if a key is set, else hands you a prompt
declaw web --finish   # copy Gemini's answer first. Scrubs, scores, copies the clean text back
```

## Limits

This is best-effort. No tool can prove a vendor's private detector will miss the result. Rewrite the whole thing, not a few lines. Files and their metadata are out of scope. The homoglyph map covers the common Latin lookalikes, not every confusable in UTS #39.

Design follows [watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover). For the research, see [Watermark Stealing](https://watermark-stealing.org/) and Kirchenbauer et al. ([arXiv:2301.10226](https://arxiv.org/abs/2301.10226)).

MIT.
