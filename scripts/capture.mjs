// An isolated browser captures a real Grafana panel, never the user's browser profile.
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const url = new URL(payload.url);
if (!["http:", "https:"].includes(url.protocol)) throw new Error("Invalid panel URL");
const playwright = process.env.PLAYWRIGHT_MODULE_PATH
  ? await import(pathToFileURL(path.join(process.env.PLAYWRIGHT_MODULE_PATH, "index.mjs")).href)
  : await import("playwright");
const browser = await playwright.chromium.launch({
  headless: true,
  ...(process.env.CHROME_EXECUTABLE ? { executablePath: process.env.CHROME_EXECUTABLE } : {}),
  args: process.env.GUARDIAN_CONTAINER === "1" ? ["--no-sandbox"] : [],
});
try {
  const context = await browser.newContext({ viewport: { width: 1100, height: 420 }, deviceScaleFactor: 1 });
  // Scope authentication to the configured Grafana origin, including redirects.
  await context.route("**/*", async (route) => {
    const request = route.request();
    const headers = { ...request.headers() };
    if (new URL(request.url()).origin === url.origin) Object.assign(headers, payload.headers || {});
    await route.continue({ headers });
  });
  const page = await context.newPage();
  await page.goto(payload.url, { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.locator("[data-testid='data-testid panel content'], [data-testid='data-testid Panel container'], [data-testid='panel-container'], .panel-container").first().waitFor({ state: "visible", timeout: 20000 });
  // Grafana hydrates and resolves the data-source request after the container appears.
  await page.waitForLoadState("networkidle", { timeout: 10000 }).catch(() => {});
  await page.waitForTimeout(800);
  if (new URL(page.url()).pathname.startsWith("/login")) throw new Error("Grafana authentication failed");
  await page.screenshot({ path: payload.output, fullPage: false });
} finally {
  await browser.close();
}
