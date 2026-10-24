"""Two tiny no-LLM intents the scheduler answers before any device resolution (S8p): "list my devices" and "who/what
are you". Pure text matching on the normalised utterance; nothing here touches a device or the model."""
from __future__ import annotations

import re

from .text import normalise, strip_polite

_LIST_RES = tuple(re.compile(p) for p in (
    r"(?:(?:list|show|display|get|give)(?: me)?(?: all| the| my| every)* )?(?:devices|things)(?: list)?",
    r"what (?:devices|things) (?:do i have|are there|are connected|can you control|do you control|are available)",
    r"what can you (?:control|do)",
    r"what do you (?:control|do)",
))
_IDENTITY_RES = tuple(re.compile(p) for p in (
    r"who are you", r"what are you", r"who r u", r"what(?:s| is) your name", r"who made you", r"who built you",
    r"(?:what|which)(?: is| are)?(?: the| your)? (?:llm|model|ai|language model)(?: name| version)?(?: are you| do you use| are you using| is this| is it)?",
    r"(?:what|which) (?:llm|model|ai) (?:are you|do you use|are you using|is this|is it)",
    r"(?:what|which) (?:is|s) (?:the )?(?:name of the |name of your )?(?:llm|model|ai)",
    r"are you (?:an? )?(?:ai|bot|robot|chatgpt|gpt|claude|llama|gemini)",
    r"tell me about yourself", r"introduce yourself",
))


def _norm(text: str) -> str:
    return " ".join(strip_polite(normalise(text)).split())


def is_list_intent(text: str) -> bool:
    s = _norm(text)
    return bool(s) and any(rx.fullmatch(s) for rx in _LIST_RES)


def is_identity_intent(text: str) -> bool:
    s = _norm(text)
    return bool(s) and len(s) <= 80 and any(rx.fullmatch(s) for rx in _IDENTITY_RES)
