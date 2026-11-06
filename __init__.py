from .constructor import (PromptApplyLoras, PromptAutoPick, PromptBase, PromptCharacter, PromptLoras, PromptResult, PromptScene,
                          PromptSettings)

NODE_CLASS_MAPPINGS = {"PromptBase": PromptBase, "PromptCharacter": PromptCharacter, "PromptScene": PromptScene,
                       "PromptLoras": PromptLoras, "PromptResult": PromptResult, "PromptApplyLoras": PromptApplyLoras,
                       "PromptSettings": PromptSettings, "PromptAutoPick": PromptAutoPick}
NODE_DISPLAY_NAME_MAPPINGS = {
    "PromptBase": "Конструктор · Основа",
    "PromptCharacter": "Конструктор · Персонаж",
    "PromptScene": "Конструктор · Сцена",
    "PromptLoras": "Конструктор · LoRA",
    "PromptResult": "Конструктор · Итог",
    "PromptApplyLoras": "Конструктор · Применить LoRA",
    "PromptSettings": "Конструктор · Настройки модели",
    "PromptAutoPick": "Конструктор · Автоотбор",
}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
