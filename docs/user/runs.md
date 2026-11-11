# Runs
<!-- order: 20 -->

Every run, and how each one went.

## The overview

One table of every run (short runs are dimmed), then totals, trends over time, and the problem spots that keep
coming back across mows. Trends need two mows or more.

## A run

Open a run for its stats (duration, distance, RTK share, coverage, gaps, battery and energy per 100 m²), replay
with a scrubber (Play draws the track as it goes; the Speed menu next to it runs 1× to 300× real time, hurrying
through stretches where the robot stood still, or the whole run in about 30 s), coverage by area, and:

- **Duration** is working time: a pause or a charging stop mid-plan is not counted, so a mow paused overnight and
  finished in the morning shows its real length (and pace). A break of half an hour or more shows underneath.
- **Gaps:** only RTK-fixed track counts. Where the position was not fixed the ground is "unknown", never a gap.
  Gaps under 1 m² are ignored; each gap is labelled missed, boundary margin or next to RTK loss. A run that picks a
  plan up again after an earlier run (one that was not joined onto it) counts what that run mowed as done.
- **Planned vs driven:** the robot plans the run's areas (a preview; nothing is sent to drive) and the track is
  compared with it. Off-plan stretches are pink: across the stripes, or away from the plan.
- **Problems in this run:** stuck spells, place-related errors, steep ground, weak positioning (naming a weak HaLow
  link or late corrections where there was one) and hard going.
- **Colour:** the track by RTK, satellites, HaLow, battery, blade load, slope, speed or wheel slip.
- **Join with previous:** one mow split into two runs (a long charge break) can be joined.
