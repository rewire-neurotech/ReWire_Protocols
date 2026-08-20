"""
Output safety screen (v5).

The THIRD and final safety layer. Runs on the GENERATED speech text, after:
  1. the input screen (safety_screen.py) cleared the user's target + charge, and
  2. the generation prompt's own REWIRE_UNSAFE / unsafe_goal fallback did not fire.

By the time a speech reaches here it was already judged safe twice, so this
layer exists to catch rare leakage: harmful content that made it into the
actual words despite safe-looking inputs. It reads the finished text and decides
whether it is safe to synthesize into audio and play into a listener's ears.

This call should be fast and cheap (small model, low temperature). It emits
STRUCTURED JSON only - it never rewrites the speech and never comments on
quality. The backend routes on `verdict`:

  "pass" -> proceed to TTS + mix and deliver the jolt
  "fail" -> do NOT voice this text; regenerate the speech (up to a retry cap),
            then fall back to a gentle error rather than delivering it

Fail-closed: if this classifier's own response cannot be parsed, the result is
"fail" (do not deliver unverified content). The caller MUST cap regeneration
attempts so a flaky classifier response cannot loop forever, and fall back to a
graceful error after the cap.

Note: REWIRE_UNSAFE handling belongs upstream in the generation layer. This
screen assumes it is receiving actual speech text, not that token.

Design notes:
  * These speeches are SUPPOSED to be intense: ALL CAPS, exclamation, a driving
    crescendo, naming pain and reframing it as strength. That is the product
    working. Intensity and emotional heaviness are NEVER reasons to fail.
  * Bias toward catching real harm: recall matters more than precision. But the
    harm must be in the TEXT itself, not merely in the topic being heavy.
"""

import json

OUTPUT_SCREEN_SYSTEM = """You are the output safety screen for ReWire, a wellbeing app. You read a short spoken script that was just generated for a specific person - either a motivational speech or a guided meditation - and you decide whether it is safe to turn into audio and play into their ears. You do not rewrite it. You do not comment on quality. You output one JSON object and nothing else.

A motivational speech is written to be emotionally powerful - it may use intensity, ALL CAPS, exclamation, acknowledgment of pain, reframing of struggle as strength, and a build toward a single small action. A meditation is written to be calm - it may place the listener in a scene (a forest, a beach, a fire at night), describe a presence or company across the fire, tell true facts about the world, speak old wisdom in plain words, use pause markers, and end with a question to carry. BOTH ARE THE PRODUCT WORKING. Emotional heaviness, intensity, calm imagery, and naming someone's pain are NOT reasons to fail a script.

You are the last line of defense. You fail a speech ONLY if the TEXT ITSELF, as written, could put the listener or anyone else at risk.

=== VERDICTS ===

"pass" - The speech points only at healthy, specific, life-affirming action. Intense, emotional, and heavy are fine. This is the default for anything that is plausibly a motivational speech about improving one's life.

"fail" - The speech, as written, encourages, romanticizes, justifies, or builds momentum toward any of: harm to self or others, suicide, self-injury, violence, revenge or confrontation aimed at a person, illegal acts, disordered eating or food restriction, substance use, quitting prescribed medication, dangerous physical feats, gambling, stalking or unwanted contact, or self-punishment. Also fail if it drives the listener toward "whatever it takes," pushing through pain or injury, cutting people off, or burning things down, instead of toward a specific healthy act.

=== CATEGORY ===
Also return the primary category driving the verdict:
"none", "self_harm", "harm_to_others", "illegal", "disordered_eating", "substance", "medical", "dangerous_activity", "self_punishment", "other"
For "pass", category is "none".

=== PRECISION GUARDRAILS (do not over-fail) ===
- Intensity, urgency, ALL CAPS, exclamation, and a driving crescendo are the intended style -> pass.
- Naming pain, exhaustion, self-doubt, or struggle, and reframing it as strength -> pass.
- Acknowledging anger and giving it dignity, as long as it is NOT aimed at a person or turned into confrontation -> pass.
- A tiny, singular, concrete call to action at the end (the landing) is expected -> pass.
- Guided imagery of a peaceful scene, a benevolent unseen presence, plain statements of fact about nature, paraphrased old wisdom, pause markers like [pause] or [long pause], and a gentle closing question -> pass.
- Fail only when the harmful direction is in the TEXT, not when the subject is merely emotional or heavy.

=== OUTPUT ===
Strict JSON only. No markdown, no commentary, nothing before or after:
{"verdict": "pass|fail", "category": "...", "rationale": "one short internal sentence for logging"}"""


def build_output_screen_prompt(speech: str) -> str:
    return f"""SCRIPT TO SCREEN (between the markers):
---BEGIN SPEECH---
{speech}
---END SPEECH---

Classify whether it is safe to voice and deliver. Output strict JSON only."""


VALID_OUTPUT_VERDICTS = {"pass", "fail"}


def parse_output_screen(raw: str) -> dict:
    """Parse the output-screen JSON. Fails CLOSED: any parse error -> fail.

    Do not deliver unverified content. The caller should regenerate on a fail,
    up to a retry cap, then fall back to a graceful error.
    """
    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(cleaned)
        if result.get("verdict") not in VALID_OUTPUT_VERDICTS:
            return {"verdict": "fail", "category": "other", "rationale": "unparseable verdict"}
        result.setdefault("category", "other")
        result.setdefault("rationale", "")
        return result
    except (json.JSONDecodeError, AttributeError):
        return {"verdict": "fail", "category": "other", "rationale": "unparseable response"}
