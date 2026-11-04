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
    """-> dict(found, base, tags, name) или None, если сеть недоступна."""
    c = cache["civitai"].get(sha)
    if c and (c.get("found") or time.time() - c.get("ts", 0) < NOTFOUND_TTL):
        return c
    try:
        v = _get_json(f"https://civitai.com/api/v1/model-versions/by-hash/{sha}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            c = {"found": False, "ts": time.time()}
            cache["civitai"][sha] = c
            return c
        print(f"[TagDialect] Civitai HTTP {e.code}")
        return None
    except Exception as e:
        print(f"[TagDialect] Civitai недоступен: {e}")
        return None
    tags, name = [], (v.get("model") or {}).get("name", "")
    if v.get("modelId"):
        try:
            m = _get_json(f"https://civitai.com/api/v1/models/{v['modelId']}")
            tags = [str(t).lower() for t in m.get("tags", [])]
            name = m.get("name") or name
        except Exception as e:
            print(f"[TagDialect] теги модели не получены: {e}")
    c = {"found": True, "base": v.get("baseModel", ""), "tags": tags, "name": name,
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


def decide(ckpt_name, mode, fallback_regex):
    """-> (dialect, info)"""
    if mode in ("e621", "danbooru"):
        return mode, "выбрано вручную"
    with _lock:
        cache = _load_cache()
        try:
            path = _resolve(ckpt_name)
            if path:
                sha = _sha256(path, cache)
                meta = civitai_lookup(sha, cache)
                if meta and meta.get("found"):
                    d, why = classify_meta(meta)
                    return d, f"Civitai «{meta.get('name')}»: {why}"
                why_fb = "нет на Civitai" if meta else "Civitai недоступен"
            else:
                why_fb = "файл чекпоинта не найден"
        finally:
            try:
                _save_cache(cache)
            except Exception as e:
                print(f"[TagDialect] кэш не сохранён: {e}")
    try:
        hit = re.search(fallback_regex or DEFAULT_FALLBACK, ckpt_name, re.I)
    except re.error as e:
        return "danbooru", f"{why_fb}; ошибка в regex: {e}"
    if hit:
        return "e621", f"{why_fb}; правило: в пути есть «{hit.group(0)}»"
    return "danbooru", f"{why_fb}; правило не сработало"


class TagDialectSwitch:
    """Выбирает ветку промпта (e621 или Danbooru) по тому, что понимает чекпоинт.
    Считается только выбранная ветка (lazy-входы)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "ckpt_name": ("STRING", {"forceInput": True}),
                "mode": (["auto", "e621", "danbooru"], {"default": "auto"}),
                "fallback_regex": ("STRING", {"default": DEFAULT_FALLBACK,
                                              "tooltip": "Если модели нет на Civitai: путь совпал -> e621, иначе Danbooru"}),
            },
            "optional": {
                "e621_text": ("STRING", {"forceInput": True, "lazy": True}),
                "danbooru_text": ("STRING", {"forceInput": True, "lazy": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "BOOLEAN")
    RETURN_NAMES = ("text", "dialect", "info", "is_e621")
    FUNCTION = "run"
    CATEGORY = "utils/prompt"

    def check_lazy_status(self, ckpt_name, mode, fallback_regex, e621_text=None, danbooru_text=None):
        d, _ = decide(ckpt_name, mode, fallback_regex)
        if d == "e621" and e621_text is None:
            return ["e621_text"]
        if d == "danbooru" and danbooru_text is None:
            return ["danbooru_text"]
        return []

    def run(self, ckpt_name, mode, fallback_regex, e621_text=None, danbooru_text=None):
        d, info = decide(ckpt_name, mode, fallback_regex)
        info = f"{d.upper()} ← {info}  [{ckpt_name}]"
        print(f"[TagDialect] {info}")
        text = e621_text if d == "e621" else danbooru_text
        return {"ui": {"text": [info]}, "result": (text or "", d, info, d == "e621")}
