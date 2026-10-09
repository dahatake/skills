#!/usr/bin/env python3
"""Validate the portable Agent Plugins manifests without third-party packages."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
PLUGIN_KEYS = {
    "$schema",
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "extensions",
}
NAME_PATTERN = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
SKILL_NAME_PATTERN = re.compile(r"^(?!.*--)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
SKILL_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name}: cannot read valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: top-level value must be an object")
    return value


def require_string(document: dict[str, Any], field: str, source: str) -> None:
    if field in document and not isinstance(document[field], str):
        raise ValueError(f"{source}: {field} must be a string")


def validate_plugin() -> None:
    manifest = load_object(ROOT / "plugin.json")
    unknown = sorted(set(manifest) - PLUGIN_KEYS)
    if unknown:
        raise ValueError(f"plugin.json: unknown top-level fields: {', '.join(unknown)}")
    if manifest.get("$schema") != PLUGIN_SCHEMA:
        raise ValueError(f"plugin.json: $schema must be {PLUGIN_SCHEMA}")

    name = manifest.get("name")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or NAME_PATTERN.fullmatch(name) is None
    ):
        raise ValueError("plugin.json: name does not satisfy Agent Plugins 1.0.0")

    for field in ("version", "description", "homepage", "repository", "license"):
        require_string(manifest, field, "plugin.json")

    author = manifest.get("author")
    if author is not None:
        if not isinstance(author, dict) or set(author) - {"name", "email", "url"}:
            raise ValueError("plugin.json: author must contain only name, email, and url")
        for field in author:
            require_string(author, field, "plugin.json author")

    keywords = manifest.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list)
        or any(not isinstance(keyword, str) for keyword in keywords)
    ):
        raise ValueError("plugin.json: keywords must be an array of strings")

    extensions = manifest.get("extensions")
    if extensions is not None and (
        not isinstance(extensions, dict)
        or any(not isinstance(value, dict) for value in extensions.values())
    ):
        raise ValueError("plugin.json: every extensions value must be an object")


def validate_server(name: str, server: Any) -> None:
    if not isinstance(server, dict):
        raise ValueError(f"mcp.json: server {name!r} must be an object")

    server_type = server.get("type")
    fields = {
        "stdio": ({"type", "command", "args", "env", "cwd"}, {"type", "command"}),
        "streamable-http": ({"type", "url", "headers"}, {"type", "url"}),
        "sse": ({"type", "url", "headers"}, {"type", "url"}),
    }
    if server_type not in fields:
        raise ValueError(f"mcp.json: server {name!r} has an unknown type")

    allowed, required = fields[server_type]
    if set(server) - allowed or not required <= set(server):
        raise ValueError(f"mcp.json: server {name!r} has invalid fields")
    for field in required - {"type"}:
        require_string(server, field, f"mcp.json server {name!r}")

    if server_type == "stdio":
        args = server.get("args")
        if args is not None and (
            not isinstance(args, list) or any(not isinstance(arg, str) for arg in args)
        ):
            raise ValueError(f"mcp.json: server {name!r} args must be strings")
        env = server.get("env")
        if env is not None and (
            not isinstance(env, dict)
            or any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items())
        ):
            raise ValueError(f"mcp.json: server {name!r} env must map strings to strings")
        require_string(server, "cwd", f"mcp.json server {name!r}")
    else:
        headers = server.get("headers")
        if headers is not None and (
            not isinstance(headers, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in headers.items()
            )
        ):
            raise ValueError(f"mcp.json: server {name!r} headers must map strings to strings")


def validate_mcp() -> None:
    config = load_object(ROOT / "mcp.json")
    if set(config) != {"$schema", "mcpServers"}:
        raise ValueError("mcp.json: top-level fields must be $schema and mcpServers")
    if config["$schema"] != MCP_SCHEMA:
        raise ValueError(f"mcp.json: $schema must be {MCP_SCHEMA}")
    servers = config["mcpServers"]
    if not isinstance(servers, dict):
        raise ValueError("mcp.json: mcpServers must be an object")
    for name, server in servers.items():
        validate_server(name, server)


def frontmatter_fields(path: Path) -> dict[str, list[str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{path}: cannot read valid UTF-8 text: {exc}") from exc
    if not lines or lines[0] != "---":
        raise ValueError(f"{path}: YAML frontmatter must start with ---")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{path}: YAML frontmatter is not closed") from exc

    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[1:end]:
        match = re.match(r"^([a-z][a-z0-9-]*):(.*)$", line)
        if match:
            current = match.group(1)
            fields[current] = [match.group(2).strip()]
        elif current is not None:
            fields[current].append(line)
    return fields


def scalar_value(lines: list[str]) -> str:
    first = lines[0]
    if first in {">", "|", ">-", "|-"}:
        return " ".join(line.strip() for line in lines[1:] if line.strip())
    return first.strip().strip("\"'")


def validate_skills() -> None:
    skills_dir = ROOT / "skills"
    for directory in sorted(path for path in skills_dir.iterdir() if path.is_dir()):
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            continue
        fields = frontmatter_fields(skill_file)
        unknown = sorted(set(fields) - SKILL_KEYS)
        if unknown:
            raise ValueError(f"{skill_file}: unknown frontmatter fields: {', '.join(unknown)}")

        name = scalar_value(fields.get("name", [""]))
        if (
            name != directory.name
            or not 1 <= len(name) <= 64
            or SKILL_NAME_PATTERN.fullmatch(name) is None
        ):
            raise ValueError(f"{skill_file}: name must match its valid parent directory name")

        description = scalar_value(fields.get("description", [""]))
        if not 1 <= len(description) <= 1024:
            raise ValueError(f"{skill_file}: description must contain 1-1024 characters")

        compatibility = fields.get("compatibility")
        if compatibility is not None and not 1 <= len(scalar_value(compatibility)) <= 500:
            raise ValueError(f"{skill_file}: compatibility must contain 1-500 characters")


def main() -> int:
    try:
        validate_plugin()
        validate_mcp()
        validate_skills()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("Agent Plugins manifests conform to the 1.0.0 schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
