"""Fail-closed response-language checks for user-visible assistant prose.

The model may occasionally code-switch after a contaminated conversation
turn.  This module deliberately stays independent of provider/runtime code so
the final-response loop can validate text before it is persisted or delivered.
Literal code, inline-code spans, and URLs are excluded from the check because
they may legitimately contain non-Korean scripts.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"(?:https?://|ftp://)\S+", re.IGNORECASE)

# Scripts that should not leak into Korean prose.  ASCII/Latin is allowed for
# model names, commands, tickers, and technical terminology.  Extended Latin
# is rejected as well: observed corruptions included Turkish/Vietnamese words
# such as ``oldu`` with diacritics and ``cho da`` with combining marks.  A
# legitimate foreign literal can still be preserved in an inline/fenced code
# span, which is removed by ``_prose_only`` before this pattern runs.
_KO_FOREIGN_SCRIPT_RE = re.compile(
    "["
    "\u00c0-\u024f"  # Latin-1 Supplement + Latin Extended A/B
    "\u0300-\u036f"  # Combining diacritical marks
    "\u3400-\u4dbf"  # CJK Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\u3040-\u30ff"  # Hiragana + Katakana
    "\uff65-\uff9f"  # Half-width Katakana
    "\u0400-\u052f"  # Cyrillic
    "\u0530-\u058f"  # Armenian
    "\u0590-\u05ff"  # Hebrew
    "\u0600-\u06ff"  # Arabic
    "\u0750-\u077f"
    "\u08a0-\u08ff"
    "\ufb50-\ufdff"
    "\ufe70-\ufeff"
    "\u0900-\u0dff"  # Indic scripts
    "\u0e00-\u0fff"  # Thai, Lao, Tibetan
    "\u1000-\u109f"  # Myanmar
    "\u10a0-\u10ff"  # Georgian
    "\u1200-\u137f"  # Ethiopic
    "\u1780-\u17ff"  # Khmer
    "\u1800-\u18af"  # Mongolian
    "]"
)

_HANGUL_RE = re.compile("[\uac00-\ud7a3]")
_ASCII_LATIN_RE = re.compile("[A-Za-z]")
_ASCII_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./:-]*")


@dataclass(frozen=True)
class ResponseLanguageViolation:
    """A compact, log-safe description of a mixed-script response."""

    language: str
    count: int
    sample: str


def normalize_response_language(value: object) -> str:
    """Return the supported language code, or ``""`` when disabled/invalid."""

    if value is None or value is False:
        return ""
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized in {"", "off", "none", "auto", "false", "0"}:
        return ""
    if normalized in {"ko", "ko-kr", "kr", "korean", "한국어"}:
        return "ko"
    return ""


def _prose_only(text: str) -> str:
    text = _FENCED_CODE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    return _URL_RE.sub(" ", text)


def inspect_response_language(
    text: object,
    language: object,
) -> ResponseLanguageViolation | None:
    """Detect foreign-script leakage in configured user-visible prose.

    The check is intentionally strict for Korean: a single leaked foreign
    script character triggers repair.  Legitimate literals can be preserved by
    placing them in Markdown code spans/fences, which the detector ignores.
    """

    normalized = normalize_response_language(language)
    if normalized != "ko" or not isinstance(text, str) or not text:
        return None

    prose = _prose_only(text)
    matches = _KO_FOREIGN_SCRIPT_RE.findall(prose)
    if not matches:
        # Catch short, visibly broken mixed-language fragments that use only
        # ASCII and therefore cannot be identified by Unicode script ranges.
        # Keep this deliberately narrow so normal technical Korean such as
        # "HTTP 200으로 정상입니다" remains valid.  The production failure that
        # motivated this check was a 45-character answer containing only four
        # Hangul syllables and three long English-like tokens.
        compact = re.sub(r"\s+", " ", prose).strip()
        if len(compact) <= 160:
            hangul_count = len(_HANGUL_RE.findall(compact))
            latin_chars = len(_ASCII_LATIN_RE.findall(compact))
            latin_words = _ASCII_LATIN_WORD_RE.findall(compact)
            if (
                hangul_count < 10
                and latin_chars >= 12
                and len(latin_words) >= 2
                and latin_chars > max(12, hangul_count * 3)
            ):
                return ResponseLanguageViolation(
                    language="ko",
                    count=latin_chars,
                    sample=" ".join(latin_words[:3])[:48],
                )
        return None
    return ResponseLanguageViolation(
        language="ko",
        count=len(matches),
        sample="".join(matches[:12]),
    )


def build_response_language_repair_prompt(language: object) -> str:
    """Return the private retry instruction for a language violation."""

    if normalize_response_language(language) != "ko":
        return ""
    return (
        "[System: The previous draft mixed foreign scripts into Korean prose. "
        "Rewrite the entire answer in concise, natural Korean. Do not use "
        "non-Korean scripts or fragmented English-like filler in prose. Preserve "
        "facts, numbers, commands, URLs, file paths, code blocks, and Markdown. "
        "Do not add new claims or commentary. Output only the corrected answer.]"
    )


def response_language_failure_message(language: object) -> str:
    """Return a clean fail-closed message after bounded repair retries."""

    if normalize_response_language(language) == "ko":
        return (
            "응답 생성 중 문자 오염이 반복되어 이번 답변을 보류했습니다. "
            "현재 대화와 기록은 유지되며 같은 세션에서 계속 처리합니다."
        )
    return "The response failed the configured language validation."
