---
name: local-media-gen
description: Generate images and videos locally through a ComfyUI Desktop instance using API-format workflows with declarative parameter templates. Trigger when the user wants to generate images or videos with local AI models, run a ComfyUI workflow, animate a character from a still image plus a driving video, or produce art via Stable Diffusion / SDXL / Flux / Krea2 / Qwen-Image / WAN / Wan-Animate-2 / MiniMax H3 locally. Keywords: 生成图片, 生成视频, 文生图, 文生视频, 图生视频, 角色动画, 动作迁移, 让角色动起来, 画一张, ComfyUI, 本地模型, 本地生图, 工作流.
---

# Local Media Gen

Generate images and videos on the user's own machine through a local ComfyUI
instance. Workflows are plain ComfyUI API JSON; a paired `_tp.json` template
declares which knobs are exposed as friendly parameters, so the same script
drives any workflow without code changes.

## Overview

The skill connects to ComfyUI (default `127.0.0.1:8188`), fuses caller-supplied
parameters into a workflow, submits it, polls until done, and downloads the
results. Pure Python standard library — no pip install, no API key, no cloud.

## Prerequisites

1. **ComfyUI Desktop** running locally on port 8188.
2. A base workflow JSON plus its `_tp.json` template in `assets/`.
3. The models referenced by that workflow already installed in ComfyUI.

If the user's ComfyUI is not running, tell them to start it. Do not attempt to
launch it.

## Portability

This skill is plain Markdown plus a standard-library Python engine — no pip
packages, no API key, no hardcoded absolute paths. It runs under any agent that
can read a skill file and execute shell commands.

Two host conventions need translating if you are not running on WorkBuddy:

1. **`<python>` is a placeholder for the interpreter path**, expanded by the
   host. If your runtime does not expand it, substitute your own Python 3
   command (`python3` on macOS/Linux, `python` on Windows). Nothing in the
   engine depends on a specific 3.x minor version.
2. **`present_files` is the host's file-delivery tool.** Substitute whatever
   mechanism your agent provides for handing generated files to the user —
   markdown links, an attachment API, or plain paths.

Everything else is portable as-is. To install elsewhere, copy the whole
directory into that agent's skill folder (`~/.claude/skills/` for Claude Code,
`.cursor/rules/` or a custom loader for Cursor, or any path your loader scans
for a `SKILL.md` with `name` and `description` frontmatter).

The only machine-specific state is the ComfyUI endpoint (`--host` / `--port`,
default `127.0.0.1:8188`) and the model filenames inside each workflow JSON.
Both live in data, not in code.

## Workflow

### Step 0: Write the prompt in the target model's dialect

**Every bundled workflow wants a different kind of text, and the wrong shape
hurts more than a vague prompt.** Before picking a workflow, open
`references/prompt_conventions.md` and read the section for the asset you are
about to use.

The key is the asset, not the model family — Krea2 text-to-image wants flowing
sentences, Krea2 identity-edit wants a one-line English edit instruction. SDXL
wants Danbooru tags; Krea2 explicitly rejects them. `wan_animate2` wants an
objective description of the **reference image** with no action words at all,
because the motion comes from the driving video.

When the user supplies their own prompt, don't silently rewrite it if it's badly
mismatched. Name what the model expects, offer a converted version, and let them
choose — a silent rewrite makes the result hard to reproduce.

### Step 1: Pick the workflow

| User wants | Use |
|-----------|-----|
| A quick image | `assets/sdxl_t2i_illustrious.json` |
| A full-chain image (hires + face/hand fix) | `assets/velina_full_chain.json` (if present) |
| A video from a still image | `assets/wan22_i2v_4step.json` |
| **Animate a character with a driving video** | `assets/wan_animate2.json` |
| **Edit an existing image by instruction** | `assets/qwen21_edit.json` |
| **Semi-realistic / photographic rendering** | `assets/krea2_t2i.json` |
| Their own workflow | whatever path they provide |

All bundled workflows are real, converted from the user's own ComfyUI graph
files — they run against this machine's models. When the user has their own
workflow, use it instead.

**Krea2 needs Block Swap — this is not optional.** Krea2 is a 12.8B model in a
6.3GB IQ4_XS GGUF. Without offload management, ComfyUI's own partial-load path
degrades into a per-operator transfer loop: measured **150 s/it**. With the
`Krea2BlockSwap` node (`custom_nodes/ComfyUI-Krea2BlockSwap/`, from
github.com/olob0/ComfyUI-Krea2BlockSwap) wired between the loader and the
sampler at `blocks_to_swap=8`, the same run measures **7 s/it** — a 21×
speedup with bit-identical output. Raise `blocks_to_swap` (12, 16, …) when
increasing resolution; 1024×1024 needs about 12 on an 8GB card. `--lowvram`
does NOT substitute for this and changes nothing.

Also: the Krea2 text encoder must be the **safetensors** Qwen3-VL-4B
(`qwen3vl_4b_int8_convrot.safetensors`). The `-iq4_xs.gguf` variant fails with
"This gguf file is incompatible with llama.cpp". And Krea2 uses the
**Qwen-Image VAE** (`qwen_image_vae.safetensors`), not Flux's `ae.safetensors`.

**Instruction-based editing (Qwen-Image 2.1).** When the user wants to change
an existing picture — swap the outfit, replace the background, restyle, add or
remove an element — use `qwen21_edit.json`. One reference image plus one
natural-language instruction; Chinese works directly. The model keeps the
subject's identity and pose while applying the change. ~2 min per image at
1024 on this machine.

Critical wiring detail: the conditioning must come from
`TextEncodeQwenImage21`, **not** from a plain `CLIPTextEncode` or from the
gguf-loader pack's `QwenImageImg2Img`. Only that node injects the
`<|vision_start|><|image_pad|><|vision_end|>` tokens and attaches
`reference_latents`, so the model can actually *see* the source image. Using
plain text encoding produces a vague reconstruction that ignores the
instruction. The node also emits the correctly-sized empty latent (64 channels,
/16) — wire its `latent` output into the sampler and keep `denoise = 1.0`.


**Character animation (Wan-Animate-2).** When the user wants a character from a
still image to perform the motion, expression and gestures of a driving video,
use `wan_animate2.json`. Two inputs: a reference image (who) and a driving video
(what motion). No pose extraction, no DWPose, no skeleton — Wan-Animate-2 is an
end-to-end dual-branch DiT that reads motion priors straight from the video.

```bash
<python> scripts/comfyui_generate.py -w assets/wan_animate2.json \
    --prompt "人物外观描述：……。背景描述：……。" \
    --param reference_image=char.png --param driving_video=dance.mp4 \
    --param crop_x=0 --param crop_y=0 --param crop_w=720 --param crop_h=1280 \
    --param length=33 --max-wait 1800
```

Measured on this machine (RTX 4060 8GB, distill int8): **464×816 / 33 frames /
6 steps in 216 seconds**, 27 s/it. That is the surprising part — a 16.65GB model
does fine on 8GB as long as it runs the distill checkpoint at `cfg=1.0`.

Six things that will bite you, in the order they usually do:

1. **`prompt` must describe the reference image, not the motion.** The official
   shape is `人物外观描述：…。背景描述：…。` — objective appearance plus
   background, no action words, no adjectives of mood. A prompt that contradicts
   the reference (wrong hair colour, wrong outfit) visibly degrades the result.
   If the user's reference is a different character than they described, ask.
2. **Framing follows the reference image, not the driving video.** A half-body
   reference yields a half-body animation even when the driving video is a full
   body dance. For full-body motion, supply a full-body reference.
3. **Pillarboxed driving videos must be cropped.** `WanAnimate2ToVideo` stretches
   its input; black bars get treated as part of the pose and squash the subject.
   Measure the content box and pass `crop_x / crop_y / crop_w / crop_h`. For a
   video with no bars, set `crop_x=0 crop_y=0` and `crop_w/crop_h` to the native
   resolution — `ImageCrop` at full size is a no-op. Measure it, don't guess:
   take the per-column max brightness over a few frames and find where it rises
   above ~18.
4. **`TrimVideoLatent` is not optional.** `WanAnimate2ToVideo` prepends the
   reference frame's latent, so the sampler emits extra frames. Wire its
   `trim_latent` output (index 3) into `TrimVideoLatent.trim_amount` or the
   output opens on a few frames of garbage.
5. **`shift` is 5.0, not 8.0.** `WAN21_SCAIL2`-style models inherit Wan2.1's
   shift of 8.0, but `WAN_Animate2` declares 5.0. Using 8.0 changes the noise
   schedule and softens motion.
6. **Free VRAM before the first run.** A previous workflow can leave ~5GB
   resident, leaving only 3GB for a 16.65GB model. `POST /free` with
   `{"unload_models":true,"free_memory":true}` recovers it.

Model dependencies: `wan_animate_2_distill_int8_convrot.safetensors`
(16.65GB, ModelScope `Comfy-Org/Wan-Animate-2`), `wan_2.1_vae.safetensors`,
`umt5_xxl_fp8_e4m3fn_scaled.safetensors`, `clip_vision_h.safetensors`.
**The VAE must be Wan2.1 (16 latent channels).** `wan2.2_vae` is 48-channel and
will fail — the node allocates a 16-channel latent.

`distill` vs `base`: the distill checkpoint wants 6 steps at `cfg=1.0` (one
forward pass per step). The base checkpoint wants 20–30 steps at `cfg` 5–8,
which is a dual pass — roughly **8–10× the compute**, i.e. the difference
between minutes and an hour on 8GB. Default to distill.

Optional speedup: insert `WanAnimate2Cache` between the model sampler and the
KSampler. It caches the pose branch's per-block activations so that branch runs
once instead of every step — roughly halves generation time. The cost is RAM:
~12.5GB at 480×832 / 81 frames in bf16, scaling with resolution and length.
At `int4` that drops to about a quarter. On a 34GB machine running a 16.65GB
model plus a 6.7GB text encoder, only enable it for long clips, and prefer
`dtype=int4`.

### Step 2: Inspect available parameters

Before passing parameters, check what the workflow actually accepts:

```bash
<python> scripts/comfyui_generate.py -w <workflow.json> --list-params
```

This prints every declared parameter with its type, default, range, and
description. If the workflow has no template, the output says so and only
`prompt` is supported.

### Step 3: Generate

```bash
<python> scripts/comfyui_generate.py \
    -w assets/sdxl_t2i_illustrious.json \
    --prompt "a cozy reading nook, warm afternoon light" \
    --param steps=30 --param width=832 --param height=1216 \
    -o ./outputs
```

For video, raise the timeout — video generation is far slower than images:

```bash
<python> scripts/comfyui_generate.py \
    -w assets/wan22_i2v_4step.json \
    --param image=nook.png \
    --prompt "warm afternoon light shifts slowly across the room, locked-off camera" \
    --param length=81 \
    --max-wait 1800 -o ./outputs
```

### Step 4: Deliver

On success the script prints the saved file paths and a JSON summary. Deliver the
generated files to the user with whatever presentation mechanism the host
provides (`present_files` on WorkBuddy; see **Portability**).

### Step 5: Handle failures

Read the error and act on it:

| Error | Action |
|-------|--------|
| `无法连接 ComfyUI` | Tell the user to start ComfyUI Desktop |
| `缺少必填参数` | Supply the named param, or check `--list-params` |
| `未知参数` | Run `--list-params` and use a declared name |
| `参数 'x' 需要整数` | Fix the value type |
| `仍存在未替换的占位符` | The template is malformed — inspect it |
| `任务完成但没有找到输出文件` | Workflow lacks an output node |
| Timeout | Raise `--max-wait`; video needs 1800+ |

More detail in `references/comfyui_config.md`.

## Template System

A template declares parameters and where they go. Minimal example:

```json
{
  "description": "SDXL 文生图",
  "kind": "image",
  "params": {
    "prompt": {"type": "string", "required": true},
    "steps":  {"type": "int", "default": 25, "min": 1, "max": 150}
  },
  "nodes": {
    "6": {"inputs": {"text": "{prompt}"}},
    "3": {"inputs": {"steps": "{steps}"}}
  }
}
```

Key behaviors:
- Pure placeholders keep their declared type: `"{steps}"` + `"30"` → `30` (int).
- Mixed interpolation works: `"{prompt}, masterpiece"` → `"a prompt, masterpiece"`.
- Missing optional params fall back to their `default`.
- `seed = -1` is replaced with a random seed, which is reported back so the
  result can be reproduced.
- Only inputs that already exist on the base node are written; connections and
  untouched fields are never modified.
- Unresolved placeholders abort the run before submission.

Full specification in `references/comfyui_config.md`.

## Connecting the User's Own Workflow

This is the main integration path. When the user supplies a ComfyUI workflow:

1. **Export in API format.** In ComfyUI: settings → enable Devtools Options →
   Save (API Format). The UI export format will not work.
2. **Place both files in `assets/`** with matching stems:
   `my_flow.json` + `my_flow_tp.json`.
3. **Find the node IDs** to expose by reading `my_flow.json` — the positive
   prompt node, the sampler, the latent node, the save node.
4. **Write the template** pointing `nodes` at those IDs.
5. **Verify without generating:**
   ```bash
   <python> scripts/comfyui_generate.py -w assets/my_flow.json --list-params
   <python> scripts/comfyui_generate.py -w assets/my_flow.json \
       --prompt "test" --param steps=30 --dry-run
   ```
   The `--dry-run` output shows the exact workflow that would be submitted.
6. **Run it for real** by dropping `--dry-run`.

If the user does not want to write a template, they can run without one — the
script falls back to injecting the prompt into the first `CLIPTextEncode` node.
Only `prompt` works in that mode.

## Converting UI-format Workflows

ComfyUI saves two different things: the **graph** file you save from the editor
(has `nodes` array + `links` array) and the **API** file used for submission.
Only the API form can be posted to `/prompt`. ComfyUI performs this conversion
in the browser (`app.graphToPrompt`), not on the server, so this skill ships a
Python equivalent.

```bash
<python> scripts/ui_to_api.py <ui_workflow.json> -o <out.api.json> --explain
```

It requires ComfyUI to be running (it reads node schemas from `/object_info`).
What it handles:

| Situation | Behavior |
|-----------|----------|
| `Reroute` nodes | Link resolved through to the real source |
| `PrimitiveNode` | Its literal value inlined into the consumer |
| Bypassed node (`mode: 4`) with link inputs | Skipped; links forwarded to its same-type input |
| Bypassed pure-widget node (e.g. detector provider) | Restored — it has no upstream to forward |
| Muted node (`mode: 2`) | Skipped |
| Isolated nodes (not reaching any output) | Skipped |
| `Note` / `MarkdownNote` / rgthree UI nodes | Skipped |
| Widget values stored as strings | Coerced to INT / FLOAT / BOOLEAN per schema |
| Missing required input | Conversion fails with the node id and field name |
| Unknown node type | Fails — the custom node isn't installed or ComfyUI needs a restart |

Output detection is name-pattern based (`Save*`, `Preview*`, `* Saver`), so
third-party savers such as `Image Saver` also count as graph roots.

Always verify a converted workflow before trusting it:

```bash
<python> scripts/comfyui_generate.py -w <out.api.json> --prompt "test" --dry-run
```

Then submit one small run. A conversion can be structurally valid and still fail
ComfyUI's validation (wrong model filename, missing input image) — the error
message names the node and field.

## CLI Reference

```
python comfyui_generate.py --workflow <path> [options]

Required
  -w, --workflow          Base workflow JSON (API format)

Parameters
  -p, --prompt            Shortcut for --param prompt=...
  -P, --param KEY=VALUE   Template-declared param, repeatable

Inspection
  --list-params           Show the template's parameters and exit
  --list-templates        List all template pairs (no -w needed; scans assets/)

Escape hatches
  --no-template           Ignore _tp.json, auto-detect CLIPTextEncode
  --prompt-node-id        Target node in no-template mode
  --node-overrides        Raw JSON node patch, applied last
  --dry-run               Fuse and print, do not submit

Runtime
  -o, --output            Output directory (default ./outputs)
  --host / --port         ComfyUI address (default 127.0.0.1:8188)
  --max-wait              Seconds to wait (default 600; video: 1800+)
  --poll-interval         Poll seconds (default 2.0)
  -v, --verbose           Detailed progress
```

## Bundled Resources

### scripts/
- `comfyui_generate.py` — CLI entry point: load, fuse, submit, poll, download.
- `workflow_template.py` — Template parsing, param resolution, fusion engine.
- `ui_to_api.py` — Converts one UI-format graph file into API format using
  `/object_info`. Handles Reroute, PrimitiveNode, bypass forwarding, isolated
  nodes, and widget type coercion.
- `convert_workflows.py` — Scans a whole directory, detects each file's format,
  and converts every UI-format graph. Start here when the user points at their
  ComfyUI `workflows` folder.
- `_selftest.py` — 44 assertions covering the template engine's edge cases.
  Run it after modifying the engine: `<python> scripts/_selftest.py`.

### references/
- `comfyui_config.md` — API endpoints, template spec, node parameter reference,
  troubleshooting.
- `prompt_conventions.md` — per-workflow prompt dialect: language, shape,
  negative-prompt policy, hard no's, and a worked example for each asset. Read
  this before writing any prompt.

### assets/
- `sdxl_t2i_illustrious.json` / `_tp.json` — SDXL 光辉系 single-pass text-to-image.
- `krea2_t2i.json` / `_tp.json` — Krea2 distilled turbo text-to-image, natural
  language prompts (requires the `Krea2BlockSwap` node — see above).
- `qwen21_edit.json` / `_tp.json` — Qwen-Image 2.1 instruction-based editing.
- `wan22_i2v_4step.json` / `_tp.json` — Wan2.2 I2V 14B four-step image-to-video
  (GGUF dual-model sampling + RIFE + H.264 MP4).
- `wan_animate2.json` / `_tp.json` — Wan-Animate-2 14B character animation
  (reference image + driving video → animated character, no pose extraction).
- `krea2_identity_edit.json` / `_tp.json` — Krea2 character-consistency editing.
- `minimax_h3_i2v.json` / `_tp.json` — MiniMax H3 local image-to-video.
  **Not verified end-to-end on this machine**: the 32B text encoder
  (`qwen3vl_32b_minimax_h3_nvfp4_awq`, 15.7GB) now loads and encodes correctly on
  CPU, but the 20.97GB INT8 UNET still has to fit an 8GB card with no H3 block
  swap available. Treat this workflow as a wiring reference, not a working path.

These are real workflows converted from this machine's ComfyUI graph files and
run against its installed models.

## Examples

**Generate an image:**
> "画一张赛博朋克风格的雨夜街道"
> → `-w assets/sdxl_t2i_illustrious.json --prompt "cyberpunk rainy street at night, neon reflections, cinematic"`

**Generate with specific dimensions:**
> "生成一张竖版手机壁纸，768x1344"
> → add `--param width=768 --param height=1344`

**Reproduce a previous result:**
> "用上次那个种子再来一张"
> → add `--param seed=<seed from the previous JSON summary>`

**Animate a still image into video:**
> "让这张海浪照片动起来"
> → `-w assets/wan22_i2v_4step.json --param image=waves.png --prompt "ocean waves crashing on rocks, slow push-in, locked-off camera" --max-wait 1800`

**Animate a character with a reference video:**
> "让我这个角色跳这段舞"
→ `-w assets/wan_animate2.json --param reference_image=char.png --param driving_video=dance.mp4 --param crop_x=0 --param crop_y=0 --param crop_w=720 --param crop_h=1280 --prompt "人物外观描述：…。背景描述：…。" --max-wait 1800`

**Use their own workflow:**
> "用我 C:/flows/anime.json 这个工作流画个猫娘"
→ `-w C:/flows/anime.json --prompt "..."` plus `C:/flows/anime_tp.json` if it exists

## Design Notes

The template mechanism is adapted from **ComfyFusion Engine**
(`MarcusYuan/mcp-comfyui-anything`, Apache-2.0), specifically its two-stage
fusion model (`user patch + template patch → unified patch → base workflow`) and
the `_tp.json` naming convention.

It was reimplemented rather than adopted wholesale for three reasons: the
original requires six pip dependencies and a resident MCP server process; its
`Patcher.fuse` only handles *pure* placeholders and does no type coercion, so
`--param steps=30` would arrive as the string `"30"`; and it leaves unresolved
placeholders in the workflow instead of failing fast.

Improvements over the original:
- Mixed interpolation (`"{prompt}, masterpiece"`)
- Type coercion driven by declared param types
- Defaults, required validation, `choices`, and min/max bounds
- Pre-submission leftover-placeholder guard
- Non-node entry detection (catches stray `_README` keys)
- Zero external dependencies

Prompt-engineering assets from **Visual Muse**
(`baobaodawang-creater/visual-muse`, MIT-0) — style template library, black-image
retry logic — were considered but not imported: that project targets macOS/Linux
and only handles images, and its style layer assumes an extra LLM call that is
redundant here.

## Planned Enhancements

- Batch generation across multiple prompts
- Image-to-image (input image + img2img workflow)
- Style preset library (port the useful parts of Visual Muse's templates)
- Automatic black-image detection and retry with a new seed
- Model listing and hot-swapping via ComfyUI's `/object_info`
