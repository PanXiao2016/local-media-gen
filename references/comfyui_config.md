# ComfyUI Configuration Reference

## Server Connection

| Parameter | Default | Environment Variable | CLI Flag |
|-----------|---------|---------------------|----------|
| Host | `127.0.0.1` | `COMFYUI_HOST` | `--host` |
| Port | `8188` | `COMFYUI_PORT` | `--port` |

ComfyUI Desktop runs on `127.0.0.1:8188` by default. Local instances need no API key.

## API Endpoints Used

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/system_stats` | GET | Health check |
| `/prompt` | POST | Queue a workflow |
| `/queue` | GET | Running/pending counts |
| `/history/{prompt_id}` | GET | Completed result |
| `/view?filename=...` | GET | Download a generated file |

## Workflow JSON Format (API Format)

ComfyUI supports two export formats:
- **API JSON** — flat object keyed by node ID; each node has `class_type` and `inputs`. This is what the skill consumes.
- **UI export** — full node graph with a `links` array. Not supported.

Export an API-format workflow:
1. Load the workflow in the ComfyUI graph editor.
2. Open settings (gear icon) → enable **Devtools Options**.
3. Click **Save (API Format)**.

**Critical:** every top-level key in this file must be a real node. Adding helper
keys like `_README` or `_comment` makes ComfyUI reject the submission with a
node-not-found error. The generator validates this up front and refuses to submit.
Put human-readable notes in the paired `_tp.json` `description` field instead.

## UI Format vs API Format

| | UI format | API format |
|---|---|---|
| Saved by | "Save" in the graph editor | "Save (API Format)" with Devtools enabled |
| Top-level shape | `{nodes: [...], links: [...], version, extra}` | `{ "<node_id>": {class_type, inputs} }` |
| Accepted by `/prompt` | No | Yes |
| Human editable | Yes (visual) | Painfully |

Most workflows a user has on disk are **UI format**. `scripts/ui_to_api.py`
converts them.

### How the converter maps widgets

`widgets_values` is a positional array; the field names live in `/object_info`.
For each declared input in order, if the type is a widget type (`INT`, `FLOAT`,
`STRING`, `BOOLEAN`, `LIST`, `COMBO`, or anything prefixed `COMFY_`), consume the
next value from `widgets_values`.

Two exceptions consume an extra slot:

- **`control_after_generate: true`** on a seed-like INT — the following
  `"fixed"` / `"randomize"` entry is a UI control, not a node input.
- **`image_upload: true`** on `LoadImage` — an upload-type entry may follow the
  filename.

Everything else is a link type (`MODEL`, `CLIP`, `VAE`, `CONDITIONING`,
`LATENT`, `IMAGE`, `VIDEO`, …) and comes from the `links` array as
`["source_node_id", source_slot]`.

### Node states the converter handles

| State | Meaning | Converter action |
|-------|---------|-----------------|
| `mode: 0` | Active | Converted normally |
| `mode: 2` | Muted | Skipped |
| `mode: 4` | Bypassed, has link inputs | Skipped, consumers re-link to its same-type input |
| `mode: 4` | Bypassed, all-widget inputs (e.g. detector provider) | Restored, otherwise consumers lose their source |
| Not reachable from any output | Isolated leftover on canvas | Skipped |
| `Reroute` / `PrimitiveNode` | Wire helpers | Resolved away |

### Why conversion can still fail

Conversion only guarantees structural validity. ComfyUI's own validation can
still reject the result:

- **`Invalid image file: xxx.png`** — the file isn't in the input directory
  ComfyUI is actually serving. Check `LoadImage`'s option list from
  `/object_info`; a Desktop install often serves `ComfyUI-Shared/input` rather
  than `ComfyUI/input`.
- **`Value not in list: ckpt_name: 'x'`** — model not installed or not visible
  through the configured `extra_model_paths`.
- **Unknown node type** — the custom node isn't loaded; restart ComfyUI.

### Launching ComfyUI with the right directories

A Desktop install usually separates code from user data. When starting
`main.py` manually, point it at the same directories Desktop uses, or models and
input images will be invisible:

```bash
python main.py --port 8188 \
  --extra-model-paths-config <paths.yaml> \
  --input-directory  "G:/Comfy-Desktop/ComfyUI-Shared/input" \
  --output-directory "G:/Comfy-Desktop/ComfyUI-Shared/output"
```

---

# Template System

This is the core mechanism adapted from ComfyFusion Engine. A **base workflow**
stays the technical blueprint; a **template** declares which nodes and inputs are
exposed as friendly parameters.

## Pairing Rule

Same directory, same stem, `_tp` suffix:

```
assets/
├── <name>.json              # base workflow (submitted to ComfyUI)
└── <name>_tp.json           # template (never submitted)
```

If no template exists, the generator falls back to auto-detecting a
`CLIPTextEncode` node and injecting the prompt there.

## Template Format

```json
{
  "description": "SDXL 文生图",
  "kind": "image",
  "params": {
    "prompt": {
      "type": "string",
      "required": true,
      "description": "正向提示词"
    },
    "seed": {
      "type": "int",
      "default": -1,
      "description": "-1 表示随机"
    },
    "steps": {
      "type": "int",
      "default": 25,
      "min": 1,
      "max": 150
    },
    "sampler_name": {
      "type": "string",
      "default": "euler",
      "choices": ["euler", "dpmpp_2m"]
    }
  },
  "nodes": {
    "6": {"inputs": {"text": "{prompt}"}},
    "3": {"inputs": {"seed": "{seed}", "steps": "{steps}", "sampler_name": "{sampler_name}"}}
  }
}
```

### `params` — parameter declarations

| Field | Required | Description |
|-------|----------|-------------|
| `type` | no | `string` / `int` / `float` / `bool` (default `string`) |
| `default` | no | Value used when the caller omits it |
| `required` | no | If true and no value/default given, the run aborts |
| `description` | no | Shown in `--list-params` |
| `choices` | no | Allowed values; anything else is rejected |
| `min` / `max` | no | Numeric bounds, enforced after coercion |

Parameters used in `nodes` but never declared in `params` are treated as
implicit optional `string` params. Declaring them is still recommended so they
show up in `--list-params`.

### `nodes` — injection points

Keys are node IDs from the base workflow; values are partial `inputs` patches.
Only fields that already exist on the base node are written — the template can
never invent new inputs or break connections.

## Placeholder Rules

| Template value | Caller passes | Result |
|----------------|--------------|--------|
| `"{seed}"` | `42` (str) | `42` as **int** (native type preserved) |
| `"{cfg}"` | `"8.5"` | `8.5` as **float** |
| `"{prompt}"` | `"a cat"` | `"a cat"` |
| `"{prompt}, masterpiece"` | `"a cat"` | `"a cat, masterpiece"` (mixed interpolation) |
| `"{a} and {b}"` | `a="x", b="y"` | `"x and y"` |
| `"{undeclared}"` | — | left as-is, then caught by the leftover check |

Dicts and lists inside a template patch are walked recursively.

### Seed convention

`seed = -1` means "pick a random seed". The engine substitutes a random
32-bit value before submission, so the resolved seed is reported back and can be
reused to reproduce the exact image.

### Leftover placeholder guard

Before submission the final workflow is scanned for any remaining `{name}`
pattern. If found, the run aborts with the offending paths listed. This catches
typos and undeclared params *before* ComfyUI fails with an opaque error.

## Fusion Flow

```
caller params  ──┐
                 ├─► resolve (defaults + type coercion + validation)
template params ─┘            │
                              ▼
                    substitute into template patch
                              │
                              ▼
                    write into base workflow inputs
                              │
                              ▼
                    leftover placeholder check
                              │
                              ▼
                    submit to ComfyUI
```

`--node-overrides` is applied after all of the above, as a raw escape hatch.

## Writing a Template for Your Own Workflow

1. Export your workflow in API format into `assets/my_workflow.json`.
2. Open the JSON and find the node IDs you want to expose:
   - the positive prompt node (usually `CLIPTextEncode`),
   - the sampler node (seed/steps/cfg),
   - the latent/image node (width/height),
   - the output node (filename prefix).
3. Create `assets/my_workflow_tp.json` declaring those params and pointing
   `nodes` at the right IDs and field names.
4. Verify without touching ComfyUI:

```bash
python scripts/comfyui_generate.py -w assets/my_workflow.json --list-params
python scripts/comfyui_generate.py -w assets/my_workflow.json \
    --prompt "test" --param steps=30 --dry-run
```

5. Confirm the `--dry-run` output shows the expected values in `workflow`, then
   drop `--dry-run` to actually generate.

## Node Parameter Reference

### CLIPTextEncode
- `text` — prompt string (positive or negative)

### KSampler / KSamplerAdvanced
- `seed` — integer
- `steps` — diffusion steps
- `cfg` — guidance scale
- `sampler_name` — `euler`, `euler_ancestral`, `dpmpp_2m`, `dpmpp_2m_sde`, `ddim`, `uni_pc`, `lcm`
- `scheduler` — `normal`, `karras`, `exponential`, `sgm_uniform`, `simple`, `ddim_uniform`
- `denoise` — 0.0–1.0

### EmptyLatentImage / EmptySD3LatentImage
- `width`, `height`, `batch_size`

### CheckpointLoaderSimple / UNETLoader
- `ckpt_name` / `unet_name` — model filename

### SaveImage
- `filename_prefix`

### SaveAnimatedWEBP
- `filename_prefix`, `fps`, `quality`, `lossless`, `method`

### VHS_VideoCombine (VideoHelperSuite)
- `frame_rate`, `filename_prefix`, `format`, `loop_count`

## Video Generation Notes

Video workflows typically use AnimateDiff, SVD, WAN, CogVideo, or HunyuanVideo
nodes. The output node determines where results land:

- `SaveAnimatedWEBP` → `.webp`, appears in the history `images` array
- `VHS_VideoCombine` → `.mp4`/`.gif`, appears in the `gifs` array

The downloader handles `images`, `gifs`, and `videos` arrays.

## Timeout and Polling

- Default max wait: **600s**. Video generation often needs more — use `--max-wait 1800` or higher.
- Poll interval: **2s**.

## Troubleshooting

### "无法连接 ComfyUI"
ComfyUI Desktop is not running, or the port differs. Check with
`curl http://127.0.0.1:8188/system_stats`.

### "工作流中存在非节点条目"
The base workflow has helper keys (`_README`, `_comment`, etc.). Remove them —
ComfyUI parses every top-level key as a node ID. Move the text into the
template's `description`.

### "缺少必填参数"
The template marks a param `required` and the caller did not pass it. Either pass
it via `--param name=value`, or give it a `default` in the template.

### "未知参数"
The caller passed a param the template does not declare. Run `--list-params` to
see valid names. (In no-template mode, only `prompt` is accepted.)

### "仍存在未替换的占位符"
A template references `{something}` that was never resolved. Usually a typo in
the template, or a param declared without a default and not supplied.

### "任务完成但没有找到输出文件"
The workflow lacks an output node, or the output node is not connected to the
final decode. Verify the workflow runs standalone inside ComfyUI first.

### Prompt has no effect
Auto-detection (no-template mode) picks the *first* `CLIPTextEncode` node, which
may be the negative prompt. Create a template and point `nodes` at the correct
node IDs.

### Black or corrupted images (SDXL)
A known fp16 VAE issue. Fix inside the workflow: load the VAE at fp32, or add a
tiled VAE decode node. The skill does not patch the workflow automatically — fix
it in the base workflow JSON.

### Video flickers
Increase motion module strength, or lower the CFG. Flickering is usually a
workflow/model issue, not an API issue.
