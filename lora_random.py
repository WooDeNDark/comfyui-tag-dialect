"""Случайные LoRA, совместимые с текущим чекпоинтом.

Для каждой LoRA: SHA256 -> Civitai (база, теги, trigger words, nsfw), иначе — по папке/имени файла.
Выдаёт промпт с добавленными trigger words и тегами <lora:имя:вес> для LoraTagLoader.
"""
import os
import random
import re

from .tag_dialect import (_lock, _load_cache, _save_cache, _sha256, civitai_lookup,
                          checkpoint_info, family_of, family_from_path, _join)

# какие семейства LoRA подходят к какому чекпоинту
COMPAT = {
    "illu": {"illu", "sdxl"},
    "pony": {"pony", "sdxl"},
    "sdxl": {"sdxl", "illu", "pony"},
    "anima": {"anima"},
    "sd15": {"sd15"},
    "flux": {"flux"},
    "other": set(),
}
CATS = ["character", "style", "clothing", "concept", "detail"]
CAT_WORDS = {
    "character": ("character", "characters", "oc"),
    "style": ("style", "art style", "artstyle", "artist", "styles"),
    "clothing": ("clothing", "outfit", "costume", "dress", "lingerie", "underwear", "bodysuit", "uniform"),
    "concept": ("concept", "concepts", "poses", "pose", "action", "body", "gesture"),
    "detail": ("tool", "tools", "detail", "enhancer", "slider", "quality"),
}
NSFW_NAME = re.compile(r"nsfw|sexy|lingerie|underwear|nude|lewd|nylon|stocking|slingshot|(?:^|[_\W])ass(?:[_\W]|$)|bodystocking", re.I)
NSFW_PROMPT = re.compile(r"(^|,)\s*(nsfw|explicit|rating[_ ]explicit|questionable|nude)\s*(,|$)", re.I)


def _category(meta, rel):
    if re.search(r"detail|enhanc", rel, re.I):
        return "detail"
    tags = set(meta.get("tags") or []) if meta else set()
    for c in CATS:
        if tags & set(CAT_WORDS[c]):
            return c
    p = rel.lower().replace("\\", "/")
    for c in CATS:
        if any(f"/{w}/" in f"/{p}" for w in CAT_WORDS[c]) or (c == "detail" and "detail" in p):
            return c
    if re.search(r"(^|/)(sexy|concept)/", p):
        return "concept"
    return "other"


NSFW_TAGS = {"nsfw", "sexy", "lingerie", "underwear", "nude", "nudity", "lewd", "fetish", "hentai", "porn", "erotic"}


def _is_nsfw(meta, rel):
    """Флаг Civitai часто не стоит у «бельевых» LoRA — проверяем ещё теги, имя файла и trigger words."""
    if meta:
        if meta.get("nsfw") or (set(meta.get("tags") or []) & NSFW_TAGS):
            return True
        if NSFW_NAME.search(" ".join(meta.get("trained") or [])) or NSFW_NAME.search(meta.get("name") or ""):
            return True
    return bool(NSFW_NAME.search(rel))


def lora_catalog():
    """-> list(dict(rel, stem, family, category, nsfw, trained, name, src))"""
    import folder_paths
    out = []
    with _lock:
        cache = _load_cache()
        try:
            for rel in folder_paths.get_filename_list("loras"):
                path = folder_paths.get_full_path("loras", rel)
                if not path or not os.path.isfile(path):
                    continue
                stem = os.path.splitext(os.path.basename(rel.replace("\\", "/")))[0]
                meta = None
                try:
                    meta = civitai_lookup(_sha256(path, cache), cache)
                except Exception as e:
                    print(f"[LoraRandom] {rel}: {e}")
                found = bool(meta and meta.get("found"))
                fam = family_of(meta.get("base")) if found else family_from_path(rel)
                out.append({
                    "rel": rel, "stem": stem, "family": fam,
                    "category": _category(meta if found else None, rel),
                    "nsfw": _is_nsfw(meta if found else None, rel),
                    "trained": (meta.get("trained") or []) if found else [],
                    "name": meta.get("name") if found else stem,
                    "src": "Civitai" if found else "папка/имя",
                })
        finally:
            try:
                _save_cache(cache)
            except Exception as e:
                print(f"[LoraRandom] кэш не сохранён: {e}")
    return out


def _triggers(words, rnd, limit=2):
    """Берём первые 1–2 trigger words (у многих LoRA там длинные списки вариантов)."""
    clean = []
    for w in words:
        for t in w.split(","):
            t = t.strip()
            if t and t.lower() not in [c.lower() for c in clean]:
                clean.append(t)
    return clean[:limit]


class LoraRandomizer:
    """Добавляет к промпту 0..N случайных LoRA, совместимых с чекпоинтом, с их trigger words."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "ckpt_name": ("STRING", {"forceInput": True}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "max_loras": ("INT", {"default": 2, "min": 0, "max": 6,
                                      "tooltip": "Сколько LoRA максимум за одну генерацию"}),
                "chance": ("FLOAT", {"default": 0.6, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "Вероятность добавить каждую следующую LoRA"}),
                "strength_min": ("FLOAT", {"default": 0.5, "min": -2.0, "max": 2.0, "step": 0.05}),
                "strength_max": ("FLOAT", {"default": 0.9, "min": -2.0, "max": 2.0, "step": 0.05}),
                "categories": ("STRING", {"default": "style, clothing, concept, detail, other",
                                          "tooltip": "Из каких категорий брать: character, style, clothing, concept, detail, other"}),
                "nsfw_loras": (["auto (по промпту)", "только SFW", "разрешить"], {"default": "auto (по промпту)",
                               "tooltip": "auto: NSFW-LoRA только если в промпте есть nsfw/explicit (режим NSFW)"}),
                "add_triggers": ("BOOLEAN", {"default": True}),
                "exclude_regex": ("STRING", {"default": "", "tooltip": "LoRA, путь которых совпал, не берутся"}),
                "always_include": ("STRING", {"default": "", "tooltip": "Имена файлов через запятую — добавляются всегда (вес = strength_max)"}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "info")
    FUNCTION = "run"
    CATEGORY = "utils/prompt"

    def run(self, text, ckpt_name, seed, max_loras, chance, strength_min, strength_max,
            categories, nsfw_loras, add_triggers, exclude_regex, always_include):
        _, ck_fam, _ = checkpoint_info(ckpt_name)
        cats = {c.strip().lower() for c in categories.split(",") if c.strip()}
        if nsfw_loras == "разрешить":
            allow_nsfw = True
        elif nsfw_loras == "только SFW":
            allow_nsfw = False
        else:
            allow_nsfw = bool(NSFW_PROMPT.search(text or ""))
        try:
            excl = re.compile(exclude_regex, re.I) if exclude_regex.strip() else None
        except re.error:
            excl = None
        pinned = {s.strip().lower() for s in always_include.split(",") if s.strip()}

        cat = lora_catalog()
        ok_fams = COMPAT.get(ck_fam, set())
        skipped = {"семейство": 0, "категория": 0, "nsfw": 0, "исключено": 0}
        pool, chosen = [], []
        for l in cat:
            if l["stem"].lower() in pinned or l["rel"].lower() in pinned:
                chosen.append((l, round(strength_max, 2)))
            elif l["family"] not in ok_fams:
                skipped["семейство"] += 1
            elif l["category"] not in cats:
                skipped["категория"] += 1
            elif l["nsfw"] and not allow_nsfw:
                skipped["nsfw"] += 1
            elif excl and excl.search(l["rel"]):
                skipped["исключено"] += 1
            else:
                pool.append(l)

        n_ok = len(pool) + len(chosen)
        rnd = random.Random(seed)
        lo, hi = sorted((strength_min, strength_max))
        used_cats = set()
        for _ in range(max_loras):
            if not pool or rnd.random() >= chance:
                break
            # не больше одной LoRA на категорию (кроме detail), чтобы они не спорили
            cands = [l for l in pool if l["category"] == "detail" or l["category"] not in used_cats] or pool
            l = rnd.choice(cands)
            pool.remove(l)
            used_cats.add(l["category"])
            chosen.append((l, round(rnd.uniform(lo, hi), 2)))

        trig, tags, lines = [], [], []
        for l, w in chosen:
            t = _triggers(l["trained"], rnd) if add_triggers else []
            trig += t
            tags.append(f"<lora:{l['stem']}:{w}>")
            lines.append(f"{l['stem']} ×{w} [{l['category']}, {l['family']}, {l['src']}]" + (f" + {', '.join(t)}" if t else ""))
        out = _join(text, *trig)
        if tags:
            out = out + " " + " ".join(tags)
        sk = ", ".join(f"{k} {v}" for k, v in skipped.items() if v)
        info = (f"Чекпоинт: семейство {ck_fam}; подходящих LoRA: {n_ok} из {len(cat)}"
                + (f" (отсеяно: {sk})" if sk else "")
                + f"; NSFW-LoRA {'можно' if allow_nsfw else 'нельзя'}\n"
                + ("Выбрано:\n  " + "\n  ".join(lines) if lines else "Выбрано: ничего (сработал шанс или нет подходящих)"))
        print(f"[LoraRandom] {info}")
        return {"ui": {"text": [info]}, "result": (out, info)}
