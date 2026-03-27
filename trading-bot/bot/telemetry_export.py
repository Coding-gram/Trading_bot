"""
Prometheus-Compatible Metrics Export (Suggestion #12)
Exposes telemetry counters in Prometheus text exposition format.
Can be served via a simple HTTP endpoint or written to a file for node_exporter.
"""
import logging
import time
from bot import telemetry

logger = logging.getLogger(__name__)


def generate_prometheus_metrics() -> str:
    """Generate Prometheus text exposition format from current telemetry snapshot.

    Returns:
        Multi-line string in Prometheus text format, ready to serve via HTTP
        or write to a .prom file for node_exporter's textfile collector.
    """
    snap = telemetry.snapshot()
    counters = snap.get("counters", {})
    derived = snap.get("derived", {})

    lines = [
        f"# HELP trading_bot_info Bot information",
        f"# TYPE trading_bot_info gauge",
        f'trading_bot_info{{version="1.0"}} 1',
        f"",
        f"# Generated at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        f"",
    ]

    # Export all counters
    for metric, value in sorted(counters.items()):
        safe_metric = metric.replace(".", "_").replace("-", "_")
        lines.append(f"# TYPE trading_bot_{safe_metric} counter")
        lines.append(f"trading_bot_{safe_metric} {value}")

    lines.append("")

    # Export derived metrics as gauges
    for metric, value in sorted(derived.items()):
        safe_metric = metric.replace(".", "_").replace("-", "_")
        lines.append(f"# TYPE trading_bot_{safe_metric} gauge")
        lines.append(f"trading_bot_{safe_metric} {value}")

    lines.append("")
    return "\n".join(lines)


def write_metrics_file(filepath: str = "/tmp/trading_bot_metrics.prom") -> None:
    """Write metrics to a .prom file for Prometheus node_exporter textfile collector."""
    try:
        content = generate_prometheus_metrics()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        logger.warning("Failed to write Prometheus metrics file: %s", e)
