"""Fathom MCP server — generic adapter that reads tools from the API.

Connects to any Fathom instance (self-hosted or cloud). Discovers
available tools from GET /v1/tools, filtered by the token's scopes.

Reads FATHOM_API_URL and FATHOM_API_KEY from environment.
Run: python server.py
"""
from __future__ import annotations

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("FATHOM_API_URL", "http://localhost:8201")
API_KEY = os.environ.get("FATHOM_API_KEY", "")

mcp = FastMCP(
    "Fathom",
    instructions=(
        "Fathom is a personal memory lake. Use these tools to search, write, "
        "and query the user's lake of memories — fragments of thought, research, "
        "conversations, photos, and experience. Search before answering. "
        "Follow threads: if a result mentions something unfamiliar, search for that too."
    ),
)


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def _client() -> httpx.Client:
    return httpx.Client(base_url=API_URL, headers=_headers(), timeout=30)


def _format_results(data, key: str = "results") -> str:
    """Format API response into readable text for the LLM."""
    # Handle search results (nested under .delta)
    items = data if isinstance(data, list) else data.get(key, data.get("deltas", []))
    if not items:
        return "No results."

    lines = [f"{len(items)} results:\n"]
    for raw in items:
        d = raw.get("delta", raw) if isinstance(raw, dict) and "delta" in raw else raw
        ts = (d.get("timestamp") or "")[:16]
        tags = ", ".join((d.get("tags") or [])[:4])
        src = d.get("source", "")
        content = (d.get("content") or "")[:400]
        media = f" [image: {d['media_hash']}]" if d.get("media_hash") else ""
        lines.append(f"[{ts} · {src} · {tags}]{media}\n{content}\n")
    return "\n".join(lines)


# ── Dynamic tool registration from /v1/tools ─────


def _execute_tool(tool_def: dict, args: dict) -> str:
    """Execute a tool by calling its endpoint on the consumer API."""
    endpoint = tool_def["endpoint"]
    method = endpoint["method"]
    path = endpoint["path"]

    # Map tool argument names to API parameter names
    request_map = tool_def.get("request_map", {})
    mapped = {}
    for arg_name, value in args.items():
        api_name = request_map.get(arg_name, arg_name)
        mapped[api_name] = value

    with _client() as c:
        if method == "POST":
            r = c.post(path, json=mapped)
        elif method == "GET":
            # For GET, arrays need special handling
            params = {}
            for k, v in mapped.items():
                if isinstance(v, list):
                    params[k] = ",".join(str(i) for i in v)
                elif v is not None:
                    params[k] = v
            r = c.get(path, params=params)
        else:
            return f"Unsupported method: {method}"

        r.raise_for_status()
        data = r.json()

    # Format based on endpoint type
    if path == "/v1/search":
        return _format_results(data)
    elif path == "/v1/deltas" and method == "POST":
        return f"Written. ID: {data.get('id', '?')}"
    elif path == "/v1/deltas" and method == "GET":
        return _format_results(data)
    elif path == "/v1/stats":
        return f"Lake: {data.get('total', '?')} deltas, {data.get('embedded', '?')} embedded ({data.get('percent', '?')}% coverage)"
    elif path == "/v1/chat/completions":
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return json.dumps(data, indent=2)[:2000]
    else:
        return json.dumps(data, indent=2)[:2000]


def _register_tools():
    """Fetch tool definitions from the API and register them as MCP tools."""
    try:
        with _client() as c:
            r = c.get("/v1/tools")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        # Fall back: register a single "lake_stats" so the server isn't empty
        @mcp.tool()
        def connection_error() -> str:
            """Could not connect to Fathom API."""
            return f"Failed to connect to {API_URL}: {e}"
        return

    tools = data.get("tools", [])
    for tool_def in tools:
        _register_one(tool_def)


def _register_one(tool_def: dict):
    """Register a single tool definition as an MCP tool."""
    name = tool_def["name"]
    desc = tool_def["description"]
    params = tool_def.get("parameters", {})
    props = params.get("properties", {})
    required = params.get("required", [])

    # Build the function dynamically
    def make_handler(td):
        def handler(**kwargs) -> str:
            return _execute_tool(td, kwargs)
        handler.__name__ = td["name"]
        handler.__doc__ = td["description"]

        # Build type annotations from JSON Schema for FastMCP
        annotations = {}
        for pname, pschema in props.items():
            ptype = pschema.get("type", "string")
            if ptype == "string":
                annotations[pname] = str
            elif ptype == "integer":
                annotations[pname] = int
            elif ptype == "array":
                annotations[pname] = list[str]
            else:
                annotations[pname] = str

            # Set defaults for optional params
            if pname not in required:
                default = pschema.get("default")
                if default is not None:
                    handler.__defaults__ = handler.__defaults__ or ()
                    # Can't easily set per-param defaults dynamically,
                    # so we'll use None and let the API handle defaults
                    pass

        handler.__annotations__ = annotations
        return handler

    fn = make_handler(tool_def)
    mcp.tool()(fn)


# Register tools at import time
_register_tools()


if __name__ == "__main__":
    mcp.run(transport="stdio")
