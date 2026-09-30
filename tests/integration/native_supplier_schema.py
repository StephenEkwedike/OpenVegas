"""Synthetic supplier encoding selected by the actual dispatched tool schema."""
import json


def tool_function(payload, call):
    functions = {tool["function"]["name"]: tool["function"]
                 for tool in payload["tools"] if tool.get("type") == "function"}
    name = call["tool_name"]
    if "call_local_tool" in functions:
        assert set(functions) == {"call_local_tool"}
        return {"name": "call_local_tool", "arguments": json.dumps(call)}
    assert name in functions, "Synthetic response must use a dispatched function"
    arguments = dict(call["arguments"])
    if "timeout_sec" in call:
        arguments["timeout_sec"] = call["timeout_sec"]
    if name == "Bash" and "shell_mode" in call:
        arguments["shell_mode"] = call["shell_mode"]
    elif "shell_mode" in call:
        expected = "mutating" if name in {"Write", "FindAndReplace", "InsertAtEnd"} else "read_only"
        assert call["shell_mode"] == expected
    assert arguments.keys() <= functions[name]["parameters"]["properties"].keys()
    return {"name": name, "arguments": json.dumps(arguments)}
