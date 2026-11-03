#!/usr/bin/env node
/**
 * Step-by-step run: project, run, persona image, start, watch, retry.
 *
 * Shows the level to plug in at when your own system owns scheduling, progress
 * reporting or a UI. Every call here has a counterpart button in the Web Studio.
 *
 *     SCRIPT2VIDEO_BASE_URL=http://127.0.0.1:8080 \
 *     node examples/upload-and-retry.mjs avatar.png
 */

import { readFile } from "node:fs/promises";

import { ApiError, FinalVideoNotFound, Script2VideoClient } from "../dist/index.js";

const SCRIPT = "把这张人物图做成 15 秒口播视频，讲解我们的新品。";

async function retryThenReport(client, runId) {
  const jobs = await client.listRunJobs(runId);
  const failed = jobs.filter((job) => job.status === "failed");
  if (failed.length === 0) {
    console.log("nothing to retry");
    return;
  }
  for (const job of failed) {
    console.log(`  retrying ${job.jobId} (${job.jobType ?? "job"})`);
    const result = await client.retryAction(runId, "retry_job", { jobId: job.jobId });
    console.log(`    -> ${result["status"]}`);
  }
}

async function main() {
  const client = new Script2VideoClient();

  const project = await client.createProject("示例：口播重试");
  console.log(`project ${project.projectId}`);

  const run = await client.createRun(project.projectId, SCRIPT, {
    pipelineKey: "avatar_spokesperson",
    targetPlatform: "douyin",
    targetDurationSeconds: 15,
    costBudget: 8,
  });
  console.log(`run ${run.runId} status=${run.status}`);

  const imagePath = process.argv[2];
  if (imagePath) {
    const asset = await client.uploadPersonImage(run.runId, {
      filename: imagePath.split("/").pop(),
      content: await readFile(imagePath),
    });
    console.log(`uploaded persona image -> ${asset.assetId} (${asset.mimeType})`);
  }

  // The first paid call on a run needs an explicit acknowledgement. Passing
  // acceptPaidProviders clears every provider the run is waiting on.
  await client.startRun(run.runId, { acceptPaidProviders: true });

  const seen = new Set();
  let finished;
  try {
    finished = await client.waitRun(run.runId, {
      timeoutMs: 1_800_000,
      onProgress: (current) => {
        if (!seen.has(current.status)) {
          seen.add(current.status);
          console.log(`  stage=${current.currentStageKey} status=${current.status}`);
        }
      },
    });
  } catch (error) {
    if (error instanceof ApiError) {
      console.error(`API error while waiting: HTTP ${error.status} ${error.code} — ${error.message}`);
      return 1;
    }
    throw error;
  }

  console.log(`settled at ${finished.status}`);

  if (finished.status === "failed") {
    await retryThenReport(client, run.runId);
    return 1;
  }

  if (finished.isBlockedOnHuman) {
    console.log("a human needs to review before this run continues:");
    const requests = await client.request("GET", `/api/runs/${run.runId}/review-requests`);
    for (const request of requests ?? []) {
      console.log(`  ${request["review_request_id"]} status=${request["status"]}`);
    }
    return 0;
  }

  try {
    const video = await client.getFinalVideo(run.runId);
    console.log(`final video: ${video.url}`);
    return 0;
  } catch (error) {
    if (error instanceof FinalVideoNotFound) {
      console.error(error.message);
      return 1;
    }
    throw error;
  }
}

process.exitCode = await main();
