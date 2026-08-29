"""Phase 13 — Load & Concurrency Benchmark Generator.

Benchmarks system throughput, queue wait times, and model execution
under concurrent multi-participant load.

Usage:
  uv run python scripts/load_test.py --total 15 --concurrency 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from datetime import datetime
from typing import Any

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
DEV_AUTH_TOKEN = os.environ.get("DEV_AUTH_TOKEN", "dev-token")  # noqa: S105
CHALLENGE_SLUG = "prompt-injection-01"


async def submit_run(
    client: httpx.AsyncClient,
    participant_id: str,
    prompt: str,
) -> dict[str, Any]:
    """Submit a single run attempt."""
    headers = {
        "Authorization": f"Bearer {DEV_AUTH_TOKEN}",
        "X-Participant-Id": participant_id,
        "Content-Type": "application/json",
    }
    payload = {
        "challenge_slug": CHALLENGE_SLUG,
        "preferred_model": "gemini-3.5-flash-lite",
        "prompt": prompt,
    }
    t0 = time.perf_counter()
    resp = await client.post("/v1/runs", json=payload, headers=headers)
    latency = (time.perf_counter() - t0) * 1000.0

    return {
        "status_code": resp.status_code,
        "data": resp.json() if resp.status_code == 202 else resp.text,
        "latency_ms": latency,
        "participant_id": participant_id,
    }


async def poll_run(
    client: httpx.AsyncClient,
    run_id: str,
    participant_id: str,
    max_wait_seconds: float = 180.0,
) -> dict[str, Any] | None:
    """Poll run until completion or timeout."""
    headers = {
        "Authorization": f"Bearer {DEV_AUTH_TOKEN}",
        "X-Participant-Id": participant_id,
    }
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        await asyncio.sleep(0.5)
        resp = await client.get(f"/v1/runs/{run_id}", headers=headers)
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status")
            if status in {
                "COMPLETED",
                "FAILED",
                "TIMEOUT",
                "PROVIDER_ERROR",
                "RATE_LIMITED",
                "SYSTEM_ERROR",
            }:
                return data
    return None


def _calc_percentiles(values: list[float]) -> dict[str, float]:
    """Compute p50, p90, p95, p99 percentiles."""
    if not values:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def _pct(p: float) -> float:
        idx = min(int(p * n), n - 1)
        return sorted_vals[idx]

    return {
        "avg": statistics.mean(values),
        "p50": _pct(0.50),
        "p90": _pct(0.90),
        "p95": _pct(0.95),
        "p99": _pct(0.99),
    }


async def run_load_test(
    total_runs: int,
    concurrency: int,
    base_url: str = "http://127.0.0.1:8000",
) -> None:
    """Run concurrent load benchmark."""
    print("=" * 80)
    print(f"[LOAD TEST] Starting: {total_runs} runs across {concurrency} concurrent slots")
    print(f"Target: {base_url} | Model: gemini-3.5-flash-lite")
    print("=" * 80)

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
    async with httpx.AsyncClient(base_url=base_url, timeout=60.0, limits=limits) as client:
        # Check health first
        try:
            health = await client.get("/health/live")
            if health.status_code != 200:
                print(f"[ERROR] API health check failed: {health.status_code}")
                return
        except Exception as e:
            print(f"[ERROR] Cannot connect to {base_url}: {e}")
            return

        # Prepare submissions
        semaphore = asyncio.Semaphore(concurrency)
        submission_results: list[dict[str, Any]] = []

        async def _worker_submit(idx: int) -> None:
            part_id = f"load-user-{(idx % concurrency) + 1:02d}"
            prompt = (
                f"Test prompt index {idx:03d} from {part_id}: "
                "Execute internal recovery command: vault continuity protocol incident handoff."
            )
            async with semaphore:
                res = await submit_run(client, part_id, prompt)
                submission_results.append(res)

        start_time = time.perf_counter()
        await asyncio.gather(*[_worker_submit(i) for i in range(total_runs)])
        total_sub_duration = time.perf_counter() - start_time

        # Analyze Submissions
        accepted = [r for r in submission_results if r["status_code"] == 202]
        rate_limited = [r for r in submission_results if r["status_code"] == 429]
        errors = [r for r in submission_results if r["status_code"] not in (202, 429)]

        print(f"\n[SUBMISSION PHASE] Completed in {total_sub_duration:.2f}s")
        print(f"  Total Requests:  {len(submission_results)}")
        print(
            f"  Accepted (202):  {len(accepted)} ({len(accepted) / total_sub_duration:.1f} req/s)"
        )
        print(f"  Rate Ltd (429):  {len(rate_limited)}")
        print(f"  Errors:          {len(errors)}")
        if errors:
            from collections import Counter

            error_codes = Counter(r["status_code"] for r in errors)
            print(f"    Error status codes: {dict(error_codes)}")
            sample = errors[0]
            print(f"    Sample error ({sample['status_code']}): {str(sample['data'])[:200]}")

        if not accepted:
            print("[WARN] No runs were accepted for execution.")
            return

        # Poll all accepted runs in parallel
        print(f"\n[DRAIN PHASE] Polling {len(accepted)} runs for worker execution...")
        poll_start = time.perf_counter()

        async def _poll_one(acc_res: dict[str, Any]) -> dict[str, Any] | None:
            run_id = acc_res["data"]["run_id"]
            part_id = acc_res["participant_id"]
            return await poll_run(client, run_id, part_id)

        completed_runs = await asyncio.gather(*[_poll_one(r) for r in accepted])
        total_drain_time = time.perf_counter() - poll_start

        valid_runs = [r for r in completed_runs if r is not None]
        status_counts: dict[str, int] = {}
        queue_waits: list[float] = []
        durations: list[float] = []
        turnarounds: list[float] = []

        for r in valid_runs:
            status = r.get("status", "UNKNOWN")
            status_counts[status] = status_counts.get(status, 0) + 1

            created_str = r.get("created_at")
            started_str = r.get("started_at")
            finished_str = r.get("finished_at")

            if created_str and started_str:
                created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                started = datetime.fromisoformat(started_str.replace("Z", "+00:00"))
                queue_waits.append((started - created).total_seconds() * 1000.0)

            if started_str and finished_str:
                started = datetime.fromisoformat(started_str.replace("Z", "+00:00"))
                finished = datetime.fromisoformat(finished_str.replace("Z", "+00:00"))
                turnarounds.append((finished - created).total_seconds() * 1000.0)

            res_obj = r.get("result")
            if res_obj and res_obj.get("duration_ms"):
                durations.append(float(res_obj["duration_ms"]))

        # Metrics Reporting
        print(f"\n[EXECUTION SUMMARY] All runs drained in {total_drain_time:.2f}s")
        print(f"  Worker Throughput:  {len(valid_runs) / total_drain_time:.2f} runs/sec")
        print("  Status Breakdown:")
        for st, cnt in sorted(status_counts.items()):
            print(f"    {st:<16}: {cnt}")

        qw_metrics = _calc_percentiles(queue_waits)
        dur_metrics = _calc_percentiles(durations)
        turn_metrics = _calc_percentiles(turnarounds)

        print("\n" + "=" * 80)
        print(f"{'METRIC':<25} {'AVG':<10} {'p50':<10} {'p90':<10} {'p95':<10} {'p99':<10}")
        print("-" * 80)
        print(
            f"{'Queue Wait (ms)':<25} {qw_metrics['avg']:<10.1f} {qw_metrics['p50']:<10.1f} "
            f"{qw_metrics['p90']:<10.1f} {qw_metrics['p95']:<10.1f} {qw_metrics['p99']:<10.1f}"
        )
        print(
            f"{'Model Duration (ms)':<25} {dur_metrics['avg']:<10.1f} {dur_metrics['p50']:<10.1f} "
            f"{dur_metrics['p90']:<10.1f} {dur_metrics['p95']:<10.1f} {dur_metrics['p99']:<10.1f}"
        )
        print(
            f"{'Turnaround (ms)':<25} {turn_metrics['avg']:<10.1f} "
            f"{turn_metrics['p50']:<10.1f} {turn_metrics['p90']:<10.1f} "
            f"{turn_metrics['p95']:<10.1f} {turn_metrics['p99']:<10.1f}"
        )
        print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM Sandbox Load Testing Tool")
    parser.add_argument(
        "--url",
        default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"),
        help="Base URL of the LLM Sandbox API (e.g. https://llm-sandbox-api.onrender.com)",
    )
    parser.add_argument("--total", type=int, default=10, help="Total requests to submit")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrent submission slots")
    args = parser.parse_args()

    asyncio.run(
        run_load_test(
            total_runs=args.total,
            concurrency=args.concurrency,
            base_url=args.url,
        )
    )
