// Task 12B: cloud transcript + annotation workbench, in a real browser.
//
// Every network call is mocked at the browser network boundary (page.route).
// No demo data lives in web/cloud/index.html, and no real microphone, audio
// device, model, or backend is involved. The audio payload is a generated
// silent WAV buffer; it is not a real recording.
//
// Screenshots go to test-results only (testInfo.outputPath).

import { expect, test } from "@playwright/test";

const ORIGIN = "http://127.0.0.1:" + (process.env.CLOUD_TEST_PORT ?? "43119");
const CSRF_TOKEN = "test-csrf-token";
const WORKBENCH = "/web/cloud/index.html?meeting=m1";

function makeMeeting(overrides = {}) {
  return {
    id: "m1",
    title: "产品评审",
    mode: "dual",
    generation: 3,
    current_transcript_id: "t2",
    deleted_at: null,
    ...overrides,
  };
}

function makeTranscript(overrides = {}) {
  return {
    id: "t2",
    meeting_id: "m1",
    status: "complete",
    empty: false,
    warnings: [],
    speakers: [
      { id: "spk1", label: "主持人", suspect: false },
      { id: "spk2", label: "发言人 2", suspect: true },
    ],
    segments: [
      {
        id: "s1",
        start_ms: 0,
        end_ms: 1200,
        text: "大家好，我们开始。",
        speaker_id: "spk1",
        track_id: "mic",
      },
      {
        id: "s2",
        start_ms: 1000,
        end_ms: 2400,
        text: "先看预算。",
        speaker_id: "spk2",
        track_id: "system",
      },
    ],
    ...overrides,
  };
}

// 5 seconds of silence, 16 kHz mono 16-bit PCM. Generated locally.
function silentWav(seconds = 5) {
  const sampleRate = 16000;
  const samples = sampleRate * seconds;
  const wav = Buffer.alloc(44 + samples * 2);
  wav.write("RIFF", 0);
  wav.writeUInt32LE(36 + samples * 2, 4);
  wav.write("WAVE", 8);
  wav.write("fmt ", 12);
  wav.writeUInt32LE(16, 16);
  wav.writeUInt16LE(1, 20);
  wav.writeUInt16LE(1, 22);
  wav.writeUInt32LE(sampleRate, 24);
  wav.writeUInt32LE(sampleRate * 2, 28);
  wav.writeUInt16LE(2, 32);
  wav.writeUInt16LE(16, 34);
  wav.write("data", 36);
  wav.writeUInt32LE(samples * 2, 40);
  return wav;
}

function meetingFromPath(path) {
  const prefix = "/api/cloud/meetings/";
  if (!path.startsWith(prefix)) return null;
  const rest = path.slice(prefix.length);
  const slash = rest.indexOf("/");
  return slash === -1 ? decodeURIComponent(rest) : decodeURIComponent(rest.slice(0, slash));
}

async function mockApi(page, options = {}) {
  const meetings = options.meetings ?? [makeMeeting(), makeMeeting({ id: "m2", title: "周会复盘", mode: "mic" })];
  const transcripts = options.transcripts ?? {
    m1: [makeTranscript(), makeTranscript({ id: "t1", status: "completed_with_warnings", segments: [makeTranscript().segments[0]] })],
    m2: [makeTranscript({ id: "t3", meeting_id: "m2", segments: [makeTranscript().segments[0]] })],
  };
  const state = {
    sessionStatus: options.sessionStatus ?? 200,
    meetingsStatus: options.meetingsStatus ?? 200,
    meetings: meetings.map((meeting) => ({ ...meeting })),
    transcripts,
    annotations: (options.annotations ?? [
      { id: "a1", meeting_id: "m1", offset_ms: 5000, kind: "highlight", text: "重点：预算" },
    ]).map((annotation) => ({ ...annotation })),
    annotationRequests: [],
    annotationStatus: options.annotationStatus ?? 201,
    failMeetingId: options.failMeetingId ?? null,
    audioRequests: 0,
    listRequests: 0,
    sessionRequests: 0,
    nextAnnotationId: 1,
  };

  const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  await page.route("**/api/cloud/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/cloud/auth/session") {
      state.sessionRequests += 1;
      if (state.sessionStatus !== 200) {
        await json(route, { code: "session_expired" }, state.sessionStatus);
        return;
      }
      await json(route, {
        user_id: "user-1234567890",
        csrf_token: CSRF_TOKEN,
        expires_at: "2030-01-01T00:00:00Z",
      });
      return;
    }

    if (path === "/api/cloud/meetings") {
      state.listRequests += 1;
      if (state.meetingsStatus !== 200) {
        await json(route, { code: "internal_error" }, state.meetingsStatus);
        return;
      }
      await json(route, { items: state.meetings, next_cursor: null });
      return;
    }

    const meetingId = meetingFromPath(path);
    if (!meetingId) {
      await json(route, { code: "not_found" }, 404);
      return;
    }
    const meeting = state.meetings.find((item) => item.id === meetingId);
    if (!meeting) {
      await json(route, { code: "not_found" }, 404);
      return;
    }

    if (path.endsWith("/audio") && method === "GET") {
      state.audioRequests += 1;
      const wav = silentWav(5);
      const total = wav.length;
      const range = request.headers()["range"];
      if (range && range.startsWith("bytes=")) {
        const parts = range.slice(6).split("-");
        const start = Number(parts[0] || 0);
        const end = parts[1] ? Math.min(Number(parts[1]), total - 1) : total - 1;
        const chunk = wav.subarray(start, end + 1);
        await route.fulfill({
          status: 206,
          contentType: "audio/wav",
          headers: {
            "Accept-Ranges": "bytes",
            "Content-Range": "bytes " + start + "-" + end + "/" + total,
            "Content-Length": String(chunk.length),
          },
          body: chunk,
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "audio/wav",
        headers: { "Accept-Ranges": "bytes", "Content-Length": String(total) },
        body: wav,
      });
      return;
    }

    if (path.endsWith("/annotations")) {
      if (method === "POST") {
        if (request.headers()["x-csrf-token"] !== CSRF_TOKEN) {
          await json(route, { code: "csrf_failed" }, 403);
          return;
        }
        const payload = JSON.parse(request.postData() ?? "{}");
        state.annotationRequests.push(payload);
        if (state.annotationStatus !== 201) {
          await json(route, { code: "invalid_annotation" }, state.annotationStatus);
          return;
        }
        const created = {
          id: "a-new-" + state.nextAnnotationId,
          meeting_id: meetingId,
          kind: payload.kind,
          offset_ms: payload.offset_ms,
          text: payload.text,
        };
        state.nextAnnotationId += 1;
        state.annotations.push(created);
        await json(route, created, 201);
        return;
      }
      await json(route, state.annotations.filter((annotation) => annotation.meeting_id === meetingId));
      return;
    }

    if (path.endsWith("/transcripts") && method === "GET") {
      await json(route, state.transcripts[meetingId] ?? []);
      return;
    }

    if (method === "GET") {
      if (meetingId === state.failMeetingId) {
        await json(route, { code: "internal_error" }, 500);
        return;
      }
      await json(route, meeting);
      return;
    }

    await json(route, { code: "not_found" }, 404);
  });

  return state;
}

test.beforeEach(async ({ context }) => {
  await context.addCookies([{ name: "cloud_csrf", value: CSRF_TOKEN, url: ORIGIN }]);
});

async function openWorkbench(page) {
  await page.goto(WORKBENCH);
  await expect(page.getByTestId("workbench")).toBeVisible();
}

test("renders the three-column workbench with versioned transcript", async ({ page }) => {
  await mockApi(page);
  await openWorkbench(page);

  await expect(page.getByTestId("transcript-panel")).toBeVisible();
  await expect(page.getByTestId("annotations-panel")).toBeVisible();
  await expect(page.getByTestId("history-panel")).toBeVisible();
  await expect(page.getByTestId("transcript-version")).toHaveText("t2");
  await expect(page.getByTestId("transcript-status")).toHaveText("最终稿");
  await expect(page.getByTestId("transcript-segment")).toHaveCount(2);
  await expect(page.getByTestId("annotation-item")).toHaveCount(1);
  await expect(page.getByTestId("history-item")).toHaveCount(2);
  await expect(page.getByTestId("history-item").first()).toHaveAttribute("aria-current", "true");
  await expect(page.getByTestId("audio-player")).toHaveAttribute("src", "/api/cloud/meetings/m1/audio");
});

test("fits a 320px viewport without horizontal overflow", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await mockApi(page);
  await openWorkbench(page);
  await expect(page.getByTestId("transcript-segment")).toHaveCount(2);

  const metrics = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const offenders = [];
    for (const node of document.querySelectorAll("body *")) {
      const rect = node.getBoundingClientRect();
      if (rect.width > 0 && (rect.right > width + 1 || rect.left < -1)) {
        offenders.push(
          (node.getAttribute("data-testid") || node.tagName) + ":" + Math.round(rect.left) + "-" + Math.round(rect.right),
        );
      }
    }
    const timestamp = document.querySelector('[data-testid="segment-seek"]');
    return {
      width,
      scrollWidth: document.documentElement.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      offenders: offenders.slice(0, 10),
      timestamp: timestamp
        ? { clientWidth: timestamp.clientWidth, scrollWidth: timestamp.scrollWidth }
        : null,
    };
  });

  expect(metrics.width).toBe(320);
  expect(metrics.scrollWidth).toBeLessThanOrEqual(321);
  expect(metrics.bodyScrollWidth).toBeLessThanOrEqual(321);
  expect(metrics.offenders).toEqual([]);
  expect(metrics.timestamp).not.toBeNull();
  expect(metrics.timestamp.scrollWidth).toBeLessThanOrEqual(metrics.timestamp.clientWidth + 1);

  await page.getByTestId("history-panel").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("workbench-320.png"), fullPage: true });
});

test("fits the 375, 768 and 1280 breakpoints", async ({ page }, testInfo) => {
  await mockApi(page);
  await openWorkbench(page);

  for (const width of [375, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByTestId("transcript-segment")).toHaveCount(2);
    const metrics = await page.evaluate(() => {
      const width = document.documentElement.clientWidth;
      const offenders = [];
      for (const node of document.querySelectorAll("body *")) {
        const rect = node.getBoundingClientRect();
        if (rect.width > 0 && (rect.right > width + 1 || rect.left < -1)) {
          offenders.push(node.getAttribute("data-testid") || node.tagName);
        }
      }
      return { width, scrollWidth: document.documentElement.scrollWidth, offenders };
    });
    expect(metrics.width).toBe(width);
    expect(metrics.scrollWidth).toBeLessThanOrEqual(width + 1);
    expect(metrics.offenders).toEqual([]);
    await expect(page.getByTestId("recording-strip")).toBeVisible();
    await expect(page.getByTestId("transcript-panel")).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("workbench-" + width + ".png"), fullPage: true });
  }
});

test("renders untrusted transcript and annotation text as inert text", async ({ page }) => {
  const payload = "<img src=x onerror=window.__xss=1><script>window.__xss=2<\/script>";
  await mockApi(page, {
    meetings: [makeMeeting()],
    transcripts: {
      m1: [
        makeTranscript({
          segments: [
            {
              id: "s1",
              start_ms: 0,
              end_ms: 1200,
              text: payload,
              speaker_id: "spk1",
              track_id: "mic",
            },
          ],
        }),
      ],
    },
    annotations: [{ id: "a1", meeting_id: "m1", offset_ms: 0, kind: "note", text: payload }],
  });
  await openWorkbench(page);

  await expect(page.getByTestId("transcript-segment")).toHaveCount(1);
  const text = await page.getByTestId("transcript-segment").locator(".segment-text").textContent();
  expect(text).toBe(payload);
  await expect(page.getByTestId("annotation-item").locator(".annotation-text")).toHaveText(payload);

  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
  expect(await page.locator("img").count()).toBe(0);
  expect(await page.locator("script").count()).toBe(1);
});

test("history selection changes the viewed transcript but not the active recording", async ({ page }) => {
  const state = await mockApi(page);
  await openWorkbench(page);

  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("workbench:recording", {
        detail: {
          userId: "u1",
          uploadId: "up1",
          mode: "dual",
          captureRunning: true,
          activeMeetingId: "m1",
          capture: { state: "recording", startedAt: 1 },
          upload: {
            state: "uploading",
            pendingBytes: 4096,
            ackedChunks: 3,
            lastAck: { contiguousMs: 12000 },
          },
          processing: { state: "running" },
          tracks: [
            { id: "mic", source: "mic", readyState: "live" },
            { id: "system", source: "system", readyState: "live" },
          ],
        },
      }),
    );
  });

  await expect(page.getByTestId("capture-state")).toContainText("正在录音");
  await expect(page.getByTestId("capture-state")).toContainText("产品评审");
  await expect(page.getByTestId("saved-position")).toHaveText("已连续保存到 00:12.000");
  await expect(page.getByTestId("source-mic")).toHaveAttribute("data-source-state", "录音中");
  await expect(page.getByTestId("source-system")).toHaveAttribute("data-source-state", "录音中");
  await expect(page.getByTestId("playback-blocked")).toBeVisible();
  await expect(page.getByTestId("audio-player")).toHaveAttribute("aria-disabled", "true");

  await page.getByTestId("history-item").nth(1).click();

  await expect(page.getByTestId("transcript-panel").getByRole("heading", { level: 2 })).toHaveText("周会复盘");
  await expect(page.getByTestId("history-banner")).toContainText("产品评审");
  await expect(page.getByTestId("history-banner")).toContainText("不是实时稿");
  await expect(page.getByTestId("playback-caution")).toBeVisible();
  await expect(page.getByTestId("audio-player")).toHaveAttribute("src", "/api/cloud/meetings/m2/audio");

  await expect(page.getByTestId("capture-state")).toContainText("正在录音");
  await expect(page.getByTestId("stop-recording")).toBeVisible();

  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("workbench:recording", {
        detail: {
          captureRunning: true,
          activeMeetingId: "m1",
          capture: { state: "recording" },
          upload: { state: "uploading", lastAck: { contiguousMs: 12000 } },
          tracks: [
            { id: "mic", source: "mic", readyState: "live" },
            { id: "system", source: "system", readyState: "ended" },
          ],
        },
      }),
    );
  });
  await expect(page.getByTestId("source-mic")).toHaveAttribute("data-source-state", "录音中");
  await expect(page.getByTestId("source-system")).toHaveAttribute("data-source-state", "已停止");
  expect(state.meetings.length).toBe(2);
});

test("manual scroll does not follow the tail until asked", async ({ page }) => {
  const segments = Array.from({ length: 40 }, (_, index) => ({
    id: "s" + index,
    start_ms: index * 2000,
    end_ms: index * 2000 + 1800,
    text: "第 " + (index + 1) + " 段：这一段足够长，用来撑出可滚动的文字稿区域。",
    speaker_id: "spk1",
    track_id: "mic",
  }));
  await page.setViewportSize({ width: 375, height: 600 });
  await mockApi(page, {
    transcripts: {
      m1: [makeTranscript({ segments })],
      m2: [makeTranscript({ id: "t3", meeting_id: "m2", segments: [segments[0]] })],
    },
  });
  await openWorkbench(page);

  const scroller = page.getByTestId("transcript-scroll");
  const geometry = await scroller.evaluate((node) => {
    node.scrollTop = 200;
    node.dispatchEvent(new Event("scroll"));
    return { height: node.scrollHeight, clientHeight: node.clientHeight, top: node.scrollTop };
  });
  expect(geometry.height).toBeGreaterThan(geometry.clientHeight);
  await expect(page.getByTestId("back-to-latest")).toBeVisible();

  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("workbench:recording", {
        detail: {
          captureRunning: true,
          activeMeetingId: "m1",
          capture: { state: "recording" },
          upload: { state: "uploading", lastAck: { contiguousMs: 12000 } },
          tracks: [{ id: "mic", source: "mic", readyState: "live" }],
        },
      }),
    );
  });
  await expect(page.getByTestId("source-mic")).toHaveAttribute("data-source-state", "录音中");
  const topAfter = await page.getByTestId("transcript-scroll").evaluate((node) => node.scrollTop);
  expect(Math.abs(topAfter - geometry.top)).toBeLessThanOrEqual(2);

  await page.getByTestId("back-to-latest").click();
  await expect(page.getByTestId("back-to-latest")).toBeHidden();
  const atTail = await page.getByTestId("transcript-scroll").evaluate((node) => {
    const remaining = node.scrollHeight - node.scrollTop - node.clientHeight;
    return remaining;
  });
  expect(atTail).toBeLessThanOrEqual(24);
});

test("a failed history switch shows an error instead of stale transcript", async ({ page }) => {
  await mockApi(page, { failMeetingId: "m2" });
  await openWorkbench(page);

  await expect(page.getByTestId("transcript-version")).toHaveText("t2");
  await page.getByTestId("history-item").nth(1).click();

  await expect(page.getByTestId("error-state")).toBeVisible();
  await expect(page.getByTestId("error-state")).toContainText("无法加载工作台");
  await expect(page.getByTestId("transcript-panel")).toHaveCount(0);
  await expect(page.getByTestId("history-banner")).toHaveCount(0);
});

test("seeking from a transcript segment sets audio time and annotation offset", async ({ page }) => {
  await mockApi(page);
  await openWorkbench(page);

  await page.getByTestId("segment-seek").nth(1).click();
  await expect(page.getByTestId("audio-player")).toHaveAttribute("data-requested-offset-ms", "1000");
  await expect.poll(async () =>
    page.evaluate(() => {
      const audio = document.querySelector('[data-testid="audio-player"]');
      return audio ? audio.currentTime : -1;
    }),
  ).toBeGreaterThan(0.85);
  await expect(page.getByTestId("annotation-offset")).toHaveValue("1000");
});

test("annotation POST carries CSRF and body, appears once, no optimistic insert", async ({ page }) => {
  const state = await mockApi(page);
  await openWorkbench(page);

  await expect(page.getByTestId("annotation-item")).toHaveCount(1);
  await expect(page.getByTestId("annotations-version")).toContainText("t2");

  await page.getByTestId("annotation-kind").selectOption("comment");
  await page.getByTestId("annotation-offset").fill("2500");
  await page.getByTestId("annotation-text").fill("补充：排期再确认");
  await expect(page.getByTestId("annotation-item")).toHaveCount(1);

  await page.getByTestId("annotation-save").click();

  await expect(page.getByTestId("annotation-status")).toHaveText("标注已保存");
  await expect(page.getByTestId("annotation-item")).toHaveCount(2);
  await expect(page.getByTestId("annotation-list").getByTestId("annotation-item").first()).toContainText("补充：排期再确认");
  await expect(page.getByTestId("annotation-item").first()).toHaveAttribute("data-offset-ms", "2500");
  await expect(page.getByTestId("annotation-text")).toHaveValue("");

  expect(state.annotationRequests).toEqual([
    { kind: "comment", offset_ms: 2500, text: "补充：排期再确认" },
  ]);

  const headers = await page.evaluate(async () => {
    const response = await fetch("/api/cloud/meetings/m1/annotations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: "note", offset_ms: 1, text: "header probe" }),
    });
    return response.status;
  });
  // The probe above lacks the CSRF header on purpose: the mock must reject it.
  expect(headers).toBe(403);
});

test("annotation form rejects invalid input without a network call", async ({ page }) => {
  const state = await mockApi(page);
  await openWorkbench(page);

  await page.getByTestId("annotation-offset").fill("-5");
  await page.getByTestId("annotation-text").fill("时间非法");
  await page.getByTestId("annotation-save").click();
  await expect(page.getByTestId("annotation-error")).toContainText("不小于 0");

  await page.getByTestId("annotation-offset").fill("10");
  await page.getByTestId("annotation-text").fill("   ");
  await page.getByTestId("annotation-save").click();
  await expect(page.getByTestId("annotation-error")).toContainText("1–2000");

  expect(state.annotationRequests).toEqual([]);
});

test("stop dialog closes on Escape and returns focus", async ({ page }) => {
  await mockApi(page);
  await openWorkbench(page);

  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("workbench:recording", {
        detail: { captureRunning: true, capture: { state: "recording" }, tracks: [] },
      }),
    );
  });

  const stop = page.getByTestId("stop-recording");
  await expect(stop).toBeVisible();
  await stop.click();

  const dialog = page.locator("#stop-dialog");
  await expect(dialog).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(stop).toBeFocused();
});

test("confirming stop dispatches the host stop event once", async ({ page }) => {
  await mockApi(page);
  await openWorkbench(page);
  await page.evaluate(() => {
    window.__stops = 0;
    window.addEventListener("workbench:stop-recording", () => {
      window.__stops += 1;
    });
    window.dispatchEvent(
      new CustomEvent("workbench:recording", {
        detail: { captureRunning: true, capture: { state: "recording" }, tracks: [] },
      }),
    );
  });

  await page.getByTestId("stop-recording").click();
  await page.getByTestId("stop-confirm").click();

  await expect(page.locator("#stop-dialog")).toBeHidden();
  expect(await page.evaluate(() => window.__stops)).toBe(1);
});

test("keyboard focus shows a visible focus ring", async ({ page }) => {
  await mockApi(page);
  await openWorkbench(page);

  let reached = false;
  for (let index = 0; index < 60; index += 1) {
    await page.keyboard.press("Tab");
    reached = await page.evaluate(
      () => document.activeElement?.getAttribute("data-testid") === "segment-seek",
    );
    if (reached) break;
  }
  expect(reached).toBe(true);

  const ring = await page.evaluate(() => {
    const style = getComputedStyle(document.activeElement);
    return {
      style: style.outlineStyle,
      width: Number.parseFloat(style.outlineWidth),
      offset: Number.parseFloat(style.outlineOffset),
    };
  });
  expect(ring.style).toBe("solid");
  expect(ring.width).toBeGreaterThanOrEqual(2);
  expect(ring.offset).toBeGreaterThanOrEqual(2);
});

test("dark scheme and reduced motion are honoured", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await mockApi(page);
  await openWorkbench(page);

  expect(
    await page.evaluate(() => window.matchMedia("(prefers-color-scheme: dark)").matches),
  ).toBe(true);
  expect(
    await page.evaluate(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches),
  ).toBe(true);

  const colors = await page.evaluate(() => {
    const body = getComputedStyle(document.body);
    const button = getComputedStyle(document.querySelector(".btn--primary"));
    return {
      background: body.backgroundColor,
      text: body.color,
      transition: Number.parseFloat(button.transitionDuration),
    };
  });
  expect(colors.background).toBe("rgb(17, 24, 20)");
  expect(colors.text).toBe("rgb(237, 245, 239)");
  expect(colors.transition).toBeLessThan(0.05);

  await page.evaluate(() => {
    window.dispatchEvent(
      new CustomEvent("workbench:recording", {
        detail: {
          captureRunning: true,
          activeMeetingId: "m1",
          capture: { state: "recording" },
          tracks: [],
        },
      }),
    );
  });
  await expect(page.getByTestId("stop-recording")).toBeVisible();
  const dangerContrast = await page.evaluate(() => {
    const button = document.querySelector('[data-testid="stop-recording"]');
    const style = getComputedStyle(button);
    const luminance = (value) => {
      const channels = value.match(/\d+/g).slice(0, 3).map(Number);
      const linear = (channel) => {
        const c = channel / 255;
        return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
      };
      return 0.2126 * linear(channels[0]) + 0.7152 * linear(channels[1]) + 0.0722 * linear(channels[2]);
    };
    const foreground = luminance(style.color);
    const background = luminance(style.backgroundColor);
    const lighter = Math.max(foreground, background);
    const darker = Math.min(foreground, background);
    return (lighter + 0.05) / (darker + 0.05);
  });
  expect(dangerContrast).toBeGreaterThanOrEqual(4.5);
});

test("expired session shows the login gate and no meeting content", async ({ page }) => {
  const state = await mockApi(page, { sessionStatus: 401 });
  await page.goto(WORKBENCH);

  await expect(page.getByTestId("login-gate")).toBeVisible();
  await expect(page.getByTestId("login-link")).toHaveAttribute("href", ORIGIN + "/api/cloud/auth/login");
  await expect(page.getByTestId("transcript-panel")).toHaveCount(0);
  await expect(page.getByTestId("audio-player")).toHaveCount(0);
  expect(state.listRequests).toBe(0);
  expect(state.audioRequests).toBe(0);
});

test("list failure shows a retry affordance and recovers", async ({ page }) => {
  const state = await mockApi(page, { meetingsStatus: 500 });
  await page.goto(WORKBENCH);

  await expect(page.getByTestId("error-state")).toBeVisible();
  await expect(page.getByTestId("error-state")).toContainText("无法加载工作台");

  state.meetingsStatus = 200;
  await page.getByTestId("retry-load").click();
  await expect(page.getByTestId("workbench")).toBeVisible();
  await expect(page.getByTestId("transcript-version")).toHaveText("t2");
});

test("empty account shows an empty state and never requests audio", async ({ page }) => {
  const state = await mockApi(page, { meetings: [] });
  await page.goto("/web/cloud/index.html");

  await expect(page.getByTestId("empty-state")).toBeVisible();
  await expect(page.getByTestId("transcript-panel")).toHaveCount(0);
  expect(state.audioRequests).toBe(0);
});
