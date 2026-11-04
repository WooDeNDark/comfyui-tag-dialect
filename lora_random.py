"""Случайные LoRA, совместимые с текущим чекпоинтом (до 10 за генерацию).

Для каждой LoRA: SHA256 -> Civitai (база, теги, trigger words, nsfw), иначе — по папке/имени файла.
Защита от конфликтов:
  * квоты по ролям (style/clothing/body/act/concept/detail/other);
  * выбранная LoRA заменяет соответствующий слот конструктора (тег слота вычёркивается из промпта);
  * веса по ролям + общий бюджет весов;
  * учитывается состав (solo / duo / с мужским персонажем) и режим SFW/NSFW.
"""
import os
import random
import re

from .tag_dialect import (_lock, _load_cache, _save_cache, _sha256, civitai_lookup,
                          checkpoint_info, family_of, family_from_path, _join, composition)

COMPAT = {
    "illu": {"illu", "sdxl"}, "pony": {"pony", "sdxl"}, "sdxl": {"sdxl", "illu", "pony"},
    "anima": {"anima"}, "sd15": {"sd15"}, "flux": {"flux"}, "other": set(),
}
CAT_WORDS = {
    "character": {"character", "characters", "oc"},
    "style": {"style", "art style", "artstyle", "artist", "styles"},
    "clothing": {"clothing", "outfit", "costume", "dress", "lingerie", "underwear", "bodysuit", "uniform", "swimsuit"},
    "concept": {"concept", "concepts", "poses", "pose", "action", "body", "gesture", "body type"},
    "detail": {"tool", "tools", "detail", "details", "enhancer", "slider", "quality", "contrast"},
}
BODY_RE = re.compile(r"\b(body ?type|body shape|hourglass|curvy|thick|thighs|hips|long legs|breasts?|figure|chubby|muscular)\b", re.I)
ACT_RE = re.compile(r"\b(sex|blowjob|fellatio|paizuri|titjob|handjob|footjob|penetration|missionary|cowgirl|doggystyle|"
                    r"hetero|couple|threesome|size difference|giant ?dom|creampie|cum explosion|knotting|prone ?bone|mating press|"
                    r"reverse cowgirl|spooning|piledriver|full nelson|amazon position|69|scissoring|tribadism|cunnilingus)\b", re.I)
# мужская анатомия — по словам из имени и trigger words; тег страницы «male» у стилей/одежды ничего не значит
MALE_RE = re.compile(r"\b(penis|penises|knot|knotted|knotting|blowjob|fellatio|1boy|dick|cock|semen|testicles|"
                     r"cum|giant male|male)\b", re.I)
ANAT_RE = re.compile(r"\b(penis|penises|knotted|knotting|blowjob|fellatio|dick|cock|testicles|semen)\b", re.I)


def _is_male(meta, rel, role):
    m = meta or {}
    name_trig = " ".join([rel, m.get("name") or "", " ".join(m.get("trained") or [])])
    if role in ("style", "detail", "clothing", "character"):
        return bool(ANAT_RE.search(name_trig))
    tags = set(m.get("tags") or [])
    return bool(MALE_RE.search(name_trig) or tags & {"penis", "blowjob", "fellatio", "knotting", "cum"})
NSFW_NAME = re.compile(r"nsfw|sexy|lingerie|underwear|nude|lewd|nylon|stocking|slingshot|(?:^|[_\W])ass(?:[_\W]|$)|"
                       r"bodystocking|chastity|bdsm|bunnysuit|bikini|porn|penis|pussy|cum", re.I)
NSFW_TAGS = {"nsfw", "sexy", "lingerie", "underwear", "nude", "nudity", "lewd", "fetish", "hentai", "porn", "erotic", "sex"}
NSFW_PROMPT = re.compile(r"(^|,)\s*(nsfw|explicit|rating[_ ]explicit|questionable|nude)\s*(,|$)", re.I)
ROLES = ["style", "clothing", "body", "act", "concept", "detail", "other"]
DEF_QUOTAS = "style=1, clothing=1, body=1, act=1, concept=2, detail=2, other=1"
DEF_WEIGHTS = "style=0.6-0.8, clothing=0.7-0.9, body=0.5-0.8, act=0.8-1.0, concept=0.6-0.9, detail=0.3-0.6, other=0.5-0.8"
DEF_SLOTMAP = "clothing=Одежда; style=Художник,Стиль; body=Телосложение"


def _valid_safetensors(path):
    """Отсекает битые/недокачанные файлы (например, HTML-страницу вместо модели)."""
    try:
        if os.path.getsize(path) < 1024 * 1024:
            return False
        with open(path, "rb") as f:
            head = f.read(9)
        n = int.from_bytes(head[:8], "little")
        return 0 < n < 100 * 1024 * 1024 and head[8:9] == b"{"
    except Exception:
        return False


def _text_of(meta, rel):
    m = meta or {}
    return " ".join([rel, m.get("name") or "", " ".join(m.get("tags") or []), " ".join(m.get("trained") or [])])


def _role(meta, rel):
    """Роль LoRA: style / clothing / body / act / concept / detail / other / character."""
    tags = set((meta or {}).get("tags") or [])
    txt = _text_of(meta, rel)
    if tags & CAT_WORDS["character"]:
        return "character"
    for c in ("detail", "style"):
        if tags & CAT_WORDS[c]:
            return c
    if ACT_RE.search(txt):
        return "act"
    if tags & CAT_WORDS["clothing"]:
        return "clothing"
    if BODY_RE.search(txt) or tags & {"body", "body type"}:
        return "body"
    if tags & CAT_WORDS["concept"]:
        return "concept"
    p = rel.lower().replace("\\", "/")
    if re.search(r"detail(?!ed)|enhanc", p):
        return "detail"
    for c, words in (("character", ("character",)), ("act", ("sex",)), ("body", ("body",)),
                     ("clothing", ("clothing", "bodysuit")), ("style", ("style",)), ("concept", ("concept", "sexy"))):
        if any(f"/{w}/" in f"/{p}" for w in words):
            return c
    return "other"


def _is_nsfw(meta, rel):
    if meta:
        if meta.get("nsfw") or (set(meta.get("tags") or []) & NSFW_TAGS):
            return True
    return bool(NSFW_NAME.search(_text_of(meta, rel)))


def _parse_map(s, num=False):
    out = {}
    for part in re.split(r"[;,]\s*(?=\w+\s*=)", s or ""):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip().lower(), v.strip()
        if num:
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", v)]
            if nums:
                out[k] = nums if len(nums) > 1 else nums[0]
        else:
            out[k] = [x.strip() for x in v.split(",") if x.strip()]
    return out


def lora_catalog():
    """-> list(dict(rel, stem, family, role, nsfw, male, trained, name, src))"""
    import folder_paths
    out = []
    with _lock:
        cache = _load_cache()
        try:
            for rel in folder_paths.get_filename_list("loras"):
                path = folder_paths.get_full_path("loras", rel)
                if not path or not os.path.isfile(path):
                    continue
                if not _valid_safetensors(path):
                    print(f"[LoraRandom] пропускаю битый файл: {rel}")
                    continue
                stem = os.path.splitext(os.path.basename(rel.replace("\\", "/")))[0]
                meta = None
                try:
                    meta = civitai_lookup(_sha256(path, cache), cache)
                except Exception as e:
                    print(f"[LoraRandom] {rel}: {e}")
                m = meta if (meta and meta.get("found")) else None
                out.append({
                    "rel": rel, "stem": stem,
                    "family": family_of(m.get("base")) if m else family_from_path(rel),
                    "role": _role(m, rel), "nsfw": _is_nsfw(m, rel),
                    "male": _is_male(m, rel, _role(m, rel)),
                    "trained": (m.get("trained") or []) if m else [],
                    "name": m.get("name") if m else stem, "src": "Civitai" if m else "папка/имя",
                })
        finally:
            try:
                _save_cache(cache)
            except Exception as e:
                print(f"[LoraRandom] кэш не сохранён: {e}")
    return out


def _triggers(words, limit=2):
    clean = []
    for w in words:
        for t in w.split(","):
            t = t.strip().strip("()")
            if t and t.lower() not in [c.lower() for c in clean]:
                clean.append(t)
    return clean[:limit]


def _slot_tags(prompt, prefixes):
    """Теги, которые выбрали слоты конструктора с такими заголовками (из исполняемого графа)."""
    tags = []
    for node in (prompt or {}).values():
        if not isinstance(node, dict) or not str(node.get("class_type", "")).startswith("ErePrompt"):
            continue
        title = (node.get("_meta") or {}).get("title", "")
        if any(title.lower().startswith(p.lower()) for p in prefixes):
            txt = (node.get("inputs") or {}).get("text")
            if isinstance(txt, str):
                tags += [t.strip() for t in txt.split(",") if t.strip()]
    return tags


def _remove_tags(text, tags):
    drop = {t.lower() for t in tags}
    kept, removed = [], []
    for t in (text or "").split(","):
        s = t.strip()
        if s and s.lower() in drop:
            removed.append(s)
        elif s:
            kept.append(s)
    return ", ".join(kept), removed


class LoraRandomizer:
    """Добавляет к промпту до max_loras случайных LoRA, совместимых с чекпоинтом, без конфликтов со слотами."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "text": ("STRING", {"forceInput": True}),
                "ckpt_name": ("STRING", {"forceInput": True}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "max_loras": ("INT", {"default": 10, "min": 0, "max": 20}),
                "chance": ("FLOAT", {"default": 0.8, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "Вероятность добавить каждую следующую LoRA (0.8 ≈ в среднем 3–4 за раз)"}),
                "roles": ("STRING", {"default": ", ".join(ROLES),
                                     "tooltip": "Из каких ролей брать: style, clothing, body, act (секс-сцены), concept, detail, other, character"}),
                "quotas": ("STRING", {"default": DEF_QUOTAS, "tooltip": "Сколько LoRA каждой роли максимум за раз"}),
                "weights": ("STRING", {"default": DEF_WEIGHTS, "tooltip": "Диапазон веса по ролям"}),
                "weight_budget": ("FLOAT", {"default": 3.5, "min": 0.5, "max": 10.0, "step": 0.1,
                                            "tooltip": "Если сумма весов (без detail) больше — все веса пропорционально уменьшаются"}),
                "male_preference": ("FLOAT", {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05,
                                              "tooltip": "Насколько реже брать LoRA с мужской анатомией (1 = на равных, 0 = никогда)"}),
                "nsfw_loras": (["auto (по промпту)", "только SFW", "разрешить"], {"default": "auto (по промпту)"}),
                "add_triggers": ("BOOLEAN", {"default": True}),
                "replace_slots": ("BOOLEAN", {"default": True,
                                              "tooltip": "LoRA одежды/стиля/тела вычёркивает тег соответствующего слота конструктора"}),
                "slot_map": ("STRING", {"default": DEF_SLOTMAP, "tooltip": "роль=заголовки слотов (начало заголовка)"}),
                "exclude_regex": ("STRING", {"default": ""}),
                "always_include": ("STRING", {"default": "", "tooltip": "Имена файлов через запятую — добавляются всегда"}),
            },
            "hidden": {"prompt": "PROMPT"},
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "info")
    FUNCTION = "run"
    CATEGORY = "utils/prompt"

    def run(self, text, ckpt_name, seed, max_loras, chance, roles, quotas, weights, weight_budget, male_preference,
            nsfw_loras, add_triggers, replace_slots, slot_map, exclude_regex, always_include, prompt=None):
        _, ck_fam, _ = checkpoint_info(ckpt_name)
        want = {r.strip().lower() for r in roles.split(",") if r.strip()}
        quota = {k: int(v) for k, v in _parse_map(quotas, num=True).items() if not isinstance(v, list)}
        wr = {k: (v if isinstance(v, list) else [v, v]) for k, v in _parse_map(weights, num=True).items()}
        allow_nsfw = {"разрешить": True, "только SFW": False}.get(nsfw_loras, bool(NSFW_PROMPT.search(text or "")))
        solo, multi, male = composition(text)
        try:
            excl = re.compile(exclude_regex, re.I) if exclude_regex.strip() else None
        except re.error:
            excl = None
        pinned = {s.strip().lower() for s in always_include.split(",") if s.strip()}

        cat = lora_catalog()
        ok_fams = COMPAT.get(ck_fam, set())
        skipped = {}
        skip = lambda why: skipped.__setitem__(why, skipped.get(why, 0) + 1)
        pool, chosen = [], []
        for l in cat:
            nsfw_hit = l["nsfw"] if l["role"] not in ("style", "detail") else bool(NSFW_NAME.search(l["rel"]))
            if l["stem"].lower() in pinned or l["rel"].lower() in pinned:
                chosen.append(l)
            elif l["family"] not in ok_fams:
                skip("семейство")
            elif l["role"] not in want:
                skip("роль")
            elif nsfw_hit and not allow_nsfw:
                skip("nsfw")
            elif l["role"] == "act" and not multi:
                skip("нужен партнёр")
            elif l["male"] and not male:
                skip("мужская анатомия")
            elif excl and excl.search(l["rel"]):
                skip("исключено")
            else:
                pool.append(l)
        n_ok = len(pool) + len(chosen)

        rnd = random.Random(seed)
        used = {}
        for l in chosen:
            used[l["role"]] = used.get(l["role"], 0) + 1
        while len(chosen) < max_loras and pool and rnd.random() < chance:
            cands = [l for l in pool if used.get(l["role"], 0) < quota.get(l["role"], 1)]
            if not cands:
                break
            w = [male_preference if l["male"] else 1.0 for l in cands]
            if sum(w) <= 0:
                break
            l = rnd.choices(cands, weights=w)[0]
            pool.remove(l)
            used[l["role"]] = used.get(l["role"], 0) + 1
            chosen.append(l)

        # веса по ролям + бюджет
        ws = []
        for l in chosen:
            lo, hi = sorted(wr.get(l["role"], wr.get("other", [0.5, 0.8]))[:2])
            ws.append(rnd.uniform(lo, hi))
        main = sum(w for l, w in zip(chosen, ws) if l["role"] != "detail")
        scale = weight_budget / main if main > weight_budget else 1.0
        ws = [round(w * (scale if l["role"] != "detail" else 1.0), 2) for l, w in zip(chosen, ws)]

        # LoRA заменяет слот конструктора
        out, replaced = text or "", []
        if replace_slots:
            smap = _parse_map(slot_map)
            for role in {l["role"] for l in chosen}:
                if role in smap:
                    out, rem = _remove_tags(out, _slot_tags(prompt, smap[role]))
                    replaced += [f"{r} (→ {role})" for r in rem]

        trig, tags, lines = [], [], []
        for l, w in zip(chosen, ws):
            t = _triggers(l["trained"]) if add_triggers else []
            trig += t
            tags.append(f"<lora:{l['stem']}:{w}>")
            lines.append(f"{l['stem']} ×{w} [{l['role']}{', ♂' if l['male'] else ''}, {l['family']}]" + (f" + {', '.join(t)}" if t else ""))
        out = _join(out, *trig)
        if tags:
            out += " " + " ".join(tags)

        comp = "solo" if solo else ("с мужским персонажем" if male else ("несколько" if multi else "не указан"))
        sk = ", ".join(f"{k} {v}" for k, v in skipped.items())
        info = (f"Чекпоинт: {ck_fam}; состав: {comp}; NSFW-LoRA {'можно' if allow_nsfw else 'нельзя'}\n"
                f"Подходящих LoRA: {n_ok} из {len(cat)}" + (f" (отсеяно: {sk})" if sk else "")
                + (f"\nБюджет весов: сумма {main:.2f} → ×{scale:.2f}" if scale < 1 else "")
                + ("\nВыбрано " + str(len(chosen)) + ":\n  " + "\n  ".join(lines) if lines else "\nВыбрано: ничего")
                + ("\nЗаменены теги слотов: " + ", ".join(replaced) if replaced else ""))
        print(f"[LoraRandom] {info}")
        return {"ui": {"text": [info]}, "result": (out, info)}
