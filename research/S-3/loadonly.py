"""Load one file with a given heap limit; report time and retained heap."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "S-1"))
import spike

spike.start_jvm(sys.argv[2])
t = time.perf_counter()
try:
    _, ont = spike.load(Path(sys.argv[1]).resolve())
    print(
        f"{sys.argv[1]} -Xmx{sys.argv[2]}: loaded in {time.perf_counter() - t:.0f}s, "
        f"retained {spike.java_heap_used_mb():.0f} MB, {ont.getAxiomCount()} axioms",
        flush=True,
    )
except BaseException as e:  # noqa: BLE001
    print(
        f"{sys.argv[1]} -Xmx{sys.argv[2]}: FAILED after {time.perf_counter() - t:.0f}s: {type(e).__name__}", flush=True
    )
