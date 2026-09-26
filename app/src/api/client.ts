import type { Bridge, Method, Params, Result } from "./contract";

export class EngineError extends Error {
  constructor(
    readonly code: number,
    message: string,
  ) {
    super(message);
    this.name = "EngineError";
  }
}

export const ErrorCode = {
  noProject: -32000,
  busy: -32001,
  app: -32002,
  ffmpegMissing: -32003,
  engineGone: -32099,
} as const;

declare global {
  interface Window {
    mcsync?: Bridge;
  }
}

export function bridge(): Bridge {
  if (!window.mcsync) throw new Error("Multicam Sync must run inside its desktop app");
  return window.mcsync;
}

/** Call an engine method; failures become EngineError with the JSON-RPC code. */
export async function call<M extends Method>(method: M, params: Params<M>): Promise<Result<M>> {
  const response = await bridge().invoke(method, params);
  if (response.ok) return response.result;
  throw new EngineError(response.error.code, response.error.message);
}
