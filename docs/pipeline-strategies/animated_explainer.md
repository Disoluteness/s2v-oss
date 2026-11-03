# Animated Explainer Pipeline Strategy

## Clean-Room Boundary

`animated_explainer` is a Script2Video Studio pipeline implemented from
scratch. It may borrow the high-level product idea of staged video production,
but it does not copy OpenMontage source code, schemas, prompts, renderer
components, skills, pipeline files, or UI code.

## Product Positioning

This beta pipeline turns a topic or script into a structured short explainer:
narration, mobile captions, visual beats, optional supporting images, optional
voice/music, and a planned motion-graphics render path.

Primary use cases:

- Science explainer, such as "why the sky is blue".
- Product concept explainer.
- Educational short.
- Data or process explainer.

The V3 goal is product-matrix coverage and orchestration correctness. The V5
goal is higher-quality composition through a dedicated composition graph and
HTML/Canvas renderer. Until V5 lands, this pipeline should not silently route
to the avatar-specific compose path.

## Stage Strategy

`intake`

- Normalize topic, raw script, target platform, audience assumptions, duration,
  and aspect ratio.
- Preserve user wording and avoid adding unverified claims.

`plan`

- Define one clear explanatory thesis.
- Choose the structure: hook, intuition, mechanism, example, recap.
- Record visual metaphor and render assumptions before expensive generation.

`script`

- Produce plain-language narration with short captions.
- Prefer concrete analogies and keep terms explainable.

`storyboard`

- Convert narration into visual beats: kinetic text, simple diagram, comparison,
  timeline, or symbolic scene.
- Mark image-generation needs only when a still asset improves clarity.

`asset_generation`

- Generate subtitles as the required deterministic asset.
- Add TTS, music, or supporting images only when providers are available and
  selected by the execution plan.

`edit`

- Produce a motion edit plan with scene order, caption emphasis, overlays,
  transitions, audio notes, and render intent.

`compose`

- Require `video_composition` at preflight so the pipeline is honest about its
  render dependency.
- Do not auto-dispatch the current avatar-specific FFmpeg compose job. The
  actual explainer renderer belongs to the V5 composition graph work.

`final_review`

- Review explanation clarity, visual accuracy, subtitle fit, and audio mix.

`export`

- Package the render output and access metadata after final approval.

## Artifact Contract

The beta pipeline reuses the current v1 artifact schemas:

- `intake_brief.v1`
- `production_plan.v1`
- `spoken_script.v1`
- `shot_list.v1`
- `asset_manifest.v1`
- `edit_plan.v1`
- `render_summary.v1`
- `review_notes.v1`
- `publish_package.v1`

Future renderer-specific details should be added through backward-compatible
schema versions or nested render metadata, not by changing existing v1 fields in
place.

## Verification Scope

Minimum verification for this pipeline:

- Loader resolves `animated_explainer`.
- Preflight and execution plan use the `animated_explainer` pipeline key.
- Required render capability is visible in preflight.
- Stage manifest does not auto-dispatch `video_composition` for compose while
  the explainer renderer is still planned.
- A database run can be created with a snapshotted pipeline version.
- A smoke run can advance through review gates to a placeholder render summary,
  proving the orchestration path is wired before V5 rendering work begins.
