# local-media-gen

An agent skill that drives a **local ComfyUI instance** to generate images and
videos — declarative parameter templates instead of hardcoded node graphs.

Works on an 8GB consumer GPU. No API keys, no cloud, no `pip install`.

> 让 AI agent 直接调用你本机的 ComfyUI 出图/出视频。工作流用 JSON 声明，参数用模板暴露，换模型不用改代码。

---

## Why this exists

Most "AI image" agent skills call a cloud API. This one doesn't. It talks to the
ComfyUI already running on your machine, which means:

- Your models, your LoRAs, your custom nodes — whatever you already installed.
- No per-image cost, no rate limits, no content filter you didn't choose.
- Nothing leaves the machine.

The hard part of driving ComfyUI from an agent isn't the HTTP call — it's that
every workflow has different node IDs, different input names, and different
required parameters. So each workflow ships with a **`_tp.json` template** that
declares which knobs are exposed as friendly parameters:

```
assets/wan_animate2.json      # the graph, submitted verbatim to ComfyUI
assets/wan_animate2_tp.json   # 23 named parameters, typed, with defaults
```

The engine fuses caller parameters into the graph, validates types and ranges,
refuses to submit with unresolved placeholders, then polls until done and
downloads the results. Adding a new workflow means adding two JSON files — no
code changes.

## What it can do

| Capability | Asset | Notes |
|---|---|---|
| Text-to-image (anime, Danbooru-tag) | `sdxl_t2i_illustrious.json` | SDXL 光辉系 |
| Text-to-image (photoreal, natural language) | `krea2_t2i.json` | Krea2 turbo, needs Block Swap |
| Instruction-based image editing | `qwen21_edit.json` | "change the background to snow" |
| Character-consistent editing | `krea2_identity_edit.json` | identity held by LoRA reference |
| Image-to-video | `wan22_i2v_4step.json` | Wan2.2 14B, 4-step, GGUF |
| **Character animation from a driving video** | `wan_animate2.json` | Wan-Animate-2 14B |
| **Character replacement (keeps the video's background)** | `wa_scail2.json` | SCAIL-2 14B |
| Image-to-video (experimental) | `minimax_h3_i2v.json` | **not verified end-to-end** |

Character animation is the interesting one: give it a still image of a character
and a video of someone dancing, and it transfers the motion, expression and
gestures onto your character. No pose extraction, no DWPose, no skeleton —
Wan-Animate-2 is an end-to-end dual-branch DiT that reads motion priors straight
from the video.

Measured on an **RTX 4060 8GB** with the distill int8 checkpoint:
**464×816, 33 frames, 6 steps in 216 seconds.**

## Requirements

1. **ComfyUI** running locally on port 8188 (default).
2. **Python 3.8+** — standard library only, nothing to install.
3. **The models** referenced by whichever workflow you use, installed in ComfyUI.

The skill will not start ComfyUI for you. If it isn't running, it says so.

Model files are named inside each workflow JSON. If you have the same models
under different filenames, either rename yours or pass `--param unet_name=...`
(most workflows expose the loader names as parameters).

## Install

Copy the directory into your agent's skill folder:

```bash
git clone https://github.com/PanXiao2016/local-media-gen.git
cp -r local-media-gen ~/.workbuddy-ai/skills/       # WorkBuddy
cp -r local-media-gen ~/.claude/skills/             # Claude Code
```

Any loader that scans for a `SKILL.md` with `name` and `description` frontmatter
will pick it up.

### Non-WorkBuddy hosts

Two host conventions need translating (see the **Portability** section of
`SKILL.md`):

- `<python>` in the docs is a placeholder for the interpreter path. Substitute
  `python3` or `python`.
- `present_files` is the host's file-delivery tool. Substitute your own.

## Quick start

```bash
# what's available
python scripts/comfyui_generate.py --list-templates

# what does one workflow expose
python scripts/comfyui_generate.py -w assets/wan_animate2.json --list-params

# generate
python scripts/comfyui_generate.py -w assets/sdxl_t2i_illustrious.json \
    --prompt "masterpiece, best quality, 1girl, solo, long black hair" \
    --param width=832 --param height=1216

# animate a character
python scripts/comfyui_generate.py -w assets/wan_animate2.json \
    --prompt "人物外观描述：……。背景描述：……。" \
    --param reference_image=char.png --param driving_video=dance.mp4 \
    --param crop_x=0 --param crop_y=0 --param crop_w=720 --param crop_h=1280 \
    --max-wait 1800
```

Input files must already be in ComfyUI's `input/` directory.

## The prompt is not universal

Every workflow was trained on a different kind of text, and the wrong shape
hurts more than a vague prompt. SDXL wants Danbooru tags; Krea2 explicitly
rejects them. `wan_animate2` wants an **objective description of the reference
image** with no action words at all, because the motion comes from the video.

`references/prompt_conventions.md` has a per-asset table (language, shape,
negative policy, hard no's) plus a worked example for each. The skill reads it
before writing any prompt.

## Repo layout

```
README.md                       # this file — for humans
SKILL.md                        # what the agent reads
LICENSE                         # MIT
references/
  comfyui_config.md             # how the template system works, troubleshooting
  prompt_conventions.md         # per-asset prompt dialects
scripts/
  comfyui_generate.py           # submit / poll / download
  workflow_template.py          # template engine (fuse, coerce, validate)
  ui_to_api.py                  # convert ComfyUI UI-format graphs to API format
  api_to_ui.py                  # the reverse, for debugging in the GUI
  convert_workflows.py          # batch conversion of a workflows directory
  _selftest.py                  # 44 assertions, no ComfyUI needed
assets/
  <name>.json / <name>_tp.json  # graph + parameter template
test-prompts.json               # skill-triggering test cases
```

## Testing

```bash
python scripts/_selftest.py
```

44 assertions covering template fusion, type coercion, range validation,
placeholder guarding, implicit parameter detection and error handling. Runs
without ComfyUI.

## Status

- `sdxl_t2i_illustrious`, `krea2_t2i`, `qwen21_edit`, `krea2_identity_edit`,
  `wan22_i2v_4step`, `wan_animate2`, `wa_scail2` — run end-to-end on the machine
  this was built on (RTX 4060 8GB, Windows).
- `minimax_h3_i2v` — wiring only. The 32B text encoder loads and encodes, but a
  20.97GB INT8 UNET does not fit 8GB of VRAM without H3 block swap, which
  ComfyUI no longer provides. Treat it as a reference, not a working path.

Measured on that card: Wan-Animate-2 does 464×816 / 33 frames / 6 steps in
**216 s**; SCAIL-2 does 512×896 / 33 frames / 20 steps in **20m08s** (it has no
distill checkpoint, so 20 steps is the floor).

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, sell it, ship it closed. Just keep
the copyright notice.
