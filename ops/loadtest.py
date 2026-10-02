"""Layer 11 — measure, do not guess. Names the current bottleneck with numbers."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request


def one(
    url: str, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None
) -> tuple[int, float]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
            code = r.status
    except urllib.error.HTTPError as e:
        e.read()
        code = e.code
    except Exception:
        code = 0
    return code, (time.perf_counter() - t0) * 1000


def run(url: str, n: int, conc: int, method: str = "GET", make_body=None) -> dict[str, object]:
    lat: list[float] = []
    codes: dict[int, int] = {}
    lock = threading.Lock()
    counter = {"i": 0}

    def worker() -> None:
        while True:
            with lock:
                if counter["i"] >= n:
                    return
                i = counter["i"]
                counter["i"] += 1
            body = headers = None
            if make_body:
                payload, headers = make_body(i)
                body = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
            code, ms = one(url, method, body, headers)
            with lock:
                lat.append(ms)
                codes[code] = codes.get(code, 0) + 1

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(conc)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    lat.sort()

    def pct(p: float) -> float:
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 1) if lat else 0.0

    return {
        "url": url,
        "requests": n,
        "concurrency": conc,
        "wall_s": round(wall, 2),
        "rps": round(n / wall, 1),
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "p99_ms": pct(0.99),
        "max_ms": round(max(lat), 1) if lat else 0,
        "mean_ms": round(statistics.mean(lat), 1) if lat else 0,
        "status": codes,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8080")
    ap.add_argument("-n", type=int, default=300)
    ap.add_argument("-c", type=int, default=20)
    a = ap.parse_args()

    print("=" * 68)
    print("LOAD TEST — layer 11")
    print("=" * 68)
    results = []

    for label, path in [
        ("static html (cached, fingerprinted assets)", "/index.html"),
        ("health endpoint (no db)", "/api/healthz"),
        ("readiness (db read)", "/api/readyz"),
    ]:
        r = run(f"{a.base}{path}", a.n, a.c)
        r["label"] = label
        results.append(r)
        print(
            f"\n{label}\n  rps={r['rps']}  p50={r['p50_ms']}ms  p95={r['p95_ms']}ms  "
            f"p99={r['p99_ms']}ms  codes={r['status']}"
        )

    def body(i: int):
        return (
            {
                "name": f"بیمار تست {i}",
                "phone": f"0912{i:07d}",
                "service": "consult",
                "note": "load test",
            },
            {"Idempotency-Key": f"load-{time.time()}-{i}"},
        )

    r = run(f"{a.base}/api/bookings", min(a.n, 120), a.c, "POST", body)
    r["label"] = "booking write (db write + delivery, rate limited on purpose)"
    results.append(r)
    print(
        f"\n{r['label']}\n  rps={r['rps']}  p50={r['p50_ms']}ms  p95={r['p95_ms']}ms  "
        f"codes={r['status']}"
    )

    slowest = max(results, key=lambda x: x["p95_ms"])
    print("\n" + "=" * 68)
    print(f"BOTTLENECK: {slowest['label']}")
    print(f"  p95 = {slowest['p95_ms']} ms at concurrency {slowest['concurrency']}")
    print(f"  throughput = {slowest['rps']} req/s")
    print("=" * 68)
    pathlib_write(results)
    return 0


def pathlib_write(results: list[dict[str, object]]) -> None:
    import pathlib

    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "loadtest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"written: {out}")


if __name__ == "__main__":
    sys.exit(main())
