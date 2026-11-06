"""Определяет, понимает ли чекпоинт e621-теги, и выбирает нужную ветку промпта.

Порядок: ручной режим -> метаданные Civitai (по SHA256 файла) -> жёсткое правило (regex по пути).
Результаты хэширования и Civitai кэшируются в cache.json рядом с нодой.
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")
CACHE_V = 3                           # при смене формата записи Civitai запрашиваются заново
NOTFOUND_TTL = 7 * 24 * 3600          # «нет на Civitai» перепроверяем раз в неделю
E621_BASES = ("noobai",)                # базы, которые учили на e621; Pony смешанная: решают теги модели
FURRY_TAGS = {"furry", "anthro", "anthropomorphic", "e621", "yiff", "furry art", "furries"}
FURRY_NAME_RE = re.compile(r"furry|yiff|anthro|e621", re.I)
DEFAULT_FALLBACK = r"furry|yiff|anthro|e621|noob|indigo|fluffy|pony"

_lock = threading.Lock()


def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"hashes": {}, "civitai": {}}


def _save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_PATH)


def _full_path(ckpt_name):
    try:
        import folder_paths
        p = folder_paths.get_full_path("checkpoints", ckpt_name)
        if p:
            return p
    except Exception:
        pass
    return ckpt_name if os.path.isfile(ckpt_name) else None


def _resolve(ckpt_name):
    """Находит файл чекпоинта, даже если пришло имя без расширения/папки."""
    p = _full_path(ckpt_name)
    if p:
        return p
    try:
        import folder_paths
        want = os.path.splitext(os.path.basename(ckpt_name.replace("\\", "/")))[0].lower()
        for n in folder_paths.get_filename_list("checkpoints"):
            if os.path.splitext(os.path.basename(n.replace("\\", "/")))[0].lower() == want:
                return folder_paths.get_full_path("checkpoints", n)
    except Exception:
        pass
    return None


def _sha256(path, cache):
    st = os.stat(path)
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    if key in cache["hashes"]:
        return cache["hashes"][key]
    h = None
    side = os.path.splitext(path)[0] + ".sha256"      # sidecar от Image Saver, если есть
    if os.path.isfile(side):
        try:
            with open(side, "r", encoding="utf-8") as f:
                t = f.read().strip().split()[0].lower()
            if re.fullmatch(r"[0-9a-f]{64}", t):
                h = t
        except Exception:
            pass
    if h is None:
        print(f"[TagDialect] считаю SHA256 {os.path.basename(path)} (один раз, потом из кэша)...")
        d = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                d.update(chunk)
        h = d.hexdigest()
    cache["hashes"][key] = h
    return h


def _get_json(url):
    headers = {"User-Agent": "comfyui-tag-dialect/1.0"}
    tok = os.environ.get("CIVITAI_API_TOKEN") or os.environ.get("CIVITAI_API_KEY")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


_WEIGHT_RE = re.compile(r"(?:strength|weight|вес|сила)[^0-9\n]{0,24}(\d(?:[.,]\d+)?)(?!\d)"
                        r"(?:\s*(?:-|–|—|~|to|до)\s*(\d(?:[.,]\d+)?)(?!\d))?", re.I)
_LORA_TAG_RE = re.compile(r"<lora:[^:>]+:(\d(?:\.\d+)?)", re.I)


def _weight_ok(w):
    return 0.1 <= w <= 1.5


def _weights_from_text(html):
    """Вес из описания автора: «weight 0.6-0.8», «strength: 0.7», «вес 0,5»."""
    txt = re.sub(r"<[^>]+>", " ", html or "")
    for m in _WEIGHT_RE.finditer(txt):
        lo = float(m.group(1).replace(",", "."))
        hi = float(m.group(2).replace(",", ".")) if m.group(2) else lo
        lo, hi = sorted((lo, hi))
        if _weight_ok(lo) and _weight_ok(hi):
            return lo, hi
    return None


def _weights_from_images(version):
    """Веса, с которыми автор сделал примеры этой версии (берём только однозначные случаи)."""
    out = []
    for im in version.get("images") or []:
        meta = im.get("meta") if isinstance(im, dict) else None
        if not isinstance(meta, dict):
            continue
        res = [r for r in (meta.get("civitaiResources") or []) if isinstance(r, dict)]
        hit = [r.get("weight") for r in res if r.get("modelVersionId") == version.get("id")]
        if not hit:
            loras = [r for r in (meta.get("resources") or []) if isinstance(r, dict) and r.get("type") == "lora"]
            hit = [r.get("weight") for r in loras] if len(loras) == 1 else []
        if not hit:
            tags = _LORA_TAG_RE.findall(str(meta.get("prompt") or ""))
            hit = tags if len(tags) == 1 else []
        for w in hit:
            try:
                w = float(w)
            except (TypeError, ValueError):
                continue
            if _weight_ok(w):
                out.append(w)
    return sorted(out)


def recommended_weight(version, descriptions):
    """-> {"lo", "hi", "src"} или None: сначала слова автора, потом примеры (нужно минимум 2)."""
    for html in descriptions:
        got = _weights_from_text(html)
        if got:
            return {"lo": got[0], "hi": got[1], "src": "описание"}
    ws = _weights_from_images(version)
    if len(ws) >= 2:
        return {"lo": ws[0], "hi": ws[-1], "src": f"примеры ({len(ws)})"}
    return None


def civitai_lookup(sha, cache):
    """-> dict(found, base, tags, name, trained, nsfw, type, rec) или None, если сеть недоступна."""
    c = cache["civitai"].get(sha)
    if c and c.get("v") == CACHE_V and (c.get("found") or time.time() - c.get("ts", 0) < NOTFOUND_TTL):
        return c
    try:
        v = _get_json(f"https://civitai.com/api/v1/model-versions/by-hash/{sha}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            c = {"v": CACHE_V, "found": False, "ts": time.time()}
            cache["civitai"][sha] = c
            return c
        print(f"[TagDialect] Civitai HTTP {e.code}")
        return c if c else None
    except Exception as e:
        print(f"[TagDialect] Civitai недоступен: {e}")
        return c if c else None
    vm = v.get("model") or {}
    tags, name, nsfw, mtype, mdesc = [], vm.get("name", ""), bool(vm.get("nsfw")), vm.get("type", ""), ""
    if v.get("modelId"):
        try:
            m = _get_json(f"https://civitai.com/api/v1/models/{v['modelId']}")
            tags = [str(t).lower() for t in m.get("tags", [])]
            name = m.get("name") or name
            nsfw = bool(m.get("nsfw", nsfw))
            mtype = m.get("type") or mtype
            mdesc = m.get("description") or ""
        except Exception as e:
            print(f"[TagDialect] теги модели не получены: {e}")
    c = {"v": CACHE_V, "found": True, "base": v.get("baseModel", ""), "tags": tags, "name": name,
         "trained": [w for w in (v.get("trainedWords") or []) if isinstance(w, str)],
         "nsfw": nsfw, "type": mtype, "rec": recommended_weight(v, [v.get("description"), mdesc]),
         "url": f"https://civitai.com/models/{v.get('modelId')}?modelVersionId={v.get('id')}", "ts": time.time()}
    cache["civitai"][sha] = c
    return c


def classify_meta(meta):
    base = (meta.get("base") or "").lower()
    tags = set(meta.get("tags") or [])
    if any(b in base for b in E621_BASES):
        return "e621", f"база {meta.get('base')} обучена на e621"
    hit = sorted(tags & FURRY_TAGS)
    if hit:
        return "e621", f"теги Civitai: {', '.join(hit)} (база {meta.get('base')})"
    if FURRY_NAME_RE.search(meta.get("name") or ""):
        return "e621", f"название на Civitai: {meta.get('name')} (база {meta.get('base')})"
    return "danbooru", f"база {meta.get('base')}, фурри-тегов нет"


def family_of(base):
    b = (base or "").lower()
    if "pony" in b:
        return "pony"
    if "noobai" in b or "illustrious" in b:
        return "illu"
    if "anima" in b:
        return "anima"
    if "sdxl" in b or "playground" in b:
        return "sdxl"
    if "sd 1" in b:
        return "sd15"
    if "flux" in b:
        return "flux"
    return "other"


def family_from_path(path):
    p = (path or "").lower().replace("\\", "/")
    if "pony" in p:
        return "pony"
    if "noob" in p or "illustrious" in p or re.search(r"(^|/)il[_-]|_il(v|_)", p):
        return "illu"
    if "anima" in p:
        return "anima"
    if "sdxl" in p or "xl" in p:
        return "sdxl"
    return "other"


# Качество-теги по семейству — так пишут под популярными картинками этих моделей на Civitai
QUALITY_POS = {
    "illu": "masterpiece, best quality, amazing quality, very aesthetic, absurdres, newest",
    "pony": "score_9, score_8_up, score_7_up, score_6_up, {source}",
    "anima": "masterpiece, best quality, very aesthetic, absurdres",
    "sdxl": "masterpiece, best quality, highly detailed",
    "other": "masterpiece, best quality",
}
QUALITY_NEG = {
    "illu": "worst quality, bad quality, low quality, lowres",
    "pony": "score_4, score_5, score_6, source_pony, worst quality, low quality",
    "anima": "worst quality, low quality, lowres",
    "sdxl": "worst quality, low quality, lowres, blurry",
    "other": "worst quality, low quality",
}


def checkpoint_info(ckpt_name):
    """-> (meta|None, family, why_no_meta) — с кэшем."""
    with _lock:
        cache = _load_cache()
        try:
            path = _resolve(ckpt_name)
            if not path:
                return None, family_from_path(ckpt_name), "файл чекпоинта не найден"
            meta = civitai_lookup(_sha256(path, cache), cache)
            if meta and meta.get("found"):
                return meta, family_of(meta.get("base")), ""
            return None, family_from_path(ckpt_name), ("нет на Civitai" if meta else "Civitai недоступен")
        finally:
            try:
                _save_cache(cache)
            except Exception as e:
                print(f"[TagDialect] кэш не сохранён: {e}")


def decide(ckpt_name, mode, fallback_regex):
    """-> (dialect, info, family)"""
    meta, fam, why_fb = checkpoint_info(ckpt_name)
    if mode in ("e621", "danbooru"):
        return mode, "выбрано вручную", fam
    if meta:
        d, why = classify_meta(meta)
        return d, f"Civitai «{meta.get('name')}»: {why}", fam
    try:
        hit = re.search(fallback_regex or DEFAULT_FALLBACK, ckpt_name, re.I)
    except re.error as e:
        return "danbooru", f"{why_fb}; ошибка в regex: {e}", fam
    if hit:
        return "e621", f"{why_fb}; правило: в пути есть «{hit.group(0)}»", fam
    return "danbooru", f"{why_fb}; правило не сработало", fam


def composition(text):
    t = f", {(text or '').lower()},"
    has = lambda w: re.search(rf"(^|,)\s*{re.escape(w)}\s*(,|$)", t) is not None
    male = has("male/female") or has("1boy") or has("hetero") or "1boy" in t
    multi = male or any(has(w) for w in ("duo", "trio", "group", "2girls", "3girls", "multiple girls", "female/female"))
    solo = has("solo") and not multi
    return solo, multi, male


# Мужской персонаж соло и male/male не нужны никогда; при solo — запрещаем второго персонажа
COMP_NEG_ALWAYS = "male/male, male focus, yaoi, 2boys, multiple boys, bara, feral, quadruped"
COMP_NEG_SOLO = "duo, trio, group, 1boy, male, 2girls, multiple girls"
COMP_NEG_FEMALE_ONLY = "1boy, male"


def composition_negative(text):
    solo, multi, male = composition(text)
    if solo:
        return _join(COMP_NEG_ALWAYS, COMP_NEG_SOLO)
    if multi and not male:
        return _join(COMP_NEG_ALWAYS, COMP_NEG_FEMALE_ONLY)
    return COMP_NEG_ALWAYS


def _join(*parts):
    """Склеивает куски промпта через запятую и убирает повторы тегов."""
    out, seen = [], set()
    for part in parts:
        for t in (part or "").split(","):
            t = t.strip()
            k = t.lower().replace("_", " ")
            if t and k not in seen:
                seen.add(k)
                out.append(t)
    return ", ".join(out)
