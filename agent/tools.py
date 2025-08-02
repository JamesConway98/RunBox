"""The tool registry.

Fixed, defined in this file, and deliberately small. There is no user-supplied
tool code anywhere in Runbox — that is a stated non-goal, and it is also the
single largest thing standing between this sandbox and a genuinely hostile
multi-tenant boundary.

`http_get` does not touch the network directly. The container runs with
`--network=none`; the tool talks to the runner's egress proxy over a unix
socket, and the proxy enforces the host allowlist. The agent never gets raw
egress even if it is fully compromised.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Where the agent is allowed to read from. Mounted read-only by the runner.
WORKSPACE = Path(os.environ.get("RUNBOX_WORKSPACE", "/workspace"))

# The runner's egress proxy. Absent in unit tests, in which case http_get fails
# cleanly rather than falling back to real network access.
PROXY_SOCKET = os.environ.get("RUNBOX_PROXY_SOCKET", "/run/runbox/egress.sock")

MAX_OUTPUT_CHARS = 24_000


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., str]


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n\n[truncated, {len(text)} chars total]"


def http_get(url: str) -> str:
    """Fetch a URL through the runner's allowlisting egress proxy."""
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")

    request = json.dumps({"url": url}).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(30)
            sock.connect(PROXY_SOCKET)
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    except FileNotFoundError:
        raise ToolError("egress proxy unavailable in this environment")
    except socket.timeout:
        raise ToolError("request timed out after 30s")
    except OSError as exc:
        raise ToolError(f"egress proxy error: {exc}")

    try:
        response = json.loads(b"".join(chunks).decode("utf-8", "replace"))
    except ValueError:
        raise ToolError("malformed response from egress proxy")

    if error := response.get("error"):
        raise ToolError(error)

    status = response.get("status", 0)
    body = response.get("body", "")
    return _truncate(f"HTTP {status}\n\n{body}")


def read_file(path: str) -> str:
    """Read a UTF-8 file from the run workspace."""
    target = (WORKSPACE / path.lstrip("/")).resolve()

    # Belt and braces. The mount is read-only and the container is unprivileged,
    # but a traversal that escapes the workspace should still be an error rather
    # than something we quietly serve.
    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        raise ToolError("path escapes the workspace")

    if not target.exists():
        raise ToolError(f"no such file: {path}")
    if not target.is_file():
        raise ToolError(f"not a file: {path}")

    try:
        return _truncate(target.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise ToolError(str(exc))


def list_files(path: str = ".") -> str:
    """List the workspace, so the model can orient itself before reading."""
    target = (WORKSPACE / path.lstrip("/")).resolve()
    try:
        target.relative_to(WORKSPACE.resolve())
    except ValueError:
        raise ToolError("path escapes the workspace")
    if not target.is_dir():
        raise ToolError(f"not a directory: {path}")

    entries = []
    for entry in sorted(target.iterdir()):
        suffix = "/" if entry.is_dir() else ""
        size = "" if entry.is_dir() else f"  {entry.stat().st_size}b"
        entries.append(f"{entry.name}{suffix}{size}")
    return "\n".join(entries) or "(empty)"


REGISTRY: dict[str, Tool] = {
    "http_get": Tool(
        name="http_get",
        description=(
            "Fetch the contents of a URL over HTTP GET. Only hosts on the "
            "platform allowlist are reachable. Returns the status line and body."
        ),
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
            "required": ["url"],
        },
        fn=http_get,
    ),
    "read_file": Tool(
        name="read_file",
        description="Read a UTF-8 text file from the run workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to /workspace"}},
            "required": ["path"],
        },
        fn=read_file,
    ),
    "list_files": Tool(
        name="list_files",
        description="List the files and directories in the run workspace.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory, defaults to '.'"}},
        },
        fn=list_files,
    ),
}


def resolve(names: list[str]) -> list[Tool]:
    """Map requested tool names to registry entries, rejecting unknown ones."""
    resolved = []
    for name in names:
        tool = REGISTRY.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        resolved.append(tool)
    return resolved


def invoke(tool: Tool, args: dict[str, Any]) -> str:
    try:
        return tool.fn(**args)
    except ToolError:
        raise
    except TypeError as exc:
        raise ToolError(f"bad arguments for {tool.name}: {exc}")
    except Exception as exc:  # noqa: BLE001 — a tool crash must not kill the run
        raise ToolError(f"{tool.name} failed: {exc}")
