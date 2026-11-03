# Source Clip Factory Pipeline Strategy

## Clean-Room Boundary

`source_clip_factory` is implemented from scratch for Script2Video Studio. It
uses the local Script2Video DSL, artifact schemas, tool contracts, state
machine, and worker abstractions. It does not copy OpenMontage source code,
schemas, prompts, UI code, renderer components, skills, or pipeline files.

## Product Positioning

This beta pipeline turns long source videos, uploaded media batches, or media
links into a reviewable short-clip production flow. The V3 target is to make the
service understand the stages and contracts for source-led clipping. Deeper
video understanding belongs to V4, and fully automated batch export belongs to
later worker/render work.

Primary use cases:

- Long video to short clips.
- Webinar, livestream, or interview highlights.
- Product demo clips.
- Multi-asset short-clip batches.

## Stage Strategy

`intake`

- Normalize target platform, desired clip count, duration, aspect ratio, and
  selection goals.
- Preserve user source context and any manual instructions.

`source_ingest`

- Register uploaded source assets or source links.
- Record usage-rights notes, missing inputs, and manual context.
- Stop for human review so missing or wrong source media is caught early.

`analysis`

- Probe media metadata through local FFmpeg/ffprobe when available.
- Extract representative interval frame samples as `analysis_frame` assets.
- Record transcript availability, scene notes, and analysis limitations.
- Do not pretend placeholder transcript output is real ASR.

`clip_selection`

- Produce candidate clip windows, rationales, and review status.
- Stop for human review before edit/export choices are locked.

`edit`

- Convert approved candidates into an edit plan: trim windows, captions,
  reframing, audio handling, and target render profile.

`export_clips`

- Produce a `clip_export_manifest`.
- In V3 this stage is planned but not auto-dispatched because the clip-export
  worker payload is not implemented yet.

`final_review`

- Review clip quality, cut points, captions, rights, and platform fit.

`publish`

- Create the publish package and access metadata for exported clips.

## Artifact Contract

The pipeline introduces source-led artifacts:

- `source_media_review.v1`
- `media_analysis_report.v1`
- `clip_candidate_list.v1`
- `clip_export_manifest.v1`

It also reuses:

- `intake_brief.v1`
- `edit_plan.v1`
- `review_notes.v1`
- `publish_package.v1`

The new artifacts allow placeholder payloads during V3 smoke testing. Real
media-analysis implementations should fill the fields instead of changing their
meaning.

## Verification Scope

Minimum verification for this pipeline:

- Loader resolves `source_clip_factory`.
- Tool registry exposes `media_analysis`, `clip_selection`, and `clip_export`.
- Preflight and execution plan use the `source_clip_factory` key.
- Stage manifest keeps `clip_export` and `storage` planned but not
  auto-dispatched until worker payloads are implemented.
- Database smoke run can advance through source ingest and clip selection review
  gates and reach final review with a `clip_export_manifest`.
