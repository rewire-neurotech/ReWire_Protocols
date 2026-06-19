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