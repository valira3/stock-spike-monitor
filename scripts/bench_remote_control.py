#!/usr/bin/env python3
"""Single-file benchmark of the underlying system as it matters for Claude
Code remote-control workloads. Stdlib only -- no project deps -- so you can
copy this file to any machine and run `python3 bench_remote_control.py` to
get a comparable scorecard.

Measures:
  - Hardware (CPU model, cores, RAM, kernel)
  - CPU single + multi-thread compute
  - Memory bandwidth (sequential write+read)
  - Disk: sequential write/read, random 4K reads, metadata ops
  - Python: cold subprocess startup, in-process import
  - JSON parse throughput
  - Subprocess fork latency
  - Network latency to common Claude-Code-relevant endpoints
  - Concurrency: thread-pool overhead

Total runtime: ~30-60s. Writes a temp working dir under /tmp/bench-*.

Output: a markdown table to stdout. Final line is a single-number "score"
(sum of MB/s + 1000/ms-ops, lower-is-worse) so two machines can be ranked
quickly. The full table is the substantive comparison.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _cpuinfo() -> dict:
    out = {"model": "unknown", "cores_logical": os.cpu_count() or 0, "cores_physical": 0}
    try:
        with open("/proc/cpuinfo") as f:
            txt = f.read()
        for line in txt.splitlines():
            if "model name" in line and out["model"] == "unknown":
                out["model"] = line.split(":", 1)[1].strip()
        physical = set()
        for block in txt.split("\n\n"):
            core_id = None
            phys_id = None
            for line in block.splitlines():
                if line.startswith("core id"):
                    core_id = line.split(":", 1)[1].strip()
                elif line.startswith("physical id"):
                    phys_id = line.split(":", 1)[1].strip()
            if core_id is not None and phys_id is not None:
                physical.add((phys_id, core_id))
        out["cores_physical"] = len(physical) or out["cores_logical"]
    except Exception:
        pass
    return out


def _meminfo() -> dict:
    out = {"total_kb": 0, "available_kb": 0}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    out["total_kb"] = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    out["available_kb"] = int(line.split()[1])
    except Exception:
        pass
    return out


def _hostinfo() -> dict:
    return {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "python": platform.python_version(),
    }


# ---------- benchmarks ----------

def bench_cpu_single() -> float:
    """SHA-256 of 100 MB of bytes. Returns MB/s."""
    data = bytes(1024 * 1024) * 100  # 100 MB of zeros (representative of hash speed)
    t = time.perf_counter()
    hashlib.sha256(data).hexdigest()
    return 100 / (time.perf_counter() - t)


def bench_cpu_multi(workers: int) -> float:
    """Same SHA-256, run on N threads concurrently. Returns aggregate MB/s.
    Note: hashlib's C impl releases the GIL, so this scales with cores."""
    work = bytes(1024 * 1024) * 25  # 25 MB per worker
    t = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda _: hashlib.sha256(work).hexdigest(), range(workers)))
    return (25 * workers) / (time.perf_counter() - t)


def bench_memory_seq(size_mb: int = 256) -> tuple[float, float]:
    """Write a `size_mb` bytearray then read it. Returns (write_MB/s, read_MB/s)."""
    n = size_mb * 1024 * 1024
    t = time.perf_counter()
    ba = bytearray(n)  # write
    dt_w = time.perf_counter() - t
    t = time.perf_counter()
    _ = bytes(ba)  # read into a new bytes object (copy)
    dt_r = time.perf_counter() - t
    return size_mb / dt_w, size_mb / dt_r


def bench_disk_seq(tmp: Path, size_mb: int = 256) -> tuple[float, float]:
    """Sequential write+read of `size_mb` to tmp/bench.bin. Returns (write_MB/s, read_MB/s)."""
    fp = tmp / "seq.bin"
    chunk = bytes(1024 * 1024)
    t = time.perf_counter()
    with open(fp, "wb") as f:
        for _ in range(size_mb):
            f.write(chunk)
        f.flush()
        os.fsync(f.fileno())
    dt_w = time.perf_counter() - t
    t = time.perf_counter()
    with open(fp, "rb") as f:
        while f.read(1024 * 1024):
            pass
    dt_r = time.perf_counter() - t
    fp.unlink()
    return size_mb / dt_w, size_mb / dt_r


def bench_disk_random(tmp: Path, total_mb: int = 64, block: int = 4096,
                      reads: int = 2000) -> float:
    """Random 4 KB reads. Returns IOPS."""
    import random as _random
    fp = tmp / "rand.bin"
    n_bytes = total_mb * 1024 * 1024
    with open(fp, "wb") as f:
        f.write(os.urandom(n_bytes))
    pos_max = n_bytes - block
    positions = [_random.randint(0, pos_max) for _ in range(reads)]
    t = time.perf_counter()
    with open(fp, "rb") as f:
        for p in positions:
            f.seek(p)
            f.read(block)
    dt = time.perf_counter() - t
    fp.unlink()
    return reads / dt


def bench_metadata_ops(tmp: Path, n: int = 2000) -> float:
    """File create + unlink throughput. Returns ops/sec (each op = create+stat+unlink)."""
    sub = tmp / "meta"
    sub.mkdir(exist_ok=True)
    t = time.perf_counter()
    for i in range(n):
        fp = sub / f"f{i}"
        fp.touch()
        fp.stat()
        fp.unlink()
    dt = time.perf_counter() - t
    sub.rmdir()
    return n / dt


def bench_python_subprocess_startup(n: int = 10) -> float:
    """Spawn python3 -c 'pass' N times. Returns avg startup-to-exit ms."""
    py = sys.executable
    t = time.perf_counter()
    for _ in range(n):
        subprocess.run([py, "-c", "pass"], check=True)
    dt = time.perf_counter() - t
    return 1000.0 * dt / n


def bench_subprocess_true(n: int = 100) -> float:
    """/bin/true (or equivalent) N times. Returns avg ms."""
    cmd = ["/bin/true"] if Path("/bin/true").exists() else ["true"]
    t = time.perf_counter()
    for _ in range(n):
        subprocess.run(cmd, check=True)
    dt = time.perf_counter() - t
    return 1000.0 * dt / n


def bench_json_parse(size_mb: int = 10) -> float:
    """Parse a `size_mb` JSON blob. Returns MB/s."""
    obj = [{"k": "v" * 20, "n": i, "f": float(i) * 1.5, "b": True} for i in range(80_000)]
    blob = json.dumps(obj)
    while len(blob) < size_mb * 1024 * 1024:
        blob = json.dumps([obj] * 4)
    blob = blob[: size_mb * 1024 * 1024]
    t = time.perf_counter()
    try:
        json.loads(blob)
    except json.JSONDecodeError:
        # If we truncated mid-token, parse the inner valid prefix instead.
        valid = json.dumps([obj] * 4)
        size_mb = len(valid) / (1024 * 1024)
        t = time.perf_counter()
        json.loads(valid)
    return size_mb / (time.perf_counter() - t)


def bench_network(urls: list[str], timeout: float = 4.0, samples: int = 3) -> dict:
    """HTTPS HEAD to each URL, return median ms."""
    out = {}
    for u in urls:
        ms_list = []
        for _ in range(samples):
            t = time.perf_counter()
            try:
                req = urllib.request.Request(u, method="HEAD")
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    r.status
                ms_list.append(1000.0 * (time.perf_counter() - t))
            except Exception:
                ms_list.append(float("nan"))
        ms_list.sort()
        out[u] = ms_list[len(ms_list) // 2]
    return out


def bench_thread_pool_overhead(workers: int = 8, tasks: int = 1000) -> float:
    """Submit N noop tasks to ThreadPoolExecutor(workers). Returns tasks/sec."""
    def _noop(_):
        return 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        t = time.perf_counter()
        list(pool.map(_noop, range(tasks)))
        dt = time.perf_counter() - t
    return tasks / dt


# ---------- driver ----------

def main() -> int:
    print("# Remote-control system benchmark")
    print()
    host = _hostinfo()
    cpu = _cpuinfo()
    mem = _meminfo()
    print("## Host")
    print(f"- host:     `{host['host']}`")
    print(f"- platform: `{host['platform']}`")
    print(f"- kernel:   `{host['kernel']}`")
    print(f"- python:   `{host['python']}`")
    print(f"- CPU:      `{cpu['model']}`")
    print(f"- cores:    logical={cpu['cores_logical']}  physical={cpu['cores_physical']}")
    print(f"- RAM:      total={_human_bytes(mem['total_kb']*1024)}  "
          f"available={_human_bytes(mem['available_kb']*1024)}")
    print()

    with tempfile.TemporaryDirectory(prefix="bench-rc-") as tmp_s:
        tmp = Path(tmp_s)

        rows = []  # (name, value, unit)
        def t(name, fn, unit):
            v = fn()
            rows.append((name, v, unit))

        # CPU
        t("cpu.sha256_singlethread",  bench_cpu_single,              "MB/s")
        t(f"cpu.sha256_{cpu['cores_logical']}thread",
                                       lambda: bench_cpu_multi(cpu['cores_logical']),
                                                                     "MB/s")
        # Memory
        mw, mr = bench_memory_seq(256)
        rows.append(("memory.bytearray_alloc",        mw, "MB/s"))
        rows.append(("memory.bytearray_copy_to_bytes",mr, "MB/s"))
        # Disk
        dw, dr = bench_disk_seq(tmp, 256)
        rows.append(("disk.seq_write",     dw, "MB/s"))
        rows.append(("disk.seq_read",      dr, "MB/s"))
        t("disk.random_4k_read",     lambda: bench_disk_random(tmp), "IOPS")
        t("disk.metadata_create_stat_unlink",
                                      lambda: bench_metadata_ops(tmp), "ops/s")
        # Python + subprocess
        t("python.cold_startup",      bench_python_subprocess_startup, "ms/spawn")
        t("subprocess.true",          bench_subprocess_true,         "ms/spawn")
        # JSON
        t("json.parse",               bench_json_parse,              "MB/s")
        # Concurrency
        t("threading.pool_8_noop",    bench_thread_pool_overhead,    "tasks/s")
        # Network
        urls = [
            "https://api.anthropic.com",
            "https://api.github.com",
            "https://raw.githubusercontent.com",
            "https://pypi.org",
        ]
        net = bench_network(urls, samples=3)
        for u, ms in net.items():
            rows.append((f"net.{u.split('//')[1]}", ms, "ms (median, HEAD)"))

        # Table
        print("## Results")
        print()
        print("| metric                                  | value         | unit |")
        print("|-----------------------------------------|---------------|------|")
        for name, val, unit in rows:
            if isinstance(val, float):
                if val != val:  # NaN
                    s = "TIMEOUT"
                elif val >= 1000:
                    s = f"{val:,.0f}"
                elif val >= 10:
                    s = f"{val:,.1f}"
                else:
                    s = f"{val:.3f}"
            else:
                s = str(val)
            print(f"| {name:<39} | {s:>13} | {unit} |")
        print()

        # Single-number score: sum of throughput-ish numbers + inverse latency
        # (lower is worse; useful only for diffing same metrics across machines).
        score = 0.0
        for name, val, unit in rows:
            if not isinstance(val, float) or val != val:
                continue
            if unit in ("MB/s", "IOPS", "ops/s", "tasks/s"):
                score += val
            elif unit.startswith("ms") and val > 0:
                # latency: contribute inverse so faster = bigger score
                score += 1000.0 / val
        print(f"## Aggregate score (higher = better, sum of throughput + inverse-latency)")
        print(f"`{score:,.0f}`")
        print()
        print("> Save the full table above. To compare to another machine, run this "
              "same script there and diff the rows. The aggregate score is a quick "
              "single-number sanity check; the per-row numbers are the real comparison.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
