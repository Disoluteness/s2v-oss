# Talking Head Pipeline Strategy

## Clean-Room Boundary

`talking_head` is implemented from scratch for Script2Video Studio. It reuses
this repository's own pipeline DSL, artifact contracts, tool contracts, state
machine, and worker abstractions. It does not copy OpenMontage source code,
schemas, prompts, UI code, renderer components, skills, or pipeline files.

## Product Positioning

This beta pipeline takes long on-camera footage — interviews, lessons, founder
vlogs, webinar recordings — and produces single-speaker shorts. It is not a
"generate a person" pipeline: the person already exists in the footage, and the
job is to find the complete thought, remove the dead air around it, caption it,
and keep the speaker framed across aspect ratios.

Primary use cases:

- Interview highlights.
- Course or lesson clip extraction.
- Founder / expert vlog shorts.
- Webinar speaker cuts.

The closest sibling is `source_clip_factory`. Both consume existing media and
both emit clip packages; the difference is the selection criterion. Clip factory
asks "which window is interesting"; talking head asks "which window is a
finished sentence with a hook".

## Stage Strategy

`intake`

- Normalize speaker identity, caption language, target clip count and duration,
  and where the person sits in frame.
- Record whether the original voice is kept. Dubbing a talking head is a
  different pipeline (`localization_dub`), not a variant of this one.

`source_ingest`

- Register the on-camera footage and confirm a single identifiable speaker.
- Check sync audio, usage rights, headroom, and whether the speaker moves out of
  frame at any point.
- Stop for human review: a wrong or unreadable source is much cheaper to catch
  here than after cutting.

`analysis`

- Probe metadata and shot boundaries; sample frames as `analysis_frame` assets.
- Transcribe speech. Transcription is a hard requirement for this line because
  the selection criterion is linguistic, not visual.
- Flag placeholder transcripts as placeholders. A local placeholder transcript
  can carry a demo run, but it must never be presented as ASR output.

`clip_selection`

- Score candidate windows on three axes:
  - **Completeness** — does the clip contain a full thought (setup, claim,
    landing), not a fragment?
  - **Hook** — do the first two seconds give a reason to keep watching?
  - **Density** — how much dead air, filler, and verbal stumble sits inside the
    window?
- Prefer one strong window over three weak ones. Overlapping candidates are
  allowed but must be marked as overlapping so the reviewer is not fooled into
  thinking there are more distinct moments than there really are.
- Stop for human review before any cut is locked.

`edit`

- Cut approved windows, remove silence above the configured threshold, burn
  captions in the target language, and normalize loudness.
- Choose a reframing box that keeps the speaker centered. When the speaker moves
  across the frame, prefer a per-clip box over a single global crop.
- Captions are mandatory on this line: the format is watched muted far more
  often than not.

`export_clips`

- Produce a `clip_export_manifest`.
- Export is planned but not auto-dispatched, matching the clip-factory contract.

`final_review`

- Review cut points, caption coverage, and framing.
- Confirm the clip still makes sense to someone who has not seen the source.

`publish`

- Create the package with per-clip caption and framing metadata.

## Selection Scoring

The three axes are deliberately few. A score the reviewer cannot argue with is
a score that will be ignored. Each candidate carries its three sub-scores, the
window boundaries, and a one-line rationale written in the reviewer's language.

Completeness and hook are judged from the transcript; density is judged from the
audio energy timeline plus transcript gaps. No model is required to compute any
of them — the numbers must be explainable from the artifacts.

## Known Limits

- No face detection. Framing is a human decision recorded in the edit plan, not
  an automatic tracking result.
- No speaker diarization. Multi-speaker footage is handled as one stream; use
  `podcast_repurpose` for audio-first multi-voice material.
- Silence detection uses the audio energy timeline, not a VAD model, so long
  ambient pauses may survive a pass and need a manual trim.
- Placeholder transcripts can make a demo run succeed while the selection is
  meaningless. The stage flags this; it does not block it.
