from .tag_dialect import TagDialectSwitch
from .lora_random import LoraRandomizer
from .nsfw_actions import NsfwActionPicker
from .slot_extras import SlotExtras
from .constructor import (PromptApplyLoras, PromptBase, PromptCharacter, PromptLoras, PromptResult, PromptScene,
                          PromptSettings)

NODE_CLASS_MAPPINGS = {"TagDialectSwitch": TagDialectSwitch, "LoraRandomizer": LoraRandomizer,
                       "NsfwActionPicker": NsfwActionPicker, "SlotExtras": SlotExtras,
                       "PromptBase": PromptBase, "PromptCharacter": PromptCharacter, "PromptScene": PromptScene,
                       "PromptLoras": PromptLoras, "PromptResult": PromptResult, "PromptApplyLoras": PromptApplyLoras,
                       "PromptSettings": PromptSettings}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TagDialectSwitch": "Tag Dialect Switch (e621 / Danbooru)",
    "LoraRandomizer": "LoRA Randomizer (совместимые с чекпоинтом)",
    "NsfwActionPicker": "NSFW Action Picker (действие под состав)",
    "SlotExtras": "Slot Extras (несколько тегов из слота, покров под вид)",
    "PromptBase": "Конструктор · Основа",
    "PromptCharacter": "Конструктор · Персонаж",
    "PromptScene": "Конструктор · Сцена",
    "PromptLoras": "Конструктор · LoRA",
    "PromptResult": "Конструктор · Итог",
    "PromptApplyLoras": "Конструктор · Применить LoRA",
    "PromptSettings": "Конструктор · Настройки модели",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
