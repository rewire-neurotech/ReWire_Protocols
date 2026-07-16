import argparse
import numpy as np
from collections import Counter


def encoder(mode):
    if mode != "emb":
        return None
    try:
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as e:
        print(f"(embeddings unavailable: {str(e)[:120]}, using TF-IDF)")
        return None


def features(rows, mode, enc):
    texts = [(r["target"] + " . " + r["charge"]) for r in rows]
    if enc is not None:
        return np.asarray(enc.encode(texts, show_progress_bar=False, normalize_embeddings=True)), None, "emb"
    from sklearn.feature_extraction.text import TfidfVectorizer
    from scipy.sparse import hstack
    vw = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)
    vc = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2)
    xw, xc = vw.fit_transform(texts), vc.fit_transform(texts)
    return hstack([xw, xc]).tocsr(), (vw, vc), "tfidf"


def gen_gap(rows, mode, enc):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import confusion_matrix
    labels = ["safe", "clarify", "block", "crisis"]

    def is_hand(r):
        s = r.get("source")
        if s is not None:
            return s == "hard+" or (s == "seed" and r.get("note", "") != "")
        return r.get("note", "") != ""

    hand = [r for r in rows if is_hand(r) and r.get("source") != "aug"]
    hg = {r.get("group", r["target"]) for r in hand}
    train = [r for r in rows if r.get("group", r["target"]) not in hg]
    if len(hand) < 10 or not train:
        print("\n(not enough hand-authored items for a generalization test)")
        return
    txt = lambda rs: [r["target"] + " . " + r["charge"] for r in rs]
    if enc is not None:
        xtr = np.asarray(enc.encode(txt(train), show_progress_bar=False, normalize_embeddings=True))
        xte = np.asarray(enc.encode(txt(hand), show_progress_bar=False, normalize_embeddings=True))
    else:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from scipy.sparse import hstack
        vw = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)
        vc = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=2)
        xtr = hstack([vw.fit_transform(txt(train)), vc.fit_transform(txt(train))]).tocsr()
        xte = hstack([vw.transform(txt(hand)), vc.transform(txt(hand))]).tocsr()
    ytr = np.array([r["verdict"] for r in train])
    yte = np.array([r["verdict"] for r in hand])
    clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0).fit(xtr, ytr)
    pred = clf.predict(xte)
    cm = confusion_matrix(yte, pred, labels=labels)
    ci = labels.index("crisis")
    rec = cm[ci, ci] / cm[ci].sum() if cm[ci].sum() else float("nan")
    leaked = cm[ci, labels.index("safe")] + cm[ci, labels.index("clarify")]
    print(f"\n=== generalization test: train on {len(train)} templated, test on {len(hand)} hand-authored ===")
    print(f"  accuracy on unfamiliar phrasing: {100*(pred==yte).mean():.1f}%")
    print(f"  crisis recall on unfamiliar phrasing: {100*rec:.1f}%  (leaked {leaked}/{cm[ci].sum()})")
    print("  the drop from the in-distribution recall above is the real generalization gap.")


def run(a):
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedGroupKFold, train_test_split
    from sklearn.metrics import classification_report, confusion_matrix
    from sklearn.cluster import KMeans

    rows = [__import__("json").loads(l) for l in open(a.set)]
    labels = ["safe", "clarify", "block", "crisis"]
    y = np.array([r["verdict"] for r in rows])
    groups = np.array([r.get("group", r["target"]) for r in rows])
    enc = encoder(a.mode)
    X, feat, mode = features(rows, a.mode, enc)
    print(f"items: {len(rows)}   features: {'MiniLM' if mode == 'emb' else 'TF-IDF'}   classes: {dict(Counter(y))}")
    print("note: synthetic data, numbers are an optimistic baseline.\n")

    skf = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=7)
    pred = np.empty(len(rows), dtype=object)
    proba = np.zeros((len(rows), 4))
    for tr, te in skf.split(X, y, groups):
        clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0).fit(X[tr], y[tr])
        pred[te] = clf.predict(X[te])
        p = clf.predict_proba(X[te])
        for j, c in enumerate(clf.classes_):
            proba[te, labels.index(c)] = p[:, j]

    print("=== 5-fold CV ===")
    print(classification_report(y, pred, labels=labels, digits=3, zero_division=0))
    cm = confusion_matrix(y, pred, labels=labels)
    print("confusion (rows=true, cols=pred):")
    print("        " + "".join(f"{v:>9}" for v in labels))
    for i, g in enumerate(labels):
        print(f"  {g:6}" + "".join(f"{cm[i][j]:9}" for j in range(4)))
    ci = labels.index("crisis")
    rec = cm[ci, ci] / cm[ci].sum()
    leaked = cm[ci, labels.index("safe")] + cm[ci, labels.index("clarify")]
    print(f"\ncrisis recall: {100*rec:.1f}%   leaked to safe/clarify: {leaked}/{cm[ci].sum()}")

    gen_gap(rows, mode, enc)

    print(f"\n=== conformal crisis threshold (target {int(100*a.target)}% recall) ===")
    idx = np.arange(len(rows))
    np.random.RandomState(1).shuffle(idx)
    cal, test = idx[:len(idx) // 2], idx[len(idx) // 2:]
    scores = np.sort(proba[cal, ci][y[cal] == "crisis"])
    tau = scores[max(0, int((1 - a.target) * len(scores)) - 1)] if len(scores) else 0.5
    flag = proba[test, ci] >= tau
    trec = np.mean(flag[y[test] == "crisis"]) if (y[test] == "crisis").any() else float("nan")
    over = np.mean(flag[y[test] != "crisis"])
    print(f"  tau={tau:.3f}  test recall {100*trec:.1f}%, non-crisis flagged {100*over:.1f}%")

    if mode != "emb":
        vw, vc = feat
        names = np.array(vw.get_feature_names_out().tolist() + vc.get_feature_names_out().tolist())
        clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0).fit(X, y)
        print("\n=== top tokens per verdict ===")
        for c in labels:
            top = np.argsort(clf.coef_[list(clf.classes_).index(c)])[-8:][::-1]
            print(f"  {c:8} " + ", ".join(str(names[t]) for t in top))

    err = np.where(pred != y)[0]
    if len(err) >= 6:
        k = min(4, len(err) // 3)
        km = KMeans(n_clusters=k, n_init=5, random_state=0).fit(X[err] if mode == "emb" else X[err].toarray())
        print(f"\n=== {len(err)} errors in {k} failure modes ===")
        for c in range(k):
            ms = err[km.labels_ == c][:3]
            print(f"  mode {c} (n={sum(km.labels_==c)}): " + " | ".join(f"[{rows[m]['verdict']}->{pred[m]}] {rows[m]['target']}" for m in ms))

    print("\n=== learning curve ===")
    xtr, xte, ytr, yte = train_test_split(X, y, test_size=0.3, stratify=y, random_state=0)
    for frac in (0.2, 0.4, 0.6, 0.8, 1.0):
        n = int(frac * xtr.shape[0])
        clf = LogisticRegression(max_iter=2000, class_weight="balanced", C=4.0).fit(xtr[:n], ytr[:n])
        c2 = confusion_matrix(yte, clf.predict(xte), labels=labels)
        print(f"  train={n:5}  crisis-recall={100*c2[ci,ci]/max(1,c2[ci].sum()):5.1f}%")

    ent = -np.sum(np.clip(proba, 1e-9, 1) * np.log(np.clip(proba, 1e-9, 1)), axis=1)
    print("\n=== most uncertain (label next) ===")
    for t in np.argsort(ent)[-8:][::-1]:
        print(f"  H={ent[t]:.2f}  [{y[t]}]  {rows[t]['target']} / {rows[t]['charge'][:45]}")

    import csv
    with open("bench/ml_predictions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "target", "charge", "gold", "pred", "p_crisis", "entropy"])
        for i, r in enumerate(rows):
            w.writerow([r["id"], r["target"], r["charge"], y[i], pred[i], f"{proba[i,ci]:.3f}", f"{ent[i]:.3f}"])
    print("\nsaved bench/ml_predictions.csv")


ap = argparse.ArgumentParser()
ap.add_argument("--set", default="bench/rewire_eval_set.jsonl")
ap.add_argument("--emb", dest="mode", action="store_const", const="emb", default="tfidf")
ap.add_argument("--target", type=float, default=0.95)
run(ap.parse_args())