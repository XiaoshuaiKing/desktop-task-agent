from tools.tool_schemas import TOOL_SCHEMAS,TOOL_REGISTRY

USER_ROLES = {
    "user_a": "admin",
    "user_b": "user"
}

ROLE_TOOLS={
    "admin":set(TOOL_REGISTRY.keys()),
    "user":{
        "list_browser_tabs",
        "open_vscode",
        "open_wps",
        "open_software",
        "find_latest_document",
        "open_website",
        "smart_web_operate",
        "extract_web_content",
        "click_web_element",
        "close_website",
        "get_screenshot",
        "read_clipboard",
        "write_clipboard",
        "set_reminder",
        "find_and_open_file",
        "manage_window",
        "inspect_window_ui",
        "operate_window_ui",
        "read_text_file",
        "press_keys",
        "click_mouse",
        "scroll_mouse",
        "read_task_original"
    },
    "guest":{
        "find_latest_document",
        "open_website",
        "extract_web_content",
        "get_screenshot",
        "read_clipboard",
        "find_and_open_file",
        "read_text_file",
        "inspect_window_ui",
        "read_task_original"
    }
}

def get_user_role(user_id):
    return USER_ROLES.get(user_id, "guest")

def check_permission(func_name, role):
    return func_name in ROLE_TOOLS.get(role, set())

def get_required_params(tool_name):
    """根据工具名，从 TOOL_SCHEMAS 中提取 required 参数列表"""
    for tool in TOOL_SCHEMAS:
        if tool["function"]["name"] == tool_name:
            return tool["function"].get("parameters", {}).get("required", [])
    return [] 

def check_args(args,required_params,tool_name):
    if not all(key in args for key in required_params):
        return False
    type_map={"string":str,"integer":int,"number":(int,float),"boolean":bool}
    for tool in TOOL_SCHEMAS:
        if tool["function"]["name"]==tool_name:
            properties=tool["function"]["parameters"].get("properties",{})
            for key,value in args.items():
                expected=properties.get(key,{}).get("type")
                if expected in type_map and not isinstance(value,type_map[expected]):
                    return False
            return True
    return False





