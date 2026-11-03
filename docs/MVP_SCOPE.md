# MVP Scope

## Goal

Create a clean-room MVP for a Chinese spokesperson advertisement workflow.

The first usable product should let a user submit a script and receive a
downloadable 9:16 MP4 after staged review.

## Frozen MVP Scope

- Single pipeline: `spokesperson_ad`
- Single main output: 15-60 second vertical video
- Single workspace UI
- Stage review for production plan, spoken script, storyboard, assets, and final render
- One avatar provider adapter
- One TTS provider adapter when the avatar provider does not provide voice
- One music source
- FFmpeg composition
- Basic run and stage logs

## Out of Scope

- Multi-tenant billing
- Full video editor
- Template marketplace
- Automatic publishing to social platforms
- Large-scale worker autoscaling
- Multiple video pipelines

## First Engineering Milestones

1. API skeleton and in-memory run lifecycle
2. PostgreSQL persistence
3. Object storage abstraction
4. Agent runner stage artifacts
5. First avatar provider adapter
6. Subtitle and audio generation
7. FFmpeg composition
8. Minimal web review UI
9. End-to-end sample generation

