# Cinematic Pipeline Strategy

## Clean-Room Boundary

`cinematic` is implemented from scratch for Script2Video Studio. It reuses this
repository's own pipeline DSL, artifact contracts, tool contracts, state
machine, and worker abstractions. It does not copy OpenMontage source code,
schemas, prompts, UI code, renderer components, skills, or pipeline files.

## Product Positioning

This beta pipeline cuts a mood-led piece from existing footage: a brand trailer,
a teaser, a mood film. The criterion is emotional, not informational — the same
shot that is useless in an explainer can be the strongest beat in a trailer.

Primary use cases:

- Brand trailer or teaser.
- Mood film / atmosphere piece.
- Product teaser.
- Event promo.

Because the promise is motion and mood rather than narrative, this pipeline is
the natural home of the pre-render promise check: a piece that claims to be
motion-led must not turn out to be a slideshow with music.

## Stage Strategy

`intake`

- Normalize the emotional register (calm, tense, uplift, melancholy), target
  duration, pacing, reference pieces, and where the music comes from — user
  supplied or generated.

`source_ingest`

- Register footage and music; confirm rights for both.
- Record whether the piece is carried by the shots or by the soundtrack. That
  single answer changes the edit plan more than any other input.
- Review shot variety: eight near-identical shots cannot carry a minute.

`analysis`

- Probe metadata, detect shot boundaries, and compute average shot length.
- Sample frames and extract an audio energy timeline; the timeline is the beat
  map used by selection.
- Note motion characteristics per shot where the analysis can support it.

`clip_selection`

- Rank shots by emotional fit, visual strength, and beat alignment.
- Narrative completeness is explicitly **not** a criterion here. A beautiful
  three-second insert has no story and can still be the right choice.
- Record the intended position of each shot in the arc (open, build, peak,
  release), so the reviewer sees a shape rather than a list.

`edit`

- Lay out the arc, place music with cue points, choose transitions, and specify
  a grade.
- When no grading tool is available, the plan records a flat grade and says so.
  It must never claim a grade that was not applied.
- Keep total shot count consistent with the target duration and pacing: a
  60-second piece at "fast" pacing needs enough shots to survive the claim.

`export_clips`

- Export the graded cut. Export is planned and not auto-dispatched.

`final_review`

- Review motion, pacing against the beat, grade consistency, and whether the arc
  lands.
- Apply the promise check: if the intake asked for motion-led and the plan is
  mostly stills, the reviewer sees the contradiction before rendering, not after.

`publish`

- Create the package with grade notes, music attribution, and export metadata.

## The Motion Promise

Cinematic work is the easiest place to accidentally ship a slideshow. Three
numbers make the risk visible before render:

1. **Still ratio** — share of screen time where the frame does not change.
2. **Average shot length** — long average plus a "fast" claim is a contradiction.
3. **Intent coverage** — share of shots whose plan states a movement or
   transition intention.

These are computed from the analysis artifacts, not judged by taste. Taste
belongs to the human gate; these three numbers belong to the system.

## Known Limits

- No automatic color grading before the P5 tools land. Grade intent is recorded
  and applied manually or left flat, never faked.
- No camera-motion estimation beyond what shot analysis can infer. "This shot
  drifts left" may be a human note rather than a measured fact.
- No 21:9 render profile yet. Widescreen output uses 16:9 with letterboxing
  recorded in the export metadata.
- Music generation depends on a provider; without one the plan must fall back to
  user-supplied music or an explicitly silent cut.
