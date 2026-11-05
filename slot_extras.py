"""Несколько совместимых тегов из слотов конструктора и покров под вид персонажа.

Слот выбирает один тег. Нода смотрит, к какой группе slot_groups.json он относится, и с вероятностью chance
добавляет теги из групп других осей: к позе руки и ноги, к месту детали, к одежде аксессуары.
Теги одной оси несовместимы между собой. Слот, чей тег убрала LoRA или нода действий, не расширяется.

Правила группы: axis — ось; requires и requires_also — нужна хотя бы одна из групп каждого списка;
forbids — группы, с которыми нельзя; primary_only — только прямой выбор слота; clothed — только в SFW.
"""
import json
import os
import random
import re

from .lora_random import NSFW_PROMPT, _slot_tags

GROUPS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "slot_groups.json")
LORA_TAG_RE = re.compile(r"\s*<lora:[^>]+>")
COVER_RE = re.compile(r"\b(fur|scales|feathers|skin)\b")


def load_groups():
    with open(GROUPS_PATH, encoding="utf-8") as f:
        return json.load(f)


def _split(body):
    return [t.strip() for t in body.split(",") if t.strip()]


def _allowed(group, axes, seen, nsfw):
    """Можно ли добавить тег из группы при уже выбранных группах."""
    if group["axis"] in axes or group.get("primary_only") or (nsfw and group.get("clothed")):
        return False
    names = {g["name"] for g in seen}
    for key in ("requires", "requires_also"):
        if group.get(key) and not names.intersection(group[key]):
            return False
    if names.intersection(group.get("forbids", [])):
        return False
    return not any(group["name"] in g.get("forbids", []) for g in seen)


def _pick_extras(tags, slots, prompt, rnd, chance, max_per_slot, max_total, nsfw):
    """Возвращает [(слот, тег)]. Сначала собирает выбор всех слотов: правила бывают между слотами."""
    present = {t.lower() for t in tags}
    seen, axes, active = [], {}, []
    for title, groups in slots.items():
        picked = {t.lower() for t in _slot_tags(prompt, [title])} & present
        own = [g for g in groups if picked.intersection(t.lower() for t in g["tags"])]
        if own:
            seen += own
            axes[title] = {g["axis"] for g in own}
            active.append(title)
    rnd.shuffle(active)
    added = []
    for title in active:
        for _ in range(max_per_slot):
            if len(added) >= max_total or rnd.random() >= chance:
                break
            cands = [g for g in slots[title] if _allowed(g, axes[title], seen, nsfw)
                     and any(t.lower() not in present for t in g["tags"])]
            if not cands:
                break
            group = rnd.choice(cands)
            tag = rnd.choice([t for t in group["tags"] if t.lower() not in present])
            seen.append(group)
            axes[title].add(group["axis"])
            present.add(tag.lower())
            added.append((title, tag))
    return added


def _fit_cover(tags, added_tags, cover_data, species, slot_picks, rnd):
    """Меняет мех, чешую, перья и кожу в тегах окраса на покров вида. Возвращает новые теги и заметки."""
    cover = cover_data["species"].get(species.lower(), cover_data["default"])
    if not cover:
        return tags, []
    known = set(cover_data["tags"][cover])
    out, notes = [], []
    for tag in tags:
        m = COVER_RE.search(tag)
        is_cover_tag = tag in added_tags or tag.lower() in slot_picks
        if not is_cover_tag or not m or m.group(1) == cover:
            out.append(tag)
            continue
        new = COVER_RE.sub(cover, tag)
        if new not in known:
            new = None if tag in added_tags else rnd.choice(cover_data["fallback"][cover])
        if new:
            out.append(new)
        notes.append(f"Покров: {tag} → {new or 'убран'} ({species})")
    return out, notes


class SlotExtras:
    """Добавляет к тегам слотов совместимые теги и согласует окрас с видом."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "dialect": ("STRING", {"forceInput": True}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "chance": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "Вероятность добавить к слоту очередной тег"}),
                "max_per_slot": ("INT", {"default": 2, "min": 0, "max": 5,
                                         "tooltip": "Сколько тегов можно добавить к одному слоту"}),
                "max_total": ("INT", {"default": 6, "min": 0, "max": 30,
                                      "tooltip": "Сколько тегов можно добавить ко всему промпту"}),
            },
            "hidden": {"prompt": "PROMPT"},
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "info")
    FUNCTION = "run"
    CATEGORY = "utils/prompt"

    def run(self, text, dialect, seed, chance, max_per_slot, max_total, prompt=None):
        text = text or ""
        data = load_groups().get(dialect)
        if not data:
            info = f"Детали: нет групп для словаря {dialect}"
            return {"ui": {"text": [info]}, "result": (text, info)}

        lora_tags = LORA_TAG_RE.findall(text)
        tags = _split(LORA_TAG_RE.sub("", text))
        rnd = random.Random(seed)
        nsfw = bool(NSFW_PROMPT.search(", ".join(tags)))
        added = _pick_extras(tags, data["slots"], prompt, rnd, chance, max_per_slot, max_total, nsfw)
        notes = [f"{title}: + {tag}" for title, tag in added]
        tags += [tag for _, tag in added]

        present = {t.lower() for t in tags}
        species = [t for t in _slot_tags(prompt, ["Вид"]) if t.lower() in present]
        if species and data.get("cover"):
            coat_extras = {tag for title, tag in added if title == "Окрас"}
            coat_picks = {t.lower() for t in _slot_tags(prompt, ["Окрас"])}
            tags, cover_notes = _fit_cover(tags, coat_extras, data["cover"], species[0], coat_picks, rnd)
            notes += cover_notes

        info = "\n".join(notes) or "Детали: без добавлений"
        print(f"[SlotExtras] {info}")
        return {"ui": {"text": [info]}, "result": (", ".join(tags) + "".join(lora_tags), info)}
