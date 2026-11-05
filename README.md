# comfyui-tag-dialect

ComfyUI node **Tag Dialect Switch (e621 / Danbooru)** — decides whether the loaded checkpoint understands e621 tags and passes through the matching prompt branch.

Decision order (`mode = auto`):
1. SHA256 of the checkpoint file → Civitai `model-versions/by-hash` → base model + model tags.
   * base Pony / NoobAI → e621
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

## NSFW Action Picker

Adds an action or an explicit pose that fits the composition (`solo`, `pair_mf`, `pair_ff`, `group`) and the dialect. Pools live in `actions.json`, edit that file to change them. It can also swap the clothing and expression slots for explicit variants (`extras_chance`). A chosen tag removes the tag of the regular slot. The prompt stays unchanged in SFW mode; an `act` LoRA keeps its scene and a `clothing` LoRA keeps its outfit.

## Prompt constructor

Five nodes in a chain replace the per-tag slots: **Основа → Персонаж → Сцена → LoRA → Итог**. A plan object travels between them, and the prompt is assembled once, in the last node. All tags live in `tags.json`.

- **Основа** sets the dialect, SFW or NSFW, composition and seed.
- **Персонаж** and **Сцена** have a dropdown for each field. `случайно` lets the node pick; any other value pins the field, and the other fields adapt to it.
- **LoRA** runs after the scene, so it sees the pins. It replaces unpinned fields of its role and leaves pinned ones alone.
- **Итог** outputs the prompt, the negative, a report and a recipe line.

Paste the recipe into **Основа** to repeat a result with any seed, and delete the fields that should change. A dropdown value overrides the recipe. Every field has its own random stream, so pinning one field keeps the others. The LoRA node can be bypassed.

## Slot Extras

A constructor slot picks one tag. This node adds compatible tags from other groups of the same slot: hands and legs to a pose, furniture to a room, accessories to an outfit. Groups live in `slot_groups.json`: tags of one `axis` exclude each other, `requires` and `forbids` set the rules, including rules between slots. A slot whose tag was removed by a LoRA or by NSFW Action Picker is not extended.

`chance` is the probability of each added tag, `max_per_slot` and `max_total` limit their number. The node also matches fur, scales, feathers and skin from the coat slot to the species.

Place it after NSFW Action Picker. Keep exactly one active tag in each randomizer slot: the number of active tags is preserved between seeds.

## Quality tags

Tag Dialect Switch also prepends family-specific quality tags (Illustrious/NoobAI, Pony score tags, SDXL, Anima) and appends their negative pair.
