"""Synthetic, dependency-free MCP server for the pinned Pi transport check."""

import json
import os
import signal
import sys
from pathlib import Path

capture = Path(sys.argv[1])
state = {
    "pid": os.getpid(), "args": sys.argv[2:], "methods": [], "requests": [],
    "env": {name: os.environ.get(name) for name in (
        "ELECTRON_RUN_AS_NODE", "T3_ACP_MCP_ENDPOINT", "T3_ACP_MCP_AUTHORIZATION",
    )},
}
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    for line in sys.stdin:
        request = json.loads(line)
        method = request["method"]
        state["methods"].append(method)
        state["requests"].append(request)
        capture.write_text(json.dumps(state))
        if "id" not in request:
            continue
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-stdio", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "fixture_echo", "description": "Synthetic echo",
                "inputSchema": {
                    "type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"], "additionalProperties": False,
                },
            }]}
        elif method == "tools/call":
            result = {"content": [{
                "type": "text", "text": "echo:" + request["params"]["arguments"]["text"],
            }], "isError": False}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"],
                              "error": {"code": -32601, "message": "Unknown method"}}),
                  flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
finally:
    state["closed"] = True
    capture.write_text(json.dumps(state))
