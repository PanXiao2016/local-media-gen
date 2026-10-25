#!/usr/bin/env python3
"""Ad-hoc verification of the template engine's edge cases."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from workflow_template import (
    TemplateError,
    WorkflowTemplate,
    fuse,
    find_template_for,
    substitute,
    find_placeholders,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ASSETS = Path(__file__).resolve().parent.parent / "assets"
IMG = ASSETS / "sdxl_t2i_illustrious.json"
VID = ASSETS / "wan22_i2v_4step.json"
passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def expect_error(name, fn, needle=""):
    global passed, failed
    try:
        fn()
    except TemplateError as e:
        if needle and needle not in str(e):
            failed += 1
            print(f"  FAIL  {name}  (报错信息不含 '{needle}'：{e})")
        else:
            passed += 1
            print(f"  PASS  {name}")
        return
    except Exception as e:
        failed += 1
        print(f"  FAIL  {name}  (抛了非 TemplateError：{type(e).__name__}: {e})")
        return
    failed += 1
    print(f"  FAIL  {name}  (预期报错但没报)")


print("\n[1] 模板发现与加载")
tp = find_template_for(IMG)
check("配对模板被找到", tp is not None and tp.name == "sdxl_t2i_illustrious_tp.json", str(tp))
tpl = WorkflowTemplate.load(tp)
check("参数数量正确", len(tpl.params) == 13, f"实际 {len(tpl.params)}")
check("kind 为 image", tpl.kind == "image", tpl.kind)

print("\n[2] 类型转换")
resolved = tpl.resolve({"prompt": "cat", "steps": "30", "cfg": "8.5", "seed": "7"})
check("steps 转 int", resolved["steps"] == 30 and isinstance(resolved["steps"], int), repr(resolved["steps"]))
check("cfg 转 float", resolved["cfg"] == 8.5 and isinstance(resolved["cfg"], float), repr(resolved["cfg"]))
check("seed 转 int", resolved["seed"] == 7 and isinstance(resolved["seed"], int), repr(resolved["seed"]))
check("prompt 保持字符串", resolved["prompt"] == "cat", repr(resolved["prompt"]))

print("\n[3] 默认值与必填")
check("默认值被填充", resolved["height"] == 1216, repr(resolved.get("height")))
expect_error("缺必填参数报错", lambda: tpl.resolve({}), "缺少必填参数")
expect_error("未知参数报错", lambda: tpl.resolve({"prompt": "x", "bogus": "1"}), "未知参数")

print("\n[4] 校验规则")
expect_error("int 类型错误报错", lambda: tpl.resolve({"prompt": "x", "steps": "abc"}), "需要整数")
expect_error("超出 max 报错", lambda: tpl.resolve({"prompt": "x", "steps": "999"}), "不能大于")
expect_error("低于 min 报错", lambda: tpl.resolve({"prompt": "x", "width": "8"}), "不能小于")
expect_error("choices 越界报错", lambda: tpl.resolve({"prompt": "x", "sampler_name": "nope"}), "必须是")

print("\n[5] seed = -1 随机化")
r1 = tpl.resolve({"prompt": "x", "seed": "-1"})
r2 = tpl.resolve({"prompt": "x", "seed": "-1"})
check("seed -1 被替换为随机数", r1["seed"] != -1 and 0 <= r1["seed"] < 2**32, repr(r1["seed"]))
check("两次随机结果不同", r1["seed"] != r2["seed"], f"{r1['seed']} vs {r2['seed']}")

print("\n[6] 占位符替换")
check("纯占位符保留原生类型",
      substitute("{seed}", {"seed": 42}) == 42 and isinstance(substitute("{seed}", {"seed": 42}), int))
check("混合内插",
      substitute("{prompt}, masterpiece", {"prompt": "cat"}) == "cat, masterpiece")
check("多占位符内插",
      substitute("{a} and {b}", {"a": "x", "b": "y"}) == "x and y")
check("未定义占位符原样保留",
      substitute("{missing}", {}) == "{missing}")
check("嵌套 dict 递归",
      substitute({"k": "{v}"}, {"v": 1}) == {"k": 1})
check("嵌套 list 递归",
      substitute(["a", "{v}"], {"v": 2}) == ["a", 2])
check("非字符串原样返回",
      substitute(123, {}) == 123)

print("\n[7] 残留占位符检测")
leftover = find_placeholders({"a": {"b": "{oops}"}, "c": ["{x}"]})
check("检测到 2 个残留", len(leftover) == 2, str(leftover))

print("\n[8] 完整融合（图片）")
base_wf = json.loads(IMG.read_text(encoding="utf-8"))
final, report = fuse(base_wf, tpl, {"prompt": "a cat", "seed": "42", "width": "768"})
check("prompt 写入节点 36", final["36"]["inputs"]["text"] == "a cat", final["36"]["inputs"]["text"])
check("seed 写入节点 42 且为 int",
      final["42"]["inputs"]["seed"] == 42 and isinstance(final["42"]["inputs"]["seed"], int))
check("width 写入节点 25 且为 int",
      final["25"]["inputs"]["width"] == 768 and isinstance(final["25"]["inputs"]["width"], int))
check("连接数组未被破坏", final["42"]["inputs"]["model"] == ["45", 0], str(final["42"]["inputs"]["model"]))
check("非模板字段未被改动", final["45"]["inputs"]["ckpt_name"] == base_wf["45"]["inputs"]["ckpt_name"])
check("applied 记录 13 处", len(report["applied"]) == 13, str(len(report["applied"])))
check("无残留占位符", find_placeholders(final) == [], str(find_placeholders(final)))
check("原工作流未被修改（深拷贝）",
      base_wf["36"]["inputs"]["text"] != "a cat", base_wf["36"]["inputs"]["text"])

print("\n[9] 完整融合（视频）")
vtp = find_template_for(VID)
check("视频模板被找到", vtp is not None, str(vtp))
vtpl = WorkflowTemplate.load(vtp)
check("视频 kind 为 video", vtpl.kind == "video", vtpl.kind)
vfinal, vreport = fuse(
    json.loads(VID.read_text(encoding="utf-8")),
    vtpl,
    {"image": "x.png", "prompt": "waves", "length": "24", "fps": "12", "split_step": "1"},
)
check("length 写入 104 且为 int",
      vfinal["104"]["inputs"]["length"] == 24 and isinstance(vfinal["104"]["inputs"]["length"], int),
      repr(vfinal["104"]["inputs"]["length"]))
check("fps 写入输出节点 93",
      vfinal["93"]["inputs"]["frame_rate"] == 12 and isinstance(vfinal["93"]["inputs"]["frame_rate"], int),
      repr(vfinal["93"]["inputs"]["frame_rate"]))
check("split_step 写入 57.end_at_step", vfinal["57"]["inputs"]["end_at_step"] == 1,
      repr(vfinal["57"]["inputs"]["end_at_step"]))
check("split_step 写入 58.start_at_step", vfinal["58"]["inputs"]["start_at_step"] == 1,
      repr(vfinal["58"]["inputs"]["start_at_step"]))
check("seed 同步到两个采样器",
      vfinal["57"]["inputs"]["noise_seed"] == vfinal["58"]["inputs"]["noise_seed"],
      f"{vfinal['57']['inputs']['noise_seed']} vs {vfinal['58']['inputs']['noise_seed']}")

print("\n[10] 容错：模板引用不存在的节点")
bad_tpl = WorkflowTemplate({
    "description": "bad",
    "params": {"prompt": {"type": "string", "required": True}},
    "nodes": {"999": {"inputs": {"text": "{prompt}"}}},
})
_, bad_report = fuse(json.loads(IMG.read_text(encoding="utf-8")), bad_tpl, {"prompt": "x"})
check("跳过不存在的节点", len(bad_report["skipped"]) == 1, str(bad_report["skipped"]))
check("产生警告", len(bad_report["warnings"]) == 1, str(bad_report["warnings"]))

print("\n[11] 容错：模板引用不存在的字段")
bad_tpl2 = WorkflowTemplate({
    "description": "bad2",
    "params": {"prompt": {"type": "string", "required": True}},
    "nodes": {"36": {"inputs": {"no_such_field": "{prompt}"}}},
})
_, r2 = fuse(json.loads(IMG.read_text(encoding="utf-8")), bad_tpl2, {"prompt": "x"})
check("跳过不存在的字段", "36.no_such_field（字段不存在）" in r2["skipped"], str(r2["skipped"]))

print("\n[12] 隐式参数（模板用了但没声明）")
implicit_tpl = WorkflowTemplate({
    "description": "implicit",
    "nodes": {"36": {"inputs": {"text": "{prompt}"}}},
})
check("隐式参数被自动声明", "prompt" in implicit_tpl.params, str(list(implicit_tpl.params)))
check("隐式参数记录在 implicit_params", implicit_tpl.implicit_params == ["prompt"],
      str(implicit_tpl.implicit_params))

print(f"\n{'='*50}")
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
