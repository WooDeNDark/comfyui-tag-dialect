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
from .slot_extras import _allowed
from .tag_dialect import (DEFAULT_FALLBACK, QUALITY_NEG, QUALITY_POS, _join, checkpoint_info, composition,
                          composition_negative, decide)

TAGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tags.json")
PLAN = "TAG_PLAN"
RANDOM = "случайно"
CHARACTER_FIELDS = ("Вид", "Телосложение", "Окрас", "Волосы", "Глаза", "Особенности")
# Свет зависит от места, кадр — от сцены
SCENE_FIELDS = ("Окружение", "Свет", "Эмоция", "Стиль", "Сцена", "Кадр", "Поза", "Одежда")
PROMPT_ORDER = ("Стиль", "Вид", "Телосложение", "Окрас", "Волосы", "Глаза", "Одежда", "Эмоция", "Поза", "Кадр",
                "Окружение", "Свет", "Особенности", "Сцена")
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
    """Теги, которые можно написать в словаре запуска."""
    if plan["dialect"] != "danbooru":
        return list(tags)
    names = data["danbooru_names"]
    return [t for t in tags if t not in names or names[t]]


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


def pick_primary(plan, data, name, rnd, skip=()):
    """Один тег поля. Откровенный вариант из той же группы выпадает с вероятностью nsfw_rate."""
    field = data["fields"][name]
    seen = seen_groups(data, plan)
    groups = [g for g in field["groups"] if primary_ok(g, seen, data["field_names"][name])]
    if plan["rating"] == "NSFW" and rnd.random() < field.get("nsfw_rate", 0):
        pool = [t for g in groups for t in available(data, plan, g.get("nsfw_tags", []))]
        if pool:
            return rnd.choice(pool)
    pool = [t for g in groups for t in available(data, plan, g["tags"]) if t not in skip]
    return rnd.choice(pool) if pool else None


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
            cands = [g for g in groups if _allowed(g, axes, seen, nsfw)
                     and [t for t in available(data, plan, g["tags"]) if t not in skip]]
            if not cands:
                break
            group = rnd.choice(cands)
            tag = rnd.choice([t for t in available(data, plan, group["tags"]) if t not in skip])
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
            },
        }

    RETURN_TYPES = (PLAN,)
    RETURN_NAMES = ("plan",)
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, ckpt_name, seed, rating, composition, dialect, recipe):
        data = load_tags()
        chosen, why, family = decide(ckpt_name, dialect, DEFAULT_FALLBACK)
        plan = {"seed": seed, "ckpt": ckpt_name, "dialect": chosen, "family": family, "rating": rating,
                "why": why, "pins": parse_recipe(recipe), "fields": {}, "groups": [], "loras": [],
                "triggers": [], "notes": []}
        comps = list(data["composition"])
        pinned = plan["pins"].get("Состав")
        if composition != RANDOM:
            plan["composition"] = composition
        elif pinned and pinned[0] in comps:
            plan["composition"] = pinned[0]
        else:
            plan["composition"] = field_rng(plan, "Состав").choice(comps)
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
                "extras_chance": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05,
                                            "tooltip": "Вероятность добавить к полю ещё один совместимый тег"}),
                "max_extras": ("INT", {"default": 3, "min": 0, "max": 15}),
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
                tag = pick_primary(plan, data, name, field_rng(plan, name))
                set_field(plan, data, name, [tag] if tag else [])
        plan["notes"] += add_extras(plan, data, CHARACTER_FIELDS, extras_chance, 2, max_extras)
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
                "extras_chance": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05,
                                            "tooltip": "Вероятность добавить к полю ещё один совместимый тег"}),
                "max_extras": ("INT", {"default": 3, "min": 0, "max": 15}),
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
        for name in ("Окружение", "Свет", "Эмоция"):
            if name not in plan["pins"]:
                tag = pick_primary(plan, data, name, field_rng(plan, name))
                set_field(plan, data, name, [tag] if tag else [])
        if "Стиль" not in plan["pins"]:
            pool = data["styles"].get(plan["dialect"], [])
            set_field(plan, data, "Стиль", [field_rng(plan, "Стиль").choice(pool)] if pool else [])

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

        if "Кадр" not in plan["pins"]:
            tag = pick_primary(plan, data, "Кадр", field_rng(plan, "Кадр"), NARROW_FRAMES if has_scene else ())
            set_field(plan, data, "Кадр", [tag] if tag else [])
        if "Поза" not in plan["pins"]:
            tag = None if has_scene else pick_primary(plan, data, "Поза", field_rng(plan, "Поза"))
            set_field(plan, data, "Поза", [tag] if tag else [])
        if "Одежда" not in plan["pins"]:
            rnd = field_rng(plan, "Одежда")
            if nsfw and rnd.random() < nude_chance:
                set_field(plan, data, "Одежда", ["nude"])
            else:
                tag = pick_primary(plan, data, "Одежда", rnd)
                set_field(plan, data, "Одежда", [tag] if tag else [])
        plan["notes"] += add_extras(plan, data, SCENE_FIELDS, extras_chance, 2, max_extras)
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
                "chance": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "Вероятность добавить каждую следующую LoRA"}),
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
            plan["loras"].append({"stem": l["stem"], "weight": w})
            plan["notes"].append(f"LoRA: {l['stem']} ×{w} [{l['role']}]" + (f" + {', '.join(words)}" if words else ""))
            plan["notes"] += clear_for_role(plan, data, l["role"])
            if l["role"] == "act":
                plan["act"] = True
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
                "negative_text": ("STRING", {"forceInput": True, "tooltip": "Базовый негатив с защитой возраста"}),
                "add_quality": ("BOOLEAN", {"default": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("text", "negative", "info", "recipe")
    FUNCTION = "run"
    CATEGORY = "utils/prompt/constructor"

    def run(self, plan, negative_text, add_quality):
        data = load_tags()
        dialect, family = plan["dialect"], plan["family"]
        rating = data["rating"][plan["rating"]]
        comp = data["composition"][plan["composition"]][dialect]
        parts = [data["detail"], rating["positive"], data["base"][dialect], comp, data["gender"][dialect]]
        fields = plan["fields"]
        for name in PROMPT_ORDER:
            parts += [spelled(data, plan, t) for t in fields.get(name, {}).get("tags", [])]
        if plan.get("act"):
            parts.append("sex")
        text = _join(*parts, *plan["triggers"])
        negative = _join(negative_text, rating["negative"])
        if add_quality:
            source = "source_furry" if dialect == "e621" else "source_anime"
            text = _join(QUALITY_POS.get(family, QUALITY_POS["other"]).format(source=source), text)
            negative = _join(negative, QUALITY_NEG.get(family, QUALITY_NEG["other"]))
        negative = _join(negative, composition_negative(text))
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
        info = "\n".join([f"Словарь: {dialect} · {family} ← {plan['why']}", f"Рейтинг: {plan['rating']}",
                          f"Состав: {plan['composition']}"] + plan["notes"])
        print(f"[Constructor] {info}")
        return {"ui": {"text": [info]}, "result": (text, negative, info, recipe)}
