import os, sys, json, argparse, re
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services import llm


def build(inp, day):
    t, c = inp["target"], inp["charge"]
    try:
        plan = llm.generate_plan(t, c)
        speech = llm.generate_protocol_speech("activate", day, t, c, plan)
        wc = len(re.sub(r"\[[^\]]*\]", "", speech).split())
        return {"id": inp.get("id"), "target": t, "charge": c, "day": day,
                "status": "generated", "speech": speech, "word_count": wc}
    except llm.ProtocolUnsafe as e:
        return {"id": inp.get("id"), "target": t, "charge": c, "day": day,
                "status": "blocked_by_own_screen", "stage": e.stage, "speech": None}
    except Exception as e:
        return {"id": inp.get("id"), "target": t, "charge": c, "day": day,
                "status": "error:" + type(e).__name__, "speech": None}


ap = argparse.ArgumentParser()
ap.add_argument("--set", default="bench/rewire_eval_set.jsonl")
ap.add_argument("--only", default="safe,clarify")
ap.add_argument("--limit", type=int, default=60)
ap.add_argument("--day", type=int, default=1)
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--out", default="bench/corpus.jsonl")
a = ap.parse_args()

keep = set(a.only.split(","))
rows = [json.loads(l) for l in open(a.set) if json.loads(l)["verdict"] in keep][:a.limit]
print(f"generating {len(rows)} speeches (day {a.day})...")
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    out = list(ex.map(lambda r: build(r, a.day), rows))
with open(a.out, "w") as f:
    for o in out:
        f.write(json.dumps(o, ensure_ascii=False) + "\n")
from collections import Counter
print("done:", dict(Counter(o["status"] for o in out)))
print("wrote", a.out)