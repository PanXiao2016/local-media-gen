#!/usr/bin/env python3
"""
API Format -> UI Format 转换器。

ComfyUI 的图形编辑器只认 UI Format（{nodes, links, version, extra}），而
/prompt 只认 API Format。本脚本把可提交的 API 工作流转回可打开的图形工作流，
用于把 skill 里验证过的链路同步到 ComfyUI 桌面版。

验证方式：转换后可用 ui_to_api.py 转回 API，对比节点与连线是否等价。

用法：
  python api_to_ui.py -w assets/krea2_t2i.json -o out.json
  python api_to_ui.py -w assets/krea2_t2i.json --title "Krea2 文生图" -o out.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui_to_api import (  # noqa: E402
    ConvertError,
    is_widget_type,
    load_object_info,
    ordered_inputs,
)

# 布局参数
COL_W = 380
ROW_H = 220
NODE_W = 320


def _default_for(typ, meta):
    if "default" in meta:
        return meta["default"]
    if typ == "INT":
        return meta.get("min", 0)
    if typ == "FLOAT":
        return float(meta.get("min", 0.0))
    if typ == "BOOLEAN":
        return False
    if typ == "STRING":
        return ""
    opts = meta.get("options")
    if opts:
        return opts[0]
    return ""


def _topo_order(api):
    """按依赖做拓扑排序，供布局使用。"""
    deps = {}
    for nid, node in api.items():
        deps[nid] = {
            src for val in node["inputs"].values()
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str)
            for src in [val[0]]
        }
    order, seen = [], set()

    def visit(n, stack=()):
        if n in seen or n in stack:
            return
        for d in sorted(deps.get(n, ())):
            visit(d, stack + (n,))
        if n not in seen:
            seen.add(n)
            order.append(n)

    for n in sorted(api, key=lambda x: int(x) if x.isdigit() else 0):
        visit(n)
    return order


def _depth(api, order):
    """每个节点到源头的最大距离，用于分列。"""
    depth = {}
    for nid in order:
        d = 0
        for val in api[nid]["inputs"].values():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                d = max(d, depth.get(val[0], 0) + 1)
        depth[nid] = d
    return depth


def build_ui(api, object_info, title=""):
    order = _topo_order(api)
    depth = _depth(api, order)

    nodes = []
    links = []
    link_id = 0
    # (origin_id, origin_slot) -> [link_id, ...]
    out_links = {}
    # (target_id, input_name) -> link_id
    in_links = {}

    # 先建节点骨架，确定输入槽顺序
    node_index = {}
    for nid in order:
        node = api[nid]
        ntype = node["class_type"]
        spec = ordered_inputs(object_info, ntype)
        if spec is None:
            raise ConvertError(
                f"节点 {nid} 的类型 '{ntype}' 不在 /object_info 中（自定义节点未安装？）"
            )

        link_entries = []   # [{name,type}]
        widget_values = []
        for name, typ, meta, _sec in spec:
            # autogrow（COMFY_AUTOGROW_V3）：API 里的键是 "images.image_1" 这种点号形式，
            # 前端把它渲染成多个独立输入槽。必须按前缀展开，不能当成单个 widget。
            if isinstance(typ, str) and typ.startswith("COMFY_AUTOGROW"):
                for key in node["inputs"]:
                    if key.startswith(f"{name}."):
                        link_entries.append({"name": key, "type": "IMAGE"})
                continue

            val = node["inputs"].get(name)
            is_link = isinstance(val, list) and len(val) == 2 and isinstance(val[0], str)
            if is_link:
                link_entries.append({"name": name, "type": typ if isinstance(typ, str) else "*"})
                # widget 被转成输入时，widgets_values 里仍保留一个值
                if is_widget_type(typ):
                    widget_values.append(_default_for(typ, meta))
                    if meta.get("control_after_generate"):
                        widget_values.append("fixed")
            elif is_widget_type(typ):
                widget_values.append(val if val is not None else _default_for(typ, meta))
                if meta.get("control_after_generate"):
                    widget_values.append("fixed")
                if meta.get("image_upload"):
                    widget_values.append("image")
            # 非 widget 且无连线 → 忽略（可选输入未接）

        outputs_spec = object_info[ntype].get("output") or []
        names = object_info[ntype].get("output_name") or outputs_spec
        outputs = []
        for i, otype in enumerate(outputs_spec):
            nm = names[i] if i < len(names) else f"out{i}"
            outputs.append({
                "name": nm,
                "type": otype if isinstance(otype, str) else "*",
                "links": [],
                "slot_index": i,
            })

        d = depth[nid]
        row = sum(1 for x in nodes if depth.get(str(x["id"]), 0) == d)
        node_obj = {
            "id": int(nid) if nid.isdigit() else len(nodes) + 1,
            "type": ntype,
            "pos": [d * COL_W, row * ROW_H],
            "size": [NODE_W, 60 + 24 * max(1, len(widget_values))],
            "flags": {},
            "order": len(nodes),
            "mode": 0,
            "inputs": link_entries,
            "outputs": outputs,
            "properties": {"Node name for S&R": ntype},
            "widgets_values": widget_values,
        }
        nodes.append(node_obj)
        node_index[nid] = node_obj

    # 建立连线
    for nid in order:
        node = api[nid]
        target = node_index[nid]
        for slot, entry in enumerate(target["inputs"]):
            val = node["inputs"].get(entry["name"])
            if not (isinstance(val, list) and len(val) == 2 and isinstance(val[0], str)):
                continue
            src_id, src_slot = val[0], int(val[1])
            src = node_index.get(src_id)
            if src is None:
                raise ConvertError(f"节点 {nid} 的输入 '{entry['name']}' 指向不存在的节点 {src_id}")
            link_id += 1
            ltype = entry["type"]
            links.append([link_id, src["id"], src_slot, target["id"], slot, ltype])
            # UI 格式要求每个输入槽回写自己的 link id，否则读回来时连线丢失
            target["inputs"][slot]["link"] = link_id
            out_links.setdefault((src_id, src_slot), []).append(link_id)
            if src_slot < len(src["outputs"]):
                src["outputs"][src_slot]["links"].append(link_id)

    max_id = max((n["id"] for n in nodes), default=0)
    return {
        "last_node_id": max_id,
        "last_link_id": link_id,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {"ds": {"scale": 0.75, "offset": [0, 0]}, "title": title},
        "version": 0.4,
    }


def main():
    ap = argparse.ArgumentParser(description="API Format -> UI Format 转换")
    ap.add_argument("-w", "--workflow", required=True, help="API 格式工作流 JSON")
    ap.add_argument("-o", "--output", required=True, help="输出 UI 格式 JSON")
    ap.add_argument("--title", default="", help="工作流标题")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--object-info", help="离线 /object_info JSON（不给则从服务器取）")
    args = ap.parse_args()

    api = json.load(open(args.workflow, encoding="utf-8"))
    if "nodes" in api:
        raise ConvertError("输入已经是 UI 格式")

    oi = load_object_info(args.host, args.port, cache_path=args.object_info)
    ui = build_ui(api, oi, title=args.title or os.path.basename(args.workflow))

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    json.dump(ui, open(args.output, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"已写出 {args.output}")
    print(f"  节点 {len(ui['nodes'])} 个，连线 {len(ui['links'])} 条")
    for n in ui["nodes"]:
        print(f"    [{n['id']:>2}] {n['type']:<32} in={len(n['inputs'])} widget={len(n['widgets_values'])}")


if __name__ == "__main__":
    try:
        main()
    except ConvertError as e:
        print(f"转换失败：{e}", file=sys.stderr)
        sys.exit(1)
