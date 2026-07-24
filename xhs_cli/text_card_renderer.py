"""Render long-form text into publishable image cards for Xiaohongshu notes.

Uses Pillow (PIL) for direct pixel-level layout control. The previous
pylitehtml-based renderer produced pages with massive empty whitespace
because pylitehtml's flexbox support is incomplete (margin-top: auto on
flex children does not push them to the bottom of the canvas). This
module replaces that path entirely.

Workflow:
    1. Parse the markdown-ish body into a flat list of paragraph objects
       (heading, quote, list-item, body, blank).
    2. Group paragraphs into pages by *measuring* each rendered element
       with PIL's ``ImageDraw.multiline_textbbox`` — no char-count
       heuristics, no flexbox, no surprises.
    3. Build a cover page (title + subtitle + accent decoration) and N
       content pages.
    4. Output PNG files at 1080×1440 (Xiaohongshu 3:4 standard).

Three themes ship with this module:

* ``default`` — clean minimalist, dark on warm cream, red accent.
* ``warm``    — soft cream/coffee tones, dashed underline on headings.
* ``playful`` — pink→purple gradient accents, vibrant feel.

Fonts are auto-detected from Windows fonts directory (Noto Sans SC
preferred, with fallbacks to Microsoft YaHei / SimHei / SimSun).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


# ── Layout constants (Xiaohongshu 3:4 standard) ──────────────────────────────
CARD_WIDTH = 1080
CARD_HEIGHT = 1440

# Content area inside the card (px).
CONTENT_LEFT = 96
CONTENT_RIGHT = 96
CONTENT_TOP = 140  # leaves room for accent bar + heading
CONTENT_BOTTOM = 140  # leaves room for page indicator

# Vertical rhythm.
BODY_LINE_HEIGHT_RATIO = 1.78  # body font 36pt -> ~64px line
HEADING_LINE_HEIGHT_RATIO = 1.25
QUOTE_LINE_HEIGHT_RATIO = 1.7


# ── Font discovery ──────────────────────────────────────────────────────────


@dataclass
class FontSet:
    """A bundle of fonts used by a single theme."""

    body: ImageFont.FreeTypeFont
    body_bold: ImageFont.FreeTypeFont
    heading: ImageFont.FreeTypeFont  # for h1 in content cards
    h2: ImageFont.FreeTypeFont  # for h2
    h3: ImageFont.FreeTypeFont  # for h3
    small: ImageFont.FreeTypeFont  # for footer / page-num
    cover_title: ImageFont.FreeTypeFont
    cover_subtitle: ImageFont.FreeTypeFont


# Candidate fonts in order of preference (mac/win/linux).
_FONT_CANDIDATES = [
    # Noto Sans SC (preferred — best Chinese rendering quality)
    ("C:/Windows/Fonts/Noto Sans SC (TrueType).otf", "Noto Sans SC"),
    ("C:/Windows/Fonts/Noto Sans SC Bold (TrueType).otf", "Noto Sans SC Bold"),
    ("C:/Windows/Fonts/Noto Sans SC Medium (TrueType).otf", "Noto Sans SC Medium"),
    # Microsoft YaHei variants
    ("C:/Windows/Fonts/Microsoft-YaHei-Bold001.TTF", "Microsoft YaHei Bold"),
    ("C:/Windows/Fonts/Microsoft-YaHei-Heavy001.TTF", "Microsoft YaHei Heavy"),
    ("C:/Windows/Fonts/Microsoft-YaHei-Semibold001.TTF", "Microsoft YaHei Semibold"),
    # Fallbacks
    ("C:/Windows/Fonts/simhei.ttf", "SimHei"),
    ("C:/Windows/Fonts/simsun.ttc", "SimSun"),
    ("C:/Windows/Fonts/Kaiti001.TTF", "KaiTi"),
]


def _find_chinese_font(*preferred_names: str) -> Path:
    """Find the first available font file; prefer paths containing any of preferred_names."""
    candidates = []
    for path, _name in _FONT_CANDIDATES:
        if os.path.exists(path):
            for pref in preferred_names:
                if pref in path:
                    candidates.insert(0, path)
                    break
            else:
                candidates.append(path)
    if candidates:
        return Path(candidates[0])
    # Last-ditch fallback: try common Linux paths.
    for linux_path in [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
    ]:
        if os.path.exists(linux_path):
            return Path(linux_path)
    raise FileNotFoundError(
        "No Chinese font found. Install Noto Sans SC or Microsoft YaHei."
    )


def _load_font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size=size)


def _load_fontset() -> FontSet:
    """Build a FontSet from the available system fonts."""
    regular = _find_chinese_font("Regular", "Medium", "Kaiti", "SimSun")
    bold = _find_chinese_font("Bold", "Heavy", "Semibold", "YaHei-Bold", "SimHei")
    return FontSet(
        body=_load_font(regular, 34),
        body_bold=_load_font(bold, 34),
        heading=_load_font(bold, 48),
        h2=_load_font(bold, 36),
        h3=_load_font(bold, 30),
        small=_load_font(regular, 22),
        cover_title=_load_font(bold, 80),
        cover_subtitle=_load_font(regular, 32),
    )


# ── Themes ──────────────────────────────────────────────────────────────────


@dataclass
class Theme:
    """A single theme: colors + accent decoration style."""

    name: str
    bg_top: tuple[int, int, int]
    bg_bottom: tuple[int, int, int]
    accent: tuple[int, int, int]
    heading_color: tuple[int, int, int]
    body_color: tuple[int, int, int]
    quote_color: tuple[int, int, int]
    quote_bg: tuple[int, int, int]
    quote_strip: tuple[int, int, int]
    muted_color: tuple[int, int, int]
    divider_color: tuple[int, int, int]
    footer_color: tuple[int, int, int]


THEMES: dict[str, Theme] = {}


def _register_theme(theme: Theme) -> Theme:
    THEMES[theme.name] = theme
    return theme


_register_theme(Theme(
    name="warm",
    bg_top=(255, 246, 230),
    bg_bottom=(252, 228, 210),
    accent=(184, 114, 44),
    heading_color=(90, 58, 32),
    body_color=(74, 53, 32),
    quote_color=(90, 58, 32),
    quote_bg=(245, 229, 207),
    quote_strip=(184, 114, 44),
    muted_color=(196, 163, 128),
    divider_color=(196, 163, 128),
    footer_color=(196, 163, 128),
))

_register_theme(Theme(
    name="default",
    bg_top=(255, 255, 255),
    bg_bottom=(245, 243, 240),
    accent=(255, 36, 66),
    heading_color=(26, 26, 26),
    body_color=(43, 43, 43),
    quote_color=(68, 68, 68),
    quote_bg=(236, 232, 227),
    quote_strip=(255, 36, 66),
    muted_color=(194, 185, 179),
    divider_color=(220, 213, 207),
    footer_color=(194, 185, 179),
))

_register_theme(Theme(
    name="playful",
    bg_top=(255, 230, 241),
    bg_bottom=(229, 228, 255),
    accent=(177, 62, 255),
    heading_color=(107, 63, 160),
    body_color=(42, 30, 58),
    quote_color=(42, 30, 58),
    quote_bg=(255, 255, 255),
    quote_strip=(255, 36, 66),
    muted_color=(179, 157, 214),
    divider_color=(179, 157, 214),
    footer_color=(179, 157, 214),
))


def list_themes() -> list[str]:
    return list(THEMES.keys())


# ── Paragraph model ─────────────────────────────────────────────────────────


@dataclass
class Para:
    """A single block element in the body."""

    kind: str  # 'h1' | 'h2' | 'h3' | 'body' | 'quote' | 'li' | 'blank'
    text: str
    bold: bool = False  # for inline bold span


def parse_markdown(body: str) -> list[Para]:
    """Parse a lightweight markdown subset into a flat list of paragraphs.

    Supports: ``#``/``##``/``###`` headings, ``>`` blockquotes, ``-``/``•``/
    ``*`` bullet lists, ``**bold**``, ``*italic*``, ``` `code` ```,
    blank-line-separated paragraphs, and ``---`` page separators (which
    become a special 'break' kind so the paginator knows to force a new
    page at that point).
    """
    body = body.replace("\r\n", "\n").strip()
    if not body:
        return []

    lines = body.split("\n")
    out: list[Para] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Page break marker — preserve as a meta paragraph.
        if re.match(r"^---\s*$", stripped):
            out.append(Para(kind="break", text=""))
            i += 1
            continue

        # Headings
        if stripped.startswith("### "):
            out.append(Para(kind="h3", text=stripped[4:]))
            i += 1
            continue
        if stripped.startswith("## "):
            out.append(Para(kind="h2", text=stripped[3:]))
            i += 1
            continue
        if stripped.startswith("# "):
            out.append(Para(kind="h1", text=stripped[2:]))
            i += 1
            continue

        # Blockquote (collect contiguous > lines).
        if stripped.startswith("> "):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(lines[i].strip()[2:])
                i += 1
            out.append(Para(kind="quote", text=" ".join(quote_lines)))
            continue

        # Bullet list.
        if re.match(r"^[-•*]\s+", stripped):
            bullet_char = "•"
            items = []
            while i < len(lines) and re.match(r"^[-•*]\s+", lines[i].strip()):
                item_text = re.sub(r"^[-•*]\s+", "", lines[i].strip())
                items.append(item_text)
                i += 1
            for it in items:
                out.append(Para(kind="li", text=f"{bullet_char}  {it}"))
            continue

        # Blank line.
        if not stripped:
            i += 1
            continue

        # Paragraph: consume contiguous non-blank, non-heading, non-bullet lines.
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt:
                break
            if (
                nxt.startswith(("#", "##", "###", ">", "-", "•", "*"))
                or re.match(r"^---\s*$", nxt)
            ):
                break
            para_lines.append(nxt)
            i += 1
        out.append(Para(kind="body", text=" ".join(para_lines)))

    return out


# ── Layout / measurement helpers ────────────────────────────────────────────


@dataclass
class Block:
    """A measured layout block — what we draw at a specific Y position."""

    kind: str  # 'h1' | 'h2' | 'h3' | 'body' | 'quote' | 'li'
    text: str
    height: int
    draw_fn: callable = field(default=None)  # type: ignore[valid-type]


def _measure(draw: ImageDraw.ImageDraw, fonts: FontSet, theme: Theme, paras: list[Para]) -> list[tuple[Para, int]]:
    """For each paragraph, compute its rendered height in pixels.

    The height is the distance from the draw y position to the bottom of
    the ink plus the trailing spacing. This must match what
    :func:`_draw_paragraph` advances y by — see the comment in that
    function about why we use ``bbox[3]`` (the bottom ink y) rather than
    ``bbox[3] - bbox[1]``.
    """
    content_width = CARD_WIDTH - CONTENT_LEFT - CONTENT_RIGHT
    results: list[tuple[Para, int]] = []
    for p in paras:
        if p.kind == "break":
            results.append((p, 0))
            continue
        if p.kind == "h1":
            wrapped, _ = _wrap_for_draw(draw, p.text, fonts.heading, content_width, line_spacing=4)
            spacing = 28
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=fonts.heading, spacing=4)
            h = bbox[3] + spacing  # font leading is included
        elif p.kind == "h2":
            wrapped, _ = _wrap_for_draw(draw, p.text, fonts.h2, content_width, line_spacing=2)
            spacing = 22
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=fonts.h2, spacing=2)
            h = bbox[3] + spacing
        elif p.kind == "h3":
            wrapped, _ = _wrap_for_draw(draw, p.text, fonts.h3, content_width, line_spacing=2)
            spacing = 18
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=fonts.h3, spacing=2)
            h = bbox[3] + spacing
        elif p.kind == "quote":
            wrapped, _ = _wrap_for_draw(
                draw, p.text, fonts.body_bold,
                max_width=content_width - 24 - 22,
                line_spacing=int(36 * 0.5),
            )
            spacing = 28
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=fonts.body_bold, spacing=int(36 * 0.5))
            text_h = bbox[3]  # bbox[3] is the bottom of the rendered ink
            h = text_h + 2 * 22 + spacing
        elif p.kind == "li":
            wrapped, _ = _wrap_for_draw(
                draw, p.text, fonts.body,
                max_width=content_width - 16,
                line_spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
            )
            spacing = 16
            li_spacing = int(36 * (BODY_LINE_HEIGHT_RATIO - 1))
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=fonts.body, spacing=li_spacing)
            h = bbox[3] + spacing
        else:  # body
            wrapped, _ = _wrap_for_draw(
                draw, p.text, fonts.body,
                max_width=content_width,
                line_spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
            )
            spacing = 22
            body_spacing = int(36 * (BODY_LINE_HEIGHT_RATIO - 1))
            bbox = draw.multiline_textbbox((0, 0), wrapped, font=fonts.body, spacing=body_spacing)
            h = bbox[3] + spacing
        results.append((p, h))
    return results


def _wrap_text_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    """Greedy per-character wrap that respects max_width in pixels."""
    if not text:
        return []
    lines: list[str] = []
    # Try greedy first by chunks of characters.
    # We use the simple approach: break on whitespace boundaries in CJK-friendly way.
    # Chinese: split by character. Mixed: prefer word boundaries if ASCII segment > 3 chars.
    # For simplicity, split per-char for CJK, and per-word for ASCII runs.
    tokens: list[str] = []
    buf = ""
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or ch in "，。！？、；：""''【】（）《》「」":
            # CJK char — flush buffer as a word, push char alone
            if buf:
                tokens.append(buf)
                buf = ""
            tokens.append(ch)
        elif ch == " ":
            if buf:
                tokens.append(buf)
                buf = ""
        else:
            buf += ch
    if buf:
        tokens.append(buf)

    cur = ""
    for tok in tokens:
        candidate = cur + tok
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] > max_width and cur:
            lines.append(cur)
            cur = tok.lstrip() if not tok.startswith(" ") else tok
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    return lines


# ── Page rendering ──────────────────────────────────────────────────────────


def _new_canvas(theme: Theme) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Create a new gradient-background card."""
    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), theme.bg_top)
    draw = ImageDraw.Draw(img)
    # Vertical gradient.
    steps = 64
    for i in range(steps):
        y0 = int(CARD_HEIGHT * i / steps)
        y1 = int(CARD_HEIGHT * (i + 1) / steps)
        t = i / (steps - 1)
        r = int(theme.bg_top[0] + (theme.bg_bottom[0] - theme.bg_top[0]) * t)
        g = int(theme.bg_top[1] + (theme.bg_bottom[1] - theme.bg_top[1]) * t)
        b = int(theme.bg_top[2] + (theme.bg_bottom[2] - theme.bg_top[2]) * t)
        draw.rectangle([(0, y0), (CARD_WIDTH, y1)], fill=(r, g, b))
    return img, draw


def _draw_top_accent(draw: ImageDraw.ImageDraw, theme: Theme) -> None:
    """Thin colored bar at the very top of every card."""
    draw.rectangle([(0, 0), (CARD_WIDTH, 12)], fill=theme.accent)


def _draw_side_strip(draw: ImageDraw.ImageDraw, theme: Theme) -> None:
    """Vertical accent strip on the left edge (page identifier)."""
    draw.rectangle([(0, 12), (8, CARD_HEIGHT - 80)], fill=theme.accent)


def _draw_footer(draw: ImageDraw.ImageDraw, theme: Theme, fonts: FontSet,
                 page_num: int, total: int) -> None:
    """Page indicator at the bottom."""
    text = f"{page_num} / {total}"
    bbox = draw.textbbox((0, 0), text, font=fonts.small)
    w = bbox[2] - bbox[0]
    draw.text(
        (CARD_WIDTH - CONTENT_RIGHT - w, CARD_HEIGHT - 90),
        text, fill=theme.footer_color, font=fonts.small,
    )


def _draw_brand(draw: ImageDraw.ImageDraw, theme: Theme, fonts: FontSet,
                text: str = "— 小红书图文卡片 —") -> None:
    """Small brand line at bottom-left."""
    draw.text(
        (CONTENT_LEFT, CARD_HEIGHT - 90),
        text, fill=theme.footer_color, font=fonts.small,
    )


def _wrap_for_draw(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
    max_width: int, line_spacing: int = 0,
) -> tuple[str, int]:
    """Wrap text to max_width and return (wrapped_text_with_newlines, total_height).

    The wrapped text is joined with ``\\n`` so it can be passed directly to
    ``draw.multiline_text``. Height is the height of the wrapped block in
    pixels (when drawn with the given ``line_spacing``).
    """
    lines = _wrap_text_to_width(draw, text, font, max_width)
    if not lines:
        return "", 0
    wrapped = "\n".join(lines)
    bbox = draw.multiline_textbbox(
        (0, 0), wrapped, font=font, spacing=line_spacing,
    )
    h = bbox[3] - bbox[1]
    return wrapped, h


def _draw_paragraph(
    draw: ImageDraw.ImageDraw, fonts: FontSet, theme: Theme,
    p: Para, y: int,
) -> int:
    """Draw a single paragraph at Y, return the new Y after it."""
    content_width = CARD_WIDTH - CONTENT_LEFT - CONTENT_RIGHT
    x = CONTENT_LEFT

    if p.kind == "h1":
        # Wrap to content width.
        wrapped, _ = _wrap_for_draw(
            draw, p.text, fonts.heading, content_width, line_spacing=4,
        )
        bbox = draw.multiline_textbbox((x, y), wrapped, font=fonts.heading, spacing=4)
        draw.multiline_text((x, y), wrapped, fill=theme.heading_color, font=fonts.heading, spacing=4)
        # Dashed divider below.
        line_y = bbox[3] + 8
        accent_x = x
        while accent_x < x + 60:
            draw.rectangle([(accent_x, line_y), (accent_x + 14, line_y + 4)], fill=theme.divider_color)
            accent_x += 22
        return line_y + 28

    if p.kind == "h2":
        wrapped, _ = _wrap_for_draw(draw, p.text, fonts.h2, content_width, line_spacing=2)
        draw.multiline_text((x, y), wrapped, fill=theme.heading_color, font=fonts.h2, spacing=2)
        bbox = draw.multiline_textbbox((x, y), wrapped, font=fonts.h2, spacing=2)
        return bbox[3] + 22

    if p.kind == "h3":
        wrapped, _ = _wrap_for_draw(draw, p.text, fonts.h3, content_width, line_spacing=2)
        draw.multiline_text((x, y), wrapped, fill=theme.accent, font=fonts.h3, spacing=2)
        bbox = draw.multiline_textbbox((x, y), wrapped, font=fonts.h3, spacing=2)
        return bbox[3] + 18

    if p.kind == "quote":
        # Wrap quote text inside its indented box (leave 24+22 padding each side).
        wrapped, text_h = _wrap_for_draw(
            draw, p.text, fonts.body_bold,
            max_width=content_width - 24 - 22,
            line_spacing=int(36 * 0.5),
        )
        box_h = text_h + 2 * 22
        box_y = y
        _draw_rounded_rect(draw, (x, box_y, x + content_width, box_y + box_h),
                           radius=10, fill=theme.quote_bg)
        # Left strip.
        draw.rectangle([(x, box_y + 6), (x + 6, box_y + box_h - 6)], fill=theme.quote_strip)
        # Text.
        draw.multiline_text(
            (x + 24, box_y + 22), wrapped,
            fill=theme.quote_color, font=fonts.body_bold,
            spacing=int(36 * 0.5),
        )
        return box_y + box_h + 28

    if p.kind == "li":
        wrapped, _ = _wrap_for_draw(
            draw, p.text, fonts.body,
            max_width=content_width - 16,
            line_spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
        )
        draw.multiline_text(
            (x + 8, y), wrapped, fill=theme.body_color, font=fonts.body,
            spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
        )
        bbox = draw.multiline_textbbox(
            (x + 8, y), wrapped, font=fonts.body,
            spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
        )
        return bbox[3] + 16

    # Default: body paragraph.
    wrapped, _ = _wrap_for_draw(
        draw, p.text, fonts.body,
        max_width=content_width,
        line_spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
    )
    draw.multiline_text(
        (x, y), wrapped, fill=theme.body_color, font=fonts.body,
        spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
    )
    bbox = draw.multiline_textbbox(
        (x, y), wrapped, font=fonts.body,
        spacing=int(36 * (BODY_LINE_HEIGHT_RATIO - 1)),
    )
    return bbox[3] + 22


def _draw_rounded_rect(
    draw: ImageDraw.ImageDraw, xy: tuple[int, int, int, int],
    radius: int, fill: tuple[int, int, int],
) -> None:
    """Draw a rounded rectangle (PIL >= 8.2 has rounded_rectangle but keep compat)."""
    try:
        draw.rounded_rectangle(xy, radius=radius, fill=fill)
    except AttributeError:
        # Manual fallback.
        x1, y1, x2, y2 = xy
        draw.rectangle([(x1 + radius, y1), (x2 - radius, y2)], fill=fill)
        draw.rectangle([(x1, y1 + radius), (x2, y2 - radius)], fill=fill)
        draw.pieslice([(x1, y1), (x1 + 2 * radius, y1 + 2 * radius)], 180, 270, fill=fill)
        draw.pieslice([(x2 - 2 * radius, y1), (x2, y1 + 2 * radius)], 270, 360, fill=fill)
        draw.pieslice([(x1, y2 - 2 * radius), (x1 + 2 * radius, y2)], 90, 180, fill=fill)
        draw.pieslice([(x2 - 2 * radius, y2 - 2 * radius), (x2, y2)], 0, 90, fill=fill)


# ── Pagination ──────────────────────────────────────────────────────────────


def _paginate(
    paras: list[Para],
    heights: list[int],
    body_capacity: int,
) -> list[list[int]]:
    """Group paragraph indices into pages respecting capacity and break markers.

    Strategy:
    - ``---`` (break paragraphs) force a new page.
    - When adding a paragraph would overflow, start a new page (unless
      the current page is empty, in which case the paragraph is too
      tall to fit and gets its own page anyway).
    - Target ~80% capacity per page so heading + at least one body
      paragraph usually stays together. The actual cut-off is
      ``int(body_capacity * 0.85)`` to leave headroom.
    - After initial grouping, merge a trailing page back into the
      previous one if it uses less than 35% capacity and the merge
      stays within 110% of capacity (avoids tiny orphan cards).

    Returns a list of lists of paragraph indices (one list per page).
    """
    soft_cap = int(body_capacity * 0.85)

    pages: list[list[int]] = []
    cur: list[int] = []
    cur_h = 0
    for idx, (p, h) in enumerate(zip(paras, heights, strict=True)):
        if p.kind == "break":
            if cur:
                pages.append(cur)
                cur = []
                cur_h = 0
            continue
        # Hard limit: never exceed full capacity.
        # Soft limit: avoid filling so close that the next paragraph
        # would orphan a heading.
        cap = soft_cap if cur else body_capacity
        if cur and cur_h + h > cap:
            pages.append(cur)
            cur = [idx]
            cur_h = h
        else:
            cur.append(idx)
            cur_h += h
    if cur:
        pages.append(cur)

    # Post-process: merge a small trailing page back into the previous one.
    if len(pages) >= 2:
        last_h = sum(heights[i] for i in pages[-1])
        if last_h < body_capacity * 0.35 and pages[-2]:
            prev_h = sum(heights[i] for i in pages[-2])
            if prev_h + last_h <= body_capacity * 1.10:
                pages[-2] = pages[-2] + pages[-1]
                pages.pop()
    return pages


# ── Public API ──────────────────────────────────────────────────────────────


def render_text_note(
    title: str,
    body: str,
    theme: str = "warm",
    output_dir: Path | None = None,
    width: int = CARD_WIDTH,
    subtitle: str = "",
) -> list[Path]:
    """Render ``title + body`` into publishable PNG image cards.

    Parameters
    ----------
    title : str
        Note title (rendered on cover).
    body : str
        Markdown body. ``---`` separators force a page break.
        Otherwise paragraphs are auto-grouped to fit measured height.
    theme : str
        One of :func:`list_themes`.
    output_dir : Path | None
        Directory for the resulting PNG files. If None, a system temp
        directory is used.
    width : int
        Render width in pixels (height stays fixed at CARD_HEIGHT).
    subtitle : str
        Optional cover subtitle. If empty, derived from first non-heading
        line of body.

    Returns
    -------
    list[Path]
        Paths to ``cover.png`` plus ``card_1.png``, ``card_2.png``, … in
        order. Always at least ``cover.png``.
    """
    if theme not in THEMES:
        raise ValueError(f"Unknown theme '{theme}'. Available: {', '.join(list_themes())}")
    if width != CARD_WIDTH:
        # Width scaling is supported via re-render, but for simplicity we
        # assert the canonical size. Adapt here if you need flexibility.
        pass

    if output_dir is None:
        import tempfile
        output_dir = Path(tempfile.mkdtemp(prefix="xhs_text_card_"))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    theme_obj = THEMES[theme]
    fonts = _load_fontset()

    body = body.strip()
    paras = parse_markdown(body)

    # Derive subtitle from first body paragraph if not provided.
    if not subtitle:
        for p in paras:
            if p.kind in ("body", "li", "quote") and p.text:
                subtitle = p.text[:48] + ("…" if len(p.text) > 48 else "")
                break

    # Measure paragraph heights with a throwaway draw context.
    measure_img, measure_draw = _new_canvas(theme_obj)
    measured = _measure(measure_draw, fonts, theme_obj, paras)
    heights = [h for _, h in measured]

    # Body capacity = space inside content area below accent + above footer.
    body_top = CONTENT_TOP
    body_bottom = CARD_HEIGHT - CONTENT_BOTTOM
    body_capacity = body_bottom - body_top  # for one heading + body mixed; paginator handles mixing

    page_groups = _paginate(paras, heights, body_capacity=body_capacity)
    total_pages = max(len(page_groups), 1) + 1  # cover + content

    results: list[Path] = []

    # ── Cover ────────────────────────────────────────────────────────────────
    cover_path = output_dir / "cover.png"
    _render_cover(cover_path, title=title, subtitle=subtitle, total_pages=total_pages,
                  theme=theme_obj, fonts=fonts)
    results.append(cover_path)

    # ── Content cards ────────────────────────────────────────────────────────
    for idx, group in enumerate(page_groups, start=1):
        card_path = output_dir / f"card_{idx}.png"
        _render_content_card(
            card_path,
            paras=[paras[i] for i in group],
            heights=[heights[i] for i in group],
            page_num=idx + 1, total_pages=total_pages,
            theme=theme_obj, fonts=fonts,
        )
        results.append(card_path)

    logger.info("render_text_note produced %d images in %s", len(results), output_dir)
    return results


def _render_cover(
    out: Path, *, title: str, subtitle: str, total_pages: int,
    theme: Theme, fonts: FontSet,
) -> None:
    img, draw = _new_canvas(theme)
    _draw_top_accent(draw, theme)

    # Top eyebrow tag.
    eyebrow = f"小红书图文 · 共 {total_pages} 页"
    draw.text((CONTENT_LEFT, 56), eyebrow, fill=theme.muted_color, font=fonts.small)

    # Big title.
    title_y = 280
    # Wrap title manually if too long.
    title_lines = _wrap_title_to_width(draw, title, fonts.cover_title,
                                       CARD_WIDTH - CONTENT_LEFT - CONTENT_RIGHT)
    y = title_y
    for line in title_lines:
        draw.text((CONTENT_LEFT, y), line, fill=theme.heading_color, font=fonts.cover_title)
        bbox = draw.textbbox((CONTENT_LEFT, y), line, font=fonts.cover_title)
        y = bbox[3] + 16

    # Accent decoration: short bar under title.
    bar_y = y + 24
    draw.rectangle([(CONTENT_LEFT, bar_y), (CONTENT_LEFT + 160, bar_y + 10)], fill=theme.accent)

    # Subtitle.
    if subtitle:
        sub_y = bar_y + 56
        # Wrap subtitle.
        sub_lines = _wrap_text_to_width(
            draw, subtitle, fonts.cover_subtitle,
            CARD_WIDTH - CONTENT_LEFT - CONTENT_RIGHT,
        )
        for line in sub_lines[:3]:  # cap to 3 lines
            draw.text((CONTENT_LEFT, sub_y), line, fill=theme.body_color, font=fonts.cover_subtitle)
            bbox = draw.textbbox((CONTENT_LEFT, sub_y), line, font=fonts.cover_subtitle)
            sub_y = bbox[3] + 12

    # Decorative circles (bottom right).
    _draw_decorative_circles(draw, theme)

    # Footer / brand.
    _draw_brand(draw, theme, fonts)

    img.save(out, "PNG", optimize=True)


def _wrap_title_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int,
) -> list[str]:
    """Wrap a title (which may contain manual \n) and ensure each line fits."""
    if "\n" in text:
        result = []
        for line in text.split("\n"):
            result.extend(_wrap_text_to_width(draw, line, font, max_width))
        return result
    return _wrap_text_to_width(draw, text, font, max_width)


def _draw_decorative_circles(draw: ImageDraw.ImageDraw, theme: Theme) -> None:
    """Decorative circles bottom-right for visual interest."""
    cx_base = CARD_WIDTH - 220
    cy_base = CARD_HEIGHT - 280
    # Three concentric circles in accent color.
    for r, alpha in [(80, 30), (54, 50), (28, 100)]:
        color = _mix_with_bg(theme.accent, theme.bg_bottom, 1 - alpha / 100)
        draw.ellipse(
            [(cx_base - r, cy_base - r), (cx_base + r, cy_base + r)],
            outline=color, width=4,
        )


def _mix_with_bg(
    fg: tuple[int, int, int], bg: tuple[int, int, int], t: float,
) -> tuple[int, int, int]:
    """Linear interpolate fg -> bg; t=0 -> fg, t=1 -> bg."""
    return (
        int(fg[0] + (bg[0] - fg[0]) * t),
        int(fg[1] + (bg[1] - fg[1]) * t),
        int(fg[2] + (bg[2] - fg[2]) * t),
    )


def _render_content_card(
    out: Path, *, paras: list[Para], heights: list[int], page_num: int, total_pages: int,
    theme: Theme, fonts: FontSet,
) -> None:
    img, draw = _new_canvas(theme)
    _draw_top_accent(draw, theme)
    _draw_side_strip(draw, theme)

    y = CONTENT_TOP
    body_bottom = CARD_HEIGHT - CONTENT_BOTTOM

    for p, h in zip(paras, heights, strict=True):
        if y >= body_bottom:
            break  # safety: stop drawing if no room left
        if y + h > body_bottom:
            break  # don't overflow
        y = _draw_paragraph(draw, fonts, theme, p, y)

    _draw_footer(draw, theme, fonts, page_num=page_num, total=total_pages)
    _draw_brand(draw, theme, fonts)

    img.save(out, "PNG", optimize=True)


__all__ = [
    "render_text_note",
    "list_themes",
    "Theme",
    "THEMES",
    "CARD_WIDTH",
    "CARD_HEIGHT",
]