# comfyui-tag-dialect

Random prompt constructor for ComfyUI: coherent tag prompts for SDXL checkpoints with LoRA selection, presets per checkpoint and a floor of safety rules.

## Install

Clone into `custom_nodes` and restart ComfyUI. The ready workflow is in `workflows/`. It needs these node packs: rgthree-comfy, comfyui-image-saver, ComfyUI-Impact-Pack, ComfyUI-Impact-Subpack, ComfyUI_UltimateSDUpscale, cg-image-filter, ComfyUI-KJNodes, ComfyUI-Custom-Scripts.

ComfyUI node **Tag Dialect Switch (e621 / Danbooru)** — decides whether the loaded checkpoint understands e621 tags and passes through the matching prompt branch.

Decision order (`mode = auto`):
1. SHA256 of the checkpoint file → Civitai `model-versions/by-hash` → base model + model tags.
   * base NoobAI → e621
   * Civitai tags `furry`, `anthro`, `e621`, `yiff`… or a furry-ish model name → e621
   * otherwise → Danbooru
2. If the model is not on Civitai (or Civitai is unreachable) → `fallback_regex` against the checkpoint path.

`mode = e621 / danbooru` forces the choice. Only the chosen branch is evaluated (lazy inputs).
Hashes and Civitai answers are cached in `cache.json` next to the node. Set `CIVITAI_API_TOKEN` to use your Civitai key.

## LoRA Randomizer

Adds 0..`max_loras` random LoRAs compatible with the current checkpoint (Civitai base model by SHA256, folder name as fallback),
with their trigger words, as `<lora:name:weight>` tags for LoraTagLoader. NSFW LoRAs are used only when the prompt is NSFW (`auto`).

The folder `loras/<family>/<role>/` sets the role (`detail`, `style`, `clothing`, `body`, `sex`, `concept`, `character`) and wins over Civitai tags.
Each role has its own weight range. The weight recommended on Civitai (author's description, then sample images) can only lower the upper bound.

### Hint file for a LoRA

Put `<lora>.txt` next to the LoRA, for example when it is not on Civitai. All lines are optional and override Civitai data:

```
trigger: tag one, tag two
role: style
weight: 0.5-0.7
nsfw: yes
male: no
```

A line without a key is treated as trigger words.

For a whole catalog, put the same data in `user/default/lora_hints.json`, keyed by file name without extension: `{"My_Lora": {"trained": ["tag one"], "role": "style", "rec": {"lo": 0.5, "hi": 0.7, "src": "подсказка"}, "random": false}}`. `random: false` keeps a LoRA out of the random pick; recipes and `always_include` still reach it. A text file next to the LoRA wins over this file.

## NSFW Action Picker

Adds an action or an explicit pose that fits the composition (`solo`, `pair_mf`, `pair_ff`, `group`) and the dialect. Pools live in `actions.json`, edit that file to change them. It can also swap the clothing and expression slots for explicit variants (`extras_chance`). A chosen tag removes the tag of the regular slot. The prompt stays unchanged in SFW mode; an `act` LoRA keeps its scene and a `clothing` LoRA keeps its outfit.

## Prompt constructor

Five nodes in a chain replace the per-tag slots: **Основа → Персонаж → Сцена → LoRA → Итог**. A plan object travels between them, and the prompt is assembled once, in the last node. All tags live in `tags.json`.

- **Основа** sets the dialect, SFW or NSFW, composition and seed, and takes the base negative. A checkpoint that is not SDXL stops the run with a clear error.
- **Персонаж** and **Сцена** have a dropdown for each field. `случайно` lets the node pick; any other value pins the field, and the fields picked after it adapt to it.
- **LoRA** runs after the scene, so it sees the pins. It replaces unpinned fields of its role and leaves pinned ones alone. LoRAs that need a male partner are picked only for `m/f`.
- **Итог** outputs the prompt with LoRA tags, the negative, a report, a recipe line and `clean`, the prompt without LoRA tags for the text encoder.
- **Применить LoRA** loads the plan's LoRAs by file path. Connect it instead of LoraTagLoader: names need no ASCII and no unique prefix.
- **Настройки модели** outputs steps, CFG, sampler, scheduler and CFG rescale for the checkpoint from `presets.json`.

Picks are coherent, not independent. `tags.json` holds composition weights, tag weights, a `locations` table that sets the light and poses for a place, and `species_traits` that set the natural coat of a species. Horns and wings go only to species listed in their group. In a scene with a partner the mood cannot be anger, sadness or boredom.

Paste the recipe into **Основа** to repeat a result with any seed, and delete the fields that should change. A dropdown value overrides the recipe. The LoRA node can be bypassed. **Итог** must not be bypassed: it has no string input, so a bypass stops the run and the negative cannot reach the prompt.

`safety.py` is the floor that no setting removes: the age negative is always added, and stop tags are struck from the final prompt wherever they came from, including recipes, pins and LoRA triggers.

Run the checks without ComfyUI: `python3 -m unittest discover tests`.

## Slot Extras

A constructor slot picks one tag. This node adds compatible tags from other groups of the same slot: hands and legs to a pose, furniture to a room, accessories to an outfit. Groups live in `slot_groups.json`: tags of one `axis` exclude each other, `requires` and `forbids` set the rules, including rules between slots. A slot whose tag was removed by a LoRA or by NSFW Action Picker is not extended.

`chance` is the probability of each added tag, `max_per_slot` and `max_total` limit their number. The node also matches fur, scales, feathers and skin from the coat slot to the species.

Place it after NSFW Action Picker. Keep exactly one active tag in each randomizer slot: the number of active tags is preserved between seeds.

## Quality tags

Tag Dialect Switch also prepends family-specific quality tags (Illustrious/NoobAI, Pony score tags, SDXL, Anima) and appends their negative pair.
