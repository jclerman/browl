"""Summarize run.sh's outputs: one row per setting, as a Markdown table, and the NMT breakdown at 16 clients.

Container memory is docker stats' figure (the cgroup's memory less inactive file cache), in MiB: the median while idle
and during page load from 1 and 16 clients, and the max over the whole run (bulk queries included). anon and file are
the cgroup's, each at its own maximum over the run.

Usage (from the repo root): python3 research/S-6/summary.py <label>...
"""

import json
import re
import statistics
import sys
from pathlib import Path

DATA = Path("research/S-6/data")
NMT = ["Java Heap", "Class", "Thread", "Code", "GC", "Other", "Symbol", "Native Memory Tracking", "Metaspace"]


def mib(s: str) -> float:
    n, u = re.match(r"([\d.]+)\s*(\w+)", s.strip()).groups()  # type: ignore[union-attr]
    return float(n) * {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[u]


def memory(label: str) -> dict[str, dict[str, float]]:
    phases: dict[str, list[tuple[float, float, float]]] = {}
    for line in (DATA / f"{label}.mem.tsv").read_text().splitlines():
        f = line.split("\t")
        if len(f) >= 7 and f[2].strip() and f[3]:
            phases.setdefault(f[0], []).append((mib(f[2]), int(f[3]) / 2**20, int(f[4]) / 2**20))
    out = {}
    for phase, v in phases.items():
        out[phase] = {
            "median": statistics.median(x[0] for x in v),
            "max": max(x[0] for x in v),
            "anon": max(x[1] for x in v),
            "file": max(x[2] for x in v),
        }
    return out


def run(label: str) -> dict:
    r: dict = {}
    for line in (DATA / f"{label}.run.jsonl").read_text().splitlines():
        if line.startswith("{"):
            d = json.loads(line)
            if "concurrency" in d:
                r[f"c{d['concurrency']}"] = d
            else:
                r.update(d)
    return r


def nmt(label: str, phase: str) -> dict[str, float]:
    p = DATA / f"{label}.nmt-{phase}.txt"
    text = p.read_text() if p.exists() else ""
    out = {}
    if m := re.search(r"Total: reserved=\d+KB, committed=(\d+)KB", text):
        out["Total"] = int(m[1]) / 1024
    for name in NMT:
        if m := re.search(rf"-\s+{name} \(reserved=\d+KB, committed=(\d+)KB", text):
            out[name] = int(m[1]) / 1024
    if m := re.search(r"\(threads #(\d+)\)", text):
        out["threads"] = int(m[1])
    return out


def system_gcs(label: str) -> int:
    p = DATA / f"{label}.gc.log"
    return p.read_text().count("System.gc()") if p.exists() else -1


def main() -> None:
    print(
        "| Setting | Median MiB: idle / 1 / 16 clients | Max MiB | Max anon / file MiB | Pages/s, 1 / 16 "
        "| p50 / p95 at 16 | Bulk: dictionary, edges | System.gc() | OOM, restarts |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    rows = []
    for label in sys.argv[1:]:
        m, r = memory(label), run(label)
        med = " / ".join(f"{m[p]['median']:,.0f}" if p in m else "—" for p in ("idle", "load-1", "load-16"))
        top = {k: max(x[k] for x in m.values()) for k in ("max", "anon", "file")}
        c1, c16 = r.get("c1", {}), r.get("c16", {})
        bulk = r.get("bulk", [])
        ok = r.get("bulk_rc") == 0 and len(bulk) == 2
        b = ", ".join(f"{x['s']:.0f} s" for x in bulk) if ok else "**fail**"
        errors = c1.get("errors", 0) + c16.get("errors", 0)
        print(
            f"| {label} | {med} | {top['max']:,.0f} | {top['anon']:,.0f} / {top['file']:,.0f} "
            f"| {c1.get('pages_per_s', 0):.0f} / {c16.get('pages_per_s', 0):.0f}"
            f"{f' ({errors} errors)' if errors else ''} | {c16.get('page_ms_p50', 0)} / {c16.get('page_ms_p95', 0)} ms "
            f"| {b} | {system_gcs(label)} | {'yes' if r.get('oom_killed') else 'no'}, {r.get('restarts')} |"
        )
        rows.append((label, nmt(label, "load-16"), nmt(label, "bulk")))
    print()
    print("NMT committed MiB at 16 clients (and after the bulk queries):")
    print()
    cols = ["Total", *NMT[:-1], "threads"]
    print("| Setting | " + " | ".join(cols) + " |")
    print("| --- |" + " --- |" * len(cols))
    for label, a, b in rows:
        print(f"| {label} | " + " | ".join(f"{a.get(c, 0):,.0f} ({b.get(c, 0):,.0f})" for c in cols) + " |")


if __name__ == "__main__":
    main()
