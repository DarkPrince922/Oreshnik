"""Codex's MCP tools for a gateway that takes plain function tools only.

Checked with the real Codex 0.169: it offers an MCP server's tools to the model as one `{"type": "namespace", "name": "mcp__<server>",
"tools": [<function tools>]}` per server, and expects the model's call back as `{"type": "function_call", "namespace": "mcp__<server>",
"name": "<tool>", ...}` (a plain `mcp__<server>__<tool>` name is refused: "unsupported call"). A gateway that is not OpenAI's own API
refuses the namespace ("This endpoint supports function tools only"). So, between the two, each namespace is unfolded into plain function
tools named `<namespace>__<tool>` on the way out, and the model's calls of those names are folded back into the namespace form on the way
in — in the streamed events and in the history Codex sends with the next request.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_NAME = 64           # what OpenAI-style function names allow; longer ones get a short hash instead of their tail
_BAD = re.compile(r"[^A-Za-z0-9_-]")


def flat_name(namespace: str, name: str) -> str:
    flat = _BAD.sub("_", f"{namespace}__{name}")
    if len(flat) > MAX_NAME:
        flat = flat[: MAX_NAME - 9] + "_" + hashlib.sha256(flat.encode()).hexdigest()[:8]
    return flat


def unfold_request(body: dict) -> tuple[dict, dict[str, tuple[str, str]]]:
    """The request with every namespace tool unfolded and the history's namespaced calls renamed; plus flat name -> (namespace, name)."""
    tools = body.get("tools")
    mapping: dict[str, tuple[str, str]] = {}
    if isinstance(tools, list):
        out = []
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
                ns = str(tool.get("name", ""))
                for inner in tool["tools"]:
                    if isinstance(inner, dict) and inner.get("type") == "function" and inner.get("name"):
                        flat = flat_name(ns, str(inner["name"]))
                        mapping[flat] = (ns, str(inner["name"]))
                        out.append({**inner, "name": flat})
            else:
                out.append(tool)
        body = {**body, "tools": out}
    items = body.get("input")
    if isinstance(items, list):
        fixed = []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call" and item.get("namespace"):
                ns, name = str(item["namespace"]), str(item.get("name", ""))
                flat = flat_name(ns, name)
                mapping.setdefault(flat, (ns, name))
                item = {k: v for k, v in item.items() if k != "namespace"} | {"name": flat}
            fixed.append(item)
        body = {**body, "input": fixed}
    return body, mapping


def fold_item(item: Any, mapping: dict[str, tuple[str, str]]) -> Any:
    if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name") in mapping and not item.get("namespace"):
        ns, name = mapping[item["name"]]
        return {**item, "namespace": ns, "name": name}
    return item


def fold_event(event: dict, mapping: dict[str, tuple[str, str]]) -> dict:
    """One streamed event (or a whole non-streamed response) with the model's calls of unfolded tools put back into namespace form."""
    if "item" in event:
        event = {**event, "item": fold_item(event["item"], mapping)}
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("output"), list):
        event = {**event, "response": {**response, "output": [fold_item(i, mapping) for i in response["output"]]}}
    if isinstance(event.get("output"), list):
        event = {**event, "output": [fold_item(i, mapping) for i in event["output"]]}
    return event


def fold_sse_line(line: str, mapping: dict[str, tuple[str, str]]) -> str:
    if not mapping or not line.startswith("data:") or "function_call" not in line:
        return line
    try:
        event = json.loads(line[5:].strip())
    except ValueError:
        return line
    if not isinstance(event, dict):
        return line
    return "data: " + json.dumps(fold_event(event, mapping), ensure_ascii=False, separators=(",", ":"))
