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
# Goal title cleanup (Felix's exact prompt)
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
# AI suggestions for tips and challenges (Felix's exact prompts)
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
    challenges: list[str],
    tips: list[str],
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
    challenges: list[str] | None = None,
    tips: list[str] | None = None,
) -> str:
    """
    Generate an AI suggestion (tip or challenge) for a goal.
    Returns the suggestion text string.
    Falls back to a random pre-written suggestion on any error.
    """
    import random

    challenges = challenges or []
    tips = tips or []
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
