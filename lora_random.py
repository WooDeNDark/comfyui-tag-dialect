"""Случайные LoRA, совместимые с текущим чекпоинтом (до 10 за генерацию).

Для каждой LoRA: SHA256 -> Civitai (база, теги, trigger words, nsfw, рекомендованный вес), иначе — по папке/имени
файла. Файл-подсказка <lora>.txt рядом с LoRA дополняет или заменяет эти данные (см. _read_hint).
Защита от конфликтов:
  * квоты по ролям (style/clothing/body/act/concept/detail/other);
  * выбранная LoRA заменяет соответствующий слот конструктора (тег слота вычёркивается из промпта);
  * веса по ролям + общий бюджет весов;
  * учитывается состав (solo / duo / с мужским персонажем) и режим SFW/NSFW.
"""
import json
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
    if role == "act" or PARTNER_RE.search(rel):
        return True
    m = meta or {}
    name_trig = " ".join([rel, m.get("name") or "", " ".join(m.get("trained") or [])])
    if role in ("style", "detail", "clothing", "character"):
        return bool(ANAT_RE.search(name_trig))
    tags = set(m.get("tags") or [])
    return bool(MALE_RE.search(name_trig) or tags & {"penis", "blowjob", "fellatio", "knotting", "cum"})
NSFW_NAME = re.compile(r"nsfw|sexy|lingerie|underwear|nude|lewd|nylon|stocking|slingshot|(?:^|[_\W])ass(?:[_\W]|$)|"
                       r"bodystocking|chastity|bdsm|bunnysuit|bikini|porn|penis|pussy|cum", re.I)
NSFW_TAGS = {"nsfw", "sexy", "lingerie", "underwear", "nude", "nudity", "lewd", "fetish", "hentai", "porn", "erotic", "sex"}
# Концепты со сценой с мужчиной: роль concept, но без партнёра они ломают состав
PARTNER_RE = re.compile(r"tugging|sitting.?on.?lap|titty.?sucking", re.I)
ROLES = ["style", "clothing", "body", "act", "concept", "detail", "other"]
DEF_QUOTAS = "style=1, clothing=1, body=1, act=1, concept=2, detail=2, other=1"
DEF_WEIGHTS = "style=0.6-0.8, clothing=0.7-0.9, body=0.5-0.8, act=0.8-1.0, concept=0.6-0.9, detail=0.3-0.6, other=0.5-0.8"


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


FOLDER_ROLES = {"detail": "detail", "style": "style", "clothing": "clothing", "body": "body", "sex": "act",
                "concept": "concept", "character": "character"}
HINT_KEYS = {"trigger": "trained", "triggers": "trained", "триггер": "trained", "триггеры": "trained",
             "role": "role", "роль": "role", "weight": "weight", "вес": "weight",
             "nsfw": "nsfw", "male": "male"}


def _read_hint(path):
    """<lora>.txt рядом с файлом LoRA. Формат — строки «ключ: значение», все необязательны:
        trigger: tag one, tag two
        role: style          (style, clothing, body, act, concept, detail, other, character)
        weight: 0.5-0.7      (или одно число)
        nsfw: yes            (yes/no)
        male: no             (yes/no)
    Строка без ключа считается trigger words."""
    side = os.path.splitext(path)[0] + ".txt"
    if not os.path.isfile(side):
        return {}
    raw = open(side, "rb").read()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    out, words = {}, []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, sep, v = line.partition(":")
        key = HINT_KEYS.get(k.strip().lower()) if sep else None
        v = v.strip()
        if key is None:
            words.append(line)
        elif key == "trained":
            words.append(v)
        elif key == "role":
            out["role"] = v.lower()
        elif key == "weight":
            nums = sorted(float(x) for x in re.findall(r"\d+(?:[.,]\d+)?", v.replace(",", ".")))
            if nums:
                out["rec"] = {"lo": nums[0], "hi": nums[-1], "src": "подсказка"}
        else:
            out[key] = v.lower() in ("yes", "true", "1", "да")
    if words:
        out["trained"] = words
    return out


def _stored_hints():
    """Подсказки из user/default/lora_hints.json: каталог у каждого свой, поэтому файла нет в пакете."""
    try:
        import folder_paths
        path = os.path.join(folder_paths.get_user_directory(), "default", "lora_hints.json")
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (AttributeError, OSError, ValueError):
        return {}


def _folder_role(rel):
    """Папка loras/<семейство>/<роль>/ разобрана вручную, поэтому важнее тегов Civitai."""
    for part in rel.lower().replace("\\", "/").split("/")[:-1]:
        if part in FOLDER_ROLES:
            return FOLDER_ROLES[part]
    return None


def _role(meta, rel):
    """Роль LoRA: style / clothing / body / act / concept / detail / other / character."""
    by_folder = _folder_role(rel)
    if by_folder:
        return by_folder
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
    stored = _stored_hints()
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
                hint = {**stored.get(stem, {}), **_read_hint(path)}
                role = hint.get("role") or _role(m, rel)
                out.append({
                    "rel": rel, "stem": stem,
                    "family": family_of(m.get("base")) if m else family_from_path(rel),
                    "role": role, "nsfw": hint.get("nsfw", _is_nsfw(m, rel)),
                    "male": hint.get("male", _is_male(m, rel, role)),
                    "trained": hint.get("trained") or ((m.get("trained") or []) if m else []),
                    "rec": hint.get("rec") or (m.get("rec") if m else None), "random": hint.get("random", True),
                    "name": m.get("name") if m else stem, "src": "Civitai" if m else "папка/имя",
                })
        finally:
            try:
                _save_cache(cache)
            except Exception as e:
                print(f"[LoraRandom] кэш не сохранён: {e}")
    return out


def _weight_range(l, wr):
    """Диапазон веса: роль + рекомендация. Подсказка <lora>.txt решает сама; рекомендация с Civitai только
    понижает верхнюю границу, потому что сильные LoRA в связке пережигают картинку."""
    lo, hi = sorted(wr.get(l["role"], wr.get("other", [0.5, 0.8]))[:2])
    rec = l.get("rec")
    if rec and rec.get("src") == "подсказка":
        return rec["lo"], rec["hi"]
    if rec and rec["hi"] < hi:
        hi = rec["hi"]
        lo = min(lo, max(rec["lo"], hi * 0.7))
    return lo, hi


def _split_outside_parens(s):
    parts, depth, cur = [], 0, ""
    for ch in s:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth <= 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    return parts + [cur]


def _triggers(words, limit=2):
    """Trigger words для промпта. Авторы пишут туда что угодно, поэтому отбрасываем:
    чужие теги <lora:…> (они подгрузили бы LoRA второй раз с весом 1) и фразы со скобками, которые
    не закрыты или содержат запятую. Скобки в обычных тегах экранируем: иначе ComfyUI считает их весом."""
    clean = []
    for w in words:
        for t in _split_outside_parens(re.sub(r"<[^>]*>", "", w)):
            t = t.strip()
            if not t or t.count("(") != t.count(")") or ("," in t):
                continue
            if t.startswith("(") and t.endswith(")"):
                t = t[1:-1].strip()
            t = re.sub(r"(?<!\\)([()])", r"\\\1", t)
            if t and t.lower() not in [c.lower() for c in clean]:
                clean.append(t)
    return clean[:limit]


def choose_loras(ck_fam, allow_nsfw, solo, multi, male, want, quota, wr, seed, max_loras, chance,
                 weight_budget, male_preference, exclude_regex, always_include):
    """Выбирает LoRA под чекпоинт и состав и считает веса. Роль из want вне списка не берётся."""
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
        elif not l.get("random", True):
            skip("только вручную")
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
    # Выбор из всего пула: иначе после заполнения крупных ролей вероятность уходит одиночным LoRA малых ролей
    while len(chosen) < max_loras and pool and rnd.random() < chance:
        w = [male_preference if l["male"] else 1.0 for l in pool]
        if sum(w) <= 0:
            break
        l = rnd.choices(pool, weights=w)[0]
        pool.remove(l)
        if used.get(l["role"], 0) >= quota.get(l["role"], 1):
            continue
        used[l["role"]] = used.get(l["role"], 0) + 1
        chosen.append(l)

    # веса по ролям + бюджет
    ws = []
    for l in chosen:
        ws.append(rnd.uniform(*_weight_range(l, wr)))
    main = sum(w for l, w in zip(chosen, ws) if l["role"] != "detail")
    scale = weight_budget / main if main > weight_budget else 1.0
    ws = [round(w * (scale if l["role"] != "detail" else 1.0), 2) for l, w in zip(chosen, ws)]
    return {"chosen": chosen, "weights": ws, "main": main, "scale": scale,
            "fit": n_ok, "skipped": skipped, "catalog": cat}
