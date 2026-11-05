"""Действие, откровенная поза, одежда и эмоция для режима NSFW: под состав и словарь из actions.json.

В режиме SFW промпт не меняется. Сцену, заданную act-LoRA, и одежду, заданную clothing-LoRA, нода не подменяет.
Выбранный вариант вычёркивает тег обычного слота: иначе в промпте будут две разные позы или два наряда.
Сцена из действия или act-LoRA убирает обычную позу и кадры, где сцены не видно.
"""
import json
import os
import random
import re

from .lora_random import NSFW_PROMPT, _remove_tags, _slot_tags
from .tag_dialect import _join, composition

ACTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actions.json")
LORA_TAG_RE = re.compile(r"\s*<lora:[^>]+>")
ACT_LORA_RE = re.compile(r"\[act\b")
GROUP_RE = re.compile(r"(^|,)\s*(trio|group|3girls|multiple girls)\s*(,|$)", re.I)
CLOTHING_LORA_RE = re.compile(r"\[clothing\b")
NARROW_FRAMES = ("portrait", "headshot portrait", "three-quarter portrait", "close-up", "upper body")
# (имя, пул, слоты, заголовок, множитель к extras_chance). Маленькие пулы срабатывают реже,
# иначе половина картинок попадёт в пару одинаковых локаций и ракурсов.
EXTRAS = (("clothing", "nsfw_clothing", ["Одежда"], "Одежда", 1.0),
          ("expression", "nsfw_expression", ["Эмоция"], "Эмоция", 1.0),
          ("camera", "nsfw_camera", ["Кадр"], "Кадр", 0.3),
          ("location", "nsfw_location", ["Окружение"], "Окружение", 0.2),
          ("body", "nsfw_body", ["Телосложение"], "Телосложение", 0.3))


def load_actions():
    with open(ACTIONS_PATH, encoding="utf-8") as f:
        return json.load(f)


def pool_key(text):
    """solo / pair_mf / pair_ff / group — или None, если состав в промпте не указан."""
    solo, multi, male = composition(text)
    if solo:
        return "solo"
    if male:
        return "pair_mf"
    if not multi:
        return None
    return "group" if GROUP_RE.search(text or "") else "pair_ff"


class NsfwActionPicker:
    """Добавляет в NSFW-промпт случайное действие или позу под состав."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "dialect": ("STRING", {"forceInput": True}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "chance": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "Вероятность добавить действие; в остальных случаях остаётся слот «Поза»"}),
                "pose_slots": ("STRING", {"default": "Поза", "tooltip": "Начало заголовка слотов, которые действие заменяет"}),
                "extras_chance": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05,
                                            "tooltip": "Вероятность заменить слоты «Одежда», «Эмоция», «Кадр», «Окружение», «Телосложение» откровенным вариантом"}),
            },
            "optional": {
                "lora_info": ("STRING", {"forceInput": True,
                                         "tooltip": "Выход info ноды LoRA: если взята act-LoRA, сцену задаёт она"}),
            },
            "hidden": {"prompt": "PROMPT"},
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "info")
    FUNCTION = "run"
    CATEGORY = "utils/prompt"

    def run(self, text, dialect, seed, chance, pose_slots, extras_chance=0.5, lora_info=None, prompt=None):
        text = text or ""
        tags = LORA_TAG_RE.findall(text)
        body = LORA_TAG_RE.sub("", text)
        lora_info = lora_info or ""
        if not NSFW_PROMPT.search(body):
            info = "Действие: нет, режим SFW"
            print(f"[NsfwAction] {info}")
            return {"ui": {"text": [info]}, "result": (text, info)}

        pools = load_actions().get(dialect, {})
        rnd = random.Random(seed)
        notes = []

        def swap(slots, tag):
            """Вставляет тег вместо тега обычного слота."""
            nonlocal body
            body, removed = _remove_tags(body, _slot_tags(prompt, slots))
            body = _join(body, tag)
            return f"; убрано: {', '.join(removed)}" if removed else ""

        def clear_scene(prefixes):
            """Убирает позу слота и кадр, в который сцена не поместится."""
            nonlocal body
            frames = [t for t in _slot_tags(prompt, ["Кадр"]) if t.lower() in NARROW_FRAMES]
            body, removed = _remove_tags(body, _slot_tags(prompt, prefixes) + frames)
            return f"; убрано: {', '.join(removed)}" if removed else ""

        key = pool_key(body)
        pool = pools.get(key or "", [])
        prefixes = [p.strip() for p in pose_slots.split(",") if p.strip()]
        if ACT_LORA_RE.search(lora_info):
            removed = clear_scene(prefixes)
            body = _join(body, "sex")
            notes.append("Действие: задаёт act-LoRA" + removed)
        elif not pool:
            notes.append("Действие: нет, состав не указан")
        elif rnd.random() >= chance:
            notes.append("Действие: нет, оставлена поза слота")
        else:
            action = rnd.choice(pool)
            removed = clear_scene(prefixes)
            body = _join(body, action)
            notes.append(f"Действие [{key}]: {action}" + removed)

        for name, pool_name, slots, title, rate in EXTRAS:
            extra = pools.get(pool_name) or []
            if name == "clothing" and CLOTHING_LORA_RE.search(lora_info):
                notes.append("Одежда: задаёт LoRA")
            elif extra and rnd.random() < extras_chance * rate:
                tag = rnd.choice(extra)
                notes.append(f"{title}: {tag}" + swap(slots, tag))

        info = "\n".join(notes)
        print(f"[NsfwAction] {info}")
        out = body + "".join(tags)
        return {"ui": {"text": [info]}, "result": (out, info)}
