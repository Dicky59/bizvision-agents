import { defineConfig, devices } from "@playwright/test";

// The widget is exercised against a mocked API (see e2e/mock.ts), so these runs are deterministic
// and need neither the database nor an LLM key.
export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  reporter: "list",
  retries: 0,
  use: { baseURL: "http://localhost:5173", trace: "retain-on-failure" },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: true,
    timeout: 60_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
    { name: "mobile-chrome", use: { ...devices["Pixel 7"] } },
    { name: "mobile-safari", use: { ...devices["iPhone 14"] } },
  ],
});
