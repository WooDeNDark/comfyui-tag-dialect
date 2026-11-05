from .tag_dialect import TagDialectSwitch
from .lora_random import LoraRandomizer
from .nsfw_actions import NsfwActionPicker
from .slot_extras import SlotExtras

NODE_CLASS_MAPPINGS = {"TagDialectSwitch": TagDialectSwitch, "LoraRandomizer": LoraRandomizer,
                       "NsfwActionPicker": NsfwActionPicker, "SlotExtras": SlotExtras}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TagDialectSwitch": "Tag Dialect Switch (e621 / Danbooru)",
    "LoraRandomizer": "LoRA Randomizer (совместимые с чекпоинтом)",
    "NsfwActionPicker": "NSFW Action Picker (действие под состав)",
    "SlotExtras": "Slot Extras (несколько тегов из слота, покров под вид)",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
