// Builds the installers' icons and graphics from the Syncora design masters (build/brand, copied verbatim from the
// handoff):
//   build/icon.icns            macOS app icon        (syncora-app-icon-macos.svg: 16 … 1024, @1x and @2x)
//   build/icon.ico             Windows app icon      (syncora-app-icon-square.svg: 16 … 64 as bitmaps, 256 as PNG)
//   build/icon.png             1024 px square master (Linux, fallback, the window icon)
//   build/syncora-doc.*        .syncora document icon (syncora-doc-icon.svg; the SYNCORA label only from 64 px)
//   build/installerSidebar.bmp Windows setup sidebar, 164 × 314 (Brand Kit 1o: symbol on red, "Syncora" 26/800)
//   build/background.png       macOS DMG window, 660 × 400 and @2x (Brand Kit 1n)
// Rendered one at a time with Playwright's Chromium (or CHROMIUM_PATH=/path/to/chromium), with the bundled Archivo.
//   node scripts/render-icons.mjs
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "@playwright/test";

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const build = path.join(root, "build");
const brand = path.join(build, "brand");
const fontFile = (name) => readFileSync(path.join(root, "src/design-system/fonts", name)).toString("base64");
const fonts = `
  @font-face { font-family: Archivo; font-weight: 800; src: url(data:font/woff2;base64,${fontFile("Archivo-ExtraBold.woff2")}) format("woff2"); }
  @font-face { font-family: Archivo; font-weight: 600; src: url(data:font/woff2;base64,${fontFile("Archivo-SemiBold.woff2")}) format("woff2"); }
  @font-face { font-family: Archivo; font-weight: 400; src: url(data:font/woff2;base64,${fontFile("Archivo-Regular.woff2")}) format("woff2"); }`;
const svg = (name) => readFileSync(path.join(brand, name), "utf-8").replace(/<metadata>[\s\S]*?<\/metadata>/, "");
const symbolPaper = readFileSync(path.join(root, "src/design-system/assets/syncora-symbol-paper-solid.svg"), "utf-8")
  .replace(/<metadata>[\s\S]*?<\/metadata>/, "")
  .replace(/ width="48" height="48"/, "");

const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {});
const page = await browser.newPage({ deviceScaleFactor: 1 });

/**
 * PNG of ``markup`` (an SVG fitted into a size × size transparent square, or a full HTML body) at ``width`` ×
 * ``height`` CSS pixels and ``scale`` device pixels per CSS pixel. Renders are sequential: the page is shared.
 */
async function render(markup, width, height = width, { scale = 1, html = false } = {}) {
  await page.setViewportSize({ width, height });
  const body = html
    ? markup
    : `<style>body { display: grid; place-items: center; }
         svg { max-width: ${width}px; max-height: ${height}px; width: auto; height: ${height}px; display: block; }</style>${markup}`;
  await page.setContent(`<html><head><style>${fonts}
    html, body { margin: 0; background: transparent; width: ${width}px; height: ${height}px; overflow: hidden; }
    * { box-sizing: border-box; }
  </style></head><body style="zoom: ${scale}">${body}</body></html>`);
  if (scale !== 1) await page.setViewportSize({ width: width * scale, height: height * scale });
  await page.evaluate(() => document.fonts.ready);
  return page.screenshot({ omitBackground: true, clip: { x: 0, y: 0, width: width * scale, height: height * scale } });
}

/** RGBA pixels of a PNG, decoded by the browser. */
async function pixels(png) {
  const data = await page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const canvas = document.createElement("canvas");
    canvas.width = img.width;
    canvas.height = img.height;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);
    return {
      width: img.width,
      height: img.height,
      rgba: Array.from(ctx.getImageData(0, 0, img.width, img.height).data),
    };
  }, png.toString("base64"));
  return { ...data, rgba: Buffer.from(data.rgba) };
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

/** A 32-bit icon bitmap (DIB: header, bottom-up BGRA rows, then an all-opaque AND mask), as ICO entries need. */
function iconBitmap({ width, height, rgba }) {
  const header = Buffer.alloc(40);
  header.writeUInt32LE(40, 0);
  header.writeInt32LE(width, 4);
  header.writeInt32LE(height * 2, 8); // colour rows + mask rows
  header.writeUInt16LE(1, 12);
  header.writeUInt16LE(32, 14);
  const colour = Buffer.alloc(width * height * 4);
  for (let y = 0; y < height; y++)
    for (let x = 0; x < width; x++) {
      const src = ((height - 1 - y) * width + x) * 4;
      const dst = (y * width + x) * 4;
      colour[dst] = rgba[src + 2];
      colour[dst + 1] = rgba[src + 1];
      colour[dst + 2] = rgba[src];
      colour[dst + 3] = rgba[src + 3];
    }
  const maskRow = Math.ceil(width / 32) * 4;
  header.writeUInt32LE(colour.length + maskRow * height, 20);
  return Buffer.concat([header, colour, Buffer.alloc(maskRow * height)]);
}

/**
 * ICO: sizes below 256 as bitmaps, which every part of Windows reads (taskbar, title bar, Explorer, shortcuts, the
 * installer); 256 as PNG (Vista and later).
 */
async function ico(markupFor, sizes) {
  const images = [];
  for (const size of sizes) {
    const png = await render(markupFor(size), size);
    images.push([size, size >= 256 ? png : iconBitmap(await pixels(png))]);
  }
  const dir = Buffer.alloc(6);
  dir.writeUInt16LE(1, 2);
  dir.writeUInt16LE(images.length, 4);
  let offset = 6 + 16 * images.length;
  const entries = images.map(([size, data]) => {
    const e = Buffer.alloc(16);
    e.writeUInt8(size >= 256 ? 0 : size, 0);
    e.writeUInt8(size >= 256 ? 0 : size, 1);
    e.writeUInt16LE(1, 4);
    e.writeUInt16LE(32, 6);
    e.writeUInt32LE(data.length, 8);
    e.writeUInt32LE(offset, 12);
    offset += data.length;
    return e;
  });
  return Buffer.concat([dir, ...entries, ...images.map(([, data]) => data)]);
}

/** A 24-bit Windows bitmap (what NSIS wizard images must be). */
function bmp24({ width, height, rgba }) {
  const row = Math.ceil((width * 3) / 4) * 4;
  const head = Buffer.alloc(54);
  head.write("BM", 0, "ascii");
  head.writeUInt32LE(54 + row * height, 2);
  head.writeUInt32LE(54, 10);
  head.writeUInt32LE(40, 14);
  head.writeInt32LE(width, 18);
  head.writeInt32LE(height, 22);
  head.writeUInt16LE(1, 26);
  head.writeUInt16LE(24, 28);
  head.writeUInt32LE(row * height, 34);
  head.writeInt32LE(2835, 38); // 72 dpi
  head.writeInt32LE(2835, 42);
  const body = Buffer.alloc(row * height);
  for (let y = 0; y < height; y++)
    for (let x = 0; x < width; x++) {
      const src = ((height - 1 - y) * width + x) * 4;
      const dst = y * row + x * 3;
      body[dst] = rgba[src + 2];
      body[dst + 1] = rgba[src + 1];
      body[dst + 2] = rgba[src];
    }
  return Buffer.concat([head, body]);
}

const ICNS_TYPES = [
  ["icp4", 16],
  ["icp5", 32],
  ["icp6", 64],
  ["ic07", 128],
  ["ic08", 256],
  ["ic09", 512],
  ["ic10", 1024],
  ["ic11", 32],
  ["ic12", 64],
  ["ic13", 256],
  ["ic14", 512],
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

const WINDOWS_SIZES = [16, 20, 24, 32, 40, 48, 64, 256];
const mac = svg("syncora-app-icon-macos.svg");
const square = svg("syncora-app-icon-square.svg");
const doc = svg("syncora-doc-icon.svg");
const docFor = (size) => (size >= 64 ? doc : doc.replace(/<text[\s\S]*?<\/text>/, ""));

writeFileSync(path.join(build, "icon.icns"), await icnsFrom(() => mac));
writeFileSync(path.join(build, "icon.ico"), await ico(() => square, WINDOWS_SIZES));
writeFileSync(path.join(build, "icon.png"), await render(square, 1024));
writeFileSync(path.join(build, "syncora-doc.icns"), await icnsFrom(docFor));
writeFileSync(path.join(build, "syncora-doc.ico"), await ico(docFor, WINDOWS_SIZES));

// Windows setup wizard sidebar (Brand Kit 1o).
const sidebar = `<div style="width:164px;height:314px;background:#ec3013;display:flex;flex-direction:column;
  justify-content:space-between;padding:24px;font-family:Archivo;color:#f3f2f2">
  <div style="width:48px;height:48px">${symbolPaper}</div>
  <span style="font-weight:800;font-size:26px;letter-spacing:-0.03em;line-height:1">Syncora</span></div>`;
writeFileSync(
  path.join(build, "installerSidebar.bmp"),
  bmp24(await pixels(await render(sidebar, 164, 314, { html: true }))),
);

// macOS DMG window (Brand Kit 1n). Finder draws the two icons and their names at the positions set in
// electron-builder.yml (dmg.contents): the Syncora icon in the left cell, Applications on the dashed square.
const dmg = `<div style="width:660px;height:400px;background:#f3f2f2;color:#201e1d;font-family:Archivo;display:flex;
  flex-direction:column">
  <div style="padding:28px 40px;border-bottom:2px solid rgba(32,30,29,0.4);display:flex;justify-content:space-between;
    align-items:center"><span style="font-weight:800;font-size:20px">Drag Syncora to Applications</span>
    <span style="font-size:12px;color:#605d5d">macOS 12 or later</span></div>
  <div style="position:relative;flex:1">
    <svg style="position:absolute;left:270px;top:136px" width="120" height="24" viewBox="0 0 120 24">
      <rect x="0" y="11" width="104" height="2" fill="#ec3013"></rect>
      <path d="M102 4 L114 12 L102 20" fill="none" stroke="#ec3013" stroke-width="2"></path></svg>
    <div style="position:absolute;left:441px;top:84px;width:128px;height:128px;border:2px dashed rgba(32,30,29,0.4)"></div>
  </div></div>`;
writeFileSync(path.join(build, "background.png"), await render(dmg, 660, 400, { html: true }));
writeFileSync(path.join(build, "background@2x.png"), await render(dmg, 660, 400, { html: true, scale: 2 }));

await browser.close();
console.log(
  "wrote build/icon.icns, icon.ico, icon.png, syncora-doc.icns, syncora-doc.ico, installerSidebar.bmp, background.png",
);
