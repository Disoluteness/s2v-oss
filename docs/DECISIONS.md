# Architecture Decisions

## ADR-001: Clean-Room Implementation

We will implement this platform from scratch. Existing AI video workflow
platforms may inform high-level product patterns, but no source code,
configuration, schemas, prompt text, UI code, or renderer components are copied.

## ADR-002: Pipeline-First Production

Every video run follows an explicit pipeline definition. Each stage declares
its required inputs, outputs, capabilities, timeout, and review policy.

## ADR-003: Artifact Contracts

Stage outputs are structured artifacts. Media files are assets. Artifacts are
stored as versioned JSON payloads and are used as contracts between stages.

## ADR-004: Provider Adapters

Every external vendor is wrapped in a provider adapter that normalizes
availability checks, cost estimation, input validation, execution, status
polling, cancellation, and output normalization.

## ADR-005: Start With FFmpeg Composition

The MVP starts with FFmpeg composition because spokesperson ads can ship with a
stable video/audio/subtitle pipeline. Remotion can be added after the baseline
workflow is reliable.

## ADR-006: Freeze the v1 Contract at 1.0.0

We declare the HTTP contract stable and publish what stability means. The
version constant moves from `0.9.0` to `1.0.0`, and `docs/API_STABILITY.md`
records the policy that number now refers to: which surface is stable, which is
experimental, which changes are additive, and the (deliberately short) deprecation
path.

Until this point the version tracked the roadmap, which was honest while the
contract was still being written. The two ideas come apart here: the roadmap is
done, so the number has to start meaning a promise instead of a progress report.
Leaving it at `0.9.x` would tell integrators — correctly — to expect the shape to
move.

Two choices inside the policy are load-bearing:

- **Additive changes stay free; removals and tightenings need the deprecation
  path.** The test is not how important a thing looks but whether a client
  written against the old docs would fail *silently*. That is why `/metrics` is
  experimental (pinning its label set would turn observability convenience into a
  permanent liability) while error-code strings are stable (callers branch on
  them).
- **No parallel `/api/v1` and `/api/v2`.** There is one deployment shape and one
  class of integrator; running two versions costs two docs, two regression paths
  and two gate runs for a benefit nothing currently needs. If that changes, this
  ADR is what gets revisited first.

The policy is enforced, not merely documented: `tests/test_release.py` pins the
catalogues in the repository, and gate section 25 `verify_release` repeats the
same assertions against a running instance.

