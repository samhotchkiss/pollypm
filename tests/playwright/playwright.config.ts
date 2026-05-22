import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the PollyPM v0 web UI.
 *
 * Prereq: `pm serve` (or `pm up`) must be running on 127.0.0.1:8765,
 * with the session cookie endpoint at /ui/ available. The webServer
 * block below is commented out by default — uncomment if you want
 * Playwright to start `pm serve` for you. We keep it off by default
 * because Sam's normal dev loop already has the daemon running.
 */

const BASE_URL = process.env.POLLYPM_BASE_URL || "http://127.0.0.1:8765";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [["html", { open: "never" }], ["list"]],
  timeout: 30_000,
  expect: {
    timeout: 5_000,
  },
  use: {
    baseURL: BASE_URL,
    headless: true,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // Uncomment to have Playwright start `pm serve` itself.
  // webServer: {
  //   command: "pm serve",
  //   url: BASE_URL + "/api/v1/health",
  //   reuseExistingServer: !process.env.CI,
  //   timeout: 30_000,
  // },
});
