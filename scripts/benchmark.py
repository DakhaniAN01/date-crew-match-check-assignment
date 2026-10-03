"""Concurrent load test against a running server.

Simulates many matchmakers using the tool at once: login, list clients, open a
shortlist, run pre-share checks. Reports throughput and latency percentiles.

    RATE_LIMIT_PER_MINUTE=1000000 uvicorn app.main:app --port 8078
    python scripts/benchmark.py --base http://127.0.0.1:8078 --users 50 --requests 40
"""

import argparse
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.seed import DEMO_PASSWORD  # noqa: E402

ACCOUNTS = ["priya@datecrew.test", "rahul@datecrew.test", "meera@datecrew.test"]


def session(base: str, n_requests: int, seed: int, ready: threading.Barrier) -> tuple[float, list[float]]:
    rng = random.Random(seed)
    timings = []
    with httpx.Client(base_url=base, timeout=30) as c:
        t = time.perf_counter()
        tok = c.post("/api/auth/login", json={"email": rng.choice(ACCOUNTS), "password": DEMO_PASSWORD}).json()
        login_ms = (time.perf_counter() - t) * 1000
        h = {"Authorization": f"Bearer {tok['access_token']}"}
        clients = c.get("/api/clients", headers=h).json()["data"]
        ready.wait()  # everyone is signed in, now measure steady-state traffic
        for _ in range(n_requests):
            cl = rng.choice(clients)["id"]
            if rng.random() < 0.4:
                req = lambda: c.get(f"/api/clients/{cl}/shortlist", headers=h)  # noqa: E731
            else:
                cand = f"TDC-{rng.randint(1, 20000):05d}"
                req = lambda: c.post("/api/match/check", headers=h, json={"client_id": cl, "candidate_id": cand})  # noqa: E731
            t = time.perf_counter()
            r = req()
            timings.append((time.perf_counter() - t) * 1000)
            r.raise_for_status()
    return login_ms, timings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8078")
    ap.add_argument("--users", type=int, default=50, help="concurrent simulated users")
    ap.add_argument("--requests", type=int, default=40, help="requests per user")
    args = ap.parse_args()

    ready = threading.Barrier(args.users + 1)
    with ThreadPoolExecutor(max_workers=args.users) as pool:
        futures = [pool.submit(session, args.base, args.requests, i, ready) for i in range(args.users)]
        ready.wait()
        started = time.perf_counter()
        results = [f.result() for f in futures]
    elapsed = time.perf_counter() - started
    logins = sorted(r[0] for r in results)
    all_t = sorted(t for r in results for t in r[1])
    q = statistics.quantiles(all_t, n=100)
    print(f"{len(all_t)} requests from {args.users} concurrent users in {elapsed:.1f}s ({len(all_t) / elapsed:.0f} req/s)")
    print(f"latency ms  p50 {q[49]:.1f}  p95 {q[94]:.1f}  p99 {q[98]:.1f}  max {all_t[-1]:.1f}")
    print(f"login ms (deliberately slow password hashing, {args.users} at once)  median {statistics.median(logins):.0f}")


if __name__ == "__main__":
    main()
