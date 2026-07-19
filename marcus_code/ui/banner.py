import sys

# Solid-block letterforms, 5 rows tall. Only the glyphs MARCUS CODE actually
# needs. Deliberately NOT run through Rich's Console — Rich's markup parser
# treats a literal "[" as the start of a style tag (see marcus_code/ui.py's
# escape() usage), and raw ANSI escape sequences like "\x1b[94m" contain
# exactly that character; passing this through console.print() risks the
# same silent-corruption bug already hit twice with the (y)/(n)/(a) prompt
# and the "[default]" setup hints. Plain print() sidesteps it entirely.
_FONT: dict[str, list[str]] = {
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "R": ["████ ", "█   █", "████ ", "█  █ ", "█   █"],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "U": ["█   █", "█   █", "█   █", "█   █", " ███ "],
    "S": [" ████", "█    ", " ███ ", "    █", "████ "],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    " ": ["   ", "   ", "   ", "   ", "   "],
}
_GLYPH_HEIGHT = 5

LOGO_TEXT = "MARCUS CODE"
TAGLINE = "Plan. Build. Verify."

_BLUE = "\x1b[94m"
# Faint dim-gray tagline — deliberately muted so it recedes behind the logo
# (SGR 2 "faint" + bright-black; terminals that ignore SGR 2 still show gray).
_FAINT = "\x1b[2;90m"
_CYAN = "\x1b[96m"
_RESET = "\x1b[0m"

MAX_WIDTH = 80


def _render_block_text(text: str) -> list[str]:
    glyphs = [_FONT.get(ch.upper(), _FONT[" "]) for ch in text]
    return [" ".join(glyph[row] for glyph in glyphs) for row in range(_GLYPH_HEIGHT)]


def enable_windows_ansi() -> bool:
    """Turn on VT/ANSI processing on the Windows console (what colorama's
    init() does internally) so raw ANSI escapes render instead of printing
    as literal garbage. Idempotent and harmless to call repeatedly or on
    terminals that already support it (Windows Terminal, VS Code, modern
    PowerShell). Returns whether ANSI is (now) usable.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        STD_OUTPUT_HANDLE = -11
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except Exception:  # noqa: BLE001 - any failure here just means "no fancy banner"
        return False


def supports_fancy_banner() -> bool:
    """All three must hold: a real (non-redirected) terminal, UTF-8 output
    encoding, and working ANSI color — matching Console.IsOutputRedirected
    and Console.OutputEncoding from the .NET-style spec this was written
    against, translated to Python's equivalents."""
    if not sys.stdout.isatty():
        return False
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    if "utf" not in encoding:
        return False
    return enable_windows_ansi()


def render_banner(*, force_fancy: bool | None = None) -> str:
    """Render the MARCUS CODE header. force_fancy overrides capability
    detection (for tests); leave it None in production code."""
    fancy = supports_fancy_banner() if force_fancy is None else force_fancy
    if not fancy:
        return LOGO_TEXT

    rows = _render_block_text(LOGO_TEXT)
    width = len(rows[0])
    art_lines = [f"{_BLUE}{line}{_RESET}" for line in rows]
    # Left-aligned, flush with the logo's left edge (no centering) — matches
    # the tagline against the art instead of floating it in the middle.
    tagline = f"{_FAINT}{TAGLINE}{_RESET}"
    underline = f"{_CYAN}{'─' * width}{_RESET}"
    return "\n".join([*art_lines, tagline, underline])
