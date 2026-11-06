# comfyui-tag-dialect

Random prompt constructor for ComfyUI: coherent tag prompts for SDXL checkpoints with LoRA selection, presets per checkpoint and a floor of safety rules.

## Install

Clone into `custom_nodes` and restart ComfyUI. The ready workflow is in `workflows/`. It needs these node packs: rgthree-comfy, comfyui-image-saver, ComfyUI-Impact-Pack, ComfyUI-Impact-Subpack, ComfyUI-TiledDiffusion, cg-image-filter, ComfyUI-KJNodes, ComfyUI-Custom-Scripts.

The face detailer uses [Fdetailer](https://civitai.com/models/1228695): put `fdetailer_v11.pt` into `models/ultralytics/segm`.

The upscale pass needs an SDXL tile ControlNet in `models/controlnet`, named `controlnet-tile-sdxl.safetensors`. It runs TiledDiffusion at denoise 0.35 with the ControlNet at strength 0.5, released at 0.8 of the schedule. Without the ControlNet the pass draws extra small figures on empty background: the prompt applies to every tile. Held to the end, it removes most of the added detail.

## Dialect

The dialect is `auto` by default: the SHA256 of the checkpoint is looked up on Civitai. A NoobAI base or furry tags give e621, otherwise Danbooru. A checkpoint missing from Civitai falls back to a regex on its path. Answers are cached in `cache.json`. Set `CIVITAI_API_TOKEN` to use your key.

## Prompt constructor

Five nodes in a chain replace the per-tag slots: **Основа → Персонаж → Сцена → LoRA → Итог**. A plan object travels between them, and the prompt is assembled once, in the last node. All tags live in `tags.json`.

- **Основа** sets the dialect, SFW or NSFW, composition and seed, and takes the base negative. A checkpoint that is not SDXL stops the run with a clear error.
- **Персонаж** and **Сцена** have a dropdown for each field. `случайно` lets the node pick; any other value pins the field, and the fields picked after it adapt to it.
- **LoRA** runs after the scene, so it sees the pins. It replaces unpinned fields of its role and leaves pinned ones alone. LoRAs that need a male partner are picked only for `m/f`.
- **Итог** outputs the prompt with LoRA tags, the negative, a report, a recipe line and `clean`, the prompt without LoRA tags for the text encoder.
- **Применить LoRA** loads the plan's LoRAs by file path. Connect it instead of LoraTagLoader: names need no ASCII and no unique prefix.
- **Настройки модели** outputs steps, CFG, sampler, scheduler and CFG rescale for the checkpoint from `presets.json`.

Picks are coherent, not independent. `tags.json` holds composition weights, tag weights, a `locations` table that sets the light and poses for a place, and `species_traits` that set the natural coat of a species. Horns and wings go only to species listed in their group. In a scene with a partner the mood cannot be anger, sadness or boredom.

Paste the recipe into **Основа** to repeat a result with any seed, and delete the fields that should change. A dropdown value overrides the recipe. The LoRA node can be bypassed. **Итог** must not be bypassed: the prompt would be empty. It has no string input, so the negative cannot reach the prompt.

`safety.py` is the floor that no setting removes: the age negative is always added, and stop tags are struck from the final prompt wherever they came from, including recipes, pins and LoRA triggers.

Run the checks without ComfyUI: `python3 -m unittest discover tests`.

## Hint file for a LoRA

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
