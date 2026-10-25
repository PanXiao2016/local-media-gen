#!/usr/bin/env python3
"""
ComfyUI Local Media Generator

Submits a ComfyUI API-format workflow to a local ComfyUI instance, polls for
completion, and downloads the generated images/videos.

Parameter handling is driven by an optional paired template file
(`<workflow>_tp.json`) that declares which nodes/inputs are exposed as friendly
parameters. When no template exists, the script falls back to auto-detecting a
CLIPTextEncode node for the prompt.

Usage:
    python comfyui_generate.py --workflow assets/text_to_image.json \
        --prompt "a cute cat on a windowsill, soft sunlight"

    python comfyui_generate.py --workflow assets/text_to_video.json \
        --prompt "ocean waves" --param num_frames=24 --param fps=12 \
        --max-wait 1800

    python comfyui_generate.py --workflow assets/text_to_image.json --list-params

    python comfyui_generate.py --workflow assets/text_to_image.json \
        --prompt "test" --dry-run

Environment:
    COMFYUI_HOST  - ComfyUI host (default: 127.0.0.1)
    COMFYUI_PORT  - ComfyUI port (default: 8188)
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workflow_template import (  # noqa: E402
    TemplateError,
    WorkflowTemplate,
    find_template_for,
    fuse,
    list_templates,
)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _http_get(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return json.loads(data.decode("utf-8"))
        return data


def _http_post(url, payload, timeout=30):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"ComfyUI 拒绝请求（HTTP {e.code}）：\n{_format_validation_error(detail)}"
        ) from None


def _format_validation_error(raw):
    """
    Turn ComfyUI's node_errors JSON into a readable list.

    Raw shape:
        {"error": {...}, "node_errors": {"105": {"errors": [{"details": "..."}],
                                                "class_type": "LoadImage"}}}
    """
    try:
        data = json.loads(raw)
    except Exception:
        return raw[:1500]

    lines = []
    top = data.get("error", {})
    if top.get("message"):
        lines.append(f"  {top['message']}")

    for node_id, node in (data.get("node_errors") or {}).items():
        ctype = node.get("class_type", "?")
        for err in node.get("errors", []):
            msg = err.get("details") or err.get("message") or ""
            lines.append(f"  节点 {node_id} ({ctype}): {msg}")

    return "\n".join(lines) if lines else raw[:1500]


def _http_download(url, dest_path, timeout=120):
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)


# ---------------------------------------------------------------------------
# Workflow loading and legacy prompt injection
# ---------------------------------------------------------------------------

def load_workflow(workflow_path):
    path = Path(workflow_path)
    if not path.exists():
        raise FileNotFoundError(f"工作流文件不存在：{path}")
    with open(path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    if not isinstance(wf, dict):
        raise ValueError(
            "工作流必须是 API 格式的 JSON 对象，不是数组。"
            "请在 ComfyUI 中用 'Save (API Format)' 导出。"
        )
    return wf


def validate_workflow_shape(workflow):
    """Reject entries that are not real nodes (e.g. leftover _README keys)."""
    bad = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or "class_type" not in node:
            bad.append(node_id)
    if bad:
        raise ValueError(
            "工作流中存在非节点条目（ComfyUI 会拒绝提交）："
            + ", ".join(map(str, bad))
            + "。请移除这些字段，说明文字应写在 _tp.json 的 description 里。"
        )
    return workflow


def apply_prompt_legacy(workflow, prompt_text, prompt_node_id=None):
    """
    Fallback path used when no template exists.

    Injects the prompt into a CLIPTextEncode node, auto-detected or explicit.
    """
    if not prompt_text:
        return workflow

    if prompt_node_id:
        if prompt_node_id not in workflow:
            raise KeyError(f"节点 '{prompt_node_id}' 不在工作流中。")
        workflow[prompt_node_id].setdefault("inputs", {})["text"] = prompt_text
        return workflow

    clip_nodes = [
        (nid, nd) for nid, nd in workflow.items()
        if isinstance(nd, dict) and "CLIPTextEncode" in nd.get("class_type", "")
    ]

    if clip_nodes:
        target_id, target_node = clip_nodes[0]
        target_node.setdefault("inputs", {})["text"] = prompt_text
        return workflow

    for nid, nd in workflow.items():
        if isinstance(nd, dict) and "text" in nd.get("inputs", {}):
            workflow[nid]["inputs"]["text"] = prompt_text
            return workflow

    raise ValueError(
        "工作流中没有 CLIPTextEncode 或 text 输入节点，无法注入提示词。"
        "请为该工作流创建一个 _tp.json 模板，或用 --prompt-node-id 指定节点。"
    )


def apply_overrides(workflow, overrides_json):
    """Apply raw node overrides last, bypassing the template system."""
    if not overrides_json:
        return workflow
    overrides = json.loads(overrides_json) if isinstance(overrides_json, str) else overrides_json
    for node_id, node_override in overrides.items():
        if node_id not in workflow:
            print(f"警告：覆盖目标节点 '{node_id}' 不在工作流中，已跳过。", file=sys.stderr)
            continue
        if "inputs" in node_override:
            workflow[node_id].setdefault("inputs", {}).update(node_override["inputs"])
        if "class_type" in node_override:
            workflow[node_id]["class_type"] = node_override["class_type"]
    return workflow


def parse_kv_params(pairs):
    """Parse repeated `key=value` CLI args into a dict."""
    params = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"--param 需要 key=value 格式，收到：{item!r}")
        key, value = item.split("=", 1)
        params[key.strip()] = value
    return params


# ---------------------------------------------------------------------------
# ComfyUI API interaction
# ---------------------------------------------------------------------------

def get_server_url(host=None, port=None):
    h = host or os.environ.get("COMFYUI_HOST", "127.0.0.1")
    p = port or os.environ.get("COMFYUI_PORT", "8188")
    return f"http://{h}:{p}"


def check_server(base_url, timeout=10):
    try:
        return True, _http_get(f"{base_url}/system_stats", timeout=timeout)
    except Exception as e:
        return False, str(e)


def queue_prompt(base_url, workflow_data, client_id="local-media-gen"):
    result = _http_post(
        f"{base_url}/prompt",
        {"prompt": workflow_data, "client_id": client_id},
        timeout=60,
    )
    if "prompt_id" not in result:
        raise RuntimeError(f"ComfyUI 未返回 prompt_id：{result}")
    return result["prompt_id"]


def get_history(base_url, prompt_id, timeout=30):
    return _http_get(f"{base_url}/history/{prompt_id}", timeout=timeout)


def get_queue_status(base_url, timeout=10):
    return _http_get(f"{base_url}/queue", timeout=timeout)


def poll_prompt(base_url, prompt_id, max_wait=600, poll_interval=2, verbose=False):
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > max_wait:
            raise TimeoutError(
                f"任务 {prompt_id} 在 {max_wait}s 内未完成。"
                f"视频生成较慢，可加大 --max-wait。"
            )

        history = get_history(base_url, prompt_id, timeout=15)
        if prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status", {})
            status_str = status.get("status_str", "")
            if status.get("completed", False) or "success" in status_str.lower():
                if verbose:
                    print(f"[poll] 任务 {prompt_id} 已完成。")
                return entry
            if status.get("status") == "error":
                messages = status.get("messages", [])
                raise RuntimeError(
                    f"任务 {prompt_id} 执行失败：{status_str}。"
                    f"详情：{json.dumps(messages, ensure_ascii=False)[:800]}"
                )

        if verbose:
            queue = get_queue_status(base_url, timeout=10)
            print(
                f"[poll] 已等待 {elapsed:.0f}s  "
                f"运行中={len(queue.get('queue_running', []))}  "
                f"排队中={len(queue.get('queue_pending', []))}"
            )

        time.sleep(poll_interval)


def download_outputs(base_url, history_entry, output_dir, verbose=False):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []

    outputs = history_entry.get("outputs", {})
    for node_id, node_output in outputs.items():
        for key in ("images", "gifs", "videos"):
            for item in node_output.get(key, []):
                filename = item.get("filename")
                if not filename:
                    continue
                params = urllib.parse.urlencode({
                    "filename": filename,
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                })
                url = f"{base_url}/view?{params}"
                dest = output_dir / Path(filename).name
                if verbose:
                    print(f"[download] {url} -> {dest}")
                _http_download(url, dest, timeout=300)
                downloaded.append(dest)

    return downloaded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="通过本地 ComfyUI 生成图片/视频（支持 _tp.json 参数模板）"
    )
    p.add_argument("--workflow", "-w", default=None,
                   help="ComfyUI API 格式工作流 JSON 路径"
                        "（仅 --list-templates 时可省略，默认扫描 skill 的 assets/）")
    p.add_argument("--prompt", "-p", default=None,
                   help="提示词快捷方式，等价于 --param prompt=...")
    p.add_argument("--param", "-P", action="append", default=[], metavar="KEY=VALUE",
                   help="按模板声明的参数传值，可重复，例如 --param steps=30")
    p.add_argument("--list-params", action="store_true",
                   help="列出该工作流模板暴露的参数后退出")
    p.add_argument("--list-templates", action="store_true",
                   help="列出目录下所有可发现的模板对后退出")
    p.add_argument("--no-template", action="store_true",
                   help="忽略 _tp.json，回退到自动检测 CLIPTextEncode")
    p.add_argument("--prompt-node-id", default=None,
                   help="无模板模式下指定提示词节点 ID")
    p.add_argument("--node-overrides", default=None,
                   help='最后的原始节点覆盖，JSON 字符串，例如 \'{"3":{"inputs":{"cfg":9}}}\'')
    p.add_argument("--dry-run", action="store_true",
                   help="只做融合并输出结果，不提交给 ComfyUI")
    p.add_argument("--output", "-o", default="./outputs",
                   help="生成文件保存目录（默认 ./outputs）")
    p.add_argument("--host", default=None, help="ComfyUI 主机（默认 127.0.0.1）")
    p.add_argument("--port", default=None, help="ComfyUI 端口（默认 8188）")
    p.add_argument("--max-wait", type=int, default=1800,
                   help="最长等待秒数（默认 1800）。8GB 显存切换模型家族时"
                        "初始化可能耗时 10~20 分钟，不要设太小")
    p.add_argument("--free-first", action="store_true",
                   help="提交前先 POST /free 卸载已有模型。切换模型家族时强烈建议开启，"
                        "否则容易 OOM（例如 Qwen 之后接着跑 Wan）")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="轮询间隔秒数（默认 2.0）")
    p.add_argument("--verbose", "-v", action="store_true", help="输出详细过程")
    return p


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args()

    # --- Template listing mode ---
    if args.list_templates:
        if args.workflow:
            scan_dir = Path(args.workflow).parent
        else:
            scan_dir = Path(__file__).resolve().parent.parent / "assets"
        if not scan_dir.is_dir():
            print(f"错误：模板目录不存在 {scan_dir}", file=sys.stderr)
            sys.exit(1)
        print(f"[dir] {scan_dir}")
        pairs = list_templates(scan_dir)
        if not pairs:
            print("（该目录下没有 _tp.json 模板）")
        for item in pairs:
            mark = "" if item["base_exists"] else "  （缺少基础工作流）"
            print(f"{Path(item['template']).name}  ->  {Path(item['base']).name}{mark}")
        return

    if not args.workflow:
        print("错误：必须提供 --workflow/-w（或使用 --list-templates 浏览模板）", file=sys.stderr)
        sys.exit(1)

    workflow_path = Path(args.workflow)

    # --- Param listing mode ---
    if args.list_params:
        tp_path = find_template_for(workflow_path)
        if not tp_path:
            print(f"该工作流没有配对模板（期望：{workflow_path.stem}_tp.json）")
            print("将回退到自动检测 CLIPTextEncode 节点注入提示词。")
            return
        print(WorkflowTemplate.load(tp_path).describe())
        return

    # --- Load base workflow ---
    try:
        base_workflow = validate_workflow_shape(load_workflow(workflow_path))
    except (ValueError, FileNotFoundError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)

    if args.verbose:
        print(f"[workflow] 已加载 {workflow_path}（{len(base_workflow)} 个节点）")

    # --- Collect params ---
    try:
        user_params = parse_kv_params(args.param)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
    if args.prompt is not None:
        user_params.setdefault("prompt", args.prompt)

    # --- Fuse with template, or fall back ---
    report = None
    tp_path = None if args.no_template else find_template_for(workflow_path)

    if tp_path:
        try:
            template = WorkflowTemplate.load(tp_path)
            workflow, report = fuse(base_workflow, template, user_params)
        except TemplateError as e:
            print(f"模板错误：{e}", file=sys.stderr)
            sys.exit(1)
        if args.verbose:
            print(f"[template] 使用 {tp_path.name}，应用了 {len(report['applied'])} 处注入")
            for w in report["warnings"]:
                print(f"[template] 警告：{w}")
    else:
        if user_params and set(user_params) - {"prompt"}:
            ignored = sorted(set(user_params) - {"prompt"})
            print(
                f"警告：无模板模式下忽略参数 {ignored}（仅 prompt 生效）。"
                f"如需传参请创建 {workflow_path.stem}_tp.json。",
                file=sys.stderr,
            )
        try:
            workflow = apply_prompt_legacy(
                base_workflow, args.prompt, args.prompt_node_id
            )
        except (ValueError, KeyError) as e:
            print(f"错误：{e}", file=sys.stderr)
            sys.exit(1)

    # --- Raw overrides applied last ---
    if args.node_overrides:
        try:
            workflow = apply_overrides(workflow, args.node_overrides)
        except json.JSONDecodeError as e:
            print(f"错误：--node-overrides 不是合法 JSON：{e}", file=sys.stderr)
            sys.exit(1)

    # --- Dry run: show what would be submitted ---
    if args.dry_run:
        print(json.dumps(
            {"report": report, "workflow": workflow},
            indent=2, ensure_ascii=False,
        ))
        return

    # --- Submit to ComfyUI ---
    base_url = get_server_url(args.host, args.port)
    if args.verbose:
        print(f"[server] 检查 {base_url} ...")
    ok, info = check_server(base_url, timeout=10)
    if not ok:
        print(f"错误：无法连接 ComfyUI（{base_url}）：{info}", file=sys.stderr)
        print("请确认 ComfyUI Desktop 已启动。", file=sys.stderr)
        sys.exit(1)

    if args.free_first:
        # 8GB 显存下切换模型家族时，上一个模型残留的显存常导致 OOM。
        try:
            _http_post(base_url + "/free",
                       {"unload_models": True, "free_memory": True})
            print("[free] 已请求卸载已有模型")
        except Exception as e:
            print(f"[free] 卸载请求失败（忽略）：{e}", file=sys.stderr)

    try:
        prompt_id = queue_prompt(base_url, workflow)
    except Exception as e:
        print(f"错误：提交任务失败：{e}", file=sys.stderr)
        sys.exit(1)
    print(f"[queue] 任务 ID：{prompt_id}")

    print(f"[poll] 等待生成完成（最长 {args.max_wait}s）...")
    try:
        entry = poll_prompt(
            base_url, prompt_id,
            max_wait=args.max_wait,
            poll_interval=args.poll_interval,
            verbose=args.verbose,
        )
    except (TimeoutError, RuntimeError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)

    files = download_outputs(base_url, entry, args.output, verbose=args.verbose)
    if files:
        print(f"\n完成，共生成 {len(files)} 个文件：")
        for f in files:
            print(f"  {f}")
    else:
        print("\n警告：任务完成但没有找到输出文件。", file=sys.stderr)
        print("请检查工作流是否包含 SaveImage / SaveAnimatedWEBP / VHS_VideoCombine 等输出节点。",
              file=sys.stderr)

    print("\n" + json.dumps({
        "status": "success" if files else "no_output",
        "prompt_id": prompt_id,
        "output_dir": str(Path(args.output).resolve()),
        "files": [str(f.resolve()) for f in files],
        "params": (report or {}).get("params", {}),
    }, indent=2, ensure_ascii=False))

    if not files:
        sys.exit(2)


if __name__ == "__main__":
    main()
