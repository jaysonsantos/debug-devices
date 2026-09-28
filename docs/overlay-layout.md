# Highlight layout (spec)

The phone app (`/v1/overlay`), the MCP server (the annotated image of `phone_highlight`), and the monitor page draw highlight boxes with the same rules. Change this file first, then the three places.

## Why

On a real board, two highlighted pads can be 40-80 px apart in a 1568 px photo. Labels drawn directly above each box covered the neighbour box and overlapped each other. Thin green outlines were hard to see on a red board.

## Rules

1. **Tags, not long labels, at the box.** Each box has a short tag (1-3 characters). A box without a tag gets the next free letter: `A`, `B`, `C`, ... The tag goes in a small badge next to the box. The full label goes into a legend.
2. **Legend.** One legend panel lists `tag: label` for each box, in the tag order. It sits in the corner of the view that is farthest from all boxes (the largest minimum distance from the corner panel to any box). It has a dark background with 85% opacity. When the legend would cover a box, it moves to the next corner; when every corner covers a box, it goes outside the image on the page and in the annotated image (an extra strip at the bottom), and on the phone it becomes semi-transparent.
3. **Badge placement.** Try the 8 positions around the box (right, left, above, below, then the diagonals), first at a gap of 4 px, then at 3 larger gaps. Take the first position where the badge does not cover any box (with 4 px padding), any other badge, or the legend, and stays inside the view. If no position works, put the badge outside the box cluster (on the ray from the cluster centre through the box, beyond the cluster bounds) and draw a thin leader line from the badge to the nearest box edge.
4. **Minimum box size.** Draw each box at least 32 px (image pixels) or 24 dp (phone) wide and high, grown around its centre. The real, smaller area stays in the data.
5. **Outline with contrast.** Draw a dark outline (black, 60% opacity, 2 px/dp wider on each side), then the colour outline (3 px/dp). Colours go in this order: green `#00E676`, cyan `#00E5FF`, yellow `#FFEA00`, magenta `#FF4081`, then again. The badge and the legend entry use the colour of their box.
6. **Arrows** (targets outside the view) follow rule 5 for colour and contrast, and their labels go into the legend too (with their tag).
7. **Inset (page and annotated image only).** When the largest box is smaller than 3% of the image width, add an inset in a free corner: the area around all boxes, enlarged 3x (at most 25% of the image width), with the same outlines and tags. The phone preview has no inset.
8. **Stable output.** The same boxes in the same view give the same layout (no random choices).

## Geometry (exact, shared by all implementations)

The rules above, made exact, so that the Python layout (`mcp/debug_devices_mcp/overlay_layout.py`) and the app give the same numbers. `docs/overlay-layout-vectors.json` has inputs and expected outputs; every implementation must pass them (tolerance 0.01 unit). The unit is an image pixel (the annotated image and the page, in the pixels of the drawn picture) or a dp (the phone). Text is not measured: sizes come from the character count.

- **The phone view**: on the phone, the view is the safe area of the preview: the preview area without the status bar, the navigation bar, display cutouts, and the app's own status label. Boxes outside that area count as outside the view (no badge; the legend still lists them, rule 2 and B-S2).
- **Inputs**: the view size `width`, `height`; `min_box` (32 for images, 24 for the phone); the boxes in view units (`x`, `y`, `width`, `height`, optional `tag`, `label`) in their order; the arrows (`angle_deg`, optional `tag`, `label`); `inset` (true for the page and images, false for the phone).
- **Tags**: a box or arrow without a tag gets the first letter from `A` to `Z` that no other box or arrow uses, in the order boxes then arrows. Only the first 3 characters of a tag count.
- **Colours**: item `i` in the order boxes then arrows gets colour `i % 4` of `#00E676`, `#00E5FF`, `#FFEA00`, `#FF4081`.
- **Drawn box**: the box grown around its centre to at least `min_box` wide and high.
- **Badge size**: width `8 + 9 × len(tag)`, height 18.
- **Legend size**: rows are `tag: label` for the boxes, then the arrows. Width `12 + 8 × (the longest row in characters)`, height `12 + 18 × rows`. No boxes and no arrows: no legend.
- **Distance** between two rectangles: 0 when they overlap or touch, else the Euclidean distance between their nearest points. **Overlap** means an overlap of more than 0 in both axes (touching edges do not overlap).
- **Legend corner**: the legend rectangle 8 units in from the corner, for the corners top-left, top-right, bottom-left, bottom-right. Sort the corners by the smallest distance from the legend to any drawn box (largest first; equal values keep the corner order). Take the first corner where the legend does not overlap any drawn box grown by 4 on each side. With no box (arrows only), take top-left. When every corner overlaps, the legend is outside: at `x` = 8, `y` = view height + 8, and `legend_outside` is true (the image gets an extra strip of legend height + 16 at the bottom). The phone has no strip: when `legend_outside` is true, it draws the legend in the first corner of the sorted list, at 50% opacity (the badges were placed with the legend outside, so they do not avoid it).
- **Badges** (in box order): for each gap in 4, 12, 24, 40, try the positions right, left, above, below, above-right, above-left, below-right, below-left. With the drawn box `D`, the badge size `w`×`h`, and gap `g`:
  - right: `(D.right + g, D.cy − h/2)`, left: `(D.left − g − w, D.cy − h/2)`
  - above: `(D.cx − w/2, D.top − g − h)`, below: `(D.cx − w/2, D.bottom + g)`
  - above-right: `(D.right + g, D.top − g − h)`, above-left: `(D.left − g − w, D.top − g − h)`
  - below-right: `(D.right + g, D.bottom + g)`, below-left: `(D.left − g − w, D.bottom + g)`

  Take the first badge rectangle that is inside the view and does not overlap any drawn box grown by 4, any badge placed before (grown by 4), or the legend (when it is inside).
- **Badge fallback**: the cluster is the bounding box of all drawn boxes, with centre `c`. The direction goes from `c` to the drawn box centre (right, `(1, 0)`, when they are the same point). The point `p` is where the ray leaves the cluster. The badge centre is `p` + direction × (16 + max(w, h) / 2), then the badge is moved inside the view. `outside` is true, and a leader line goes from the badge centre to the nearest point of the drawn box.
- **Arrows**: the anchor is on the ray from the view centre at `angle_deg` (0 = right, 90 = down), 28 units inside the view edge.
- **Inset** (only when `inset` is true, and the largest box side is less than 3% of the view width): the source is the bounding box of the boxes (not the drawn ones), grown on each side by half of its larger side, then kept inside the view. The scale is 3, reduced so that the inset width is at most 25% of the view width. The inset (source size × scale) goes 8 units in from a corner, in the corner order, at the first corner where it overlaps no drawn box (grown by 4), no badge, and not the legend. No free corner: no inset.
- **Numbers**: all values are rounded to 2 decimals in the output.
