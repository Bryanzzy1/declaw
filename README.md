# declaw

Strip AI provenance from text you own, for privacy and hygiene. Text only, stdlib Python, no dependencies.

Two marks ride in AI text, and they need two different tools:

| Layer | Mark | How declaw handles it |
| --- | --- | --- |
| **A** | Invisible Unicode (zero-width, variation selectors, tag chars, bidi, exotic spaces) | Deterministic scrub, zero quality cost |
| **B** | Statistical token-choice watermark (green-list logit bias, SynthID-Text and Kirchenbauer family) | Rewrite through a peer model, then pick the most token-diverged result |

Layer A is a clean delete. Layer B cannot be deleted, the mark lives in word choice, so the only real removal is a full rewrite by a model that does not share the secret key. declaw does the deterministic part itself and drives the rewrite part through Gemini.

## Why a rewrite, and why a strong one

The watermark biases which tokens the model picks. A detector counts how many "green" tokens appear and runs a z-test. Editing a few words leaves the signal. Re-generating the text through a different strong model re-picks every token, so the green rate falls back to chance and the z-test fails. A weak local model removes less and costs more quality, so use a peer model like Gemini. See the research: [Watermark Stealing](https://watermark-stealing.org/), [Black-Box Detection of Watermarks](https://github.com/eth-sri/watermark-detection), and the origin paper Kirchenbauer et al. (arXiv:2301.10226).

## Install

```bash
git clone https://github.com/Bryanzzy1/declaw
cd declaw
python declaw.py selftest   # should print: selftest ok
```

Optional, for the automated rewrite path: copy `.env.example` to `.env` and paste your [Gemini API key](https://aistudio.google.com/apikey). The key is read from `.env` or the environment only, never from the command line, and `.env` is gitignored.

## Use

```bash
# Layer A: strip invisible characters
python declaw.py scrub draft.md -o draft.clean.md
python declaw.py inspect draft.md          # just report what is hidden

# Layer B: get the rewrite prompt to paste into Gemini
python declaw.py prompt draft.md --strength paraphrase

# Pick the best of several Gemini rewrites
python declaw.py score draft.md cand1.txt cand2.txt -o final.txt
```

### Browser loop (claude.ai web + Gemini web)

The web apps cannot run scripts, so declaw uses the clipboard:

```bash
# 1. copy your Claude output, then:
python declaw.py web            # scrubs, puts a Gemini prompt on your clipboard
                                # (or rewrites directly if GEMINI_API_KEY is set)
# 2. paste into Gemini, copy its answer, then:
python declaw.py web --finish   # scrubs the answer, scores it, copies clean text back
```

## Honest limits

- No tool can certify a vendor's secret detector will fail after removal. This is best-effort.
- Rewrite all of the text, not part. Untouched spans keep their mark.
- Files, images, and their C2PA / EXIF metadata are out of scope. Text only.

## Credit

Layer split and the rewrite-plus-score approach follow the design of [guillaumemeyer/watermarks-remover](https://github.com/guillaumemeyer/watermarks-remover). declaw is an independent, minimal, text-only take.

## License

MIT
