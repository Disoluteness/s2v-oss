#!/usr/bin/env node
/**
 * Script in, final video out.
 *
 *     SCRIPT2VIDEO_BASE_URL=http://127.0.0.1:8080 \
 *     SCRIPT2VIDEO_API_KEY=s2v_... \
 *     node examples/script-to-video.mjs
 *
 * Without an API key the client falls back to X-Actor-Id (set
 * SCRIPT2VIDEO_ACTOR_ID) or to the deployment's default actor, which is how the
 * internal gateway path is expected to work.
 */

import { writeFile } from "node:fs/promises";

import { ApiError, FinalVideoNotFound, Script2VideoClient } from "../dist/index.js";

const SCRIPT =
  "开场三秒用一个反常识的问题抓住注意力。" +
  "然后介绍产品解决了什么具体问题，给出一个可验证的数据。" +
  "最后用一句有记忆点的行动号召收尾。";

async function main() {
  const client = new Script2VideoClient();

  const identity = await client.me();
  console.log(`acting as ${identity.actorId} (source=${identity.source})`);
  console.log(`permissions: ${identity.permissions.length}`);

  try {
    const video = await client.scriptToVideo(SCRIPT, {
      projectTitle: "示例：新品预热",
      timeoutMs: 1_800_000,
      acceptPaidProviders: true,
      onProgress: (run) => console.log(`  ... ${run.status} @ ${run.currentStageKey}`),
    });

    console.log(`\nfinal video: ${video.url}`);
    console.log(`  asset: ${video.assetId} (${video.assetType}, ${video.mimeType})`);
    console.log(`  duration: ${video.durationMs} ms  size: ${video.fileSize} bytes`);

    const bytes = await client.downloadAsset(video.assetId);
    await writeFile("out.mp4", bytes);
    console.log(`  saved ${bytes.length} bytes to out.mp4`);
    return 0;
  } catch (error) {
    if (error instanceof FinalVideoNotFound) {
      console.error(`no video was produced: ${error.message}`);
      return 1;
    }
    if (error instanceof ApiError) {
      console.error(`API refused the request: HTTP ${error.status} ${error.code} — ${error.message}`);
      console.error(`  retryable: ${error.retryable}`);
      return 1;
    }
    throw error;
  }
}

process.exitCode = await main();
