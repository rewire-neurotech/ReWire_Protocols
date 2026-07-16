import os, sys, json, csv
from collections import Counter, defaultdict


def load(p):
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def kappa(a, b):
    n = len(a)
    if not n:
        return float("nan")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(a) | set(b))
    return (po - pe) / (1 - pe) if pe != 1 else 1.0


def score(model, clin):
    tp = sum(1 for m, c in zip(model, clin) if m == "fail" and c == "fail")
    fn = sum(1 for m, c in zip(model, clin) if m != "fail" and c == "fail")
    tn = sum(1 for m, c in zip(model, clin) if m != "fail" and c != "fail")
    fp = sum(1 for m, c in zip(model, clin) if m == "fail" and c != "fail")
    sens = tp / (tp + fn) if tp + fn else 0
    spec = tn / (tn + fp) if tn + fp else 0
    prec = tp / (tp + fp) if tp + fp else 0
    return dict(sensitivity=round(sens, 3), specificity=round(spec, 3),
                precision=round(prec, 3), kappa=round(kappa(model, clin), 3), n=len(model))


verdicts = load("bench/model_verdicts.jsonl")
corpus = {c["id"]: c for c in load("bench/corpus.jsonl")}
cat = {r["id"]: r["category"] for r in load("bench/rewire_eval_set.jsonl")}
clin = {}
if os.path.exists("bench/clinician_review.csv"):
    for row in csv.DictReader(open("bench/clinician_review.csv")):
        v = (row.get("clinician_verdict(fill:pass/fail)") or "").strip().lower()
        if v in ("pass", "fail"):
            clin[row["id"]] = v

if not verdicts:
    sys.exit("no bench/model_verdicts.jsonl, run rate_corpus.py first.")
models = [k[2:] for k in verdicts[0] if k.startswith("v_")]

print("=== models vs clinician ===")
if not clin:
    print("  clinician_review.csv has no filled labels yet.")
    chosen = models[0]
else:
    best = None
    for m in models:
        sub = [(r["v_" + m], clin[r["id"]]) for r in verdicts if r["id"] in clin and r.get("v_" + m) in ("pass", "fail")]
        if not sub:
            continue
        sc = score([x for x, _ in sub], [y for _, y in sub])
        print(f"  {m:8} {sc}")
        key = (sc["sensitivity"], sc["specificity"])
        if best is None or key > best[1]:
            best = (m, key)
    chosen = best[0] if best else models[0]
print(f"  chosen rater: {chosen}\n")

recs = []
for r in verdicts:
    v = r.get("v_" + chosen)
    if v not in ("pass", "fail") or r["id"] not in corpus:
        continue
    c = corpus[r["id"]]
    recs.append(dict(unsafe=1 if v == "fail" else 0, day=c.get("day"),
                     wc=c.get("word_count") or 0, cat=cat.get(r["id"], "?")))

n = len(recs)
base = sum(r["unsafe"] for r in recs) / n if n else 0
print(f"=== regressions ({chosen}, {n} speeches, base unsafe {100*base:.1f}%) ===")


def by(key, label):
    g = defaultdict(list)
    for r in recs:
        g[r[key]].append(r["unsafe"])
    print(f"\n  unsafe by {label}:")
    for k in sorted(g, key=lambda x: str(x)):
        print(f"    {str(k):16} {100*sum(g[k])/len(g[k]):5.1f}%  (n={len(g[k])})")


by("cat", "input category")
by("day", "day")
if recs:
    wcs = sorted(r["wc"] for r in recs)
    t1, t2 = wcs[len(wcs) // 3], wcs[2 * len(wcs) // 3]
    g = defaultdict(list)
    for r in recs:
        b = "short" if r["wc"] <= t1 else "medium" if r["wc"] <= t2 else "long"
        g[b].append(r["unsafe"])
    print("\n  unsafe by length:")
    for b in ("short", "medium", "long"):
        if g[b]:
            print(f"    {b:16} {100*sum(g[b])/len(g[b]):5.1f}%  (n={len(g[b])})")

try:
    from sklearn.linear_model import LogisticRegression
    import numpy as np
    y = np.array([r["unsafe"] for r in recs])
    if len(set(y)) > 1:
        cats = sorted({r["cat"] for r in recs})
        X = np.array([[r["day"] or 0, r["wc"]] + [1 if r["cat"] == c else 0 for c in cats] for r in recs], dtype=float)
        lr = LogisticRegression(max_iter=1000).fit(X, y)
        names = ["day", "word_count"] + ["cat=" + c for c in cats]
        print("\n  logistic coefficients (log-odds of unsafe):")
        for nm, co in sorted(zip(names, lr.coef_[0]), key=lambda x: -abs(x[1]))[:10]:
            print(f"    {nm:22} {co:+.3f}")
except ImportError:
    pass