from __future__ import annotations

import asyncio
import html
import json
import math
import re
import tempfile
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from .domain import StudioError, digest, identify


def detail_text(result: dict) -> str:
    persona = result["persona"]
    metadata = {
        name: value
        for name, value in result.items()
        if name not in {"persona", "snapshot", "expected_revision", "chain_id"}
    }
    fields = {
        name: value
        for name, value in persona.items()
        if name not in {"system_prompt", "begin_dialogs", "custom_error_message"}
    }
    lines = [
        "人格捏造 · 人格详情",
        "",
        "【版本与权限】",
        json.dumps(metadata, ensure_ascii=False),
        "",
        "【人格元信息与能力】",
        json.dumps(fields, ensure_ascii=False, indent=2),
        "",
        "【系统提示词】",
        persona["system_prompt"],
        "",
        "【预设对话】",
    ]
    for index, dialog in enumerate(persona.get("begin_dialogs") or []):
        lines.extend(
            [f"{index + 1}. {'用户' if index % 2 == 0 else '助手'}", dialog, ""]
        )
    lines.extend(
        [
            "【自定义错误回复】",
            persona.get("custom_error_message")
            if persona.get("custom_error_message") is not None
            else "null",
        ]
    )
    if "snapshot" in result:
        snapshot = {
            name: value
            for name, value in result["snapshot"].items()
            if name != "persona"
        }
        lines.extend(["", "【备份元信息】", json.dumps(snapshot, ensure_ascii=False)])
    return "\n".join(lines)


def detail_html(result: dict, width: int) -> str:
    content = html.escape(detail_text(result), quote=True)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<style>html,body{margin:0;background:#f5f2ec;color:#20252c;}"
        f"body{{width:{width}px;}}"
        "pre{box-sizing:border-box;margin:0;padding:30px;white-space:pre-wrap;"
        "overflow-wrap:anywhere;word-break:break-word;font-size:20px;line-height:30px;"
        'font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif;}'
        "</style></head><body><pre>" + content + "</pre></body></html>"
    )


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
                detail_html(result, settings.render_width), wait_until="load"
            )
            dimensions = await page.evaluate(
                "() => ({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})"
            )
            width, height = dimensions["width"], dimensions["height"]
            if (
                not isinstance(width, int | float)
                or not isinstance(height, int | float)
                or not math.isfinite(width)
                or not math.isfinite(height)
                or width > settings.render_width
                or height <= 0
            ):
                raise StudioError(
                    "render_limit", "渲染尺寸无效或存在横向溢出，不截断内容。"
                )
            total = math.ceil(height / settings.render_page_height)
            if total > settings.render_max_pages:
                raise StudioError(
                    "render_limit",
                    f"完整详情需要 {total} 页，超过上限 {settings.render_max_pages}；未发送截断图片。",
                )
            for index in range(total):
                top = index * settings.render_page_height
                page_height = min(settings.render_page_height, height - top)
                if width * page_height > settings.render_max_pixels:
                    raise StudioError("render_limit", "渲染单页超过像素上限。")
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
            name = f"persona_{digest(result['persona']['persona_id'])[:12]}.md"
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
