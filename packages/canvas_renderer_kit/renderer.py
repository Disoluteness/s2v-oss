from __future__ import annotations

import hashlib
import html
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class HtmlCanvasRenderRequest(BaseModel):
    composition_graph: dict[str, Any]
    title: str = "Script2Video Canvas Preview"
    preview_time_seconds: float = Field(default=0.0, ge=0.0)
    include_debug_overlay: bool = True


class HtmlCanvasRenderResult(BaseModel):
    html: str
    report: dict[str, Any]
    validation: dict[str, Any]
    first_frame: dict[str, Any]


class HtmlCanvasRenderer:
    renderer_key = "html_canvas_renderer"
    provider_key = "local"

    def render(self, request: HtmlCanvasRenderRequest) -> HtmlCanvasRenderResult:
        graph = request.composition_graph
        validation = self.validate_graph(graph)
        first_frame = self.first_frame_plan(
            graph,
            preview_time_seconds=request.preview_time_seconds,
        )
        html_document = _html_document(
            graph=graph,
            title=request.title,
            validation=validation,
            first_frame=first_frame,
            include_debug_overlay=request.include_debug_overlay,
        )
        report = {
            "schema_version": "1.0",
            "generated_at": datetime.now(UTC).isoformat(),
            "renderer": self.renderer_key,
            "html_sha256": hashlib.sha256(html_document.encode("utf-8")).hexdigest(),
            "html_bytes": len(html_document.encode("utf-8")),
            "canvas": graph.get("canvas") or {},
            "render_profile": graph.get("render_profile") or {},
            "duration_seconds": graph.get("duration_seconds"),
            "track_count": len(graph.get("tracks") or []),
            "caption_count": len((graph.get("captions") or {}).get("segments") or []),
        }
        return HtmlCanvasRenderResult(
            html=html_document,
            report=report,
            validation=validation,
            first_frame=first_frame,
        )

    def validate_graph(self, graph: dict[str, Any]) -> dict[str, Any]:
        canvas = graph.get("canvas") or {}
        tracks = graph.get("tracks") or []
        render_targets = graph.get("render_targets") or []
        errors = []
        warnings = []
        if not _positive_int(canvas.get("width")) or not _positive_int(canvas.get("height")):
            errors.append(
                {
                    "code": "invalid_canvas_dimensions",
                    "message": "Canvas width and height must be positive integers.",
                }
            )
        if not tracks:
            errors.append(
                {
                    "code": "empty_track_list",
                    "message": "Composition graph must contain at least one track.",
                }
            )
        if not any(track.get("type") in {"video", "caption"} for track in tracks):
            warnings.append(
                {
                    "code": "no_visual_track",
                    "message": "No video or caption track is present for the canvas preview.",
                }
            )
        if not any(target.get("runtime") == "html_canvas" for target in render_targets):
            warnings.append(
                {
                    "code": "html_canvas_target_missing",
                    "message": "Composition graph does not declare an html_canvas render target.",
                }
            )
        graph_validation = graph.get("validation") or {}
        for issue in graph_validation.get("missing_inputs") or []:
            warnings.append(
                {
                    "code": "graph_missing_input",
                    "message": issue.get("message") or "Composition graph has missing inputs.",
                    "input": issue.get("input"),
                }
            )
        return {
            "ready_for_preview": not errors,
            "errors": errors,
            "warnings": warnings,
            "checks": {
                "canvas_dimensions": not any(
                    error["code"] == "invalid_canvas_dimensions" for error in errors
                ),
                "tracks_present": bool(tracks),
                "html_canvas_target": any(
                    target.get("runtime") == "html_canvas" for target in render_targets
                ),
            },
        }

    def first_frame_plan(
        self,
        graph: dict[str, Any],
        *,
        preview_time_seconds: float,
    ) -> dict[str, Any]:
        canvas = graph.get("canvas") or {}
        background = canvas.get("background") or {}
        commands = [
            {
                "op": "fill_rect",
                "target": "canvas",
                "color": background.get("color") or "#000000",
                "x": 0,
                "y": 0,
                "width": canvas.get("width"),
                "height": canvas.get("height"),
            }
        ]
        if _track_by_id(graph, "track_video_primary") is not None:
            commands.append(
                {
                    "op": "draw_video_placeholder",
                    "target": "track_video_primary",
                    "fit": "contain",
                    "label": "Primary video",
                }
            )
        caption = _caption_at(graph, preview_time_seconds)
        if caption:
            commands.append(
                {
                    "op": "draw_caption",
                    "target": "track_caption_main",
                    "text": caption.get("text"),
                    "start_seconds": caption.get("start_seconds"),
                    "end_seconds": caption.get("end_seconds"),
                }
            )
        commands.append(
            {
                "op": "draw_progress",
                "target": "timeline",
                "time_seconds": preview_time_seconds,
                "duration_seconds": graph.get("duration_seconds"),
            }
        )
        return {
            "time_seconds": preview_time_seconds,
            "command_count": len(commands),
            "commands": commands,
        }


def _html_document(
    *,
    graph: dict[str, Any],
    title: str,
    validation: dict[str, Any],
    first_frame: dict[str, Any],
    include_debug_overlay: bool,
) -> str:
    canvas = graph.get("canvas") or {}
    width = int(canvas.get("width") or 1080)
    height = int(canvas.get("height") or 1920)
    graph_json = _safe_json(graph)
    validation_json = _safe_json(validation)
    first_frame_json = _safe_json(first_frame)
    title_text = html.escape(title)
    debug_html = ""
    if include_debug_overlay:
        validation_label = html.escape(
            "ready" if validation.get("ready_for_preview") else "warnings"
        )
        render_profile = graph.get("render_profile") or {}
        profile_label = html.escape(
            render_profile.get("display_name")
            or render_profile.get("profile_key")
            or "未指定规格"
        )
        debug_html = (
            '<aside class="debug">'
            "HTML/Canvas preview · self-contained · validation: "
            f"{validation_label} · profile: {profile_label}</aside>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title_text}</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: -apple-system, BlinkMacSystemFont, "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
      background: #111;
      color: #fff;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #111;
    }}
    .stage {{
      position: relative;
      width: min(100vw, calc(100vh * {width} / {height}));
      aspect-ratio: {width} / {height};
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
      background: #000;
    }}
    canvas {{
      width: 100%;
      height: 100%;
      display: block;
    }}
    .debug {{
      position: fixed;
      left: 12px;
      bottom: 12px;
      max-width: min(560px, calc(100vw - 24px));
      padding: 10px 12px;
      border-radius: 8px;
      background: rgba(0, 0, 0, 0.66);
      font-size: 12px;
      line-height: 1.45;
      color: #f4f7fb;
      backdrop-filter: blur(10px);
    }}
  </style>
</head>
<body>
  <main class="stage" aria-label="Script2Video HTML Canvas preview">
    <canvas id="s2v-canvas" width="{width}" height="{height}"></canvas>
  </main>
  <script id="composition-graph" type="application/json">{graph_json}</script>
  <script id="render-validation" type="application/json">{validation_json}</script>
  <script id="first-frame-plan" type="application/json">{first_frame_json}</script>
  <script>
    const graph = JSON.parse(document.getElementById("composition-graph").textContent);
    const validation = JSON.parse(document.getElementById("render-validation").textContent);
    const canvas = document.getElementById("s2v-canvas");
    const ctx = canvas.getContext("2d");
    const duration = Math.max(0.001, Number(graph.duration_seconds || 1));
    const startedAt = performance.now();

    function captionAt(time) {{
      const captions = graph.captions && Array.isArray(graph.captions.segments)
        ? graph.captions.segments
        : [];
      return captions.find((caption) => (
        time >= Number(caption.start_seconds || 0) &&
        time <= Number(caption.end_seconds || 0)
      ));
    }}

    function drawRoundedRect(x, y, width, height, radius) {{
      const r = Math.max(0, Math.min(radius, width / 2, height / 2));
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + width, y, x + width, y + height, r);
      ctx.arcTo(x + width, y + height, x, y + height, r);
      ctx.arcTo(x, y + height, x, y, r);
      ctx.arcTo(x, y, x + width, y, r);
      ctx.closePath();
    }}

    function drawFrame(now) {{
      const elapsed = ((now - startedAt) / 1000) % duration;
      const progress = elapsed / duration;
      const background = graph.canvas && graph.canvas.background ? graph.canvas.background : {{}};
      const style = graph.canvas && graph.canvas.style ? graph.canvas.style : {{}};
      const colors = style.brand_colors || {{}};
      const captionStyle = style.caption_style || {{}};
      const safeArea = graph.canvas && graph.canvas.safe_area ? graph.canvas.safe_area : {{}};

      ctx.fillStyle = background.color || colors.primary || "#000000";
      ctx.fillRect(0, 0, canvas.width, canvas.height);

      const marginX = Math.max(32, Math.round(canvas.width * 0.06));
      const marginY = Math.max(48, Math.round(canvas.height * 0.06));
      const videoX = marginX;
      const videoY = marginY;
      const videoW = canvas.width - marginX * 2;
      const videoH = canvas.height - marginY * 2;
      ctx.save();
      ctx.globalAlpha = 0.88;
      ctx.fillStyle = "rgba(255, 255, 255, 0.08)";
      drawRoundedRect(videoX, videoY, videoW, videoH, 24);
      ctx.fill();
      ctx.strokeStyle = "rgba(255, 255, 255, 0.24)";
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = "rgba(255, 255, 255, 0.74)";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = `${{Math.max(22, Math.round(canvas.width * 0.036))}}px sans-serif`;
      ctx.fillText("Primary video layer", canvas.width / 2, canvas.height / 2);
      ctx.restore();

      const caption = captionAt(elapsed);
      if (caption && caption.text) {{
        const fontSize = Number(captionStyle.font_size || Math.max(36, Math.round(canvas.width * 0.052)));
        const bottom = Number((safeArea && safeArea.bottom) || Math.round(canvas.height * 0.1));
        const text = String(caption.text);
        ctx.font = `700 ${{fontSize}}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const maxTextWidth = canvas.width - marginX * 2;
        const lines = wrapText(ctx, text, maxTextWidth);
        const lineHeight = fontSize * 1.25;
        const boxH = lines.length * lineHeight + 36;
        const boxW = Math.min(canvas.width - marginX, Math.max(...lines.map((line) => ctx.measureText(line).width)) + 64);
        const boxX = (canvas.width - boxW) / 2;
        const boxY = canvas.height - bottom - boxH;
        ctx.fillStyle = "rgba(0, 0, 0, 0.62)";
        drawRoundedRect(boxX, boxY, boxW, boxH, 18);
        ctx.fill();
        ctx.fillStyle = captionStyle.color || "#ffffff";
        lines.forEach((line, index) => {{
          ctx.fillText(line, canvas.width / 2, boxY + 24 + lineHeight * index + lineHeight / 2);
        }});
      }}

      ctx.fillStyle = "rgba(255, 255, 255, 0.26)";
      ctx.fillRect(marginX, canvas.height - 28, canvas.width - marginX * 2, 8);
      ctx.fillStyle = colors.secondary || "#f7c948";
      ctx.fillRect(marginX, canvas.height - 28, (canvas.width - marginX * 2) * progress, 8);

      if ({str(include_debug_overlay).lower()} && !validation.ready_for_preview) {{
        ctx.fillStyle = "rgba(255, 82, 82, 0.9)";
        ctx.font = "24px sans-serif";
        ctx.textAlign = "left";
        ctx.fillText("Preview validation warning", 24, 42);
      }}

      requestAnimationFrame(drawFrame);
    }}

    function wrapText(context, text, maxWidth) {{
      const chars = Array.from(text);
      const lines = [];
      let line = "";
      for (const char of chars) {{
        const test = line + char;
        if (context.measureText(test).width > maxWidth && line) {{
          lines.push(line);
          line = char;
        }} else {{
          line = test;
        }}
      }}
      if (line) lines.push(line);
      return lines.slice(0, 3);
    }}

    requestAnimationFrame(drawFrame);
  </script>
  {debug_html}
</body>
</html>
"""


def _safe_json(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        serialized.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("</", "<\\/")
    )


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and value > 0


def _track_by_id(graph: dict[str, Any], track_id: str) -> dict[str, Any] | None:
    return next(
        (
            track
            for track in graph.get("tracks") or []
            if isinstance(track, dict) and track.get("track_id") == track_id
        ),
        None,
    )


def _caption_at(graph: dict[str, Any], time_seconds: float) -> dict[str, Any] | None:
    captions = (graph.get("captions") or {}).get("segments") or []
    return next(
        (
            caption
            for caption in captions
            if float(caption.get("start_seconds") or 0)
            <= time_seconds
            <= float(caption.get("end_seconds") or 0)
        ),
        captions[0] if captions else None,
    )
