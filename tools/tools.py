import os
import re
import json
import time
import glob
import base64
import shutil
import smtplib
import asyncio
import threading
import subprocess
import functools
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl
import mimetypes
import logging
import config.logger_config
from contextvars import ContextVar
from email.message import EmailMessage

logger=logging.getLogger(__name__)
_qq_api=ContextVar("qq_api",default=None)
_qq_loop=ContextVar("qq_loop",default=None)
# 当前运行的任务：(task_id, user_id)。由 run_agent 绑定，供需要校验归属的工具使用。
# 用 ContextVar 而不是模块全局，避免多线程/多用户并发时串号。
_current_task=ContextVar("current_task",default=None)


def bind_task_context(task_id,user_id=None):
    """由执行器在任务开始时调用，绑定可信的任务与用户身份。"""
    _current_task.set({
        "task_id":str(task_id) if task_id else None,
        "user_id":str(user_id) if user_id else None
    })


def current_task_context():
    return _current_task.get() or {}

_qq_user_id=ContextVar("qq_user_id",default=None)

def set_runtime_context(api=None,loop=None,user_id=None):
    _qq_api.set(api)
    _qq_loop.set(loop)
    _qq_user_id.set(user_id)

def json_result(func):
    @functools.wraps(func)
    def wrapper(*args,**kwargs):
        try:
            result=func(*args,**kwargs)
            if isinstance(result,str):
                return result
            return json.dumps(result,ensure_ascii=False)
        except Exception as e:
            logger.warning(f"工具 {func.__name__} 执行失败: {e}",exc_info=True)
            return json.dumps({"status":"error","message":str(e)},ensure_ascii=False)
    return wrapper

def _format_url(url):
    url=url.strip()
    if url.startswith(("http://","https://")):
        return url
    shortcuts={"deepseek":"chat.deepseek.com","baidu":"www.baidu.com","bing":"www.bing.com","google":"www.google.com","sogou":"www.sogou.com","360":"www.so.com","bilibili":"www.bilibili.com","taobao":"www.taobao.com","jd":"www.jd.com","github":"github.com","douyin":"www.douyin.com","weibo":"weibo.com","doubao":"www.doubao.com"}
    if url.lower() in shortcuts:
        return "https://"+shortcuts[url.lower()]
    if "." in url:
        return "https://"+url
    return url

# ==================== UIA 浏览器自动化模块 ====================
# 说明：本模块直接替换原来的 Playwright + CDP 浏览器控制实现。
#      不启动远程调试端口，不连接 CDP，不需要浏览器远程调试授权。
#      通过 Windows UI Automation（UIA）和真实键盘操作控制浏览器。

_BROWSER_LOCK = threading.RLock()

# 网页输入框动态等待配置：避免页面加载较慢时只扫描一次就失败。
WEB_INPUT_WAIT_TIMEOUT = 15
WEB_INPUT_POLL_INTERVAL = 0.6


def _normalize_browser_name(browser_name=None):
    """规范化浏览器名称，默认使用 Edge。"""
    browser_name = (browser_name or "edge").strip().lower()
    aliases = {
        "edge": "edge", "msedge": "edge", "microsoft edge": "edge", "微软浏览器": "edge",
        "chrome": "chrome", "google chrome": "chrome", "谷歌浏览器": "chrome",
    }
    if browser_name not in aliases:
        raise ValueError("浏览器配置无效，browser_name 只能是 edge 或 chrome")
    return aliases[browser_name]


def _get_browser_path(browser_name="edge"):
    """查找 Edge 或 Chrome 可执行文件路径。"""
    browser_name = _normalize_browser_name(browser_name)
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get(
        "LOCALAPPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Local")
    )

    if browser_name == "edge":
        candidates = [
            shutil.which("msedge"),
            shutil.which("msedge.exe"),
            os.path.join(local_app_data, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
        ]
    else:
        candidates = [
            shutil.which("chrome"),
            shutil.which("chrome.exe"),
            os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
        ]

    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    return None


def _get_uia():
    """延迟导入 uiautomation，避免影响非浏览器工具的导入。"""
    try:
        import uiautomation as auto
        return auto
    except ImportError as exc:
        raise RuntimeError("缺少 uiautomation，请使用当前项目 Python 环境安装：pip install uiautomation") from exc


def _with_browser_uia(func):
    """为浏览器工具统一加 UIA COM 初始化和线程锁。"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        import ctypes
        with _BROWSER_LOCK:
            com_initialized = False
            try:
                # UIA 通过 COM 工作。S_OK(0) 和 S_FALSE(1) 都需要配对 CoUninitialize。
                result = ctypes.windll.ole32.CoInitialize(None)
                com_initialized = result in (0, 1)
            except Exception:
                # 当前线程可能已经处于其他 COM Apartment；继续尝试使用 UIA。
                com_initialized = False
            try:
                return func(*args, **kwargs)
            finally:
                if com_initialized:
                    try:
                        ctypes.windll.ole32.CoUninitialize()
                    except Exception:
                        pass
    return wrapper


def _browser_window_matches(window, browser_name):
    """判断顶层窗口是否属于指定浏览器。"""
    try:
        if window.ControlTypeName != "WindowControl":
            return False
        title = (window.Name or "").casefold()
        if browser_name == "edge":
            return "microsoft" in title and "edge" in title
        return "google chrome" in title or ("chrome" in title and "edge" not in title)
    except Exception:
        return False


def _find_browser_windows(browser_name="edge"):
    """枚举当前桌面中指定浏览器的顶层窗口。"""
    auto = _get_uia()
    browser_name = _normalize_browser_name(browser_name)
    windows = []
    try:
        for window in auto.GetRootControl().GetChildren():
            if _browser_window_matches(window, browser_name):
                windows.append(window)
    except Exception as exc:
        logger.warning("枚举浏览器窗口失败：%s", exc)
    return windows


def _find_browser_window(browser_name="edge"):
    """获取一个可用的浏览器窗口；不存在时返回 None。"""
    windows = _find_browser_windows(browser_name)
    return windows[0] if windows else None


def _walk_uia_controls(root, max_depth=28, max_nodes=6000):
    """以广度优先方式遍历 UIA 控件树，限制深度和节点数避免扫描失控。"""
    from collections import deque
    queue = deque([(root, 0)])
    visited = 0
    while queue and visited < max_nodes:
        control, depth = queue.popleft()
        visited += 1
        yield control, depth
        if depth >= max_depth:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            continue
        for child in children:
            queue.append((child, depth + 1))


def _get_browser_path_or_error(browser_name="edge"):
    """查找浏览器路径；未安装时给出明确错误。"""
    browser_name = _normalize_browser_name(browser_name)
    path = _get_browser_path(browser_name)
    if not path:
        display_name = "Microsoft Edge" if browser_name == "edge" else "Google Chrome"
        raise FileNotFoundError(f"没有找到{display_name}，请检查浏览器是否安装")
    return path


def _ensure_browser_window(browser_name="edge", timeout=25):
    """确保浏览器窗口存在；没有运行时通过普通启动方式打开，不使用 CDP。"""
    auto = _get_uia()
    browser_name = _normalize_browser_name(browser_name)
    auto.uiautomation.SetGlobalSearchTimeout(5)

    window = _find_browser_window(browser_name)
    if window is not None:
        try:
            window.SetFocus()
        except Exception:
            pass
        return window

    browser_path = _get_browser_path_or_error(browser_name)
    display_name = "Edge" if browser_name == "edge" else "Chrome"
    logger.info("浏览器未运行，准备启动 %s：%s", display_name, browser_path)

    # 使用普通浏览器启动参数，不设置远程调试端口、不创建自动化专用配置目录。
    process = subprocess.Popen(
        [browser_path, "--new-window", "about:blank"],
        shell=False,
        close_fds=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("%s 启动请求已提交，启动进程 PID=%s", display_name, process.pid)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.4)
        window = _find_browser_window(browser_name)
        if window is not None:
            try:
                window.SetFocus()
            except Exception:
                pass
            logger.info("%s 窗口已就绪，窗口 PID=%s", display_name, window.ProcessId)
            return window

    raise TimeoutError(f"{display_name} 已提交启动，但 {timeout} 秒内未发现浏览器窗口")


def _find_address_bar(window):
    """通过 UIA 控件属性查找浏览器地址栏。"""
    for control, _ in _walk_uia_controls(window, max_depth=18, max_nodes=3500):
        try:
            if control.ControlTypeName != "EditControl":
                continue
            class_name = control.ClassName or ""
            name = control.Name or ""
            if "omnibox" in class_name.casefold() or name in ("地址和搜索栏", "Address and search bar"):
                return control
        except Exception:
            continue
    return None


def _read_browser_url(window):
    """读取当前浏览器地址栏内容，不改变当前页面。"""
    address_bar = _find_address_bar(window)
    if address_bar is None:
        return ""
    try:
        return str(address_bar.GetValuePattern().Value or "").strip()
    except Exception:
        return ""


def _hosts_match(actual_host, target_host):
    """
    判断两个主机名是否可以视为同一导航目标。

    默认要求 hostname 完全一致；仅兼容 www 与裸域名之间的常见跳转，
    以及明确列出的搜索引擎区域域名。不会把同一注册域名下的任意子域名
    （例如 www.deepseek.com 与 chat.deepseek.com）视为同一页面。
    """
    actual_host = (actual_host or "").casefold().strip(".")
    target_host = (target_host or "").casefold().strip(".")

    if not actual_host or not target_host:
        return False
    if actual_host == target_host:
        return True

    # 兼容 www.example.com 与 example.com 之间的常见规范化跳转。
    actual_without_www = actual_host[4:] if actual_host.startswith("www.") else actual_host
    target_without_www = target_host[4:] if target_host.startswith("www.") else target_host
    if (
        actual_without_www == target_without_www
        and (actual_host.startswith("www.") or target_host.startswith("www."))
    ):
        return True

    # Bing 可能在 www.bing.com 与 cn.bing.com 之间发生区域跳转。
    # 采用显式白名单，避免将其他网站的不同子域名错误合并。
    known_host_alias_groups = (
        {"www.bing.com", "cn.bing.com", "bing.com"},
    )
    return any(
        actual_host in aliases and target_host in aliases
        for aliases in known_host_alias_groups
    )


def _url_matches(actual_url, target_url):
    """验证导航结果；主机名严格匹配，仅兼容明确允许的规范化跳转。"""
    if not actual_url or not target_url:
        return False
    if target_url in ("about:blank", "edge://newtab/", "chrome://newtab/"):
        return actual_url == target_url

    actual = urlsplit(actual_url)
    target = urlsplit(target_url)
    if not actual.hostname or not target.hostname:
        return actual_url == target_url

    if not _hosts_match(actual.hostname, target.hostname):
        return False

    # 目标路径必须精确匹配，或是目标路径下的子路径，避免 /searchabc
    # 被误判为 /search。
    target_path = target.path.rstrip("/")
    actual_path = actual.path.rstrip("/")
    if target_path and not (
        actual_path == target_path
        or actual_path.startswith(target_path + "/")
    ):
        return False

    # 将查询参数解析后比较，兼容空格被编码为 + 或 %20 的情况。
    if target.query:
        actual_params = parse_qsl(actual.query, keep_blank_values=True)
        target_params = parse_qsl(target.query, keep_blank_values=True)
        if not all(item in actual_params for item in target_params):
            return False

    return True


# 常见搜索引擎的搜索结果路径和关键词参数。
_SEARCH_ENGINE_CONFIG = {
    "baidu.com": ("/s", "wd"),
    "bing.com": ("/search", "q"),
    "google.com": ("/search", "q"),
    "google.cn": ("/search", "q"),
    "sogou.com": ("/web", "query"),
    "so.com": ("/s", "q"),
    "yahoo.com": ("/search", "p"),
}


def _build_search_url(base_url, keyword):
    """
    根据搜索引擎首页或搜索页 URL，直接构造搜索结果 URL。

    返回 None 表示该网址不是内置支持的搜索引擎，调用方应继续使用
    原有 UIA 输入框操作逻辑，保证聊天网站等普通网页的交互能力。
    """
    parsed = urlsplit(base_url)
    host = (parsed.hostname or "").casefold()
    engine_config = None

    for domain, config in _SEARCH_ENGINE_CONFIG.items():
        if host == domain or host.endswith("." + domain):
            engine_config = config
            break

    if engine_config is None:
        return None

    search_path, keyword_param = engine_config
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() != keyword_param.casefold()
    ]
    query_items.append((keyword_param, str(keyword).strip()))

    return urlunsplit((
        parsed.scheme or "https",
        parsed.netloc,
        search_path,
        urlencode(query_items),
        "",
    ))


def _is_blank_browser_page(url):
    """判断当前标签页是否为空白页或浏览器新标签页。"""
    value = (url or "").strip().casefold()
    return value in ("", "about:blank", "edge://newtab/", "edge://newtab", "chrome://newtab/", "chrome://newtab")


def _paste_text(text):
    """通过剪贴板粘贴文本，并尽量恢复用户原剪贴板文本。"""
    try:
        import pyperclip
    except ImportError as exc:
        raise RuntimeError("缺少 pyperclip，请在项目 Python 环境安装：pip install pyperclip") from exc

    auto = _get_uia()
    try:
        old_clipboard = pyperclip.paste()
    except Exception:
        old_clipboard = None
    try:
        pyperclip.copy(str(text))
        auto.SendKeys("{Ctrl}v")
        # 留出剪贴板内容被目标应用读取的时间，再恢复用户原剪贴板。
        time.sleep(0.35)
    finally:
        if old_clipboard is not None:
            try:
                pyperclip.copy(old_clipboard)
            except Exception:
                pass


def _navigate_current_tab(window, url, timeout=25):
    """通过地址栏和键盘导航到指定 URL，并轮询确认地址栏结果。"""
    auto = _get_uia()
    window.SetFocus()
    time.sleep(0.15)

    address_bar = _find_address_bar(window)
    if address_bar is None:
        raise RuntimeError("没有找到浏览器地址栏，无法导航")

    auto.SendKeys("{Ctrl}l")
    time.sleep(0.15)
    _paste_text(url)

    # 在提交前检查地址栏是否收到完整 URL，避免把错误内容提交到页面。
    try:
        typed_url = address_bar.GetValuePattern().Value
    except Exception:
        typed_url = ""
    if typed_url and str(typed_url).strip() != url:
        raise RuntimeError(f"地址栏输入验证失败，预期：{url}，实际：{typed_url}")

    auto.SendKeys("{Enter}")
    deadline = time.monotonic() + timeout
    last_url = ""
    while time.monotonic() < deadline:
        time.sleep(0.4)
        last_url = _read_browser_url(window)
        if _url_matches(last_url, url):
            return last_url

    raise TimeoutError(f"网页导航超时，目标：{url}；最后读取到：{last_url or '地址栏为空'}")


def _open_url_in_browser(window, url, timeout=25):
    """打开目标网址：复用相同页面；非空白页面则新建标签页，避免覆盖用户当前页面。"""
    auto = _get_uia()
    current_url = _read_browser_url(window)
    if _url_matches(current_url, url):
        try:
            window.SetFocus()
        except Exception:
            pass
        return current_url, False

    if not _is_blank_browser_page(current_url):
        window.SetFocus()
        auto.SendKeys("{Ctrl}t")
        time.sleep(0.35)

    actual_url = _navigate_current_tab(window, url, timeout=timeout)
    return actual_url, True


def _control_rect_is_valid(control):
    """判断 UIA 控件是否具有有效的屏幕矩形。"""
    try:
        rect = control.BoundingRectangle
        width = getattr(rect, "width", None)
        height = getattr(rect, "height", None)
        if width is None or height is None:
            width = rect.right - rect.left
            height = rect.bottom - rect.top
        return width > 0 and height > 0
    except Exception:
        return False


def _get_value_pattern(control):
    """安全获取控件 ValuePattern；不支持时返回 None。"""
    try:
        return control.GetValuePattern()
    except Exception:
        return None


def _parse_web_input_selector(selector):
    """
    将常见 CSS 风格选择器拆解为 UIA 可检查的属性提示。

    注意：UIA 不能执行真正的 CSS 查询。这里仅把 #id、.class、
    [name=...]、[placeholder=...] 等信息映射到 AutomationId、
    ClassName 和 Name，不能保证覆盖所有网页 DOM 属性。
    """
    selector = (selector or "").strip()

    # 这些选择器只指定输入框类型，无法通过 UIA 精确映射到 DOM。
    generic_selectors = {
        "", "body", "html", "*", "document",
        "input", "textarea", "input[type='text']",
        'input[type="text"]', "input[type=text]",
        "input[type='search']", 'input[type="search"]',
        "input[type=search]",
    }
    if selector.casefold() in generic_selectors:
        return {
            "specific": False,
            "ids": [],
            "classes": [],
            "attributes": [],
            "literal": "",
        }

    ids = re.findall(r"#([\w-]+)", selector)
    classes = re.findall(r"\.([\w-]+)", selector)
    attributes = re.findall(
        r"\[\s*(name|placeholder|aria-label|data-testid|id|type)\s*=\s*['\"]?([^'\"\]]+)['\"]?\s*\]",
        selector,
        flags=re.IGNORECASE,
    )

    # 如果没有解析出常见 CSS 结构，则把整个字符串作为普通属性提示。
    # 例如传入 "Search" 时，允许在 Name / ClassName / AutomationId 中查找。
    literal = ""
    if not ids and not classes and not attributes:
        simple_tag = re.fullmatch(r"[a-zA-Z][\w-]*", selector)
        if not simple_tag:
            literal = selector

    # type 通常不会作为 UIA 属性直接暴露，因此不单独作为严格匹配条件。
    strict_attributes = [
        (name.casefold(), value.strip())
        for name, value in attributes
        if name.casefold() != "type" and value.strip()
    ]
    specific = bool(ids or classes or strict_attributes or literal)

    return {
        "specific": specific,
        "ids": ids,
        "classes": classes,
        "attributes": strict_attributes,
        "literal": literal,
    }


def _find_web_input(window, selector=""):
    """
    在当前网页中寻找可见、可聚焦、非只读的输入框。

    定位原则：
    1. 排除浏览器地址栏、不可见、不可聚焦、只读和无有效坐标的控件。
    2. 对 #id、.class、name、placeholder 等提示执行优先匹配。
    3. 指定了具体选择器却没有匹配时，不回退到任意输入框，防止误输入。
    4. 多个候选优先级相同则报错，不猜测目标。
    """
    selector = (selector or "").strip()
    selector_lower = selector.casefold()
    hints = _parse_web_input_selector(selector)
    candidates = []

    for control, depth in _walk_web_controls(window, max_depth=32, max_nodes=10000):
        try:
            if control.ControlTypeName != "EditControl":
                continue

            name = (control.Name or "").strip()
            class_name = (control.ClassName or "").strip()
            automation_id = (control.AutomationId or "").strip()

            # 排除浏览器地址栏，避免将搜索词误输入地址栏。
            if (
                "omnibox" in class_name.casefold()
                or name in ("地址和搜索栏", "Address and search bar")
            ):
                continue

            if not control.IsEnabled or control.IsOffscreen or not control.IsKeyboardFocusable:
                continue
            if not _control_rect_is_valid(control):
                continue

            pattern = _get_value_pattern(control)
            if pattern is None or pattern.IsReadOnly:
                continue

            name_lower = name.casefold()
            class_lower = class_name.casefold()
            id_lower = automation_id.casefold()
            combined = f"{name} {class_name} {automation_id}".casefold()

            # selector_score 只记录选择器本身的匹配程度；
            # heuristic_score 记录搜索框名称等辅助特征，不能替代具体选择器匹配。
            selector_score = 0
            heuristic_score = 0

            for item in hints["ids"]:
                item_lower = item.casefold()
                if id_lower == item_lower:
                    selector_score += 1200
                elif item_lower in id_lower:
                    selector_score += 900

            for item in hints["classes"]:
                item_lower = item.casefold()
                if item_lower in class_lower:
                    selector_score += 700

            for attr_name, attr_value in hints["attributes"]:
                value_lower = attr_value.casefold()
                if attr_name == "id":
                    if id_lower == value_lower:
                        selector_score += 1200
                    elif value_lower in id_lower:
                        selector_score += 900
                elif value_lower in combined:
                    selector_score += 500

            if hints["literal"]:
                literal_lower = hints["literal"].casefold()
                if literal_lower in combined:
                    selector_score += 300

            # 对 textarea 这种类型提示做弱匹配；UIA 的 EditControl 本身
            # 不一定能区分 HTML input 与 textarea。
            if selector_lower == "textarea" and "textarea" in class_lower:
                selector_score += 100
            elif selector_lower == "input" and "input" in class_lower:
                selector_score += 100

            if any(
                token in combined
                for token in ("search", "搜索", "keyword", "query", "kw")
            ):
                heuristic_score += 40
            if "textarea" in class_lower:
                heuristic_score += 20
            if "输入搜索词" in name:
                heuristic_score += 35
            if id_lower == "q":
                heuristic_score += 35

            total_score = selector_score + heuristic_score
            candidates.append((
                total_score,
                selector_score,
                control,
                {
                    "name": name,
                    "class_name": class_name,
                    "automation_id": automation_id,
                    "depth": depth,
                    "selector_score": selector_score,
                    "total_score": total_score,
                },
            ))
        except Exception:
            # 单个 UIA 节点读取失败不应中断整棵控件树扫描。
            continue

    if not candidates:
        documents = _find_document_controls(window)
        raise RuntimeError(
            "未找到可交互的网页输入框。"
            f"当前找到 {len(documents)} 个 DocumentControl，"
            "但网页文档树中没有满足条件的 EditControl。"
            "请检查输入框是否位于 iframe / 特殊编辑器中，或网页是否向 UIA 暴露了输入控件。"
        )

    # HTML 的 id/class 不一定映射为 UIA 的 AutomationId/ClassName。
    # 如果具体选择器未命中，但网页文档树中只有一个可交互输入框，
    # 则允许选择这个唯一候选；存在多个候选时仍然拒绝猜测。
    if hints["specific"]:
        matched = [item for item in candidates if item[1] > 0]
        if matched:
            candidates = matched
        elif len(candidates) == 1:
            logger.warning(
                "UIA 未暴露选择器 %r 对应的属性，改用唯一可交互输入框：%s",
                selector,
                candidates[0][3],
            )
        else:
            details = [item[3] for item in candidates[:8]]
            raise RuntimeError(
                f"网页输入框未匹配指定选择器 {selector!r}，"
                f"且存在 {len(candidates)} 个可交互候选，无法安全选择。"
                f"候选控件：{details}"
            )

    candidates.sort(key=lambda item: item[0], reverse=True)

    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        details = [item[3] for item in candidates[:8]]
        raise RuntimeError(
            f"发现多个同优先级网页输入框，无法安全确定目标。"
            f"请提供更具体的 CSS Selector。候选控件：{details}"
        )

    return candidates[0][2]


def _wait_for_web_input(
    window,
    selector="",
    timeout=WEB_INPUT_WAIT_TIMEOUT,
    poll_interval=WEB_INPUT_POLL_INTERVAL,
):
    """
    动态等待网页输入框出现。

    页面导航完成不代表网页控件树已经加载完成，因此在超时时间内
    周期性重新扫描。始终无法唯一定位时返回带有页面信息的诊断错误。
    """
    start_time = time.monotonic()
    deadline = start_time + timeout
    attempts = 0
    last_error = "尚未执行控件扫描"

    while time.monotonic() < deadline:
        attempts += 1
        try:
            control = _find_web_input(window, selector)
            elapsed = time.monotonic() - start_time

            logger.info(
                "网页输入框定位成功：耗时=%.2f秒，扫描次数=%d，"
                "Name=%r，ClassName=%r，AutomationId=%r",
                elapsed,
                attempts,
                control.Name,
                control.ClassName,
                control.AutomationId,
            )
            return control

        except RuntimeError as exc:
            last_error = str(exc)
            logger.debug(
                "网页输入框暂未就绪：第 %d 次扫描，原因：%s",
                attempts,
                last_error,
            )

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval, remaining))

    elapsed = time.monotonic() - start_time
    try:
        current_url = _read_browser_url(window)
    except Exception:
        current_url = ""

    try:
        window_title = window.Name or ""
    except Exception:
        window_title = ""

    message = (
        f"等待网页输入框超时：等待 {elapsed:.1f} 秒，扫描 {attempts} 次。"
        f"页面标题：{window_title!r}；当前网址：{current_url or '无法读取'}；"
        f"最后一次定位结果：{last_error}"
    )
    logger.warning(message)
    raise TimeoutError(message)


def _input_text_into_control(window, control, text):
    """聚焦输入框、粘贴文本并通过 ValuePattern 回读验证。"""
    auto = _get_uia()
    control.SetFocus()
    time.sleep(0.2)

    focused = auto.GetFocusedControl()
    try:
        if focused.ProcessId != window.ProcessId:
            raise RuntimeError("输入框聚焦后，键盘焦点不属于目标浏览器进程")
        if focused.ControlTypeName != "EditControl":
            raise RuntimeError(f"输入框聚焦失败，当前焦点类型为：{focused.ControlTypeName}")
        # 进一步核对焦点控件身份，避免误把文本粘贴到浏览器地址栏或其他输入框。
        expected_class = (control.ClassName or "").strip()
        actual_class = (focused.ClassName or "").strip()
        expected_id = (control.AutomationId or "").strip()
        actual_id = (focused.AutomationId or "").strip()
        expected_name = (control.Name or "").strip()
        actual_name = (focused.Name or "").strip()

        if expected_class and actual_class != expected_class:
            raise RuntimeError("键盘焦点 ClassName 与目标输入框不一致")
        if expected_id and actual_id != expected_id:
            raise RuntimeError("键盘焦点 AutomationId 与目标输入框不一致")
        if expected_name and actual_name and actual_name != expected_name:
            raise RuntimeError("键盘焦点 Name 与目标输入框不一致")
    except AttributeError:
        raise RuntimeError("无法验证输入框焦点")

    auto.SendKeys("{Ctrl}a")
    _paste_text(text)
    time.sleep(0.35)

    # 某些网页的 UIA ValuePattern 更新存在短暂延迟，短时间轮询后再判定输入失败。
    readback_deadline = time.monotonic() + 2.0
    actual = ""
    while time.monotonic() < readback_deadline:
        pattern = _get_value_pattern(control)
        actual = str(pattern.Value if pattern is not None else "")
        if actual == str(text):
            return actual
        time.sleep(0.1)

    raise RuntimeError(
        f"输入内容回读不一致，预期：{text!r}；实际：{actual!r}。"
        "请检查网页是否接受键盘输入，以及 UIA ValuePattern 是否正确暴露。"
    )


def _find_document_controls(window):
    """收集网页区域暴露的 DocumentControl。"""
    docs = []
    for control, _ in _walk_uia_controls(window, max_depth=24, max_nodes=6000):
        try:
            if control.ControlTypeName == "DocumentControl":
                docs.append(control)
        except Exception:
            continue
    return docs



def _walk_web_controls(window, max_depth=32, max_nodes=10000):
    """
    遍历网页内部控件，优先从 DocumentControl 子树开始。

    浏览器窗口包含标签栏、工具栏和扩展按钮等大量浏览器自身控件。
    正文提取已经证明 DocumentControl 可以被找到，因此网页输入框和
    点击目标应直接在 DocumentControl 内部扫描，避免窗口级遍历预算
    被浏览器外壳控件消耗。
    """
    documents = _find_document_controls(window)

    if not documents:
        logger.warning(
            "当前浏览器窗口没有找到 DocumentControl，网页元素定位退化为窗口级扫描"
        )
        yield from _walk_uia_controls(
            window,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        return

    logger.debug("网页控件扫描：找到 %d 个 DocumentControl", len(documents))

    for doc_index, document in enumerate(documents, start=1):
        try:
            logger.debug(
                "开始扫描第 %d 个网页文档：Name=%r，ProcessId=%s",
                doc_index,
                document.Name,
                document.ProcessId,
            )
        except Exception:
            pass

        yield from _walk_uia_controls(
            document,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )


def _collect_text_from_root(root, max_chars=12000):
    """提取一个文档节点下的正文、链接文本和列表文本，并去重。"""
    texts = []
    seen = set()
    for control, _ in _walk_uia_controls(root, max_depth=32, max_nodes=8000):
        try:
            control_type = control.ControlTypeName
            if control_type not in ("TextControl", "HyperlinkControl", "ListItemControl"):
                continue
            value = (control.Name or "").strip()
            if not value or value in seen:
                continue
            # 排除空白和浏览器框架标题，但保留网页正文短文本。
            if value in ("无标题 - 用户配置 1 - Microsoft Edge", "在拆分屏幕中打开"):
                continue
            seen.add(value)
            texts.append(value)
            if sum(len(x) for x in texts) >= max_chars:
                break
        except Exception:
            continue
    return "\n".join(texts).strip()


def _selector_matches_uia(control, selector):
    """把少量常见 CSS 风格选择器转换为 UIA 属性提示，不执行 CSS。"""
    selector = (selector or "").strip()
    if not selector or selector.casefold() in ("body", "html", "*", "document"):
        return True
    try:
        automation_id = (control.AutomationId or "").strip()
        class_name = (control.ClassName or "").strip()
        name = (control.Name or "").strip()
    except Exception:
        return False
    if selector.startswith("#"):
        return automation_id.casefold() == selector[1:].casefold()
    if selector.startswith("."):
        return selector[1:].casefold() in class_name.casefold()
    hint = selector.casefold()
    return hint in automation_id.casefold() or hint in class_name.casefold() or hint in name.casefold()


def _extract_document_content(window, selector="body", wait_timeout=10):
    """等待网页文档树出现并提取正文。"""
    deadline = time.monotonic() + wait_timeout
    best_content = ""
    while time.monotonic() < deadline:
        docs = _find_document_controls(window)
        document_candidates = []
        for doc in docs:
            # 先尝试按 #id / .class 等提示缩小范围；UIA 不提供 CSS 查询能力。
            selected_roots = []
            if selector and selector.casefold() not in ("body", "html", "*", "document"):
                for control, _ in _walk_uia_controls(doc, max_depth=28, max_nodes=6000):
                    if control is not doc and _selector_matches_uia(control, selector):
                        selected_roots.append(control)
            if selected_roots:
                parts = [_collect_text_from_root(item) for item in selected_roots]
                content = "\n".join(x for x in parts if x).strip()
            else:
                content = _collect_text_from_root(doc)
            if content:
                document_candidates.append(content)

        if document_candidates:
            best_content = max(document_candidates, key=len)
            if len(best_content) >= 80:
                break
        time.sleep(0.5)

    # 某些网页不暴露 DocumentControl，退化为从窗口中收集文本控件。
    if not best_content:
        best_content = _collect_text_from_root(window)
    return best_content


def _find_click_target(window, keyword_or_selector):
    """按 AutomationId、ClassName 或可见文字定位网页按钮/链接。"""
    query = (keyword_or_selector or "").strip()
    if not query:
        raise ValueError("点击目标不能为空")

    matches = []
    exact_text_matches = []
    allowed_types = {"ButtonControl", "HyperlinkControl", "TextControl", "ListItemControl", "MenuItemControl"}

    for control, depth in _walk_web_controls(window, max_depth=32, max_nodes=10000):
        try:
            control_type = control.ControlTypeName
            if control_type not in allowed_types:
                continue
            if not control.IsEnabled or control.IsOffscreen or not _control_rect_is_valid(control):
                continue
            name = (control.Name or "").strip()
            automation_id = (control.AutomationId or "").strip()
            class_name = (control.ClassName or "").strip()
            if not (name or automation_id or class_name):
                continue

            matched = False
            exact = False
            if query.startswith("#"):
                matched = automation_id.casefold() == query[1:].casefold()
                exact = matched
            elif query.startswith("."):
                matched = query[1:].casefold() in class_name.casefold()
            elif query.casefold() in (automation_id + " " + class_name).casefold():
                matched = True
            else:
                exact = name.casefold() == query.casefold()
                matched = exact or query.casefold() in name.casefold()

            if matched:
                item = (control, control_type, name, depth)
                matches.append(item)
                if exact:
                    exact_text_matches.append(item)
        except Exception:
            continue

    chosen = exact_text_matches if exact_text_matches else matches
    # 去除同一 UIA 元素可能出现的重复包装结果。
    unique = []
    seen = set()
    for item in chosen:
        key = (item[1], item[2], item[3], str(item[0].BoundingRectangle))
        if key not in seen:
            seen.add(key)
            unique.append(item)

    if not unique:
        raise RuntimeError(f"没有找到网页元素：{query}")
    if len(unique) > 1:
        # 优先选择按钮和超链接，避免点击纯文本节点。
        actionable = [x for x in unique if x[1] in ("ButtonControl", "HyperlinkControl", "MenuItemControl")]
        if len(actionable) == 1:
            return actionable[0][0]
        raise RuntimeError(f"找到多个匹配元素，请提供更具体的文字或 AutomationId：{[x[2] for x in unique[:8]]}")
    return unique[0][0]


def _wait_for_click_target(
    window,
    keyword_or_selector,
    timeout=8.0,
    poll_interval=0.4,
):
    """
    动态等待网页点击目标出现。

    仅对“暂未找到元素”进行重试；多匹配等安全错误不应被隐藏。
    """
    deadline = time.monotonic() + timeout
    last_error = None

    while time.monotonic() < deadline:
        try:
            return _find_click_target(window, keyword_or_selector)
        except RuntimeError as exc:
            last_error = exc
            if "没有找到网页元素" not in str(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(poll_interval, remaining))

    raise TimeoutError(
        f"等待网页元素超时：{keyword_or_selector!r}；最后一次定位结果：{last_error}"
    )


def _invoke_uia_control(control):
    """优先调用 InvokePattern；不支持时使用有效坐标执行 UIA 点击。"""
    try:
        control.GetInvokePattern().Invoke()
        return "InvokePattern"
    except Exception:
        if not _control_rect_is_valid(control):
            raise RuntimeError("目标控件不支持 InvokePattern，且没有有效屏幕坐标")
        control.Click(simulateMove=False)
        return "Click"


# ==================== UIA 浏览器自动化模块结束 ====================

def _scan_drives_for_file(filename):
    user_home=os.path.expanduser("~")
    search_dirs=[os.path.join(user_home,"Desktop"),os.path.join(user_home,"Documents"),os.path.join(user_home,"Downloads")]
    import string
    search_dirs.extend([f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")])
    start=time.time()
    visited=set()
    for base in search_dirs:
        base=os.path.abspath(base)
        if base in visited or not os.path.exists(base):
            continue
        visited.add(base)
        for root,dirs,files in os.walk(base):
            if time.time()-start>15:
                return None
            dirs[:]=[d for d in dirs if not d.startswith(".") and d.lower() not in {"windows","program files","program files (x86)","appdata","node_modules",".git","$recycle.bin","system volume information"}]
            for name in files:
                if name.startswith("~$"):
                    continue
                if filename.lower() in name.lower():
                    path=os.path.join(root,name)
                    return path,round(os.path.getsize(path)/(1024*1024),1)
    return None

@json_result
def open_vscode(project_path="."):
    candidates=[shutil.which("code"),r"C:\Program Files\Microsoft VS Code\Code.exe",r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",os.path.expanduser(r"~\AppData\Local\Programs\Microsoft VS Code\Code.exe")]
    for exe in candidates:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe,project_path],shell=False)
            return {"status":"success","message":"VSCode 已打开"}
    raise FileNotFoundError("未找到 VSCode")

@json_result
def open_wps(file_path=None):
    if file_path:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")
        os.startfile(file_path)
        return {"status":"success","message":f"文件已打开: {file_path}"}
    subprocess.Popen(["cmd","/c","start","","wps"],shell=False)
    return {"status":"success","message":"WPS 已打开"}

@json_result
def open_software(name):
    aliases={"微信":"WeChat","wechat":"WeChat","qq":"QQ","钉钉":"DingTalk","企业微信":"WXWork","计算器":"calc","记事本":"notepad","画图":"mspaint","任务管理器":"taskmgr","控制面板":"control","资源管理器":"explorer","命令提示符":"cmd","浏览器":"msedge","edge":"msedge","chrome":"chrome"}
    web_apps={"deepseek":"https://chat.deepseek.com","豆包":"https://www.doubao.com","kimi":"https://kimi.moonshot.cn","通义千问":"https://tongyi.aliyun.com","文心一言":"https://yiyan.baidu.com"}
    if name.lower() in web_apps:
        os.startfile(web_apps[name.lower()])
        return {"status":"success","message":f"{name} 已打开"}
    target=aliases.get(name.lower(),aliases.get(name,name))
    try:
        subprocess.run(["cmd","/c","start","",target],timeout=5,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {"status":"success","message":f"{name} 已打开"}
    except Exception:
        pass
    found=shutil.which(target) or shutil.which(str(target).replace(".exe",""))
    if found:
        subprocess.Popen([found],shell=False)
        return {"status":"success","message":f"{name} 已打开"}
    raise FileNotFoundError(f"未找到软件: {name}")

@json_result
def find_latest_document():
    user_home=os.path.expanduser("~")
    search_dirs=[os.path.join(user_home,"Desktop"),os.path.join(user_home,"Documents"),os.path.join(user_home,"Downloads")]
    extensions=("*.doc","*.docx","*.xls","*.xlsx","*.ppt","*.pptx","*.pdf")
    latest_file=None
    latest_time=0
    for directory in search_dirs:
        if not os.path.exists(directory):
            continue
        for ext in extensions:
            for path in glob.glob(os.path.join(directory,ext)):
                if os.path.basename(path).startswith("~$"):
                    continue
                mtime=os.path.getmtime(path)
                if mtime>latest_time:
                    latest_time=mtime
                    latest_file=path
    if not latest_file:
        raise FileNotFoundError("未找到最近修改的办公文档")
    return {"status":"success","message":"找到最新文档","file_path":latest_file}

@json_result
def close_software(app_name,force_execute=False):
    import ctypes
    tray_apps=("微信","wechat","qq","钉钉","dingtalk","企业微信","wxwork")
    if any(x in app_name.lower() for x in tray_apps):
        force_execute=True
    if force_execute:
        app_map={"wps":["wps.exe","wpp.exe","et.exe"],"vscode":["code.exe"],"微信":["WeChat.exe"],"企业微信":["WXWork.exe"],"qq":["QQ.exe"],"钉钉":["DingTalk.exe"],"浏览器":["msedge.exe","chrome.exe"],"chrome":["chrome.exe"],"edge":["msedge.exe"]}
        targets=next((v for k,v in app_map.items() if k.lower() in app_name.lower()),[app_name])
        if targets==[app_name] and not app_name.lower().endswith(".exe"):
            targets=[app_name+".exe"]
        for exe in targets:
            subprocess.run(f'taskkill /F /IM "{exe}" /T',shell=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {"status":"success","message":f"已结束 {app_name} 进程"}
    user32=ctypes.windll.user32
    matched=[]
    def callback(hwnd,extra):
        if user32.IsWindowVisible(hwnd):
            length=user32.GetWindowTextLengthW(hwnd)
            if length>0:
                buff=ctypes.create_unicode_buffer(length+1)
                user32.GetWindowTextW(hwnd,buff,length+1)
                if app_name.lower() in buff.value.lower():
                    matched.append(hwnd)
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)(callback),0)
    if not matched:
        raise RuntimeError(f"未找到 {app_name} 的运行窗口")
    for hwnd in matched:
        user32.PostMessageW(hwnd,0x0010,0,0)
    return {"status":"success","message":f"已向 {app_name} 发送安全关闭信号"}

@json_result
@_with_browser_uia
def list_browser_tabs(keyword="", browser_name="edge"):
    """只读列出浏览器窗口中的标签页标题；当前活动标签页附带可读取的 URL。"""
    auto = _get_uia()
    browser_name = _normalize_browser_name(browser_name)
    window = _ensure_browser_window(browser_name)
    current_url = _read_browser_url(window)
    tabs = []

    for control, _ in _walk_uia_controls(window, max_depth=18, max_nodes=4000):
        try:
            if control.ControlTypeName != "TabItemControl":
                continue
            title = (control.Name or "").strip()
            if not title:
                continue
            if keyword and keyword.casefold() not in title.casefold():
                continue
            try:
                active = bool(control.IsSelected)
            except Exception:
                active = False
            # 部分 Edge 版本不暴露 IsSelected，使用窗口标题做弱判断。
            if not active and title and title.split(" - 内存使用率")[0] in (window.Name or ""):
                active = True
            tabs.append({
                "title": title,
                "url": current_url if active else None,
                "active": active,
            })
        except Exception:
            continue

    return {"status": "success", "browser": browser_name, "window_title": window.Name, "tabs": tabs}


@json_result
@_with_browser_uia
def open_website(url, browser_name="edge"):
    """使用 UIA 启动或复用浏览器，并在当前空白页或新标签页打开指定网址。"""
    browser_name = _normalize_browser_name(browser_name)
    url = _format_url(url)
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"无效网址：{url}")

    window = _ensure_browser_window(browser_name)
    actual_url, navigated = _open_url_in_browser(window, url)
    return {
        "status": "success",
        "message": "网页已打开" if navigated else "目标网页已经处于当前标签页",
        "url": actual_url,
        "window_title": window.Name,
        "browser": browser_name,
    }


@json_result
@_with_browser_uia
def smart_web_operate(url, css_selector="", keyword="", browser_name="edge"):
    """
    执行网页搜索或普通网页输入。

    对百度、Bing、Google、搜狗、360、Yahoo 等搜索引擎，直接构造搜索结果
    URL 并导航，不再依赖网页搜索框的 UIA 控件树。
    对 DeepSeek 等普通网页，保留原有 UIA 输入框定位和键盘提交逻辑。
    css_selector 保留为可选参数，兼容旧版本工具调用。
    """
    browser_name = _normalize_browser_name(browser_name)
    url = _format_url(url)
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"无效网址：{url}")
    keyword = str(keyword).strip()
    if not keyword:
        raise ValueError("搜索关键词不能为空")

    window = _ensure_browser_window(browser_name)

    # 搜索引擎走直接 URL 导航，避免等待 UIA 搜索框导致超时。
    search_url = _build_search_url(url, keyword)
    if search_url:
        actual_url, navigated = _open_url_in_browser(window, search_url)
        return {
            "status": "success",
            "message": f"已直接打开搜索结果：{keyword}",
            "mode": "direct_search_url",
            "search_url": search_url,
            "current_url": actual_url,
            "navigated": navigated,
            "window_title": window.Name,
        }

    # 非搜索引擎网页仍使用原有 UIA 输入流程，例如聊天网站的输入框。
    auto = _get_uia()
    _open_url_in_browser(window, url)

    # 确保目标浏览器窗口处于前台，再动态等待网页输入框进入 UIA 控件树。
    window.SetFocus()
    control = _wait_for_web_input(
        window,
        selector=css_selector,
        timeout=WEB_INPUT_WAIT_TIMEOUT,
        poll_interval=WEB_INPUT_POLL_INTERVAL,
    )

    input_value = _input_text_into_control(window, control, keyword)

    # 输入内容已通过 ValuePattern 回读验证，再提交网页表单。
    auto.SendKeys("{Enter}")
    time.sleep(2.0)
    current_url = _read_browser_url(window)

    return {
        "status": "success",
        "message": f"已在 {url} 输入并提交内容：{keyword}",
        "mode": "uia_input",
        "input_value": input_value,
        "current_url": current_url,
        "window_title": window.Name,
    }


@json_result
@_with_browser_uia
def extract_web_content(url, css_selector="body", browser_name="edge"):
    """通过 UIA DocumentControl 提取网页正文；CSS 风格选择器仅作为控件属性匹配提示。"""
    browser_name = _normalize_browser_name(browser_name)
    url = _format_url(url) if url else ""
    window = _ensure_browser_window(browser_name)

    if url:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"无效网址：{url}")
        _open_url_in_browser(window, url)

    content = _extract_document_content(window, css_selector or "body", wait_timeout=12)
    if not content:
        raise RuntimeError("UIA 未能从当前网页提取到正文。页面可能尚未加载，或网站没有向辅助功能树暴露正文内容。")

    if len(content) > 3000:
        content = content[:3000] + "\n...[内容过长已截断]"

    return {
        "status": "success",
        "content": content,
        "url": _read_browser_url(window),
        "window_title": window.Name,
    }


@json_result
@_with_browser_uia
def click_web_element(url, keyword_or_selector, browser_name="edge"):
    """在目标网页中按可见文字、AutomationId(#id) 或 ClassName(.class) 点击元素。"""
    browser_name = _normalize_browser_name(browser_name)
    url = _format_url(url) if url else ""
    window = _ensure_browser_window(browser_name)

    if url:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"无效网址：{url}")
        _open_url_in_browser(window, url)

    # 从网页 DocumentControl 子树动态等待目标，避免只扫描浏览器外壳。
    control = _wait_for_click_target(
        window,
        keyword_or_selector,
        timeout=8.0,
        poll_interval=0.4,
    )
    control_name = control.Name or keyword_or_selector
    action_method = _invoke_uia_control(control)
    time.sleep(1.0)

    return {
        "status": "success",
        "message": f"已点击网页元素：{control_name}",
        "method": action_method,
        "current_url": _read_browser_url(window),
        "window_title": window.Name,
    }


@json_result
@_with_browser_uia
def close_website(keyword, browser_name="edge"):
    """根据标签页标题关键词定位并关闭唯一匹配的浏览器标签页。"""
    auto = _get_uia()
    browser_name = _normalize_browser_name(browser_name)
    keyword = (keyword or "").strip()
    if not keyword:
        raise ValueError("请提供要关闭的标签页标题或网址关键词")

    window = _ensure_browser_window(browser_name)
    tabs = []
    for control, _ in _walk_uia_controls(window, max_depth=18, max_nodes=4000):
        try:
            if control.ControlTypeName != "TabItemControl":
                continue
            title = (control.Name or "").strip()
            if title and keyword.casefold() in title.casefold():
                tabs.append((control, title))
        except Exception:
            continue

    if not tabs:
        current_url = _read_browser_url(window)
        if keyword.casefold() in current_url.casefold():
            # 当前活动页 URL 匹配，但 UIA 未暴露可匹配的 TabItem 时，关闭当前标签页。
            window.SetFocus()
            auto.SendKeys("{Ctrl}w")
            return {"status": "success", "message": f"已关闭当前标签页：{current_url}"}
        raise ValueError(f"没有找到包含“{keyword}”的标签页")

    if len(tabs) > 1:
        raise ValueError(f"关键词匹配到多个标签页，请提供更具体的标题：{[item[1] for item in tabs]}")

    tab_control, title = tabs[0]
    tab_control.Click(simulateMove=False)
    time.sleep(0.25)
    window.SetFocus()
    auto.SendKeys("{Ctrl}w")
    time.sleep(0.5)

    return {"status": "success", "message": f"已关闭标签页：{title}"}


@json_result
def get_screenshot():
    from PIL import ImageGrab
    save_path=os.path.join(os.getcwd(),"current_screenshot.png")
    ImageGrab.grab().save(save_path)
    result=json.loads(send_file_to_qq(save_path))
    if result.get("status")!="success":
        raise RuntimeError(result.get("message","截图发送失败"))
    return {"status":"success","message":"截图已通过 QQ 发送"}

@json_result
def read_clipboard():
    import pyperclip
    text=pyperclip.paste()
    return {"status":"success","content":text if str(text).strip() else "剪贴板为空"}

@json_result
def write_clipboard(text):
    import pyperclip
    pyperclip.copy(text)
    return {"status":"success","message":"文本已写入剪贴板"}

@json_result
def set_reminder(time_str,content):
    match=re.match(r"^(\d+)([smh])$",time_str.strip().lower())
    if not match:
        raise ValueError("时间格式错误，例如 10s、5m、1h")
    value,unit=match.groups()
    seconds=int(value)*{"s":1,"m":60,"h":3600}[unit]
    api=_qq_api.get()
    loop=_qq_loop.get()
    user_id=_qq_user_id.get()
    if not api or not loop or not user_id:
        raise RuntimeError("QQ 运行上下文未设置")
    def on_timer():
        coro=api.post_c2c_message(openid=user_id,msg_type=0,content=f"⏰ 提醒：{content}")
        asyncio.run_coroutine_threadsafe(coro,loop)
    threading.Timer(seconds,on_timer).start()
    return {"status":"success","message":f"已设定 {time_str} 后提醒"}

@json_result
def find_and_open_file(filename,auto_open=True):
    result=_scan_drives_for_file(filename)
    if result is None:
        raise FileNotFoundError(f"未找到文件: {filename}")
    full_path,size_mb=result
    if auto_open:
        os.startfile(full_path)
        return {"status":"success","message":f"已打开文件 {filename}","file_path":full_path}
    return {"status":"info","message":"文件已找到","file_path":full_path,"size_mb":size_mb}

@json_result
def send_file_to_email(filepath,target_email=None):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")
    sender=os.getenv("SENDER_EMAIL")
    auth_code=os.getenv("EMAIL_AUTH_CODE")
    receiver=target_email or os.getenv("RECEIVER_EMAIL")
    if not sender or not auth_code or not receiver:
        raise ValueError("缺少 SENDER_EMAIL / EMAIL_AUTH_CODE / RECEIVER_EMAIL 配置")
    smtp_servers={"qq.com":"smtp.qq.com","foxmail.com":"smtp.qq.com","163.com":"smtp.163.com","126.com":"smtp.126.com"}
    domain=sender.split("@")[-1].lower()
    smtp_host=smtp_servers.get(domain)
    if not smtp_host:
        raise ValueError(f"暂不支持邮箱域名: {domain}")
    filename=os.path.basename(filepath)
    msg=EmailMessage()
    msg["Subject"]=f"QQ Agent 文件：{filename}"
    msg["From"]=sender
    msg["To"]=receiver
    msg.set_content("附件是您需要的文件。")
    ctype,_=mimetypes.guess_type(filepath)
    maintype,subtype=(ctype or "application/octet-stream").split("/",1)
    with open(filepath,"rb") as f:
        msg.add_attachment(f.read(),maintype=maintype,subtype=subtype,filename=filename)
    with smtplib.SMTP_SSL(smtp_host,465,timeout=20) as server:
        server.login(sender,auth_code)
        server.send_message(msg)
    return {"status":"success","message":f"文件 {filename} 已发送至 {receiver}"}

@json_result
def send_file_to_qq(filepath):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")
    size_mb=os.path.getsize(filepath)/(1024*1024)
    if size_mb>5:
        raise ValueError(f"文件 {size_mb:.1f}MB 超过 QQ 5MB 限制")
    api=_qq_api.get()
    loop=_qq_loop.get()
    user_id=_qq_user_id.get()
    if not api or not loop or not user_id:
        raise RuntimeError("QQ 运行上下文未设置")
    ext=os.path.splitext(filepath)[1].lower()
    if ext in (".png",".jpg",".jpeg",".gif",".bmp",".webp"):
        file_type=1
    elif ext in (".mp4",".avi",".mov",".wmv"):
        file_type=2
    elif ext in (".mp3",".wav",".aac",".silk"):
        file_type=3
    else:
        raise ValueError(f"QQ 直传暂不支持 {ext}，请改用邮件")
    with open(filepath,"rb") as f:
        data=base64.b64encode(f.read()).decode("utf-8")
    async def _send():
        from botpy.http import Route
        route=Route("POST","/v2/users/{openid}/files",openid=user_id)
        await api._http.request(route,json={"file_type":file_type,"file_data":data,"srv_send_msg":True})
    future=asyncio.run_coroutine_threadsafe(_send(),loop)
    future.result(timeout=30)
    return {"status":"success","message":f"文件 {os.path.basename(filepath)} 已通过 QQ 发送"}

@json_result
def manage_window(keyword=None,action="maximize",window_index=0):
    import ctypes
    import psutil
    user32=ctypes.windll.user32
    windows=[]
    def process_name(hwnd):
        pid=ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        try:
            return psutil.Process(pid.value).name().lower()
        except Exception:
            return ""
    def callback(hwnd,extra):
        if user32.IsWindowVisible(hwnd):
            length=user32.GetWindowTextLengthW(hwnd)
            if length>0:
                buff=ctypes.create_unicode_buffer(length+1)
                user32.GetWindowTextW(hwnd,buff,length+1)
                windows.append({"hwnd":hwnd,"title":buff.value,"process":process_name(hwnd)})
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)(callback),0)
    if not keyword:
        return {"status":"info","windows":[f"{x['title']} | {x['process']}" for x in windows[:100]]}
    matched=[x for x in windows if keyword.lower() in x["title"].lower() or keyword.lower() in x["process"]]
    if not matched:
        raise ValueError(f"未找到窗口: {keyword}")
    target=matched[window_index if 0<=window_index<len(matched) else 0]
    if action=="maximize":
        user32.ShowWindow(target["hwnd"],9)
        user32.ShowWindow(target["hwnd"],3)
        user32.SetForegroundWindow(target["hwnd"])
    elif action=="minimize":
        user32.ShowWindow(target["hwnd"],6)
    else:
        raise ValueError(f"不支持的窗口动作: {action}")
    return {"status":"success","message":f"窗口已{action}: {target['title']}"}

@json_result
@_with_browser_uia
def inspect_window_ui(keyword):
    """
    检查浏览器网页区域实际暴露给 UIA 的控件。

    旧实现只扫描窗口前 5 层，并且只统计少数控件类型，因此通常只能看到
    Edge 的最小化、最大化、关闭等窗口按钮，不能据此判断网页 DOM 是否可访问。
    新实现复用网页 DocumentControl 子树遍历逻辑，输出真实网页控件诊断信息。
    """
    auto = _get_uia()
    keyword = (keyword or "").strip().casefold()
    if not keyword:
        raise ValueError("请提供浏览器窗口标题关键词")

    matched_windows = []
    for window in auto.GetRootControl().GetChildren():
        try:
            if (
                window.ControlTypeName == "WindowControl"
                and keyword in (window.Name or "").casefold()
            ):
                matched_windows.append(window)
        except Exception:
            continue

    if not matched_windows:
        raise ValueError(f"没有找到包含关键词 {keyword!r} 的窗口")

    window = matched_windows[0]
    try:
        window.SetFocus()
    except Exception:
        pass

    documents = _find_document_controls(window)
    type_counts = {}
    interactive_elements = []
    allowed_types = {
        "ButtonControl",
        "EditControl",
        "HyperlinkControl",
        "ListItemControl",
        "MenuItemControl",
        "TextControl",
        "DocumentControl",
    }

    for control, depth in _walk_web_controls(
        window,
        max_depth=32,
        max_nodes=10000,
    ):
        try:
            control_type = control.ControlTypeName
            type_counts[control_type] = type_counts.get(control_type, 0) + 1

            if control_type not in allowed_types:
                continue

            name = (control.Name or "").strip()
            automation_id = (control.AutomationId or "").strip()
            class_name = (control.ClassName or "").strip()
            if not (name or automation_id or class_name):
                continue

            try:
                rect = control.BoundingRectangle
                rect_info = {
                    "left": rect.left,
                    "top": rect.top,
                    "right": rect.right,
                    "bottom": rect.bottom,
                }
            except Exception:
                rect_info = None

            try:
                enabled = bool(control.IsEnabled)
            except Exception:
                enabled = None
            try:
                offscreen = bool(control.IsOffscreen)
            except Exception:
                offscreen = None
            try:
                focusable = bool(control.IsKeyboardFocusable)
            except Exception:
                focusable = None

            interactive_elements.append({
                "type": control_type,
                "name": name,
                "automation_id": automation_id,
                "class_name": class_name,
                "depth": depth,
                "enabled": enabled,
                "offscreen": offscreen,
                "keyboard_focusable": focusable,
                "bounds": rect_info,
            })

            if len(interactive_elements) >= 100:
                break
        except Exception:
            continue

    return {
        "status": "success",
        "window_name": window.Name,
        "document_count": len(documents),
        "control_type_counts": type_counts,
        "interactive_elements": interactive_elements,
        "message": (
            "已扫描网页 DocumentControl 子树"
            if documents
            else "未找到 DocumentControl，结果来自浏览器窗口级回退扫描"
        ),
    }

@json_result
def operate_window_ui(keyword,element_name,action="click",text_value=""):
    import ctypes
    import pyautogui
    import pyperclip
    import uiautomation as auto
    from concurrent.futures import ThreadPoolExecutor,TimeoutError as FutureTimeout
    def operate():
        ctypes.windll.ole32.CoInitialize(None)
        try:
            auto.uiautomation.SetGlobalSearchTimeout(3)
            win=next((w for w in auto.GetRootControl().GetChildren() if w.ControlType==auto.ControlType.WindowControl and keyword.lower() in w.Name.lower()),None)
            if not win:
                return "找不到窗口"
            win.SetActive()
            time.sleep(0.5)
            control=next((c for c,_ in auto.WalkControl(win,maxDepth=6) if c.Name and element_name.lower() in c.Name.lower()),None)
            if not control:
                return "找不到元素"
            if action=="click":
                control.Click(simulateMove=False)
            elif action=="write":
                if control.ControlType==auto.ControlType.EditControl:
                    control.GetValuePattern().SetValue(text_value)
                else:
                    control.Click()
                    old=pyperclip.paste()
                    try:
                        pyperclip.copy(text_value)
                        pyautogui.hotkey("ctrl","v")
                    finally:
                        pyperclip.copy(old)
            else:
                raise ValueError(f"不支持的动作: {action}")
            return "ok"
        finally:
            ctypes.windll.ole32.CoUninitialize()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result=executor.submit(operate).result(timeout=15)
    except FutureTimeout:
        raise TimeoutError("窗口操作超时")
    if result!="ok":
        raise ValueError(result)
    return {"status":"success","message":f"已{action} {element_name}"}

@json_result
def read_text_file(filepath):
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"文件不存在: {filepath}")
    if os.path.getsize(filepath)>500*1024:
        raise ValueError("文件超过500KB")
    with open(filepath,"r",encoding="utf-8",errors="replace") as f:
        content=f.read()
    if len(content)>3000:
        content=content[:3000]+"\n...[内容过长已截断]"
    return {"status":"info","filepath":filepath,"content":content}

@json_result
def write_text_file(filepath,content):
    os.makedirs(os.path.dirname(filepath) or ".",exist_ok=True)
    with open(filepath,"w",encoding="utf-8") as f:
        f.write(content)
    return {"status":"success","message":f"文件已保存: {filepath}"}

@json_result
def rename_file(old_path,new_name):
    if not os.path.exists(old_path):
        raise FileNotFoundError(f"原文件不存在: {old_path}")
    new_path=os.path.join(os.path.dirname(old_path),new_name)
    if os.path.exists(new_path):
        raise FileExistsError(f"目标文件已存在: {new_path}")
    os.rename(old_path,new_path)
    return {"status":"success","message":f"已重命名为 {new_name}","new_path":new_path}

@json_result
def press_keys(keys,delay=0.5):
    import pyautogui
    time.sleep(delay)
    if "+" in keys:
        pyautogui.hotkey(*[x.strip() for x in keys.split("+")])
    else:
        pyautogui.press(keys)
    return {"status":"success","message":f"已执行按键: {keys}"}

@json_result
def call_vscode_claude(task):
    import ctypes
    import pyautogui
    import pyperclip
    user32=ctypes.windll.user32
    hwnd_found=0
    def callback(hwnd,extra):
        nonlocal hwnd_found
        if user32.IsWindowVisible(hwnd):
            length=user32.GetWindowTextLengthW(hwnd)
            if length>0:
                buff=ctypes.create_unicode_buffer(length+1)
                user32.GetWindowTextW(hwnd,buff,length+1)
                if "Visual Studio Code" in buff.value or "Trae" in buff.value:
                    hwnd_found=hwnd
                    return False
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)(callback),0)
    if not hwnd_found:
        raise RuntimeError("未找到运行中的 VS Code 窗口")
    user32.ShowWindow(hwnd_found,9 if user32.IsIconic(hwnd_found) else 5)
    user32.SetForegroundWindow(hwnd_found)
    pyautogui.press("esc",presses=3,interval=0.1)
    pyautogui.press("f1")
    time.sleep(0.8)
    old=pyperclip.paste()
    try:
        pyperclip.copy("Claude Code: Focus input")
        pyautogui.hotkey("ctrl","v")
        pyautogui.press("enter")
        time.sleep(2.5)
        pyperclip.copy(task)
        pyautogui.hotkey("ctrl","v")
        pyautogui.press("enter")
    finally:
        pyperclip.copy(old)
    return {"status":"success","message":"任务已投递给 Claude"}

@json_result
def click_mouse(clicks=1,button="left"):
    import pyautogui
    pyautogui.click(clicks=clicks,button=button)
    return {"status":"success","message":f"已{button}键点击{clicks}次"}

@json_result
def scroll_mouse(clicks=-1):
    import pyautogui
    pyautogui.scroll(clicks)
    return {"status":"success","message":f"已滚动 {clicks} 步"}


@json_result
def read_task_original(task_id, max_chars=4000):
    """读取当前任务的原始请求全文。

    安全约束：**只能读当前任务**。task_id 来自模型输出，不可信；归属由执行器
    通过 bind_task_context 绑定，这里做双向校验（任务 ID + 用户 ID）。
    否则任意用户可用别人的 task_id 读到他人任务原文。
    """
    from core.task_manager import TaskManager
    from core.session_manager import DB_PATH

    if not task_id or not str(task_id).strip():
        raise ValueError("task_id 不能为空")

    bound = current_task_context()
    if not bound.get("task_id"):
        return {"status":"error","message":"当前没有绑定任务，拒绝回查原文"}
    if str(task_id).strip() != bound["task_id"]:
        return {"status":"error","message":"只能回查当前任务的原文"}

    task = TaskManager(DB_PATH).get_task(bound["task_id"])
    if not task:
        return {"status":"error","message":"找不到当前任务"}
    owner = str(task.get("user_id") or "")
    if bound.get("user_id") and owner and owner != bound["user_id"]:
        return {"status":"error","message":"任务归属与当前用户不一致，拒绝回查"}

    original = str(task.get("description") or "")
    text = original
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[原文过长已截断]"
    return {"status":"ok","task_id":bound["task_id"],"chars":len(original),
            "truncated":len(original) > max_chars,"description":text}

