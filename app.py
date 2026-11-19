"""
Teamcenter MCP relay: the CLOUD half (deploy on Render).

Gemini / Claude  --HTTPS-->  /mcp  (this server)  <--HTTPS long-poll--  agent.py on your PC  -->  Teamcenter

This server never talks to Teamcenter. Each tool call is put in a queue; the agent on your
PC picks it up (it polls /agent/next over normal outbound HTTPS), runs it against your
Teamcenter, and posts the answer to /agent/result.

Environment variables (set in Render -> Environment):
  MCP_TOKEN    secret for AI apps:  https://<app>.onrender.com/mcp?key=<MCP_TOKEN>
               (or header  Authorization: Bearer <MCP_TOKEN>)
  AGENT_TOKEN  a DIFFERENT secret, only for your agent.py
"""

from __future__ import annotations

import asyncio
import hmac
import itertools
import os
import time
from typing import Any
from urllib.parse import parse_qs

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
TOOL_TIMEOUT = float(os.environ.get("TOOL_TIMEOUT", "90"))
AGENT_STALE_AFTER = 75  # seconds without a poll = agent considered offline

mcp = MCPServer(
    "teamcenter",
    instructions="Teamcenter tools. Data comes live from the user's own Teamcenter through their agent.",
)

# --------------------------------------------------------------------------
# The relay: a queue of jobs for the agent, and futures waiting for results
# --------------------------------------------------------------------------

_jobs: asyncio.Queue | None = None
_waiting: dict[str, asyncio.Future] = {}
_ids = itertools.count(1)
_agent_last_seen = 0.0


def _queue() -> asyncio.Queue:
    global _jobs
    if _jobs is None:
        _jobs = asyncio.Queue()
    return _jobs


def agent_online() -> bool:
    return time.time() - _agent_last_seen < AGENT_STALE_AFTER


async def relay(tool: str, args: dict[str, Any]) -> Any:
    """Send one tool call to the agent and wait for its answer."""
    if not agent_online():
        return {"error": "The Teamcenter agent is offline. Start agent.py on the PC that can reach Teamcenter, then try again."}
    job_id = str(next(_ids))
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _waiting[job_id] = fut
    await _queue().put({"id": job_id, "tool": tool, "args": args})
    try:
        reply = await asyncio.wait_for(fut, TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": f"No answer from the agent within {int(TOOL_TIMEOUT)} s."}
    finally:
        _waiting.pop(job_id, None)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "Agent reported an error."))
    return reply.get("result")


@mcp.custom_route("/agent/next", methods=["GET"])
async def agent_next(request: Request) -> Response:
    """Long-poll: the agent waits here up to ~25 s for the next job."""
    global _agent_last_seen
    _agent_last_seen = time.time()
    wait = min(float(request.query_params.get("wait", "25")), 50)
    try:
        job = await asyncio.wait_for(_queue().get(), wait)
    except asyncio.TimeoutError:
        _agent_last_seen = time.time()
        return Response(status_code=204)
    if job["id"] not in _waiting:          # caller already gave up
        return Response(status_code=204)
    return JSONResponse(job)


@mcp.custom_route("/agent/result", methods=["POST"])
async def agent_result(request: Request) -> Response:
    global _agent_last_seen
    _agent_last_seen = time.time()
    data = await request.json()
    fut = _waiting.get(str(data.get("id")))
    if fut and not fut.done():
        fut.set_result(data)
    return JSONResponse({"ok": True})


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request) -> Response:
    state = "online" if agent_online() else "offline"
    return PlainTextResponse(f"Teamcenter MCP relay is running. Agent: {state}. MCP endpoint: /mcp")

# --------------------------------------------------------------------------
# Tools: same names and parameters as teamcenter_mcp.py; each one is relayed
# --------------------------------------------------------------------------

@mcp.tool()
async def search_items(item_id: str = "*", name: str = "", max_results: int = 25,
                       query_name: str = "Item...") -> Any:
    """Search Teamcenter items with a saved query (default "Item...").
    Wildcards (*) are allowed, e.g. item_id="PRT-10*". Returns uid, type, item_id, name, owner."""
    return await relay("search_items", dict(item_id=item_id, name=name, max_results=max_results, query_name=query_name))


@mcp.tool()
async def get_properties(uid: str, properties: list[str]) -> Any:
    """Read any properties (by internal name, e.g. object_name, item_revision_id)
    of a Teamcenter object identified by its UID."""
    return await relay("get_properties", dict(uid=uid, properties=properties))


@mcp.tool()
async def get_item_revision(item_id: str, revision_id: str = "") -> Any:
    """Get an item revision by item ID. If revision_id is empty, returns the latest revision.
    Includes status, owner, dates and the revision UID for use with other tools."""
    return await relay("get_item_revision", dict(item_id=item_id, revision_id=revision_id))


@mcp.tool()
async def list_datasets(revision_uid: str) -> Any:
    """List datasets (CAD files, PDFs, etc.) attached to an item revision."""
    return await relay("list_datasets", dict(revision_uid=revision_uid))


@mcp.tool()
async def get_bom(revision_uid: str, revision_rule: str = "Latest Working") -> Any:
    """Expand one level of the BOM under an item revision. Returns child item IDs,
    revisions, names and quantities. Use a child's revision uid to go deeper."""
    return await relay("get_bom", dict(revision_uid=revision_uid, revision_rule=revision_rule))


@mcp.tool()
async def create_item(name: str, item_type: str = "Item", item_id: str = "",
                      revision_id: str = "", description: str = "") -> Any:
    """Create a new Teamcenter item with its first revision. Leave item_id empty to let
    Teamcenter assign it. Always confirm name, type and ID with the user before calling."""
    return await relay("create_item", dict(name=name, item_type=item_type, item_id=item_id,
                                           revision_id=revision_id, description=description))


@mcp.tool()
async def revise_item(item_id: str, from_revision: str = "", new_revision_id: str = "",
                      name: str = "", description: str = "") -> Any:
    """Create the next revision of an item (e.g. B -> C). Confirm with the user before calling."""
    return await relay("revise_item", dict(item_id=item_id, from_revision=from_revision,
                                           new_revision_id=new_revision_id, name=name, description=description))


@mcp.tool()
async def update_properties(item_id: str, properties: dict[str, str], revision_id: str = "",
                            target: str = "revision") -> Any:
    """Change properties (e.g. {"object_desc": "..."}) on an item revision (latest unless
    revision_id is given) or on the item (target="item"). Confirm changes with the user first."""
    return await relay("update_properties", dict(item_id=item_id, properties=properties,
                                                 revision_id=revision_id, target=target))


@mcp.tool()
async def add_bom_line(parent_item_id: str, child_item_id: str, quantity: str = "1",
                       parent_revision_id: str = "", child_revision_id: str = "") -> Any:
    """Add a child item under a parent assembly's BOM with a quantity. Confirm with the user first."""
    return await relay("add_bom_line", dict(parent_item_id=parent_item_id, child_item_id=child_item_id,
                                            quantity=quantity, parent_revision_id=parent_revision_id,
                                            child_revision_id=child_revision_id))


@mcp.tool()
async def remove_bom_line(parent_item_id: str, child_item_id: str, parent_revision_id: str = "") -> Any:
    """Remove a child item from a parent assembly's BOM. Confirm with the user first."""
    return await relay("remove_bom_line", dict(parent_item_id=parent_item_id, child_item_id=child_item_id,
                                               parent_revision_id=parent_revision_id))


@mcp.tool()
async def list_workflow_templates() -> Any:
    """List the workflow template names available to start, e.g. "TCM Release Process"."""
    return await relay("list_workflow_templates", {})


@mcp.tool()
async def start_workflow(item_id: str, template_name: str, revision_id: str = "",
                         process_name: str = "", description: str = "") -> Any:
    """Submit an item revision to a workflow, e.g. "TCM Release Process". Confirm with the user first."""
    return await relay("start_workflow", dict(item_id=item_id, template_name=template_name,
                                              revision_id=revision_id, process_name=process_name,
                                              description=description))

# --------------------------------------------------------------------------
# Token checks: AI apps use MCP_TOKEN on /mcp, the agent uses AGENT_TOKEN on /agent/*
# --------------------------------------------------------------------------

def _given_token(scope) -> str:
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (parse_qs(scope.get("query_string", b"").decode()).get("key") or [""])[0]


class Guard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "") in ("", "/"):
            return await self.app(scope, receive, send)
        path = scope["path"]
        need = AGENT_TOKEN if path.startswith("/agent/") else MCP_TOKEN
        given = _given_token(scope)
        if not need or not hmac.compare_digest(given, need):
            msg = b"Missing or wrong token." if need else b"Token not configured on the server."
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": msg})
        return await self.app(scope, receive, send)


app = Guard(mcp.streamable_http_app(host="0.0.0.0", stateless_http=True))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
