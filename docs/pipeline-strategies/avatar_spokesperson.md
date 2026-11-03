# Avatar Spokesperson Pipeline Strategy

## Clean-Room Boundary

`avatar_spokesperson` is implemented from scratch for Script2Video Studio. It
uses the existing Script2Video pipeline DSL, artifact schemas, provider
contracts, worker jobs, and Web workspace controls. It does not copy
OpenMontage source code, schemas, prompts, UI components, renderer components,
or pipeline files.

## Product Positioning

This pipeline is the formal V3 version of the original MVP spokesperson flow.
It supports short avatar-led videos where a presenter carries the message and
supporting assets make the product or topic concrete.

Primary use cases:

- Brand ad: short promotional videos for a brand, campaign, or product.
- Knowledge spokesperson: educational or explanatory scripts delivered by an
  avatar host.
- Product intro: concise product feature walkthroughs with optional product
  imagery.
- Commerce short video: host-led short-form shopping or recommendation clips.

The legacy `spokesperson_ad` key remains available for existing runs and tests.
New production runs should prefer `avatar_spokesperson`.

## Stage Strategy

`intake`

- Normalize raw script, brand, product, platform, duration, language, and aspect
  ratio.
- Preserve user wording. Do not rewrite the script here.

`plan`

- Decide the delivery format, hook, tone, required media, provider assumptions,
  and review checklist.
- Treat provider-backed avatar generation as a production choice, not as a
  silent fallback.

`script`

- Produce speakable timed narration segments and short mobile captions.
- Keep captions compact enough for vertical video.

`storyboard`

- Map every spoken segment to an avatar framing and visual support requirement.
- Identify whether person image, product image, brand logo, subtitles, music, or
  generated imagery is required.

`asset_generation`

- Use manifest dispatch for provider-backed avatar, subtitle, TTS, music, image,
  compose, and storage steps.
- Queue long-running provider work as background jobs.
- Allow provider callback and polling to coexist.

`asset_review`

- Gate generated avatar clips, audio clarity, reference-image fit, and subtitle
  readability before edit planning.

`edit`

- Select approved assets, define the timeline intent, caption style, audio mix,
  and render target.

`compose`

- Render the final video from approved avatar video, subtitles, narration, and
  optional music through the selected composition tool.

`final_review`

- Review the final rendered video for quality, brand accuracy, audio mix, and
  subtitle fit.

`export`

- Produce a publish package with render asset id, temporary access link, and
  workspace link.

## Artifact Contract

The inherited DSL stages use the current v1 artifact schemas:

- `intake_brief.v1`
- `production_plan.v1`
- `spoken_script.v1`
- `shot_list.v1`
- `asset_manifest.v1`
- `review_notes.v1`
- `edit_plan.v1`
- `render_summary.v1`
- `publish_package.v1`

Future variants should add fields through backward-compatible schema versions
instead of changing existing v1 semantics in place.

## Verification Scope

Minimum verification for this pipeline:

- Loader can resolve `avatar_spokesperson` through `extends`.
- The pipeline snapshot round-trips and has a stable content hash.
- Preflight and execution plan include the new pipeline key.
- Database run creation stores a pipeline version snapshot for the new key.
- MVP sample creation uses `avatar_spokesperson` by default while legacy
  `spokesperson_ad` remains loadable.
