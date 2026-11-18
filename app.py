"""
Teamcenter MCP server: DEMO version with fake data, for hosting on Hugging Face Spaces.

Same 12 tool names as teamcenter_mcp.py, but nothing here talks to a real Teamcenter.
All data lives in memory and resets every time the Space restarts.

Endpoint:  https://<your-space>.hf.space/mcp
Access:    every request must carry the secret MCP_TOKEN, either as
             - header   Authorization: Bearer <MCP_TOKEN>
             - or URL   https://<your-space>.hf.space/mcp?key=<MCP_TOKEN>
Set MCP_TOKEN under the Space's Settings -> Variables and secrets -> New secret.
"""

from __future__ import annotations

import copy
import hmac
import os
from urllib.parse import parse_qs

import uvicorn
from mcp.server.mcpserver import MCPServer

# --------------------------------------------------------------------------
# Fake Teamcenter data
# --------------------------------------------------------------------------

def _rev(rid, name, status="", created="12-Nov-2026 16:17"):
    return {"item_revision_id": rid, "object_name": name, "object_desc": "",
            "release_status_list": status or None, "owning_user": "demo (demo)",
            "owning_group": "Engineering", "creation_date": created, "last_mod_date": created,
            "date_released": "", "datasets": [], "bom": []}

SEED = {
    "000043": {"object_name": "NewItem", "object_type": "Item",
               "revisions": [_rev("A", "NewItem", "Released"), _rev("B", "NewItem")]},
    "000044": {"object_name": "Bracket", "object_type": "Part",
               "revisions": [_rev("A", "Bracket", "Released")]},
    "000045": {"object_name": "Bolt M6x20", "object_type": "Part",
               "revisions": [_rev("A", "Bolt M6x20", "Released")]},
    "000046": {"object_name": "Housing Assembly", "object_type": "Part",
               "revisions": [_rev("A", "Housing Assembly")]},
}
SEED["000043"]["revisions"][1]["datasets"] = [
    {"object_name": "000043_B.pdf", "object_type": "PDF", "relation": "IMAN_specification"}]
SEED["000046"]["revisions"][0]["bom"] = [
    {"item_id": "000044", "revision": "A", "quantity": "2"},
    {"item_id": "000045", "revision": "A", "quantity": "4"}]
SEED["000046"]["revisions"][0]["datasets"] = [
    {"object_name": "000046_A.prt", "object_type": "UGMASTER", "relation": "IMAN_specification"},
    {"object_name": "000046_A.jt", "object_type": "DirectModel", "relation": "IMAN_Rendering"}]

DB = copy.deepcopy(SEED)
TEMPLATES = ["TCM Release Process", "Fast Track Release", "Change Notice Review"]
WORKFLOWS: list[dict] = []
_next_id = [47]


def _uid(item_id, rev=None):
    return f"DEMO_{item_id}" + (f"_{rev}" if rev else "")


def _find(item_id):
    item = DB.get(item_id)
    if not item:
        raise ValueError(f"Item {item_id} not found.")
    return item


def _get_rev(item_id, revision_id=""):
    item = _find(item_id)
    revs = item["revisions"]
    if not revision_id:
        return item, revs[-1]
    for r in revs:
        if r["item_revision_id"] == revision_id:
            return item, r
    raise ValueError(f"Revision {revision_id} not found. Available: {[r['item_revision_id'] for r in revs]}")


def _rev_view(item_id, item, r):
    v = {k: r[k] for k in ("item_revision_id", "object_name", "object_desc", "release_status_list",
                           "owning_user", "owning_group", "creation_date", "last_mod_date", "date_released")}
    return {"uid": _uid(item_id, r["item_revision_id"]), "item_id": item_id,
            "object_type": item["object_type"] + " Revision", **v}


def _uid_to_rev(uid):
    parts = uid.split("_")
    if len(parts) != 3 or parts[0] != "DEMO":
        raise ValueError("Unknown UID. Use a revision UID returned by get_item_revision.")
    return parts[1], parts[2]


def _next_rev(cur):
    if cur.isdigit():
        return str(int(cur) + 1).zfill(len(cur))
    chars = list(cur)
    i = len(chars) - 1
    while i >= 0:
        if chars[i] != "Z":
            chars[i] = chr(ord(chars[i]) + 1)
            return "".join(chars)
        chars[i] = "A"
        i -= 1
    return "A" + "".join(chars)


mcp = MCPServer("teamcenter-demo",
                instructions="DEMO Teamcenter with fake data (items 000043-000046). Changes reset when the server restarts.")

# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------

@mcp.tool()
def search_items(item_id: str = "*", name: str = "", max_results: int = 25,
                 query_name: str = "Item...") -> list[dict]:
    """Search Teamcenter items (DEMO data). Wildcards (*) are allowed, e.g. item_id="0000*".
    Returns uid, item_id, name, type, owner."""
    import fnmatch
    out = []
    for iid, it in DB.items():
        if item_id and not fnmatch.fnmatch(iid, item_id):
            continue
        if name and not fnmatch.fnmatch(it["object_name"].lower(), name.lower() if "*" in name else f"*{name.lower()}*"):
            continue
        out.append({"uid": _uid(iid), "item_id": iid, "object_name": it["object_name"],
                    "object_type": it["object_type"], "owning_user": "demo (demo)",
                    "last_mod_date": it["revisions"][-1]["last_mod_date"]})
    return out[:max_results]


@mcp.tool()
def get_properties(uid: str, properties: list[str]) -> dict:
    """Read properties (e.g. object_name, item_revision_id) of a revision by its UID (DEMO data)."""
    item_id, rid = _uid_to_rev(uid)
    item, r = _get_rev(item_id, rid)
    view = _rev_view(item_id, item, r)
    return {"uid": uid, "props": {p: view.get(p) for p in properties}}


@mcp.tool()
def get_item_revision(item_id: str, revision_id: str = "") -> dict:
    """Get an item revision by item ID. If revision_id is empty, returns the latest revision.
    Includes status, owner, dates and the revision UID for use with other tools."""
    item, r = _get_rev(item_id, revision_id)
    v = _rev_view(item_id, item, r)
    if not revision_id:
        v["all_revisions"] = [x["item_revision_id"] for x in item["revisions"]]
    return v


@mcp.tool()
def list_datasets(revision_uid: str) -> list[dict]:
    """List datasets (files) attached to an item revision (DEMO data)."""
    item_id, rid = _uid_to_rev(revision_uid)
    _, r = _get_rev(item_id, rid)
    return [dict(d, owning_user="demo (demo)") for d in r["datasets"]]


@mcp.tool()
def get_bom(revision_uid: str, revision_rule: str = "Latest Working") -> list[dict]:
    """Expand one level of the BOM under an item revision. Returns child item IDs,
    revisions, names and quantities. Try 000046 (Housing Assembly)."""
    item_id, rid = _uid_to_rev(revision_uid)
    _, r = _get_rev(item_id, rid)
    out = []
    for i, line in enumerate(r["bom"], 1):
        child = DB.get(line["item_id"], {})
        out.append({"bl_item_item_id": line["item_id"], "bl_rev_item_revision_id": line["revision"],
                    "bl_rev_object_name": child.get("object_name", "?"), "bl_quantity": line["quantity"],
                    "bl_sequence_no": str(i * 10), "revision_uid": _uid(line["item_id"], line["revision"])})
    return out

# --------------------------------------------------------------------------
# Write tools (fake data only, resets on restart)
# --------------------------------------------------------------------------

@mcp.tool()
def create_item(name: str, item_type: str = "Item", item_id: str = "",
                revision_id: str = "", description: str = "") -> dict:
    """Create a new item with its first revision (DEMO data). Leave item_id empty to get the next number.
    Confirm the name, type and ID with the user before calling."""
    if not item_id:
        while f"{_next_id[0]:06d}" in DB:
            _next_id[0] += 1
        item_id = f"{_next_id[0]:06d}"
    if item_id in DB:
        return {"error": f"Item {item_id} already exists."}
    r = _rev(revision_id or "A", name)
    r["object_desc"] = description
    DB[item_id] = {"object_name": name, "object_type": item_type, "revisions": [r]}
    return {"item_uid": _uid(item_id), **_rev_view(item_id, DB[item_id], r)}


@mcp.tool()
def revise_item(item_id: str, from_revision: str = "", new_revision_id: str = "",
                name: str = "", description: str = "") -> dict:
    """Create the next revision of an item (e.g. B -> C) (DEMO data). Confirm with the user before calling."""
    item, base = _get_rev(item_id, from_revision)
    new_id = new_revision_id or _next_rev(item["revisions"][-1]["item_revision_id"])
    if any(x["item_revision_id"] == new_id for x in item["revisions"]):
        return {"error": f"Revision {new_id} already exists."}
    r = copy.deepcopy(base)
    r.update(item_revision_id=new_id, release_status_list=None, date_released="",
             object_name=name or base["object_name"], object_desc=description or base["object_desc"])
    item["revisions"].append(r)
    return _rev_view(item_id, item, r)


@mcp.tool()
def update_properties(item_id: str, properties: dict[str, str], revision_id: str = "",
                      target: str = "revision") -> dict:
    """Change properties such as {"object_name": "...", "object_desc": "..."} on a revision (DEMO data).
    Released revisions can't be changed. Confirm the changes with the user before calling."""
    item, r = _get_rev(item_id, revision_id)
    if r["release_status_list"]:
        return {"error": f"{item_id}/{r['item_revision_id']} is released and can't be changed."}
    allowed = {"object_name", "object_desc"}
    bad = [k for k in properties if k not in allowed]
    if bad:
        return {"error": f"Demo only supports {sorted(allowed)}; not {bad}."}
    r.update(properties)
    return {"updated": _uid(item_id, r["item_revision_id"]), "now": {k: r[k] for k in properties}}


@mcp.tool()
def add_bom_line(parent_item_id: str, child_item_id: str, quantity: str = "1",
                 parent_revision_id: str = "", child_revision_id: str = "") -> dict:
    """Add a child item under a parent assembly's BOM with a quantity (DEMO data). Confirm with the user first."""
    _, pr = _get_rev(parent_item_id, parent_revision_id)
    _, cr = _get_rev(child_item_id, child_revision_id)
    if child_item_id == parent_item_id:
        return {"error": "An item can't contain itself."}
    pr["bom"].append({"item_id": child_item_id, "revision": cr["item_revision_id"], "quantity": quantity})
    return {"added": child_item_id, "under": parent_item_id, "quantity": quantity}


@mcp.tool()
def remove_bom_line(parent_item_id: str, child_item_id: str, parent_revision_id: str = "") -> dict:
    """Remove a child item from a parent assembly's BOM (DEMO data). Confirm with the user first."""
    _, pr = _get_rev(parent_item_id, parent_revision_id)
    before = len(pr["bom"])
    pr["bom"] = [l for l in pr["bom"] if l["item_id"] != child_item_id]
    removed = before - len(pr["bom"])
    if not removed:
        return {"error": f"{child_item_id} is not in the BOM of {parent_item_id}."}
    return {"removed": child_item_id, "lines_removed": removed, "from": parent_item_id}


@mcp.tool()
def list_workflow_templates() -> list[str]:
    """List the workflow templates available to start (DEMO data)."""
    return TEMPLATES


@mcp.tool()
def start_workflow(item_id: str, template_name: str, revision_id: str = "",
                   process_name: str = "", description: str = "") -> dict:
    """Submit an item revision to a workflow (DEMO). In this demo, "TCM Release Process" and
    "Fast Track Release" mark the revision Released immediately. Confirm with the user first."""
    if template_name not in TEMPLATES:
        return {"error": f"Unknown template. Available: {TEMPLATES}"}
    item, r = _get_rev(item_id, revision_id)
    if "Release" in template_name:
        r["release_status_list"] = "Released"
        r["date_released"] = "today (demo)"
    WORKFLOWS.append({"item": item_id, "rev": r["item_revision_id"], "template": template_name})
    return {"started": template_name, "on": f"{item_id}/{r['item_revision_id']}",
            "process_uids": [f"DEMO_PROC_{len(WORKFLOWS)}"]}

# --------------------------------------------------------------------------
# HTTP app with a token check
# --------------------------------------------------------------------------

TOKEN = os.environ.get("MCP_TOKEN", "")


class RequireToken:
    """Rejects any request without the right token (header or ?key=). The home page '/' stays open."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path in ("/", ""):
            body = (b"Teamcenter MCP demo is running. Connect your MCP client to /mcp "
                    b"(fake data, token required).")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            return await send({"type": "http.response.body", "body": body})
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        given = ""
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        if not given:
            given = (parse_qs(scope.get("query_string", b"").decode()).get("key") or [""])[0]
        if not TOKEN or not hmac.compare_digest(given, TOKEN):
            msg = b"Missing or wrong token." if TOKEN else b"Server has no MCP_TOKEN secret set, so all requests are refused."
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            return await send({"type": "http.response.body", "body": msg})
        return await self.app(scope, receive, send)


app = RequireToken(mcp.streamable_http_app(host="0.0.0.0", stateless_http=True))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")),
                proxy_headers=True, forwarded_allow_ips="*")
