"""Agentic-RL Sandbox Benchmark - main orchestrator.

Workflow:
  1. Detect EC2 instance metadata
  2. For each runner not in SKIP:
       - warmup
       - sweep concurrencies, run_one, save JSON locally + S3
       - cooldown
  3. Aggregate all per-(benchmark, concurrency) JSON into summary.json
  4. Render minimal HTML report
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from config import CFG, Config
from instance_meta import detect_instance
from runners import ALL_RUNNERS
from runners.base import BenchmarkResult
from s3_uploader import ResultStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# httpx logs every request at INFO regardless of status (e.g.
# `HTTP Request: POST ... "HTTP/1.1 200 OK"`). At benchmark concurrency
# this floods the log. Errors are surfaced via response.raise_for_status()
# in the runners themselves, so suppress httpx's own access lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("orchestrator")


async def run_benchmark(
    bid: str, cfg: Config, instance: dict, store: ResultStore
) -> list[dict]:
    runner = ALL_RUNNERS[bid]
    log.info("=== %s (%s) starting ===", bid, runner.workload)

    log.info("[%s] warmup ...", bid)
    try:
        await runner.warmup(cfg)
    except Exception as e:
        log.error("[%s] warmup failed: %s - skipping", bid, e)
        return []

    results: list[dict] = []
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = (
        f"{runner.workload}_result_{instance['instance_type']}_{ts}"
    )

    for c in cfg.concurrencies:
        log.info("[%s] concurrency=%d duration=%ds", bid, c, cfg.duration_sec)
        try:
            res: BenchmarkResult = await runner.run_one(cfg, instance, c)
        except Exception as e:
            log.error("[%s] run failed at c=%d: %s", bid, c, e)
            continue
        d = res.to_dict()
        results.append(d)

        rel = f"{instance['arch']}/{out_dir}/c{c:04d}.json"
        store.write_json(rel, d)
        log.info(
            "[%s] c=%d throughput=%s p99=%.1fms",
            bid, c,
            list(d["throughput"].items())[0] if d["throughput"] else "?",
            (d["latency_ms"].get("p99")
             if isinstance(d["latency_ms"], dict) and "p99" in d["latency_ms"]
             else d["latency_ms"].get("trajectory", {}).get("p99", 0.0)),
        )

        if cfg.cooldown_sec > 0:
            log.info("[%s] cooldown %ds", bid, cfg.cooldown_sec)
            await asyncio.sleep(cfg.cooldown_sec)

    # Per-benchmark roll-up
    rollup = {
        "benchmark": bid,
        "workload": runner.workload,
        "instance": instance,
        "runs": results,
    }
    store.write_json(f"{instance['arch']}/{out_dir}/rollup.json", rollup)
    log.info("=== %s done, %d concurrency points ===", bid, len(results))
    return results


def render_html(summary: dict, out_path: Path) -> None:
    """Minimal Chart.js report - single page summary."""
    rows = []
    for bid, runs in summary["benchmarks"].items():
        for r in runs:
            tput = next(iter(r["throughput"].values()), 0) if r["throughput"] else 0
            lat = r["latency_ms"]
            if isinstance(lat, dict) and "p99" in lat:
                p99 = lat.get("p99")
            elif isinstance(lat, dict):
                p99 = lat.get("trajectory", {}).get("p99")
            else:
                p99 = None
            rows.append({
                "benchmark": bid,
                "concurrency": r["concurrency"],
                "throughput": tput,
                "p99_ms": p99,
                "cpu_avg": r["resource"].get("cpu_util_avg"),
                "cost_per_1k": r["cost"].get("cost_per_1k_units_usd"),
            })

    def cell(v, fmt=".2f"):
        if v is None:
            return "<td>-</td>"
        return f"<td>{v:{fmt}}</td>"

    body_rows = "".join(
        f"<tr><td style='text-align:left'>{r['benchmark']}</td>"
        f"<td>{r['concurrency']}</td>"
        f"{cell(r['throughput'], '.2f')}"
        f"{cell(r['p99_ms'], '.1f')}"
        f"{cell(r['cpu_avg'], '.1f')}"
        f"{cell(r['cost_per_1k'], '.4f')}"
        "</tr>"
        for r in rows
    )

    inst = summary["instance"]
    html = f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Agentic-RL Sandbox Bench - {inst['instance_type']}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0"></script>
<style>
body{{font-family:system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:right}}
th{{background:#f5f5f5}}
h1{{font-size:1.4rem}} h2{{font-size:1.1rem;margin-top:2rem}}
.meta{{color:#666;font-size:.9rem}}
</style></head><body>
<h1>Agentic-RL Sandbox Benchmark</h1>
<p class="meta">
  Instance: <b>{inst['instance_type']}</b> ({inst['arch']})
  &middot; Region: {inst['region']}
  &middot; Generated: {summary['generated_at']}
</p>
<h2>All runs</h2>
<table>
<tr><th>Benchmark</th><th>Concurrency</th><th>Throughput (ops/s)</th>
    <th>P99 (ms)</th><th>CPU avg (%)</th><th>$/1k ops</th></tr>
{body_rows}
</table>
</body></html>
"""
    out_path.write_text(html)


async def main() -> int:
    cfg = CFG
    instance = await detect_instance()
    log.info("instance=%s arch=%s region=%s",
             instance["instance_type"], instance["arch"], instance["region"])

    store = ResultStore(
        results_dir=cfg.results_dir,
        bucket=cfg.s3_bucket,
        prefix=cfg.s3_prefix,
        region=cfg.aws_region,
    )

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "instance": instance,
        "config": {
            "duration_sec": cfg.duration_sec,
            "concurrencies": cfg.concurrencies,
        },
        "benchmarks": {},
    }

    for bid in sorted(ALL_RUNNERS.keys()):
        if bid in cfg.skip:
            log.info("skipping %s (in SKIP)", bid)
            continue
        runs = await run_benchmark(bid, cfg, instance, store)
        summary["benchmarks"][bid] = runs

    # Final summary + report
    ts = time.strftime("%Y%m%d-%H%M%S")
    summary_rel = f"{instance['arch']}/summary_{instance['instance_type']}_{ts}.json"
    store.write_json(summary_rel, summary)

    report_rel = f"{instance['arch']}/report_{instance['instance_type']}_{ts}.html"
    report_path = Path(cfg.results_dir) / report_rel
    report_path.parent.mkdir(parents=True, exist_ok=True)
    render_html(summary, report_path)
    store.write_text(report_rel, report_path.read_text())

    log.info("ALL DONE. Local: %s | S3: s3://%s/%s",
             cfg.results_dir, cfg.s3_bucket or "-", cfg.s3_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
