"""``python -m mcsync.service``: serve JSON-RPC on stdin/stdout."""

from mcsync.cli import main

if __name__ == "__main__":
    main(["serve"])
