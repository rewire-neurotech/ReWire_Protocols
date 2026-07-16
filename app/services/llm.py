import json
import time
import anthropic
from app.core.config import cfg
from app.services.prompt import SYSTEM_PROMPT

_RETRYABLE = {429, 500, 502, 503, 504, 529}


class SafetyHalt(Exception):
    """raised when claude outputs SAFETY_HALT instead of a speech."""
    def __init__(self, reason=""):
        self.reason = reason


def generate_speech(user_prompt: str, max_retries: int = 4, backoff_base: float = 2.0) -> str:
    if cfg.DEV_MODE:
        return ("You set a goal. That matters. Most people never even get that far. "
                "They stay in the comfort of dreaming without ever writing it down. "
                "But you did. You made it real. Now here is what I need you to understand. "
                "The gap between where you are and where you want to be is not filled with talent. "
                "It is filled with showing up. Again. And again. Even when it is boring. "
                "Even when nobody is watching. Especially then. So do not wait for motivation. "
                "Start before you are ready. Start messy. Start scared. Just start. "
                "Because the version of you that finishes this is already in the room. "
                "You just have to let them take over. Now go. Your first step is waiting.")

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=cfg.CLAUDE_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )

            text = ""
            for block in msg.content:
                if block.type == "text":
                    text += block.text
            text = text.strip()

            if text.startswith("SAFETY_HALT"):
                reason = text.replace("SAFETY_HALT", "", 1).strip()
                raise SafetyHalt(reason)

            return text

        except SafetyHalt:
            raise

        except Exception as e:
            last_exc = e
            status = getattr(e, "status_code", None)

            if status in _RETRYABLE:
                if attempt < max_retries:
                    sleep_s = backoff_base ** attempt
                    if status == 429:
                        sleep_s = backoff_base ** (attempt + 1)
                    print(f"[LLM] {status}, retry {attempt}/{max_retries} in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                else:
                    raise

            if status is None and ("connect" in str(e).lower() or "timeout" in str(e).lower()):
                if attempt < max_retries:
                    sleep_s = backoff_base ** attempt
                    print(f"[LLM] connection error, retry {attempt}/{max_retries} in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                else:
                    raise

            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("unknown LLM error")


def call_claude(system_prompt: str, user_message: str = "Generate.", max_tokens: int = 100, max_retries: int = 3, backoff_base: float = 1.5) -> str:
    """lightweight claude call for short generations."""
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=cfg.CLAUDE_MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            text = ""
            for block in msg.content:
                if block.type == "text":
                    text += block.text
            return text.strip()

        except Exception as e:
            last_exc = e
            status = getattr(e, "status_code", None)

            if status in _RETRYABLE:
                if attempt < max_retries:
                    sleep_s = backoff_base ** attempt
                    if status == 429:
                        sleep_s = backoff_base ** (attempt + 1)
                    print(f"[LLM] call_claude {status}, retry {attempt}/{max_retries} in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                else:
                    raise

            if status is None and ("connect" in str(e).lower() or "timeout" in str(e).lower()):
                if attempt < max_retries:
                    sleep_s = backoff_base ** attempt
                    print(f"[LLM] call_claude connection error, retry {attempt}/{max_retries} in {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    continue
                else:
                    raise

            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("unknown LLM error")


# ---------------------------------------------------------------------------
# Goal title cleanup (v4)
# ---------------------------------------------------------------------------

_TITLE_SYSTEM_PROMPT = """You convert a user's freeform entry into a short goal title for a mental health app.
Your job: identify THE THING TO BE DONE in the entry and reformulate it as a short, crisp goal. Stay close to what the user actually wrote \u2014 keep the concrete action or habit they named. Strip the filler ("I want to", "I should really", "again", hedging) and surface the core action as a clear directive. Do NOT abstract it into a feeling, identity, or outcome \u2014 keep it as the thing they will actually do.
Rules:
- 4\u20135 words maximum
- Sentence case (capitalize only the first word and any proper nouns)
- Keep the specific action/habit the user named
- Open with an action verb \u2014 make it the thing to be done
- Tight and motivating, never vague or clinical
- Return ONLY the title \u2014 no quotes, no trailing punctuation, no explanation
Examples:
Entry: "I want to start moving my body again by running every day"
Title: Run every day
Entry: "I keep scrolling at night instead of sleeping and it's wrecking me"
Title: Put the phone down at night
Entry: "I want to call my mom more, I feel so distant from everyone lately"
Title: Call mom more often
Entry: "I should really meditate for 10 minutes each morning"
Title: Meditate each morning"""


def clean_goal_title(raw_title: str) -> str:
    """Use Claude Haiku to turn a raw goal into a clean 4-5 word display title."""
    if cfg.DEV_MODE:
        words = raw_title.strip().split()
        return " ".join(words[:5]) if len(words) > 5 else raw_title.strip()

    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    try:
        msg = client.messages.create(
            model=cfg.HAIKU_MODEL,
            max_tokens=30,
            system=_TITLE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": raw_title}],
        )
        text = ""
        for block in msg.content:
            if block.type == "text":
                text += block.text
        cleaned = text.strip().strip('"').strip("'")
        if cleaned:
            print(f"[LLM] title cleanup: '{raw_title}' -> '{cleaned}'")
            return cleaned[:200]
        return raw_title[:200]
    except Exception as e:
        print(f"[LLM] title cleanup failed: {e}")
        return raw_title[:200]


# ---------------------------------------------------------------------------
# AI suggestions for tips and challenges (v4)
# ---------------------------------------------------------------------------

_CHALLENGE_SYSTEM = """You help someone name the real obstacle standing between them and a personal goal. You write a single "challenge": the honest, specific reason this goal is hard for THIS person, in their own first-person voice.

Rules:
- Output one sentence, 6 to 18 words. Plain, warm, human.
- First person ("I..."), present tense. Name the concrete moment it breaks down (a behaviour, a situation, a feeling), never an abstract trait.
- It should feel uncomfortably accurate, like something the person could have written about themselves on a hard day.
- Fit the specific goal and its context. Do not repeat or lightly reword any challenge already listed.
- No advice, no solutions, no reassurance, no silver lining. Just the obstacle, honestly.
- Avoid clich\u00e9s and jargon ("lack of motivation", "time management", "self-care", "consistency").
Return ONLY raw JSON, nothing else: {"text": "..."}"""

_TIP_SYSTEM = """You give one piece of advice to someone working toward a personal goal. It should feel tailored and immediately doable, like a thoughtful friend who knows them and their specific obstacles.

Rules:
- Output one sentence, 8 to 22 words. Warm, specific, concrete.
- Imperative voice ("Lay your shoes by the door the night before...").
- Tie it directly to the goal and, when challenges are listed, answer one of them head on.
- Make it small and low-friction: a single next action someone could do today, not a program or a mindset.
- Sound like lived experience. No "try to", no hedging, no preamble, no generic advice ("stay consistent", "set goals", "be disciplined").
- Do not repeat or lightly reword any tip already saved.
Return ONLY raw JSON, nothing else: {"text": "..."}"""

_FALLBACK_CHALLENGES = [
    "I start strong, then the second week quietly disappears.",
    "I keep waiting to feel ready instead of just beginning.",
    "The moment it gets boring, I find a reason to stop.",
    "I tell myself I'll start tomorrow, and tomorrow keeps moving.",
    "One missed day turns into giving up on the whole thing.",
    "I get pulled into everyone else's needs before I get to mine.",
]

_FALLBACK_TIPS = [
    "Shrink it until it feels almost too small to skip, then start there.",
    "Pick one fixed time each day so you never have to decide.",
    "Set out the very first step the night before so starting takes no thought.",
    "Tell one person your plan today, so quitting has a witness.",
    "Mark each day you do it on a paper calendar and protect the streak.",
    "Attach it to something you already do without fail, right after it.",
]


def _build_suggestion_user_msg(
    kind: str,
    goal_title: str,
    goal_desc: str,
    challenges: list,
    tips: list,
) -> str:
    ch = "\n".join(f"- {c}" for c in challenges) if challenges else "none yet"
    tp = "\n".join(f"- {t}" for t in tips) if tips else "none yet"
    return f"""Goal: {goal_title}
Why it matters: {goal_desc if goal_desc and goal_desc.strip() else "not written yet"}
Challenges already named:
{ch}
Tips already saved:
{tp}

Write one {"tip" if kind == "tip" else "challenge"} now."""


def generate_suggestion(
    kind: str,
    goal_title: str,
    goal_desc: str = "",
    challenges=None,
    tips=None,
) -> str:
    """
    Generate an AI suggestion (tip or challenge) for a goal.
    Returns the suggestion text string.
    Falls back to a random pre-written suggestion on any error.
    """
    import random

    challenges = challenges or []
    tips = tips or []

    if cfg.DEV_MODE:
        pool = _FALLBACK_TIPS if kind == "tip" else _FALLBACK_CHALLENGES
        existing = tips if kind == "tip" else challenges
        fresh = [t for t in pool if t not in existing]
        arr = fresh if fresh else pool
        return random.choice(arr)

    system = _TIP_SYSTEM if kind == "tip" else _CHALLENGE_SYSTEM
    user_msg = _build_suggestion_user_msg(kind, goal_title, goal_desc, challenges, tips)

    try:
        client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model=cfg.CLAUDE_MODEL,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = ""
        for block in msg.content:
            if block.type == "text":
                raw += block.text
        raw = raw.strip()

        # Parse JSON response
        clean = raw.replace("```json", "").replace("```", "").strip()
        import re
        m = re.search(r"\{[\s\S]*\}", clean)
        if m:
            clean = m.group(0)
        parsed = json.loads(clean)
        if parsed and isinstance(parsed.get("text"), str) and parsed["text"].strip():
            return parsed["text"].strip()
        raise ValueError("no text in response")

    except Exception as e:
        print(f"[LLM] suggestion generation failed ({kind}): {e}")
        # Fallback to pre-written suggestions
        pool = _FALLBACK_TIPS if kind == "tip" else _FALLBACK_CHALLENGES
        existing = tips if kind == "tip" else challenges
        fresh = [t for t in pool if t not in existing]
        arr = fresh if fresh else pool
        return random.choice(arr)


# ===========================================================================
# v5 PROTOCOL GENERATION
# ---------------------------------------------------------------------------
# Wires the three prompts (jolt_prompts) and the two safety screens
# (safety_screen upstream, output_screen downstream) into the generation flow.
# All v4 functions above are untouched and still used by the v4 routes.
# ===========================================================================

from app.services import jolt_prompts, safety_screen, output_screen


class ProtocolUnsafe(Exception):
    """Raised when v5 generation cannot safely produce a jolt.

    stage    : where it stopped -> "plan" | "speech" | "output_screen"
    verdict  : short machine reason -> "unsafe_goal" | "unsafe" | "fail"
    category : safety category for logging, when available
    The upstream input screen does NOT raise this; screen_input() returns a
    verdict dict so the route can branch on clarify / block / crisis.
    """
    def __init__(self, stage: str, verdict: str = "", category: str = "", detail: str = ""):
        self.stage = stage
        self.verdict = verdict
        self.category = category
        self.detail = detail
        super().__init__(f"protocol unsafe at {stage}: {verdict} {category} {detail}".strip())


def _claude_text(model: str, system: str, user: str, max_tokens: int,
                 temperature: float = 1.0, max_retries: int = 4,
                 backoff_base: float = 2.0) -> str:
    """Low-level Claude call returning concatenated text, with retry/backoff.

    Shares the retry policy of generate_speech() but is parameterized by model,
    temperature, and token budget so it serves speeches (Sonnet, hot) and the
    cheap classifier/plan calls (Haiku/Sonnet, cold) alike.
    """
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = ""
            for block in msg.content:
                if block.type == "text":
                    text += block.text
            return text.strip()

        except Exception as e:
            last_exc = e
            status = getattr(e, "status_code", None)

            if status in _RETRYABLE and attempt < max_retries:
                sleep_s = backoff_base ** (attempt + 1 if status == 429 else attempt)
                print(f"[LLM] {status}, retry {attempt}/{max_retries} in {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue

            if status is None and ("connect" in str(e).lower() or "timeout" in str(e).lower()) and attempt < max_retries:
                sleep_s = backoff_base ** attempt
                print(f"[LLM] connection error, retry {attempt}/{max_retries} in {sleep_s:.1f}s")
                time.sleep(sleep_s)
                continue

            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("unknown LLM error")


# --- Upstream input safety screen ------------------------------------------

def screen_input(target: str, charge: str) -> dict:
    """Run the input safety screen on the onboarding answers.

    Returns {"verdict","category","rationale"} with verdict in
    safe | clarify | block | crisis. Fails closed to "clarify" on any error,
    so generation is never reached without a screen result.
    """
    if cfg.DEV_MODE:
        return {"verdict": "safe", "category": "none", "rationale": "dev mode"}
    try:
        raw = _claude_text(
            cfg.HAIKU_MODEL,
            safety_screen.SCREEN_SYSTEM,
            safety_screen.build_screen_prompt(target, charge),
            max_tokens=200,
            temperature=0.0,
        )
        return safety_screen.parse_screen(raw)
    except Exception as e:
        print(f"[LLM] input screen error: {e}")
        return {"verdict": "clarify", "category": "other", "rationale": "screen error"}


# --- Plan (break the goal into 5 daily components) --------------------------

_DEV_PLAN = {
    "days": [
        {"day": 1, "stage": "initiation",    "action": "Do the smallest version once",  "brief": "Make simply starting feel safe and winnable."},
        {"day": 2, "stage": "proof",         "action": "Do a slightly bigger version",  "brief": "Prove that 'I don't do this' is now false."},
        {"day": 3, "stage": "middle",        "action": "Do the small version again",    "brief": "Protect the streak through the quiet middle."},
        {"day": 4, "stage": "momentum",      "action": "Take one real step up",         "brief": "Show them how far they have already come."},
        {"day": 5, "stage": "consolidation", "action": "Do the full version once",      "brief": "Consolidate the identity; look back at the week."},
    ]
}


def generate_plan(target: str, charge: str, max_attempts: int = 2) -> dict:
    """Run the PLAN prompt and return the 5-day plan dict {"days": [...]}.

    Retries once on malformed JSON (wraps parse_plan's assertions so a bad plan
    never crashes the request). Raises ProtocolUnsafe if the model judges the
    goal unsafe (returns {"error": "unsafe_goal"}).
    """
    if cfg.DEV_MODE:
        return _DEV_PLAN

    last_err = None
    for attempt in range(1, max_attempts + 1):
        raw = _claude_text(
            cfg.CLAUDE_MODEL,
            jolt_prompts.PLAN_SYSTEM,
            jolt_prompts.build_plan_prompt(target, charge),
            max_tokens=1000,
            temperature=0.7,
        )
        try:
            plan = jolt_prompts.parse_plan(raw)
        except (AssertionError, json.JSONDecodeError, KeyError, ValueError) as e:
            last_err = e
            print(f"[LLM] plan parse failed (attempt {attempt}/{max_attempts}): {e}")
            continue

        if isinstance(plan, dict) and "error" in plan:
            raise ProtocolUnsafe("plan", verdict="unsafe_goal", detail=str(plan.get("error", "")))
        return plan

    # Last resort: after retries the model still returned unparseable JSON.
    # Never 500 protocol creation over a model wobble -- fall back to the
    # generic arc so the user still gets a working (if un-personalized) protocol.
    print(f"[LLM] plan unparseable after {max_attempts} attempts ({last_err!r}); using fallback plan")
    return _DEV_PLAN


# --- Speech (day 1 and days 2-5) with downstream output screening -----------

_DEV_PROTOCOL_SPEECH = (
    "You have carried this quietly for a long time... longer than you would admit.\n"
    "And still, here you are.\n\n"
    "---\n\n"
    "[softly] Most people never even write it down. You did. That already sets you apart. "
    "You are NOT behind. You are NOT broken. You are simply at the threshold, where every "
    "real change begins. [pause] So today, there is only one thing that matters. One small, "
    "honest act. [whispers] Here it is."
)


def _screen_output(speech: str) -> dict:
    """Run the downstream output screen on a generated speech.

    Returns the verdict dict. Fails closed to "fail" on any error so unverified
    content is never delivered.
    """
    if cfg.DEV_MODE:
        return {"verdict": "pass", "category": "none", "rationale": "dev mode"}
    try:
        raw = _claude_text(
            cfg.HAIKU_MODEL,
            output_screen.OUTPUT_SCREEN_SYSTEM,
            output_screen.build_output_screen_prompt(speech),
            max_tokens=200,
            temperature=0.0,
        )
        return output_screen.parse_output_screen(raw)
    except Exception as e:
        print(f"[LLM] output screen error: {e}")
        return {"verdict": "fail", "category": "other", "rationale": "screen error"}


def generate_protocol_speech(
    protocol_type: str,
    day: int,
    target: str,
    charge: str,
    plan: dict,
    day_action: str = None,
    completed_actions=None,
    yesterday_reflection: str = "",
    correction: str = "",
    max_screen_retries: int = 2,
) -> str:
    """Generate a jolt speech for one protocol day, screened before return.

    Day 1 uses the first-jolt prompt; days 2-5 use the state-aware prompt.
    target_words is read from the track registry in config for this type + day,
    so the speech is written to fill that day's real music track.

    Raises ProtocolUnsafe if the model returns REWIRE_UNSAFE, or if the output
    screen fails after max_screen_retries regenerations.
    """
    track = cfg.get_protocol_track(protocol_type, day)
    target_words = track["target_words"]
    today = next(d for d in plan["days"] if d["day"] == day)
    action = day_action or today["action"]

    if cfg.DEV_MODE:
        return f"{_DEV_PROTOCOL_SPEECH} {action}."

    completed_actions = completed_actions or []
    last_fail = None

    for attempt in range(1, max_screen_retries + 1):
        if day == 1:
            system = jolt_prompts.SPEECH1_SYSTEM
            user = jolt_prompts.build_speech1_prompt(target, charge, action, target_words)
        else:
            system, user = jolt_prompts.build_speechn_prompts(
                day, plan, target, charge, completed_actions, target_words, yesterday_reflection
            )

        if correction:
            user = f"{user}\n\n{correction}"

        speech = _claude_text(cfg.CLAUDE_MODEL, system, user, max_tokens=4096, temperature=1.0)

        if speech.strip().startswith("REWIRE_UNSAFE"):
            raise ProtocolUnsafe("speech", verdict="unsafe", detail="REWIRE_UNSAFE token")

        verdict = _screen_output(speech)
        if verdict.get("verdict") == "pass":
            return speech

        last_fail = verdict
        print(f"[LLM] output screen FAIL {attempt}/{max_screen_retries}: {verdict.get('category')}")

    raise ProtocolUnsafe(
        "output_screen",
        verdict="fail",
        category=(last_fail or {}).get("category", "other"),
        detail="output screen failed after retries",
    )


def generate_journal_speech(entry_text: str, target_words: int,
                            max_screen_retries: int = 2) -> str:
    """Generate a jolt speech from a journal entry, screened before return.

    Mirrors generate_protocol_speech but seeded by the entry text alone (no
    protocol, day, or plan). The caller passes target_words from whichever
    track the journal jolt uses. Raises ProtocolUnsafe if the model returns
    REWIRE_UNSAFE, or if the output screen fails after max_screen_retries
    regenerations. The entry text should already have passed the input safety
    screen before this is called.
    """
    if cfg.DEV_MODE:
        return f"{_DEV_PROTOCOL_SPEECH} Just the next honest line."

    last_fail = None
    for attempt in range(1, max_screen_retries + 1):
        system = jolt_prompts.JOURNAL_SYSTEM
        user = jolt_prompts.build_journal_prompt(entry_text, target_words)

        speech = _claude_text(cfg.CLAUDE_MODEL, system, user, max_tokens=4096, temperature=1.0)

        if speech.strip().startswith("REWIRE_UNSAFE"):
            raise ProtocolUnsafe("speech", verdict="unsafe", detail="REWIRE_UNSAFE token (journal)")

        verdict = _screen_output(speech)
        if verdict.get("verdict") == "pass":
            return speech

        last_fail = verdict
        print(f"[LLM] journal output screen FAIL {attempt}/{max_screen_retries}: {verdict.get('category')}")

    raise ProtocolUnsafe(
        "output_screen",
        verdict="fail",
        category=(last_fail or {}).get("category", "other"),
        detail="journal output screen failed after retries",
    )