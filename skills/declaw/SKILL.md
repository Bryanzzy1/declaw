---
name: declaw
description: Strip AI provenance from text you own, for privacy and hygiene. Runs the full clean pipeline on prose the user wants de-watermarked and human-sounding, invisible-character scrub plus a peer-model rewrite that removes the statistical token watermark. Use when the user says "declaw", "strip the watermark", "de-watermark this", "clean this text", "remove AI marks", or asks to make Claude output untraceable and natural. Text only, no files.
---

# declaw

Take text the user owns and return it human-sounding and free of AI provenance marks. Two marks, two layers, both handled here.

## When this triggers
- The user asks to strip, remove, or clean an AI watermark from text.
- The user pastes Claude or other AI output and wants it de-watermarked and natural.
- The user says "declaw" or "clean this."

## The pipeline (run in order)
1. **Style.** Invoke the `humanizer` skill first so the prose reads human. Skip only if the text is already the user's own voice.
2. **Layer A, deterministic.** Run `python declaw.py scrub` on the draft. Removes invisible Unicode: zero-width, variation selectors, tag chars, bidi, exotic spaces. Zero quality cost. Do this even on your own drafts, it also cleans anything a later model adds.
3. **Layer B, statistical.** The token-choice watermark only comes out with a real rewrite through a model that does not share the key.
   - If `GEMINI_API_KEY` is set, run `python declaw.py web --backend gemini` (or feed the prompt from `declaw prompt` to Gemini) to rewrite automatically.
   - Otherwise run `python declaw.py prompt --strength paraphrase`, give the user the prompt to paste into the Gemini web app, and take the result back.
4. **Pick best.** If you gathered more than one rewrite, `python declaw.py score original.txt cand1.txt cand2.txt -o final.txt`. Higher divergence means more of the watermark destroyed.
5. **Re-scrub and deliver.** Run `declaw scrub` once more on the winner. Deliver the clean text.

## Browser workflow (claude.ai web)
The web app cannot run skills, so drive it from the clipboard:
1. Copy the Claude output.
2. `python declaw.py web` scrubs it and puts a Gemini rewrite prompt on the clipboard (or rewrites directly if the key is set).
3. Paste into Gemini, copy the answer.
4. `python declaw.py web --finish` scrubs the answer, scores divergence, and copies the clean text back.

## Honest limits
- Cannot certify the vendor's secret detector will fail. Removal is best-effort by rewrite, the same ceiling every tool has.
- A weak local paraphraser removes less signal than a peer model. Prefer Gemini or an equal-strength model.
- Rewrite all of the text, not part. Untouched spans keep their mark.
- No file, image, or metadata handling. Text only.
