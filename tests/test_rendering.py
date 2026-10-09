import asyncio
import io
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_studio import rendering
from astrbot_plugin_persona_studio.domain import Settings, StudioError

PNG = b"\x89PNG\r\n\x1a\nimage"


def fake_browser(env, *, height=3000, ready=True):
    page = SimpleNamespace(
        set_content=AsyncMock(),
        evaluate=AsyncMock(return_value={"width": 1000, "height": height}),
        screenshot=AsyncMock(return_value=PNG),
    )
    calls = []

    @asynccontextmanager
    async def session(**kwargs):
        calls.append(kwargs)
        yield page

    service = SimpleNamespace(ready=ready, session=session)
    env.context.get_registered_star = lambda name: SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(service=service)
    )
    return page, calls


async def detail(env):
    response = await env.studio.execute(
        env.event, "manage", "get", {"persona_id": "alice"}
    )
    assert response["ok"]
    return response


def test_html_escapes_content_and_md_keeps_it():
    result = {
        "persona": {
            "persona_id": "name",
            "system_prompt": '<script>alert("bad")</script>\n'
            + "\u0060" * 3
            + "unsafe",
            "begin_dialogs": ["user", "assistant"],
            "tools": None,
            "skills": [],
        },
        "owner": ["platform", "user"],
    }
    document = rendering.detail_html(result, 1000)
    assert "<script>" not in document and "&lt;script&gt;" in document
    assert "default-src" in document and "src=" not in document
    markdown = rendering.detail_markdown(result)
    assert "\u0060" * 4 + "json" in markdown and "\u0060" * 3 + "unsafe" in markdown


async def test_paginated_images_and_optional_md(created, tmp_path, monkeypatch):
    page, calls = fake_browser(created)
    monkeypatch.setattr(rendering, "get_astrbot_temp_path", lambda: str(tmp_path))
    captured = []

    async def send(message):
        component = message.chain[0]
        if component.type.value == "File":
            path = await component.get_file()
            captured.append(
                await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
            )

    created.event.send = AsyncMock(side_effect=send)
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=True
    )
    assert response["ok"], response
    assert response["delivery"]["images_submitted"] == 2
    assert response["delivery"]["md_submitted"]
    assert captured and "版本1" in captured[0]
    assert not list(tmp_path.rglob("*.md"))
    assert calls[0]["javascript_enabled"] is False
    assert page.screenshot.call_args_list[1].kwargs["clip"]["y"] == 1500


async def test_browser_missing_does_not_send_text(created):
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=False
    )
    assert response["error"]["code"] == "browser_unavailable"
    assert response["data"]["persona"]["system_prompt"] == "版本1"
    created.event.send.assert_not_called()


async def test_page_limit_rejects_without_partial_images(created):
    page, _ = fake_browser(created, height=100000)
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=False
    )
    assert response["error"]["code"] == "render_limit"
    created.event.send.assert_not_called()
    page.screenshot.assert_not_called()


async def test_send_failure_reports_partial_without_retry(created):
    fake_browser(created)
    created.event.send.side_effect = [None, RuntimeError("platform failed")]
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=False
    )
    assert not response["ok"] and not response["retry_safe"]
    assert response["delivery"]["images_submitted"] == 1
    assert response["delivery"]["failed_message_may_have_been_delivered"]
    assert created.event.send.await_count == 2


async def test_timeout_does_not_retry(created):
    page, _ = fake_browser(created)
    page.set_content.side_effect = asyncio.TimeoutError
    with pytest.raises(StudioError, match="超时"):
        await rendering.render_pages(
            created.context, (await detail(created))["data"], Settings()
        )
    page.set_content.assert_awaited_once()


async def test_revocation_while_rendering_does_not_return_raw_data(created):
    page, _ = fake_browser(created)

    async def revoke(*args, **kwargs):
        created.config["whitelist"] = []
        return PNG

    page.screenshot.side_effect = revoke
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=False
    )
    assert response["error"]["code"] == "forbidden"
    assert "data" not in response
    created.event.send.assert_not_called()


async def test_real_browser_renders_long_offline_content(created, tmp_path):
    from astrbot_plugin_browser.service import BrowserService, find_system_chromium
    from PIL import Image

    executable = find_system_chromium()
    if executable is None:
        pytest.skip("No installed Chromium browser for integration test")
    service = BrowserService(browser_executable=str(executable))
    await service.initialize()
    created.context.get_registered_star = lambda name: SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(service=service)
    )
    try:
        response = await detail(created)
        response["data"]["persona"]["system_prompt"] = "\n".join(
            f"第 {index + 1} 行：完整人格提示词，保留换行与中文。"
            for index in range(80)
        )
        images = await rendering.render_pages(
            created.context, response["data"], Settings()
        )
        assert len(images) >= 2
        for index, image in enumerate(images):
            with Image.open(io.BytesIO(image)) as picture:
                assert picture.width == 1000 and 0 < picture.height <= 1500
            (tmp_path / f"preview-{index + 1}.png").write_bytes(image)
    finally:
        await service.close()
