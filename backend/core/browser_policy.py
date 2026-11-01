"""Read browser policy state in an isolated world, outside page-owned JS prototypes."""
import json


async def _collection_frames(session):
    """Get real frame worlds and closed roots through CDP, including OOP frames.

    No hooks are injected into the page's world and no JS builtins from it are used.
    Object handles are scoped to one read and released by collection_evaluate.
    """
    main = await session.get_or_create_cdp_session()
    sessions = {main.session_id: main}
    manager = getattr(session, "session_manager", None)
    if manager:
        sessions.update(manager.get_all_sessions())
    frames, parents = {}, {}
    for cdp in list(sessions.values()):
        try:
            tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
            doc = await cdp.cdp_client.send.DOM.getDocument(params={"depth": -1, "pierce": True}, session_id=cdp.session_id)
        except Exception:
            continue
        def tree_walk(node, parent=None):
            frame = node["frame"]["id"]
            parent = parent or node["frame"].get("parentId")
            if parent:
                parents[frame] = parent
            for child in node.get("childFrames", []):
                tree_walk(child, frame)
        tree_walk(tree["frameTree"])
        roots = {}
        top_id = tree["frameTree"]["frame"]["id"]
        def dom_walk(node, frame):
            if node.get("nodeType") == 9:
                frame = node.get("frameId", frame)
            for shadow in node.get("shadowRoots", []):
                if shadow.get("shadowRootType") != "user-agent":
                    roots.setdefault(frame, []).append(shadow["backendNodeId"])
                    dom_walk(shadow, frame)
            for child in node.get("children", []):
                dom_walk(child, frame)
            if node.get("contentDocument"):
                dom_walk(node["contentDocument"], node.get("frameId", frame))
        dom_walk(doc["root"], top_id)
        def frame_ids(node):
            yield node["frame"]["id"]
            for child in node.get("childFrames", []):
                yield from frame_ids(child)
        for frame in frame_ids(tree["frameTree"]):
            if frame in frames:
                continue
            try:
                world = await cdp.cdp_client.send.Page.createIsolatedWorld(
                    params={"frameId": frame, "worldName": "langhire-collection"}, session_id=cdp.session_id)
                objects = []
                for backend_id in roots.get(frame, []):
                    resolved = await cdp.cdp_client.send.DOM.resolveNode(
                        params={"backendNodeId": backend_id, "executionContextId": world["executionContextId"]}, session_id=cdp.session_id)
                    objects.append(resolved["object"]["objectId"])
                frames[frame] = {"cdp": cdp, "context": world["executionContextId"], "roots": objects}
            except Exception:
                continue  # detached/OOP frame; its own session is tried next
    return main, frames, parents


async def _frame_call(frame, function, args, opacity=1):
    cdp = frame["cdp"]
    # Pass closed roots as genuine object handles, never serialized page values.
    declaration = "function(...values){const roots=values.splice(" + str(len(args)) + ");return (" + function + ")(...values,roots,"+str(float(opacity))+")}"
    result = await cdp.cdp_client.send.Runtime.callFunctionOn(params={
        "executionContextId": frame["context"], "functionDeclaration": declaration,
        "arguments": [{"value": arg} for arg in args] + [{"objectId": obj} for obj in frame["roots"]],
        "returnByValue": True}, session_id=cdp.session_id)
    return _value(result)


async def _frame_offset(frame_id, frames, parents):
    if frame_id not in parents:
        return []
    parent_id = parents[frame_id]
    if parent_id not in frames:
        return None
    parent = frames[parent_id]
    cdp = parent["cdp"]
    owner = await cdp.cdp_client.send.DOM.getFrameOwner(params={"frameId": frame_id}, session_id=cdp.session_id)
    node = await cdp.cdp_client.send.DOM.resolveNode(params={"backendNodeId": owner["backendNodeId"],
        "executionContextId": parent["context"]}, session_id=cdp.session_id)
    obj = node["object"]["objectId"]
    try:
        result = await cdp.cdp_client.send.Runtime.callFunctionOn(params={"objectId": obj, "returnByValue": True,
            "functionDeclaration": r'''function(){
              let opacity=1;
              if(['hidden','collapse'].includes(getComputedStyle(this).visibility)) return null;
              for(let e=this;e;e=e.parentElement || e.getRootNode()?.host){
                const s=getComputedStyle(e);opacity*=Number(s.opacity);
                if(s.display==='none'||opacity<.05||e.hidden||e.inert||e.getAttribute('aria-hidden')==='true') return null;
              }
              const r=this.getBoundingClientRect();
              if(r.width<2||r.height<2||!this.clientWidth||!this.clientHeight) return null;
              return {width:this.clientWidth,height:this.clientHeight,opacity};
            }'''}, session_id=cdp.session_id)
        offset = _value(result)
    finally:
        await cdp.cdp_client.send.Runtime.releaseObject(params={"objectId": obj}, session_id=cdp.session_id)
    if offset is None:
        return None
    box = await cdp.cdp_client.send.DOM.getBoxModel(params={"backendNodeId": owner["backendNodeId"]}, session_id=cdp.session_id)
    offset["quad"] = box["model"]["content"]
    offset.update(parent=parent_id, child=frame_id, owner=owner["backendNodeId"])
    outer = await _frame_offset(parent_id, frames, parents)
    return None if outer is None else [offset] + outer


def _project(point, offset):
    """Map a frame viewport point through its actual CSS content quadrilateral."""
    u,v=point[0]/offset["width"],point[1]/offset["height"]
    x0,y0,x1,y1,x2,y2,x3,y3=offset["quad"]
    dx1,dx2,dx3=x1-x2,x3-x2,x0-x1+x2-x3
    dy1,dy2,dy3=y1-y2,y3-y2,y0-y1+y2-y3
    if abs(dx3)+abs(dy3)<1e-8:
        g=h=0
    else:
        den=dx1*dy2-dx2*dy1
        if abs(den)<1e-8:
            raise ValueError("Collapsed frame transform")
        g,h=(dx3*dy2-dx2*dy3)/den,(dx1*dy3-dx3*dy1)/den
    den=g*u+h*v+1
    return ((x1-x0+g*x1)*u+(x3-x0+h*x3)*v+x0)/den, ((y1-y0+g*y1)*u+(y3-y0+h*y3)*v+y0)/den


async def collection_evaluate(session, function, *args, scroll=False):
    """Read all frames/closed roots; verify child visibility in the top viewport.

    Nested frame scrolls finish before their containing page scrolls, so a long
    embedded list isn't skipped when the outer page moves past its viewport.
    """
    if not hasattr(session, "get_or_create_cdp_session"):
        return await evaluate(session, function, *args)
    main, frames, parents = await _collection_frames(session)
    top_tree = await main.cdp_client.send.Page.getFrameTree(session_id=main.session_id)
    top_id = top_tree["frameTree"]["frame"]["id"]
    merged = {"cards": [], "covered": 0, "hidden": 0, "unreadable_frames": 0}
    async def visible(frame_id, points, offsets):
        for point in points:
            x, y = point
            clear = True
            for offset in offsets:
                x, y = _project((x,y), offset)
                parent = frames[offset["parent"]]["cdp"]
                try:
                    hit = await parent.cdp_client.send.DOM.getNodeForLocation(
                        params={"x": int(x), "y": int(y), "includeUserAgentShadowDOM": False}, session_id=parent.session_id)
                    if hit.get("backendNodeId") != offset["owner"] and hit.get("frameId") != offset["child"]:
                        clear = False
                        break
                except Exception:
                    clear = False
                    break
            if clear:
                return True
        return False
    try:
        ordered = sorted(frames, key=lambda key: key == top_id) if scroll else list(frames)
        for frame_id in ordered:
            frame = frames[frame_id]
            try:
                offsets = await _frame_offset(frame_id, frames, parents) if frame_id != top_id else []
                if offsets is None:
                    continue
                opacity = 1
                for offset in offsets:
                    opacity *= offset["opacity"]
                if opacity < .05:
                    continue
                if scroll and offsets:
                    size = await _frame_call(frame, "() => [innerWidth,innerHeight]", [])
                    if not await visible(frame_id, [[size[0]/2,size[1]/2],[5,5]], offsets):
                        continue
                value = await _frame_call(frame, function, list(args), opacity=opacity)
                if scroll:
                    if value:
                        return True
                    continue
                if frame_id == top_id:
                    merged.update({key: value.get(key) for key in ("url", "title", "password", "blocker")})
                merged["covered"] += value.get("covered", 0)
                merged["hidden"] += value.get("hidden", 0)
                for card in value.get("cards", []):
                    points = card.pop("_points", [])
                    if frame_id == top_id or await visible(frame_id, points, offsets):
                        if (not args or len(args)<2 or len(merged["cards"])<args[1]) and not any(c["id"]==card["id"] for c in merged["cards"]):
                            merged["cards"].append(card)
            except Exception:
                merged["unreadable_frames"] += 1
                continue  # an independently navigating frame cannot invalidate other results
        return False if scroll else merged
    finally:
        for frame in frames.values():
            for obj in frame["roots"]:
                try:
                    await frame["cdp"].cdp_client.send.Runtime.releaseObject(params={"objectId": obj}, session_id=frame["cdp"].session_id)
                except Exception:
                    pass


async def _context(session, node=None):
    cdp = (await session.cdp_client_for_node(node) if node is not None
           else await session.get_or_create_cdp_session())
    frame = getattr(node, "frame_id", None) if node is not None else None
    if not frame:
        tree = await cdp.cdp_client.send.Page.getFrameTree(session_id=cdp.session_id)
        frame = tree["frameTree"]["frame"]["id"]
    result = await cdp.cdp_client.send.Page.createIsolatedWorld(
        params={"frameId": frame, "worldName": "langhire-policy"}, session_id=cdp.session_id)
    return cdp, result["executionContextId"]


def _value(result):
    if result.get("exceptionDetails"):
        raise RuntimeError("The browser policy state could not be read")
    return (result.get("result") or {}).get("value")


async def evaluate(session, function, *args):
    # Lightweight unit-test doubles expose only a fake Page; real BrowserSession
    # always has get_or_create_cdp_session and never takes this fallback.
    if not hasattr(session, "get_or_create_cdp_session"):
        page = await session.get_current_page()
        return await page.evaluate(function, *args)
    cdp, context_id = await _context(session)
    expression = "(" + function + ")(..." + json.dumps(list(args)) + ")"
    result = await cdp.cdp_client.send.Runtime.evaluate(
        params={"expression": expression, "contextId": context_id, "returnByValue": True},
        session_id=cdp.session_id)
    return _value(result)


async def at_point(session, x, y, function, fallback):
    """`function` run on what a click at viewport point (x, y) hits, found by the browser's own hit
    test, which sees into closed shadow roots (a page script's elementFromPoint stops at their host).
    Test doubles without CDP run `fallback(x, y)` in the page instead."""
    if not hasattr(session, "get_or_create_cdp_session"):
        page = await session.get_current_page()
        return await page.evaluate(fallback, x, y)
    cdp = await session.get_or_create_cdp_session()
    hit = await cdp.cdp_client.send.DOM.getNodeForLocation(
        params={"x": int(x), "y": int(y), "includeUserAgentShadowDOM": False}, session_id=cdp.session_id)
    world = await cdp.cdp_client.send.Page.createIsolatedWorld(
        params={"frameId": hit["frameId"], "worldName": "langhire-policy"}, session_id=cdp.session_id)
    resolved = await cdp.cdp_client.send.DOM.resolveNode(
        params={"backendNodeId": hit["backendNodeId"], "executionContextId": world["executionContextId"]},
        session_id=cdp.session_id)
    result = await cdp.cdp_client.send.Runtime.callFunctionOn(
        params={"objectId": resolved["object"]["objectId"], "functionDeclaration": function,
                "returnByValue": True}, session_id=cdp.session_id)
    return _value(result)


async def hosts_closed_shadow(session, element_js) -> bool:
    """Whether the element `element_js` returns hosts a closed shadow root (then an element inside it,
    which page scripts can't see, may be the one that has focus). False for test doubles without CDP."""
    if not hasattr(session, "get_or_create_cdp_session"):
        return False
    cdp, context_id = await _context(session)
    result = await cdp.cdp_client.send.Runtime.evaluate(
        params={"expression": "(" + element_js + ")()", "contextId": context_id, "returnByValue": False},
        session_id=cdp.session_id)
    if result.get("exceptionDetails"):
        raise RuntimeError("The focused element could not be read")
    object_id = (result.get("result") or {}).get("objectId")
    if not object_id:
        return False
    node = (await cdp.cdp_client.send.DOM.describeNode(params={"objectId": object_id, "depth": 1},
                                                       session_id=cdp.session_id))["node"]
    return any(root.get("shadowRootType") == "closed" for root in node.get("shadowRoots") or [])


async def call_on_node(session, node, function, *args):
    cdp, context_id = await _context(session, node)
    resolved = await cdp.cdp_client.send.DOM.resolveNode(
        params={"backendNodeId": node.backend_node_id, "executionContextId": context_id},
        session_id=cdp.session_id)
    result = await cdp.cdp_client.send.Runtime.callFunctionOn(
        params={"objectId": resolved["object"]["objectId"], "functionDeclaration": function,
                "arguments": [{"value": arg} for arg in args], "returnByValue": True},
        session_id=cdp.session_id)
    return _value(result)
