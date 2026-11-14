// Task 13 UI: browser voiceprint enrollment and revocation.
//
// The microphone and MediaRecorder are faked in the browser only; no real audio
// device is opened and no demo profile is shipped in the page. Every network
// call is mocked at the browser boundary. 视觉草案，未获 G4 批准。

import { expect, test } from "@playwright/test";

const ORIGIN = "http://127.0.0.1:" + (process.env.CLOUD_TEST_PORT ?? "43119");
const CSRF_TOKEN = "test-csrf-token";
const PAGE = "/web/cloud/voiceprints.html";

function makeProfile(overrides = {}) {
  return {
    id: "vp-1",
    name: "张三",
    version: 2,
    metadata: {
      model_id: "ecapa-tdnn",
      model_version: "2026.05",
      weights_sha256: "abc123",
      dimension: 192,
      sample_rate: 16000,
      preprocess_version: "p1",
      normalization_version: "n1",
      calibration_version: "c1",
    },
    revoked: false,
    created_at: "2026-05-01T08:00:00Z",
    captured_at: "2026-05-01T07:58:00Z",
    ...overrides,
  };
}

function fieldValue(body, name) {
  const match = body.match(new RegExp('name="' + name + '"\\r\\n\\r\\n([\\s\\S]*?)\\r\\n--'));
  return match ? match[1] : "";
}

async function mockApi(page, options = {}) {
  const state = {
    sessionStatus: options.sessionStatus ?? 200,
    listStatus: options.listStatus ?? 200,
    profiles: (options.profiles ?? [makeProfile()]).map((profile) => ({ ...profile })),
    profilesRequests: 0,
    sessionRequests: 0,
    enrollments: [],
    enrollmentRequests: 0,
    deleteRequests: [],
    cancelRequests: [],
    taskGets: 0,
    tasks: new Map(),
    taskSequence: options.taskSequence ?? ["waiting", "completed"],
    conflict: options.conflict ?? null,
    nextProfileId: 1,
  };

  const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  function applyEnrollment(task) {
    const enrollment = task.enrollment;
    if (enrollment.mode === "replace") {
      const index = state.profiles.findIndex((profile) => profile.id === enrollment.profileId);
      if (index !== -1) {
        state.profiles[index] = {
          ...state.profiles[index],
          version: enrollment.version + 1,
          captured_at: "2026-06-01T09:00:00Z",
        };
      }
      return;
    }
    state.profiles.push(
      makeProfile({
        id: "vp-new-" + state.nextProfileId,
        name: enrollment.name,
        version: 1,
        captured_at: "2026-06-01T09:00:00Z",
      }),
    );
    state.nextProfileId += 1;
  }

  await page.route("**/api/cloud/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/cloud/auth/session") {
      state.sessionRequests += 1;
      if (state.sessionStatus !== 200) {
        await json(route, { error: "session_expired" }, state.sessionStatus);
        return;
      }
      await json(route, {
        user_id: "user-1234567890",
        csrf_token: CSRF_TOKEN,
        expires_at: "2030-01-01T00:00:00Z",
      });
      return;
    }

    if (path === "/api/cloud/voiceprints" && method === "GET") {
      state.profilesRequests += 1;
      if (state.listStatus !== 200) {
        await json(route, { error: "internal_error" }, state.listStatus);
        return;
      }
      await json(route, state.profiles);
      return;
    }

    if (path === "/api/cloud/voiceprints" && method === "POST") {
      state.enrollmentRequests += 1;
      if (request.headers()["x-csrf-token"] !== CSRF_TOKEN) {
        await json(route, { error: "csrf_failed" }, 403);
        return;
      }
      if (state.conflict) {
        await json(route, { error: state.conflict }, 409);
        return;
      }
      const buffer = request.postDataBuffer();
      const body = buffer ? buffer.toString("utf8") : "";
      const name = fieldValue(body, "name");
      const consent = fieldValue(body, "consent");
      const replaceVersion = fieldValue(body, "replace_version");
      const file = body.match(
        /name="audio"; filename="([^"]+)"\r\nContent-Type: ([^\r\n]+)\r\n\r\n([\s\S]*?)\r\n--/,
      );
      const enrollment = {
        name,
        consent,
        mode: replaceVersion ? "replace" : "create",
        replaceVersion,
        profileId: options.replaceProfileId ?? "vp-1",
        version: Number(replaceVersion || 1),
        filename: file ? file[1] : "",
        contentType: file ? file[2] : "",
        audioBytes: file ? file[3].length : 0,
      };
      state.enrollments.push(enrollment);
      const task = {
        id: "task-1",
        kind: "voiceprint_enrollment",
        status: "waiting",
        error_code: null,
        enrollment,
        sequence: state.taskSequence.slice(),
        index: 0,
        applied: false,
      };
      state.tasks.set(task.id, task);
      await json(route, { ...task, sequence: undefined, index: undefined, applied: undefined, enrollment: undefined }, 201);
      return;
    }

    const taskMatch = path.match(/^\/api\/cloud\/voiceprints\/tasks\/([^/]+)$/);
    if (taskMatch && method === "GET") {
      state.taskGets += 1;
      const task = state.tasks.get(decodeURIComponent(taskMatch[1]));
      if (!task) {
        await json(route, { error: "not_found" }, 404);
        return;
      }
      const status = task.sequence[Math.min(task.index, task.sequence.length - 1)];
      task.index += 1;
      if (status === "completed" && !task.applied) {
        task.applied = true;
        applyEnrollment(task);
      }
      await json(route, {
        id: task.id,
        kind: task.kind,
        status,
        error_code: status === "failed" ? options.taskErrorCode ?? "voiceprint_extraction_failed" : null,
      });
      return;
    }

    const cancelMatch = path.match(/^\/api\/cloud\/voiceprints\/tasks\/([^/]+)\/cancel$/);
    if (cancelMatch && method === "POST") {
      state.cancelRequests.push(decodeURIComponent(cancelMatch[1]));
      await route.fulfill({ status: 204 });
      return;
    }

    const profileMatch = path.match(/^\/api\/cloud\/voiceprints\/([^/]+)$/);
    if (profileMatch && method === "DELETE") {
      if (request.headers()["x-csrf-token"] !== CSRF_TOKEN) {
        await json(route, { error: "csrf_failed" }, 403);
        return;
      }
      const id = decodeURIComponent(profileMatch[1]);
      state.deleteRequests.push(id);
      state.profiles = state.profiles.filter((profile) => profile.id !== id);
      await route.fulfill({ status: 204 });
      return;
    }

    await json(route, { error: "not_found" }, 404);
  });

  return state;
}

async function installFakeMedia(page) {
  await page.addInitScript(() => {
    window.__fakeTracks = [];
    window.__mediaCalls = [];
    function fakeTrack(index) {
      return {
        kind: "audio",
        label: "fake-microphone-" + index,
        readyState: "live",
        enabled: true,
        muted: false,
        stopped: false,
        stop() {
          this.stopped = true;
          this.readyState = "ended";
        },
        addEventListener() {},
        removeEventListener() {},
        getSettings() {
          return { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
        },
      };
    }
    const devices = navigator.mediaDevices || {};
    Object.defineProperty(devices, "getUserMedia", {
      configurable: true,
      value: async (constraints) => {
        window.__mediaCalls.push(JSON.parse(JSON.stringify(constraints)));
        const tracks = [fakeTrack(0), fakeTrack(1)];
        window.__fakeTracks.push(...tracks);
        return {
          active: true,
          getTracks: () => tracks.slice(),
          getAudioTracks: () => tracks.slice(),
        };
      },
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: devices,
    });

    class FakeMediaRecorder {
      static isTypeSupported(type) {
        return ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].includes(type);
      }
      constructor(stream, options = {}) {
        this.stream = stream;
        this.mimeType = options.mimeType || "audio/webm";
        this.state = "inactive";
        this.ondataavailable = null;
        this.onstop = null;
        this.onerror = null;
      }
      start() {
        if (this.state !== "inactive") throw new Error("recorder already started");
        this.state = "recording";
        const type = (this.mimeType || "audio/webm").split(";")[0];
        const chunk = new Blob(["fake-voiceprint-audio"], { type });
        setTimeout(() => {
          if (this.state !== "inactive" && this.ondataavailable) {
            this.ondataavailable({ data: chunk });
          }
        }, 0);
      }
      stop() {
        if (this.state === "inactive") return;
        this.state = "inactive";
        const type = (this.mimeType || "audio/webm").split(";")[0];
        const tail = new Blob(["fake-voiceprint-tail"], { type });
        setTimeout(() => {
          if (this.ondataavailable) this.ondataavailable({ data: tail });
          if (this.onstop) this.onstop(new Event("stop"));
        }, 0);
      }
    }
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      writable: true,
      value: FakeMediaRecorder,
    });
  });
}

test.beforeEach(async ({ context }) => {
  await context.addCookies([{ name: "cloud_csrf", value: CSRF_TOKEN, url: ORIGIN }]);
});

async function openVoiceprints(page) {
  await page.goto(PAGE);
  await expect(page.getByTestId("profile-card")).toHaveCount(1);
}

async function startRecording(page) {
  await page.getByTestId("enroll-name").fill("张三");
  await page.getByTestId("enroll-consent").check();
  await page.getByTestId("enroll-start").click();
  await expect(page.getByTestId("record-status")).toHaveText("正在录音");
}

test("escape stops every microphone and saves no template", async ({ page }) => {
  const state = await mockApi(page);
  await installFakeMedia(page);
  await openVoiceprints(page);

  await page.getByTestId("enroll-open").click();
  await startRecording(page);
  expect(await page.evaluate(() => window.__fakeTracks.length)).toBe(2);

  await page.keyboard.press("Escape");
  await expect(page.getByTestId("enroll-dialog")).toBeHidden();

  const tracks = await page.evaluate(() =>
    window.__fakeTracks.map((track) => ({ stopped: track.stopped, readyState: track.readyState })),
  );
  expect(tracks).toEqual([
    { stopped: true, readyState: "ended" },
    { stopped: true, readyState: "ended" },
  ]);
  expect(state.enrollmentRequests).toBe(0);
  await expect(page.getByTestId("enroll-open")).toBeFocused();
  await expect(page.getByTestId("profile-card")).toHaveCount(1);

  await page.getByTestId("enroll-open").click();
  await expect(page.getByTestId("enroll-save")).toBeDisabled();
  await page.getByTestId("enroll-name").fill("李四");
  await page.getByTestId("enroll-consent").check();
  await page.getByTestId("enroll-cancel").click();
  await expect(page.getByTestId("enroll-dialog")).toBeHidden();
  expect(state.enrollmentRequests).toBe(0);
});

test("successful enrollment posts audio, polls to completion and reloads the list", async ({ page }) => {
  const state = await mockApi(page);
  await installFakeMedia(page);
  await openVoiceprints(page);
  const initialListRequests = state.profilesRequests;

  await page.getByTestId("enroll-open").click();
  await startRecording(page);
  await page.getByTestId("enroll-save").click();

  await expect(page.getByTestId("enroll-status")).toContainText("处理中");
  await expect(page.getByTestId("page-status")).toBeHidden();

  await expect(page.getByTestId("enroll-dialog")).toBeHidden({ timeout: 10000 });
  await expect(page.getByTestId("profile-card")).toHaveCount(2);
  await expect(page.getByTestId("page-status")).toHaveText("声纹已登记");
  expect(state.enrollmentRequests).toBe(1);
  expect(state.profilesRequests).toBeGreaterThan(initialListRequests);
  expect(state.taskGets).toBeGreaterThanOrEqual(2);

  const enrollment = state.enrollments[0];
  expect(enrollment.name).toBe("张三");
  expect(enrollment.consent).toBe(
    "我同意将本次录音用于提取本人声纹，仅匹配我自己的会议记录。",
  );
  expect(enrollment.mode).toBe("create");
  expect(enrollment.audioBytes).toBeGreaterThan(0);
  expect(enrollment.filename).toMatch(/^sample\.(webm|mp4)$/);
  expect(enrollment.contentType).toContain("audio/");

  const tracks = await page.evaluate(() =>
    window.__fakeTracks.every((track) => track.stopped && track.readyState === "ended"),
  );
  expect(tracks).toBe(true);
  await page.getByTestId("profile-card").nth(1).getByTestId("profile-name").waitFor();
  await expect(page.getByTestId("profile-card").nth(1).getByTestId("profile-name")).toHaveText("张三");
  await expect(page.getByTestId("profile-card").nth(1).getByTestId("profile-model")).toHaveText(
    "ecapa-tdnn / 2026.05",
  );
});

test("replace needs the second confirmation and sends replace_version", async ({ page }) => {
  const state = await mockApi(page);
  await installFakeMedia(page);
  await openVoiceprints(page);

  await page.getByTestId("profile-replace").click();
  await expect(page.getByTestId("enroll-dialog")).toBeVisible();
  await expect(page.getByTestId("enroll-name")).toHaveValue("张三");
  expect(await page.getByTestId("enroll-name").getAttribute("readonly")).not.toBeNull();
  await expect(page.getByTestId("replace-warning")).toContainText("保存成功后才替换版本 2");
  await expect(page.getByTestId("replace-warning")).toContainText("失败会保留旧版本");
  await expect(page.getByTestId("replace-confirm")).not.toBeChecked();

  await page.getByTestId("enroll-consent").check();
  await page.getByTestId("enroll-start").click();
  await expect(page.getByTestId("record-status")).toHaveText("正在录音");
  await expect(page.getByTestId("enroll-save")).toBeDisabled();
  expect(state.enrollmentRequests).toBe(0);

  await page.getByTestId("replace-confirm").check();
  await expect(page.getByTestId("enroll-save")).toBeEnabled();
  await page.getByTestId("enroll-save").click();

  await expect(page.getByTestId("enroll-dialog")).toBeHidden({ timeout: 10000 });
  await expect(page.getByTestId("profile-version")).toHaveText("版本 3");
  expect(state.enrollments).toHaveLength(1);
  expect(state.enrollments[0].replaceVersion).toBe("2");
  expect(state.enrollments[0].mode).toBe("replace");
  await expect(page.getByTestId("page-status")).toHaveText("声纹已登记");
});

test("revoke discloses the history limit, cancels without a write, and deletes only on confirm", async ({
  page,
}) => {
  const state = await mockApi(page);
  await openVoiceprints(page);

  const disclosure = await page.getByTestId("revoke-disclosure").textContent();
  expect(disclosure).toContain("不会自动改写");
  expect(disclosure).toContain("另行重跑");

  await page.getByTestId("profile-revoke").click();
  const dialog = page.getByTestId("revoke-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("张三");
  await expect(dialog).toContainText("不会自动改写");

  await page.getByTestId("revoke-cancel").click();
  await expect(dialog).toBeHidden();
  expect(state.deleteRequests).toEqual([]);
  await expect(page.getByTestId("profile-card")).toHaveCount(1);
  await expect(page.getByTestId("profile-revoke")).toBeFocused();

  await page.getByTestId("profile-revoke").click();
  await page.getByTestId("revoke-confirm").click();
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("profile-card")).toHaveCount(0);
  await expect(page.getByTestId("page-status")).toHaveText("已撤销");
  expect(state.deleteRequests).toEqual(["vp-1"]);
});

test("cancel while processing posts cancel and keeps no profile", async ({ page }) => {
  const state = await mockApi(page, { taskSequence: ["waiting", "waiting", "waiting"] });
  await installFakeMedia(page);
  await openVoiceprints(page);

  await page.getByTestId("enroll-open").click();
  await startRecording(page);
  await page.getByTestId("enroll-save").click();
  await expect(page.getByTestId("enroll-cancel-task")).toBeVisible();
  await expect(page.getByTestId("enroll-status")).toContainText("处理中");

  await page.getByTestId("enroll-cancel-task").click();
  await expect(page.getByTestId("enroll-status")).toHaveText("已取消本次处理。");
  expect(state.cancelRequests).toEqual(["task-1"]);
  expect(state.deleteRequests).toEqual([]);
  await expect(page.getByTestId("profile-card")).toHaveCount(1);
  await expect(page.getByTestId("enroll-dialog")).toBeVisible();
});

test("a 409 voiceprint_exists asks before replacing instead of silently replacing", async ({ page }) => {
  const state = await mockApi(page, { conflict: "voiceprint_exists" });
  await installFakeMedia(page);
  await openVoiceprints(page);

  await page.getByTestId("enroll-open").click();
  await startRecording(page);
  await page.getByTestId("enroll-save").click();

  await expect(page.getByTestId("enroll-conflict")).toBeVisible();
  await expect(page.getByTestId("enroll-conflict")).toContainText("该姓名已存在有效声纹");
  await expect(page.getByTestId("enroll-use-replace")).toBeVisible();
  await expect(page.getByTestId("enroll-dialog")).toBeVisible();
  await expect(page.getByTestId("profile-version")).toHaveText("版本 2");

  await page.getByTestId("enroll-use-replace").click();
  await expect(page.getByTestId("enroll-name")).toHaveValue("张三");
  await expect(page.getByTestId("replace-warning")).toBeVisible();
  await expect(page.getByTestId("enroll-dialog")).toBeVisible();
  expect(state.enrollmentRequests).toBe(1);
});

test("fits 320px with the enrollment dialog open", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  const longModel = "ecapa-tdnn-".repeat(12);
  await mockApi(page, {
    profiles: [
      makeProfile({
        name: "一个名字非常非常长的声纹用于检查换行",
        metadata: {
          model_id: longModel,
          model_version: "2026.05.01-build-" + "x".repeat(40),
        },
      }),
    ],
  });
  await page.goto(PAGE);
  await expect(page.getByTestId("profile-card")).toHaveCount(1);
  await page.getByTestId("enroll-open").click();
  await expect(page.getByTestId("enroll-dialog")).toBeVisible();

  const metrics = await page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const offenders = [];
    for (const node of document.querySelectorAll("body *")) {
      const rect = node.getBoundingClientRect();
      if (rect.width > 0 && (rect.right > width + 1 || rect.left < -1)) {
        offenders.push(
          (node.getAttribute("data-testid") || node.tagName) +
            ":" +
            Math.round(rect.left) +
            "-" +
            Math.round(rect.right),
        );
      }
    }
    const dialog = document.querySelector('[data-testid="enroll-dialog"]');
    const dialogRect = dialog.getBoundingClientRect();
    return {
      width,
      scrollWidth: document.documentElement.scrollWidth,
      bodyScrollWidth: document.body.scrollWidth,
      offenders: offenders.slice(0, 10),
      dialog: {
        left: dialogRect.left,
        right: dialogRect.right,
        scrollWidth: dialog.scrollWidth,
        clientWidth: dialog.clientWidth,
      },
    };
  });

  expect(metrics.width).toBe(320);
  expect(metrics.scrollWidth).toBeLessThanOrEqual(321);
  expect(metrics.bodyScrollWidth).toBeLessThanOrEqual(321);
  expect(metrics.offenders).toEqual([]);
  expect(metrics.dialog.left).toBeGreaterThanOrEqual(-1);
  expect(metrics.dialog.right).toBeLessThanOrEqual(321);
  expect(metrics.dialog.scrollWidth).toBeLessThanOrEqual(metrics.dialog.clientWidth + 1);
});

test("keyboard focus shows a visible focus ring", async ({ page }) => {
  await mockApi(page);
  await page.goto(PAGE);
  await expect(page.getByTestId("profile-card")).toHaveCount(1);

  let reached = false;
  for (let index = 0; index < 40; index += 1) {
    await page.keyboard.press("Tab");
    reached = await page.evaluate(
      () => document.activeElement?.getAttribute("data-testid") === "profile-replace",
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
  await page.goto(PAGE);
  await expect(page.getByTestId("profile-card")).toHaveCount(1);

  expect(
    await page.evaluate(() => window.matchMedia("(prefers-color-scheme: dark)").matches),
  ).toBe(true);
  expect(
    await page.evaluate(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches),
  ).toBe(true);

  const colors = await page.evaluate(() => {
    const body = getComputedStyle(document.body);
    const button = getComputedStyle(document.querySelector(".btn--primary"));
    const disclosure = getComputedStyle(document.querySelector('[data-testid="revoke-disclosure"]'));
    return {
      background: body.backgroundColor,
      text: body.color,
      transition: Number.parseFloat(button.transitionDuration),
      disclosureColor: disclosure.color,
    };
  });
  expect(colors.background).toBe("rgb(17, 24, 20)");
  expect(colors.text).toBe("rgb(237, 245, 239)");
  expect(colors.transition).toBeLessThan(0.05);
  expect(colors.disclosureColor).not.toBe(colors.background);
});

test("expired session shows the login gate and requests no profiles", async ({ page }) => {
  const state = await mockApi(page, { sessionStatus: 401 });
  await page.goto(PAGE);

  await expect(page.getByTestId("login-gate")).toBeVisible();
  await expect(page.getByTestId("login-link")).toHaveAttribute(
    "href",
    ORIGIN + "/api/cloud/auth/login",
  );
  await expect(page.getByTestId("profile-card")).toHaveCount(0);
  expect(state.profilesRequests).toBe(0);
  expect(state.enrollmentRequests).toBe(0);
});