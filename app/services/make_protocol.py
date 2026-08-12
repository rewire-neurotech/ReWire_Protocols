#!/usr/bin/env python3
"""
make_protocol.py -- the missing glue between jolt_prompts_V6.py and
track_sections.csv.

    pip install anthropic
    export ANTHROPIC_API_KEY=...

    python make_protocol.py \
        --goal "Meditate every day" \
        --why "I had a real practice for years and I let it go" \
        --category meditate \
        --extras technique="Vipassana / noting" when="First thing"

Writes five speeches plus the plan into out/<slug>/.

WHY THIS FILE EXISTS. Putting the two you have side by side gets you close but
not all the way, for two reasons:

  1. jolt_prompts_V6.py only WRITES prompts. It has no API call in it on
     purpose, so it does not care which SDK you use. Something has to send
     those prompts to the model. That is `call()` below.

  2. track_sections.csv is raw analysis output -- one row per section, in
     milliseconds. The prompt's `sections=` argument wants a list of dicts
     with word budgets already worked out. That is `load_tracks()` below.

ONE NUMBER TO FIX: WPS, a few lines down. It is how many words your v3 voice
actually speaks per second, and every word budget is derived from it. 2.30 is
a guess. Render one speech, count the spoken words, divide by the seconds of
speech, and put the real number here. If it is wrong, every speech is the
wrong length and mix.py has to time-stretch the voice, which is audible.
"""

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

import jolt_prompts_V6 as jp

MODEL = "claude-sonnet-4-6"
WPS = 2.30          # <-- MEASURE THIS. see the note above.
CSV_PATH = Path(__file__).parent / "track_sections.csv"


# ---------------------------------------------------------------------------
# 1. the API call
# ---------------------------------------------------------------------------

def call(system: str, user: str, temperature: float = 1.0) -> str:
    import anthropic
    client = anthropic.Anthropic()
    r = client.messages.create(
        model=MODEL, max_tokens=4000, temperature=temperature,
        system=system, messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in r.content if b.type == "text").strip()


# ---------------------------------------------------------------------------
# 2. the CSV -> what the prompt expects
# ---------------------------------------------------------------------------

def load_tracks(path=CSV_PATH) -> dict:
    """Returns {track_id: {...}} with sections already shaped for the prompt.

    Two judgement calls are baked in here:

    mean_level, not the type label, decides intensity. In your CSV, sections
    labelled LOW average level 0.589 while RISE sections average 0.504 -- a
    LOW section is on average LOUDER than a RISE, because the labels describe
    which way the music is moving rather than how loud it is. Feeding the raw
    labels to the prompt would put the crescendo in the wrong place.

    Quiet sections get fewer words per second. The voice slows down there and
    the silence is doing work, so a flat words-per-second overfills them.
    """
    rows = list(csv.DictReader(open(path)))
    grouped = {}
    for r in rows:
        grouped.setdefault(r["track_id"], []).append(r)

    out = {}
    for tid, rs in grouped.items():
        rs.sort(key=lambda r: int(r["section_idx"]))
        secs = []
        for r in rs:
            typ = (r.get("type_override") or r["type"]).strip().upper()
            idx = int(r["section_idx"])
            level = float(r["mean_level"])
            dur = int(r["section_duration_ms"]) / 1000
            secs.append({
                "name": (r.get("section_name") or "").strip() or f"{typ.lower()}_{idx}",
                "type": typ,
                "start": int(r["start_ms"]) / 1000,
                "duration": dur,
                "level": level,
                "words": int(round(dur * WPS * (0.80 + 0.32 * min(1.0, level)))),
            })

        peak = max(secs, key=lambda s: s["level"])
        quiet = [s for s in secs if s["level"] < 0.45]
        landing = quiet[-1] if quiet else secs[-1]
        peak["role"] = "peak"
        landing["role"] = "landing"

        head = secs[0]
        out[tid] = {
            "id": tid,
            "file": rs[0]["track_file"],
            "family": rs[0]["track_file"].split("/")[0].split(". ")[-1].lower(),
            "duration": int(rs[0]["track_duration_ms"]) / 1000,
            "sections": secs,
            # seconds of quiet at the top, where the 2 opening statements sit.
            # 0 means the track starts loud and they need a cold open or a fade.
            "lead_in": head["duration"] if head["level"] < 0.45 else 0.0,
            "words": sum(s["words"] for s in secs),
            "peak_at": peak["start"] / (int(rs[0]["track_duration_ms"]) / 1000),
        }
    return out


def pick_tracks(tracks: dict, family: str | None = None) -> dict:
    """One track per day. Chosen against the protocol's shape rather than by
    mood: day 3 is the soft day of the week so it gets the quietest peak, and
    day 5 carries the biggest crescendo so it gets the loudest peak with the
    longest quiet tail to land in."""
    ids = [t for t in tracks if not family or tracks[t]["family"] == family]
    if not ids:
        raise SystemExit(f"no tracks in family {family!r}. have: "
                         f"{sorted({t['family'] for t in tracks.values()})}")
    peak_of = lambda i: max(s["level"] for s in tracks[i]["sections"])
    tail_of = lambda i: max((s["duration"] for s in tracks[i]["sections"]
                             if s["level"] < 0.45), default=0)
    by_peak = sorted(ids, key=peak_of)
    soft = by_peak[0]
    loud = max(ids, key=lambda i: (peak_of(i), tail_of(i)))
    rest = [i for i in by_peak if i not in (soft, loud)] or by_peak
    return {1: rest[0 % len(rest)], 2: rest[1 % len(rest)], 3: soft,
            4: rest[2 % len(rest)], 5: loud}


# ---------------------------------------------------------------------------
# 3. run it
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "protocol"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--goal", required=True, help="what they typed in step 1")
    ap.add_argument("--why", default="", help="what they typed in step 2")
    ap.add_argument("--category", default="", help="onboarding category slug")
    ap.add_argument("--extras", nargs="*", default=[],
                    help='key=value pairs, e.g. technique="Body scan"')
    ap.add_argument("--family", default=None,
                    help="restrict music to one family (revelation, primal, "
                         "spectral, adventure)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--out", default="out")
    ap.add_argument("--dry", action="store_true",
                    help="print the prompts and the track plan, call nothing")
    args = ap.parse_args()

    extras = {}
    for pair in args.extras:
        k, _, v = pair.partition("=")
        if v:
            extras[k.strip()] = v.strip()

    tracks = load_tracks()
    chosen = pick_tracks(tracks, args.family)

    print(f"goal:     {args.goal}")
    print(f"why:      {args.why or '(none)'}")
    print(f"category: {args.category or '(free text)'}")
    print(f"extras:   {extras or '(none)'}\n")
    for d in range(1, 6):
        t = tracks[chosen[d]]
        cold = "  COLD OPEN NEEDED" if t["lead_in"] == 0 else ""
        print(f"  day {d}  {t['words']:>3}w over {t['duration']:.0f}s  "
              f"peak @{t['peak_at']:.0%}  lead-in {t['lead_in']:.0f}s  "
              f"{t['id'][:44]}{cold}")
    print()

    out_dir = Path(args.out) / slugify(args.goal)
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- the plan -----------------------------------------------------------
    plan_user = jp.build_plan_prompt(args.goal, args.why, args.category, extras)
    if args.dry:
        (out_dir / "day1_prompt.txt").write_text(
            jp.build_speech1_prompt(
                args.goal, args.why, "<action>", tracks[chosen[1]]["words"],
                axes={"domain": "?", "stage": "?", "blocker": "?"},
                category=args.category, extras=extras,
                sections=tracks[chosen[1]]["sections"],
                lead_in=tracks[chosen[1]]["lead_in"], seed=args.seed))
        print(plan_user)
        print(f"\ndry run. day-1 user prompt written to "
              f"{out_dir/'day1_prompt.txt'}. nothing was called.")
        return

    print("planning...")
    plan = jp.parse_plan(call(jp.PLAN_SYSTEM, plan_user, temperature=0.4))
    if "error" in plan:
        print(f"refused: {plan['error']}")
        return
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    for d in plan["days"]:
        print(f"  day {d['day']}: {d['action']}")
    print(f"  axes: {plan['axes']}\n")

    # --- the five speeches --------------------------------------------------
    speeches = {}
    for day in range(1, 6):
        t = tracks[chosen[day]]
        if day == 1:
            system = jp.SPEECH1_SYSTEM
            user = jp.build_speech1_prompt(
                args.goal, args.why, plan["days"][0]["action"], t["words"],
                axes=plan["axes"], category=args.category, extras=extras,
                sections=t["sections"], lead_in=t["lead_in"], seed=args.seed)
        else:
            completed = [d["action"] for d in plan["days"] if d["day"] < day]
            system, user = jp.build_speechn_prompts(
                day, plan, args.goal, args.why, completed, t["words"],
                category=args.category, extras=extras,
                sections=t["sections"], lead_in=t["lead_in"], seed=args.seed)

        text = call(system, user)
        speeches[day] = text
        spoken = len([w for w in re.sub(r"\[\[.*?\]\]|\[.*?\]|---", " ", text).split()
                      if w.strip(".,!?;:-—…\"'")])
        off = (spoken - t["words"]) / t["words"] * 100
        flag = "  OFF TARGET" if abs(off) > 10 else ""
        (out_dir / f"day{day}.txt").write_text(text)
        print(f"  day {day}: {spoken} spoken words vs {t['words']} target "
              f"({off:+.0f}%){flag}  track {t['id'][:40]}")

    (out_dir / "manifest.json").write_text(json.dumps({
        "goal": args.goal, "why": args.why, "category": args.category,
        "extras": extras, "plan": plan,
        "tracks": {str(d): {"id": tracks[chosen[d]]["id"],
                            "file": tracks[chosen[d]]["file"],
                            "target_words": tracks[chosen[d]]["words"]}
                   for d in range(1, 6)},
    }, indent=2))

    print(f"\nwritten to {out_dir}/  (day1.txt ... day5.txt, plan.json, "
          f"manifest.json)")
    print("manifest.json tells mix.py which audio file goes with which speech.")


if __name__ == "__main__":
    sys.exit(main())
