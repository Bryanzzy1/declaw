# declaw

Strip AI fingerprints from text you wrote with an assistant. Pure Python, no dependencies, text only.

AI text carries two hidden marks, and each needs a different fix:

| Layer | Mark | Fix |
| --- | --- | --- |
| **A** | Invisible Unicode (zero-width chars, variation selectors, tag chars, bidi) | Delete them |
| **B** | Statistical token watermark (SynthID-Text, Kirchenbauer family) | Rewrite through another model |

Layer A you just delete. Layer B gets baked in while the model writes, so the only way out is a full rewrite by a model that does not share the secret key. declaw does Layer A itself and runs Layer B through Gemini.

## Setup

```bash
git clone https://github.com/Bryanzzy1/declaw && cd declaw
python declaw.py selftest        # should print: selftest ok
```

Want the automated rewrite? Copy `.env.example` to `.env` and drop in a [Gemini key](https://aistudio.google.com/apikey). declaw reads it from `.env` only, and `.env` stays out of git.

## Use

```bash
python declaw.py scrub draft.md -o clean.md     # Layer A
python declaw.py prompt draft.md                # Layer B: a rewrite prompt for Gemini
python declaw.py score draft.md a.txt b.txt     # keep the most-reworded rewrite
```

Working in the browser (claude.ai plus Gemini)? Drive it from the clipboard:

```bash
python declaw.py web            # copy Claude's output first. Scrubs, then rewrites if a key is set, else hands you a prompt
python declaw.py web --finish   # copy Gemini's answer first. Scrubs, scores, copies the clean text back
```

## Limits

This is best-effort. No tool can prove a vendor's private detector will miss the result. Rewrite the whole thing, not a few lines. Files and their metadata are out of scope.

Design follows [watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover). For the research, see [Watermark Stealing](https://watermark-stealing.org/) and Kirchenbauer et al. ([arXiv:2301.10226](https://arxiv.org/abs/2301.10226)).

MIT.
