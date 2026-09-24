"""配置文件：把常用设置写进 ``mediaharvest.toml``，一次设定反复使用。

配置文件默认放在**项目根目录**（``mediaharvest.toml``），跟着项目走，
随时可看可改，也能提交到版本控制。

生成方式（任选）::

    ./mh --init-config          # 生成带注释的完整模板（推荐先做这个）
    ./mh --show-config          # 查看当前生效的配置
    ./mh --set-out-dir ~/Downloads/media

优先级（从高到低）::

    命令行参数  >  环境变量  >  配置文件  >  内置默认值

也就是说，配置文件提供默认值，命令行随时可以覆盖它。

支持的全部配置项（详见 :data:`CONFIG_TEMPLATE`）::

    [download]     下载目录、并发数、体积限制、是否分目录
    [web]          Web 界面的端口、监听地址
    [crawl]        渲染策略、yt-dlp 策略、超时、等待时间
    [network]      代理、User-Agent、Cookie、重试
    [advanced]     HLS 分片并发、分片上限、转封装开关

约定：

* ``~``、``$VAR`` / ``${VAR}`` 会被展开；
* 相对路径按**运行时的工作目录**解析（与内置默认值 ``downloads`` 一致）；
* 未识别的配置项会被原样保留，保存时不会丢失；
* 类型写错时**不会崩溃**——记录一条警告并使用默认值。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:  # Python 3.11+
    import tomllib as _toml
    _TOML_ERRORS: tuple = (_toml.TOMLDecodeError,)
except ModuleNotFoundError:  # pragma: no cover - 取决于解释器版本
    try:
        import tomli as _toml  # type: ignore[no-redef]

        _TOML_ERRORS = (_toml.TOMLDecodeError,)
    except ModuleNotFoundError:  # pragma: no cover
        _toml = None  # type: ignore[assignment]
        _TOML_ERRORS = ()

__all__ = [
    "AppConfig",
    "CONFIG_TEMPLATE",
    "ConfigError",
    "DEFAULT_OUT_DIR",
    "DEFAULTS",
    "ENV_CONFIG",
    "ENV_OUT_DIR",
    "PROJECT_CONFIG_NAME",
    "project_config_path",
    "project_root",
    "config_candidates",
    "describe_config",
    "effective_out_dir",
    "get",
    "load_config",
    "parse_out_dir",
    "resolve_config_path",
    "save_config",
    "toml_available",
    "user_config_path",
    "write_template",
]

#: 没配置过任何东西时的下载目录
DEFAULT_OUT_DIR = "downloads"
#: 指定配置文件的环境变量
ENV_CONFIG = "MEDIAHARVEST_CONFIG"
#: 直接指定下载目录的环境变量（优先于配置文件）
ENV_OUT_DIR = "MEDIAHARVEST_OUT_DIR"
#: 项目级配置文件名
PROJECT_CONFIG_NAME = "mediaharvest.toml"

#: 环境变量覆盖：``配置键 -> 环境变量名``。
#: 只对「不方便改命令行」的场景提供（如服务化部署时的端口）。
ENV_OVERRIDES: Dict[str, str] = {
    "web.port": "MEDIAHARVEST_PORT",
    "web.host": "MEDIAHARVEST_HOST",
    "download.out_dir": ENV_OUT_DIR,
    "network.proxy": "MEDIAHARVEST_PROXY",
}

#: 所有配置项的默认值。``None`` 表示「不设置」。
DEFAULTS: Dict[str, Any] = {
    # 下载
    "download.out_dir": DEFAULT_OUT_DIR,
    "download.concurrency": 8,
    "download.hls_concurrency": 8,
    "download.max_size": "",
    "download.min_size": "",
    "download.overwrite": False,
    "download.flat": False,
    "download.keep_ts": False,
    # Web
    "web.host": "127.0.0.1",
    "web.port": 8848,
    "web.open_browser": True,
    # 抓取
    "crawl.render": "auto",
    "crawl.use_ytdlp": "auto",
    "crawl.timeout": 20.0,
    "crawl.retries": 2,
    "crawl.wait_after_load": 1.5,
    "crawl.browser_timeout": 30.0,
    "crawl.scroll": True,
    "crawl.headless": True,
    "crawl.depth": 0,
    "crawl.max_pages": 1,
    "crawl.same_host_only": False,
    "crawl.types": "image,video",
    # 网络
    "network.proxy": "",
    "network.user_agent": "",
    "network.cookie": "",
    "network.cookies_from_browser": "",
    # 高级
    "advanced.max_segments": 20000,
    "advanced.remux": True,
}

#: ``配置键 -> (中文说明, 单位/取值提示)``，用于生成模板与错误提示
SCHEMA: Dict[str, Tuple[str, str]] = {
    "download.out_dir": ("下载保存目录", "绝对路径或相对路径，支持 ~ 和 $VAR"),
    "download.concurrency": ("普通文件下载并发数", "1-32"),
    "download.hls_concurrency": ("HLS 分片下载并发数", "1-32"),
    "download.max_size": ("单文件体积上限，超过则跳过", "如 \"200M\"，留空不限"),
    "download.min_size": ("单文件体积下限，小于则跳过", "如 \"5K\""),
    "download.overwrite": ("覆盖已存在的同名文件", "true / false"),
    "download.flat": ("不按类型分目录，全部平铺", "true / false"),
    "download.keep_ts": ("流媒体转 MP4 后保留原始 .ts", "true / false"),
    "web.host": ("Web 界面监听地址", "127.0.0.1 仅本机；0.0.0.0 允许局域网"),
    "web.port": ("Web 界面端口", "1024-65535"),
    "web.open_browser": ("启动 Web 界面时自动打开浏览器", "true / false"),
    "crawl.render": ("浏览器渲染策略", "auto / always / never"),
    "crawl.use_ytdlp": ("yt-dlp 站点适配策略", "auto / always / never"),
    "crawl.timeout": ("HTTP 请求超时（秒）", "正数"),
    "crawl.retries": ("请求失败重试次数", "0-10"),
    "crawl.wait_after_load": ("渲染后额外等待（秒）", "用于等 JS 加载完"),
    "crawl.browser_timeout": ("浏览器页面加载超时（秒）", "正数"),
    "crawl.scroll": ("渲染时自动滚动触发懒加载", "true / false"),
    "crawl.headless": ("无头模式（false 会弹出浏览器窗口）", "true / false"),
    "crawl.depth": ("站内爬取深度", "0 表示只抓当前页"),
    "crawl.max_pages": ("最多抓取页面数", "正整数"),
    "crawl.same_host_only": ("只保留同域名资源", "true / false"),
    "crawl.types": ("默认抓取类型", "image,video,audio,hls,dash"),
    "network.proxy": ("代理地址", "如 http://127.0.0.1:7890，留空不用"),
    "network.user_agent": ("自定义 User-Agent", "留空用内置浏览器 UA"),
    "network.cookie": ("Cookie 字符串", "如 \"a=1; b=2\""),
    "network.cookies_from_browser": ("从浏览器导入 Cookie", "chrome / firefox / edge / safari"),
    "advanced.max_segments": ("单个 M3U8 的分片数上限", "防止直播流无限下载"),
    "advanced.remux": ("把 HLS 自动转封装成 MP4", "true / false"),
}

#: 各配置分区的中文标题
_SECTIONS: List[Tuple[str, str]] = [
    ("download", "下载"),
    ("web", "Web 界面"),
    ("crawl", "抓取策略"),
    ("network", "网络"),
    ("advanced", "高级"),
]

_HEADER = (
    "# ============================================================\n"
    "#  mediaharvest 配置文件\n"
    "#\n"
    "#  优先级：命令行参数 > 环境变量 > 本文件 > 内置默认值\n"
    "#  也就是说：这里填的是「默认值」，命令行随时可以覆盖。\n"
    "#\n"
    "#  改完直接生效，无需重启（Web 界面改端口需重启）。\n"
    "#  校验当前配置：./mh --show-config\n"
    "# ============================================================\n"
)


class ConfigError(Exception):
    """配置文件读写失败。"""


def toml_available() -> bool:
    """当前环境是否具备 TOML 解析能力。"""
    return _toml is not None


# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------

def user_config_path() -> str:
    """用户级配置文件路径（``~/.config/mediaharvest/config.toml``）。

    默认不启用；当项目级路径不可写时会回退到这里。
    遵循 ``XDG_CONFIG_HOME``；Windows 下回退到 ``%APPDATA%``。
    """
    if os.name == "nt":  # pragma: no cover - 平台相关
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "mediaharvest", "config.toml")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "mediaharvest", "config.toml")


def project_root() -> str:
    """项目根目录（本包所在目录的上一级）。

    用它来定位 ``mediaharvest.toml``，而不是依赖「当前工作目录」——
    否则从别的目录运行（例如 PyCharm 直接 Run、或系统服务方式启动）时
    会找不到配置文件，静默回落到内置默认值。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def project_config_path() -> str:
    """项目级配置文件路径。"""
    return os.path.join(project_root(), PROJECT_CONFIG_NAME)


def config_candidates(explicit: str = "") -> List[str]:
    """按优先级列出候选配置文件路径。

    顺序：显式指定 / 环境变量 → 项目根目录 → 当前目录 → 用户级。
    「当前目录」也列进来，是为了支持「在某个目录下放一份配置就近生效」
    这种用法；项目根目录排在它前面，保证 PyCharm 等场景下能找到项目配置。
    """
    out: List[str] = []
    for raw in (explicit, os.environ.get(ENV_CONFIG, "")):
        if raw and raw.strip():
            out.append(os.path.abspath(os.path.expanduser(raw.strip())))
    out.append(project_config_path())
    cwd_config = os.path.abspath(PROJECT_CONFIG_NAME)
    if os.path.normpath(cwd_config) != os.path.normpath(project_config_path()):
        out.append(cwd_config)
    out.append(user_config_path())
    # 去重且保序
    seen: Dict[str, None] = {}
    for path in out:
        seen.setdefault(os.path.normpath(path), None)
    return list(seen)


def resolve_config_path(explicit: str = "") -> str:
    """返回生效的配置文件路径。

    **显式指定优先**：``--config PATH`` 或 ``MEDIAHARVEST_CONFIG`` 一旦给出，
    就直接采用该路径（即使文件还不存在——那表示「用这个文件」，
    保存时会创建它），不再回落到自动查找。

    否则按「项目根目录 → 当前目录 → 用户级」找第一个已存在的；
    都不存在时以项目根目录下的 ``mediaharvest.toml`` 作为写入目标
    （项目目录不可写则退到用户级）。
    """
    for raw in (explicit, os.environ.get(ENV_CONFIG, "")):
        if raw and raw.strip():
            return os.path.abspath(os.path.expanduser(raw.strip()))

    for candidate in (project_config_path(), os.path.abspath(PROJECT_CONFIG_NAME)):
        if os.path.isfile(candidate):
            return candidate
    user = user_config_path()
    if os.path.isfile(user):
        return user

    project = project_config_path()
    if os.access(os.path.dirname(project) or ".", os.W_OK):
        return project
    return user


def parse_out_dir(value: str) -> str:
    """展开 ``~`` / 环境变量，把相对路径转成绝对路径。"""
    text = (value or "").strip()
    if not text:
        return ""
    expanded = os.path.expanduser(os.path.expandvars(text))
    return os.path.abspath(expanded)


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------

@dataclass
class AppConfig:
    """一份加载后的配置。

    取值统一走 :meth:`get`，它会按「配置文件 → 默认值」的顺序返回，
    并做类型校验；类型不对时记录到 :attr:`warnings` 并使用默认值，
    不会让程序崩溃。
    """

    #: 实际读取到的文件路径
    path: str = ""
    #: 该文件是否存在
    exists: bool = False
    #: 解析错误（文件损坏时非空）
    error: str = ""
    #: 完整内容，保存时用于保留未识别的键
    data: Dict[str, Any] = field(default_factory=dict)
    #: 类型/取值问题的警告列表
    warnings: List[str] = field(default_factory=list)

    # ---- 取值 --------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """按「环境变量 → 配置文件 → 传入默认值 → 内置默认值」取一个配置项。"""
        env_name = ENV_OVERRIDES.get(key, "")
        if env_name:
            env_value = os.environ.get(env_name, "")
            if env_value.strip():
                fallback = DEFAULTS.get(key)
                return _coerce(key, env_value.strip(), fallback, self.warnings)

        section, _, name = key.partition(".")
        table = self.data.get(section)
        if isinstance(table, dict) and name in table:
            fallback = DEFAULTS.get(key) if default is None else default
            return _coerce(key, table[name], fallback, self.warnings)

        if default is not None:
            return default
        return DEFAULTS.get(key)

    def get_str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return "" if value is None else str(value)

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        return bool(value)

    def out_dir(self, cli_value: str = "") -> str:
        """最终下载目录（绝对路径）。命令行优先于本文件。"""
        if cli_value and cli_value.strip():
            return parse_out_dir(cli_value)
        return parse_out_dir(self.get_str("download.out_dir", DEFAULT_OUT_DIR)) or os.path.abspath(
            DEFAULT_OUT_DIR
        )

    # ---- 展示 --------------------------------------------------------

    @property
    def out_dir_abs(self) -> str:
        """展开后的绝对下载目录。"""
        return parse_out_dir(self.get_str("download.out_dir", DEFAULT_OUT_DIR))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "error": self.error,
            "warnings": list(self.warnings),
            "values": {k: self.get(k) for k in DEFAULTS},
        }


def _coerce(key: str, raw: Any, fallback: Any, warnings: List[str]) -> Any:
    """把配置值转成默认值同类型；失败则告警并回退。"""
    if fallback is None:
        return raw
    try:
        if isinstance(fallback, bool):
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                low = raw.strip().lower()
                if low in ("true", "yes", "on", "1"):
                    return True
                if low in ("false", "no", "off", "0"):
                    return False
            if isinstance(raw, int):
                return bool(raw)
            raise ValueError("需要 true 或 false")
        if isinstance(fallback, int) and not isinstance(fallback, bool):
            if isinstance(raw, bool):
                raise ValueError("需要整数")
            return int(str(raw).strip() if isinstance(raw, str) else raw)
        if isinstance(fallback, float):
            if isinstance(raw, bool):
                raise ValueError("需要数字")
            return float(str(raw).strip() if isinstance(raw, str) else raw)
        if isinstance(fallback, str):
            if isinstance(raw, (list, tuple)):
                return ",".join(str(v) for v in raw)
            if isinstance(raw, bool):
                return "true" if raw else "false"
            return str(raw).strip()
    except (TypeError, ValueError) as exc:
        label, _ = SCHEMA.get(key, (key, ""))
        warnings.append(f"[{key}] {label} 取值无效（{exc}），已使用默认值 {fallback!r}")
        return fallback
    return raw


def load_config(explicit: str = "") -> AppConfig:
    """读取配置文件；文件不存在或不可读时返回空配置（不抛异常）。"""
    path = resolve_config_path(explicit)
    cfg = AppConfig(path=path)
    if not os.path.isfile(path):
        return cfg
    cfg.exists = True

    if _toml is None:
        cfg.error = "缺少 TOML 解析库（Python < 3.11 需安装 tomli）"
        return cfg

    try:
        with open(path, "rb") as fh:
            data = _toml.load(fh)
    except _TOML_ERRORS as exc:
        cfg.error = f"配置文件语法错误: {exc}"
        return cfg
    except OSError as exc:
        cfg.error = f"配置文件读取失败: {exc}"
        return cfg

    if not isinstance(data, dict):
        cfg.error = "配置文件顶层必须是表（table）"
        return cfg

    cfg.data = data
    return cfg


def get(key: str, default: Any = None, explicit: str = "") -> Any:
    """便捷函数：不持有 AppConfig 时直接读一项配置。"""
    return load_config(explicit).get(key, default)


def effective_out_dir(cli_value: str = "", explicit_config: str = "",
                      use_config: bool = True) -> str:
    """按优先级算出最终下载目录（绝对路径）。

    顺序：命令行 > 环境变量 > 配置文件 > 内置默认值。
    """
    if cli_value and cli_value.strip():
        return parse_out_dir(cli_value)
    cfg = load_config(explicit_config) if use_config else AppConfig()
    if not use_config:
        env_value = os.environ.get(ENV_OUT_DIR, "")
        if env_value.strip():
            return parse_out_dir(env_value)
        return os.path.abspath(DEFAULT_OUT_DIR)
    return cfg.out_dir()


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

def _toml_string(text: str) -> str:
    """TOML 基本字符串转义。"""
    out = ["\""]
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\"":
            out.append("\\\"")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append("\"")
    return "".join(out)


def _toml_value(value: Any) -> str:
    """把 Python 值序列化成 TOML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise ConfigError(f"不支持的配置值类型: {type(value).__name__}")


def _toml_key(key: str) -> str:
    """裸键优先，含特殊字符时加引号。"""
    if key and all(c.isalnum() or c in "-_" for c in key):
        return key
    return _toml_string(key)


def _dump_table(data: Dict[str, Any], prefix: str = "") -> List[str]:
    """把一个 dict 渲染成 TOML 行（标量在前，子表在后）。"""
    scalars: List[str] = []
    tables: List[str] = []
    for key, value in data.items():
        if value is None:
            continue
        if not isinstance(key, str):
            raise ConfigError(f"配置键必须是字符串: {key!r}")
        if isinstance(value, dict):
            name = prefix + _toml_key(key)
            body = _dump_table(value, prefix=f"{name}.")
            has_scalars = any(
                v is not None and not isinstance(v, dict) for v in value.values()
            )
            if has_scalars:
                tables.append(f"[{name}]\n" + "\n".join(body) + "\n")
            elif not value:
                if not prefix:
                    tables.append(f"[{name}]\n")
            else:
                tables.append("\n".join(body) + "\n")
            continue
        scalars.append(f"{_toml_key(key)} = {_toml_value(value)}")

    out = list(scalars)
    if scalars and tables:
        out.append("")
    out.extend(tables)
    return out


def save_config(updates: Dict[str, Any], explicit: str = "") -> str:
    """把若干配置项写进文件（保留文件里其它已有的键与注释外的内容）。

    ``updates`` 的键形如 ``"web.port"`` / ``"download.out_dir"``。
    返回写入的文件路径。
    """
    if not toml_available():
        raise ConfigError(
            "缺少 TOML 解析库，无法保存配置；"
            "Python 3.11 以下请先安装：pip install tomli"
        )

    target = resolve_config_path(explicit)
    existing = load_config(target) if os.path.isfile(target) else AppConfig(path=target)
    data: Dict[str, Any] = dict(existing.data) if existing.data else {}

    for key, value in updates.items():
        section, _, name = key.partition(".")
        if not name:
            raise ConfigError(f"配置键格式应为 section.name，收到: {key!r}")
        table = data.get(section)
        if not isinstance(table, dict):
            table = {}
        table = dict(table)
        table[name] = value
        data[section] = table

    directory = os.path.dirname(target)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"无法创建配置目录 {directory}: {exc}") from exc

    body = "\n".join(_dump_table(data)).strip()
    body = re.sub(r"\n{3,}", "\n\n", body) + "\n"
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(_HEADER)
            fh.write("\n")
            fh.write(body)
    except OSError as exc:
        raise ConfigError(f"无法写入配置文件 {target}: {exc}") from exc
    return target


#: 带注释的完整配置模板，由 :func:`write_template` 写出
CONFIG_TEMPLATE = """{header}
{body}"""


def _render_template(commented: bool) -> str:
    """生成配置模板正文。

    ``commented=True`` 时所有行都被注释掉（写成示例，不影响默认行为）。
    """
    lines: List[str] = []
    prefix = "# " if commented else ""
    for section, title in _SECTIONS:
        if lines:
            lines.append("")
        lines.append(f"{prefix}[{section}]")
        for key, value in DEFAULTS.items():
            sec, _, name = key.partition(".")
            if sec != section:
                continue
            label, hint = SCHEMA.get(key, ("", ""))
            if label:
                text = f"# {label}" + (f"（{hint}）" if hint else "")
                lines.append(text)
            lines.append(f"{prefix}{name} = {_toml_value(value)}")
    return "\n".join(lines).rstrip() + "\n"


def write_template(explicit: str = "", *, force: bool = False,
                   commented: bool = False) -> str:
    """写出带注释的配置模板。

    ``commented=True`` 生成「全部注释掉」的示例文件——想照着手改又不想
    改变现有行为时用这个。``force=False`` 时已存在则不覆盖。
    """
    target = resolve_config_path(explicit)
    if os.path.isfile(target) and not force:
        raise ConfigError(
            f"配置文件已存在，未覆盖: {target}\n"
            f"  如需重新生成，请加 --force，或先备份再删除该文件"
        )

    directory = os.path.dirname(target)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"无法创建配置目录 {directory}: {exc}") from exc

    text = _HEADER + "\n" + _render_template(commented)
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        raise ConfigError(f"无法写入配置文件 {target}: {exc}") from exc
    return target


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------

def describe_config(explicit: str = "", cli_out: str = "") -> str:
    """生成 ``--show-config`` 用的多行说明。"""
    cfg = load_config(explicit)
    lines = [
        f"配置文件    : {cfg.path}",
        f"文件状态    : {'已读取' if cfg.exists else '不存在（使用内置默认值）'}",
    ]
    if cfg.error:
        lines.append(f"解析问题    : {cfg.error}")
    for warn in cfg.warnings[:8]:
        lines.append(f"配置警告    : {warn}")
    if not cfg.exists and not cfg.error:
        lines.append(f"生成模板    : ./mh --init-config")

    lines.append("")
    lines.append("生效配置（命令行 > 环境变量 > 配置文件 > 默认值）:")

    # 下载目录单独展示来源，因为它的优先级链最常被问到
    source = "内置默认值"
    env_value = os.environ.get(ENV_OUT_DIR, "").strip()
    if cli_out and cli_out.strip():
        source = "命令行 -o/--output"
    elif env_value:
        source = f"环境变量 {ENV_OUT_DIR}"
    elif cfg.exists and isinstance(cfg.data.get("download"), dict) \
            and "out_dir" in cfg.data["download"]:
        source = "配置文件"

    for section, title in _SECTIONS:
        rows = []
        for key in DEFAULTS:
            sec, _, name = key.partition(".")
            if sec != section:
                continue
            value = cfg.get(key)
            if isinstance(value, str) and not value:
                value = "(未设置)"
            rows.append((name, value))
        if not rows:
            continue
        lines.append(f"  [{section}]  {title}")
        for name, value in rows:
            lines.append(f"    {name:<20} = {value}")

    lines.append("")
    lines.append(f"下载目录来源: {source}")
    lines.append(f"下载目录实值: {cfg.out_dir(cli_out)}")
    if not toml_available():  # pragma: no cover
        lines.append("提示        : 当前 Python 缺少 tomli，配置文件不可用")
    return "\n".join(lines)


def _self_check() -> int:  # pragma: no cover - 手动调试用
    print(describe_config())
    print(f"\n候选路径    : {config_candidates()}")
    print(f"TOML 可用   : {toml_available()}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_self_check())
