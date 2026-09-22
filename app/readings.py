"""Offline, display-only furigana; subtitle text and exports never change."""
from functools import lru_cache
import re
import threading

_lock = threading.Lock()
_kanji = re.compile(r"[\u3400-\u9fff々〆〇\U00020000-\U0002fa1f]")


@lru_cache(maxsize=1)
def _tokenizer():
    from janome.tokenizer import Tokenizer
    return Tokenizer()


def _hiragana(text):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


@lru_cache(maxsize=2048)
def furigana(text):
    parts, cursor = [], 0
    with _lock:
        for token in _tokenizer().tokenize(text):
            surface = token.surface
            pos = text.find(surface, cursor)
            if pos < cursor:
                return [{"text": text, "reading": ""}]
            if pos > cursor:
                parts.append({"text": text[cursor:pos], "reading": ""})
            cursor = pos + len(surface)
            reading = _hiragana(token.reading)
            if not _kanji.search(surface) or not re.fullmatch(r"[ぁ-ゖー]+", reading):
                parts.append({"text": surface, "reading": ""})
                continue
            # Strip matching kana at the edges: 食べる -> 食(た)べる, お祝い -> お祝(いわ)い.
            prefix, suffix = "", ""
            while surface and reading and not _kanji.match(surface[0]) and _hiragana(surface[0]) == reading[0]:
                prefix += surface[0]
                surface, reading = surface[1:], reading[1:]
            while surface and reading and not _kanji.match(surface[-1]) and _hiragana(surface[-1]) == reading[-1]:
                suffix = surface[-1] + suffix
                surface, reading = surface[:-1], reading[:-1]
            if prefix:
                parts.append({"text": prefix, "reading": ""})
            if surface:
                parts.append({"text": surface, "reading": reading})
            if suffix:
                parts.append({"text": suffix, "reading": ""})
    if cursor < len(text):
        parts.append({"text": text[cursor:], "reading": ""})
    return parts
