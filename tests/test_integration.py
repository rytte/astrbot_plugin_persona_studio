import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import sp
from astrbot.core import astr_agent_tool_exec, astr_main_agent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.db.po import Conversation
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_persona_studio.main import PersonaStudioPlugin


@pytest.mark.parametrize("path", ["main", "handoff", "full"])
async def test_core_permission_checks_depend_on_tool_selection(
    created, monkeypatch, path
):
    monkeypatch.setattr(
        sp, "global_get", AsyncMock(return_value={"_default": {"echo": "admin"}})
    )
    monkeypatch.setattr(
        astr_main_agent,
        "SkillManager",
        lambda: SimpleNamespace(list_skills=lambda **kwargs: []),
    )
    run_context = ContextWrapper(
        context=SimpleNamespace(context=created.context, event=created.event)
    )
    if path == "full":
        toolset = created.tools.get_full_tool_set()
    elif path == "main":
        request = ProviderRequest(
            conversation=Conversation(
                platform_id="platform",
                user_id=created.event.unified_msg_origin,
                cid="conversation",
                persona_id="alice",
            )
        )
        await astr_main_agent._ensure_persona_and_skills(
            request, {"computer_use_runtime": "none"}, created.context, created.event
        )
        toolset = request.func_tool
    else:
        monkeypatch.setattr(astr_agent_tool_exec, "llm_tools", created.tools)
        toolset = FunctionToolExecutor._build_handoff_toolset(run_context, ["echo"])
    tool = toolset.get_tool("echo")
    results = [
        result async for result in FunctionToolExecutor.execute(tool, run_context)
    ]
    if path == "full":
        assert "Permission denied" in results[-1].content[0].text
        created.handler.assert_not_awaited()
    else:
        assert results[-1].content[0].text == "echo-result"
        created.handler.assert_awaited_once()
    created.handler.reset_mock()
    created.event.role = "admin"
    results = [
        result async for result in FunctionToolExecutor.execute(tool, run_context)
    ]
    assert results[-1].content[0].text == "echo-result"
    created.handler.assert_awaited_once()


async def test_webui_write_can_be_overwritten_during_restore(created, monkeypatch):
    first = await created.studio.execute(
        created.event,
        "manage",
        "update",
        {"persona_id": "alice", "system_prompt": "版本2"},
    )
    backup_id = first["data"]["backup_id"]
    version = await created.studio.execute(
        created.event, "versions", "get", {"backup_id": backup_id}
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = created.db.update_persona

    async def pause(persona_id, *args, **kwargs):
        if kwargs.get("system_prompt") == "版本1" or (args and args[0] == "版本1"):
            entered.set()
            await release.wait()
        return await original(persona_id, *args, **kwargs)

    monkeypatch.setattr(created.db, "update_persona", pause)
    restore = asyncio.create_task(
        created.studio.execute(
            created.event,
            "versions",
            "restore",
            {
                "backup_id": backup_id,
                "expected_revision": version["data"]["expected_revision"],
            },
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await asyncio.wait_for(
            created.manager.update_persona("alice", system_prompt="later WebUI change"),
            5,
        )
        assert (await created.studio.current("alice"))[
            "system_prompt"
        ] == "later WebUI change"
    finally:
        release.set()
        restored = await asyncio.wait_for(restore, 5)
    assert restored["ok"] and restored["data"]["persona"]["system_prompt"] == "版本1"
    assert (await created.studio.current("alice"))["system_prompt"] == "版本1"


async def test_revocation_during_quota_check_prevents_create(env, monkeypatch):
    original = env.studio.check_quota

    async def revoke(actor, settings):
        await original(actor, settings)
        env.config["whitelist"] = []

    monkeypatch.setattr(env.studio, "check_quota", revoke)
    response = await env.studio.execute(
        env.event,
        "manage",
        "create",
        {"persona_id": "denied", "system_prompt": "prompt"},
    )
    assert response["error"]["code"] == "forbidden"
    assert await env.studio.current("denied") is None


async def test_plugin_initializes_without_core_patch(env, tmp_path, monkeypatch):
    from astrbot.api.star import StarTools

    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: tmp_path / "plugin")
    plugin = PersonaStudioPlugin(env.context, env.config)
    await plugin.initialize()
    try:
        assert plugin.studio.manager is env.manager
        assert plugin.maintenance_task is not None
    finally:
        await plugin.terminate()


async def test_plugin_tools_and_command_dispatch(created):
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    assert '"ok": true' in await plugin.persona_manage(created.event, "list", {})
    message = created.native_event(text="/ps lock alice")
    replies = [reply async for reply in plugin.persona(message, "lock alice")]
    assert len(replies) == 1
    assert (
        await created.studio.execute(
            created.event,
            "manage",
            "update",
            {"persona_id": "alice", "system_prompt": "blocked"},
        )
    )["error"]["code"] == "persona_locked"


async def test_explicit_builtin_tool_stays_available(created, monkeypatch):
    from astrbot.core.tools.message_tools import SendMessageToUserTool

    tool = created.tools.get_builtin_tool(SendMessageToUserTool)
    await created.manager.update_persona("alice", tools=[tool.name])
    monkeypatch.setattr(
        astr_main_agent,
        "SkillManager",
        lambda: SimpleNamespace(list_skills=lambda **kwargs: []),
    )
    request = ProviderRequest(
        conversation=Conversation(
            platform_id="platform",
            user_id=created.event.unified_msg_origin,
            cid="conversation",
            persona_id="alice",
        )
    )
    await astr_main_agent._ensure_persona_and_skills(
        request, {"computer_use_runtime": "none"}, created.context, created.event
    )
    assert isinstance(request.func_tool.get_tool(tool.name), SendMessageToUserTool)


async def test_plugin_lifecycle_persists_metadata(env, tmp_path, monkeypatch):
    from astrbot.api.star import StarTools

    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: tmp_path / "plugin")
    plugin = PersonaStudioPlugin(env.context, env.config)
    await plugin.initialize()
    assert plugin.maintenance_task is not None
    response = await plugin.studio.execute(
        env.event,
        "manage",
        "create",
        {"persona_id": "persistent", "system_prompt": "prompt"},
    )
    assert response["ok"]
    await plugin.terminate()
    await plugin.initialize()
    try:
        response = await plugin.studio.execute(
            env.event, "manage", "get", {"persona_id": "persistent"}
        )
        assert response["data"]["owner"] == ["platform", "user"]
    finally:
        await plugin.terminate()


@pytest.mark.parametrize(
    "text,arguments", [("/ps lock alice", "lock alice"), ("/ps", "")]
)
async def test_native_command_filter_preserves_greedy_arguments(
    created, text, arguments
):
    from astrbot.core.star.filter.command import CommandFilter

    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    handler = SimpleNamespace(handler=PersonaStudioPlugin.persona)
    command_filter = CommandFilter("ps", handler_md=handler)
    event = created.native_event(text=text)
    event.message_str = text[1:]
    event.is_at_or_wake_command = True
    assert command_filter.filter(event, created.core_config)
    parsed = event.get_extra("parsed_params")
    assert parsed == {"arguments": arguments}
    replies = [reply async for reply in plugin.persona(event, **parsed)]
    assert len(replies) == 1
    record = created.storage.records("alice")[0]
    assert record["locked"] == int(bool(arguments))


async def test_native_command_can_lock_id_with_repeated_spaces(created):
    from astrbot.core.star.filter.command import CommandFilter

    result = await created.studio.execute(
        created.event,
        "manage",
        "create",
        {"persona_id": "with  spaces", "system_prompt": "prompt"},
    )
    assert result["ok"]
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    event = created.native_event(text="/ps lock with  spaces")
    event.message_str = "ps lock with  spaces"
    event.is_at_or_wake_command = True
    command_filter = CommandFilter(
        "ps", handler_md=SimpleNamespace(handler=PersonaStudioPlugin.persona)
    )
    assert command_filter.filter(event, created.core_config)
    replies = [
        reply
        async for reply in plugin.persona(event, **event.get_extra("parsed_params"))
    ]
    assert len(replies) == 1
    assert created.storage.records("with  spaces")[0]["locked"] == 1


@pytest.mark.parametrize("text", ["/persona lock alice", "/persona unlock alice"])
async def test_old_command_is_not_recognized(created, text):
    from astrbot.core.star.filter.command import CommandFilter

    event = created.native_event(text=text)
    event.message_str = text[1:]
    event.is_at_or_wake_command = True
    command_filter = CommandFilter(
        "ps", handler_md=SimpleNamespace(handler=PersonaStudioPlugin.persona)
    )
    assert not command_filter.filter(event, created.core_config)
