"""Защита, которую нельзя отключить настройками workflow.

Возрастной негатив действует всегда, даже при пустой ноде негатива. Стоп-теги вычёркиваются из готового промпта,
откуда бы они ни пришли: строка повтора, закрепление в списке, триггер LoRA.
"""
import re

AGE_GUARD = "young, child, loli, shota, cub, aged down, toddler"
STOP_RE = re.compile(
    r"\b(?:loli|lolicon|shota|shotacon|cub|cubs|child|children|kid|kids|toddler|infant|baby|underage|preteen|"
    r"teen|teens|teenager|teenage|young|younger|aged down|schoolgirl|schoolboy|school|flat chest|"
    r"feral|quadruped|bestiality|zoophilia|my little pony|mlp|friendship is magic|"
    r"rape|raped|forced|forcing|non-con|noncon|non-consensual|unconscious|asleep|sleeping|sleep|"
    r"voyeur|voyeurism|peeping|hidden camera|upskirt|panty shot|hypnosis|hypnotized|mind control|mind break|"
    r"tentacle|tentacles|gore|guro|snuff|vore|scat|feces)\b", re.I)


def is_stopped(tag):
    plain = re.sub(r":-?\d*\.?\d+\s*\)?$", "", tag.replace("\\", "").replace("_", " "))
    return bool(STOP_RE.search(re.sub(r"[()\[\]]", " ", plain)))


def strip_stopped(text):
    """-> (текст без стоп-тегов, список вычеркнутого)"""
    kept, removed = [], []
    for part in (text or "").split(","):
        tag = part.strip()
        if tag:
            (removed if is_stopped(tag) else kept).append(tag)
    return ", ".join(kept), removed
