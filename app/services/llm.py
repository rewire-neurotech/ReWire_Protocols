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


# Whether the installed anthropic SDK accepts the temperature keyword on
# Messages.create(). Some SDK releases dropped it, and passing it raises
# TypeError before any request is sent. Detected once at runtime: the first
# call that trips it flips this flag and every later call skips the keyword,
# so the failed attempt is paid at most once per process, not per call.
_SDK_ACCEPTS_TEMPERATURE = True


def _claude_text(model: str, system: str, user: str, max_tokens: int,
                 temperature: float = 1.0, max_retries: int = 4,
                 backoff_base: float = 2.0) -> str:
    """Low-level Claude call returning concatenated text, with retry/backoff.

    Shares the retry policy of generate_speech() but is parameterized by model,
    temperature, and token budget so it serves speeches (Sonnet, hot) and the
    cheap classifier/plan calls (Haiku/Sonnet, cold) alike.

    Tolerates SDKs that reject the temperature keyword: on that specific
    TypeError the call is retried immediately without temperature and the
    keyword is dropped for the rest of the process.
    """
    global _SDK_ACCEPTS_TEMPERATURE
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    last_exc = None

    for attempt in range(1, max_retries + 1):
        try:
            kwargs = dict(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            if _SDK_ACCEPTS_TEMPERATURE:
                kwargs["temperature"] = temperature
            try:
                msg = client.messages.create(**kwargs)
            except TypeError as te:
                if "temperature" in str(te) and "temperature" in kwargs:
                    _SDK_ACCEPTS_TEMPERATURE = False
                    print("[LLM] SDK rejects temperature keyword, retrying without it")
                    kwargs.pop("temperature", None)
                    msg = client.messages.create(**kwargs)
                else:
                    raise
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

# ===========================================================================
# MEDITATION GENERATION (V5 meditation build, Aug 2026)
# ---------------------------------------------------------------------------
# Day 1 uses the theme's own prompt (regular / forest / ocean / fire) on the
# day 1 model. Days 2-5 and journal jolts use the recursive prompt from
# experiment 75 on the later model, fed with the full history so far.
# Every script passes the output screen before it is returned. The prompt
# validators are advisory: Felix's own shipped takes trip them, so a
# violation triggers one fresh take and the cleanest take wins.
# ===========================================================================

from app.services import meditation_prompts


_DEV_MEDITATION_D1 = (
    "You are settled where you sit, and the weight of the day rests on the "
    "chair under you. This session stays with one small idea about time. "
    "A river cuts stone by moving one grain at a time. The stone keeps the "
    "shape of everything the water did. A person keeps the shape of their "
    "days the same way. Now ask yourself: which small thing shaped today?"
)

_DEV_MEDITATION_LATER = (
    "Settle into the chair and let the sounds in the room stay where they "
    "are. [long pause] The thing you wrote about is still here, the same "
    "size you left it. It fits inside one plain sentence, and the sentence "
    "is enough. [pause] Stay with that sentence while the music runs, and "
    "let the answers come after the searching stops."
)


def _generate_meditation(model: str, system: str, user: str, validate_fn,
                         label: str, max_takes: int = 3) -> str:
    """Generate, validate, screen. Return the cleanest take that passes the
    output screen. Raises ProtocolUnsafe when no take passes the screen."""
    best_script = None
    best_hits = None
    hits = []

    for take in range(1, max_takes + 1):
        u = user
        if hits:
            u = (
                f"{user}\n\nThe previous take broke these rules: "
                f"{', '.join(str(h) for h in hits)}. "
                f"Write a completely fresh take that breaks none of them."
            )

        script = _claude_text(model, system, u, max_tokens=4096, temperature=1.0)
        hits = validate_fn(script)

        verdict = _screen_output(script)
        if verdict.get("verdict") != "pass":
            print(f"[LLM] {label} output screen FAIL take {take}/{max_takes}: {verdict.get('category')}")
            continue

        if not hits:
            return script
        print(f"[LLM] {label} take {take}/{max_takes} has {len(hits)} validator hits")
        if best_hits is None or len(hits) < len(best_hits):
            best_script, best_hits = script, hits

    if best_script is not None:
        print(f"[LLM] {label}: returning best take with {len(best_hits)} validator hits")
        return best_script

    raise ProtocolUnsafe(
        "output_screen",
        verdict="fail",
        category="other",
        detail=f"{label}: no take passed the output screen",
    )


def generate_meditation_day1(theme: str, topic: str, why: str) -> str:
    """Day 1 meditation script for a theme (regular / forest / ocean / fire).

    Ocean and fire prompts carry their context corpora, loaded from assets
    and truncated to the config cap, inserted before the LISTENER INPUT
    heading exactly the way stimgen did.
    """
    theme = cfg.meditation_theme(theme)
    if cfg.DEV_MODE:
        return _DEV_MEDITATION_D1

    context_text = meditation_prompts.load_context(
        cfg.meditation_context_paths(theme), cfg.MEDITATION_CONTEXT_CAP
    )
    system = meditation_prompts.compose_day1_prompt(theme, context_text)
    user = meditation_prompts.build_day1_input(topic, why)

    return _generate_meditation(
        cfg.MEDITATION_MODEL_DAY1,
        system,
        user,
        lambda s: meditation_prompts.validate_day1(theme, s),
        f"meditation d1 {theme}",
    )


def generate_meditation_later(topic: str, why: str, history: list) -> str:
    """Days 2-5 meditation script, and journal jolts.

    history: list of dicts in day order, each {day, script, chills,
    reflection}, so the prompt reads everything that came before and can
    never repeat it. Journal jolts call this with the entry text as topic
    and an empty history.
    """
    if cfg.DEV_MODE:
        return _DEV_MEDITATION_LATER

    system = meditation_prompts.LATER_DAYS_PROMPT
    user = meditation_prompts.build_later_input(topic, why, history)

    return _generate_meditation(
        cfg.MEDITATION_MODEL_LATER,
        system,
        user,
        meditation_prompts.validate_later,
        "meditation later",
    )


_MEDITATION_TITLE_SYSTEM = """You title a finished meditation session for a mental health app.
You get what the listener wrote before the session and, when present, their reflection after it.
If the reflection contains an insight, the title is that insight, distilled.
Otherwise the title is a plain sentence about what the session was about.
Rules:
- 6 words maximum
- Simple words a child could read
- Sentence case, no quotes, no trailing punctuation
- Return ONLY the title"""


def generate_meditation_title(topic: str, reflection: str = "") -> str:
    """Six-word child-readable title: the listener's insight when they had
    one, otherwise a plain sentence about the topic. Haiku, cheap, safe
    fallback to the topic's first words on any error."""
    fallback = " ".join((topic or "meditation").strip().split()[:6]) or "A quiet meditation"
    if cfg.DEV_MODE:
        return fallback
    try:
        user = f"They wrote: {topic}"
        if reflection and reflection.strip():
            user += f"\nTheir reflection after: {reflection.strip()}"
        raw = _claude_text(cfg.HAIKU_MODEL, _MEDITATION_TITLE_SYSTEM, user,
                           max_tokens=30, temperature=0.7, max_retries=2)
        title = raw.strip().strip('"').strip("'")
        if title:
            words = title.split()
            if len(words) > 6:
                title = " ".join(words[:6])
            return title[:200]
        return fallback
    except Exception as e:
        print(f"[LLM] meditation title failed: {e}")
        return fallback


# --------------------------------------------------------------------------- #
# Create-time title and summary (home card, Aug 2026)
# --------------------------------------------------------------------------- #
# Felix: the protocol title must fit between the two arrows on the home
# screen, so 5 words max, generated the moment the protocol is created
# instead of after the first reflect. generate_meditation_title above stays
# as the reflect-time fallback for protocols that still have no title.

_PROTOCOL_TITLE_SYSTEM = """You title a new meditation protocol for a mental health app.
You get what the listener wrote about what they want to meditate on.
The title names the SPECIFIC thing they are sitting with. Not a mood, not a vague promise, not a greeting card line. If they wrote about a divorce, the title is about the divorce. If they wrote about their father, the title is about their father.
Rules:
- 5 words maximum
- Concrete over abstract: name the subject, the person, or the situation
- Never write vague filler like "A bright future ahead" or "Finding your inner peace"
- Sentence case, no quotes, no trailing punctuation
- Never repeat their sentence back word for word
- Return ONLY the title
Examples:
They wrote: "I keep replaying my divorce and blaming myself"
Title: The divorce and the blame
They wrote: "My dad died last spring and I never said goodbye"
Title: Saying goodbye to dad
They wrote: "I feel like a fraud at work"
Title: The fraud feeling at work"""


def generate_protocol_title(topic: str) -> str:
    """Five-word display title generated at protocol create time. Haiku,
    cheap, safe fallback to the topic's first words on any error."""
    fallback = " ".join((topic or "meditation").strip().split()[:5]) or "A quiet meditation"
    if cfg.DEV_MODE:
        return fallback
    try:
        raw = _claude_text(cfg.HAIKU_MODEL, _PROTOCOL_TITLE_SYSTEM,
                           f"They wrote: {topic}",
                           max_tokens=30, temperature=0.7, max_retries=2)
        title = raw.strip().strip('"').strip("'")
        if title:
            words = title.split()
            if len(words) > 5:
                title = " ".join(words[:5])
            return title[:200]
        return fallback
    except Exception as e:
        print(f"[LLM] protocol title failed: {e}")
        return fallback


_PROTOCOL_SUMMARY_SYSTEM = """You summarise a new meditation protocol for the card on a mental health app's home screen.
You get what the listener wrote about what they want to meditate on.
Write what this five-day protocol is about, in the third person, addressed to no one.
Rules:
- EXACTLY ONE sentence, 15 words maximum
- Sober and plain. State the subject, nothing more. No imagery, no "journey", no "exploring", no "beauty"
- Never quote or repeat their words back; describe the theme in fresh words
- No "you", no "I", no advice, no hype
- Return ONLY the sentence, no quotes
Examples:
They wrote: "I keep replaying my divorce and blaming myself"
Summary: Five days sitting with the end of a marriage and the self-blame around it.
They wrote: "I feel like a fraud at work"
Summary: Five days with the feeling of being a fraud at work."""


def generate_protocol_summary(topic: str, charge: str = "") -> str:
    """One sober third-person sentence for the home card, generated at
    protocol create time. Never cites the user's own words back at them
    (Felix). Haiku, cheap, safe fallback to a generic line on any error."""
    fallback = "A five day meditation protocol."
    if cfg.DEV_MODE:
        return fallback
    try:
        user = f"They wrote: {topic}"
        if charge and charge.strip():
            user += f"\nWhy it matters to them: {charge.strip()}"
        raw = _claude_text(cfg.HAIKU_MODEL, _PROTOCOL_SUMMARY_SYSTEM, user,
                           max_tokens=60, temperature=0.7, max_retries=2)
        summary = raw.strip().strip('"').strip("'")
        if summary:
            return summary[:500]
        return fallback
    except Exception as e:
        print(f"[LLM] protocol summary failed: {e}")
        return fallback


# --------------------------------------------------------------------------- #
# Reflect-time day title and summary sentence (Felix, Aug 2026)
# --------------------------------------------------------------------------- #
# The experience title on the protocol card was the same generic line for
# every day of every protocol (a frontend placeholder array). Each day now
# gets its own title at reflect time, distilled from that day's script and
# the listener's reflection. The home card summary also grows: each finished
# day appends one sentence. Both are Haiku with safe fallbacks and are
# written by routes/protocol_jolt.py inside the reflect handler.

_DAY_TITLE_SYSTEM = """You title one finished meditation session for a mental health app.
You get the script that was read to the listener and, when present, what they wrote afterwards.
If their reflection contains an insight, the title IS that insight, distilled. Otherwise the title names the specific thing this session was about.
Rules:
- 6 words maximum
- Concrete over abstract: name the actual subject or the actual insight
- Never write vague filler like "A moment of peace" or "Sitting with it"
- Sentence case, no quotes, no trailing punctuation
- Return ONLY the title
Examples:
Reflection: "I realised I have been angry at myself, not at her"
Title: The anger was at myself
Reflection: "" and the script was about watching thoughts pass like clouds
Title: Watching thoughts pass without chasing"""


def generate_day_title(script: str, reflection: str = "") -> str:
    """Six-word title for one day's experience, from that day's script and
    the listener's reflection. Haiku, cheap, safe fallback on any error."""
    fallback = "A quiet session"
    if cfg.DEV_MODE:
        return fallback
    try:
        user = f"The script read to them:\n{(script or '').strip()[:2000]}"
        if reflection and reflection.strip():
            user += f"\n\nTheir reflection after:\n{reflection.strip()[:1000]}"
        raw = _claude_text(cfg.HAIKU_MODEL, _DAY_TITLE_SYSTEM, user,
                           max_tokens=30, temperature=0.7, max_retries=2)
        title = raw.strip().strip('"').strip("'")
        if title:
            words = title.split()
            if len(words) > 6:
                title = " ".join(words[:6])
            return title[:200]
        return fallback
    except Exception as e:
        print(f"[LLM] day title failed: {e}")
        return fallback


_SUMMARY_SENTENCE_SYSTEM = """You add one sentence to the summary on a meditation protocol's home card.
You get the summary so far, the script from the day's session, and what the listener wrote afterwards.
Write ONE new sentence saying what this day added: the insight if they had one, otherwise what the session was about.
Rules:
- EXACTLY ONE sentence, 15 words maximum
- Sober and plain, third person, no "you", no "I", no hype
- Do not repeat anything already in the summary so far
- Never quote the listener's words back
- Return ONLY the new sentence, no quotes"""


def generate_summary_sentence(summary_so_far: str, script: str,
                              reflection: str = "") -> str:
    """One sentence saying what a finished day added, appended to the home
    card summary by the reflect handler. Haiku, safe fallback to empty
    string on any error so the summary is simply left unchanged."""
    if cfg.DEV_MODE:
        return ""
    try:
        user = f"Summary so far:\n{(summary_so_far or '').strip()[:1000]}"
        user += f"\n\nThe script read to them:\n{(script or '').strip()[:2000]}"
        if reflection and reflection.strip():
            user += f"\n\nTheir reflection after:\n{reflection.strip()[:1000]}"
        raw = _claude_text(cfg.HAIKU_MODEL, _SUMMARY_SENTENCE_SYSTEM, user,
                           max_tokens=60, temperature=0.7, max_retries=2)
        sentence = raw.strip().strip('"').strip("'")
        return sentence[:300]
    except Exception as e:
        print(f"[LLM] summary sentence failed: {e}")
        return ""
