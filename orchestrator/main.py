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

# Use uvloop where available - the default asyncio event loop tops out
# around 50-70k req/s per process; uvloop typically doubles that and
# directly translates to higher CPU utilisation when many tiny HTTP
# requests are in flight (B3 / B7 / B9 workloads).
try:
    import uvloop  # type: ignore
    uvloop.install()
except ImportError:
    pass

# Configure logging BEFORE importing runners. The runners package
# instantiates Runner objects at import time (see runners/__init__.py),
# which triggers e.g. B1's corpus load and its INFO log line. If we
# configure logging after the import, those startup messages get
# discarded by the default last-resort handler.
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

from config import CFG, Config
from instance_meta import detect_instance
from runners import ALL_RUNNERS
from runners.base import BenchmarkResult
from s3_uploader import ResultStore

log = logging.getLogger("orchestrator")


def _extract_p99(latency: dict) -> float:
    """Pull a primary P99 number out of the polymorphic latency block.

    Different runners use different shapes:
      - B1/B3/B4: flat dict {p50, p95, p99, ...}
      - B5:       {"trajectory": {p99,...}, "per_query": {...}}
      - B9:       {"rollout":    {p99,...}, "per_task": {...}}
    """
    if not isinstance(latency, dict):
        return 0.0
    if "p99" in latency:
        return latency.get("p99") or 0.0
    for key in ("rollout", "trajectory"):
        sub = latency.get(key)
        if isinstance(sub, dict) and "p99" in sub:
            return sub.get("p99") or 0.0
    return 0.0


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

    # Per-benchmark concurrency / duration overrides. B9 in particular
    # ramps 64 -> 256 -> 1024 over 30min, distinct from the per-bench
    # sweep used by B1/B3/B5.
    # B4 (browser) caps lower: each in-flight context spawns Chromium
    # renderer processes, and at c=128+ aarch64 hosts in particular
    # exhaust kernel fork paths and page-cache headroom, producing
    # negative scaling. We use a dedicated sweep that stops where the
    # workload still has signal.
    concurrencies = cfg.concurrencies
    duration = cfg.duration_sec
    if bid == "B9":
        concurrencies = cfg.b9_concurrencies
        duration = cfg.b9_duration_sec
    elif bid == "B4":
        concurrencies = cfg.b4_concurrencies or cfg.concurrencies

    for c in concurrencies:
        log.info("[%s] concurrency=%d duration=%ds", bid, c, duration)
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
            _extract_p99(d["latency_ms"]),
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


# Per-benchmark labels for the report. Each entry says which key to
# pull out of the throughput dict and how to spell it. We deliberately
# keep ops/s out of the label - "ops" hides what's actually being
# counted (a code-exec is very different from a 30-step rollout).
PRIMARY_UNITS = {
    "B1": {"tput_key": "executions_per_sec",     "unit": "exec/s",     "title": "Code Execution"},
    "B3": {"tput_key": "trajectories_per_sec",   "unit": "traj/s",     "title": "Tool Call"},
    "B4": {"tput_key": "trajectories_per_sec",   "unit": "traj/s",     "title": "Browser"},
    "B5": {"tput_key": "queries_per_sec",        "unit": "queries/s",  "title": "SQL Exec (TPC-H)"},
    "B7": {"tput_key": "episodes_per_sec",       "unit": "ep/s",       "title": "Text Game"},
    "B8": {"tput_key": "container_starts_per_sec", "unit": "starts/s", "title": "Cold Start"},
    "B9": {"tput_key": "rollouts_per_sec",       "unit": "rollouts/s", "title": "Concurrent Rollout"},
}


def _primary_throughput(bid: str, throughput: dict) -> tuple[float, str]:
    """Return (value, unit_label) for the row's primary throughput.

    Falls back to the first entry of `throughput` if `bid` isn't in
    the table or the expected key is missing - avoids silently zeroing
    out throughput when a runner is added without updating PRIMARY_UNITS.
    """
    if not isinstance(throughput, dict) or not throughput:
        return 0.0, "ops/s"
    info = PRIMARY_UNITS.get(bid)
    if info and info["tput_key"] in throughput:
        return throughput[info["tput_key"]] or 0.0, info["unit"]
    # Fallback: first numeric value, generic label.
    k, v = next(iter(throughput.items()))
    return (v or 0.0), "ops/s"


def render_html(summary: dict, out_path: Path) -> None:
    """Minimal report - main table + per-benchmark sub-task sections.

    Improvements over a flat ops/s view:
      1. Throughput cell shows the right unit per benchmark (exec/s,
         traj/s, queries/s, rollouts/s, ...) so the same number isn't
         mis-read across rows.
      2. B5 emits a per-query (Q01..Q22) latency table for each
         concurrency level. B9 emits a per-task (B1/B3/B4/B5/B7)
         latency table and ops breakdown for each concurrency level.
         Without these, the sub-task data the runners already collect
         is invisible from the report.
    """

    def cell(v, fmt=".2f"):
        if v is None:
            return "<td>-</td>"
        return f"<td>{v:{fmt}}</td>"

    # ----- main table -----
    main_rows: list[str] = []
    for bid, runs in summary["benchmarks"].items():
        title = PRIMARY_UNITS.get(bid, {}).get("title", "")
        bid_label = f"{bid}<br><span style='color:#888;font-size:.8em'>{title}</span>" if title else bid
        for r in runs:
            tput, unit = _primary_throughput(bid, r["throughput"])
            p99 = _extract_p99(r["latency_ms"])
            cpu_avg = r["resource"].get("cpu_util_avg")
            cost_1k = r["cost"].get("cost_per_1k_units_usd")
            main_rows.append(
                f"<tr><td style='text-align:left'>{bid_label}</td>"
                f"<td>{r['concurrency']}</td>"
                f"<td>{tput:,.2f} <span style='color:#888;font-size:.85em'>{unit}</span></td>"
                f"{cell(p99, '.1f')}"
                f"{cell(cpu_avg, '.1f')}"
                f"{cell(cost_1k, '.4f')}"
                "</tr>"
            )
    body_rows = "".join(main_rows)

    # ----- B5 per-query sub-table per concurrency -----
    def render_per_query_section(bid: str, runs: list[dict]) -> str:
        if not runs:
            return ""
        if not any(isinstance(r.get("latency_ms"), dict)
                   and r["latency_ms"].get("per_query") for r in runs):
            return ""
        out = [f"<h2>{bid} - per-query latency (TPC-H Q01..Q22)</h2>"]
        for r in runs:
            pq = (r.get("latency_ms") or {}).get("per_query") or {}
            if not pq:
                continue
            out.append(f"<h3 style='font-size:1rem;margin-top:1rem'>c={r['concurrency']}</h3>")
            out.append("<table>")
            out.append(
                "<tr><th style='text-align:left'>Query</th><th>count</th>"
                "<th>p50 (ms)</th><th>p95 (ms)</th><th>p99 (ms)</th>"
                "<th>mean (ms)</th><th>max (ms)</th></tr>"
            )
            # sort by query id (keys like Q01..Q22)
            for qkey in sorted(pq.keys()):
                row = pq[qkey] or {}
                out.append(
                    f"<tr><td style='text-align:left'>{qkey}</td>"
                    f"<td>{int(row.get('count', 0))}</td>"
                    f"{cell(row.get('p50'), '.1f')}"
                    f"{cell(row.get('p95'), '.1f')}"
                    f"{cell(row.get('p99'), '.1f')}"
                    f"{cell(row.get('mean'), '.1f')}"
                    f"{cell(row.get('max'), '.1f')}"
                    "</tr>"
                )
            out.append("</table>")
        return "\n".join(out)

    # ----- B9 per-task + ops breakdown sub-tables per concurrency -----
    def render_per_task_section(bid: str, runs: list[dict]) -> str:
        if not runs:
            return ""
        if not any(isinstance(r.get("latency_ms"), dict)
                   and r["latency_ms"].get("per_task") for r in runs):
            return ""
        out = [f"<h2>{bid} - per-task latency &amp; ops mix</h2>"]
        for r in runs:
            pt = (r.get("latency_ms") or {}).get("per_task") or {}
            if not pt:
                continue
            extra = r.get("extra") or {}
            ops_break = extra.get("ops_breakdown") or {}
            mix = extra.get("task_mix") or {}
            avg_steps = extra.get("avg_steps_per_rollout")

            header_bits = [f"c={r['concurrency']}"]
            if avg_steps is not None:
                header_bits.append(f"avg steps/rollout={avg_steps}")
            out.append(
                f"<h3 style='font-size:1rem;margin-top:1rem'>{' &middot; '.join(header_bits)}</h3>"
            )

            # per-task latency table (one row per sub-benchmark)
            out.append("<table>")
            out.append(
                "<tr><th style='text-align:left'>Sub-task</th>"
                "<th>weight</th><th>ops</th><th>count</th>"
                "<th>p50 (ms)</th><th>p95 (ms)</th><th>p99 (ms)</th>"
                "<th>mean (ms)</th><th>max (ms)</th></tr>"
            )
            for tkey in sorted(pt.keys()):
                row = pt[tkey] or {}
                w = mix.get(tkey)
                ops = ops_break.get(tkey, row.get("ops"))
                w_cell = f"{w:.2f}" if isinstance(w, (int, float)) else "-"
                ops_cell = f"{int(ops)}" if ops is not None else "-"
                out.append(
                    f"<tr><td style='text-align:left'>{tkey} "
                    f"<span style='color:#888;font-size:.85em'>"
                    f"{PRIMARY_UNITS.get(tkey, {}).get('title', '')}</span></td>"
                    f"<td>{w_cell}</td>"
                    f"<td>{ops_cell}</td>"
                    f"<td>{int(row.get('count', 0))}</td>"
                    f"{cell(row.get('p50'), '.1f')}"
                    f"{cell(row.get('p95'), '.1f')}"
                    f"{cell(row.get('p99'), '.1f')}"
                    f"{cell(row.get('mean'), '.1f')}"
                    f"{cell(row.get('max'), '.1f')}"
                    "</tr>"
                )
            out.append("</table>")
        return "\n".join(out)

    sub_sections: list[str] = []
    for bid, runs in summary["benchmarks"].items():
        # Currently B5 carries per_query and B9 carries per_task; the
        # generic checks above handle either case if a future runner
        # adopts the same shape.
        sec = render_per_query_section(bid, runs)
        if sec:
            sub_sections.append(sec)
        sec = render_per_task_section(bid, runs)
        if sec:
            sub_sections.append(sec)
    sub_html = "\n".join(sub_sections)

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
h3{{font-size:1rem;margin-top:1rem;color:#444}}
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
<tr><th>Benchmark</th><th>Concurrency</th><th>Throughput</th>
    <th>P99 (ms)</th><th>CPU avg (%)</th><th>$/1k units</th></tr>
{body_rows}
</table>
{sub_html}
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
            "b9_duration_sec": cfg.b9_duration_sec,
            "b9_concurrencies": cfg.b9_concurrencies,
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
