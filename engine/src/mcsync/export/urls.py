"""File URLs for media references in XML timelines."""

from __future__ import annotations

import re
from urllib.parse import quote

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def file_url(path: str, *, localhost: bool = False) -> str:
    """``file://`` URL of an absolute path, percent-encoded as RFC 3986 requires (UTF-8).

    ``localhost=True`` gives the ``file://localhost/…`` form that FCP 7 XML readers (Premiere Pro, Resolve) expect;
    FCPXML uses ``file:///…``. Windows drive paths become ``/C:/…``, UNC paths ``file://server/share/…``.
    """
    host = "localhost" if localhost else ""
    if path.startswith(("\\\\", "//")):  # UNC: \\\\server\\share\\dir\\file
        server, _, rest = path.replace("\\", "/").lstrip("/").partition("/")
        return f"file://{server}/{quote(rest, safe='/')}"
    if _WINDOWS_DRIVE.match(path):
        posix = "/" + path.replace("\\", "/")
        return f"file://{host}{quote(posix, safe='/:')}"
    if not path.startswith("/"):
        raise ValueError(f"not an absolute path: {path!r}")
    return f"file://{host}{quote(path, safe='/')}"
