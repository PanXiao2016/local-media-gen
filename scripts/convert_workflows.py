#!/usr/bin/env python3
"""
Scan a directory of ComfyUI workflow files, detect their format, and convert
UI-format graphs to API format.

Usage:
    python convert_workflows.py <workflows_dir> [-o <out_dir>] [--report-only]

Example:
    python convert_workflows.py \
        "~/ComfyUI/user/default/workflows" \
        -o ./converted

ComfyUI must be running: node schemas come from /object_info.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_to_api import (  # noqa: E402
    ConvertError,
    convert,
    load_object_info,
)


def detect_format(data):
    """Return 'api', 'ui', or 'unknown'."""
    if not isinstance(data, dict):
        return "unknown"
    if isinstance(data.get("nodes"), list):
        return "ui"
    if data and all(isinstance(v, dict) and "class_type" in v for v in data.values()):
        return "api"
    return "unknown"


def safe_name(stem):
    """Make a filesystem-safe name out of a workflow title."""
    keep = []
    for ch in stem:
        if ch.isalnum() or ch in "-_. ":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip().replace(" ", "_")[:120]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description="扫描并转换 ComfyUI 工作流")
    p.add_argument("workflows_dir", help="工作流目录（会递归扫描）")
    p.add_argument("-o", "--output-dir", default=None, help="转换结果输出目录")
    p.add_argument("--report-only", action="store_true", help="只报告，不写文件")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default="8188")
    p.add_argument("--object-info-cache", default=None)
    args = p.parse_args()

    root = Path(args.workflows_dir)
    if not root.is_dir():
        print(f"错误：目录不存在：{root}", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output_dir) if args.output_dir else None
    if out_dir and not args.report_only:
        out_dir.mkdir(parents=True, exist_ok=True)

    try:
        oi = load_object_info(args.host, args.port, args.object_info_cache)
    except Exception as e:
        print(f"错误：无法获取 /object_info（ComfyUI 是否在运行？）：{e}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        [f for f in root.rglob("*.json")
         if f.name != ".index.json" and not f.stem.endswith("_tp")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    converted, already_api, failed = [], [], []

    for f in files:
        rel = str(f.relative_to(root))
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            failed.append((rel, f"读取失败：{e}"))
            continue

        fmt = detect_format(data)

        if fmt == "api":
            already_api.append((rel, len(data)))
            if out_dir and not args.report_only:
                (out_dir / f"{safe_name(f.stem)}.api.json").write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            continue

        if fmt != "ui":
            failed.append((rel, "无法识别的格式（既非 UI 也非 API）"))
            continue

        try:
            api, _ = convert(data, oi)
        except ConvertError as e:
            failed.append((rel, str(e).replace("\n", " | ")[:240]))
            continue

        converted.append((rel, len(api)))
        if out_dir and not args.report_only:
            (out_dir / f"{safe_name(f.stem)}.api.json").write_text(
                json.dumps(api, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    print(f"=== 已转换 {len(converted)} ===")
    for rel, n in converted:
        print(f"  [{n:>3} 节点] {rel}")
    print(f"\n=== 已是 API 格式 {len(already_api)} ===")
    for rel, n in already_api:
        print(f"  [{n:>3} 节点] {rel}")
    print(f"\n=== 失败 {len(failed)} ===")
    for rel, err in failed:
        print(f"  {rel}\n      {err}")

    if out_dir and not args.report_only:
        print(f"\n输出目录：{out_dir.resolve()}")

    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
