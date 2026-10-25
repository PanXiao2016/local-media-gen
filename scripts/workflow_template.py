#!/usr/bin/env python3
"""
Workflow template engine for ComfyUI.

Implements a two-stage fusion model:
    user params  +  template patch  ->  unified patch  ->  final workflow

A template (`<workflow>_tp.json`) declares which nodes/inputs are exposed as
friendly parameters, using `{placeholder}` syntax. The base workflow
(`<workflow>.json`) stays untouched as the technical blueprint.

Template format (superset of the original ComfyFusion format):

    {
      "description": "SDXL text-to-image",
      "kind": "image",
      "params": {
        "prompt":   {"type": "string", "required": true, "description": "正向提示词"},
        "seed":     {"type": "int",    "default": -1,   "description": "-1 表示随机"},
        "steps":    {"type": "int",    "default": 25,   "min": 1, "max": 150}
      },
      "nodes": {
        "6": {"inputs": {"text": "{prompt}"}},
        "3": {"inputs": {"seed": "{seed}", "steps": "{steps}"}}
      }
    }

Differences from the original implementation (deliberate fixes):
  - Mixed interpolation is supported: "{prompt}, masterpiece" -> "a cat, masterpiece"
  - Pure placeholders keep their declared native type (int stays int, not "25")
  - Declared defaults fill missing params; required params raise instead of
    silently leaving a "{prompt}" string in the workflow
  - Leftover placeholders are detected before submission, not after a failure
  - No external dependencies (no deepmerge, no pydantic)
"""

import copy
import json
import random
import re
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
PURE_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")

TYPE_ALIASES = {
    "str": "string",
    "text": "string",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
}

TRUE_WORDS = {"1", "true", "yes", "on", "y", "t"}
FALSE_WORDS = {"0", "false", "no", "off", "n", "f"}


class TemplateError(Exception):
    """Raised when a template is malformed or a parameter cannot be resolved."""


# ---------------------------------------------------------------------------
# Parameter specs
# ---------------------------------------------------------------------------

class ParamSpec:
    """A single declared parameter of a template."""

    __slots__ = ("name", "type", "default", "required", "description",
                 "choices", "minimum", "maximum")

    def __init__(self, name, spec):
        if not isinstance(spec, dict):
            spec = {}
        self.name = name
        raw_type = str(spec.get("type", "string")).lower()
        self.type = TYPE_ALIASES.get(raw_type, raw_type)
        if self.type not in ("string", "int", "float", "bool"):
            raise TemplateError(
                f"参数 '{name}' 的类型 '{raw_type}' 不受支持，"
                f"可用类型：string / int / float / bool"
            )
        self.default = spec.get("default")
        self.required = bool(spec.get("required", False))
        self.description = spec.get("description", "")
        self.choices = spec.get("choices")
        self.minimum = spec.get("min")
        self.maximum = spec.get("max")

    def coerce(self, value):
        """Convert a raw value (usually a CLI string) to this param's type."""
        if value is None:
            return None

        if self.type == "int":
            try:
                return int(float(value)) if isinstance(value, str) else int(value)
            except (TypeError, ValueError):
                raise TemplateError(
                    f"参数 '{self.name}' 需要整数，收到：{value!r}"
                )

        if self.type == "float":
            try:
                return float(value)
            except (TypeError, ValueError):
                raise TemplateError(
                    f"参数 '{self.name}' 需要数字，收到：{value!r}"
                )

        if self.type == "bool":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in TRUE_WORDS:
                return True
            if text in FALSE_WORDS:
                return False
            raise TemplateError(
                f"参数 '{self.name}' 需要布尔值，收到：{value!r}"
            )

        return value if isinstance(value, str) else str(value)

    def validate(self, value):
        """Range and choice validation after coercion."""
        if self.choices is not None and value not in self.choices:
            raise TemplateError(
                f"参数 '{self.name}' 的取值必须是 {self.choices} 之一，收到：{value!r}"
            )
        if self.minimum is not None and isinstance(value, (int, float)):
            if value < self.minimum:
                raise TemplateError(
                    f"参数 '{self.name}' 不能小于 {self.minimum}，收到：{value}"
                )
        if self.maximum is not None and isinstance(value, (int, float)):
            if value > self.maximum:
                raise TemplateError(
                    f"参数 '{self.name}' 不能大于 {self.maximum}，收到：{value}"
                )
        return value

    def to_dict(self):
        out = {"type": self.type}
        if self.default is not None:
            out["default"] = self.default
        if self.required:
            out["required"] = True
        if self.description:
            out["description"] = self.description
        if self.choices:
            out["choices"] = self.choices
        if self.minimum is not None:
            out["min"] = self.minimum
        if self.maximum is not None:
            out["max"] = self.maximum
        return out


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

class WorkflowTemplate:
    """A parsed `_tp.json` template."""

    def __init__(self, data, source_path=None):
        if not isinstance(data, dict):
            raise TemplateError("模板必须是 JSON 对象")

        self.source_path = Path(source_path) if source_path else None
        self.description = data.get("description", "")
        self.kind = data.get("kind", "unknown")

        raw_params = data.get("params") or {}
        if not isinstance(raw_params, dict):
            raise TemplateError("模板的 'params' 必须是对象")
        self.params = {name: ParamSpec(name, spec) for name, spec in raw_params.items()}

        self.nodes = data.get("nodes") or {}
        if not isinstance(self.nodes, dict):
            raise TemplateError("模板的 'nodes' 必须是对象")

        # Any placeholder used in `nodes` but never declared in `params`
        # is treated as an implicit string parameter.
        declared = set(self.params)
        used = set()
        for node_patch in self.nodes.values():
            for raw in (node_patch.get("inputs") or {}).values():
                used.update(PLACEHOLDER_RE.findall(raw) if isinstance(raw, str) else [])
        for name in sorted(used - declared):
            self.params[name] = ParamSpec(name, {"type": "string"})
        self.implicit_params = sorted(used - declared)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            raise TemplateError(f"模板文件不存在：{path}")
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f), source_path=path)

    def describe(self):
        """
        Human/agent readable parameter listing.

        Uses a list layout rather than aligned columns because CJK glyphs are
        double-width in monospace fonts, which breaks space-padded tables.
        """
        if not self.params:
            return f"{self.description or '(无描述)'}\n\n该模板没有暴露任何参数。"

        lines = []
        if self.description:
            lines.append(self.description)
            lines.append("")

        for name, spec in self.params.items():
            tags = [spec.type]
            if spec.required:
                tags.append("必填")
            elif spec.default is not None:
                tags.append(f"默认 {spec.default!r}")
            if spec.choices:
                tags.append("可选值 " + " / ".join(map(str, spec.choices)))
            elif spec.minimum is not None or spec.maximum is not None:
                lo = spec.minimum if spec.minimum is not None else "-"
                hi = spec.maximum if spec.maximum is not None else "-"
                tags.append(f"范围 {lo}~{hi}")

            lines.append(f"  {name}  ({', '.join(tags)})")
            if spec.description:
                lines.append(f"      {spec.description}")

        return "\n".join(lines)

    def resolve(self, user_params, strict=True):
        """
        Merge user-supplied params with declared defaults and coerce types.

        Returns a dict of resolved values keyed by parameter name.
        """
        resolved = {}
        unknown = [k for k in user_params if k not in self.params]
        if unknown and strict:
            raise TemplateError(
                f"未知参数：{', '.join(unknown)}。"
                f"该模板支持的参数：{', '.join(self.params) or '(无)'}"
            )

        for name, spec in self.params.items():
            raw = user_params.get(name)
            if raw is None:
                if spec.default is not None:
                    value = spec.coerce(spec.default)
                elif spec.required:
                    raise TemplateError(f"缺少必填参数：'{name}'（{spec.description}）")
                else:
                    # Optional and no default: leave the placeholder untouched
                    # so the base workflow's own value survives.
                    continue
            else:
                value = spec.coerce(raw)

            value = spec.validate(value)
            resolved[name] = value

        # `seed = -1` is a widespread convention for "pick a random seed".
        if resolved.get("seed") == -1:
            resolved["seed"] = random.randint(0, 2**32 - 1)

        return resolved


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------

def substitute(value, params):
    """
    Recursively replace `{name}` placeholders in a template value.

    - A pure placeholder ("{seed}") returns the parameter's native type.
    - A mixed string ("{prompt}, best quality") is string-interpolated.
    - Dicts and lists are walked recursively.
    - Unresolved placeholders are left as-is and caught later by find_placeholders.
    """
    if isinstance(value, str):
        pure = PURE_PLACEHOLDER_RE.match(value.strip())
        if pure:
            key = pure.group(1)
            return params[key] if key in params else value
        return PLACEHOLDER_RE.sub(
            lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0),
            value,
        )

    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}

    if isinstance(value, list):
        return [substitute(v, params) for v in value]

    return value


def find_placeholders(obj, path=""):
    """Collect every unresolved `{placeholder}` still present in the workflow."""
    found = []
    if isinstance(obj, str):
        for name in PLACEHOLDER_RE.findall(obj):
            found.append(f"{path} -> {{{name}}}")
    elif isinstance(obj, dict):
        for key, val in obj.items():
            found.extend(find_placeholders(val, f"{path}.{key}" if path else str(key)))
    elif isinstance(obj, list):
        for i, val in enumerate(obj):
            found.extend(find_placeholders(val, f"{path}[{i}]"))
    return found


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

def fuse(base_workflow, template, user_params, strict_params=True):
    """
    Apply a template to a base workflow.

    Stage 1: resolve params (user values + template defaults, coerced).
    Stage 2: write substituted values into the base workflow's existing inputs.

    Returns:
        (final_workflow, report) where report is a dict describing what happened.

    Raises:
        TemplateError: on unresolvable params or leftover placeholders.
    """
    resolved = template.resolve(user_params, strict=strict_params)

    final = copy.deepcopy(base_workflow)
    applied, skipped, warnings = [], [], []

    for node_id, node_patch in template.nodes.items():
        if node_id not in final:
            skipped.append(f"{node_id}（节点不存在）")
            warnings.append(f"模板引用了不存在的节点 {node_id}，已跳过")
            continue

        base_inputs = final[node_id].get("inputs")
        if not isinstance(base_inputs, dict):
            skipped.append(f"{node_id}（无 inputs 字段）")
            warnings.append(f"节点 {node_id} 没有 inputs 字段，已跳过")
            continue

        for key, raw_value in (node_patch.get("inputs") or {}).items():
            if key not in base_inputs:
                skipped.append(f"{node_id}.{key}（字段不存在）")
                warnings.append(f"节点 {node_id} 没有输入字段 '{key}'，已跳过")
                continue
            base_inputs[key] = substitute(raw_value, resolved)
            applied.append(f"{node_id}.{key}")

    leftover = find_placeholders(final)
    if leftover:
        raise TemplateError(
            "工作流中仍存在未替换的占位符，拒绝提交：\n  "
            + "\n  ".join(leftover[:10])
            + ("\n  ..." if len(leftover) > 10 else "")
        )

    report = {
        "template": str(template.source_path) if template.source_path else None,
        "kind": template.kind,
        "params": resolved,
        "applied": applied,
        "skipped": skipped,
        "warnings": warnings,
    }
    return final, report


# ---------------------------------------------------------------------------
# Template discovery
# ---------------------------------------------------------------------------

def find_template_for(workflow_path):
    """
    Locate the `_tp.json` template paired with a base workflow.

    Pairing rule: same directory, same stem, `_tp` suffix.
        text_to_image.json -> text_to_image_tp.json
    """
    workflow_path = Path(workflow_path)
    candidate = workflow_path.with_name(f"{workflow_path.stem}_tp.json")
    return candidate if candidate.exists() else None


def list_templates(directory):
    """List every discoverable template pair under a directory."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    results = []
    for tp in sorted(directory.glob("*_tp.json")):
        base = tp.with_name(tp.name[: -len("_tp.json")] + ".json")
        results.append({
            "base": str(base),
            "template": str(tp),
            "base_exists": base.exists(),
        })
    return results


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if len(sys.argv) < 2:
        print("用法: python workflow_template.py <模板路径>")
        sys.exit(1)
    print(WorkflowTemplate.load(sys.argv[1]).describe())
