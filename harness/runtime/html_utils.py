"""HTML text extraction helper used by native tools and CLI tools."""

from html.parser import HTMLParser


class _HTMLTextExtractor(HTMLParser):
    """Minimal tag-stripper — avoids pulling in a full HTML parsing dependency."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self._skipping = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skipping += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)


def strip_html(html: str) -> str:
    """Return readable plain text from HTML, stripping script/style blocks."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    return "\n".join(extractor.chunks)
