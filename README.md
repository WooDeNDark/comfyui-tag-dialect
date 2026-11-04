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
