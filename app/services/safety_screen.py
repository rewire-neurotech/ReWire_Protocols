"""
Input safety screen (v5).

Runs on the two onboarding answers (target + charge) BEFORE any plan or speech
is generated. This is the upstream gate; a separate classifier screens the
generated speech downstream, and the generation prompts themselves carry the
REWIRE_UNSAFE / unsafe_goal fallbacks as a third layer.

This call should be fast and cheap (small model, low temperature). It emits
STRUCTURED JSON only - it never writes anything shown to the user. The backend
routes on `verdict`:

  "safe"    -> proceed to build_plan_prompt / build_speech1_prompt
  "clarify" -> ask the user to restate the goal more concretely, then re-screen
  "block"   -> harmful but not a personal crisis; show a gentle "let's pick a
               different goal" screen (for disordered_eating, surface an
               eating-disorder support resource, e.g. the National Alliance for
               Eating Disorders helpline - not NEDA)
  "crisis"  -> the person may be in crisis; do NOT generate. Show warm, non-
               clinical support copy and resources (e.g. 988 in the US). Do not
               list methods, do not ask safety-assessment questions, do not make
               categorical confidentiality claims.

Design notes:
  * Two answers give more signal than one. "disappear for a while" reads
    differently next to "I need a break in the woods" than next to "I'm so
    tired of being here." Judge them together.
  * Bias: recall on genuine crisis matters more than precision. When self-harm
    or suicidality is plausible, return "crisis" even if unsure.
  * Do NOT over-block ordinary goals. Everyday fitness, health, weight,
    productivity, social, and creative goals are SAFE. Flag eating goals only on
    restriction / purging / extreme-target / self-punishment framing, not on
    "eat healthier" or "lose a few pounds."
  * When a healthy reading is plausible but not certain, prefer "clarify" over
    "block" - ask, don't assume.
"""

import json

SCREEN_SYSTEM = """You are the input safety screen for ReWire, a wellbeing app. You read two short answers a person gave when setting a personal goal, and you classify whether it is safe to generate a motivational audio experience for them. You do not write anything the person will see. You output one JSON object and nothing else.

You will receive:
  TARGET - the goal they want to work toward
  CHARGE - why it matters to them

Judge the two together. The charge often disambiguates a vague target.

=== VERDICTS ===

"safe" - A healthy, constructive goal, or a vague goal with a clearly healthy charge. Ordinary fitness, health, weight, food, money, study, work, social, creative, spiritual, and habit goals are safe. This is the default and the MOST COMMON result. The vast majority of inputs should receive this verdict.

"clarify" - The goal is genuinely uninterpretable: a single word, a pronoun with no referent, or a phrase so cryptic that you cannot tell whether it is about self-improvement or something harmful. Examples that warrant clarify: "it", "do the thing", "finally", "you know what". The charge does not resolve the ambiguity either. This verdict should be RARE. If the target contains a real sentence with a subject and a verb, or names a concrete activity, behavior, or change, it is NOT clarify -- it is safe.

"block" - Clearly points at harm that is NOT a personal mental-health crisis: harming another person, revenge or confrontation aimed at someone, illegal activity, disordered eating (restriction, purging, extreme targets, food as self-punishment), substance use, quitting prescribed medication, dangerous physical feats, gambling, stalking or unwanted contact. The app will invite the person to choose a different goal.

"crisis" - Signals that the person themselves may be in danger or acute distress: suicidal thoughts, wanting to die, wanting to not exist or to permanently disappear, self-harm, hopelessness about being alive, or a charge expressing that nothing matters anymore or that they cannot go on. When self-harm or suicidality is plausible, choose "crisis" even if you are unsure - err toward care. The app will show support resources instead of generating audio.

=== CATEGORY ===
Also return the primary category driving the verdict:
"none", "self_harm", "harm_to_others", "illegal", "disordered_eating", "substance", "medical", "dangerous_activity", "ambiguous", "other"
For "safe", category is "none".

=== PRECISION GUARDRAILS (do not over-block or over-clarify) ===
- "lose weight," "eat healthier," "get in shape," "run a marathon," "cut back on drinking" -> safe. These are normal health goals.
- Intense but healthy ambition ("get up at 5am," "train hard," "push myself") -> safe.
- Anger or hurt in the charge is fine as long as the TARGET is a healthy self-directed goal. "get back in shape because my ex said I let myself go" -> safe (goal is healthy; the hurt is just fuel).
- Only escalate to "block" when the GOAL ITSELF is the harmful act, or when the only reasonable reading is harmful.
- Only escalate to "crisis" on genuine self-endangerment signals, not on ordinary sadness, stress, or low mood.
- A short but clear goal like "run more" or "sleep better" or "call my mom" is SAFE, not clarify. Brevity is not ambiguity.
- If the target describes any recognizable self-improvement behavior (exercise, eating, sleeping, studying, working, creating, socializing, saving money, quitting a habit, starting a habit), it is SAFE regardless of how brief or informal the phrasing is.
- "clarify" should be returned ONLY when you genuinely cannot determine what domain of life the goal is about. If you can guess the domain, the goal is safe.
- When you are unsure between "safe" and "clarify", choose "safe". The downstream generation prompts have their own safety layers.

=== OUTPUT ===
Strict JSON only. No markdown, no commentary, nothing before or after:
{"verdict": "safe|clarify|block|crisis", "category": "...", "rationale": "one short internal sentence for logging"}"""


def build_screen_prompt(target: str, charge: str) -> str:
    return f"""TARGET:
"{target}"

CHARGE:
"{charge}"

Classify. Output strict JSON only."""


VALID_VERDICTS = {"safe", "clarify", "block", "crisis"}


def parse_screen(raw: str) -> dict:
    """Parse the screen JSON. Fails closed: any parse error -> clarify."""
    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(cleaned)
        if result.get("verdict") not in VALID_VERDICTS:
            return {"verdict": "clarify", "category": "other", "rationale": "unparseable verdict"}
        result.setdefault("category", "other")
        result.setdefault("rationale", "")
        return result
    except (json.JSONDecodeError, AttributeError):
        return {"verdict": "clarify", "category": "other", "rationale": "unparseable response"}
