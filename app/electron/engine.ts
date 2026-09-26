// Supervises the Python engine child process and speaks JSON-RPC to it over stdio.
import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import path from "node:path";

import { type EngineStatus, type Hello, PROTOCOL_VERSION, type RpcFailure } from "../src/api/contract";

export interface EngineCommand {
  command: string;
  args: string[];
  env: NodeJS.ProcessEnv;
}

/**
 * How to start the engine:
 * - packaged app: the frozen engine and FFmpeg inside the app's resources;
 * - MCSYNC_ENGINE: an explicit executable (development, CI);
 * - otherwise `python -m mcsync.cli serve` with MCSYNC_PYTHON or python3/python.
 */
export function engineCommand(opts: { packaged: boolean; resourcesPath: string }): EngineCommand {
  const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" };
  const exe = (name: string) => (process.platform === "win32" ? `${name}.exe` : name);
  if (process.env.MCSYNC_ENGINE) {
    return { command: process.env.MCSYNC_ENGINE, args: ["serve"], env };
  }
  if (opts.packaged) {
    env.MCSYNC_FFMPEG_DIR = path.join(opts.resourcesPath, "ffmpeg");
    return { command: path.join(opts.resourcesPath, "engine", exe("mcsync-engine")), args: ["serve"], env };
  }
  const python = process.env.MCSYNC_PYTHON ?? (process.platform === "win32" ? "python" : "python3");
  return { command: python, args: ["-m", "mcsync.cli", "serve"], env };
}

export class RpcError extends Error implements RpcFailure {
  constructor(
    readonly code: number,
    message: string,
  ) {
    super(message);
  }
}

const ENGINE_GONE = -32099;
const TIMEOUT = -32098;

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: RpcError) => void;
  timer: NodeJS.Timeout;
}

export class EngineProcess extends EventEmitter {
  private child: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<number, Pending>();
  private nextId = 1;
  private buffer = "";
  private stopping = false;
  private restarts = 0;
  readonly stderrTail: string[] = [];
  status: EngineStatus = { state: "stopped", hello: null, error: null };

  constructor(private readonly command: () => EngineCommand) {
    super();
  }

  private setStatus(status: EngineStatus): void {
    this.status = status;
    this.emit("status", status);
  }

  async start(): Promise<Hello> {
    this.stopping = false;
    this.setStatus({ state: "starting", hello: null, error: null });
    const { command, args, env } = this.command();
    const child = spawn(command, args, { env, stdio: ["pipe", "pipe", "pipe"], windowsHide: true });
    this.child = child;
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => this.onData(chunk));
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      this.stderrTail.push(...chunk.split("\n").filter(Boolean));
      this.stderrTail.splice(0, Math.max(0, this.stderrTail.length - 200));
    });
    child.on("error", (err) => this.onExit(null, err.message));
    child.on("exit", (code) => this.onExit(code, null));
    try {
      const hello = (await this.request("engine.hello", { client: "multicam-sync-app" }, 60_000)) as Hello;
      if (hello.protocol !== PROTOCOL_VERSION) {
        throw new RpcError(-32000, `engine protocol ${hello.protocol}, app expects ${PROTOCOL_VERSION}`);
      }
      this.setStatus({ state: "ready", hello, error: null });
      return hello;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.setStatus({ state: "crashed", hello: null, error: `${message}\n${this.stderrTail.slice(-20).join("\n")}` });
      throw err;
    }
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let newline: number;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      if (!line) continue;
      let message: { id?: number; method?: string; params?: unknown; result?: unknown; error?: RpcFailure };
      try {
        message = JSON.parse(line);
      } catch {
        this.stderrTail.push(`unparseable engine output: ${line.slice(0, 200)}`);
        continue;
      }
      if (message.method !== undefined && message.id === undefined) {
        this.emit("event", { method: message.method, params: message.params });
      } else if (typeof message.id === "number") {
        const pending = this.pending.get(message.id);
        if (!pending) continue;
        this.pending.delete(message.id);
        clearTimeout(pending.timer);
        if (message.error) pending.reject(new RpcError(message.error.code, message.error.message));
        else pending.resolve(message.result);
      }
    }
  }

  private onExit(code: number | null, error: string | null): void {
    if (!this.child) return;
    this.child = null;
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(new RpcError(ENGINE_GONE, "the engine stopped"));
    }
    this.pending.clear();
    if (this.stopping) {
      this.setStatus({ state: "stopped", hello: null, error: null });
      return;
    }
    const detail = error ?? `exit code ${code}`;
    this.setStatus({ state: "crashed", hello: null, error: `${detail}\n${this.stderrTail.slice(-20).join("\n")}` });
    if (this.restarts < 3) {
      this.restarts += 1;
      setTimeout(() => this.start().catch(() => undefined), 1000);
    }
  }

  request(method: string, params: unknown, timeoutMs = 120_000): Promise<unknown> {
    const child = this.child;
    if (!child) return Promise.reject(new RpcError(ENGINE_GONE, "the engine is not running"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new RpcError(TIMEOUT, `${method} timed out`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  async stop(): Promise<void> {
    const child = this.child;
    if (!child) return;
    this.stopping = true;
    const exited = new Promise<void>((resolve) => child.once("exit", () => resolve()));
    try {
      await this.request("engine.shutdown", {}, 15_000);
    } catch {
      // already gone or unresponsive: killed below
    }
    const timer = setTimeout(() => child.kill(), 15_000);
    await exited;
    clearTimeout(timer);
  }
}
