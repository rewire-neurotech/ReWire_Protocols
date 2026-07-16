import os, sys, json, csv, random, argparse
from concurrent.futures import ThreadPoolExecutor
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services import llm

LABELS = ["safe", "clarify", "block", "crisis"]


def recall_ci(pairs, cls, b=2000):
    hits = [1 if p == cls else 0 for g, p in pairs]
    if not hits:
        return 0, 0, 0
    base = sum(hits) / len(hits)
    rnd = random.Random(7)
    boot = []
    for _ in range(b):
        s = [hits[rnd.randrange(len(hits))] for _ in range(len(hits))]
        boot.append(sum(s) / len(s))
    boot.sort()
    return base, boot[int(.025 * b)], boot[int(.975 * b)]


def prf(cm, v):
    tp = cm[v][v]
    fn = sum(cm[v].values()) - tp
    fp = sum(cm[g][v] for g in LABELS if g != v)
    rec = tp / (tp + fn) if tp + fn else 0
    prec = tp / (tp + fp) if tp + fp else 0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0
    return prec, rec, f1


def run(path, limit, workers, hard_only):
    rows = [json.loads(l) for l in open(path)]
    if hard_only:
        rows = [r for r in rows if r.get("source") != "aug" and r.get("note", "")]
        print(f"hard-only: {len(rows)} hand-authored items\n")
    if limit:
        rows = rows[:limit]
    if os.getenv("DEV_MODE", "").lower() in ("true", "1", "yes"):
        print("DEV_MODE is on, screener returns 'safe' for all. Turn it off for real numbers.\n")

    def screen(r):
        try:
            return llm.screen_input(r["target"], r["charge"]).get("verdict", "clarify")
        except Exception as e:
            return "error:" + type(e).__name__

    with ThreadPoolExecutor(max_workers=workers) as ex:
        preds = list(ex.map(screen, rows))

    cm = defaultdict(Counter)
    errs, hard, bulk = [], [], []
    cat_tot, cat_ok = Counter(), Counter()
    for r, p in zip(rows, preds):
        g = r["verdict"]
        cm[g][p] += 1
        (hard if r.get("note") else bulk).append((g, p))
        cat_tot[r["category"]] += 1
        cat_ok[r["category"]] += (p == g)
        if p != g:
            errs.append({"id": r["id"], "target": r["target"], "charge": r["charge"],
                         "gold": g, "pred": p, "category": r["category"], "note": r.get("note", "")})

    n = len(rows)
    acc = sum(cm[v][v] for v in LABELS) / n
    print(f"items: {n}   accuracy: {100*acc:.1f}%\n")
    f1s = []
    for v in LABELS:
        pr, rc, f1 = prf(cm, v)
        f1s.append(f1)
        print(f"  {v:8} P={100*pr:5.1f}  R={100*rc:5.1f}  F1={100*f1:5.1f}")
    print(f"  macro-F1: {100*sum(f1s)/len(f1s):.1f}")

    crisis_pairs = [(g, p) for g, p in zip([r["verdict"] for r in rows], preds) if g == "crisis"]
    base, lo, hi = recall_ci(crisis_pairs, "crisis")
    print(f"\ncrisis recall: {100*base:.1f}%   95% CI [{100*lo:.1f}, {100*hi:.1f}]")
    leaked = cm["crisis"]["safe"] + cm["crisis"]["clarify"]
    print(f"  crises leaked to safe/clarify: {leaked}/{sum(cm['crisis'].values())}")

    def sub_rec(pairs, cls):
        d = [(g, p) for g, p in pairs if g == cls]
        return (sum(1 for g, p in d if p == cls) / len(d)) if d else float("nan")

    print("\nhard vs bulk:")
    print(f"  hard: {len(hard)} items, acc {100*sum(1 for g,p in hard if g==p)/max(1,len(hard)):.1f}%, crisis-recall {100*sub_rec(hard,'crisis'):.1f}%")
    print(f"  bulk: {len(bulk)} items, acc {100*sum(1 for g,p in bulk if g==p)/max(1,len(bulk)):.1f}%, crisis-recall {100*sub_rec(bulk,'crisis'):.1f}%")

    print("\nper-category accuracy:")
    for c, _ in cat_tot.most_common():
        print(f"  {c:20} {100*cat_ok[c]/cat_tot[c]:5.1f}%  (n={cat_tot[c]})")

    print("\nconfusion (rows=true, cols=pred):")
    print("        " + "".join(f"{v:>9}" for v in LABELS))
    for g in LABELS:
        print(f"  {g:6}" + "".join(f"{cm[g][p]:9}" for p in LABELS))

    with open("bench/eval_errors.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "target", "charge", "gold", "pred", "category", "note"])
        w.writeheader()
        for e in sorted(errs, key=lambda e: (e["gold"] != "crisis", e["gold"])):
            w.writerow(e)
    print(f"\n{len(errs)} misses saved to bench/eval_errors.csv")


ap = argparse.ArgumentParser()
ap.add_argument("--set", default="bench/rewire_eval_set.jsonl")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--workers", type=int, default=8)
ap.add_argument("--hard-only", action="store_true")
a = ap.parse_args()
run(a.set, a.limit, a.workers, a.hard_only)