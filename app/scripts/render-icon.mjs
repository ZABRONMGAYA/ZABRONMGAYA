// Renders build/icon.svg to build/icon.png (1024 px, transparent corners): electron-builder makes every icon size
// from it.  node scripts/render-icon.mjs   (Playwright's Chromium, or CHROMIUM_PATH)
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "build");
const svg = readFileSync(path.join(dir, "icon.svg"), "utf-8");
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ viewport: { width: 1024, height: 1024 }, deviceScaleFactor: 1 });
await page.setContent(`<html><body style="margin:0;background:transparent">${svg}</body></html>`);
await page.screenshot({ path: path.join(dir, "icon.png"), omitBackground: true });
await browser.close();
console.log("wrote build/icon.png");
