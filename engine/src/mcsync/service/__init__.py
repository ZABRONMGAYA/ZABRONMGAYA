"""The engine as a JSON-RPC service for the desktop app."""

from .app import EngineService, serve_stdio
from .rpc import JsonRpcServer, RpcError

__all__ = ["EngineService", "JsonRpcServer", "RpcError", "serve_stdio"]
