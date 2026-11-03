#!/usr/bin/env python3
"""s2v.py — 驱动 Script2Video 流水线的命令行客户端。

只用标准库，把一个口播稿一路推到成片，并在任意一步回退重做。

配置（优先级从高到低）：
  --base-url / --api-key  命令行参数
  S2V_BASE_URL / S2V_API_KEY  环境变量
  ~/.s2v/state.json       上一次 init / config 记住的地址与 run
  默认 http://127.0.0.1:18080

解压后先跑 `s2v.py config --base-url http://<服务地址>:18080`，之后所有命令不必再带地址。

用法：python3 s2v.py --help
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

STATE_PATH = Path(os.environ.get("S2V_STATE", "~/.s2v/state.json")).expanduser()
DEFAULT_BASE = "http://127.0.0.1:18080"


# ---------------------------------------------------------------- 基础设施


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(**updates) -> None:
    state = load_state()
    state.update({k: v for k, v in updates.items() if v is not None})
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, payload: object):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload


def _is_internal_host(host: str) -> bool:
    """内网地址不走代理：服务常部署在 10.x / 127.0.0.1，走了代理就是 502。"""
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _build_opener(base_url: str):
    host = urllib.parse.urlsplit(base_url).hostname or ""
    if _is_internal_host(host) or os.environ.get("S2V_NO_PROXY"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


class Client:
    def __init__(self, base_url: str, api_key: str | None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._opener = _build_opener(self.base_url)

    def _headers(self, extra: dict | None = None) -> dict:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(extra or {})
        return headers

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        idempotency_key: str | None = None,
    ):
        url = self.base_url + urllib.parse.quote(path, safe="/?&=:%,")
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers(headers))
        try:
            with self._opener.open(req, timeout=180) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"detail": raw}
            detail = payload.get("detail")
            if isinstance(detail, dict):
                code = str(detail.get("code") or "error")
                message = str(detail.get("message") or detail)
            else:
                code = str(exc.code)
                message = str(detail or raw)
            raise ApiError(exc.code, code, message, payload) from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"连不上 {self.base_url}：{exc.reason}\n先确认服务在跑，或用 --base-url 指定地址。")

    def download(self, asset_id: str) -> tuple[bytes, str]:
        url = f"{self.base_url}/api/assets/{asset_id}/download"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with self._opener.open(req, timeout=180) as resp:
                return resp.read(), resp.headers.get("Content-Type", "application/octet-stream")
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, "download_failed", exc.read().decode("utf-8", "replace"), None) from exc


# ---------------------------------------------------------------- 输出辅助


def emit(data, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    print(json.dumps(data, ensure_ascii=False, indent=1))


def short(value: object, limit: int = 90) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def pick_run(args, state=None) -> str:
    state = state or load_state()
    run_id = getattr(args, "run_id", None) or state.get("run_id")
    if not run_id:
        raise SystemExit("没有指定 run。先跑 `s2v.py init --script ...`，或加 --run-id。")
    return run_id


# ---------------------------------------------------------------- 业务封装


def latest_asset(assets: list[dict], *asset_types: str) -> dict | None:
    """同一类型可能有多个版本（重做会追加新的），取最后创建的那个。"""
    wanted = [t for t in asset_types if t]
    matches = [a for a in assets if a.get("asset_type") in wanted]
    return matches[-1] if matches else None


def open_gates(client: Client, run_id: str) -> list[dict]:
    requests = client.request("GET", f"/api/runs/{run_id}/review-requests") or []
    return [r for r in requests if r.get("status") == "open"]


def spoken_script_text(client: Client, run_id: str) -> str | None:
    artifacts = client.request("GET", f"/api/runs/{run_id}/artifacts") or []
    scripts = [a for a in artifacts if a.get("artifact_type") == "spoken_script"]
    if not scripts:
        return None
    payload = scripts[-1].get("payload") or {}
    return payload.get("full_text") or payload.get("text") or None


def summarize_run(client: Client, run_id: str) -> dict:
    run = client.request("GET", f"/api/runs/{run_id}")
    jobs = client.request("GET", f"/api/runs/{run_id}/jobs") or []
    assets = client.request("GET", f"/api/runs/{run_id}/assets") or []
    gates = open_gates(client, run_id)
    return {"run": run, "jobs": jobs, "assets": assets, "gates": gates}


def print_status(data: dict) -> None:
    run = data["run"]
    print(f"run      {run['run_id']}")
    print(f"pipeline {run.get('pipeline_key')}    status={run.get('status')}    stage={run.get('current_stage_key')}")
    stages = run.get("stages") or []
    if stages:
        print("阶段     " + " | ".join(f"{s.get('stage_key')}:{s.get('status')}" for s in stages))
    for gate in data["gates"]:
        print(
            f"待审关口 {gate.get('review_request_id')}  stage={gate.get('stage_key')}"
            f"  round={gate.get('iteration')}  逾期={gate.get('overdue')}"
        )
    for job in data["jobs"][-5:]:
        extra = ""
        if job.get("error_code"):
            extra = f"  error={job['error_code']}: {short(job.get('error_message'), 60)}"
        print(f"作业     {job.get('job_id')}  {job.get('job_type')}  {job.get('status')}  {job.get('progress')}%{extra}")
    if data["assets"]:
        print("产物")
        for asset in data["assets"][-8:]:
            meta = asset.get("asset_metadata") or {}
            note = meta.get("description") or meta.get("role") or ""
            size = asset.get("file_size")
            size_text = f"{size / 1024:.0f}KB" if size else "-"
            print(
                f"  {asset.get('asset_id')}  {asset.get('asset_type'):<16} {asset.get('mime_type') or '-':<12}"
                f" {size_text:>8}  {short(note, 40)}"
            )
    print()
    print("下一步   " + next_hint(data))


def next_hint(data: dict) -> str:
    run = data["run"]
    status = run.get("status")
    if data["gates"]:
        gate = data["gates"][0]
        return (
            f"关口 {gate['review_request_id']}（{gate.get('stage_key')}）待决策："
            f"`s2v.py decide {gate['review_request_id']} --decision approve`"
            f" 或 `--decision request_changes --comment '要改什么'`"
        )
    if status in {"draft"}:
        return "run 还没启动：`s2v.py preflight` 看能力，然后 `s2v.py start`"
    if any(j.get("status") in {"queued", "running", "waiting_provider"} for j in data["jobs"]):
        job = next(j for j in data["jobs"] if j.get("status") in {"queued", "running", "waiting_provider"})
        return f"作业 {job['job_id']} 在跑（{job['status']}）：`s2v.py job {job['job_id']} --process` 催一次，或等 worker"
    if status == "completed":
        rendered = latest_asset(data["assets"], "rendered_video")
        if rendered:
            return f"成片 {rendered['asset_id']} 已就绪：`s2v.py fetch {rendered['asset_id']} --out ./out.mp4`"
        return "run 已完成，但没有渲染产物：`s2v.py assets` 看看有什么，必要时 `s2v.py compose`"
    if status in {"failed", "cancelled"}:
        return "run 已终止：`s2v.py jobs` 看 error_code，修好后 `s2v.py rerun --stage <stage_key>`"
    return "`s2v.py status` 再看一次"


# ---------------------------------------------------------------- 子命令


def cmd_init(args) -> None:
    client = args.client
    idem = args.idempotency_key
    project = client.request(
        "POST",
        "/api/projects",
        {
            "title": args.title,
            "description": args.description,
            "default_language": args.language,
            "default_aspect_ratio": args.aspect,
        },
        idempotency_key=f"{idem}:project" if idem else None,
    )
    payload = {
        "raw_script": args.script,
        "pipeline_key": args.pipeline,
        "target_platform": args.platform,
        "target_duration_seconds": args.duration,
        "aspect_ratio": args.aspect,
    }
    for key, value in (
        ("brand_name", args.brand),
        ("product_name", args.product),
        ("quality_mode", args.quality_mode),
        ("cost_budget", args.budget),
        ("style_key", args.style),
    ):
        if value is not None:
            payload[key] = value
    run = client.request(
        "POST",
        f"/api/projects/{project['project_id']}/runs",
        payload,
        idempotency_key=f"{idem}:run" if idem else None,
    )
    save_state(base_url=client.base_url, project_id=project["project_id"], run_id=run["run_id"])
    if args.json:
        emit({"project": project, "run": run}, True)
        return
    print(f"project_id {project['project_id']}")
    print(f"run_id     {run['run_id']}   status={run.get('status')}   pipeline={run.get('pipeline_key')}")
    print()
    print("下一步   `s2v.py preflight` 看这个环境能不能兑现你要的档次，然后 `s2v.py start`")


def cmd_status(args) -> None:
    data = summarize_run(args.client, pick_run(args))
    if args.json:
        emit(data, True)
        return
    print_status(data)


def cmd_preflight(args) -> None:
    run_id = pick_run(args)
    report = args.client.request("POST", f"/api/runs/{run_id}/preflight")
    plan_artifact = args.client.request("POST", f"/api/runs/{run_id}/execution-plan")
    plan = plan_artifact.get("payload") or plan_artifact
    if args.json:
        emit({"preflight": report, "execution_plan": plan_artifact}, True)
        return
    payload = report.get("payload") or report
    print("预检")
    for stage in payload.get("stages", [])[:12]:
        print(f"  {stage.get('stage_key'):<18} {stage.get('status') or stage.get('decision')}")
    print()
    print("执行计划")
    print(f"  execution_policy      {plan.get('execution_policy')}")
    print(f"  can_execute           {plan.get('can_execute')}")
    downgrades = plan.get("intent_downgrades") or []
    print(f"  intent_downgrades     {len(downgrades)}")
    reservation = plan.get("cost_reservation") or {}
    print(
        "  成本预留              "
        f"{reservation.get('reserved_cost', reservation.get('amount'))} "
        f"{reservation.get('currency') or ''}  status={reservation.get('status')}"
    )
    blocking = plan.get("blocking_issues") or []
    print(f"  blocking_issues       {len(blocking)}")
    for item in blocking[:6]:
        print(f"    · {short(item, 160)}")
    if downgrades:
        print("  降级明细：")
        for item in downgrades:
            print(f"    · {item.get('stage_key')}/{item.get('capability')} → {item.get('selected_tool_key')}")
    print()
    print("注意   预检与执行计划都会各追加一个产物版本，别放进轮询循环。")


def cmd_start(args) -> None:
    run_id = pick_run(args)
    payload = {"accept_intent_downgrade": bool(args.accept_downgrade)}
    run = args.client.request("POST", f"/api/runs/{run_id}/start", payload)
    if args.json:
        emit(run, True)
        return
    print(f"status={run.get('status')}  stage={run.get('current_stage_key')}")
    if run.get("status") == "waiting_review":
        print()
        print("停在第一个审核关口，这是设计（不是失败）。")
        print("下一步   `s2v.py gates` 看要审什么")


def cmd_gates(args) -> None:
    run_id = pick_run(args)
    gates = open_gates(args.client, run_id)
    if args.json:
        emit(gates, True)
        return
    if not gates:
        print("没有 open 的关口。")
        print(next_hint(summarize_run(args.client, run_id)))
        return
    for gate in gates:
        print(
            f"{gate.get('review_request_id')}  stage={gate.get('stage_key')}  round={gate.get('iteration')}"
            f"  overdue={gate.get('overdue')}  assignee={gate.get('assignee_user_id')}"
        )


def cmd_gate(args) -> None:
    detail = args.client.request("GET", f"/api/review-requests/{args.review_request_id}")
    if args.json:
        emit(detail, True)
        return
    print(f"stage       {detail.get('stage_key')}   gate={detail.get('gate_type')}   status={detail.get('status')}")
    print(f"本轮        round={detail.get('iteration')}")
    print("关注点      " + ", ".join(detail.get("review_focus") or ["（无）"]))
    print("通过标准    " + "; ".join(detail.get("success_criteria") or ["（无）"]))
    scope = detail.get("artifact_scope") or []
    print("被审产物")
    for item in scope:
        print(f"  {item.get('artifact_id')}  {item.get('artifact_type')}  v{item.get('version')}")
    print()
    if scope:
        art = args.client.request("GET", f"/api/runs/{detail.get('run_id')}/artifacts") or []
        wanted = {item.get("artifact_id") for item in scope}
        for artifact in art:
            if artifact.get("artifact_id") in wanted:
                body = json.dumps(artifact.get("payload"), ensure_ascii=False, indent=1)
                print(body[:2000])
                print()
    print(
        "决策     `s2v.py decide "
        f"{args.review_request_id} --decision approve` / `--decision request_changes --comment '...'`"
    )


def cmd_decide(args) -> None:
    body = {
        "decision": args.decision,
        "comment": args.comment,
        "reason_code": args.reason_code,
    }
    if args.artifact_id:
        body["artifact_refs"] = [{"artifact_id": args.artifact_id}]
    result = args.client.request(
        "POST", f"/api/review-requests/{args.review_request_id}/decisions", body
    )
    if args.json:
        emit(result, True)
        return
    print(
        f"decision={result.get('current_decision')}  review_status={result.get('status')}"
        f"  stage={result.get('stage_key')}  下一关={result.get('gates_stage_key')}"
    )
    run_id = result.get("run_id")
    if run_id:
        print()
        print_status(summarize_run(args.client, run_id))


def cmd_quick_approve(args) -> None:
    run_id = pick_run(args)
    gates = open_gates(args.client, run_id)
    if not gates:
        raise SystemExit("没有 open 的关口。")
    gate = gates[0]
    args.review_request_id = gate["review_request_id"]
    cmd_decide(args)


def cmd_assets(args) -> None:
    run_id = pick_run(args)
    assets = args.client.request("GET", f"/api/runs/{run_id}/assets") or []
    if args.json:
        emit(assets, True)
        return
    if not assets:
        print("还没有产物。")
        return
    for asset in assets:
        print(
            f"{asset.get('asset_id')}  {asset.get('asset_type'):<18} {asset.get('mime_type') or '-':<14}"
            f" {asset.get('file_size') or 0:>9}B  {short(asset.get('storage_url'), 60)}"
        )


def cmd_jobs(args) -> None:
    run_id = pick_run(args)
    jobs = args.client.request("GET", f"/api/runs/{run_id}/jobs") or []
    if args.json:
        emit(jobs, True)
        return
    if not jobs:
        print("还没有作业。")
        return
    for job in jobs:
        print(f"{job.get('job_id')}  {job.get('job_type'):<26} {job.get('status'):<16} {job.get('progress')}%")
        if job.get("error_code"):
            print(f"    error {job['error_code']}  retryable={job.get('error_retryable')}")
            print(f"    {short(job.get('error_message'), 200)}")
            if job.get("error_remediation"):
                print(f"    修法 {short(job['error_remediation'], 200)}")


def cmd_job(args) -> None:
    if args.process:
        result = args.client.request(
            "POST",
            f"/api/runs/{pick_run(args)}/manifest-actions",
            {"action": "process_job", "job_id": args.job_id},
        )
        if args.json:
            emit(result, True)
            return
        print(result.get("message"))
    job = args.client.request("GET", f"/api/jobs/{args.job_id}")
    if args.json:
        emit(job, True)
        return
    print(f"{job.get('job_id')}  {job.get('job_type')}  {job.get('status')}  {job.get('progress')}%")
    if job.get("error_code"):
        print(f"error {job['error_code']}  retryable={job.get('error_retryable')}")
        print(short(job.get("error_message"), 400))
    if job.get("result_payload"):
        print(json.dumps(job["result_payload"], ensure_ascii=False, indent=1)[:1200])


def cmd_image(args) -> None:
    run_id = pick_run(args)
    body = {
        "prompt": args.prompt,
        "filename": args.filename or "person.png",
        "negative_prompt": args.negative,
        "watermark": False,
    }
    if args.size:
        body["size"] = args.size
    result = args.client.request("POST", f"/api/runs/{run_id}/images", body)
    asset = result["asset"]
    if args.json:
        emit(result, True)
        return
    print(f"asset_id {asset['asset_id']}  {asset.get('width')}x{asset.get('height')}  {asset.get('file_size')}B")
    print(f"下一步   看效果：`s2v.py fetch {asset['asset_id']} --out ./person.png`")


def cmd_tts(args) -> None:
    run_id = pick_run(args)
    text = args.text or spoken_script_text(args.client, run_id)
    if not text:
        raise SystemExit("没有文本：传 --text，或先让 script 阶段产出 spoken_script。")
    result = args.client.request(
        "POST",
        f"/api/runs/{run_id}/tts",
        {"text": text, "voice": args.voice, "filename": args.filename or "narration.wav"},
    )
    asset = result["asset"]
    if args.json:
        emit(result, True)
        return
    duration = (asset.get("duration_ms") or 0) / 1000
    print(f"asset_id {asset['asset_id']}  时长 {duration:.1f}s  {asset.get('file_size')}B")
    if duration >= 20:
        print("⚠️  数字人上游要求音频 <20 秒，这条超了：会被 wan2.2-s2v 拒绝，需要切段。")


def cmd_upload(args) -> None:
    run_id = pick_run(args)
    path = Path(args.file).expanduser()
    content = base64.b64encode(path.read_bytes()).decode()
    mime = args.mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = {
        "asset_type": args.asset_type,
        "filename": path.name,
        "content_base64": content,
        "mime_type": mime,
    }
    if args.duration_ms:
        body["duration_ms"] = args.duration_ms
    asset = args.client.request("POST", f"/api/runs/{run_id}/assets", body)
    if args.json:
        emit(asset, True)
        return
    print(f"asset_id {asset['asset_id']}  {asset.get('asset_type')}  {asset.get('mime_type')}  {asset.get('file_size')}B")


def cmd_avatar(args) -> None:
    run_id = pick_run(args)
    assets = args.client.request("GET", f"/api/runs/{run_id}/assets") or []
    person = args.person or (latest_asset(assets, "person_image") or {}).get("asset_id")
    audio = args.audio or (latest_asset(assets, "tts_audio", "narration_audio") or {}).get("asset_id")
    if not person and not args.person_url:
        raise SystemExit("没有人物图：`s2v.py image --prompt ...` 或 `s2v.py upload --asset-type person_image --file x.png`")
    if not audio and not args.audio_url:
        raise SystemExit("没有口播音频：`s2v.py tts`（没有文本时先跑 script 阶段）")
    body = {
        "model": args.model,
        "auto_archive": True,
        "auto_compose": bool(args.auto_compose),
    }
    if args.person or person:
        body["person_image_asset_id"] = args.person or person
    if args.person_url:
        body["person_image_url"] = args.person_url
    if args.audio or audio:
        body["audio_asset_id"] = args.audio or audio
    if args.audio_url:
        body["audio_url"] = args.audio_url
    job = args.client.request("POST", f"/api/runs/{run_id}/jobs/avatar-from-assets", body)
    if args.json:
        emit(job, True)
        return
    print(f"job_id {job['job_id']}  {job['job_type']}  {job['status']}")
    if args.auto_compose:
        print("已要求自动合成：数字人出片后会自动跑一次 compose。")
    print("注意   同一个账号同时只能有 1 个数字人任务；端到端 5~15 分钟属正常。")


def cmd_compose(args) -> None:
    run_id = pick_run(args)
    assets = args.client.request("GET", f"/api/runs/{run_id}/assets") or []
    avatar = args.avatar or (latest_asset(assets, "avatar_video_clip", "avatar_video") or {}).get("asset_id")
    audio = args.audio or (latest_asset(assets, "tts_audio", "narration_audio") or {}).get("asset_id")
    if not avatar:
        raise SystemExit("没有数字人视频片段：先 `s2v.py avatar`。")
    body = {"avatar_video_asset_id": avatar, "narration_audio_asset_id": audio}
    if args.subtitle:
        body["subtitle_asset_id"] = args.subtitle
    if args.music:
        body["music_asset_id"] = args.music
    if args.profile:
        body["render_profile_key"] = args.profile
    result = args.client.request("POST", f"/api/runs/{run_id}/compose", body)
    asset = result["asset"]
    if args.json:
        emit(result, True)
        return
    print(f"asset_id {asset['asset_id']}  {asset.get('width')}x{asset.get('height')}  {asset.get('duration_ms')}ms")
    print(f"下一步   `s2v.py fetch {asset['asset_id']} --out ./out.mp4`")


def cmd_dispatch(args) -> None:
    run_id = pick_run(args)
    result = args.client.request(
        "POST", f"/api/runs/{run_id}/manifest-actions", {"action": "dispatch_ready_steps"}
    )
    payload = result.get("result_payload") or {}
    jobs = payload.get("jobs") or []
    if args.json:
        emit(result, True)
        return
    print(result.get("message"))
    print(f"派发作业 {len(jobs)} 个")
    for job in jobs:
        print(f"  {job.get('job_id')}  {job.get('job_type')}  {job.get('status')}")
    for blocker in payload.get("blockers") or []:
        print(f"阻塞 · {blocker}")


def cmd_rerun(args) -> None:
    run_id = pick_run(args)
    if not args.stage and not args.artifact:
        raise SystemExit("要么 --stage plan，要么 --artifact spoken_script")
    body = {"action": "rerun_stage" if args.stage else "rerun_artifact", "reason": args.reason}
    if args.stage:
        body["stage_key"] = args.stage
    else:
        body["artifact_type"] = args.artifact
    result = args.client.request("POST", f"/api/runs/{run_id}/state-actions", body)
    if args.json:
        emit(result, True)
        return
    print(result.get("message"))
    payload = result.get("result_payload") or {}
    print(f"重跑阶段   {payload.get('affected_stage_keys')}")
    print(f"下游失效   {payload.get('invalidated_stage_keys')}")
    print(f"作废的审核 {payload.get('superseded_review_request_ids')}")
    print()
    run = result.get("run") or {}
    print(f"status={run.get('status')}  stage={run.get('current_stage_key')}")
    print("注意   下游已通过的关口会重新开出（轮次 +1），旧的 review_request_id 作废，别复用。")


def cmd_retry(args) -> None:
    run_id = pick_run(args)
    result = args.client.request(
        "POST", f"/api/runs/{run_id}/manifest-actions", {"action": "retry_job", "job_id": args.job_id}
    )
    print(json.dumps(result, ensure_ascii=False, indent=1) if args.json else result.get("message"))


def cmd_fetch(args) -> None:
    content, mime = args.client.download(args.asset_id)
    suffix = mimetypes.guess_extension(mime.split(";")[0]) or ".bin"
    out = Path(args.out).expanduser() if args.out else Path(f"/tmp/{args.asset_id}{suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(content)
    print(str(out))


def cmd_link(args) -> None:
    query = ""
    if args.ttl:
        query = f"?expires_in_seconds={args.ttl}"
    result = args.client.request("GET", f"/api/assets/{args.asset_id}/access-link{query}")
    if args.json:
        emit(result, True)
        return
    print(result.get("url"))
    print(f"expires_at {result.get('expires_at')}")


def cmd_artifacts(args) -> None:
    run_id = pick_run(args)
    artifacts = args.client.request("GET", f"/api/runs/{run_id}/artifacts") or []
    if args.type:
        artifacts = [a for a in artifacts if a.get("artifact_type") == args.type]
    for artifact in artifacts:
        head = (
            f"{artifact.get('artifact_type'):<22} v{artifact.get('version')}"
            f"  stage={artifact.get('stage_key'):<16} current={artifact.get('is_current')}"
        )
        print(head)
        if args.show:
            print(json.dumps(artifact.get("payload"), ensure_ascii=False, indent=1)[:2000])


def cmd_api(args) -> None:
    body = json.loads(args.body) if args.body else None
    result = args.client.request(args.method.upper(), args.path, body)
    print(json.dumps(result, ensure_ascii=False, indent=1))


def cmd_config(args) -> None:
    """记住服务地址并探活。解压后先跑这一条，之后所有命令都不必再带 --base-url。"""
    saved = os.environ.get("S2V_BASE_URL")
    if args.base_url:
        save_state(base_url=args.client.base_url)
    state = load_state()
    if args.base_url:
        source = "命令行 --base-url（已写入状态文件）"
    elif saved:
        source = "环境变量 S2V_BASE_URL（每次都盖过状态文件）"
    elif state.get("base_url"):
        source = "状态文件"
    else:
        source = "内置默认值"

    print("配置")
    print(f"  服务地址    {args.client.base_url}")
    print(f"  来源        {source}")
    print(f"  API key     {'已设置' if args.client.api_key else '未设置（服务若是 ACTOR_TRUST_MODE=open 可不设）'}")
    print(f"  状态文件    {STATE_PATH}")
    if state.get("run_id"):
        print(f"  记住的 run  {state['run_id']}")
    if saved and args.base_url is None:
        print("  注意        环境变量优先级最高：要换地址就换环境变量，别只改状态文件。")
    print()

    try:
        health = args.client.request("GET", "/api/health")
    except SystemExit as exc:
        print(f"探活        {exc}")
        print("\n地址已记下，但服务现在连不上。确认服务在跑后重跑 `s2v.py config` 即可。")
        return
    service = health.get("service") if isinstance(health, dict) else None
    print(f"探活        ok  ({service or 'api'})")
    try:
        posture = args.client.request("GET", "/api/deployment")
    except ApiError as exc:
        print(f"部署姿态    读不到（{exc.code}），不影响使用")
    else:
        posture = posture or {}
        migration = posture.get("migration") or {}
        pending = migration.get("pending") or []
        print(f"版本        {posture.get('app_version')}  ·  环境 {posture.get('environment')}")
        print(f"迁移        {migration.get('current')}  head {migration.get('head')}  pending={len(pending)}")
        if pending or migration.get("current") != migration.get("head"):
            print("            ↑ 库还不在 head 上，先在服务器跑 `docker compose run --rm api alembic upgrade head`")
    print()
    print('下一步      s2v.py init --script "口播稿" --title "投放批次"')


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s2v.py",
        description="驱动 Script2Video 流水线：建 run → 审关口 → 生成素材 → 数字人 → 合成 → 取片。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用链路：
  s2v.py init --script "口播稿" --title "10月投放"
  s2v.py preflight && s2v.py start
  s2v.py gates            # 每到一个关口就审一次
  s2v.py image --prompt "..."   # 没有人物图时
  s2v.py tts                    # 没有口播音频时（自动读 spoken_script）
  s2v.py avatar && s2v.py status
  s2v.py fetch <rendered_video_asset_id> --out ./out.mp4

不满意时：
  s2v.py rerun --stage plan            # 方向不对，回到阶段
  s2v.py image --prompt "换一张"        # 只换素材
  s2v.py compose                       # 只重渲染
""",
    )
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--run-id", help="默认取 ~/.s2v/state.json 里记住的那个")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="建项目 + 建 run")
    p.add_argument("--script", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--pipeline", default="avatar_spokesperson")
    p.add_argument("--quality-mode", choices=["local_preview", "provider_audio", "provider_avatar"])
    p.add_argument("--brand")
    p.add_argument("--product")
    p.add_argument("--duration", type=int, default=15)
    p.add_argument("--aspect", default="9:16")
    p.add_argument("--platform", default="douyin")
    p.add_argument("--budget", type=float)
    p.add_argument("--style")
    p.add_argument("--description")
    p.add_argument("--language", default="zh-CN")
    p.add_argument("--idempotency-key")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="run 全景：阶段 / 关口 / 作业 / 产物 / 下一步")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("preflight", help="能力预检 + 执行计划（会各追加一个产物）")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("start", help="启动 run（第一个关口前停下）")
    p.add_argument("--accept-downgrade", action="store_true", help="接受环境降级")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("gates", help="列出 open 的审核关口")
    p.set_defaults(func=cmd_gates)

    p = sub.add_parser("gate", help="看一个关口要审什么（含被审产物内容）")
    p.add_argument("review_request_id")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("decide", help="提交审核决策")
    p.add_argument("review_request_id")
    p.add_argument("--decision", required=True,
                   choices=["approve", "approve_with_changes", "request_changes", "reject", "reopen", "cancel"])
    p.add_argument("--comment")
    p.add_argument("--reason-code")
    p.add_argument("--artifact-id")
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("approve", help="放行当前 open 关口（便捷版）")
    p.add_argument("--comment")
    p.add_argument("--decision", default="approve",
                   choices=["approve", "approve_with_changes", "request_changes", "reject"])
    p.add_argument("--reason-code")
    p.add_argument("--artifact-id")
    p.set_defaults(func=cmd_quick_approve)

    p = sub.add_parser("assets", help="列出产物")
    p.set_defaults(func=cmd_assets)

    p = sub.add_parser("artifacts", help="列出阶段产物（可看内容）")
    p.add_argument("--type")
    p.add_argument("--show", action="store_true")
    p.set_defaults(func=cmd_artifacts)

    p = sub.add_parser("jobs", help="列出作业")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("job", help="看单个作业（--process 催一次）")
    p.add_argument("job_id")
    p.add_argument("--process", action="store_true")
    p.set_defaults(func=cmd_job)

    p = sub.add_parser("image", help="生成人物图")
    p.add_argument("--prompt", required=True)
    p.add_argument("--negative")
    p.add_argument("--size")
    p.add_argument("--filename")
    p.set_defaults(func=cmd_image)

    p = sub.add_parser("tts", help="生成口播音频（默认读 spoken_script 全文）")
    p.add_argument("--text")
    p.add_argument("--voice", default="Cherry")
    p.add_argument("--filename")
    p.set_defaults(func=cmd_tts)

    p = sub.add_parser("upload", help="上传本地文件作为素材")
    p.add_argument("--file", required=True)
    p.add_argument("--asset-type", default="person_image")
    p.add_argument("--mime")
    p.add_argument("--duration-ms", type=int)
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("avatar", help="提交数字人任务（默认取最新人物图 + 音频）")
    p.add_argument("--person")
    p.add_argument("--person-url")
    p.add_argument("--audio")
    p.add_argument("--audio-url")
    p.add_argument("--model", default="wan2.2-s2v")
    p.add_argument("--no-auto-compose", dest="auto_compose", action="store_false")
    p.set_defaults(func=cmd_avatar)

    p = sub.add_parser("compose", help="合成成片")
    p.add_argument("--avatar")
    p.add_argument("--audio")
    p.add_argument("--subtitle")
    p.add_argument("--music")
    p.add_argument("--profile")
    p.set_defaults(func=cmd_compose)

    p = sub.add_parser("dispatch", help="派发就绪的流水线步骤（自动补建执行清单）")
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("rerun", help="重跑阶段 / 重跑产物来源阶段")
    p.add_argument("--stage")
    p.add_argument("--artifact")
    p.add_argument("--reason")
    p.set_defaults(func=cmd_rerun)

    p = sub.add_parser("retry", help="重试失败作业")
    p.add_argument("job_id")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("fetch", help="下载产物到本地")
    p.add_argument("asset_id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("link", help="签发限时签名链接")
    p.add_argument("asset_id")
    p.add_argument("--ttl", type=int, default=3600)
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("api", help="直接调任意端点（逃生通道）")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("--body")
    p.set_defaults(func=cmd_api)

    p = sub.add_parser("config", help="记住服务地址并探活（解压后先跑这条）")
    p.add_argument("--base-url", default=argparse.SUPPRESS, help="写入状态文件，后续命令不必再带")
    p.set_defaults(func=cmd_config)

    # 让 `--json` / `--run-id` 写在子命令后面也认（SUPPRESS 保证不覆盖全局值）
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for subparser in action.choices.values():
                subparser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
                subparser.add_argument("--run-id", default=argparse.SUPPRESS)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    state = load_state()
    base_url = args.base_url or os.environ.get("S2V_BASE_URL") or state.get("base_url") or DEFAULT_BASE
    api_key = args.api_key or os.environ.get("S2V_API_KEY") or None
    args.client = Client(base_url, api_key)
    try:
        args.func(args)
    except ApiError as exc:
        detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
        print(f"失败  {exc}", file=sys.stderr)
        if isinstance(detail, dict):
            extra = {k: v for k, v in detail.items() if k not in {"code", "message"}}
            if extra:
                print(f"附加  {json.dumps(extra, ensure_ascii=False)[:600]}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
