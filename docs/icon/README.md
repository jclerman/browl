# browl logo and icons

browl's visual identity is three hand-written SVGs, with no raster images or external resources:

- [`browl/static/browl-logo.svg`](../../browl/static/browl-logo.svg), the **logo**: an owl on a branch, with a class
  tree for breast feathers. The app uses it as its favicon, and it suits any logo-sized spot.
- [`browl-wordmark.svg`](browl-wordmark.svg), the **wordmark**: the name, with "owl" drawn into it. It heads the
  README.
- [`browl-tree.svg`](browl-tree.svg), the **tree illustration**: the owl as one entry in a class tree, for other
  documents.

The logo lives with the app's static files because the app serves it. The other two are for documents only.

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

The texture, shading and tree detail only read at about 64px and up. At favicon size (16 to 32px) the logo reduces to
a blue owl shape with two amber eyes, which is still recognizable, so the app uses the logo itself as its favicon. A
plainer favicon variant (flat colors, thicker tree edges, no texture or perch) remains an option if the detailed one
looks muddy in browser tabs.

## Wordmark

`browl-wordmark.svg` spells "browl" in simple geometric strokes, with "owl" drawn into it:

- The **o** is an owl eye: a shaded amber iris, a dark pupil and a white glint, lit like the logo's eyes.
- The **w** is shaded like the logo's folded wings, lighter at the top.
- A thin **brow** of nodes and edges runs from the top of the b, dips over the eye, and ends at the top of the l. It
  makes the ontology reference: a small graph, and the "brow" in br‑owl.

It is drawn with paths rather than a font, so it looks the same everywhere. Its brow line is thin and disappears at
small sizes, so it is meant for landing pages such as the README, not for icons.

## Tree illustration

`browl-tree.svg` shows a class tree drawn the way browl's hierarchy view draws one: connector lines, nodes and grey
bars standing in for class names. The logo's owl, without its perch, is one of the entries, sitting on its own row's
connector. The owl is small within the frame, so the illustration suits documents rather than icons. It is generated
from the logo (the owl is copied in as an SVG symbol), so if the logo changes, it should be regenerated or edited to
match.
