"""Run ROBOT reasoning on one input under time and heap limits, and classify how it ended.

python run.py INPUT --reasoner {elk,hermit,reuse} --heap 1g --timeout 60
              [--no-exit-on-oom] [--jvm-arg OPT]... [--log NAME]

`reuse` is ReasonReuse.java: HermiT with the INDIVIDUAL_REUSE strategy (compiled once by fetch.sh).
Prints one tab-separated row: input, reasoner, heap, timeout, kind, exit status, seconds, peak RSS, leftovers, and the
line the kind was detected from. The full output goes to data/logs/.
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent
ROBOT_JAR = HERE / "lib" / "robot.jar"
CLASSES = HERE / "lib" / "classes"
LOGS = HERE / "data" / "logs"
GRACE_S = 5

# What browl would do with ROBOT's combined output, in order. The first match wins, after the time limit and the JVM's
# own out-of-memory exit status.
PATTERNS = [
    ("memory limit", re.compile(r"OutOfMemoryError")),
    ("stack overflow", re.compile(r"StackOverflowError")),
    ("inconsistent", re.compile(r"ERROR org\.obolibrary\.robot\.ReasonerHelper - The ontology is inconsistent")),
    (
        "unsatisfiable classes",
        re.compile(r"ERROR org\.obolibrary\.robot\.ReasonerHelper - There are \d+ unsatisfiable"),
    ),
    ("parse error", re.compile(r"INVALID ONTOLOGY FILE ERROR")),
]


def command(args, marker):
    java = ["java", f"-Xmx{args.heap}", f"-Ds2.run={marker}"]
    if args.exit_on_oom:
        java.append("-XX:+ExitOnOutOfMemoryError")
    java += args.jvm_arg
    out = HERE / "data" / "out.ttl"
    if args.reasoner == "reuse":
        return [*java, "-cp", f"{ROBOT_JAR}:{CLASSES}", "ReasonReuse", str(args.input), str(out)]
    return [
        *java,
        "-jar",
        str(ROBOT_JAR),
        "-vv",
        "reason",
        "--reasoner",
        args.reasoner,
        "-i",
        str(args.input),
        "-o",
        str(out),
    ]


def leftovers(marker):
    found = subprocess.run(
        ["pgrep", "-f", f"s2.run={marker}"], check=False, capture_output=True, text=True
    ).stdout.split()
    return found


def classify(status, timed_out, output):
    if timed_out:
        return "time limit", ""
    if status == 0:
        return "ok", ""
    if status == 3 and "Terminating due to java.lang.OutOfMemoryError" in output:
        return "memory limit", "exit status 3"
    for kind, pattern in PATTERNS:
        match = pattern.search(output)
        if match:
            line = output[output.rfind("\n", 0, match.start()) + 1 : output.find("\n", match.end())]
            return kind, line.strip()[:160]
    return "other failure", output.strip().splitlines()[-1][:160] if output.strip() else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--reasoner", choices=["elk", "hermit", "reuse"], default="elk")
    parser.add_argument("--heap", default="1g")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--no-exit-on-oom", dest="exit_on_oom", action="store_false")
    parser.add_argument("--jvm-arg", action="append", default=[], help="an extra JVM option; repeatable")
    parser.add_argument("--log")
    args = parser.parse_args()

    marker = uuid.uuid4().hex[:12]
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{args.log or f'{args.input.stem}-{args.reasoner}-{args.heap}'}.log"
    start = time.monotonic()
    with log.open("w") as f:
        proc = subprocess.Popen(command(args, marker), stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        status = rusage = None
        deadline = start + args.timeout
        # os.wait4 gives this child's own peak RSS; poll it so the timeout can fire.
        while status is None:
            pid, raw, rusage = os.wait4(proc.pid, os.WNOHANG)
            if pid:
                status = os.waitstatus_to_exitcode(raw)
                break
            if time.monotonic() >= deadline and not timed_out:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                deadline = time.monotonic() + GRACE_S
            elif timed_out and time.monotonic() >= deadline:
                os.killpg(proc.pid, signal.SIGKILL)
                deadline = float("inf")
            time.sleep(0.05)
        proc.returncode = status
    elapsed = time.monotonic() - start
    output = log.read_text(errors="replace")
    kind, evidence = classify(status, timed_out, output)
    # ru_maxrss is in bytes on macOS, kilobytes on Linux.
    rss_mib = rusage.ru_maxrss / (1 if sys.platform == "darwin" else 1 / 1024) / 2**20
    left = leftovers(marker)
    row = [args.input.name, args.reasoner, args.heap, f"{args.timeout:g}", kind, str(status), f"{elapsed:.1f}"]
    row += [f"{rss_mib:.0f} MiB", ",".join(left) or "none", evidence]
    print("\t".join(row), flush=True)


if __name__ == "__main__":
    main()
