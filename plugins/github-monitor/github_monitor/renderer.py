"""Fixed-size local PNG renderer for Push events."""

from __future__ import annotations

import io
from collections.abc import Iterable
from zoneinfo import ZoneInfo

from .models import NormalizedGitHubEvent

WIDTH = 1200
HEIGHT = 630

BACKGROUND = "#080C12"
CARD = "#0F141B"
SURFACE = "#151C25"
SURFACE_HOVER = "#19222D"
BORDER = "#303A46"
TEXT = "#F0F6FC"
TEXT_MUTED = "#8B98A7"
BLUE = "#58A6FF"
PURPLE = "#BC8CFF"
GREEN = "#3FB950"
RED = "#F85149"


def render_push_card(event: NormalizedGitHubEvent) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    label_font = _font(ImageFont, 19, bold=True)
    title_font = _font(ImageFont, 38, bold=True)
    actor_font = _font(ImageFont, 22)
    metric_label_font = _font(ImageFont, 18)
    metric_value_font = _font(ImageFont, 30, bold=True)
    section_font = _font(ImageFont, 21, bold=True)
    commit_font = _font(ImageFont, 20)
    sha_font = _font(ImageFont, 18, bold=True)
    footer_font = _font(ImageFont, 17)

    draw.rounded_rectangle((28, 28, 1172, 602), 28, fill=CARD, outline=BORDER, width=2)
    draw.rounded_rectangle((44, 44, 1156, 170), 20, fill=SURFACE)
    draw.rounded_rectangle((44, 44, 50, 170), 3, fill=GREEN)

    _draw_pill(draw, (76, 64), "PUSH", label_font, GREEN, "#07130B", padding=(13, 7))
    actor = _fit_width(draw, event.actor, actor_font, 310)
    draw.text((164, 68), f"{actor} 推送了代码", font=actor_font, fill=TEXT_MUTED)

    occurred = event.created_at.astimezone(ZoneInfo("Asia/Shanghai"))
    time_text = occurred.strftime("%Y-%m-%d  %H:%M")
    time_width = draw.textlength(time_text, font=footer_font)
    draw.text((1124 - time_width, 70), time_text, font=footer_font, fill=TEXT_MUTED)

    repository = _fit_width(draw, event.repository, title_font, 760)
    draw.text((76, 108), repository, font=title_font, fill=TEXT)
    branch = _fit_width(draw, event.branch or "default", label_font, 210)
    _draw_pill(
        draw,
        (936, 113),
        branch,
        label_font,
        BORDER,
        TEXT,
        padding=(14, 7),
        min_width=188,
    )

    metrics = (
        (
            "提交",
            _metric(event.payload.get("total_commits", event.payload.get("distinct_size", 0))),
            BLUE,
        ),
        ("文件", _metric(event.payload.get("files_changed", 0)), PURPLE),
        ("新增", f"+{_metric(event.payload.get('additions', 0))}", GREEN),
        ("删除", f"−{_metric(event.payload.get('deletions', 0))}", RED),
    )
    metric_y = 190
    metric_width = 259
    for index, (label, value, accent) in enumerate(metrics):
        x = 52 + index * 274
        draw.rounded_rectangle(
            (x, metric_y, x + metric_width, metric_y + 88),
            14,
            fill=SURFACE,
            outline="#25303C",
            width=1,
        )
        draw.rounded_rectangle((x + 16, metric_y + 18, x + 21, metric_y + 70), 2, fill=accent)
        draw.text((x + 38, metric_y + 17), label, font=metric_label_font, fill=TEXT_MUTED)
        draw.text((x + 38, metric_y + 42), value, font=metric_value_font, fill=accent)

    draw.text((54, 307), "提交记录", font=section_font, fill=TEXT)
    rows = event.payload.get("commits")
    commit_rows = _mapping_rows(rows)
    visible_rows = commit_rows[:4]
    if not visible_rows:
        draw.rounded_rectangle((52, 344, 1148, 402), 12, fill=SURFACE)
        draw.text((76, 360), "本次推送没有可展示的提交摘要", font=commit_font, fill=TEXT_MUTED)
    else:
        for index, row in enumerate(visible_rows):
            y = 344 + index * 54
            draw.rounded_rectangle((52, y, 1148, y + 46), 11, fill=SURFACE_HOVER)
            sha = str(row.get("sha", ""))[:7] or "unknown"
            _draw_pill(
                draw,
                (68, y + 8),
                sha,
                sha_font,
                "#2D2442",
                PURPLE,
                padding=(11, 4),
                min_width=112,
            )
            message = str(row.get("message", "")).splitlines()[0].strip() or "无提交说明"
            message = _fit_width(draw, message, commit_font, 880)
            draw.text((202, y + 10), message, font=commit_font, fill=TEXT)

    footer_y = 569
    draw.line((52, 552, 1148, 552), fill="#25303C", width=1)
    draw.text((54, footer_y), "GitHub Monitor · 实时事件", font=footer_font, fill=TEXT_MUTED)
    footer_right = f"北京时间 · {occurred.strftime('%H:%M:%S')}"
    if len(commit_rows) > len(visible_rows):
        footer_right = f"另有 {len(commit_rows) - len(visible_rows)} 条提交 · {footer_right}"
    footer_width = draw.textlength(footer_right, font=footer_font)
    draw.text((1146 - footer_width, footer_y), footer_right, font=footer_font, fill=TEXT_MUTED)

    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _font(module: object, size: int, *, bold: bool = False) -> object:
    regular = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/Deng.ttf",
    )
    bold_fonts = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/Dengb.ttf",
    )
    for path in (*bold_fonts, *regular) if bold else regular:
        try:
            return module.truetype(path, size=size)  # type: ignore[attr-defined]
        except OSError:
            continue
    return module.load_default()  # type: ignore[attr-defined,no-any-return]


def _draw_pill(
    draw: object,
    position: tuple[int, int],
    text: str,
    font: object,
    background: str,
    foreground: str,
    *,
    padding: tuple[int, int],
    min_width: int = 0,
) -> None:
    x, y = position
    text_width = int(draw.textlength(text, font=font))  # type: ignore[attr-defined]
    text_box = draw.textbbox((0, 0), text, font=font)  # type: ignore[attr-defined]
    text_height = text_box[3] - text_box[1]
    width = max(min_width, text_width + padding[0] * 2)
    height = text_height + padding[1] * 2
    draw.rounded_rectangle(  # type: ignore[attr-defined]
        (x, y, x + width, y + height),
        height // 2,
        fill=background,
    )
    draw.text(  # type: ignore[attr-defined]
        (x + (width - text_width) / 2, y + padding[1] - text_box[1]),
        text,
        font=font,
        fill=foreground,
    )


def _fit_width(draw: object, value: str, font: object, max_width: int) -> str:
    text = value.strip().replace("\n", " ")
    if draw.textlength(text, font=font) <= max_width:  # type: ignore[attr-defined]
        return text
    suffix = "…"
    while text and draw.textlength(text + suffix, font=font) > max_width:  # type: ignore[attr-defined]
        text = text[:-1]
    return f"{text}{suffix}"


def _metric(value: object) -> str:
    if isinstance(value, bool):
        return "0"
    if isinstance(value, int | float | str):
        return str(value)
    return "0"


def _mapping_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | dict):
        return []
    return [item for item in value if isinstance(item, dict)]
