# OWL-Browse

An OWL ontology browser. Host an OWL ontology as a browsable website. Every ontology entity gets its own page, built on-demand, cached once built.

## Features
 - Top/home page indicates ontology metadata
 - Displays hierarchies as collapsible trees (separate hierarchies for each OWL entity type: classes, each type of property [object, annotation, data])
   - Hierarchies indicate counts of immediate children and all descendents
 - Autocomplete search over all entities, filterable by entity-type, and by annotations to search over (e.g. rdfs:label, or any given annotation property, searching over direct values, or all values including indirect values per RDFS entailment)
 - View-page for each entity
   - Showing:
     - Immediate parents
     - Annotation-values
     - Logical axioms in easy-to-read Manchester syntax
   - Every entity displayed as a hyperlink, clickable to navigate to that entity's page

## Architecture
  - Served via Python FastAPI
    - Ontology entities represented as Pydantic objects where reasonable
  - Runs against Apache Jena/Fuseki, hosting the ontology in question
  - Admin page supports selection/upload of an ontology into the service
  - Uses Owlready2 to help with display of logical axioms in Manchester syntax
