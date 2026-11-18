# Ontology diff

Requirements and open questions for showing how two versions of an ontology differ, with each change in its local
graph context. This is a new area of work, at an early stage: nothing here is decided yet. It is requirement U-43 of
[DESIGN.md](DESIGN.md), which stays the source of truth for browl as a whole; where the two conflict, DESIGN.md wins.

## Motivation

ROBOT's `diff` lists the axioms added and removed between two ontologies, at best as Markdown. That works for a few
entities, but for more it is hard to take in, and it shows no context: a changed axiom appears without the entity's
parents, children or neighbours, so the reader can't see where the change sits in the ontology or what it affects.

browl already has most of what that context needs: hierarchy trees with descendant counts, display names, Manchester
rendering with links, and a reasoner in the load pipeline.

## Requirements

All tentative. Priorities are to be decided.

| Requirement | Notes |
| --- | --- |
| **D-1** · Compare two ontology files, in the syntaxes browl loads (A-2), with their imports handled as the load pipeline handles them. | Whether imports' changes count as changes is open (Q-6). |
| **D-2** · Find the changes at the level of OWL axioms, not triples, so that a class expression written with different blank nodes is not a change. | The OWLAPI's structural equality of axioms, as ROBOT `diff` uses. |
| **D-3** · Group the changes by entity: added, removed, logically changed, changed only in annotations. | |
| **D-4** · Name the kinds of change people look for: an entity moved in the hierarchy, deprecated, given `replaced_by`, relabelled; an IRI renamed. | Renames can only be guessed (Q-4). |
| **D-5** · A summary: counts by kind of change and entity type, linking to the changes. | |
| **D-6** · The hierarchy trees (U-7) with each node marked added, removed, moved or changed, and a count of the changes beneath each node, so the reader can go straight to the subtrees where changes gather. | |
| **D-7** · For each changed entity, a graph of its local context in both versions overlaid: parents, children and restriction edges (e.g. `part_of`), with added, removed and unchanged elements told apart. | Layout and depth are open (Q-7, Q-8). |
| **D-8** · Each changed entity's axioms before and after, in Manchester syntax with links, the changed parts marked. | |
| **D-9** · Changes in what is entailed, not only in what is asserted: classes whose inferred parents changed although their own axioms didn't. | Needs a reasoner on both versions; explanations (U-40) would show why. |
| **D-10** · The diff between the served ontology and the one an admin uploads to replace it. | Needs the previous upload kept, which today is deleted (P-6). |
| **D-11** · The diff of any two files, without changing the served ontology. | |
| **D-12** · A command-line tool that writes a diff as a self-contained static HTML report, e.g. for a pull request in an ontology's repository. | Ontology repos already run ROBOT `diff` in CI for this. |
| **D-13** · The diff is also available as JSON (U-42). | |

## Shape of a solution (sketch)

browl serves one ontology from one Fuseki store, and nothing here should change that. A sketch to start the discussion:

- **A diff job** like the reasoning stage: ROBOT or the OWLAPI, in the sandbox (P-30), reads both versions, finds the
  changed axioms, and writes them, with the local context of each changed entity in both versions, to a diff database
  in SQLite. Nothing about the diff then needs the store, so a diff doesn't need a second ontology loaded in Fuseki.
- **One engine, three front ends:** a diff page in the app for D-10 and D-11; a static report from the same templates
  for D-12; the JSON API for D-13.
- **Graphs** drawn on the server as SVG, e.g. with Graphviz `dot`, whose layered layout suits hierarchies. That works
  without JavaScript (U-17) and in a static report. An interactive viewer (e.g. Cytoscape.js, served from the app's
  static files) could come later without a front-end build step.

## Open questions

- **Q-1 · Where it runs.** Inside browl, as a separate tool that shares browl's code, or both? A command-line report
  (D-12) needs no server, Fuseki or sign-in.
- **Q-2 · Who may diff.** Admins only, or any user? Diffing uploaded files (D-11) means accepting uploads from them,
  with the same limits and sandbox as the load pipeline.
- **Q-3 · Engine.** ROBOT `diff`, the OWLAPI directly, or another tool? Prior art to check first: Bubastis (EBI) and
  ecco, which both classify changes by their effect on entailments, and the OWLDiff plugin for Protégé. As far as we
  know none shows changes in graph context.
- **Q-4 · Renames.** An IRI that disappears while another appears with the same label and axioms is probably a rename.
  Is guessing worth it, and how sure must a guess be?
- **Q-5 · Reasoning.** D-9 needs both versions classified with the same reasoner, doubling the reasoning time. On by
  default, an option, or only when the served ontology was reasoned?
- **Q-6 · Imports.** Are changes in imported ontologies shown, shown apart, or ignored?
- **Q-7 · Graph depth and size.** How far around a changed entity does its context graph reach, and how is a graph
  kept readable when an entity has hundreds of children?
- **Q-8 · Many changes at once.** One graph per changed entity, or one graph for a group of nearby changes? How does a
  release with thousands of changes (a new import, a mass deprecation) stay usable?
- **Q-9 · Scale.** Time and memory to diff two releases of Uberon, and of NCBITaxon.
- **Q-10 · Keeping versions.** For D-10, how many previous uploads are kept, and is the diff computed during the load
  or on demand?

## First step

A spike, before any design: diff two consecutive Uberon releases with ROBOT `diff` and with the OWLAPI, measure time and
heap, sort the changes into the kinds of D-3 and D-4, and draw the context graph for a few changed classes by hand or
with a quick script. It should show how noisy the raw changes are, and whether D-6 or D-7 shows them better.
