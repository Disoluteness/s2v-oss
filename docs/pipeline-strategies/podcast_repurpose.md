# Podcast Repurpose Pipeline Strategy

## Clean-Room Boundary

`podcast_repurpose` is implemented from scratch for Script2Video Studio. It
reuses this repository's own pipeline DSL, artifact contracts, tool contracts,
state machine, and worker abstractions. It does not copy OpenMontage source
code, schemas, prompts, UI code, renderer components, skills, or pipeline files.

## Product Positioning

This beta pipeline turns a long audio episode into short vertical videos. The
input is audio, not video: the visual layer exists to carry captions and keep
the frame alive, and it is deliberately minimal.

Primary use cases:

- Podcast episode to social shorts.
- Interview audio clips.
- Audio newsletter with a visual layer.
- Conference talk audio highlights.

The defining constraint: **without a transcript this pipeline cannot work**. A
window chosen from loudness alone is a guess. Transcription is therefore a
required capability, not an optional one, and a placeholder transcript fails the
stage instead of producing empty clips.

## Stage Strategy

`intake`

- Normalize episode length, desired clip count, target duration, caption
  language, and the visual style (cover frame, brand frame, or generated
  background).

`source_ingest`

- Register the audio source and confirm speaker count, language, and usage
  rights.
- Missing or inaudible audio is an explicit blocker. This line does not degrade
  into "no audio, no problem".

`analysis`

- Probe duration and loudness; transcribe the episode with segment-level
  timing.
- Cut the transcript into topic-coherent segments. Segment boundaries are the
  only legal clip boundaries downstream — a clip starting mid-sentence is a bug,
  not a style.
- Require a real transcript. Placeholder output fails the stage.

`clip_selection`

- Pick windows that stand alone: a question answered, a claim supported, a story
  finished.
- Prefer quotable density over raw volume. A 40-second window with two strong
  lines beats a 90-second window with one.
- Record the transcript excerpt for each candidate so the reviewer reads instead
  of re-listening.

`edit`

- Design the minimal visual layer: cover frame, waveform or still background,
  speaker label.
- Place captions for silent viewing and keep them inside the platform safe area.
- Normalize loudness; podcast masters are usually hot and inconsistent.

`export_clips`

- Export approved windows as video files with the designed visual layer.
- Export is planned but not auto-dispatched.

`final_review`

- Review meaning preservation after the cut, caption accuracy, and audio
  clarity.
- Confirm the visual layer does not compete with the text.

`publish`

- Create the package with transcript excerpts and episode attribution.

## Segment Boundaries

Boundary rules, applied in order:

1. A segment must open on a sentence start and close on a sentence end.
2. A speaker change is a legal boundary only when the change is resolved inside
   the segment (question and answer both present).
3. Topic shifts beat duration targets: never extend a segment past a topic shift
   to hit a number.

## Known Limits

- No speaker diarization. Multi-voice episodes are transcribed as one stream;
  speaker labels are a human input in the edit plan.
- No word-level timestamps. Captions are placed per segment, so very fast
  speech can drift within a segment.
- The visual layer is static by design. Animated audio visualizers belong to a
  later motion runtime, not this pipeline.
- Loudness is normalized to a single target; per-segment loudness matching
  between very different recording conditions is not attempted.
