from .tag_dialect import TagDialectSwitch
from .lora_random import LoraRandomizer

NODE_CLASS_MAPPINGS = {"TagDialectSwitch": TagDialectSwitch, "LoraRandomizer": LoraRandomizer}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TagDialectSwitch": "Tag Dialect Switch (e621 / Danbooru)",
    "LoraRandomizer": "LoRA Randomizer (совместимые с чекпоинтом)",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
