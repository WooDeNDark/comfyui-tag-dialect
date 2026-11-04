from .tag_dialect import TagDialectSwitch
from .lora_random import LoraRandomizer
from .nsfw_actions import NsfwActionPicker

NODE_CLASS_MAPPINGS = {"TagDialectSwitch": TagDialectSwitch, "LoraRandomizer": LoraRandomizer,
                       "NsfwActionPicker": NsfwActionPicker}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TagDialectSwitch": "Tag Dialect Switch (e621 / Danbooru)",
    "LoraRandomizer": "LoRA Randomizer (совместимые с чекпоинтом)",
    "NsfwActionPicker": "NSFW Action Picker (действие под состав)",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
