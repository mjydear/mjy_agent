"""Replaceable token counting with a conservative dependency-free fallback."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from athena.types import JSONValue


class ModelTokenizer(Protocol):
    """The small tokenizer interface needed by :class:`TokenMeter`."""

    def encode(self, text: str, **kwargs: Any) -> Sequence[object]: ...


TokenizerLike = ModelTokenizer | Callable[[str], object]

class TokenMeter:
    """Count model input tokens through an injectable tokenizer seam.

    ``tokenizer`` may be a tiktoken-like object with ``encode`` or a callable
    returning either a token sequence or an integer.  A Hugging Face-style
    ``encode`` adapter is supported as well.  If the adapter is unavailable or
    fails, the fallback counts every CJK/non-ASCII character individually and
    uses a conservative four-ASCII-characters-per-token estimate for ASCII
    runs.  In particular, it never applies an ASCII ratio to CJK text.
    """

    def __init__(
        self,
        tokenizer: TokenizerLike | None = None,
        *,
        model_tokenizer: TokenizerLike | None = None,
        cache_size: int = 2048,
    ) -> None:
        if tokenizer is not None and model_tokenizer is not None:
            raise ValueError("provide tokenizer or model_tokenizer, not both")
        if cache_size < 0:
            raise ValueError("cache_size must be non-negative")
        self._tokenizer = tokenizer if tokenizer is not None else model_tokenizer
        self._cache_size = cache_size
        self._cache: dict[str, int] = {}

    @property
    def has_tokenizer(self) -> bool:
        """Whether a model tokenizer adapter was configured."""

        return self._tokenizer is not None

    def count(self, value: str | JSONValue) -> int:
        """Return a non-negative token count for text or JSON-compatible data."""

        text = self._serialize(value)
        cached = self._cache.get(text)
        if cached is not None:
            return cached

        count = self._count_with_tokenizer(text)
        if count is None:
            count = self.fallback_count(text)
        count = max(0, count)
        if self._cache_size:
            if len(self._cache) >= self._cache_size:
                self._cache.pop(next(iter(self._cache)))
            self._cache[text] = count
        return count

    def count_json(self, value: JSONValue) -> int:
        """Count a structured value using stable, non-ASCII JSON serialization."""

        return self.count(value)

    @classmethod
    def fallback_count(cls, text: str) -> int:
        """Conservatively estimate tokens without a model vocabulary."""

        if not text:
            return 0

        total = 0
        ascii_run = 0

        def flush_ascii() -> None:
            nonlocal total, ascii_run
            if ascii_run:
                total += math.ceil(ascii_run / 4)
                ascii_run = 0

        for character in text:
            if character.isspace():
                flush_ascii()
                continue
            if ord(character) < 128:
                ascii_run += 1
                continue
            flush_ascii()
            # CJK characters are close to one token for common model
            # vocabularies; counting each one is deliberately conservative.
            total += 1

        flush_ascii()
        return total

    def _count_with_tokenizer(self, text: str) -> int | None:
        tokenizer = self._tokenizer
        if tokenizer is None:
            return None
        try:
            encoder = getattr(tokenizer, "encode", None)
            if callable(encoder):
                try:
                    result = encoder(text, add_special_tokens=False)
                except TypeError:
                    result = encoder(text)
            elif callable(tokenizer):
                result = tokenizer(text)
            else:
                return None
            if isinstance(result, bool):
                return None
            if isinstance(result, int):
                return result if result >= 0 else None
            length = len(result)  # type: ignore[arg-type]
            return length if length >= 0 else None
        except Exception:
            # A broken optional adapter must not make context construction fail.
            return None

    @staticmethod
    def _serialize(value: str | JSONValue) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            return str(value)
