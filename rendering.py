from __future__ import annotations

import asyncio
import json
import math
import re
import tempfile
import unicodedata
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .domain import StudioError, identify
from .templates import content_html, overview_html

PAGINATE_CONTENT = r"""
async ({height, maxPages}) => {
    await document.fonts.ready;
    const source = document.querySelector('[data-sheet="content"]');
    const flow = source.querySelector('.flow');
    const availableHeight = () => Math.floor(height - (source.getBoundingClientRect().height - flow.getBoundingClientRect().height));
    const firstCapacity = availableHeight();
    source.classList.add('continuation');
    const continuationCapacity = availableHeight();
    source.classList.remove('continuation');
    const bounds = flow.getBoundingClientRect();
    if (firstCapacity <= 0 || continuationCapacity <= 0) return {error: '页面标题与边距超过分页高度，请增加分页高度。'};
    const protectedRanges = [];
    for (const element of flow.querySelectorAll('[data-text]')) {
        const range = document.createRange();
        range.selectNodeContents(element);
        for (const rect of range.getClientRects()) {
            if (rect.height > 0) protectedRanges.push({top: rect.top - bounds.top, bottom: rect.bottom - bounds.top, width: rect.width});
        }
    }
    for (const heading of flow.querySelectorAll('[data-heading]')) {
        const title = heading.getBoundingClientRect();
        const text = heading.parentElement.querySelector('[data-text]');
        const range = document.createRange();
        if (text) range.selectNodeContents(text);
        const firstLine = text ? range.getClientRects()[0] : null;
        protectedRanges.push({top: title.top - bounds.top, bottom: Math.max(title.bottom, firstLine ? firstLine.bottom : title.bottom) - bounds.top});
    }
    for (const heading of flow.querySelectorAll('[data-prompt-heading]')) {
        const title = heading.getBoundingClientRect();
        const following = protectedRanges.filter(rect => rect.width > 0 && rect.top >= title.bottom - bounds.top - 1).sort((first, second) => first.top - second.top)[0];
        protectedRanges.push({top: title.top - bounds.top, bottom: Math.max(title.bottom - bounds.top, following ? following.bottom : 0)});
    }
    const spans = [];
    const totalHeight = Math.ceil(bounds.height);
    let start = 0;
    while (start < totalHeight) {
        const capacity = spans.length === 0 ? firstCapacity : continuationCapacity;
        let end = Math.min(start + capacity, totalHeight);
        if (end < totalHeight) {
            while (true) {
                const crossing = protectedRanges.filter(rect => rect.top < end && rect.bottom > end);
                if (!crossing.length) break;
                end = Math.floor(Math.min(...crossing.map(rect => rect.top)));
                if (end <= start) return {error: '页面空间不足以完整显示文字行，请增加分页高度。'};
            }
        }
        spans.push({start, end});
        if (spans.length > maxPages) return {error: '正文页数超过上限；未发送截断图片。'};
        start = end;
    }
    const sheets = spans.map((span, index) => {
        const sheet = source.cloneNode(true);
        if (index > 0) {
            sheet.classList.add('continuation');
            sheet.querySelector('.persona-header').remove();
        }
        sheet.querySelector('.flow-window').style.height = (span.end - span.start) + 'px';
        sheet.querySelector('.flow').style.transform = 'translateY(-' + span.start + 'px)';
        sheet.querySelector('[data-page-number]').textContent = '正文 · ' + (index + 1) + ' / ' + spans.length;
        return sheet;
    });
    document.body.replaceChildren(...sheets);
    return {
        width: document.documentElement.scrollWidth,
        pages: sheets.map(sheet => {
            const rect = sheet.getBoundingClientRect();
            return {top: rect.top, height: Math.ceil(rect.height)};
        })
    };
}
"""


def markdown_filename(result: dict) -> str:
    snapshot = result.get("snapshot")
    revision = snapshot["revision"] if snapshot else result["revision"]
    kind = "历史" if snapshot else "当前"
    prefix = "人格_"
    suffix = f"_{kind}_v{revision}.md"
    name = "".join(
        "_"
        if character in '<>:"/\\|?*'
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        else character
        for character in result["persona"]["persona_id"]
    )
    byte_limit = 200 - len((prefix + suffix).encode("utf-8"))
    name = (
        name.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore").strip(" .")
    )
    return f"{prefix}{name or '未命名'}{suffix}"


def detail_markdown(result: dict) -> str:
    content = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    fence = "\u0060" * max(
        3,
        1
        + max(
            (len(match.group()) for match in re.finditer(r"\u0060+", content)),
            default=0,
        ),
    )
    return f"# 人格详情\n\n完整人格、版本与权限元信息：\n\n{fence}json\n{content}\n{fence}\n"


async def render_pages(context, result: dict, settings) -> list[bytes]:
    registered = context.get_registered_star("astrbot_plugin_browser")
    service = getattr(getattr(registered, "star_cls", None), "service", None)
    if (
        registered is None
        or not registered.activated
        or service is None
        or not service.ready
    ):
        raise StudioError(
            "browser_unavailable",
            "详情图片需要已启用且就绪的 astrbot_plugin_browser，不会自动改发全文。",
        )

    async def render():
        images = []
        async with service.session(
            viewport={
                "width": settings.render_width,
                "height": settings.render_page_height,
            },
            javascript_enabled=False,
            timeout=settings.render_timeout,
        ) as page:
            await page.set_content(
                overview_html(result, settings.render_width), wait_until="load"
            )
            overview = await page.evaluate(
                "async () => { await document.fonts.ready; return {width: document.documentElement.scrollWidth, height: Math.ceil(document.querySelector('.sheet').getBoundingClientRect().height)}; }"
            )
            width, height = overview["width"], overview["height"]
            if (
                not isinstance(width, int | float)
                or not isinstance(height, int | float)
                or not math.isfinite(width)
                or not math.isfinite(height)
                or width > settings.render_width
                or width <= 0
                or height <= 0
                or settings.render_width * height > settings.render_max_pixels
            ):
                raise StudioError(
                    "render_limit",
                    "人格档案尺寸无效或超出单图像素上限；档案不拆页、不截断。",
                )
            image = await page.screenshot(
                type="png",
                animations="disabled",
                full_page=True,
                clip={"x": 0, "y": 0, "width": settings.render_width, "height": height},
                timeout=settings.render_timeout * 1000,
            )
            if not isinstance(image, bytes) or not image.startswith(
                b"\x89PNG\r\n\x1a\n"
            ):
                raise StudioError("render_failed", "浏览器没有返回有效 PNG。")
            images.append(image)
            await page.set_content(
                content_html(result, settings.render_width), wait_until="load"
            )
            layout = await page.evaluate(
                PAGINATE_CONTENT,
                {
                    "height": settings.render_page_height,
                    "maxPages": settings.render_max_pages - 1,
                },
            )
            if "error" in layout:
                raise StudioError("render_limit", layout["error"])
            width, pages = layout["width"], layout["pages"]
            if (
                not isinstance(width, int | float)
                or not math.isfinite(width)
                or not 0 < width <= settings.render_width
                or not pages
                or len(pages) + 1 > settings.render_max_pages
            ):
                raise StudioError(
                    "render_limit", "正文尺寸或总页数超出上限，不截断内容。"
                )
            for bounds in pages:
                top, page_height = bounds["top"], bounds["height"]
                if (
                    not isinstance(top, int | float)
                    or not isinstance(page_height, int | float)
                    or not math.isfinite(top)
                    or not math.isfinite(page_height)
                    or top < 0
                    or not 0 < page_height <= settings.render_page_height
                    or settings.render_width * page_height > settings.render_max_pixels
                ):
                    raise StudioError(
                        "render_limit", "正文分页尺寸无效或超过像素上限。"
                    )
                image = await page.screenshot(
                    type="png",
                    animations="disabled",
                    full_page=True,
                    clip={
                        "x": 0,
                        "y": top,
                        "width": settings.render_width,
                        "height": page_height,
                    },
                    timeout=settings.render_timeout * 1000,
                )
                if not isinstance(image, bytes) or not image.startswith(
                    b"\x89PNG\r\n\x1a\n"
                ):
                    raise StudioError("render_failed", "浏览器没有返回有效 PNG。")
                images.append(image)
        return images

    try:
        return await asyncio.wait_for(render(), timeout=settings.render_timeout)
    except StudioError:
        raise
    except asyncio.TimeoutError as exc:
        raise StudioError(
            "render_timeout", "人格详情渲染超时，没有自动重试或改发全文。"
        ) from exc
    except Exception as exc:
        raise StudioError(
            "render_failed", "人格详情图片渲染失败，没有自动重试或改发全文。"
        ) from exc


def delivery_actor(studio, event, result):
    actor = identify(event, studio.settings())
    if not actor.admin and result.get("owner") != actor.owner:
        raise StudioError("not_found_or_forbidden", "发送前权限已改变，停止发送详情。")
    return actor


async def present(studio, event, response: dict, *, include_md: bool) -> dict:
    result = response["data"]
    submitted = 0
    file_submitted = False
    path = None
    sending = False
    outcome = None
    try:
        delivery_actor(studio, event, result)
        images = await render_pages(studio.context, result, studio.settings())
        if include_md:
            directory = Path(get_astrbot_temp_path()) / "astrbot_plugin_persona_studio"
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".md",
                prefix="persona_",
                dir=directory,
                delete=False,
            ) as stream:
                path = Path(stream.name)
                stream.write(detail_markdown(result))
        messages = [MessageChain([Image.fromBytes(image)]) for image in images]
        if path is not None:
            name = markdown_filename(result)
            messages.append(MessageChain([File(name=name, file=str(path))]))
        for index, message in enumerate(messages):
            delivery_actor(studio, event, result)
            sending = True
            await event.send(message)
            sending = False
            if index < len(images):
                submitted += 1
            else:
                file_submitted = True
        delivery_actor(studio, event, result)
        response["delivery"] = {
            "images_submitted": submitted,
            "md_submitted": file_submitted,
            "status": "submitted_to_platform",
        }
        outcome = response
    except Exception as exc:
        error = (
            exc
            if isinstance(exc, StudioError)
            else StudioError(
                "delivery_failed",
                "详情消息发送失败，可能已有部分消息送达，请勿自动重试。",
            )
        )
        accessible = True
        try:
            delivery_actor(studio, event, result)
        except StudioError as permission_error:
            accessible = False
            error = permission_error
        outcome = {
            "ok": False,
            "error": {
                "code": error.code,
                "message": str(error),
                "details": error.details,
            },
            "delivery": {
                "images_submitted": submitted,
                "md_submitted": file_submitted,
                "failed_message_may_have_been_delivered": sending,
            },
            "retry_safe": False,
        }
        if accessible:
            outcome["data"] = result
    finally:
        if path is not None:
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                if outcome is not None:
                    outcome["ok"] = False
                    outcome["error"] = {
                        "code": "temporary_cleanup_failed",
                        "message": "消息处理结束，但临时文件清理失败，请管理员检查临时目录，不要重试发送。",
                    }
                    outcome["retry_safe"] = False
    return outcome
