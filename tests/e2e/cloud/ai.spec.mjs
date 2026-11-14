// Task 14 UI: private, ephemeral interview questions and safe Markdown rendering.
//
// Every network call is mocked at the browser boundary. The markdown payloads in
// these tests deliberately contain raw HTML and dangerous link schemes; no real AI
// provider, OCR engine or external host is ever contacted.

import { expect, test } from "@playwright/test";

const ORIGIN = "http://127.0.0.1:" + (process.env.CLOUD_TEST_PORT ?? "43119");
const CSRF_TOKEN = "test-csrf-token";
const PAGE = "/web/cloud/interview.html";

const DEFAULT_MARKDOWN = [
  "# 面试题",
  "## 一、专业能力（约 20 分钟）",
  "### 1. 请介绍一次你排查线上问题的经历",
  "- 预计用时：5 分钟",
  "- 考察点：问题定位与复盘能力",
  "**答案要点：**",
  "- 说明现象与影响范围",
  "- 说明定位过程和最终修复",
].join("\n\n");

function makePosition(overrides = {}) {
  return {
    id: "pos-1",
    type: "后端",
    title: "后端工程师",
    requirements: "熟悉 Python、PostgreSQL，有高并发服务经验。",
    ...overrides,
  };
}

function json(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function mockApi(page, options = {}) {
  const state = {
    sessionStatus: options.sessionStatus ?? 200,
    positions: (options.positions ?? [makePosition()]).map((item) => ({ ...item })),
    positionGets: 0,
    positionCreates: [],
    interviewRequests: [],
    interviewStatus: options.interviewStatus ?? 200,
    interviewBody: options.interviewBody ?? { markdown: DEFAULT_MARKDOWN },
    interviewError: options.interviewError ?? null,
    nextPositionId: 2,
  };

  await page.route("**/api/cloud/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/cloud/auth/session") {
      if (state.sessionStatus !== 200) {
        await json(route, { error: "authentication_required" }, state.sessionStatus);
        return;
      }
      await json(route, {
        user_id: "user-1234567890",
        csrf_token: CSRF_TOKEN,
        expires_at: "2030-01-01T00:00:00Z",
      });
      return;
    }

    if (path === "/api/cloud/positions" && method === "GET") {
      state.positionGets += 1;
      await json(route, state.positions);
      return;
    }

    if (path === "/api/cloud/positions" && method === "POST") {
      if (request.headers()["x-csrf-token"] !== CSRF_TOKEN) {
        await json(route, { error: "csrf_failed" }, 403);
        return;
      }
      const body = request.postDataJSON();
      state.positionCreates.push({
        body,
        csrf: request.headers()["x-csrf-token"],
      });
      const created = makePosition({
        id: "pos-" + state.nextPositionId,
        type: body.type,
        title: body.title,
        requirements: body.requirements,
      });
      state.nextPositionId += 1;
      state.positions.unshift(created);
      await json(route, created, 201);
      return;
    }

    const interview = path.match(/^\/api\/cloud\/positions\/([^/]+)\/interview$/);
    if (interview && method === "POST") {
      if (request.headers()["x-csrf-token"] !== CSRF_TOKEN) {
        await json(route, { error: "csrf_failed" }, 403);
        return;
      }
      const buffer = request.postDataBuffer();
      state.interviewRequests.push({
        positionId: decodeURIComponent(interview[1]),
        csrf: request.headers()["x-csrf-token"],
        body: buffer ? buffer.toString("utf8") : "",
        bytes: buffer ? buffer.length : 0,
      });
      if (state.interviewStatus !== 200) {
        await json(
          route,
          state.interviewError ?? { error: "ai_not_configured" },
          state.interviewStatus,
        );
        return;
      }
      await json(route, state.interviewBody);
      return;
    }

    await json(route, { error: "not_found" }, 404);
  });

  return state;
}

test.beforeEach(async ({ context }) => {
  await context.addCookies([{ name: "cloud_csrf", value: CSRF_TOKEN, url: ORIGIN }]);
});

async function openInterview(page, title = "后端工程师") {
  await page.goto(PAGE);
  await expect(page.getByTestId("position-select")).toBeVisible();
  await expect(page.getByTestId("selected-position-title")).toHaveText(title);
}

async function generate(page) {
  await page.getByTestId("generate-submit").click();
  await expect(page.getByTestId("result-panel")).toBeVisible();
}

test("renders untrusted markdown inert and only permits http(s) links", async ({ page }) => {
  const state = await mockApi(page);
  state.interviewBody = {
    markdown: [
      "# 面试题",
      '<script>window.__xss = "script"</script>',
      '<img src="x" onerror="window.__xss = \'img\'">',
      '<iframe srcdoc="<script>window.__xss = \'iframe\'</script>"></iframe>',
      "[危险](javascript:window.__xss='link')",
      "[数据](data:text/html,<script>window.__xss='data'</script>)",
      "[正常](https://example.com/path?q=1)",
      "**加粗** 与 *斜体* 与 `行内代码`",
    ].join("\n\n"),
  };
  await openInterview(page);
  await generate(page);

  const result = page.getByTestId("result-markdown");
  await expect(result).toBeVisible();
  expect(await page.evaluate(() => window.__xss)).toBeUndefined();
  await expect(result.locator("script, img, iframe, svg")).toHaveCount(0);
  await expect(result).toContainText("<script>");
  await expect(result).toContainText("<img");
  await expect(result).not.toContainText("javascript:");

  const anchors = result.locator("a");
  await expect(anchors).toHaveCount(1);
  await expect(anchors).toHaveAttribute("href", "https://example.com/path?q=1");
  await expect(anchors).toHaveAttribute("target", "_blank");
  await expect(anchors).toHaveAttribute("rel", /noopener/);
  await expect(anchors).toHaveAttribute("rel", /noreferrer/);

  const unsafe = await result.locator("*").evaluateAll((nodes) =>
    nodes.filter((node) =>
      Array.from(node.attributes).some(
        (attribute) =>
          attribute.name.startsWith("on") ||
          attribute.name === "srcdoc" ||
          attribute.name === "style",
      ),
    ).length,
  );
  expect(unsafe).toBe(0);
  await expect(result.locator("strong")).toHaveText("加粗");
  await expect(result.locator("em")).toHaveText("斜体");
  await expect(result.locator("code")).toHaveText("行内代码");
});

test("posts resume and images once and keeps nothing in browser storage", async ({ page }) => {
  const external = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.origin !== ORIGIN) external.push(url.toString());
  });
  const state = await mockApi(page);
  await openInterview(page);

  const png = Buffer.from([
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4, 0x89,
  ]);
  await page.getByTestId("resume-input").fill("候选人张三，五年后端经验。");
  await page.getByTestId("images-input").setInputFiles([
    { name: "resume-1.png", mimeType: "image/png", buffer: png },
    { name: "resume-2.png", mimeType: "image/png", buffer: png },
  ]);
  await expect(page.getByTestId("images-count")).toHaveText("已选择 2 张图片");
  await generate(page);

  expect(state.interviewRequests).toHaveLength(1);
  const sent = state.interviewRequests[0];
  expect(sent.positionId).toBe("pos-1");
  expect(sent.csrf).toBe(CSRF_TOKEN);
  expect(sent.body).toContain('name="resume_text"');
  expect(sent.body).toContain("候选人张三，五年后端经验。");
  expect((sent.body.match(/name="images"/g) || []).length).toBe(2);
  expect(sent.body).toContain('filename="resume-1.png"');
  expect(sent.body).toContain('filename="resume-2.png"');
  expect(sent.bytes).toBeGreaterThan(png.length);
  expect(external).toEqual([]);

  const storage = await page.evaluate(() => ({
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
  }));
  expect(storage).toEqual({ local: [], session: [] });
  expect(new URL(page.url()).search).toBe("");

  await page.reload();
  await expect(page.getByTestId("result-panel")).toBeHidden();
  await expect(page.getByTestId("resume-input")).toHaveValue("");
  await expect(page.getByTestId("images-count")).toHaveText("未选择图片");
});

test("keeps configured and long-output errors visible with no result", async ({ page }) => {
  const state = await mockApi(page, {
    interviewStatus: 503,
    interviewError: { error: "ai_not_configured" },
  });
  await openInterview(page);

  await page.getByTestId("generate-submit").click();
  const error = page.getByTestId("form-error");
  await expect(error).toBeVisible();
  await expect(error).toHaveText("AI 服务尚未配置，暂时无法生成。");
  await expect(page.getByTestId("result-panel")).toBeHidden();
  await page.getByTestId("resume-input").focus();
  await expect(error).toBeVisible();

  state.interviewStatus = 502;
  state.interviewError = { error: "ai_output_too_long" };
  await generateFails(page);
  await expect(error).toHaveText("生成内容超出长度限制，请缩短输入后重试。");
  await expect(page.getByTestId("result-panel")).toBeHidden();

  // A provider that violates the 40000-character contract is also rejected by the
  // renderer, so the oversized text is never inserted into the document.
  state.interviewStatus = 200;
  state.interviewBody = { markdown: "# " + "长".repeat(40000) };
  await generateFails(page);
  await expect(error).toHaveText("生成内容超出长度限制，请缩短输入后重试。");
  await expect(page.getByTestId("result-markdown")).toBeEmpty();
});

async function generateFails(page) {
  await page.getByTestId("generate-submit").click();
  await expect(page.getByTestId("form-error")).toBeVisible();
}

test("creates a position, submits by keyboard and moves focus to the result", async ({ page }) => {
  const state = await mockApi(page);
  await openInterview(page);

  await page.getByTestId("position-create-open").click();
  await expect(page.getByTestId("position-type")).toBeFocused();
  await expect(page.getByTestId("position-type").locator("option")).toHaveCount(4);
  await page.getByTestId("position-type").selectOption("测试");
  await page.getByTestId("position-title").fill("测试工程师");
  await page.getByTestId("position-requirements").fill("熟悉接口测试与自动化，重视质量。");
  await page.getByTestId("position-save").click();

  await expect(page.getByTestId("position-editor")).toBeHidden();
  await expect(page.getByTestId("selected-position-title")).toHaveText("测试工程师");
  await expect(page.getByTestId("selected-position-type")).toHaveText("职位类型：测试");
  await expect(page.getByTestId("page-status")).toHaveText("职位已保存。");
  await expect(page.getByTestId("position-select")).toBeFocused();
  expect(state.positionCreates).toHaveLength(1);
  expect(state.positionCreates[0].body).toEqual({
    type: "测试",
    title: "测试工程师",
    requirements: "熟悉接口测试与自动化，重视质量。",
  });
  expect(state.positionCreates[0].csrf).toBe(CSRF_TOKEN);

  await page.getByTestId("resume-input").fill("候选人李四，测试开发经验。");
  await page.getByTestId("generate-submit").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("result-panel")).toBeVisible();
  await expect(page.getByTestId("result-panel")).toBeFocused();
  expect(state.interviewRequests).toHaveLength(1);
  expect(state.interviewRequests[0].positionId).toBe("pos-2");
  expect(state.interviewRequests[0].body).toContain("候选人李四，测试开发经验。");

  await page.getByTestId("result-clear").click();
  await expect(page.getByTestId("result-panel")).toBeHidden();
  await expect(page.getByTestId("resume-input")).toBeFocused();
  await expect(page.getByTestId("result-markdown")).toBeEmpty();
});

test("fits 320px, keeps 44px targets and honors dark mode", async ({ page }) => {
  const state = await mockApi(page);
  state.interviewBody = {
    markdown: [
      "# 面试题",
      "https://example.com/" + "a".repeat(140),
      "非常长的中文内容".repeat(30),
      "```",
      "x".repeat(160),
      "```",
    ].join("\n\n"),
  };
  await page.setViewportSize({ width: 320, height: 720 });
  await openInterview(page);
  await generate(page);

  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    body: document.body.scrollWidth - document.body.clientWidth,
  }));
  expect(overflow.document).toBeLessThanOrEqual(0);
  expect(overflow.body).toBeLessThanOrEqual(0);

  const heights = await page.evaluate(() =>
    ["generate-submit", "input-clear", "result-copy", "result-clear", "position-select"].map(
      (id) => {
        const node = document.querySelector('[data-testid="' + id + '"]');
        return { id, height: node.getBoundingClientRect().height };
      },
    ),
  );
  for (const target of heights) expect(target.height).toBeGreaterThanOrEqual(44);

  await page.emulateMedia({ colorScheme: "dark" });
  const colors = await page.evaluate(() => {
    const style = getComputedStyle(document.body);
    return { background: style.backgroundColor, color: style.color };
  });
  expect(colors.background).toBe("rgb(17, 24, 20)");
  expect(colors.color).toBe("rgb(237, 245, 239)");
});

test("requires login and shows no demo position", async ({ page }) => {
  await mockApi(page, { sessionStatus: 401 });
  await page.goto(PAGE);
  await expect(page.getByTestId("login-gate")).toBeVisible();
  await expect(page.getByTestId("login-link")).toHaveAttribute(
    "href",
    /\/api\/cloud\/auth\/login/,
  );
  await expect(page.getByTestId("position-panel")).toHaveCount(0);
  await expect(page.locator("body")).not.toContainText("后端工程师");
});
