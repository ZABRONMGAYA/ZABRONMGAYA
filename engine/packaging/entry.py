"""Entry point of the frozen engine (``mcsync-engine``): the ``mcsync`` command line."""

from mcsync.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
