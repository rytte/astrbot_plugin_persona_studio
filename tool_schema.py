from copy import deepcopy


def tool_parameters(family: str, settings) -> dict:
    common = {
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "list 的分页起始位置，默认 0",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "list 每页数量，默认 20",
        },
        "format": {
            "type": "string",
            "enum": ["text", "image", "markdown"],
            "description": "仅 get：text 为合并转发文字（仅 OneBot/Aiocqhttp），image 为图片（默认），markdown 为 MD 文件；每次仅发送一种形式",
        },
        "content": {
            "type": "string",
            "enum": ["metadata", "prompt", "all"],
            "description": "仅 get：metadata 为版本权限、人格信息及工具/技能和自定义错误回复；prompt 为系统提示词与预设对话（默认）；all 为两者",
        },
    }
    if family == "manage":
        actions = ["list", "get", "create", "update", "delete", "switch"]
        names = {
            "anyOf": [
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 200,
                    "uniqueItems": True,
                },
                {"type": "null"},
            ],
            "description": "null 表示全部，[] 禁用，名称列表指定范围；省略保持不变",
        }
        properties = {
            **common,
            "persona_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "人格 ID；除 list 外必填",
            },
            "system_prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": settings.max_prompt_chars,
                "description": "完整系统提示词；create 必填",
            },
            "begin_dialogs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "偶数条预设对话，用户、助手交替；[] 清空",
            },
            "tools": deepcopy(names),
            "skills": deepcopy(names),
            "custom_error_message": {
                "anyOf": [
                    {"type": "string", "maxLength": settings.max_error_chars},
                    {"type": "null"},
                ],
                "description": "自定义错误回复，null 清除",
            },
        }
    else:
        actions = ["list", "get", "restore", "delete"]
        properties = {
            **common,
            "persona_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "list 时必填",
            },
            "backup_id": {
                "type": "string",
                "minLength": 1,
                "description": "get/restore/delete 时必填，只能使用查询返回的备份 ID",
            },
            "expected_revision": {
                "type": "string",
                "description": "restore 时必填，必须是最新查询返回的完整状态令牌，不是整数版本号",
            },
        }
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": actions,
                "description": "执行的操作；get 按 format/content 自行发送所选详情，默认仅发送提示词图片，并返回结构化详情与发送回执",
            },
            "payload": {
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
                "description": "只提交对应 action 的参数；未知参数或不符合该 action 的参数会报错",
            },
        },
        "required": ["action", "payload"],
        "additionalProperties": False,
    }
