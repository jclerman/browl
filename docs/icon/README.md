# browl logo

`browl-logo.svg` is browl's logo: an owl perched on a branch, with a class hierarchy for breast feathers. It is a
hand-written SVG with no raster images or external resources.

## Motivation

The mark should combine three ideas, each recognizable on its own:

- **OWL, the standard.** An owl is the obvious emblem, and the name browl ends in it. The mark avoids anything that
  resembles W3C's own OWL logo, so it doesn't read as an official W3C mark.
- **An ontology.** The breast feathers are a small class tree: one root, two subclasses and four leaves, drawn as nodes
  and edges. It echoes the hierarchy views that browl itself shows.
- **Browsing.** The owl is perched, as if looking over what it browses. The perch also nods to browl's tree view, where
  every class sits on a branch of its parent.

Among the concepts considered, this one ("hierarchy plumage") read best as an owl first and an ontology second, and it
had the most room for detail at logo sizes.

## Design

| Element             | Treatment                                                                         |
| ------------------- | --------------------------------------------------------------------------------- |
| Body and ear tufts  | Deep blue silhouette                                                              |
| Facial discs, wings | A lighter blue, so the face and folded wings separate from the body               |
| Eyes                | Amber irises with dark pupils and white glints                                    |
| Beak                | Amber, split into a shadowed left face and a lit right face, with a ridge line    |
| Class tree          | Pale nodes joined by thinner edges, so the edges recede                           |
| Feather texture     | Faint scallops on the forehead, wings and breast, kept behind the tree            |
| Perch and talons    | Brown rounded branch; amber talons                                                |

**Color.** Deep blue carries the structure, and amber marks the eyes and beak. The two contrast strongly, and amber owl
eyes are instantly recognizable. browl's site accent is currently a lighter steel blue (`--accent` in
`browl/static/browl.css`); the logo and the site accent may be brought closer later.

**Light.** Every shaded part is lit from the same direction, up and to the right (1:30 on a clock face). Round forms
(body, facial discs, irises, tree nodes) use radial gradients whose focal point is offset toward that direction. The
wings and the perch use vertical linear gradients, lighter on top. The white glints sit up and to the right of each
pupil, matching the light.

**Dark mode.** The SVG switches palettes with `prefers-color-scheme`. In the dark palette the body becomes a light blue,
so it stays visible on a dark background, and the tree nodes and edges become dark. Because a gradient takes its colors
from where it is defined rather than where it is used, each palette has its own set of gradients, and CSS variables
(`--body-fill`, `--node-fill` and so on) pick the right set.

## Sizes

The texture, shading and tree detail only read at about 64px and up. The logo suits the site header, the README and
similar places. For the favicon, a plainer version of the same owl (flat colors, thicker tree edges, no texture or
perch) is planned, so the two read as one family.
