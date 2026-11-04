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
NOTFOUND_TTL = 7 * 24 * 3600          # «нет на Civitai» перепроверяем раз в неделю
E621_BASES = ("pony", "noobai")         # базы, которые учили на e621
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


def civitai_lookup(sha, cache):
    """-> dict(found, base, tags, name, trained, nsfw, type) или None, если сеть недоступна."""
    c = cache["civitai"].get(sha)
    if c and c.get("v") == 2 and (c.get("found") or time.time() - c.get("ts", 0) < NOTFOUND_TTL):
        return c
    try:
        v = _get_json(f"https://civitai.com/api/v1/model-versions/by-hash/{sha}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            c = {"v": 2, "found": False, "ts": time.time()}
            cache["civitai"][sha] = c
            return c
        print(f"[TagDialect] Civitai HTTP {e.code}")
        return c if c else None
    except Exception as e:
        print(f"[TagDialect] Civitai недоступен: {e}")
        return c if c else None
    vm = v.get("model") or {}
    tags, name, nsfw, mtype = [], vm.get("name", ""), bool(vm.get("nsfw")), vm.get("type", "")
    if v.get("modelId"):
        try:
            m = _get_json(f"https://civitai.com/api/v1/models/{v['modelId']}")
            tags = [str(t).lower() for t in m.get("tags", [])]
            name = m.get("name") or name
            nsfw = bool(m.get("nsfw", nsfw))
            mtype = m.get("type") or mtype
        except Exception as e:
            print(f"[TagDialect] теги модели не получены: {e}")
    c = {"v": 2, "found": True, "base": v.get("baseModel", ""), "tags": tags, "name": name,
         "trained": [w for w in (v.get("trainedWords") or []) if isinstance(w, str)],
         "nsfw": nsfw, "type": mtype,
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
    "pony": "score_4, score_5, score_6, worst quality, low quality",
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


class TagDialectSwitch:
    """Выбирает ветку промпта (e621 или Danbooru) по тому, что понимает чекпоинт,
    и добавляет качество-теги под семейство модели (Illustrious/NoobAI, Pony, SDXL, Anima).
    Считается только выбранная ветка (lazy-входы)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "ckpt_name": ("STRING", {"forceInput": True}),
                "mode": (["auto", "e621", "danbooru"], {"default": "auto"}),
                "fallback_regex": ("STRING", {"default": DEFAULT_FALLBACK,
                                              "tooltip": "Если модели нет на Civitai: путь совпал -> e621, иначе Danbooru"}),
                "add_quality": ("BOOLEAN", {"default": True,
                                            "tooltip": "Добавить в начало качество-теги под семейство модели (score_9… для Pony и т.д.), а в негатив — их пару"}),
            },
            "optional": {
                "e621_text": ("STRING", {"forceInput": True, "lazy": True}),
                "danbooru_text": ("STRING", {"forceInput": True, "lazy": True}),
                "negative_text": ("STRING", {"forceInput": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "BOOLEAN", "STRING", "STRING")
    RETURN_NAMES = ("text", "dialect", "info", "is_e621", "negative", "family")
    FUNCTION = "run"
    CATEGORY = "utils/prompt"

    def check_lazy_status(self, ckpt_name, mode, fallback_regex, add_quality=True,
                          e621_text=None, danbooru_text=None, negative_text=None):
        d, _, _ = decide(ckpt_name, mode, fallback_regex)
        if d == "e621" and e621_text is None:
            return ["e621_text"]
        if d == "danbooru" and danbooru_text is None:
            return ["danbooru_text"]
        return []

    def run(self, ckpt_name, mode, fallback_regex, add_quality=True,
            e621_text=None, danbooru_text=None, negative_text=None):
        d, info, fam = decide(ckpt_name, mode, fallback_regex)
        text = (e621_text if d == "e621" else danbooru_text) or ""
        neg = negative_text or ""
        if add_quality:
            q = QUALITY_POS.get(fam, QUALITY_POS["other"]).format(
                source="source_furry" if d == "e621" else "source_anime")
            text = _join(q, text)
            neg = _join(neg, QUALITY_NEG.get(fam, QUALITY_NEG["other"]))
        info = f"{d.upper()} · семейство {fam} ← {info}  [{ckpt_name}]"
        print(f"[TagDialect] {info}")
        return {"ui": {"text": [info]}, "result": (text, d, info, d == "e621", neg, fam)}
