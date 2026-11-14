// Playwright configuration for the cloud recorder browser suites.
//
// The browser modules under web/cloud use real IndexedDB, so tests are served
// from a tiny local static server rather than file:// or data: URLs. The
// fixture page is deliberately temporary: Task 12 builds the real workbench.
import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.CLOUD_TEST_PORT ?? 43119);
const BASE_URL = `http://127.0.0.1:${PORT}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.mjs",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"]],
  outputDir: "test-results",
  webServer: {
    command: `node tests/browser/cloud/static-server.mjs --port ${PORT}`,
    url: `${BASE_URL}/tests/browser/cloud/fixture.html`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
  use: {
    baseURL: BASE_URL,
    headless: true,
    permissions: ["microphone"],
    launchOptions: {
      args: [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
      ],
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
