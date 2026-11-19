"""
Teamcenter MCP relay: the CLOUD half (deploy on Render). Multi-user, self-service keys.

Gemini / Claude  --HTTPS-->  /mcp  (this server)  <--HTTPS long-poll--  agent on each person's PC  -->  their Teamcenter

This server never talks to Teamcenter. Every person has their own key pair. A request made
with person A's Gemini key is queued ONLY for the agent holding person A's agent key.

Environment variables (Render -> Environment):
  MASTER_SECRET  long random secret. Keys are signed with it, so no list of keys is stored.
                 Changing it cancels EVERY key (everyone runs setup again).
  INVITE_CODE    code you give your team. tc-mcp.exe uses it once to get that person's keys.
  REVOKED        optional, comma-separated ids to block, e.g.  ravi-7k2m9q,old-laptop-x1y2z3
                 (each person's id is shown at the end of their setup and in tc-mcp.log)
  USERS          optional, old style  name:MCP_TOKEN:AGENT_TOKEN;...   still accepted.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import re
import secrets
import itertools
import os
import time
from typing import Any
from urllib.parse import parse_qs

import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

TOOL_TIMEOUT = float(os.environ.get("TOOL_TIMEOUT", "90"))
AGENT_STALE_AFTER = 40  # agents poll every <=25 s; silent longer than this = offline
PICKUP_TIMEOUT = 35     # a job not picked up this fast means the agent has gone


def _load_users() -> tuple[dict, dict]:
    """Returns (mcp_token -> user, agent_token -> user)."""
    by_mcp, by_agent = {}, {}
    for entry in os.environ.get("USERS", "").split(";"):
        parts = [x.strip() for x in entry.split(":")]
        if len(parts) == 3 and all(parts):
            name, mt, at = parts
            by_mcp[mt], by_agent[at] = name, name
    if os.environ.get("MCP_TOKEN") and os.environ.get("AGENT_TOKEN"):
        by_mcp.setdefault(os.environ["MCP_TOKEN"], "default")
        by_agent.setdefault(os.environ["AGENT_TOKEN"], "default")
    return by_mcp, by_agent


USERS_BY_MCP, USERS_BY_AGENT = _load_users()

MASTER_SECRET = os.environ.get("MASTER_SECRET", "")
INVITE_CODE = os.environ.get("INVITE_CODE", "")
REVOKED = {x.strip() for x in os.environ.get("REVOKED", "").split(",") if x.strip()}
_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


def _sign(kind: str, uid: str) -> str:
    mac = hmac.new(MASTER_SECRET.encode(), f"{kind}:{uid}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:32]


def make_key(kind: str, uid: str) -> str:
    return f"{kind}.{uid}.{_sign(kind, uid)}"


def check_signed(token: str, kind: str) -> str | None:
    """Returns the person's id if `token` is a valid, non-revoked key of this kind."""
    if not MASTER_SECRET or token.count(".") != 2:
        return None
    k, uid, sig = token.split(".")
    if k != kind or not _ID_RE.match(uid) or uid in REVOKED:
        return None
    return uid if hmac.compare_digest(sig, _sign(kind, uid)) else None


def who(token: str, kind: str) -> str | None:
    if not token:
        return None
    uid = check_signed(token, kind)
    if uid:
        return uid
    table = USERS_BY_AGENT if kind == "agent" else USERS_BY_MCP
    return next((u for t, u in table.items() if hmac.compare_digest(token, t)), None)

current_user: contextvars.ContextVar[str] = contextvars.ContextVar("current_user", default="")

mcp = MCPServer(
    "teamcenter",
    instructions="Teamcenter tools. Data comes live from the caller's own Teamcenter through their agent.",
)

# --------------------------------------------------------------------------
# Relay: per user, a queue of jobs for their agent, and futures waiting for results
# --------------------------------------------------------------------------

_queues: dict[str, asyncio.Queue] = {}
_waiting: dict[str, tuple[str, asyncio.Future]] = {}   # job id -> (user, future)
_picked: dict[str, asyncio.Event] = {}                  # job id -> set when the agent takes it
_ids = itertools.count(1)
_last_seen: dict[str, float] = {}


def _queue(user: str) -> asyncio.Queue:
    if user not in _queues:
        _queues[user] = asyncio.Queue()
    return _queues[user]


def agent_online(user: str) -> bool:
    return time.time() - _last_seen.get(user, 0) < AGENT_STALE_AFTER


async def relay(tool: str, args: dict[str, Any]) -> Any:
    """Send one tool call to THIS caller's agent and wait for its answer."""
    user = current_user.get()
    if not user:
        return {"error": "Unknown caller."}
    if not agent_online(user):
        return {"error": "Your Teamcenter agent is offline. Start tc-mcp.exe (or agent.py) on the PC that can reach your Teamcenter, then try again."}
    job_id = str(next(_ids))
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _waiting[job_id] = (user, fut)
    _picked[job_id] = asyncio.Event()
    await _queue(user).put({"id": job_id, "tool": tool, "args": args})
    try:
        try:
            await asyncio.wait_for(_picked[job_id].wait(), PICKUP_TIMEOUT)
        except asyncio.TimeoutError:
            _last_seen.pop(user, None)
            return {"error": "Your Teamcenter agent is offline (it did not pick up the request). Start tc-mcp.exe on your PC, then try again."}
        reply = await asyncio.wait_for(fut, TOOL_TIMEOUT)
    except asyncio.TimeoutError:
        return {"error": f"Your agent took the request but gave no answer within {int(TOOL_TIMEOUT)} s (Teamcenter may be slow)."}
    finally:
        _waiting.pop(job_id, None)
        _picked.pop(job_id, None)
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "Agent reported an error."))
    result = reply.get("result")
    # Lists go back as ONE object: a bare list becomes one content block per element,
    # and some clients (e.g. Gemini) only read the first block.
    if isinstance(result, list):
        return {"count": len(result), "results": result}
    return result


@mcp.custom_route("/agent/next", methods=["GET"])
async def agent_next(request: Request) -> Response:
    """Long-poll: an agent waits here up to ~25 s for ITS user's next job."""
    user = current_user.get()
    _last_seen[user] = time.time()
    wait = min(float(request.query_params.get("wait", "25")), 50)
    try:
        job = await asyncio.wait_for(_queue(user).get(), wait)
    except asyncio.TimeoutError:
        _last_seen[user] = time.time()
        return Response(status_code=204)
    if job["id"] not in _waiting:          # caller already gave up
        return Response(status_code=204)
    if await request.is_disconnected():    # the agent went away while waiting: give the job back
        _queue(user).put_nowait(job)
        _last_seen.pop(user, None)
        return Response(status_code=204)
    _picked[job["id"]].set()
    return JSONResponse(job)


@mcp.custom_route("/agent/result", methods=["POST"])
async def agent_result(request: Request) -> Response:
    user = current_user.get()
    _last_seen[user] = time.time()
    data = await request.json()
    entry = _waiting.get(str(data.get("id")))
    if entry and entry[0] == user and not entry[1].done():   # an agent can only answer its own user's jobs
        entry[1].set_result(data)
    return JSONResponse({"ok": True})


@mcp.custom_route("/register", methods=["POST"])
async def register(request: Request) -> Response:
    """Self-service: name + invite code -> this person's own key pair.
    A random suffix makes every registration a new identity, so registering with
    someone else's name never reaches their agent."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "Send JSON: {name, invite}"}, status_code=400)
    if not MASTER_SECRET or not INVITE_CODE:
        return JSONResponse({"error": "Sign-up is not enabled on this relay."}, status_code=403)
    if not hmac.compare_digest(str(data.get("invite", "")), INVITE_CODE):
        await asyncio.sleep(1.5)  # slow down guessing
        return JSONResponse({"error": "Wrong invite code."}, status_code=403)
    base = re.sub(r"[^a-z0-9_-]", "-", str(data.get("name", "")).strip().lower())[:24].strip("-") or "user"
    uid = f"{base}-{secrets.token_hex(3)}"
    return JSONResponse({"id": uid, "mcp_key": make_key("mcp", uid), "agent_key": make_key("agent", uid)})


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request) -> Response:
    online = sum(1 for u in _last_seen if agent_online(u))
    return PlainTextResponse(f"Teamcenter MCP relay is running. Agents online: {online}. MCP endpoint: /mcp")


@mcp.custom_route("/agent/whoami", methods=["GET"])
async def agent_whoami(request: Request) -> Response:
    """Lets an agent check its key and show its own status."""
    uid = current_user.get()
    return JSONResponse({"id": uid, "online": agent_online(uid)})

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
# Token check: the token decides WHO is calling (and so which agent gets the job)
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
        if scope["type"] != "http" or scope.get("path", "") in ("", "/", "/register"):
            return await self.app(scope, receive, send)
        kind = "agent" if scope["path"].startswith("/agent/") else "mcp"
        user = who(_given_token(scope), kind)
        if not user:
            msg = b"Missing, wrong or revoked key."
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain")]})
            return await send({"type": "http.response.body", "body": msg})
        tok = current_user.set(user)
        try:
            return await self.app(scope, receive, send)
        finally:
            current_user.reset(tok)


app = Guard(mcp.streamable_http_app(host="0.0.0.0", stateless_http=True))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
