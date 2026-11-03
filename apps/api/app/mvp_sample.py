from __future__ import annotations

import base64
import math
import tempfile
from pathlib import Path

from apps.api.app.db_store import DatabaseStore
from apps.api.app.models import (
    ApprovalRequest,
    Asset,
    AssetUpload,
    AvatarVideoFromAssetsRequest,
    BackgroundJob,
    ComposeVideoRequest,
    EventCreate,
    EventSourceType,
    ImageGenerateRequest,
    MusicGenerateRequest,
    MvpReferenceAssetInput,
    MvpSampleRequest,
    MvpSampleResult,
    ProjectCreate,
    RunCreate,
    RunStatus,
    SubtitleGenerateRequest,
    TtsGenerateRequest,
)
from packages.agent_runner.planner import SpokespersonAdPlanner
from packages.agent_runner.runner import RuleBasedAgentRunner
from packages.compose_kit import LocalPreviewMediaFactory


class MvpSampleRunner:
    """Runs a complete local-preview path from script input to rendered MP4."""

    def __init__(
        self,
        store: DatabaseStore,
        *,
        media_factory: LocalPreviewMediaFactory | None = None,
    ) -> None:
        self.store = store
        self.media_factory = media_factory or LocalPreviewMediaFactory()

    def run(self, payload: MvpSampleRequest) -> MvpSampleResult:
        if not self.media_factory.check_availability():
            raise ValueError("ffmpeg is required for local MVP sample media")

        original_agent_runner = self.store.agent_runner
        if not payload.use_configured_planner:
            self.store.agent_runner = RuleBasedAgentRunner(planner=SpokespersonAdPlanner())

        try:
            return self._run(payload)
        finally:
            self.store.agent_runner = original_agent_runner

    def _run(self, payload: MvpSampleRequest) -> MvpSampleResult:
        pipeline = self.store.pipeline_loader.get_pipeline("avatar_spokesperson")
        project = self.store.create_project(
            ProjectCreate(
                title=payload.title,
                default_language="zh-CN",
                default_aspect_ratio=payload.aspect_ratio,
            )
        )
        run = self.store.create_run(
            project_id=project.project_id,
            payload=RunCreate(
                pipeline_key=pipeline.pipeline_key,
                raw_script=payload.raw_script,
                brand_name=payload.brand_name,
                product_name=payload.product_name,
                target_platform=payload.target_platform,
                target_duration_seconds=payload.target_duration_seconds,
                aspect_ratio=payload.aspect_ratio,
                style_key="local_preview",
            ),
            pipeline=pipeline,
        )
        run = self.store.start_run(run.run_id)
        if run is None:
            raise RuntimeError("created run could not be started")

        for stage_key in ["plan", "script", "storyboard"]:
            run = self._approve(stage_key, run.run_id)

        run = self.store.get_run(run.run_id)
        if run is None or run.current_stage_key != "asset_review":
            raise RuntimeError("run did not reach asset_review after storyboard approval")

        stage_ids = {stage.stage_key: stage.stage_run_id for stage in run.stages}
        current_payloads = self.store.artifacts.current_payloads(run.run_id)
        spoken_script = current_payloads.get("spoken_script")
        shot_list = current_payloads.get("shot_list")
        if not spoken_script or not shot_list:
            raise RuntimeError("spoken_script and shot_list artifacts are required")

        segments = spoken_script.get("segments") or []
        duration_seconds = _duration_seconds(spoken_script, payload.target_duration_seconds)
        asset_stage_id = stage_ids.get("asset_generation")
        narration_asset = None
        external_provider_calls = False
        uploaded_reference_assets, external_reference_assets = self._collect_reference_assets(
            run_id=run.run_id,
            stage_run_id=asset_stage_id,
            payload=payload,
        )
        if (
            payload.quality_mode == "provider_avatar"
            and "person_image" not in uploaded_reference_assets
            and _external_reference_url(external_reference_assets, "person_image") is None
        ):
            uploaded_reference_assets["person_image"] = self._generate_provider_person_image(
                run_id=run.run_id,
                stage_run_id=asset_stage_id,
                payload=payload,
                spoken_script=spoken_script,
                shot_list=shot_list,
            )
            external_provider_calls = True
        reference_asset_entries = [
            *_reference_entries_from_assets(uploaded_reference_assets),
            *external_reference_assets,
        ]

        subtitle_result = self.store.generate_subtitles(
            run.run_id,
            SubtitleGenerateRequest(
                stage_run_id=asset_stage_id,
                filename="mvp-subtitles.srt",
                asset_metadata={"mode": payload.quality_mode},
            ),
        )
        if subtitle_result is None:
            raise RuntimeError("subtitle generation failed")
        _subtitle_invocation, subtitle_asset = subtitle_result

        if payload.quality_mode in {"provider_audio", "provider_avatar"}:
            tts_result = self.store.generate_tts_audio(
                run.run_id,
                TtsGenerateRequest(
                    stage_run_id=asset_stage_id,
                    segment_id="full_script",
                    text=_tts_text(spoken_script),
                    voice=payload.tts_voice,
                    filename="mvp-narration.wav",
                    asset_metadata={
                        "mode": payload.quality_mode,
                        "source_artifact_type": "spoken_script",
                    },
                ),
            )
            if tts_result is None:
                raise RuntimeError("TTS generation failed")
            _tts_invocation, narration_asset = tts_result
            external_provider_calls = True

        with tempfile.TemporaryDirectory(prefix="script2video-mvp-") as tmpdir:
            temp_root = Path(tmpdir)
            avatar_media = self.media_factory.render_avatar_preview(
                output_path=temp_root / "avatar-preview.mp4",
                duration_seconds=duration_seconds,
                width=payload.width,
                height=payload.height,
                fps=payload.fps,
                title=payload.title,
                brand_name=payload.brand_name,
                product_name=payload.product_name,
                segments=segments,
                shots=shot_list.get("shots") or [],
                reference_assets=reference_asset_entries,
            )
            avatar_asset = self._upload_generated_asset(
                run_id=run.run_id,
                asset_type="avatar_video_clip",
                filename="mvp-avatar-preview.mp4",
                media=avatar_media,
                stage_run_id=asset_stage_id,
                provider="internal",
                tool_key="local_mvp_avatar_preview",
                asset_metadata={
                    "mode": payload.quality_mode,
                    "source": "deterministic_ffmpeg_storyboard_preview",
                    "segment_count": len(segments),
                    "reference_asset_roles": sorted(uploaded_reference_assets),
                    "external_reference_count": len(external_reference_assets),
                },
            )

            music_asset = None
            if payload.include_music:
                if (
                    payload.quality_mode in {"provider_audio", "provider_avatar"}
                    and self.store.music_adapter.check_availability()
                ):
                    music_result = self.store.generate_music(
                        run.run_id,
                        MusicGenerateRequest(
                            stage_run_id=asset_stage_id,
                            prompt=payload.music_prompt or _music_prompt(shot_list),
                            music_length_ms=duration_seconds * 1000,
                            filename="mvp-provider-bgm.mp3",
                            asset_metadata={"mode": payload.quality_mode},
                        ),
                    )
                    if music_result is None:
                        raise RuntimeError("music generation failed")
                    _music_invocation, music_asset = music_result
                    external_provider_calls = True
                else:
                    music_media = self.media_factory.render_music_bed(
                        output_path=temp_root / "music-bed.wav",
                        duration_seconds=duration_seconds,
                    )
                    music_asset = self._upload_generated_asset(
                        run_id=run.run_id,
                        asset_type="background_music",
                        filename="mvp-music-bed.wav",
                        media=music_media,
                        stage_run_id=asset_stage_id,
                        provider="internal",
                        tool_key="local_mvp_music_bed",
                        asset_metadata={"mode": payload.quality_mode},
                    )

        avatar_job = None
        if payload.quality_mode == "provider_avatar":
            avatar_job = self._enqueue_provider_avatar_job(
                run_id=run.run_id,
                stage_run_id=asset_stage_id,
                payload=payload,
                narration_asset=narration_asset,
                uploaded_reference_assets=uploaded_reference_assets,
                external_reference_assets=external_reference_assets,
            )
            external_provider_calls = True

        asset_manifest = self._append_asset_manifest(
            run_id=run.run_id,
            stage_run_id=asset_stage_id,
            avatar_asset=avatar_asset,
            narration_asset=narration_asset,
            subtitle_asset=subtitle_asset,
            music_asset=music_asset,
            reference_assets=reference_asset_entries,
            duration_seconds=duration_seconds,
            external_provider_calls=external_provider_calls,
            mode=payload.quality_mode,
            avatar_job=avatar_job,
        )

        self._record_system_event(
            run.run_id,
            "mvp.assets_ready",
            "Local MVP preview assets are ready.",
            {
                "avatar_video_asset_id": avatar_asset.asset_id,
                "narration_audio_asset_id": narration_asset.asset_id if narration_asset else None,
                "subtitle_asset_id": subtitle_asset.asset_id,
                "music_asset_id": music_asset.asset_id if music_asset else None,
                "reference_asset_count": len(reference_asset_entries),
            },
        )

        run = self._approve("asset_review", run.run_id)
        stage_ids = {stage.stage_key: stage.stage_run_id for stage in run.stages}
        self._append_edit_plan(
            run_id=run.run_id,
            stage_run_id=stage_ids.get("edit"),
            avatar_asset=avatar_asset,
            narration_asset=narration_asset,
            subtitle_asset=subtitle_asset,
            music_asset=music_asset,
            reference_assets=reference_asset_entries,
            duration_seconds=duration_seconds,
            width=payload.width,
            height=payload.height,
            fps=payload.fps,
            mode=payload.quality_mode,
        )

        compose_result = self.store.compose_video(
            run.run_id,
            ComposeVideoRequest(
                stage_run_id=stage_ids.get("compose"),
                avatar_video_asset_id=avatar_asset.asset_id,
                narration_audio_asset_id=narration_asset.asset_id if narration_asset else None,
                subtitle_asset_id=subtitle_asset.asset_id,
                music_asset_id=music_asset.asset_id if music_asset else None,
                filename="mvp-final.mp4",
                width=payload.width,
                height=payload.height,
                fps=payload.fps,
                crf=payload.crf,
                preset=payload.preset,
                asset_metadata={"mode": payload.quality_mode},
            ),
        )
        if compose_result is None:
            raise RuntimeError("video composition failed")
        _compose_invocation, render_asset, render_summary = compose_result

        run = self._approve("final_review", run.run_id)
        access_link = self.store.create_asset_access_link(
            render_asset.asset_id,
            expires_in_seconds=payload.access_link_ttl_seconds,
        )
        if access_link is None:
            raise RuntimeError("rendered asset access link could not be created")

        publish_package = self.store.artifacts.append_artifact(
            run_id=run.run_id,
            stage_run_id=stage_ids.get("export"),
            stage_key="export",
            artifact_type="publish_package",
            payload={
                "version": "1.0",
                "mode": payload.quality_mode,
                "render_asset_id": render_asset.asset_id,
                "final_video_url": access_link.url,
                "workspace_url": f"/workspace?run_id={run.run_id}",
                "avatar_job": avatar_job.model_dump(mode="json") if avatar_job else None,
                "delivery": {
                    "platform": payload.target_platform,
                    "aspect_ratio": payload.aspect_ratio,
                    "width": payload.width,
                    "height": payload.height,
                    "duration_seconds": duration_seconds,
                },
            },
        )
        self.store.session.commit()
        self._record_system_event(
            run.run_id,
            "mvp.sample_completed",
            "Local MVP sample completed.",
            {"render_asset_id": render_asset.asset_id, "final_video_url": access_link.url},
        )

        final_run = self.store.get_run(run.run_id)
        if final_run is None or final_run.status != RunStatus.COMPLETED:
            raise RuntimeError("run did not complete")

        return MvpSampleResult(
            project=project,
            run=final_run,
            render_asset=render_asset,
            render_summary=render_summary,
            asset_manifest=asset_manifest,
            publish_package=publish_package,
            final_video_url=access_link.url,
            workspace_url=f"/workspace?run_id={run.run_id}",
            avatar_job=avatar_job,
            assets={
                "avatar_video": avatar_asset,
                "subtitle": subtitle_asset,
                **({"music": music_asset} if music_asset else {}),
                **({"narration_audio": narration_asset} if narration_asset else {}),
                **uploaded_reference_assets,
                "rendered_video": render_asset,
            },
        )

    def _approve(self, stage_key: str, run_id: str):
        run = self.store.review_stage(
            run_id,
            stage_key,
            ApprovalRequest(
                decision="approved",
                comment="Auto-approved by local MVP sample runner.",
            ),
        )
        if run is None:
            raise RuntimeError(f"could not approve stage: {stage_key}")
        return run

    def _collect_reference_assets(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        payload: MvpSampleRequest,
    ) -> tuple[dict[str, Asset], list[dict]]:
        uploaded_assets: dict[str, Asset] = {}
        external_references: list[dict] = []
        for role, reference in [
            ("person_image", payload.person_image),
            ("product_image", payload.product_image),
            ("brand_logo", payload.brand_logo),
        ]:
            if reference is None:
                continue
            if reference.content_base64:
                uploaded_assets[role] = self._upload_reference_asset(
                    run_id=run_id,
                    stage_run_id=stage_run_id,
                    role=role,
                    reference=reference,
                )
                continue
            if reference.url:
                external_references.append(
                    {
                        "role": role,
                        "source": "external_url",
                        "url": reference.url,
                        "filename": reference.filename,
                        "mime_type": reference.mime_type,
                        "description": reference.description,
                    }
                )
                continue
            raise ValueError(f"{role} requires either content_base64 or url")
        return uploaded_assets, external_references

    def _upload_reference_asset(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        role: str,
        reference: MvpReferenceAssetInput,
    ) -> Asset:
        filename = reference.filename or _default_reference_filename(role, reference.mime_type)
        asset = self.store.upload_asset(
            run_id,
            AssetUpload(
                asset_type="reference_image",
                filename=filename,
                content_base64=reference.content_base64 or "",
                stage_run_id=stage_run_id,
                provider="user_input",
                tool_key="mvp_reference_asset",
                mime_type=reference.mime_type,
                asset_metadata={
                    "mode": "mvp_reference",
                    "role": role,
                    "source": "inline_upload",
                    "description": reference.description,
                },
            ),
        )
        if asset is None:
            raise RuntimeError(f"{role} reference asset upload failed")
        return asset

    def _upload_generated_asset(
        self,
        *,
        run_id: str,
        asset_type: str,
        filename: str,
        media,
        stage_run_id: str | None,
        provider: str,
        tool_key: str,
        asset_metadata: dict,
    ) -> Asset:
        asset = self.store.upload_asset(
            run_id,
            AssetUpload(
                asset_type=asset_type,
                filename=filename,
                content_base64=base64.b64encode(media.content).decode("ascii"),
                stage_run_id=stage_run_id,
                provider=provider,
                tool_key=tool_key,
                mime_type=media.mime_type,
                duration_ms=media.duration_ms,
                width=media.width,
                height=media.height,
                asset_metadata={
                    **asset_metadata,
                    **media.metadata,
                    "ffmpeg_command": media.command,
                },
            ),
        )
        if asset is None:
            raise RuntimeError(f"{asset_type} asset upload failed")
        return asset

    def _enqueue_provider_avatar_job(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        payload: MvpSampleRequest,
        narration_asset: Asset | None,
        uploaded_reference_assets: dict[str, Asset],
        external_reference_assets: list[dict],
    ) -> BackgroundJob:
        if narration_asset is None:
            raise ValueError("provider_avatar requires provider narration audio")
        person_asset = uploaded_reference_assets.get("person_image")
        person_url = _external_reference_url(external_reference_assets, "person_image")
        if person_asset is None and person_url is None:
            raise ValueError("provider_avatar requires person_image as a URL or uploaded reference")
        source_audio_url = narration_asset.asset_metadata.get("source_audio_url")
        audio_url = source_audio_url if isinstance(source_audio_url, str) and source_audio_url else None
        job = self.store.enqueue_avatar_from_assets_job(
            run_id,
            AvatarVideoFromAssetsRequest(
                stage_run_id=stage_run_id,
                person_image_asset_id=person_asset.asset_id if person_asset else None,
                person_image_url=person_url,
                audio_asset_id=None if audio_url else narration_asset.asset_id,
                audio_url=audio_url,
                public_base_url=payload.public_base_url,
                access_link_ttl_seconds=payload.access_link_ttl_seconds,
                auto_archive=True,
                auto_compose=True,
                compose_width=payload.width,
                compose_height=payload.height,
                compose_fps=payload.fps,
                compose_crf=payload.crf,
                compose_preset=payload.preset,
                bgm_volume=0.18,
            ),
        )
        if job is None:
            raise RuntimeError("provider avatar job could not be queued")
        return job

    def _generate_provider_person_image(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        payload: MvpSampleRequest,
        spoken_script: dict,
        shot_list: dict,
    ) -> Asset:
        result = self.store.generate_image(
            run_id,
            ImageGenerateRequest(
                stage_run_id=stage_run_id,
                prompt=payload.avatar_image_prompt
                or _avatar_image_prompt(
                    payload=payload,
                    spoken_script=spoken_script,
                    shot_list=shot_list,
                ),
                model=payload.avatar_image_model,
                size=payload.avatar_image_size,
                filename="mvp-generated-person.png",
                negative_prompt=(
                    "text, watermark, logo, extra fingers, distorted face, hidden mouth, "
                    "side profile, low quality, blurry"
                ),
                asset_metadata={
                    "mode": payload.quality_mode,
                    "role": "person_image",
                    "source": "dashscope_generated_avatar_reference",
                    "description": "通义万相自动生成的数字人口播人物参考图",
                },
            ),
        )
        if result is None:
            raise RuntimeError("provider person image generation failed")
        _invocation, asset = result
        return asset

    def _append_asset_manifest(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        avatar_asset: Asset,
        narration_asset: Asset | None,
        subtitle_asset: Asset,
        music_asset: Asset | None,
        reference_assets: list[dict],
        duration_seconds: int,
        external_provider_calls: bool,
        mode: str,
        avatar_job: BackgroundJob | None,
    ):
        artifact = self.store.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key="asset_generation",
            artifact_type="asset_manifest",
            payload={
                "version": "1.0",
                "mode": mode,
                "external_provider_calls": external_provider_calls,
                "duration_seconds": duration_seconds,
                "reference_assets": reference_assets,
                "avatar_job": avatar_job.model_dump(mode="json") if avatar_job else None,
                "assets": [
                    {
                        "role": "avatar_video",
                        "asset_id": avatar_asset.asset_id,
                        "asset_type": avatar_asset.asset_type,
                    },
                    *(
                        [
                            {
                                "role": "narration_audio",
                                "asset_id": narration_asset.asset_id,
                                "asset_type": narration_asset.asset_type,
                            }
                        ]
                        if narration_asset
                        else []
                    ),
                    {
                        "role": "subtitle",
                        "asset_id": subtitle_asset.asset_id,
                        "asset_type": subtitle_asset.asset_type,
                    },
                    *(
                        [
                            {
                                "role": "music",
                                "asset_id": music_asset.asset_id,
                                "asset_type": music_asset.asset_type,
                            }
                        ]
                        if music_asset
                        else []
                    ),
                ],
            },
        )
        self.store.session.commit()
        return artifact

    def _append_edit_plan(
        self,
        *,
        run_id: str,
        stage_run_id: str | None,
        avatar_asset: Asset,
        narration_asset: Asset | None,
        subtitle_asset: Asset,
        music_asset: Asset | None,
        reference_assets: list[dict],
        duration_seconds: int,
        width: int,
        height: int,
        fps: int,
        mode: str,
    ) -> None:
        self.store.artifacts.append_artifact(
            run_id=run_id,
            stage_run_id=stage_run_id,
            stage_key="edit",
            artifact_type="edit_plan",
            payload={
                "version": "1.0",
                "mode": mode,
                "reference_assets": reference_assets,
                "timeline": [
                    {
                        "track": "avatar_video",
                        "asset_id": avatar_asset.asset_id,
                        "start_seconds": 0,
                        "end_seconds": duration_seconds,
                    },
                    *(
                        [
                            {
                                "track": "narration",
                                "asset_id": narration_asset.asset_id,
                                "start_seconds": 0,
                                "end_seconds": duration_seconds,
                                "mix_volume": 1.0,
                            }
                        ]
                        if narration_asset
                        else []
                    ),
                    {
                        "track": "subtitles",
                        "asset_id": subtitle_asset.asset_id,
                        "start_seconds": 0,
                        "end_seconds": duration_seconds,
                    },
                    *(
                        [
                            {
                                "track": "music",
                                "asset_id": music_asset.asset_id,
                                "start_seconds": 0,
                                "end_seconds": duration_seconds,
                                "mix_volume": 0.18,
                            }
                        ]
                        if music_asset
                        else []
                    ),
                ],
                "output": {"width": width, "height": height, "fps": fps, "format": "mp4"},
            },
        )
        self.store.session.commit()

    def _record_system_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        event_payload: dict,
    ) -> None:
        self.store.record_event(
            run_id,
            EventCreate(
                event_type=event_type,
                source_type=EventSourceType.SYSTEM,
                message=message,
                payload=event_payload,
            ),
        )


def _duration_seconds(spoken_script: dict, fallback: int) -> int:
    if spoken_script.get("total_duration_seconds"):
        return max(1, math.ceil(float(spoken_script["total_duration_seconds"])))
    segments = spoken_script.get("segments") or []
    ends = [float(segment.get("end_seconds") or 0) for segment in segments]
    return max(1, math.ceil(max(ends) if ends else fallback))


def _tts_text(spoken_script: dict) -> str:
    full_text = str(spoken_script.get("full_text") or "").strip()
    if not full_text:
        full_text = " ".join(
            str(segment.get("text") or segment.get("caption") or "").strip()
            for segment in spoken_script.get("segments") or []
            if str(segment.get("text") or segment.get("caption") or "").strip()
        )
    if len(full_text) <= 600:
        return full_text
    return full_text[:597].rstrip() + "..."


def _music_prompt(shot_list: dict) -> str:
    music_plan = shot_list.get("music_plan") or {}
    mood = music_plan.get("mood") or "warm upbeat Chinese short-video commercial"
    return (
        "A short instrumental background music bed for a Chinese vertical spokesperson ad. "
        f"Mood: {mood}. Warm, light, modern, unobtrusive under speech, no vocals."
    )


def _avatar_image_prompt(
    *,
    payload: MvpSampleRequest,
    spoken_script: dict,
    shot_list: dict,
) -> str:
    brand = payload.brand_name or "奶茶品牌"
    product = payload.product_name or "冲泡奶茶"
    script_hint = _tts_text(spoken_script)[:120]
    visual_style = shot_list.get("visual_style") or {}
    visual_tone = (
        visual_style.get("tone")
        if isinstance(visual_style, dict)
        else str(visual_style)
    ) or "warm cozy dorm room"
    return (
        "Photorealistic portrait for AI talking-avatar video, one Chinese female college "
        "student in a cozy dorm room, wearing a soft knit cardigan, seated at a desk, "
        "front-facing upper body, mouth clearly visible, natural friendly smile, holding "
        f"a cup of warm milk tea, autumn cozy mood, product context: {brand} {product}. "
        f"Scene tone: {visual_tone}. Script mood: {script_hint}. Clean background, "
        "balanced lighting, no text, no logo, no watermark, centered face, high detail."
    )


def _reference_entries_from_assets(assets_by_role: dict[str, Asset]) -> list[dict]:
    return [
        {
            "role": role,
            "source": "uploaded_asset",
            "asset_id": asset.asset_id,
            "asset_type": asset.asset_type,
            "mime_type": asset.mime_type,
            "filename": asset.asset_metadata.get("filename"),
            "description": asset.asset_metadata.get("description"),
        }
        for role, asset in assets_by_role.items()
    ]


def _external_reference_url(references: list[dict], role: str) -> str | None:
    for reference in references:
        if reference.get("role") == role and isinstance(reference.get("url"), str):
            return reference["url"]
    return None


def _default_reference_filename(role: str, mime_type: str | None) -> str:
    extension_by_mime = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }
    extension = extension_by_mime.get((mime_type or "").lower(), ".bin")
    return role.replace("_", "-") + extension
