import argparse, heapq


def sim(n, workers, job):
    free = [0.0] * workers
    heapq.heapify(free)
    out = []
    for _ in range(n):
        s = heapq.heappop(free)
        heapq.heappush(free, s + job)
        out.append(s + job)
    out.sort()
    return out


ap = argparse.ArgumentParser()
ap.add_argument("--users", type=int, default=100)
ap.add_argument("--job-sec", type=float, default=90.0)
ap.add_argument("--bed-sec", type=float, default=150.0)
a = ap.parse_args()

print(f"{a.users} users at once, {a.job_sec:.0f}s per jolt:\n")
for w in (2, 4, 8, 16, 24, 32, 48):
    d = sim(a.users, w, a.job_sec)
    p50, p95, last = d[len(d) // 2], d[int(.95 * len(d))], d[-1]
    ok = sum(1 for x in d if x <= a.bed_sec)
    print(f"  workers={w:3}  p50={p50/60:5.1f}m  p95={p95/60:5.1f}m  last={last/60:5.1f}m   within bed: {ok}/{a.users}")