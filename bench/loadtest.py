import argparse, asyncio, time, csv, httpx
from collections import Counter

STAGES = ["queued", "generating", "synthesizing", "mixing", "done"]


async def user(client, i, out):
    t0 = time.time()
    seen = {}
    try:
        r = await client.post("/api/auth/register",
            json={"email": f"lt{i}_{int(t0*1e6)}@t.com", "password": "secret123", "first_name": "U"})
        if r.status_code != 200:
            out.append({"outcome": "register_fail", "ttd": time.time() - t0}); return
        h = {"Authorization": "Bearer " + r.json()["token"]}
        r = await client.post("/api/protocols",
            json={"type": "activate", "target": "Run three mornings this week", "charge": "feel like myself"}, headers=h)
        if r.status_code != 200 or r.json().get("status") != "created":
            out.append({"outcome": "create_fail", "ttd": time.time() - t0}); return
        pid = r.json()["protocol"]["id"]
        r = await client.post(f"/api/protocol-jolt/{pid}/start", json={"day": 1}, headers=h)
        if r.status_code != 200:
            out.append({"outcome": "start_fail", "ttd": time.time() - t0}); return
        jid = r.json()["jolt_id"]
        while time.time() - t0 < 360:
            s = (await client.get(f"/api/protocol-jolt/{jid}/status", headers=h)).json()
            st = s["stage"]
            if st not in seen:
                seen[st] = time.time() - t0
            if st == "done":
                row = {"outcome": "done", "ttd": time.time() - t0}
                for a, b in zip(STAGES, STAGES[1:]):
                    if a in seen and b in seen:
                        row["dwell_" + a] = round(seen[b] - seen[a], 2)
                out.append(row); return
            if st in ("error", "blocked"):
                out.append({"outcome": st, "ttd": time.time() - t0}); return
            await asyncio.sleep(0.3)
        out.append({"outcome": "timeout", "ttd": time.time() - t0})
    except Exception as e:
        out.append({"outcome": "exc:" + type(e).__name__, "ttd": time.time() - t0})


async def level(base, c):
    out = []
    async with httpx.AsyncClient(base_url=base, timeout=60, trust_env=False,
            limits=httpx.Limits(max_connections=c + 20)) as client:
        t0 = time.time()
        await asyncio.gather(*[user(client, i, out) for i in range(c)])
        wall = time.time() - t0
    return out, wall


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


async def main(a):
    levels = [int(x) for x in a.ramp.split(",")]
    rows = []
    best = 0
    print(f"ramping {levels} vs {a.base}   target: success>={a.success}%  p95<={a.bed}s\n")
    for c in levels:
        res, wall = await level(a.base, c)
        done = [r["ttd"] for r in res if r["outcome"] == "done"]
        succ = 100 * len(done) / c
        oc = Counter(r["outcome"] for r in res)
        line = f"c={c:4}  success={succ:5.1f}%  thru={len(done)/wall:5.2f}/s  " \
               f"p50={pct(done,.5):6.2f}s p95={pct(done,.95):6.2f}s p99={pct(done,.99):6.2f}s"
        for stg in STAGES[:-1]:
            ds = [r.get("dwell_" + stg) for r in res if r.get("dwell_" + stg) is not None]
            if ds:
                line += f"  {stg[:4]}~{sum(ds)/len(ds):.1f}s"
        print(line)
        bad = {k: v for k, v in dict(oc).items() if k != "done"}
        if bad:
            print(f"        non-done: {bad}")
        for r in res:
            r["concurrency"] = c
            rows.append(r)
        if succ >= a.success and pct(done, .95) <= a.bed:
            best = c
        if succ < a.success * 0.6:
            print(f"        tipped over at c={c}, stopping.")
            break
    print(f"\nmax concurrency meeting target = {best}")
    keys = sorted({k for r in rows for k in r})
    with open("bench/loadtest_results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader(); w.writerows(rows)
    print("saved bench/loadtest_results.csv")


ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--ramp", default="5,10,25,50,100")
ap.add_argument("--success", type=float, default=95.0)
ap.add_argument("--bed", type=float, default=150.0)
asyncio.run(main(ap.parse_args()))