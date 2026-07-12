from marcus_code.banner import LOGO_TEXT, MAX_WIDTH, TAGLINE, _render_block_text, render_banner


def test_block_text_rows_are_all_equal_width():
    rows = _render_block_text(LOGO_TEXT)

    assert len(rows) == 5
    widths = {len(row) for row in rows}
    assert len(widths) == 1


def test_block_text_fits_within_max_width():
    rows = _render_block_text(LOGO_TEXT)

    assert all(len(row) <= MAX_WIDTH for row in rows)


def test_block_text_uses_only_block_char_and_space():
    rows = _render_block_text(LOGO_TEXT)

    allowed = {"█", " "}
    assert all(set(row) <= allowed for row in rows)


def test_render_banner_fancy_includes_ansi_colors_and_tagline():
    output = render_banner(force_fancy=True)

    assert "\x1b[94m" in output  # bright blue for the art
    assert "\x1b[91m" in output  # bright red for the tagline
    assert "\x1b[96m" in output  # bright cyan for the underline
    assert "\x1b[0m" in output  # reset
    assert TAGLINE in output


def test_render_banner_fancy_tagline_is_left_aligned_with_the_logo():
    output = render_banner(force_fancy=True)
    tagline_line = next(line for line in output.splitlines() if TAGLINE in line)

    # No leading spaces before the color code + text — flush with the art's
    # left edge, not centered within its width.
    assert tagline_line == f"\x1b[91m{TAGLINE}\x1b[0m"


def test_render_banner_fancy_has_cyan_underline_below_tagline():
    lines = render_banner(force_fancy=True).splitlines()
    tagline_index = next(i for i, line in enumerate(lines) if TAGLINE in line)
    underline = lines[tagline_index + 1]

    assert underline.startswith("\x1b[96m")
    assert "─" in underline


def test_render_banner_fancy_lines_fit_within_max_width():
    output = render_banner(force_fancy=True)

    for line in output.splitlines():
        # Strip ANSI escapes before measuring — they're zero-width on screen.
        visible = _strip_ansi(line)
        assert len(visible) <= MAX_WIDTH


def test_render_banner_plain_fallback_has_no_ansi_or_block_chars():
    output = render_banner(force_fancy=False)

    assert output == LOGO_TEXT
    assert "\x1b[" not in output
    assert "█" not in output


def test_render_banner_plain_fallback_has_no_emoji():
    output = render_banner(force_fancy=False)

    assert all(ord(ch) < 0x1F300 for ch in output)


def _strip_ansi(text: str) -> str:
    result = []
    in_escape = False
    for ch in text:
        if ch == "\x1b":
            in_escape = True
            continue
        if in_escape:
            if ch == "m":
                in_escape = False
            continue
        result.append(ch)
    return "".join(result)
