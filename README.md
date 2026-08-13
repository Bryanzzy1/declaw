# declaw

Remove AI provenance from text you own. Stdlib Python, no dependencies, text only.

AI text carries two marks, and they need different tools:

| Layer | Mark | Fix |
| --- | --- | --- |
| **A** | Invisible Unicode (zero-width, variation selectors, tag chars, bidi) | Deterministic scrub |
| **B** | Statistical token watermark (SynthID-Text, Kirchenbauer family) | Rewrite through a peer model |

Layer A you just delete. Layer B is baked in as the text is generated, so nothing short of a full rewrite by another model gets it out. declaw handles A itself and runs B through Gemini.

## Setup

```bash
git clone https://github.com/Bryanzzy1/declaw && cd declaw
python declaw.py selftest        # prints: selftest ok
```

For the automated rewrite: copy `.env.example` to `.env` and paste a [Gemini key](https://aistudio.google.com/apikey). The key is read from `.env` only and stays gitignored.

## Use

```bash
python declaw.py scrub draft.md -o clean.md     # Layer A
python declaw.py prompt draft.md                # Layer B prompt for Gemini
python declaw.py score draft.md a.txt b.txt     # pick the most reworded rewrite
```

Browser loop (claude.ai + Gemini, clipboard driven):

```bash
python declaw.py web            # copy Claude output first; scrubs, then rewrites (key) or hands you a prompt
python declaw.py web --finish   # copy Gemini's answer first; scrubs, scores, copies clean text back
```

## Limits

Best-effort. No tool can prove a vendor's private detector fails. Rewrite all of the text, not part. No files or metadata.

Design follows [watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover). Background: [Watermark Stealing](https://watermark-stealing.org/), Kirchenbauer et al. ([arXiv:2301.10226](https://arxiv.org/abs/2301.10226)).

MIT.
