#!/usr/bin/env python3
"""
Convert a ComfyUI UI-format workflow (the file you save from the graph editor)
into API format (the flat node-id keyed JSON that /prompt accepts).

ComfyUI itself only performs this conversion in the browser (app.graphToPrompt),
so this reimplements it in Python using node definitions from /object_info.

Usage:
    python ui_to_api.py <ui_workflow.json> [-o out.json] [--host 127.0.0.1] [--port 8188]
    python ui_to_api.py <ui_workflow.json> --object-info-cache oi.json
    python ui_to_api.py <ui_workflow.json> --explain

Exit codes:
    0  converted (and written if -o given)
    1  conversion failed
"""

import argparse
import json
import sys
import urllib.request
from pathlib import Path

# Types that occupy a slot in widgets_values (as opposed to being link inputs).
WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "LIST", "COMBO"}

# Nodes whose output is a terminal artifact. Used as roots for reachability.
OUTPUT_NODE_TYPES = {
    "SaveImage", "PreviewImage", "SaveAnimatedWEBP", "SaveAnimatedPNG",
    "SaveVideo", "VHS_VideoCombine", "SaveWEBM", "SaveAudio", "SaveAudioMP3",
    "SaveAudioOpus", "SaveLatent", "PreviewAudio", "SaveImageWebsocket",
}


def is_output_node(ntype):
    """
    Output detection by name pattern, so third-party savers such as
    `Image Saver` (was-node-suite) or `SaveImageWithMetadata` also count.
    """
    if not isinstance(ntype, str):
        return False
    if ntype in OUTPUT_NODE_TYPES:
        return True
    low = ntype.lower()
    return low.startswith("save") or low.startswith("preview") or " saver" in low


def is_widget_type(typ):
    """Widget inputs hold a literal value; everything else is a link."""
    if not isinstance(typ, str):
        return False
    return typ in WIDGET_TYPES or typ.startswith("COMFY_")

# Front-end-only nodes that never reach the API graph.
SKIP_TYPES = {
    "Note", "MarkdownNote", "Reroute", "PrimitiveNode",
    "Bookmark (rgthree)", "Fast Groups Bypasser (rgthree)",
    "Fast Groups Muter (rgthree)", "Label (rgthree)", "Node Collector (rgthree)",
    "Image Comparer (rgthree)", "Mute / Bypass Repeater (rgthree)",
}


class ConvertError(Exception):
    pass


# ---------------------------------------------------------------------------
# Node spec helpers
# ---------------------------------------------------------------------------

def load_object_info(host, port, cache_path=None):
    if cache_path and Path(cache_path).exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    url = f"http://{host}:{port}/object_info"
    with urllib.request.urlopen(url, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if cache_path:
        Path(cache_path).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    return data


def ordered_inputs(object_info, node_type):
    """Return [(name, type, meta, section)] in declaration order."""
    spec = object_info.get(node_type)
    if spec is None:
        return None
    result = []
    for section in ("required", "optional"):
        for name, raw in (spec.get("input", {}).get(section) or {}).items():
            if isinstance(raw, list) and raw:
                typ = raw[0]
                # Legacy format: the first element is the options list itself.
                if isinstance(typ, list):
                    typ, meta = "COMBO", {"options": typ}
                else:
                    meta = raw[1] if len(raw) > 1 and isinstance(raw[1], dict) else {}
            else:
                typ, meta = raw, {}
            result.append((name, typ, meta, section))
    return result


def coerce_value(val, typ):
    """
    Normalise a widget value to the declared type. UI files store everything as
    JSON scalars, so "720" for an INT would otherwise be submitted as a string.
    """
    if not isinstance(typ, str):
        return val
    if typ == "INT":
        if isinstance(val, bool):
            return int(val)
        if isinstance(val, str):
            try:
                return int(float(val))
            except ValueError:
                return val
        return val
    if typ == "FLOAT":
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                return val
        return val
    if typ == "BOOLEAN":
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes", "on")
        return bool(val)
    return val


# ---------------------------------------------------------------------------
# Link resolution
# ---------------------------------------------------------------------------

def build_link_map(ui):
    """link_id -> [link_id, origin_node, origin_slot, target_node, target_slot, type]"""
    return {l[0]: l for l in ui.get("links", []) if isinstance(l, list) and len(l) >= 5}


def resolve_source(link_id, links, nodes, bypassed=frozenset(), depth=0):
    """
    Resolve a link to either ['node_id', slot] or a literal value
    (when the source is a PrimitiveNode). Walks through Reroutes and
    through bypassed nodes (whose same-type input is passed through).
    """
    if depth > 32:
        raise ConvertError("链接解析层数过深，可能存在循环")
    link = links.get(link_id)
    if not link:
        return None
    src_id, src_slot = link[1], link[2]
    src = nodes.get(src_id)
    if src is None:
        return None

    stype = src.get("type")
    if stype == "Reroute":
        ups = src.get("inputs") or []
        if ups and ups[0].get("link") is not None:
            return resolve_source(ups[0]["link"], links, nodes, bypassed, depth + 1)
        return None

    if stype == "PrimitiveNode":
        wv = src.get("widgets_values")
        if isinstance(wv, list) and wv:
            return wv[0]
        return None

    if src_id in bypassed:
        has_link_inputs = any(
            (i.get("link") is not None) for i in (src.get("inputs") or [])
        )
        if not has_link_inputs:
            # This node gets restored rather than forwarded, so treat it normally.
            return [str(src_id), src_slot]
        # Bypassed node: forward the input whose type matches the link type.
        want = link[5] if len(link) > 5 else None
        for inp in src.get("inputs") or []:
            if inp.get("type") == want and inp.get("link") is not None:
                return resolve_source(inp["link"], links, nodes, bypassed, depth + 1)
        return [str(src_id), src_slot]

    return [str(src_id), src_slot]


def reachable_set(ui, links):
    """
    Node ids reachable from any output node by walking inputs backwards.
    Returns None when the graph has no recognizable output node.
    """
    nodes = {n["id"]: n for n in ui["nodes"]}
    roots = [n["id"] for n in ui["nodes"] if is_output_node(n.get("type"))]
    if not roots:
        return None
    seen, stack = set(), list(roots)
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        node = nodes.get(nid)
        if not node:
            continue
        for inp in node.get("inputs") or []:
            lid = inp.get("link")
            if lid is None:
                continue
            link = links.get(lid)
            if link:
                stack.append(link[1])
    return seen


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

def convert(ui, object_info, explain=False):
    if not isinstance(ui, dict) or not isinstance(ui.get("nodes"), list):
        raise ConvertError("不是 UI 格式工作流（缺少 nodes 数组）")

    nodes = {n["id"]: n for n in ui["nodes"]}
    links = build_link_map(ui)
    api = {}
    report = []

    reach = reachable_set(ui, links)
    bypassed = {n["id"] for n in ui["nodes"] if n.get("mode") == 4}
    muted = {n["id"] for n in ui["nodes"] if n.get("mode") == 2}

    for node in ui["nodes"]:
        nid = node["id"]
        ntype = node.get("type")

        if ntype in SKIP_TYPES:
            report.append(f"跳过前端节点 {nid} ({ntype})")
            continue

        if reach is not None and nid not in reach:
            report.append(f"跳过孤立节点 {nid} ({ntype})（未连到任何输出）")
            continue

        if nid in muted:
            report.append(f"跳过静音节点 {nid} ({ntype})")
            continue

        if nid in bypassed:
            has_link_inputs = any(
                (i.get("link") is not None) for i in (node.get("inputs") or [])
            )
            if is_output_node(ntype):
                if has_link_inputs:
                    report.append(f"恢复被 bypass 的输出节点 {nid} ({ntype})")
                else:
                    report.append(f"跳过被 bypass 且未接线的输出节点 {nid} ({ntype})")
                    continue
            elif not has_link_inputs:
                # Pure-widget node (e.g. a detector provider). It has no upstream
                # to forward, so bypassing it would strand every consumer.
                report.append(f"恢复被 bypass 的无上游节点 {nid} ({ntype})")
            else:
                report.append(f"跳过 bypass 节点 {nid} ({ntype})，其链接向上游透传")
                continue

        spec_inputs = ordered_inputs(object_info, ntype)
        if spec_inputs is None:
            raise ConvertError(
                f"节点 {nid} 的类型 '{ntype}' 不在 /object_info 中。"
                f"该自定义节点可能未安装，或需要重启 ComfyUI 重新注册。"
            )

        inputs = {}

        # 1) Link inputs
        for inp in node.get("inputs") or []:
            name = inp.get("name")
            lid = inp.get("link")
            if name is None or lid is None:
                continue
            src = resolve_source(lid, links, nodes, bypassed)
            if src is not None:
                inputs[name] = src

        # 2) Widget inputs
        wv = node.get("widgets_values")
        if isinstance(wv, dict):
            for key, val in wv.items():
                if key not in inputs:
                    inputs[key] = val
        elif isinstance(wv, list):
            wi = 0
            for name, typ, meta, _sec in spec_inputs:
                if not is_widget_type(typ):
                    continue
                if name in inputs:
                    continue
                if wi >= len(wv):
                    if "default" in meta:
                        inputs[name] = meta["default"]
                    continue
                inputs[name] = coerce_value(wv[wi], typ)
                wi += 1
                # seed-like INTs carry an extra "fixed"/"randomize" widget
                if meta.get("control_after_generate"):
                    wi += 1
                # LoadImage appends an upload-type widget after the filename
                if meta.get("image_upload"):
                    if wi < len(wv) and isinstance(wv[wi], str) and wv[wi] in ("image", "video", "audio"):
                        wi += 1

        api[str(nid)] = {"class_type": ntype, "inputs": inputs}
        if explain:
            report.append(f"{nid} {ntype}: {list(inputs.keys())}")

    # Validate required inputs are present
    problems = []
    for nid, node in api.items():
        spec_inputs = ordered_inputs(object_info, node["class_type"])
        for name, typ, meta, section in spec_inputs:
            if section != "required":
                continue
            # autogrow 输入在 API 里是 "images.image_1" 这种点号键，只要有任意一个子键即算满足
            if isinstance(typ, str) and typ.startswith("COMFY_AUTOGROW"):
                if any(k.startswith(f"{name}.") for k in node["inputs"]):
                    continue
            if name not in node["inputs"]:
                problems.append(f"节点 {nid} ({node['class_type']}) 缺少必填输入 '{name}'")

    if problems:
        raise ConvertError("转换结果不完整：\n  " + "\n  ".join(problems[:20]))

    return api, report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="ComfyUI UI 格式工作流转 API 格式")
    p.add_argument("workflow", help="UI 格式工作流 JSON 路径")
    p.add_argument("-o", "--output", default=None, help="输出 API JSON 路径")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8188")
    p.add_argument("--object-info-cache", default=None,
                   help="object_info 缓存文件（存在则直接读取）")
    p.add_argument("--explain", action="store_true", help="打印每个节点的映射结果")
    args = p.parse_args()

    src = Path(args.workflow)
    try:
        ui = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"错误：读取工作流失败：{e}", file=sys.stderr)
        sys.exit(1)

    try:
        oi = load_object_info(args.host, args.port, args.object_info_cache)
    except Exception as e:
        print(f"错误：无法获取 /object_info（ComfyUI 是否在运行？）：{e}", file=sys.stderr)
        sys.exit(1)

    try:
        api, report = convert(ui, oi, explain=args.explain)
    except ConvertError as e:
        print(f"转换失败：{e}", file=sys.stderr)
        sys.exit(1)

    if args.explain:
        for line in report:
            print(f"  {line}")

    print(f"\n转换成功：{len(api)} 个节点")

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(api, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已写入：{out}")
    else:
        print(json.dumps(api, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
