"""Narrow provider hooks: preserve upstream requests and attach exact download provenance."""

from collections.abc import Sequence
from typing import Any

from parsehub.types.result import ParseResult


def provenance_wrapper(original: Any) -> Any:
    async def download(self: ParseResult, **kwargs: Any) -> Any:
        result = await original(self, **kwargs)
        refs = list(self.media) if isinstance(self.media, Sequence) else [self.media] if self.media else []
        files = list(result.media) if isinstance(result.media, Sequence) else [result.media] if result.media else []
        # This wraps the base downloader itself, whose contract preserves one file per ref or raises.
        if len(refs) == len(files):
            for ref, file in zip(refs, files, strict=True):
                file._reader_original_url = ref.url
        return result
    return download
