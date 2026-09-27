// Builds the installers' icons from the Syncora design masters (build/brand, copied verbatim from the handoff):
//   build/icon.icns       macOS app icon        (syncora-app-icon-macos.svg: 16 … 1024, @1x and @2x)
//   build/icon.ico        Windows app icon      (syncora-app-icon-square.svg: 16, 32, 48, 256)
//   build/icon.png        1024 px square master (Linux, fallback)
//   build/syncora-doc.*   .syncora document icon (syncora-doc-icon.svg; the SYNCORA label only from 64 px)
// Rendered with Playwright's Chromium (or CHROMIUM_PATH), with the bundled Archivo loaded for the label text.
//   node scripts/render-icons.mjs
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const build = path.join(root, "build");
const brand = path.join(build, "brand");
const font = readFileSync(path.join(root, "src/design-system/fonts/Archivo-ExtraBold.woff2")).toString("base64");
const svg = (name) => readFileSync(path.join(brand, name), "utf-8").replace(/<metadata>[\s\S]*?<\/metadata>/, "");

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ deviceScaleFactor: 1 });

/** PNG of ``markup`` fitted (aspect kept, centred) into a size × size transparent square. */
async function render(markup, size) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(`<html><head><style>
    @font-face { font-family: Archivo; font-weight: 800; src: url(data:font/woff2;base64,${font}) format("woff2"); }
    html, body { margin: 0; background: transparent; width: ${size}px; height: ${size}px; }
    body { display: grid; place-items: center; }
    svg { max-width: ${size}px; max-height: ${size}px; width: auto; height: ${size}px; display: block; }
  </style></head><body>${markup}</body></html>`);
  await page.evaluate(() => document.fonts.ready);
  return page.screenshot({ omitBackground: true, clip: { x: 0, y: 0, width: size, height: size } });
}

// ICNS: "icns" + length, then (type, length, PNG) entries.
function icns(entries) {
  const chunks = entries.map(([type, png]) => {
    const head = Buffer.alloc(8);
    head.write(type, 0, "ascii");
    head.writeUInt32BE(png.length + 8, 4);
    return Buffer.concat([head, png]);
  });
  const head = Buffer.alloc(8);
  head.write("icns", 0, "ascii");
  head.writeUInt32BE(8 + chunks.reduce((n, c) => n + c.length, 0), 4);
  return Buffer.concat([head, ...chunks]);
}

// ICO with PNG-compressed images (Windows Vista and later).
function ico(images) {
  const dir = Buffer.alloc(6);
  dir.writeUInt16LE(0, 0);
  dir.writeUInt16LE(1, 2);
  dir.writeUInt16LE(images.length, 4);
  let offset = 6 + 16 * images.length;
  const entries = images.map(([size, png]) => {
    const e = Buffer.alloc(16);
    e.writeUInt8(size >= 256 ? 0 : size, 0);
    e.writeUInt8(size >= 256 ? 0 : size, 1);
    e.writeUInt16LE(1, 4);
    e.writeUInt16LE(32, 6);
    e.writeUInt32LE(png.length, 8);
    e.writeUInt32LE(offset, 12);
    offset += png.length;
    return e;
  });
  return Buffer.concat([dir, ...entries, ...images.map(([, png]) => png)]);
}

const ICNS_TYPES = [
  ["icp4", 16], ["icp5", 32], ["icp6", 64], ["ic07", 128], ["ic08", 256], ["ic09", 512], ["ic10", 1024],
  ["ic11", 32], ["ic12", 64], ["ic13", 256], ["ic14", 512],
];

async function icnsFrom(markupFor) {
  const cache = new Map();
  const entries = [];
  for (const [type, size] of ICNS_TYPES) {
    if (!cache.has(size)) cache.set(size, await render(markupFor(size), size));
    entries.push([type, cache.get(size)]);
  }
  return icns(entries);
}

const mac = svg("syncora-app-icon-macos.svg");
const square = svg("syncora-app-icon-square.svg");
const doc = svg("syncora-doc-icon.svg");
const docFor = (size) => (size >= 64 ? doc : doc.replace(/<text[\s\S]*?<\/text>/, ""));

writeFileSync(path.join(build, "icon.icns"), await icnsFrom(() => mac));
writeFileSync(path.join(build, "icon.ico"), ico(await Promise.all([16, 32, 48, 256].map(async (s) => [s, await render(square, s)]))));
writeFileSync(path.join(build, "icon.png"), await render(square, 1024));
writeFileSync(path.join(build, "syncora-doc.icns"), await icnsFrom(docFor));
const docIco = [];
for (const s of [16, 32, 48, 256]) docIco.push([s, await render(docFor(s), s)]);
writeFileSync(path.join(build, "syncora-doc.ico"), ico(docIco));
await browser.close();
console.log("wrote build/icon.icns, icon.ico, icon.png, syncora-doc.icns, syncora-doc.ico");
