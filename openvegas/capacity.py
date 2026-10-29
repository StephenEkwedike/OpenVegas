"""Bounded, process-local capacity observations. No network, payloads or automation."""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

WINDOW_SECONDS = 300
STALE_SECONDS = 60
MIN_REQUESTS = 20
MIN_SECONDS = 30
MAX_REQUESTS = 5000
GUIDANCE = [
    "First reduce unnecessary polling; pause hidden tabs, back off failures and coalesce duplicate reads.",
    (
        "Reuse bounded in-process caches for safe shared aggregates; retain authorization checks and short TTLs. "
        "Keep wallet, auth, admin and entitlement-protected premium responses no-store."
    ),
    (
        "Then consider Cloudflare CDN/static caching only for explicitly public, immutable, versioned assets. "
        "Never cache wallet, auth, admin, personalized or entitlement-protected premium responses; no blanket cache-everything rule."
    ),
    "Measure slow queries, indexes and DB/pool wait time before tuning bounded pools; account for every replica's connections.",
    (
        "Only after sustained, measured saturation compare a small resource increase with another replica. "
        "Validate fleet/provider metrics and cost first; these local samples do not establish safe users, RPS or sizing."
    ),
]


def _read(path: Path) -> str:
    try:
        with path.open() as stream:
            value = stream.read(65537)
        return value.strip() if len(value) <= 65536 else ""
    except (OSError, ValueError):
        return ""


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def read_resources() -> dict:
    """Small procfs/cgroup-v2 reads, on summary cache misses only; unsupported = None."""
    result = {
        "process_cpu_seconds": time.process_time(),
        "process_rss_bytes": None,
        "host_load_1m": None,
        "host_cpu_count": os.cpu_count(),
        "container_memory_bytes": None,
        "container_memory_limit_bytes": None,
        "container_memory_ratio": None,
        "container_cpu_seconds": None,
        "container_cpu_limit_cores": None,
        "container_cpu_scope": None,
        "cgroup_status": "unknown",
    }
    try:
        result["host_load_1m"] = os.getloadavg()[0]
    except (AttributeError, OSError):
        pass
    try:
        result["process_rss_bytes"] = int(_read(Path("/proc/self/statm")).split()[1]) * os.sysconf(
            "SC_PAGE_SIZE"
        )
    except (IndexError, ValueError, OSError, AttributeError):
        pass  # Do not substitute peak RSS for current memory on unsupported platforms.
    root = Path("/sys/fs/cgroup")
    membership = next(
        (s[3:] for s in _read(Path("/proc/self/cgroup")).splitlines() if s.startswith("0::")), None
    )
    if membership is None or ".." in membership.split("/"):
        return result
    # Only use the known mount, accounting for namespace-relative membership.
    mount_root = None
    for line in _read(Path("/proc/self/mountinfo")).splitlines():
        fields = line.split()
        if len(fields) > 6 and fields[4] == str(root) and " - cgroup2 " in line:
            mount_root = fields[3]
            break
    if mount_root is None:
        return result
    try:
        relative = Path(membership).relative_to(mount_root)
    except ValueError:
        return result
    group = root / relative
    groups = [group, *group.parents]
    groups = [p for p in groups if p == root or root in p.parents]
    if len(groups) > 16 or not _read(group / "cgroup.type"):
        return result
    result["cgroup_status"] = "v2-visible-hierarchy"
    for current in groups:
        raw_usage = _read(current / "memory.current")
        usage = _positive(raw_usage)
        if raw_usage == "0":
            usage = 0
        limit = _positive(_read(current / "memory.max"))
        ratio = usage / limit if usage is not None and limit else None
        if current == group:
            result["container_memory_bytes"] = usage
        if ratio is not None and (
            result["container_memory_ratio"] is None or ratio > result["container_memory_ratio"]
        ):
            result.update(
                container_memory_bytes=usage,
                container_memory_limit_bytes=limit,
                container_memory_ratio=ratio,
            )
        quota = _read(current / "cpu.max").split()
        if len(quota) != 2 or not _positive(quota[0]) or not _positive(quota[1]):
            continue
        cores = float(quota[0]) / float(quota[1])
        previous = result["container_cpu_limit_cores"]
        if previous is not None and cores >= previous:
            continue
        stats = dict(
            line.split()
            for line in _read(current / "cpu.stat").splitlines()
            if len(line.split()) == 2
        )
        used = _positive(stats.get("usage_usec"))
        if stats.get("usage_usec") == "0":
            used = 0
        result.update(
            container_cpu_limit_cores=cores,
            container_cpu_seconds=used / 1_000_000 if used is not None else None,
            container_cpu_scope=str(current),
        )
    return result


class CapacityMonitor:
    def __init__(self, *, clock=time.monotonic, resources=read_resources):
        self.clock, self.resources = clock, resources
        self.lock = Lock()
        self.rows = deque(maxlen=MAX_REQUESTS)
        self.started_at = None
        self.inflight_limit = None
        self.inflight = 0
        self.previous = None
        self.evicted_at = None

    def enable(self, inflight_limit=None):
        if inflight_limit is not None and (type(inflight_limit) is not int or inflight_limit <= 0):
            raise ValueError("inflight_limit must be a verified positive per-process limit")
        with self.lock:
            if self.started_at is None:
                self.started_at = self.clock()
            self.inflight_limit = inflight_limit

    def request_started(self):
        with self.lock:
            self.inflight += 1
        return self.clock()

    def request_finished(
        self, started, status_code, header_ms=None, *, aborted=False, failed=False
    ):
        now = self.clock()
        with self.lock:
            self.inflight = max(0, self.inflight - 1)
            duration = max(0, (now - started) * 1000)
            latency = duration if header_ms is None else max(0, header_ms)
            if not math.isfinite(latency):
                return
            if len(self.rows) == MAX_REQUESTS:
                self.evicted_at = self.rows[0][0]
            self.rows.append((now, latency, int(status_code), bool(aborted), bool(failed)))

    def snapshot(self):
        now = self.clock()
        with self.lock:
            while self.rows and self.rows[0][0] < now - WINDOW_SECONDS:
                self.rows.popleft()
            rows = list(self.rows)
            inflight, bound, started = self.inflight, self.inflight_limit, self.started_at
            truncated = self.evicted_at is not None and self.evicted_at >= now - WINDOW_SECONDS
        resources = self.resources()
        previous = self.previous
        interval = now - previous[0] if previous else None
        self.previous = (now, resources.copy())
        process_cpu = container_cpu = None
        if previous and interval >= 1:
            old = previous[1]
            delta = resources["process_cpu_seconds"] - old["process_cpu_seconds"]
            if delta >= 0:
                process_cpu = delta / interval
            quota = resources["container_cpu_limit_cores"]
            if (
                quota
                and quota == old["container_cpu_limit_cores"]
                and resources["container_cpu_scope"] == old["container_cpu_scope"]
            ):
                a, b = resources["container_cpu_seconds"], old["container_cpu_seconds"]
                if a is not None and b is not None and a >= b:
                    container_cpu = (a - b) / interval / quota
        resources.pop("container_cpu_scope", None)  # No host filesystem identity in responses.
        resources.pop("process_cpu_seconds", None)
        resources.pop("container_cpu_seconds", None)
        resources.update(
            process_cpu_cores=process_cpu,
            container_cpu_ratio=container_cpu,
            cpu_interval_seconds=interval,
        )
        elapsed = max(0, now - started) if started is not None else 0
        age = now - rows[-1][0] if rows else None
        state = (
            "hook_missing"
            if started is None
            else "no_samples"
            if not rows
            else "stale"
            if age > STALE_SECONDS
            else "insufficient"
            if len(rows) < MIN_REQUESTS or elapsed < MIN_SECONDS
            else "truncated"
            if truncated
            else "available"
        )
        latencies = sorted(r[1] for r in rows)
        p95 = latencies[math.ceil(len(rows) * 0.95) - 1] if rows else None
        failures = sum(r[2] >= 500 for r in rows)
        window = {
            "status": state,
            "seconds": WINDOW_SECONDS,
            "retained_count": len(rows),
            "retention_limit": MAX_REQUESTS,
            "truncated": truncated,
            "coverage_seconds": min(elapsed, WINDOW_SECONDS),
            "last_event_age_seconds": age,
            "latency_ms_p95": p95,
            "error_rate": failures / len(rows) if rows else None,
            "errors_5xx": failures,
            "aborted_count": sum(r[3] for r in rows),
            "application_error_count": sum(r[4] for r in rows),
            "application_error_rate": sum(r[4] for r in rows) / len(rows) if rows else None,
            "latency_basis": "time to response headers; failures before headers use elapsed time; finished/aborted HTTP only",
        }
        warnings = []

        def warn(metric, value, approaching, critical):
            if value is not None and value >= approaching:
                warnings.append(
                    {
                        "metric": metric,
                        "severity": "critical" if value >= critical else "warning",
                        "observed": round(value, 4),
                        "threshold": critical if value >= critical else approaching,
                    }
                )

        warn("container_memory_ratio", resources["container_memory_ratio"], 0.8, 0.95)
        if interval is not None and 1 <= interval <= STALE_SECONDS:
            warn("container_cpu_ratio", container_cpu, 0.8, 0.95)
        if state == "available":
            warn("http_latency_ms_p95", p95, 1500, 2000)
            warn("http_5xx_rate", window["error_rate"], 0.02, 0.05)
            warn("http_application_error_rate", window["application_error_rate"], 0.02, 0.05)
        warn(
            "inflight_ratio", inflight / bound if bound and started is not None else None, 0.8, 0.95
        )
        unknowns = [
            "Fleet utilization and safe users/RPS are unknown; each worker/replica has separate samples.",
            "DB/pool waits, network and provider limits are not measured.",
            "Host load is not CPU utilization or a container quota. Visible cgroup ancestors can include sibling processes; hidden ancestors/cpuset limits are unknown.",
        ]
        if state != "available":
            unknowns.append(
                f"HTTP window is {state}; no window-based latency/error capacity verdict."
            )
        if bound is None:
            unknowns.append(
                "No verified per-process inflight limit; request counts do not imply headroom."
            )
        if resources["process_rss_bytes"] is None:
            unknowns.append(
                "Current process RSS unavailable on this platform; not replaced with peak RSS."
            )
        if resources["container_memory_ratio"] is None or container_cpu is None:
            unknowns.append(
                "Container utilization is partly unknown: missing/unlimited cgroup-v2 limits or no CPU baseline."
            )
        if interval is None or interval > STALE_SECONDS:
            unknowns.append(
                "CPU needs two observations 1-60 seconds apart for a current capacity warning."
            )
        return {
            "observed_at": datetime.now(UTC).isoformat(),
            "scope": "process-local; visible cgroup hierarchy",
            "status": "attention" if warnings else "no_observed_threshold_breach_not_health",
            "resources": resources,
            "http_window": window,
            "inflight": inflight if started is not None else None,
            "inflight_limit": bound,
            "warnings": warnings,
            "unknowns": unknowns,
            "thresholds": {
                "resource_warning_ratio": 0.8,
                "resource_critical_ratio": 0.95,
                "latency_warning_ms": 1500,
                "latency_critical_ms": 2000,
                "error_warning_rate": 0.02,
                "error_critical_rate": 0.05,
                "min_requests": MIN_REQUESTS,
                "min_observation_seconds": MIN_SECONDS,
                "stale_event_seconds": STALE_SECONDS,
            },
            "note": "Warning thresholds are monitoring policy, not tested service capacity or an SLA. "
            "Resource samples are on demand, not a background history; cached for up to 15s. "
            "Cgroup memory includes reclaimable cache, not an OOM prediction. Exceptions before response "
            "headers infer 500; post-header application errors are counted separately. No automatic changes.",
        }


monitor = CapacityMonitor()


class CapacityMiddleware:
    """Pure ASGI wrapper: counts through streaming completion and always releases on cancellation."""

    def __init__(self, app, *, capacity_monitor=None, inflight_limit=None):
        self.app = app
        self.monitor = capacity_monitor if capacity_monitor is not None else monitor
        self.monitor.enable(inflight_limit)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = self.monitor.request_started()
        status, header_ms, aborted, failed = 500, None, False, False

        async def observe(message):
            nonlocal status, header_ms
            if message["type"] == "http.response.start":
                status = message["status"]
                header_ms = (self.monitor.clock() - started) * 1000
            await send(message)

        try:
            await self.app(scope, receive, observe)
        except asyncio.CancelledError:
            aborted = True
            if header_ms is None:
                status = 499
            raise
        except Exception:
            # A body failure after 200 headers is not an HTTP 500 response.
            failed = True
            raise
        finally:
            self.monitor.request_finished(
                started, status, header_ms, aborted=aborted, failed=failed
            )
