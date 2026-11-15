"""S-1 spike: OWLAPI (from robot.jar) via JPype — loading, Manchester rendering with link tokens, usage, timing.

Run from research/S-1 with the spike venv:  .venv/bin/python spike.py <command>
"""

import html
import json
import random
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jpype
import jpype.imports

HERE = Path(__file__).parent
ROBOT_JAR = HERE / "lib" / "robot.jar"
GEN = HERE / "data" / "generated"


def start_jvm(heap: str = "8g") -> None:
    jpype.startJVM(f"-Xmx{heap}", classpath=[str(ROBOT_JAR)], convertStrings=True)


def java_heap_used_mb() -> float:
    from java.lang import Runtime, System

    for _ in range(3):
        System.gc()
    rt = Runtime.getRuntime()
    return (rt.totalMemory() - rt.freeMemory()) / 2**20


def load(path: Path, concurrent: bool = True, silent_imports: bool = True):
    from java.io import File
    from org.semanticweb.owlapi.apibinding import OWLManager
    from org.semanticweb.owlapi.io import FileDocumentSource
    from org.semanticweb.owlapi.model import MissingImportHandlingStrategy, OWLOntologyLoaderConfiguration

    manager = OWLManager.createConcurrentOWLOntologyManager() if concurrent else OWLManager.createOWLOntologyManager()
    config = OWLOntologyLoaderConfiguration()
    if silent_imports:
        config = config.setMissingImportHandlingStrategy(MissingImportHandlingStrategy.SILENT)
    ont = manager.loadOntologyFromOntologyDocument(FileDocumentSource(File(str(path))), config)
    return manager, ont


# --- Link tokens ---------------------------------------------------------------------------------------------------
# Approach A (design §2.8): a Python ShortFormProvider returns a private-use token per entity.
TOKEN = re.compile("([0-9]+)")


@jpype.JImplements("org.semanticweb.owlapi.util.ShortFormProvider", deferred=True)
class TokenProvider:
    """Returns a placeholder token for each entity; Python swaps tokens for links after rendering."""

    def __init__(self) -> None:
        self.index: dict[str, int] = {}
        self.iris: list[str] = []

    @jpype.JOverride
    def getShortForm(self, entity):  # noqa: N802
        iri = str(entity.getIRI())
        i = self.index.get(iri)
        if i is None:
            i = self.index[iri] = len(self.iris)
            self.iris.append(iri)
        return f"{i}"

    @jpype.JOverride
    def dispose(self) -> None:
        pass


# Approach B: no Python callback. OWLAPI's DefaultPrefixManager with no prefixes renders each entity as <full IRI>.
IRI_TOKEN = re.compile(r"&lt;([^\s&]+)&gt;")


def make_renderer(provider):
    from org.semanticweb.owlapi.manchestersyntax.renderer import ManchesterOWLSyntaxOWLObjectRendererImpl

    r = ManchesterOWLSyntaxOWLObjectRendererImpl()
    r.setShortFormProvider(provider)
    return r


def iri_provider():
    from org.semanticweb.owlapi.util import DefaultPrefixManager

    pm = DefaultPrefixManager()
    pm.clear()  # drop default prefixes (owl:, rdf:, xsd:...) so every entity renders as <IRI>
    return pm


KEYWORDS = re.compile(
    r"\b(some|only|value|min|max|exactly|and|or|not|that|Self|inverse|SubClassOf|EquivalentTo|DisjointWith|"
    r"Domain|Range|Characteristics|SubPropertyOf|InverseOf|SubPropertyChain|o)\b"
)


def to_html_a(text: str, tp: TokenProvider, labels: dict[str, str]) -> str:
    out = html.escape(text, quote=False)
    out = KEYWORDS.sub(r'<span class="kw">\1</span>', out)

    def link(m: re.Match[str]) -> str:
        iri = tp.iris[int(m.group(1))]
        return f'<a href="{html.escape(iri)}">{html.escape(labels.get(iri, iri.rsplit("/", 1)[-1]))}</a>'

    return TOKEN.sub(link, out)


def to_html_b(text: str, labels: dict[str, str]) -> str:
    out = html.escape(text, quote=False)
    out = KEYWORDS.sub(r'<span class="kw">\1</span>', out)

    def link(m: re.Match[str]) -> str:
        iri = html.unescape(m.group(1))
        return f'<a href="{html.escape(iri)}">{html.escape(labels.get(iri, iri.rsplit("/", 1)[-1]))}</a>'

    return IRI_TOKEN.sub(link, out)


# --- Ontology access ------------------------------------------------------------------------------------------------
def labels_of(ont) -> dict[str, str]:
    from org.semanticweb.owlapi.model import AxiomType

    rdfs_label = "http://www.w3.org/2000/01/rdf-schema#label"
    out: dict[str, str] = {}
    for ax in ont.getAxioms(AxiomType.ANNOTATION_ASSERTION):
        if str(ax.getProperty().getIRI()) == rdfs_label and ax.getValue().isLiteral():
            out.setdefault(str(ax.getSubject()), str(ax.getValue().getLiteral()))
    return out


def page_axioms(ont, cls) -> list:
    """Axioms on a class page: annotation assertions plus the class's logical axioms."""
    from org.semanticweb.owlapi.model.parameters import Imports

    axs = list(ont.getAnnotationAssertionAxioms(cls.getIRI()))
    axs += list(ont.getAxioms(cls, Imports.INCLUDED))
    return axs


def usage_axioms(ont, entity, limit: int = 50) -> tuple[int, list]:
    from org.semanticweb.owlapi.model.parameters import Imports

    refs = ont.getReferencingAxioms(entity, Imports.INCLUDED)
    first = []
    it = refs.iterator()
    while it.hasNext() and len(first) < limit:
        first.append(it.next())
    return refs.size(), first


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p / 100 * (len(xs) - 1)))]


def summary(ms: list[float]) -> dict[str, float]:
    return {
        "n": len(ms),
        "p50": round(pct(ms, 50), 2),
        "p95": round(pct(ms, 95), 2),
        "max": round(max(ms), 2),
        "mean": round(statistics.mean(ms), 2),
    }


# --- Commands -------------------------------------------------------------------------------------------------------
def cmd_coverage() -> None:
    """Load family in three formats (and the original with a missing import); render every axiom both ways."""
    start_jvm("2g")
    for name in ["family.ofn", "family.ttl", "family.owl"]:
        t = time.perf_counter()
        _, ont = load(GEN / name)
        print(
            f"{name}: {ont.getAxiomCount()} axioms, {ont.getLogicalAxiomCount()} logical, "
            f"loaded in {(time.perf_counter() - t) * 1000:.0f} ms"
        )
    try:
        load(HERE / "data" / "family.ofn", silent_imports=False)
        print("missing import, default config: loaded (unexpected)")
    except Exception as e:  # noqa: BLE001
        print(f"missing import, default config: {type(e).__name__}")
    _, ont = load(HERE / "data" / "family.ofn")
    print(f"missing import, SILENT: loaded, {ont.getAxiomCount()} axioms")

    tp = TokenProvider()
    ra, rb = make_renderer(tp), make_renderer(iri_provider())
    kinds: dict[str, int] = {}
    mismatches = 0
    out = []
    for ax in ont.getAxioms():
        kind = str(ax.getAxiomType())
        kinds[kind] = kinds.get(kind, 0) + 1
        a, b = to_html_a(ra.render(ax), tp, {}), to_html_b(rb.render(ax), {})
        if a != b:
            mismatches += 1
            out.append(f"MISMATCH {kind}\n  A: {a}\n  B: {b}")
        else:
            out.append(f"{kind}: {a}")
    (GEN / "family-rendered.txt").write_text("\n".join(out) + "\n")
    print(f"rendered {sum(kinds.values())} axioms of {len(kinds)} kinds; A/B mismatches: {mismatches}")
    print("kinds:", ", ".join(sorted(kinds)))

    # Escaping: a label with HTML special characters must come out escaped.
    from org.semanticweb.owlapi.model import IRI

    df = ont.getOWLOntologyManager().getOWLDataFactory()
    c = df.getOWLClass(IRI.create("http://example.com/owl/families/Person"))
    ax = list(ont.getSubClassAxiomsForSubClass(c))[:1] or list(ont.getAxioms(c))[:1]
    s = to_html_a(ra.render(ax[0]), tp, {"http://example.com/owl/families/Person": "<b>Person & co</b>"})
    print("escaping:", s)


def cmd_timing(path: str, sample: int, threads: str) -> None:
    start_jvm("12g")
    from java.lang import System

    base = java_heap_used_mb()
    t = time.perf_counter()
    _, ont = load(HERE / path)
    load_s = time.perf_counter() - t
    heap = java_heap_used_mb() - base
    classes = list(ont.getClassesInSignature())
    t = time.perf_counter()
    labels = labels_of(ont)
    labels_s = time.perf_counter() - t
    print(
        json.dumps(
            {
                "file": path,
                "load_s": round(load_s, 1),
                "heap_mb_after_load": round(heap),
                "axioms": ont.getAxiomCount(),
                "logical_axioms": ont.getLogicalAxiomCount(),
                "classes": len(classes),
                "labels_s": round(labels_s, 1),
            }
        )
    )

    random.seed(1)
    picks = random.sample(classes, min(sample, len(classes)))
    tp = TokenProvider()

    def render_page_a(cls) -> int:
        r = make_renderer(tp)  # renderers keep state; one per call
        return sum(len(to_html_a(r.render(ax), tp, labels)) for ax in page_axioms(ont, cls))

    def render_page_b(cls) -> int:
        r = make_renderer(iri_provider())
        return sum(len(to_html_b(r.render(ax), labels)) for ax in page_axioms(ont, cls))

    def render_usage_b(cls) -> int:
        r = make_renderer(iri_provider())
        _, first = usage_axioms(ont, cls)
        return sum(len(to_html_b(r.render(ax), labels)) for ax in first)

    for fn in (render_page_a, render_page_b):  # warm up the JIT
        for c in picks[:200]:
            fn(c)

    results = {}
    for name, fn in [
        ("page_A_python_provider", render_page_a),
        ("page_B_iri_provider", render_page_b),
        ("usage_first50_B", render_usage_b),
    ]:
        ms = []
        for c in picks:
            t = time.perf_counter()
            fn(c)
            ms.append((time.perf_counter() - t) * 1000)
        results[name + "_serial"] = summary(ms)
        baseline = [fn(c) for c in picks[:300]]
        for n in [int(x) for x in threads.split(",")]:
            lat: list[float] = []

            def timed(c, fn=fn, lat=lat):
                t0 = time.perf_counter()
                v = fn(c)
                lat.append((time.perf_counter() - t0) * 1000)
                return v

            t = time.perf_counter()
            with ThreadPoolExecutor(n) as ex:
                list(ex.map(timed, picks))
            wall = time.perf_counter() - t
            with ThreadPoolExecutor(n) as ex:
                same = list(ex.map(fn, picks[:300])) == baseline
            results[f"{name}_threads{n}"] = {
                **summary(lat),
                "pages_per_s": round(len(picks) / wall),
                "same_output_as_serial": same,
            }
    for k, v in results.items():
        print(k, json.dumps(v))

    # Heavily referenced classes: the Usage tab's worst case.
    counts = sorted(((ont.getReferencingAxioms(c).size(), c) for c in classes), key=lambda x: -x[0])[:5]
    for n, c in counts:
        t = time.perf_counter()
        render_usage_b(c)
        print(
            f"usage: {labels.get(str(c.getIRI()), c.getIRI())}: {n} referencing axioms; "
            f"count + first 50 rendered in {(time.perf_counter() - t) * 1000:.1f} ms"
        )
    System.gc()


def cmd_horned(path: str) -> None:
    import resource

    import pyhornedowl

    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = time.perf_counter()
    o = pyhornedowl.open_ontology(str(GEN / path))
    load_s = time.perf_counter() - t
    rss = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 2**20  # bytes on macOS
    print(
        json.dumps(
            {"file": path, "load_s": round(load_s, 1), "rss_growth_mb": round(rss), "axioms": len(o.get_axioms())}
        )
    )
    names = [n for n in dir(o) if not n.startswith("_")]
    print("ontology methods:", ", ".join(names))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    if cmd == "coverage":
        cmd_coverage()
    elif cmd == "timing":
        cmd_timing(args[0], int(args[1]) if len(args) > 1 else 2000, args[2] if len(args) > 2 else "4,8")
    elif cmd == "horned":
        cmd_horned(args[0])
