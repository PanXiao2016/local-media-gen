# Prompt Conventions by Workflow

Each workflow in `assets/` was trained on a different kind of text. The same
sentence that works in one will actively hurt another. **Look up the asset
before writing the prompt** — never carry a prompt style across models.

The key is the **asset**, not the model family. Krea2 text-to-image wants long
natural-language sentences; Krea2 identity-edit wants a short edit instruction in
English. Same model, opposite rules. SDXL wants Danbooru tags; Krea2 explicitly
rejects them.

## Quick reference

| Asset | Language | Shape | Negative | Hard no |
|---|---|---|---|---|
| `sdxl_t2i_illustrious` | English | Comma-separated Danbooru tags + quality words | Required, score-based | Full sentences |
| `krea2_t2i` | English | One flowing natural-language paragraph | Ignored (cfg=1.0) | Danbooru tags |
| `qwen21_edit` | Chinese or English | One imperative instruction | Usually empty | Quality words, style words |
| `krea2_identity_edit` | English preferred | One edit instruction | Usually empty | Long scene description |
| `wan22_i2v_4step` | English preferred | Scene + camera + motion | Camera/morph negatives | Static-scene description only |
| `wan_animate2` | Chinese (official) | `人物外观描述：…。背景描述：…。` | Present but inert at cfg=1.0 | Action words, mood words |
| `wa_scail2` | English (official) | Appearance + **action** + scene | Standard video negatives | Appearance-only prompts |
| `minimax_h3_i2v` | English preferred | Motion description | Standard video negatives | Long appearance descriptions |

## sdxl_t2i_illustrious

Illustrious/光辉系 is a Danbooru-tag anime model. Tags, not prose.

- Prefix with quality tags: `masterpiece, best quality, amazing quality`.
- Subject tags in Danbooru order: `1girl, solo, long hair, silver hair, red eyes, …`
- Then scene, then style tags.
- Negative is load-bearing — the bundled default already carries
  `score_1, score_2, score_3, worst quality, low quality, …`. Keep the score
  tags; they are what this family uses to suppress low-rated outputs.
- Underscores or spaces both work; pick one and stay consistent.

```
masterpiece, best quality, 1girl, solo, long black hair, blunt bangs, red eyes,
white hairband, red and white corset dress, long sleeves, pearl necklace,
upper body, looking at viewer, soft indoor lighting, depth of field
```

## krea2_t2i

Distilled turbo model on Qwen3-VL text encoding. It reads **sentences**, and tag
soup degrades it badly.

- Write one paragraph: subject, then appearance, then environment, then light,
  then lens/medium if relevant.
- No `masterpiece`, no `1girl`, no comma-stacked tags.
- `negative` is effectively dead at `cfg=1.0`. Leave it empty rather than
  pretending it does something.

```
A young woman with long straight black hair and blunt bangs stands in a softly
lit room, wearing a red and white corset dress with long sleeves. Warm afternoon
light falls from the left, shallow depth of field, semi-realistic 3D render.
```

## qwen21_edit

Instruction-following image editor. It already sees the source image, so
describing what is already there wastes attention.

- Say only what should **change**: `把背景换成深夜的霓虹街道`.
- One instruction per run. Chaining two changes in one sentence usually lands
  only one of them.
- No quality tags, no style boilerplate.
- `negative` stays empty.

## krea2_identity_edit

Same idea as `qwen21_edit` but with the character fed in as a LoRA reference.
English is measurably more stable here.

- One imperative sentence: `make her wear a dark red velvet evening gown`.
- Do not re-describe the face or hair — the LoRA reference carries identity.
- Keep it short. Long prompts start overriding the reference.

## wan22_i2v_4step

Image-to-video. The first frame is fixed, so describe **what moves**, not what
is already visible.

- Scene anchor in one clause, then motion, then camera behaviour.
- Say explicitly when the camera is locked: `locked-off static camera`.
- The bundled negative already suppresses `camera shake, jitter, flickering,
  morphing, face deformation` — extend it rather than replace it.
- For subtle motion, describe the *magnitude*: `she only sinks a little further
  down, in one single very slow motion, then holds still`.

## wan_animate2

Character animation. The prompt's only job is to anchor appearance; motion comes
from the driving video. **The official shape is a description of the reference
image, not of the output.**

```
人物外观描述：一名黑色长直发的年轻女性，齐刘海，红色眼眸，头戴白色发箍，
佩戴金色耳坠与红色珍珠颈链。上身穿白色与红色拼接的紧身胸衣礼服，红色长袖。
背景描述：背景为浅色室内房间，光线柔和均匀。
```

Rules that matter, in order of how often they are broken:

1. **Describe the reference image, not the motion.** Writing the dance into the
   prompt fights the pose branch.
2. **No action verbs.** `站着`, `跳舞`, `转头` all belong to the driving video.
3. **No mood or evaluation.** The official guidance says to ignore subjective
   judgement and emotional inference — `温柔的`, `唯美的`, `高级感` add noise.
4. **Match the reference.** Wrong hair colour or wrong outfit visibly degrades
   output. If the description and the image disagree, the image wins and the
   prompt becomes a drag.
5. Fine detail is carried by the reference latent, not the text. Coarse anchors
   (hair, eyes, garment type and dominant colour, background kind) are enough —
   do not enumerate every accessory.
6. `negative` is inert at `cfg=1.0` on the distill checkpoint. It exists for the
   base checkpoint at cfg 5–8.

## wa_scail2

Character replacement. **This is the exact opposite of `wan_animate2`** — and
getting it backwards is the single most common mistake, because both take a
reference image plus a driving video.

`wan_animate2` wants an objective description of the *reference image* and
forbids action words. `wa_scail2` wants a description of the *final output*,
and the action belongs in it. The official blueprint's own prompt:

```
A young woman with dark hair tied in a neat high bun, with a few loose strands
framing her face, is dancing outdoors on a sunny coastal hillside. She has a
normal-sized head and a slim face, with no hat, no headwear, and no oversized
hair volume. She wears a fitted black long-sleeve crop top with a shoulder
cutout, extremely baggy black cargo pants with straps and pockets, and chunky
black combat boots. She performs energetic dance moves with one leg lifted and
arms extended, moving naturally in front of a large tree, a small white stone
house with a terracotta roof, and a bright blue sea.
```

Read that closely — it carries four things at once:

1. **Who** — hair, face shape, build.
2. **What she is doing** — `is dancing`, `performs energetic dance moves with
   one leg lifted and arms extended`. Omit this and the motion goes limp.
3. **What she wears** — down to garment cut and colour. Vague clothing lets the
   model improvise, and it improvises toward more revealing.
4. **Where** — the scene, which matters because `replacement_mode=true` keeps
   the driving video's background; the text helps the model reconcile the two.

Two habits from the official example worth copying:

- **Negative constraints inside the positive prompt.** `no hat, no headwear, and
  no oversized hair volume` — the model over-generates headwear and hair volume
  on its own, so it is told not to. Same trick for anything the reference lacks.
- **Explicit garment coverage.** If the reference outfit has a high neckline,
  say so (`fully covering her chest, modest neckline`). Observed drift: a corset
  gown came back noticeably more open than the source image.

`negative` does work here (`cfg=5` by default), unlike the distill `wan_animate2`
path — the bundled default is the standard Chinese video negative.

## minimax_h3_i2v

Motion-first, like `wan22_i2v_4step`, but the model is more sensitive to
over-long prompts. Keep the appearance clause to a handful of words and spend
the rest on movement and camera.

## When the user hands you a prompt

Do not silently rewrite it into the target convention if the mismatch is severe
(tags into a natural-language model, or a full scene description into an edit
instruction). Say what the model expects and offer a converted version — the
user usually has a reason for the wording, and a silent rewrite makes the result
hard to reproduce.
