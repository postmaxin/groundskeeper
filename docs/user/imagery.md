# Map imagery
<!-- order: 85 -->

The aerial photo under the robot's map. Set in [Settings → Map imagery](#settings/imagery).

- **One card per source that covers your yard**, each with its own sample of the yard, resolution, licence and
  season, sharpest first. Pick one, or leave it on **Automatic** (the sharpest free source with real imagery there).
- **Keyed services** (Esri, Mapbox, MapTiler, and some national ones) are contacted only after you save a key.
- **The whole catalog** (its own section, with a Browse button) lists every bundled source by country and then by
  state or province, with search and filters, without contacting any. Search a state by name or code (**Texas** or
  **TX**) to see everything that serves it: its own state, county and city sources, then the national and worldwide
  ones that cover it.
- **Add a source:** paste a map service address; the panel works out what it is, then **Try it at your yard**.
- **Align:** Align on the map (in this section, or **Align** next to the imagery credit on Live's map). Pick a spot on the robot's map, then the same spot in the photo. One pair
  fixes the shift; two or more fix rotation; three or more fix scale too.

## Where the panel thinks your yard is

From the robot's own map: the shapes around the robot's map origin. A shape more than 2 km from the rest (for example a
charging point with an empty location) is left out, so the map still opens on your yard and the right sources are
listed. The panel names it here, on Live and in [Map Studio](#studio) as **km from the yard**; fix or delete it in the
Yarbo app. If a source you expected says it **does not cover this yard**, look for that note first.

## How sharp is sharp?

The number on every card (and in the corner of each sample) is how much ground one pixel covers. **Smaller is
sharper.** People rarely have a feel for the unit, so:

| Per pixel | What you can make out | Verdict |
|---|---|---|
| 7–10 cm (0.25 ft) | Single pavers, sprinkler heads, the mower itself; a crisp lawn edge | Very sharp: county GIS flights, some state programmes |
| 15–20 cm (0.5 ft) | Beds, paths and the lawn edge are clear | Sharp: fine for drawing no-go zones |
| 25–30 cm (1 ft) | Edges go soft; you can still place a zone to within a step | Good enough |
| 50–60 cm | A lawn edge is one blurry pixel; you recognise the yard, not much more | Coarse: USGS NAIP and most national programmes |
| 1 m and up | Whole beds blur together | Too coarse: look for a better source |

Where the detail is: **county and state GIS services** are usually the sharpest free imagery there is, at 7–15 cm,
where the national programmes stop at 30–60 cm. The catalog bundles many of them; **Browse the catalog** shows
every source's sharpest figure. **Keyed services** (Esri, Mapbox, MapTiler) can be sharper than the free ones at
your yard once you add a key. And sharpest is not always best: a leaf-off (spring) flight shows the ground under
trees that a summer one hides, so look at the samples, not just the numbers.

## Getting the most detailed map

1. Open [Settings → Map imagery](#settings/imagery). The cards are sharpest first; the badge says how sharp each is.
2. Click a sample to enlarge it. Compare the two or three sharpest: clear beats slightly sharper but cloudy.
3. Sharpest free source 30 cm or coarser? Add a key to a worldwide service, or find your county or state's own
   service and paste it under **Add a source**. [Finding imagery for a new place](#help/imagery-finding) says where
   to look and what the numbers mean.
4. Easiest of all: connect an [AI assistant](#help/mcp) and ask it to *find the most detailed aerial map of my yard*.
   It compares every source, looks at the samples, searches your region's public services for a sharper one and
   gives you the address to paste in. Your location never leaves the panel.

## Privacy

The panel only contacts sources whose coverage contains your yard. Probing every source would send your home
location to all of them.
