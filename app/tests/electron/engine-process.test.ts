// @vitest-environment node
// The main process's engine supervisor (electron/engine.ts) against the stand-in engine (e2e/fake-engine.mjs).
import type { ChildProcess } from "node:child_process";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { EngineProcess } from "../../electron/engine";

const fake = path.resolve(import.meta.dirname, "..", "..", "e2e", "fake-engine.mjs");
const started: EngineProcess[] = [];

function engine(): EngineProcess {
  const e = new EngineProcess(() => ({
    command: process.execPath,
    args: [fake],
    env: { ...process.env, MCSYNC_CACHE_DIR: path.join(os.tmpdir(), "syncora-fake-engine-test") },
  }));
  started.push(e);
  return e;
}

afterEach(async () => {
  await Promise.all(started.splice(0).map((e) => e.stop()));
});

describe("EngineProcess", () => {
  it("survives a failed write to an engine that has just exited", async () => {
    const e = engine();
    await e.start();
    // Writing to an engine that exited fails with EPIPE, reported as an "error" event on its input. Without a
    // listener that event throws: in the app, Electron's modal error box then stalls the main process.
    const input = (e as unknown as { child: ChildProcess }).child.stdin!;
    const epipe = Object.assign(new Error("write EPIPE"), { code: "EPIPE" });
    expect(() => input.emit("error", epipe)).not.toThrow();
  });

  it("sends nothing once it is stopping", async () => {
    const e = engine();
    await e.start();
    const stopping = e.stop();
    await expect(e.request("project.info", {})).rejects.toThrow("the engine stopped");
    await stopping;
    expect(e.status.state).toBe("stopped");
  });
});
