import asyncio
import html
import io
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image
from astrbot_plugin_persona_studio import rendering
from astrbot_plugin_persona_studio.domain import Settings, StudioError
from astrbot_plugin_persona_studio.main import PersonaStudioPlugin
from astrbot_plugin_persona_studio.templates import format_time, prompt_html

PNG = b"\x89PNG\r\n\x1a\nimage"


def fake_browser(env, *, height=3000, overview_height=1200, ready=True):
    page = SimpleNamespace(
        set_content=AsyncMock(),
        evaluate=AsyncMock(
            side_effect=[
                {"width": 1000, "height": overview_height},
                {
                    "width": 1000,
                    "pages": [
                        {"top": top, "height": min(1500, height - top)}
                        for top in range(0, height, 1500)
                    ],
                },
            ]
        ),
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


@pytest.mark.parametrize(
    "persona_id, expected",
    [
        ("澪", "人格_澪_当前_v3.md"),
        ("Mio 春日", "人格_Mio 春日_当前_v3.md"),
        (
            '澪/测试\\副本:一?二*三"四<五>六|七',
            "人格_澪_测试_副本_一_二_三_四_五_六_七_当前_v3.md",
        ),
        ("澪\x00测试\n副本\x7f", "人格_澪_测试_副本__当前_v3.md"),
        ("CON", "人格_CON_当前_v3.md"),
        ("...", "人格_未命名_当前_v3.md"),
        ("澪\u202e.md", "人格_澪_.md_当前_v3.md"),
    ],
)
def test_markdown_filename_is_readable_and_safe(persona_id, expected):
    result = {"persona": {"persona_id": persona_id}, "revision": 3}
    assert rendering.markdown_filename(result) == expected


@pytest.mark.parametrize("persona_id", ["澪" * 255, "A" * 255, "澪😊" * 100])
def test_markdown_filename_limits_bytes_without_breaking_unicode(persona_id):
    result = {"persona": {"persona_id": persona_id}, "revision": 3}
    name = rendering.markdown_filename(result)
    assert len(name.encode("utf-8")) <= 200
    assert name.startswith("人格_") and name.endswith("_当前_v3.md")
    assert "\ufffd" not in name


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
    document = rendering.content_html(result, 1000)
    assert "<script>" not in document and "&lt;script&gt;" in document
    assert "default-src" in document and "src=" not in document
    markdown = rendering.detail_markdown(result)
    assert "\u0060" * 4 + "json" in markdown and "\u0060" * 3 + "unsafe" in markdown


async def test_overview_uses_readable_metadata_without_prompt(created):
    result = (await detail(created))["data"]
    result["persona"]["tools"] = None
    document = rendering.overview_html(result, 1000)
    assert "版本与权限" in document and "人格信息与能力" in document
    assert "归属用户" in document and "平台实例" in document
    assert "创建时间" in document and "更新时间" in document
    assert "全部工具" in document and "已禁用" in document
    assert "版本1" not in document and "系统提示词" not in document
    assert "backup_summary" not in document and "created_at" not in document
    assert "null" not in document


@pytest.mark.parametrize("dialogs", [None, []])
async def test_empty_dialogs_have_no_heading(created, dialogs):
    result = (await detail(created))["data"]
    result["persona"]["begin_dialogs"] = dialogs
    document = rendering.content_html(result, 1000)
    assert "预设对话" not in document
    assert "自定义错误回复" not in document and "null" not in document
    assert "系统提示词" in document and "版本1" in document


async def test_content_groups_prompt_and_dialogs(created):
    document = rendering.content_html((await detail(created))["data"], 1000)
    assert document.index("系统提示词") < document.index("预设对话")
    assert "第 1 轮 · 用户" in document and "第 1 轮 · 助手" in document
    assert "你好" in document and "你好呀" in document
    assert "归属用户" not in document and "人格信息与能力" not in document


def test_prompt_heading_style_preserves_raw_text():
    prompt = "开头\n\n【说话风格】\n <script>不要执行</script>\n\n结尾\n"
    fragment = prompt_html(prompt)
    assert 'class="prompt-heading"' in fragment
    assert "<script>" not in fragment
    assert html.unescape(re.sub(r"<[^>]*>", "", fragment)) == prompt


async def test_overview_escapes_capability_names(created):
    result = (await detail(created))["data"]
    result["persona"]["tools"] = ['<script>alert("bad")</script>']
    document = rendering.overview_html(result, 1000)
    assert "<script>" not in document and "&lt;script&gt;" in document


@pytest.mark.parametrize(
    "value",
    [1791541635.7853906, "2026-10-09T10:27:15.800387", "2026-10-09T18:27:15+08:00"],
)
def test_metadata_times_use_consistent_utc(value):
    assert format_time(value) == "2026-10-09 10:27:15 UTC"


def test_page_height_does_not_require_fixed_line_multiple():
    assert Settings.parse({"render_page_height": 1001}).render_page_height == 1001


async def test_paginated_images_and_optional_md(created, tmp_path, monkeypatch):
    page, calls = fake_browser(created)
    monkeypatch.setattr(rendering, "get_astrbot_temp_path", lambda: str(tmp_path))
    captured = []
    captured_names = []

    async def send(message):
        component = message.chain[0]
        if component.type.value == "File":
            captured_names.append(component.name)
            path = await component.get_file()
            captured.append(
                await asyncio.to_thread(Path(path).read_text, encoding="utf-8")
            )

    created.event.send = AsyncMock(side_effect=send)
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=True
    )
    assert response["ok"], response
    assert response["delivery"]["images_submitted"] == 3
    assert response["delivery"]["md_submitted"]
    assert captured_names == ["人格_alice_当前_v1.md"]
    assert captured and "版本1" in captured[0]
    assert not list(tmp_path.rglob("*.md"))
    assert calls[0]["javascript_enabled"] is False
    assert page.screenshot.call_args_list[0].kwargs["clip"]["height"] == 1200
    assert page.screenshot.call_args_list[2].kwargs["clip"]["y"] == 1500
    assert 'data-sheet="overview"' in page.set_content.call_args_list[0].args[0]
    assert 'data-sheet="content"' in page.set_content.call_args_list[1].args[0]


async def test_historical_md_filename_uses_snapshot_revision(
    created, tmp_path, monkeypatch
):
    updated = await created.studio.execute(
        created.event,
        "manage",
        "update",
        {"persona_id": "alice", "system_prompt": "版本2"},
    )
    assert updated["ok"], updated
    response = await created.studio.execute(
        created.event,
        "versions",
        "get",
        {"backup_id": updated["data"]["backup_id"]},
    )
    assert response["ok"], response
    assert response["data"]["revision"] == 2
    assert response["data"]["snapshot"]["revision"] == 1
    fake_browser(created, height=450)
    monkeypatch.setattr(rendering, "get_astrbot_temp_path", lambda: str(tmp_path))
    response = await rendering.present(
        created.studio, created.event, response, include_md=True
    )
    assert response["ok"] and response["delivery"]["md_submitted"]
    file = created.event.send.await_args_list[-1].args[0].chain[0]
    assert file.type.value == "File"
    assert file.name == "人格_alice_历史_v1.md"
    assert not list(tmp_path.rglob("*.md"))


@pytest.mark.parametrize("family", ["manage", "versions"])
async def test_model_get_sends_overview_before_content(created, family):
    payload = {"persona_id": "alice"}
    if family == "versions":
        updated = await created.studio.execute(
            created.event,
            "manage",
            "update",
            {"persona_id": "alice", "system_prompt": "版本2"},
        )
        assert updated["ok"], updated
        payload = {"backup_id": updated["data"]["backup_id"]}
    page, _ = fake_browser(created, height=450)
    page.screenshot.side_effect = [PNG + b"overview", PNG + b"content"]
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    handler = getattr(plugin, f"persona_{family}")
    response = json.loads(await handler(created.event, "get", payload))
    assert response["ok"], response
    assert response["delivery"]["images_submitted"] == 2
    assert not response["delivery"]["md_submitted"]
    assert created.event.send.await_count == 2
    messages = [call.args[0] for call in created.event.send.await_args_list]
    assert [message.chain[0].type.value for message in messages] == ["Image", "Image"]
    assert [message.chain[0].file for message in messages] == [
        Image.fromBytes(PNG + b"overview").file,
        Image.fromBytes(PNG + b"content").file,
    ]
    overview = page.set_content.call_args_list[0].args[0]
    content = page.set_content.call_args_list[1].args[0]
    assert 'data-sheet="overview"' in overview
    assert "版本与权限" in overview and "人格信息与能力" in overview
    assert 'data-sheet="content"' in content and "系统提示词" in content


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
    assert page.screenshot.await_count == 1


async def test_overview_pixel_limit_rejects_without_splitting(created):
    page, _ = fake_browser(created, overview_height=3000)
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=False
    )
    assert response["error"]["code"] == "render_limit"
    assert "档案" in response["error"]["message"]
    created.event.send.assert_not_called()
    page.screenshot.assert_not_called()


async def test_total_page_limit_includes_overview(created):
    fake_browser(created, height=500)
    created.config["render_max_pages"] = 1
    response = await rendering.present(
        created.studio, created.event, await detail(created), include_md=False
    )
    assert response["error"]["code"] == "render_limit"
    created.event.send.assert_not_called()


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
        assert len(images) >= 3
        for index, image in enumerate(images):
            with Image.open(io.BytesIO(image)) as picture:
                assert picture.width == 1000 and picture.height > 0
                assert picture.width * picture.height <= Settings().render_max_pixels
                if index:
                    assert picture.height <= 1500
            (tmp_path / f"preview-{index + 1}.png").write_bytes(image)
        prompt = (
            "\n"
            + "\n\n".join(
                f"【第 {index + 1} 节】\n"
                + "长段落完整换行验证，标点、中文与原始内容均保留。" * 20
                for index in range(12)
            )
            + "\n"
        )
        response["data"]["persona"]["system_prompt"] = prompt
        async with service.session(
            viewport={"width": 1000, "height": 1500}, javascript_enabled=False
        ) as page:
            await page.set_content(rendering.content_html(response["data"], 1000))
            layout = await page.evaluate(
                rendering.PAGINATE_CONTENT, {"height": 1500, "maxPages": 19}
            )
            assert "error" not in layout and len(layout["pages"]) > 1
            inspected = await page.evaluate("""() => {
                const sheets = [...document.querySelectorAll('[data-sheet="content"]')];
                let broken = 0;
                for (const sheet of sheets) {
                    const viewport = sheet.querySelector('.flow-window').getBoundingClientRect();
                    for (const element of sheet.querySelectorAll('[data-text]')) {
                        const range = document.createRange();
                        range.selectNodeContents(element);
                        for (const rect of range.getClientRects()) {
                            if (rect.width > 0 && ((rect.top < viewport.top - .05 && rect.bottom > viewport.top + .05) || (rect.top < viewport.bottom - .05 && rect.bottom > viewport.bottom + .05))) broken++;
                        }
                    }
                }
                return {broken, prompt: sheets[0].querySelector('[data-text]').textContent};
            }""")
            assert inspected["broken"] == 0
            assert inspected["prompt"] == prompt
    finally:
        await service.close()
