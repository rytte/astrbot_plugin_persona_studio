import jsonschema
import pytest
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot_plugin_persona_studio.domain import Settings
from astrbot_plugin_persona_studio.tool_schema import tool_parameters


@pytest.mark.parametrize("family", ["manage", "versions"])
def test_parameters_are_valid_json_schema(family):
    schema = tool_parameters(family, Settings())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(
        {
            "action": "list",
            "payload": {} if family == "manage" else {"persona_id": "alice"},
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {"action": "unlock", "payload": {"persona_id": "alice"}}, schema
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"action": "get", "payload": {"owner": "attacker"}}, schema)


def test_gemini_payload_has_fields_and_nullable_capabilities():
    schema = tool_parameters("manage", Settings())
    tool = FunctionTool(name="persona_manage", description="Manage", parameters=schema)
    declaration = ToolSet([tool]).google_schema()["function_declarations"][0]
    payload = declaration["parameters"]["properties"]["payload"]
    assert payload["properties"]
    tools = payload["properties"]["tools"]
    assert tools["anyOf"][0]["type"] == "array"
    assert tools["anyOf"][0]["items"]["type"] == "string"
    assert tools["anyOf"][1]["type"] == "null"
    for value in (None, [], ["echo"]):
        jsonschema.validate(
            {"action": "update", "payload": {"persona_id": "alice", "tools": value}},
            schema,
        )
