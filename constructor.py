"""Конструктор промпта: пять нод в цепочке Основа → Персонаж → Сцена → LoRA → Итог.

Между нодами идёт план: словарь с полями, а не строка. Каждый этап выбирает только своё, поэтому тег не приходится
вычёркивать из готового промпта. LoRA стоит после сцены и видит все закрепления: незакреплённое поле она заменяет,
закреплённое не трогает. Строки собираются один раз, в «Итоге».

У каждого поля свой поток случайных чисел от общего seed: закрепление одного поля не меняет остальные.
Строка повтора перечисляет всё выбранное: вставленная в «Основу», она закрепляет те поля, что в ней остались.
"""
import copy
import json
import os
import random
import re

from .lora_random import (DEF_QUOTAS, DEF_WEIGHTS, ROLES, _parse_map, _triggers, choose_loras,
                          lora_catalog)
from .safety import AGE_GUARD, strip_stopped
from .slot_extras import _allowed
from .tag_dialect import (DEFAULT_FALLBACK, QUALITY_NEG, QUALITY_POS, _join, checkpoint_info, composition,
                          composition_negative, decide)

TAGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tags.json")
PRESETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")
PLAN = "TAG_PLAN"
RANDOM = "случайно"
CHARACTER_FIELDS = ("Вид", "Телосложение", "Окрас", "Волосы", "Глаза", "Особенности")
# Свет зависит от места, кадр — от сцены
SCENE_FIELDS = ("Окружение", "Свет", "Эмоция", "Стиль", "Сцена", "Кадр", "Поза", "Одежда")
PROMPT_ORDER = ("Стиль", "Вид", "Телосложение", "Окрас", "Волосы", "Глаза", "Одежда", "Эмоция", "Поза", "Кадр",
                "Окружение", "Свет", "Особенности", "Сцена", "Фон")
RECIPE_ORDER = ("Состав",) + PROMPT_ORDER + ("LoRA",)
NARROW_FRAMES = ("portrait", "headshot portrait", "three-quarter portrait", "close-up")
COVER_RE = re.compile(r"\b(fur|scales|feathers|skin)\b")
LORA_RE = re.compile(r"^(.+):(\d+(?:\.\d+)?)$")
ROLE_FIELDS = {"clothing": ("Одежда",), "style": ("Стиль",), "body": ("Телосложение",), "act": ("Сцена", "Поза")}
_cache = {}


def load_tags():
    mtime = os.path.getmtime(TAGS_PATH)
    if _cache.get("mtime") != mtime:
        with open(TAGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        data["index"] = {g["name"]: g for field in data["fields"].values() for g in field["groups"]}
        data["field_names"] = {name: {g["name"] for g in field["groups"]} for name, field in data["fields"].items()}
        _cache.update(mtime=mtime, data=data)
    return _cache["data"]


def load_presets():
    with open(PRESETS_PATH, encoding="utf-8") as f:
        return json.load(f)


def preset_for(ckpt_name):
    """Настройки чекпоинта: первая подходящая строка поверх значений по умолчанию."""
    presets = load_presets()
    name = (ckpt_name or "").lower()
    for model in presets["models"]:
        if model["match"] in name:
            return {**presets["default"], **model}
    return dict(presets["default"])


def check_supported(ckpt_name):
    """Понятная ошибка вместо KeyError из CLIPTextEncodeSDXL."""
    for key, base in load_presets()["unsupported"].items():
        if key.lower() in (ckpt_name or "").lower():
            raise ValueError(f"Этот workflow работает только с SDXL-чекпоинтами, а {ckpt_name} собран на {base}")


def parse_recipe(text):
    """«Вид: wolf; Поза: standing, hand on hip» → {поле: [теги]}. Сцена остаётся одной строкой."""
    pins = {}
    for part in (text or "").replace("\n", ";").split(";"):
        key, sep, value = part.partition(":")
        if not sep or not key.strip():
            continue
        key = key.strip()
        tags = [t.strip() for t in value.split(",") if t.strip()]
        pins[key] = [", ".join(tags)] if key == "Сцена" and tags else tags
    return pins


def field_rng(plan, name):
    return random.Random(f"{plan['seed']}:{name}")


def available(data, plan, tags):
    """Теги, которые можно написать в словаре запуска и которые разрешены рейтингом и сценой."""
    banned = set(plan.get("ban_tags", ())) | set(data["rating"][plan["rating"]].get("exclude", ()))
    tags = [t for t in tags if t not in banned]
    if plan["dialect"] != "danbooru":
        return tags
    names = data["danbooru_names"]
    return [t for t in tags if t not in names or names[t]]


def weighted_pick(rnd, data, pairs):
    """pairs: [(тег, множитель группы)]. Вес тега берётся из tags.json: частые теги выпадают чаще редких."""
    weights = [data["weights"].get(t, 1.0) * m for t, m in pairs]
    return rnd.choices([t for t, _ in pairs], weights=weights)[0] if sum(weights) > 0 else None


def species_fits(plan, group):
    """Рога и крылья только у видов, которым они свойственны."""
    allowed = group.get("species_only")
    species = plan["fields"].get("Вид", {}).get("tags")
    return not allowed or bool(species and species[0].lower() in allowed)


def seen_groups(data, plan):
    return [data["index"][n] for n in plan["groups"]]


def primary_ok(group, seen, own_names):
    """Обычный выбор поля: правила между полями обязательны, а внутри поля действуют только для добавок."""
    names = {g["name"] for g in seen}
    for key in ("requires", "requires_also"):
        need = set(group.get(key, []))
        if need and not need & own_names and not need & names:
            return False
    if names.intersection(group.get("forbids", [])):
        return False
    return not any(group["name"] in g.get("forbids", []) for g in seen)


def set_field(plan, data, name, tags, pinned=False):
    """Записывает поле и отмечает группы его тегов."""
    plan["fields"][name] = {"tags": tags, "pinned": pinned}
    for group in data["fields"].get(name, {}).get("groups", []):
        if set(tags).intersection(group["tags"] + group.get("nsfw_tags", [])) and group["name"] not in plan["groups"]:
            plan["groups"].append(group["name"])


def pick_primary(plan, data, name, rnd, skip=(), only_axes=None):
    """Один тег поля. Откровенный вариант из той же группы выпадает с вероятностью nsfw_rate."""
    field = data["fields"][name]
    seen = seen_groups(data, plan)
    groups = [g for g in field["groups"] if primary_ok(g, seen, data["field_names"][name]) and species_fits(plan, g)
              and (only_axes is None or g["axis"] in only_axes)]
    if plan["rating"] == "NSFW" and rnd.random() < field.get("nsfw_rate", 0):
        pool = [(t, 1.0) for g in groups for t in available(data, plan, g.get("nsfw_tags", []))]
        if pool:
            return weighted_pick(rnd, data, pool)
    pool = [(t, g.get("weight", 1.0)) for g in groups for t in available(data, plan, g["tags"]) if t not in skip]
    return weighted_pick(rnd, data, pool) if pool else None


def pick_field(plan, data, name, rnd):
    """Теги поля: presence решает, есть ли поле, axes даёт по тегу на главную ось."""
    field = data["fields"][name]
    if rnd.random() >= field.get("presence", 1):
        return []
    picks = [pick_primary(plan, data, name, rnd, only_axes=axes) for axes in field.get("axes", [None])]
    return [t for t in picks if t]


def pick_coat(plan, data, rnd):
    """Природный окрас вида. Яркий окрас из общего поля выпадает редко."""
    species = plan["fields"].get("Вид", {}).get("tags") or [""]
    trait = data["species_traits"].get(species[0].lower())
    tuning = data["tuning"]
    if not trait or rnd.random() < tuning["bright_coat"]:
        return pick_field(plan, data, "Окрас", rnd)
    coat = available(data, plan, trait["coat"]) or trait["coat"]
    tags = [rnd.choice(coat)]
    pattern = available(data, plan, trait["pattern"])
    if pattern and rnd.random() < tuning["pattern_chance"]:
        tags.append(rnd.choice(pattern))
    return tags


def pick_location(plan, data):
    """Место задаёт подходящий свет и позы. Возвращает строку таблицы или None для места вне таблицы."""
    locations = data["locations"]
    if "Окружение" in plan["pins"]:
        tags = plan["fields"]["Окружение"]["tags"]
        return next((l for l in locations if l["tag"] in tags), None)
    rnd = field_rng(plan, "Окружение")
    if plan["rating"] == "NSFW" and rnd.random() < data["fields"]["Окружение"].get("nsfw_rate", 0):
        tag = pick_primary(plan, data, "Окружение", random.Random(f"{plan['seed']}:место"))
        set_field(plan, data, "Окружение", [tag] if tag else [])
        return next((l for l in locations if l["tag"] == tag), None)
    pool = [l for l in locations if available(data, plan, [l["tag"]])]
    location = rnd.choices(pool, weights=[l["weight"] for l in pool])[0]
    set_field(plan, data, "Окружение", [location["tag"], "indoors" if location["inside"] else "outdoors"])
    return location


def from_location(data, plan, options):
    """Варианты из таблицы мест, все теги которых можно написать в словаре запуска."""
    return [o for o in options if len(available(data, plan, split_tags(o))) == len(split_tags(o))]


def split_tags(text):
    return [t.strip() for t in text.split(",") if t.strip()]


def pick_ambient(plan, data):
    """Частые у людей теги вне полей. Считаются в конце: LoRA могла снять или надеть одежду."""
    if "Фон" in plan["pins"]:
        return list(plan["pins"]["Фон"])
    rnd = field_rng(plan, "Фон")
    worn = plan["fields"].get("Одежда", {}).get("tags", [])
    tags = []
    for item in data["ambient"]:
        fits = item.get("rating") in (None, plan["rating"]) and (item.get("when") != "nude" or "nude" in worn)
        if rnd.random() < item["chance"] and fits:
            tags.append(item["tag"])
    return tags


def partner_ban(data):
    """Эмоции, которые в сцене с партнёром читаются как несогласие."""
    groups = data["fields"]["Эмоция"]["groups"]
    return sorted({t for g in groups if g["name"] in ("bad", "calm") for t in g["tags"]})


def rating_of(data, plan):
    return data.get("rating_family", {}).get(plan["family"], data["rating"]).get(plan["rating"]) or data["rating"][plan["rating"]]


def add_extras(plan, data, names, chance, max_per_field, max_total):
    """Добавляет к незакреплённым полям совместимые теги других осей. Возвращает заметки."""
    nsfw = plan["rating"] == "NSFW"
    has_scene = bool(plan["fields"].get("Сцена", {}).get("tags"))
    order = random.Random(f"{plan['seed']}:порядок")
    active = [n for n in names if n in data["fields"] and n in plan["fields"]
              and not plan["fields"][n]["pinned"]]
    order.shuffle(active)
    added, notes = 0, []
    for name in active:
        rnd = field_rng(plan, name + ":детали")
        tags = plan["fields"][name]["tags"]
        groups = data["fields"][name]["groups"]
        if not tags and name != "Одежда":
            continue
        skip = set(tags) | (set(NARROW_FRAMES) if has_scene and name == "Кадр" else set())
        axes = {g["axis"] for g in groups if set(tags).intersection(g["tags"] + g.get("nsfw_tags", []))}
        for _ in range(max_per_field):
            if added >= max_total or rnd.random() >= chance:
                break
            seen = seen_groups(data, plan)
            cands = [g for g in groups if g.get("weight", 1) > 0 and species_fits(plan, g) and _allowed(g, axes, seen, nsfw)
                     and [t for t in available(data, plan, g["tags"]) if t not in skip]]
            if not cands:
                break
            pool = [(t, g.get("weight", 1.0)) for g in cands for t in available(data, plan, g["tags"]) if t not in skip]
            tag = weighted_pick(rnd, data, pool)
            group = next(g for g in cands if tag in g["tags"])
            tags.append(tag)
            skip.add(tag)
            axes.add(group["axis"])
            if group["name"] not in plan["groups"]:
                plan["groups"].append(group["name"])
            added += 1
            notes.append(f"{name}: + {tag}")
    return notes


def fit_cover(plan, data):
    """Приводит мех, чешую, перья и кожу незакреплённого окраса к виду персонажа."""
    species = plan["fields"].get("Вид", {}).get("tags")
    coat = plan["fields"].get("Окрас")
    if not species or not coat or coat["pinned"]:
        return []
    cover_data = data["cover"]
    cover = cover_data["species"].get(species[0].lower(), cover_data["default"])
    if not cover:
        return []
    known = set(available(data, plan, cover_data["tags"][cover]))
    fallback = available(data, plan, cover_data["fallback"][cover])
    rnd = field_rng(plan, "Окрас:покров")
    colors = set(data["index"]["color"]["tags"])
    has_color = any(t in colors for t in coat["tags"][1:])
    out, notes = [], []
    for i, tag in enumerate(coat["tags"]):
        m = COVER_RE.search(tag)
        if not m or m.group(1) == cover:
            out.append(tag)
            continue
        new = COVER_RE.sub(cover, tag)
        if new not in known:
            new = rnd.choice(fallback) if i == 0 and fallback and not has_color else None
        if new:
            out.append(new)
        notes.append(f"Покров: {tag} → {new or 'убран'} ({species[0]})")
    coat["tags"] = out
    return notes


def apply_dropdowns(plan, pins):
    """Выбор в списке ноды важнее строки повтора: можно вставить строку и поменять одно поле."""
    for name, value in pins.items():
        if value != RANDOM:
            plan["pins"][name] = [value]


def combo(name, data):
    field = data["fields"].get(name)
    tags = []
    for group in (field or {"groups": []})["groups"]:
        tags += group["tags"] + group.get("nsfw_tags", [])
    return ([RANDOM] + sorted(set(tags)), {"default": RANDOM})


def fill_fixed(plan, data, names):
    """Закреплённые поля записываются первыми: остальные подстраиваются под них."""
    for name in names:
        if name in plan["pins"]:
            set_field(plan, data, name, list(plan["pins"][name]), pinned=True)


class PromptBase:
    """Основа: словарь, рейтинг, состав и seed. Начало цепочки."""

    @classmethod
    def INPUT_TYPES(cls):
        data = load_tags()
        return {
            "required": {
                "ckpt_name": ("STRING", {"forceInput": True}),
                # Явный control_after_generate: без него интерфейс принимает значения за старый формат и теряет первое
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF, "control_after_generate": True}),
                "rating": (["NSFW", "SFW"], {"default": "NSFW"}),
                "composition": ([RANDOM] + list(data["composition"]), {"default": RANDOM}),
                "dialect": (["auto", "e621", "danbooru"], {"default": "auto"}),
                "recipe": ("STRING", {"default": "", "multiline": True,
                                      "tooltip": "Строка повтора из ноды «Итог». Удалите поля, которые должны меняться"}),
                # Негатив входит здесь, а не в «Итог»: при обходе ноды ComfyUI подал бы его на выход промпта
                "negative_text": ("STRING", {"forceInput": True, "tooltip": "Базовый негатив с защитой возраста"}),
            },
        }

    RETURN_TYPES = (PLAN,)
    RETURN_NAMES = ("plan",)
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, ckpt_name, seed, rating, composition, dialect, recipe, negative_text):
        check_supported(ckpt_name)
        data = load_tags()
        chosen, why, family = decide(ckpt_name, dialect, DEFAULT_FALLBACK)
        plan = {"seed": seed, "ckpt": ckpt_name, "dialect": chosen, "family": family, "rating": rating,
                "why": why, "pins": parse_recipe(recipe), "fields": {}, "groups": [], "loras": [],
                "triggers": [], "notes": [], "negative": negative_text}
        comps = list(data["composition"])
        pinned = plan["pins"].get("Состав")
        if composition != RANDOM:
            plan["composition"] = composition
        elif pinned and pinned[0] in comps:
            plan["composition"] = pinned[0]
        else:
            weights = [data["composition"][c].get("weight", 1) for c in comps]
            plan["composition"] = field_rng(plan, "Состав").choices(comps, weights=weights)[0]
        return (plan,)


class PromptCharacter:
    """Персонаж: вид, покров, волосы, глаза, телосложение, особенности."""

    @classmethod
    def INPUT_TYPES(cls):
        data = load_tags()
        fields = {name: combo(name, data) for name in CHARACTER_FIELDS}
        return {
            "required": {
                "plan": (PLAN,),
                **fields,
                "extras_chance": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.05,
                                            "tooltip": "Вероятность добавить к полю ещё один совместимый тег"}),
                "max_extras": ("INT", {"default": 2, "min": 0, "max": 15}),
            },
        }

    RETURN_TYPES = (PLAN,)
    RETURN_NAMES = ("plan",)
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, plan, extras_chance, max_extras, **pins):
        data = load_tags()
        plan = copy.deepcopy(plan)
        apply_dropdowns(plan, pins)
        fill_fixed(plan, data, CHARACTER_FIELDS)
        for name in CHARACTER_FIELDS:
            if name not in plan["pins"]:
                rnd = field_rng(plan, name)
                tags = pick_coat(plan, data, rnd) if name == "Окрас" else pick_field(plan, data, name, rnd)
                set_field(plan, data, name, tags)
        # Окрас уже согласован с видом: добавки дали бы пятна лисе и полосы льву
        plan["notes"] += add_extras(plan, data, [n for n in CHARACTER_FIELDS if n != "Окрас"], extras_chance, 2, max_extras)
        plan["notes"] += fit_cover(plan, data)
        return (plan,)


class PromptScene:
    """Сцена: действие или поза, одежда, эмоция, кадр, место, свет, стиль."""

    @classmethod
    def INPUT_TYPES(cls):
        data = load_tags()
        styles = sorted({t for tags in data["styles"].values() for t in tags})
        scenes = sorted({s for pools in data["scenes"].values() for pool in pools.values() for s in pool})
        fields = {"Стиль": ([RANDOM] + styles, {"default": RANDOM}), "Сцена": ([RANDOM] + scenes, {"default": RANDOM})}
        fields.update({n: combo(n, data) for n in SCENE_FIELDS if n not in fields})
        return {
            "required": {
                "plan": (PLAN,),
                **fields,
                "scene_chance": ("FLOAT", {"default": 0.9, "min": 0.0, "max": 1.0, "step": 0.05,
                                           "tooltip": "Вероятность взять сцену вместо обычной позы (только NSFW)"}),
                "nude_chance": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.05,
                                          "tooltip": "Вероятность оставить персонажа без одежды (только NSFW)"}),
                "extras_chance": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.05,
                                            "tooltip": "Вероятность добавить к полю ещё один совместимый тег"}),
                "max_extras": ("INT", {"default": 2, "min": 0, "max": 15}),
            },
        }

    RETURN_TYPES = (PLAN,)
    RETURN_NAMES = ("plan",)
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, plan, scene_chance, nude_chance, extras_chance, max_extras, **pins):
        data = load_tags()
        plan = copy.deepcopy(plan)
        apply_dropdowns(plan, pins)
        fill_fixed(plan, data, SCENE_FIELDS)
        nsfw = plan["rating"] == "NSFW"
        location = pick_location(plan, data)
        if "Свет" not in plan["pins"]:
            rnd = field_rng(plan, "Свет")
            options = from_location(data, plan, location["lights"]) if location else []
            set_field(plan, data, "Свет", split_tags(rnd.choice(options)) if options else pick_field(plan, data, "Свет", rnd))
        if "Стиль" not in plan["pins"]:
            rnd = field_rng(plan, "Стиль")
            # Pony обучали без имён художников, поэтому такой тег там ничего не делает
            pool = [] if plan["family"] == "pony" and plan["dialect"] == "e621" else data["styles"].get(plan["dialect"], [])
            prefix = "by " if plan["family"] == "sdxl" and plan["dialect"] == "e621" else ""
            use = pool and rnd.random() < data["tuning"]["style_chance"]
            set_field(plan, data, "Стиль", [prefix + rnd.choice(pool)] if use else [])

        scene = None
        if "Сцена" not in plan["pins"]:
            rnd = field_rng(plan, "Сцена")
            comp = data["composition"][plan["composition"]]
            pool = data["scenes"].get(plan["dialect"], {}).get(comp["scenes"], [])
            pool = [s for s in pool if not any(w in s for w in comp.get("scene_exclude", []))]
            if nsfw and "Поза" not in plan["pins"] and pool and rnd.random() < scene_chance:
                scene = rnd.choice(pool)
            set_field(plan, data, "Сцена", [scene] if scene else [])
        has_scene = bool(plan["fields"]["Сцена"]["tags"])
        if nsfw and has_scene and plan["composition"] != "solo":
            plan["ban_tags"] = partner_ban(data)
        if "Эмоция" not in plan["pins"]:
            set_field(plan, data, "Эмоция", pick_field(plan, data, "Эмоция", field_rng(plan, "Эмоция")))

        if "Кадр" not in plan["pins"]:
            rnd = field_rng(plan, "Кадр")
            tag = pick_primary(plan, data, "Кадр", rnd, NARROW_FRAMES if has_scene else ())
            frame = [tag] if tag else []
            if tag != "rear view" and rnd.random() < data["tuning"]["gaze_chance"]:
                frame.append("looking at viewer")
            set_field(plan, data, "Кадр", frame)
        if "Поза" not in plan["pins"]:
            rnd = field_rng(plan, "Поза")
            poses = from_location(data, plan, location["poses"]) if location else []
            if has_scene:
                tags = []
            elif poses:
                tags = [weighted_pick(rnd, data, [(p, 1.0) for p in poses])]
            else:
                tags = pick_field(plan, data, "Поза", rnd)
            set_field(plan, data, "Поза", tags)
        if "Одежда" not in plan["pins"]:
            rnd = field_rng(plan, "Одежда")
            if nsfw and rnd.random() < nude_chance:
                set_field(plan, data, "Одежда", ["nude"])
            else:
                tag = pick_primary(plan, data, "Одежда", rnd)
                set_field(plan, data, "Одежда", [tag] if tag else [])
        # Место, свет и кадр берут согласованный набор, поэтому добавки им не нужны
        plan["notes"] += add_extras(plan, data, ("Эмоция", "Поза", "Одежда"), extras_chance, 2, max_extras)
        return (plan,)


def clear_for_role(plan, data, role):
    """Незакреплённые поля, которые заняла LoRA. Закреплённая роль сюда не попадает."""
    notes = []
    for name in ROLE_FIELDS.get(role, ()):
        field = plan["fields"].get(name)
        if not field or field["pinned"]:
            continue
        keep = []
        if name == "Одежда":
            worn = {t for g in data["fields"][name]["groups"] if not g.get("clothed") for t in g["tags"]}
            keep = [t for t in field["tags"] if t in worn]
        gone = [t for t in field["tags"] if t not in keep]
        field["tags"] = keep
        if gone:
            notes.append(f"LoRA [{role}] заменила: {', '.join(gone)}")
    if role == "act":
        frame = plan["fields"].get("Кадр")
        if frame and not frame["pinned"]:
            gone = [t for t in frame["tags"] if t in NARROW_FRAMES]
            frame["tags"] = [t for t in frame["tags"] if t not in NARROW_FRAMES]
            if gone:
                notes.append(f"LoRA [act] убрала кадр: {', '.join(gone)}")
    return notes


class PromptLoras:
    """LoRA: подбор под чекпоинт и состав. Занимает незакреплённые поля своей роли."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "plan": (PLAN,),
                "max_loras": ("INT", {"default": 10, "min": 0, "max": 20}),
                "chance": ("FLOAT", {"default": 0.6, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "Вероятность добавить каждую следующую LoRA (0.6 ≈ 1–2 за раз)"}),
                "weight_budget": ("FLOAT", {"default": 3.5, "min": 0.5, "max": 10.0, "step": 0.1}),
                "male_preference": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05,
                                              "tooltip": "Насколько реже брать LoRA с мужской анатомией"}),
                "exclude_regex": ("STRING", {"default": ""}),
                "always_include": ("STRING", {"default": "", "tooltip": "Имена файлов через запятую"}),
            },
        }

    RETURN_TYPES = (PLAN,)
    RETURN_NAMES = ("plan",)
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, plan, max_loras, chance, weight_budget, male_preference, exclude_regex, always_include):
        data = load_tags()
        plan = copy.deepcopy(plan)
        comp = data["composition"][plan["composition"]]["e621"]
        solo, multi, male = composition(comp)
        _, family, _ = checkpoint_info(plan["ckpt"])
        pinned = plan["pins"].get("LoRA")
        if pinned is not None:
            chosen, weights = self._from_recipe(pinned)
            plan["notes"].append("LoRA: из строки повтора")
        else:
            blocked = {role for role, names in ROLE_FIELDS.items()
                       if any(plan["fields"].get(n, {}).get("pinned") for n in names)}
            want = set(ROLES) - blocked
            quota = {k: int(v) for k, v in _parse_map(DEF_QUOTAS, num=True).items() if not isinstance(v, list)}
            ranges = {k: (v if isinstance(v, list) else [v, v]) for k, v in _parse_map(DEF_WEIGHTS, num=True).items()}
            pick = choose_loras(family, plan["rating"] == "NSFW", solo, multi, male, want, quota, ranges,
                                f"{plan['seed']}:LoRA", max_loras, chance, weight_budget, male_preference,
                                exclude_regex, always_include)
            chosen, weights = pick["chosen"], pick["weights"]
            sk = ", ".join(f"{k} {v}" for k, v in pick["skipped"].items())
            plan["notes"].append(f"LoRA: подходит {pick['fit']} из {len(pick['catalog'])}" + (f" (отсеяно: {sk})" if sk else ""))
        plan["loras_chosen"] = True
        for l, w in zip(chosen, weights):
            words = _triggers(l.get("trained") or [])
            plan["triggers"] += words
            plan["loras"].append({"stem": l["stem"], "rel": l.get("rel"), "weight": w})
            plan["notes"].append(f"LoRA: {l['stem']} ×{w} [{l['role']}]" + (f" + {', '.join(words)}" if words else ""))
            plan["notes"] += clear_for_role(plan, data, l["role"])
            if l["role"] == "act":
                plan["act"] = True
                plan["ban_tags"] = partner_ban(data)
                mood = plan["fields"].get("Эмоция")
                if mood and not mood["pinned"]:
                    mood["tags"] = [t for t in mood["tags"] if t not in plan["ban_tags"]]
        return (plan,)

    @staticmethod
    def _from_recipe(items):
        """Точный набор из строки повтора: триггеры берутся из каталога."""
        catalog = {l["stem"]: l for l in lora_catalog()}
        chosen, weights = [], []
        for item in items:
            m = LORA_RE.match(item)
            stem, weight = (m.group(1), float(m.group(2))) if m else (item, 0.8)
            chosen.append(catalog.get(stem) or {"stem": stem, "role": "other"})
            weights.append(weight)
        return chosen, weights


def spelled(data, plan, tag):
    """Запись тега в словаре запуска. Скобки экранируются: иначе CLIP читает их как вес."""
    if plan["dialect"] == "danbooru":
        tag = data["danbooru_names"].get(tag) or tag
    return re.sub(r"(?<!\\)([()])", r"\\\1", tag)


class PromptResult:
    """Итог: собирает промпт и негатив, выводит отчёт и строку повтора."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "plan": (PLAN,),
                "add_quality": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("text", "negative", "info", "recipe", "clean")
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, plan, add_quality):
        data = load_tags()
        plan = copy.deepcopy(plan)
        plan["fields"]["Фон"] = {"tags": pick_ambient(plan, data), "pinned": "Фон" in plan["pins"]}
        dialect, family = plan["dialect"], plan["family"]
        rating = rating_of(data, plan)
        comp = data["composition"][plan["composition"]][dialect]
        parts = [data["detail"], rating["positive"], data["base"][dialect], comp, data["gender"][dialect]]
        fields = plan["fields"]
        for name in PROMPT_ORDER:
            parts += [spelled(data, plan, t) for t in fields.get(name, {}).get("tags", [])]
        if plan.get("act"):
            parts.append("sex")
        text = _join(*parts, *plan["triggers"])
        negative = _join(AGE_GUARD, plan.get("negative"), rating["negative"])
        if add_quality:
            source = "source_furry" if dialect == "e621" else "source_anime"
            preset = preset_for(plan["ckpt"])
            quality = preset.get("quality") or QUALITY_POS.get(family, QUALITY_POS["other"])
            text = _join(quality.format(source=source), text)
            negative = _join(negative, preset.get("quality_neg") or QUALITY_NEG.get(family, QUALITY_NEG["other"]))
        negative = _join(negative, composition_negative(text))
        text, stopped = strip_stopped(text)
        clean = text
        lora_tags = " ".join(f"<lora:{l['stem']}:{l['weight']}>" for l in plan["loras"])
        if lora_tags:
            text += " " + lora_tags

        recipe = [f"Состав: {plan['composition']}"]
        for name in PROMPT_ORDER:
            if name in fields:
                recipe.append(f"{name}: {', '.join(fields[name]['tags'])}")
        if plan.get("loras_chosen"):
            recipe.append("LoRA: " + ", ".join(f"{l['stem']}:{l['weight']}" for l in plan["loras"]))
        recipe = "; ".join(recipe)
        notes = plan["notes"] + ([f"Вычеркнуто по стоп-листу: {', '.join(stopped)}"] if stopped else [])
        info = "\n".join([f"Словарь: {dialect} · {family} ← {plan['why']}", f"Рейтинг: {plan['rating']}",
                          f"Состав: {plan['composition']}"] + notes)
        print(f"[Constructor] {info}")
        return {"ui": {"text": [info]}, "result": (text, negative, info, recipe, clean)}


class PromptApplyLoras:
    """Подключает LoRA плана по пути файла: имя не ищется по началу, годятся любые символы, в CLIP не уходит сырой тег."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("MODEL",), "clip": ("CLIP",), "plan": (PLAN,)}}

    RETURN_TYPES = ("MODEL", "CLIP")
    RETURN_NAMES = ("model", "clip")
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, model, clip, plan):
        import comfy.sd
        import comfy.utils
        import folder_paths
        for lora in plan.get("loras", []):
            path = folder_paths.get_full_path("loras", lora["rel"]) if lora.get("rel") else None
            if not path:
                print(f"[Constructor] LoRA не найдена: {lora['stem']}")
                continue
            state = comfy.utils.load_torch_file(path, safe_load=True)
            model, clip = comfy.sd.load_lora_for_models(model, clip, state, lora["weight"], lora["weight"])
        return (model, clip)


class PromptSettings:
    """Настройки сэмплера под чекпоинт из presets.json."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"ckpt_name": ("STRING", {"forceInput": True})}}

    try:
        import comfy.samplers
        RETURN_TYPES = ("INT", "FLOAT", comfy.samplers.KSampler.SAMPLERS, comfy.samplers.KSampler.SCHEDULERS, "FLOAT")
    except ImportError:
        RETURN_TYPES = ("INT", "FLOAT", "STRING", "STRING", "FLOAT")
    RETURN_NAMES = ("steps", "cfg", "sampler", "scheduler", "rescale")
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, ckpt_name):
        p = preset_for(ckpt_name)
        return (p["steps"], float(p["cfg"]), p["sampler"], p["scheduler"], float(p["rescale"]))
