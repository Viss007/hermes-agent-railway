"""Apply the Railway Hermes MCP import compatibility patch to pinned WebUI sources."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

COMMANDS_OLD = """            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _servers, _lock"""
COMMANDS_NEW = """            from tools.mcp_tool import shutdown_mcp_servers, _servers, _lock
            try:  # moved out of tools.mcp_tool in the packaged agent
                from tools.mcp_tool_discovery import discover_mcp_tools
            except ImportError:
                from tools.mcp_tool import discover_mcp_tools"""
STREAMING_OLD = """            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()"""
STREAMING_NEW = """            # discover_mcp_tools moved out of tools.mcp_tool into
            # tools.mcp_tool_discovery in the packaged agent; the old import
            # raised ImportError here, was swallowed below, and every WebUI
            # turn silently ran with zero MCP tools. Try the new module first.
            try:
                from tools.mcp_tool_discovery import discover_mcp_tools
            except ImportError:
                from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()"""

def _patch(source: str, old: str, new: str, marker: str, label: str) -> tuple[str, bool]:
    if marker in source:
        return source, False
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one pinned upstream anchor, found {count}; refusing to patch")
    return source.replace(old, new, 1), True

def _atomic_write(path: Path, content: str) -> None:
    info = path.stat()
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, stat.S_IMODE(info.st_mode))
        try:
            os.chown(tmp_name, info.st_uid, info.st_gid)
        except PermissionError:
            pass
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes-webui").resolve(strict=True)
    commands = root / "api" / "commands.py"
    streaming = root / "api" / "streaming.py"
    for path in (commands, streaming):
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Expected regular upstream source file: {path}")

    original_commands = commands.read_text(encoding="utf-8")
    original_streaming = streaming.read_text(encoding="utf-8")
    patched_commands, commands_changed = _patch(
        original_commands, COMMANDS_OLD, COMMANDS_NEW,
        "from tools.mcp_tool_discovery import discover_mcp_tools",
        "api/commands.py",
    )
    patched_streaming, streaming_changed = _patch(
        original_streaming, STREAMING_OLD, STREAMING_NEW,
        "discover_mcp_tools moved out of tools.mcp_tool",
        "api/streaming.py",
    )

    if commands_changed:
        _atomic_write(commands, patched_commands)
    if streaming_changed:
        _atomic_write(streaming, patched_streaming)

    state = "applied" if commands_changed or streaming_changed else "already applied"
    print(f"[webui-mcp-patch] {state}: commands.py={'patched' if commands_changed else 'already correct'}, streaming.py={'patched' if streaming_changed else 'already correct'}")

if __name__ == "__main__":
    main()
