"""JSON-RPC 2.0 over newline-delimited JSON on stdio.

One message per line in each direction. Requests are handled in order on the
reading thread (every method returns in milliseconds; long work runs as a job,
see :mod:`mcsync.service.jobs`). Notifications may be written from any thread;
writes are serialised by a lock so lines never interleave.

stdout belongs to the protocol. The server redirects ``sys.stdout`` to stderr
while it runs, so a stray ``print`` in any library cannot corrupt the stream.
"""

from __future__ import annotations

import inspect
import json
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any, TextIO

from mcsync.serialize import to_jsonable

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# Application errors
NO_PROJECT = -32000
BUSY = -32001
APP_ERROR = -32002
FFMPEG_MISSING = -32003


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class JsonRpcServer:
    def __init__(self, reader: TextIO, writer: TextIO, *, log: TextIO | None = None) -> None:
        self.reader, self.writer = reader, writer
        self.log = log or sys.stderr
        self.methods: dict[str, Callable[..., Any]] = {}
        self.error_mappers: list[Callable[[BaseException], RpcError | None]] = []
        self._write_lock = threading.Lock()
        self._stopping = False

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self.methods[name] = fn

    def stop(self) -> None:
        """Finish the current request, then leave :meth:`serve_forever`."""
        self._stopping = True

    # ---------------------------------------------------------------- output

    def _write(self, message: dict) -> None:
        line = json.dumps(to_jsonable(message), separators=(",", ":"), allow_nan=False)
        with self._write_lock:
            self.writer.write(line + "\n")
            self.writer.flush()

    def notify(self, method: str, params: Any = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    # ----------------------------------------------------------------- input

    def serve_forever(self) -> None:
        real_stdout = sys.stdout
        sys.stdout = sys.stderr  # keep stray prints out of the protocol stream
        try:
            for line in self.reader:
                if line.strip():
                    response = self.handle_line(line)
                    if response is not None:
                        self._write(response)
                if self._stopping:
                    break
        finally:
            sys.stdout = real_stdout

    def handle_line(self, line: str) -> dict | None:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            return self._error(None, RpcError(PARSE_ERROR, f"parse error: {exc}"))
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
        ):
            return self._error(request.get("id") if isinstance(request, dict) else None,
                               RpcError(INVALID_REQUEST, "invalid request"))  # fmt: skip
        req_id = request.get("id")
        is_notification = "id" not in request
        try:
            result = self._dispatch(request["method"], request.get("params"))
        except Exception as exc:  # noqa: BLE001 - every failure becomes a JSON-RPC error
            return None if is_notification else self._error(req_id, self._map_error(exc))
        return None if is_notification else {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _dispatch(self, method: str, params: Any) -> Any:
        fn = self.methods.get(method)
        if fn is None:
            raise RpcError(METHOD_NOT_FOUND, f"method not found: {method}")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError(INVALID_PARAMS, "params must be an object")
        try:
            inspect.signature(fn).bind(**params)
        except TypeError as exc:
            raise RpcError(INVALID_PARAMS, f"{method}: {exc}") from exc
        return fn(**params)

    def _map_error(self, exc: BaseException) -> RpcError:
        if isinstance(exc, RpcError):
            return exc
        for mapper in self.error_mappers:
            mapped = mapper(exc)
            if mapped is not None:
                return mapped
        traceback.print_exception(exc, file=self.log)
        return RpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _error(req_id: Any, err: RpcError) -> dict:
        error: dict[str, Any] = {"code": err.code, "message": err.message}
        if err.data is not None:
            error["data"] = err.data
        return {"jsonrpc": "2.0", "id": req_id, "error": error}
