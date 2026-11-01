#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dap_proto.py —— 线协议层(自 dap_config.py 拆出,#8)

只放"与板子说话"的部分:常量与设置持久化、错误码解释、URL 编解码、
响应帧正则与解析、Transport(真实串口)、DapClient(欠账机制 + 形态判别)、
串口探测。假设备在 dap_mock.py,GUI 在 dap_gui.py,自测在 dap_selftest.py,
dap_config.py 只是命令行入口薄壳。

线协议(固件 main/dap_cmd.c):
    PC -> 板   @命令 [参数...]\n
    板 -> PC   @OK ... / @ERR <码> <说明> / @DATA ... / @EVT ...
    板端 IDF 日志不带 '@' 前缀,本工具当"板端日志"显示,不当协议帧解析。

关于错误码:固件把 IDF 的断连原因码原样透传。**WPA2 密码错误在 IDF 里报的是
15(4WAY_HANDSHAKE_TIMEOUT),不是 202(AUTH_FAIL)** —— 所以 15/202/204 都按
"密码或加密方式不匹配"来解释。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

TOOL_VERSION = "1.0.0"

# 串口一律用这个速率:USB-CDC 实际不看波特率,但 pyserial 要求给一个值。
BAUD = 115200

# 命令超时(秒)。@CONNECT 固件侧最多等 25s,PC 侧再留余量。
TIMEOUT_DEFAULT = 5.0
TIMEOUT_SCAN = 15.0
TIMEOUT_CONNECT = 32.0

# AP 模式下板子的固定地址(热点网关)
AP_DEFAULT_IP = "192.168.4.1"

# 注意:这里**不再有** AP_DEFAULT_PASSWORD。
# v0.8.0 起 AP 密码由板子首启随机生成、存 NVS,经 @AP 命令读回。
# 曾经这里写着 "12345678" 并在 AP 面板里明文显示 —— 那个串是公开常量,
# 等于热点没有密码。谁要把写死的密码加回来,先想想这条线。

# ====================================================================== #
# 用户设置持久化(%APPDATA%\无线调试器\settings.json)
# ====================================================================== #
#
# 存什么:只有三个**界面偏好**,不带任何凭据。
#   addr          设备地址(mDNS 名或 IP)
#   target        OpenOCD 的 target(如 stm32f1x)
#   at32_target   atopenocd 的 target(如 at32f403axG)
#
# 刻意不存的东西:
#   · 串口号 —— 每次插拔都可能变(COM18 → COM22),存了只会连错板子
#   · WiFi 密码 —— 那是板子上的配置,PC 侧不落盘
#   · 端口/波特率 —— 常量,没必要
#
# 为什么不放在 exe 旁边:Program Files 下不可写,而且交付包可能被解压到
# 只读介质。%APPDATA% 是 Windows 上放用户设置的标准位置。
SETTINGS_VERSION = 1
SETTINGS_KEYS = ("addr", "target", "at32_target")
# 出厂默认值。GUI 用它做两件事:① 首次启动的初值 ② 判断"是不是已经全是默认,
# 是就把设置文件删掉"(见 App._persist_settings)。
DEFAULT_SETTINGS = {
    "addr": "dap.local",
    "target": "stm32f1x",
    "at32_target": "at32f403axG",
}


def delete_settings_file() -> Tuple[bool, str]:
    """删掉 settings.json。文件本来就不存在也算成功。

    做成模块级函数而不是 App 的方法:自测要用假 self 调真逻辑,
    方法绑到子类上会多一个 self 参数(踩过:TypeError takes 0 positional
    arguments but 1 was given)。纯粹的"删文件"本就不需要任何界面状态。
    """
    try:
        p = settings_path()
        if os.path.exists(p):
            os.remove(p)
        return True, p
    except Exception as exc:
        return False, str(exc)


def _settings_dir() -> str:
    """设置目录。Windows 用 %APPDATA%,其他平台用 XDG 惯例。

    函数内现算而不是模块级常量 —— 前者在测试里可以 monkeypatch 环境,
    后者会被 import 时就焊死。
    """
    base = os.environ.get("APPDATA")
    if not base:
        base = os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "无线调试器")


def settings_path() -> str:
    """settings.json 的完整路径(不保证存在)。"""
    return os.path.join(_settings_dir(), "settings.json")


def load_settings() -> Dict[str, str]:
    """读用户设置。文件缺失/损坏/版本不符都返回 {} —— 绝不抛异常。

    为什么容忍损坏:设置文件是"锦上添花",不该因为一个手抖编辑过的
    JSON 就让整个工具起不来。宁可回到默认值,也不能白屏。
    """
    p = settings_path()
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") != SETTINGS_VERSION:
        # 版本不符:当作没有设置,但**不删**用户文件 —— 万一将来要迁移
        return {}
    out = {}
    for k in SETTINGS_KEYS:
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def save_settings(values: Dict[str, str]) -> Tuple[bool, str]:
    """写用户设置。返回 (ok, message)。失败不抛异常。

    先写临时文件再 os.replace —— 直接覆盖时如果进程被杀,会留下半个 JSON,
    下次启动就读不出设置。os.replace 在同一分区上是原子的。
    """
    d = _settings_dir()
    p = settings_path()
    payload = {"version": SETTINGS_VERSION}
    for k in SETTINGS_KEYS:
        v = values.get(k)
        if isinstance(v, str) and v.strip():
            payload[k] = v.strip()
    tmp = p + ".tmp"
    try:
        os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True, p
    except Exception as exc:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False, str(exc)

# 调试服务端口
PORT_USBIP = 3240
PORT_UART_BRIDGE = 1234


# ====================================================================== #
# 错误码解释
# ====================================================================== #

@dataclass
class ErrInfo:
    title: str      # 一句话结论
    detail: str     # 为什么
    action: str     # 下一步怎么办


# 15/202/204 归为同一类:凭据或加密方式对不上。IDF 对 WPA2 密码错误报 15。
_PWD_ACTION = "重新输入密码(注意大小写、全角半角、特殊字符);如果是 WPA3 专用网络,先把路由器改成 WPA2/WPA3 混合模式。"

ERR_INFO: Dict[int, ErrInfo] = {
    15: ErrInfo("密码可能不对",
                "四次握手超时(IDF 的 4WAY_HANDSHAKE_TIMEOUT)。WPA2 下密码错误通常报这个码,"
                "而不是 202,所以看到 15 不要以为是别的问题。", _PWD_ACTION),
    201: ErrInfo("找不到这个 WiFi",
                 "板子扫不到该 SSID。可能是 SSID 拼错、路由器只开了 5GHz、或者信号太弱。",
                 "确认路由器 2.4GHz 频段开着(ESP32 只支持 2.4GHz)、SSID 大小写完全一致;"
                 "再把板子挪近路由器重试。"),
    202: ErrInfo("认证被拒",
                 "认证阶段就失败(AUTH_FAIL)。密码错误或加密方式不兼容都会这样。", _PWD_ACTION),
    203: ErrInfo("关联失败",
                 "认证过了但关联被拒(ASSOC_FAIL)。常见于路由器开了 MAC 白名单或连接数已满。",
                 "检查路由器的 MAC 过滤 / 最大客户端数;也可以重启路由器再试。"),
    204: ErrInfo("握手超时",
                 "握手阶段超时(HANDSHAKE_TIMEOUT)。密码不对、或者 WPA3 与 WPA2 协商不上。",
                 _PWD_ACTION),
    205: ErrInfo("连接失败",
                 "路由器主动断开(CONNECTION_FAIL),多见于信号太差或路由器拒绝。",
                 "靠近路由器重试;仍有问题就换一个 SSID 试试,以区分是板子问题还是路由器问题。"),
    400: ErrInfo("命令参数不对", "工具发的命令固件没看懂。", "多半是工具版本与固件不匹配,请更新工具或固件。"),
    404: ErrInfo("固件不支持这条命令",
                 "板子上的固件还是配网功能之前的老版本。",
                 "烧写 v0.8.0 或更新的固件(见 03_firmware/固件烧录指南.md)。"),
    # 405 一码两义(固件 dap_cmd.c):wifi_still_starting / no_saved_networks。
    # 两者下一步动作完全相反 —— 一个"等一会儿",一个"先去配一条网络"。
    # 静态表只能给通用解释,精确分流见 explain_reply()。
    405: ErrInfo("设备状态不允许",
                "板子当前状态做不了这条命令。常见两种:①刚上电,WiFi 还在初始化;"
                "②一条已保存网络都没有(未配网),却让它按名字置顶/连某个网。",
                "刚上电就等 20 秒再试;如果是未配网,先扫描并连接一个 WiFi。"),
    408: ErrInfo("设备没响应",
                 "等超时了。可能是 USB 线松、板子复位了、或者正处于配网连接过程中。",
                 "确认 USB 插在板子的 native USB 口(不是 CH343 那个),然后点「刷新」重新探测。"),
    409: ErrInfo("当前模式做不了这件事",
                 "AP 模式下 IDF 驱动不支持扫描 WiFi —— 因为板子自己就是热点。",
                 "先点「切到 STA」,再扫描 / 连接。"),
    423: ErrInfo("设备忙", "上一条命令还在执行。", "稍等几秒再试。"),
    500: ErrInfo("设备内部错误", "固件内部调用失败,具体见板端日志。",
                 "切到「日志」页勾上「显示板端日志」看细节;必要时给板子断电重启。"),
}

ERR_UNKNOWN = ErrInfo("未知错误", "固件返回了工具不认识的错误码。", "查看板端日志获取更多线索。")


def explain(code: int) -> ErrInfo:
    return ERR_INFO.get(code, ERR_UNKNOWN)


# 同一错误码可能对应多种原因(固件在 message 里区分)。按 message 精确覆盖。
_MSG_OVERRIDE = {
    "wifi_still_starting": ErrInfo(
        "设备还在启动",
        "板子刚上电,WiFi 还没初始化完,这个窗口期不允许切换模式。",
        "等 20 秒左右再点一次。"),
    "no_saved_networks": ErrInfo(
        "一条网络都没保存",
        "板子里还没有任何已保存的 WiFi(出厂或刚恢复出厂的正常状态)。",
        "先在「配网」页扫描并连接一个 WiFi,再来这里操作。"),
    "connect_in_progress": ErrInfo(
        "上一条连接还在跑",
        "板子正在连某个网络(@CONNECT 最多阻塞 25 秒),期间不接受新命令。",
        "等它出结果(顶部状态栏会变)再试。"),
}


def explain_reply(code: int, text: str = "") -> ErrInfo:
    """按 (码, 说明) 给解释 —— 有精确覆盖就用它,否则回落到码表。

    为什么需要:固件把 405 同时用于"还在启动"和"没有任何已保存网络",
    这两条的下一步动作完全相反。只看码只能给含混的通用提示。
    """
    msg = (text or "").strip()
    if msg in _MSG_OVERRIDE:
        return _MSG_OVERRIDE[msg]
    for key, info in _MSG_OVERRIDE.items():
        if key in msg:
            return info
    return explain(code)


# ====================================================================== #
# 编解码
# ====================================================================== #

def enc(text: str) -> str:
    """把 SSID/密码编成固件能解的形式。

    固件的 url_decode() 会把 '%XX' 还原、把 '+' 当空格。所以这里用
    quote(safe='') 让它把 '+'、'&'、'='、'%'、空格全部转成 %XX —— 绝不产生
    裸 '+' 或裸 '&',避免歧义。
    """
    return quote(text or "", safe="")


def dec(text: str) -> str:
    return unquote(text or "")


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s)


# ====================================================================== #
# 响应帧解析
# ====================================================================== #
#
# 注意:固件回显 SSID 时是原样输出,不做百分号编码(main/dap_cmd.c 里
# "ssid=%s")。所以 SSID 含空格时 `ssid=A B ip=1.2.3.4` 会撕开字段。
# 下面统一用"锚定后续字段的贪婪/惰性正则",而不是按空格或 '&' 硬切,
# 这样带空格、'&'、'/' 的 SSID 也能正确还原。

_IP = r"\d{1,3}(?:\.\d{1,3}){3}"

RE_SCAN_DATA = re.compile(r"^idx=(\d+)&ssid=(.*)&rssi=(-?\d+)&auth=([^&]*)&ch=(\d+)\s*$", re.S)
# 老固件(v0.6)的 LIST 帧没有 pin 字段:idx/ssid/pass 后直接结束。
# 新固件是 "...&pin=0|1"。两条正则都留着,先试新的再试老的 ——
# 否则升级工具但没升级固件的板子,「已保存网络」页会静默变空。
RE_LIST_DATA = re.compile(r"^idx=(\d+)&ssid=(.*)&pass=(.*?)&pin=(\d)\s*$", re.S)
RE_LIST_DATA_OLD = re.compile(r"^idx=(\d+)&ssid=(.*)&pass=(.*?)\s*$", re.S)
RE_CONNECT_OK = re.compile(r"^connected\s+ssid=(.*?)\s+ip=(" + _IP + r")\s*$", re.S)
# prov= 是 v0.8+ 固件新增的未配网标志;老固件没有该字段,可选组兼容两者。
RE_STATUS_STA = re.compile(
    r"^mode=STA\s+ssid=(.*?)\s+ip=(" + _IP + r")\s+rssi=(-?\d+)\s+ch=(\d+)"
    r"(?:\s+prov=(\d))?\s*$", re.S)
RE_STATUS_AP = re.compile(r"^mode=AP\s+ip=(" + _IP + r")\s+ssid=(.*?)\s*$", re.S)
RE_PING = re.compile(r"^pong\s+fw=(\S+)\s+mac=(\S+)\s+mode=(\S+)\s*$")
RE_ERR = re.compile(r"^(\d+)\s*(.*)$", re.S)
RE_SCAN_OK = re.compile(r"^scan\s+count=(\d+)")
RE_LIST_OK = re.compile(r"^count=(\d+)\s+source=(\S+)")
# @AP 的应答(v0.8+):"ap ssid=DAP-XXXX pass=XXXXXXXXXX [note=...]"
# 老固件没有这条命令(会回 404),调用方要能区分"没有这个功能"和"格式不认识"。
RE_AP_OK = re.compile(
    r"^ap\s+ssid=(.*?)\s+pass=(\S+)(?:\s+note=(\S+))?\s*$", re.S)


@dataclass
class Reply:
    """一条命令的完整结果:可能带多条 @DATA,以 @OK 或 @ERR 收尾。"""
    ok: bool
    code: int = 0
    text: str = ""
    data: List[str] = field(default_factory=list)

    @property
    def info(self) -> ErrInfo:
        return explain(self.code)


@dataclass
class DeviceInfo:
    fw: str
    mac: str
    mode: str


@dataclass
class Status:
    mode: str
    ip: str = "0.0.0.0"
    ssid: str = ""
    rssi: int = 0
    channel: int = 0
    prov: bool = False   # True = 未配网(一条已保存网络都没有,v0.8+ 固件上报)


@dataclass
class Network:
    ssid: str
    rssi: int
    auth: str
    channel: int


def parse_scan_data(body: str) -> Optional[Network]:
    m = RE_SCAN_DATA.match(body)
    if not m:
        return None
    return Network(ssid=m.group(2), rssi=int(m.group(3)),
                   auth=m.group(4), channel=int(m.group(5)))


def parse_list_data(body: str) -> Optional[Tuple[int, str, str, int]]:
    """一条 @LIST @DATA,归一为 (idx, ssid, pass, pin)。
    新固件带 pin= 字段;老固件(v0.6)没有,pin 按 0 处理。"""
    m = RE_LIST_DATA.match(body)
    if m:
        idx, ssid, pw, pin = int(m.group(1)), m.group(2), m.group(3), int(m.group(4))
        return idx, ssid, pw, 1 if pin else 0
    m = RE_LIST_DATA_OLD.match(body)
    if m:
        return int(m.group(1)), m.group(2), m.group(3), 0
    return None


def parse_status(body: str) -> Optional[Status]:
    m = RE_STATUS_AP.match(body)
    if m:
        return Status(mode="AP", ip=m.group(1), ssid=m.group(2))
    m = RE_STATUS_STA.match(body)
    if m:
        return Status(mode="STA", ssid=m.group(1), ip=m.group(2),
                      rssi=int(m.group(3)), channel=int(m.group(4)),
                      prov=bool(m.group(5) == "1"))
    return None


def parse_ping(body: str) -> Optional[DeviceInfo]:
    m = RE_PING.match(body)
    if not m:
        return None
    return DeviceInfo(fw=m.group(1), mac=m.group(2), mode=m.group(3))


@dataclass
class ApInfo:
    """@AP 的应答。note 非空表示"密码已换、下次切模式生效"。

    code/err 只在**读失败**时有意义(成功时恒为 0/空):
      code=0    读到了密码,ssid/password 有效
      code=404  固件没有 @AP 这条命令(<=0.7.x)—— "老固件",该建议升级
      code=408  命令发出去了没回话 —— 超时,和固件新旧无关
    区分这两者不是洁癖:早先失败一律返回 None,界面就把"板子没回话"
    也说成"固件太老,请升级 v0.8.0+";用户照做升完级还是连不上,
    排查方向被彻底带偏。错误要能分层,界面才说得出人话。
    """
    ssid: str = ""
    password: str = ""
    note: str = ""
    code: int = 0
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def missing_command(self) -> bool:
        """固件不认识 @AP(而不是这一条没读到)。"""
        return self.code == 404


def parse_ap(body: str) -> Optional[ApInfo]:
    m = RE_AP_OK.match(body)
    if not m:
        return None
    return ApInfo(ssid=m.group(1), password=m.group(2),
                  note=(m.group(3) or ""))


def version_tuple(v: str) -> Tuple[int, ...]:
    out = []
    for part in re.split(r"[._-]", v):
        if part.isdigit():
            out.append(int(part))
        else:
            break
    return tuple(out)


def rssi_bar(rssi: int) -> str:
    """把 dBm 画成 4 格信号条。

    用 ■/□(7px,与 Consolas 正文同宽)而不是 █/░:后者在浅色表格里是纯黑长条,
    看着像色块而不是信号指示;▰/▱ 虽然也成对但宽 10px,列会显得松散。
    阈值参考常见 WiFi 体验。
    """
    if rssi >= -55:
        n = 4
    elif rssi >= -65:
        n = 3
    elif rssi >= -75:
        n = 2
    elif rssi >= -85:
        n = 1
    else:
        n = 0
    return "\u25a0" * n + "\u25a1" * (4 - n)


def rssi_word(rssi: int) -> str:
    if rssi >= -55:
        return "很强"
    if rssi >= -65:
        return "良好"
    if rssi >= -75:
        return "一般"
    if rssi >= -85:
        return "偏弱"
    return "很弱"


# ====================================================================== #
# 传输层
# ====================================================================== #

class Transport:
    """串口 / 假设备共用的最小接口。"""

    name = "?"

    def open(self) -> None: ...
    def close(self) -> None: ...
    def write(self, data: bytes) -> None: ...
    def read(self, size: int = 4096, timeout: float = 0.05) -> bytes: ...

    @property
    def is_open(self) -> bool:
        raise NotImplementedError


class SerialTransport(Transport):
    def __init__(self, port: str, baud: int = BAUD):
        self.name = port
        self._port = port
        self._baud = baud
        self._ser = None

    def open(self) -> None:
        import serial  # 延迟导入:--selftest 不需要 pyserial
        # Windows 上打开端口时 DTR/RTS 会改变电平;ESP32-S3 的 USB-Serial/JTAG
        # 会把这些信号当复位/下载请求解释。必须在 open() **之前**把 DTR/RTS
        # 置 False:Serial(port, ...) 构造即 open,驱动先按默认电平拉线、
        # 板子被踹进复位,之后再 setDTR(False) 也来不及 —— 实测(2026-10-28)
        # 那条路径上 CDC 端点起不来,write 永远 Write timeout,而读日志正常。
        # 先配属性再 open,驱动从头到尾不产生 DTR/RTS 脉冲。
        self._ser = serial.Serial()
        self._ser.port = self._port
        self._ser.baudrate = self._baud
        self._ser.timeout = 0.05
        self._ser.write_timeout = 2.0
        self._ser.exclusive = True
        self._ser.dtr = False
        self._ser.rts = False
        self._ser.open()
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def write(self, data: bytes) -> None:
        self._ser.write(data)
        self._ser.flush()

    def read(self, size: int = 4096, timeout: float = 0.05) -> bytes:
        try:
            return self._ser.read(max(1, self._ser.in_waiting))
        except Exception:
            raise



# ====================================================================== #
# 客户端:把线协议包成 Python 方法
# ====================================================================== #

class DapClient:
    """串行化执行命令的客户端。

    一个后台线程持续读串口,把行分成两类:
      - 以 '@' 开头 -> 协议帧,投递到帧队列供 request() 消费
      - 其余        -> 板端日志,交给 on_log 回调
    这样即使 @CONNECT 阻塞等 25s,日志也不会丢。
    """

    def __init__(self, transport: Transport,
                 on_log: Optional[Callable[[str], None]] = None,
                 on_disconnect: Optional[Callable[[str], None]] = None):
        self._tr = transport
        self._lock = threading.Lock()
        self._frames: "queue.Queue[str]" = queue.Queue()
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.on_log = on_log
        self.on_disconnect = on_disconnect
        self.info: Optional[DeviceInfo] = None
        # 最近读到的板端日志(用于判断"应用已就绪"),只在内存里保留尾部
        self._seen_log = bytearray()
        # 上一条命令超时了 = 它的终结帧(@OK/@ERR)还没收到,仍欠着。
        # 固件是**串行**的:上一条没处理完,不会开始处理下一条。所以欠着的那
        # 一帧一定先于本条命令的应答出现 —— 收到它时丢掉即可,不需要时间窗口。
        # 见 _request_once 里"欠账"那段说明。
        self._stale_owed = 0
        # 欠账的过期时刻。板子不回话时(探错口 / 板子已经复位),这笔账永远
        # 收不到;不设过期就会把之后每一条命令的真应答都当成迟到帧吃掉。
        # 固件最长的阻塞命令是 @CONNECT(25s),所以 30s 足够覆盖任何真迟到。
        self._stale_expire = 0.0
        # 这条连接上是否**曾经有命令成功应答过**(即设备被证明是活的)。
        # 用来消掉一个真实的性能坑:探一个根本不是 DAP 的串口时(蓝牙虚拟口、
        # CH343 之类),它一个字节都不回,谈不上"有应答还在路上"。
        # 实测 probe_port 因此从 3.8s 涨到 4.9s。
        self._ever_ok = False

    # --- 生命周期 ----------------------------------------------------
    @property
    def port_name(self) -> str:
        return self._tr.name

    @property
    def is_open(self) -> bool:
        return self._tr.is_open

    def open(self) -> None:
        self._tr.open()
        self._stop.clear()
        self._frames = queue.Queue()
        self._seen_log = bytearray()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="dap-reader")
        self._reader.start()

    def wait_ready(self, timeout: float = 6.0, need_wifi: bool = True) -> bool:
        """等设备能正常收发命令、且 WiFi 驱动已初始化完。

        打开串口会让 ESP32-S3 复位(DTR/RTS 电平变化被 USB-Serial/JTAG 当成
        复位请求)。复位期间 CDC 端点没就绪,pyserial 的 write 会直接超时,
        而且**每次失败的写都要等满超时**,密集重试反而拖慢并干扰启动。

        分两阶段:
          1. 被动等启动日志出现 "command channel ready" —— 此时能发命令了,
             但 WiFi 可能还在初始化(实测 STATUS 会返回 wifi_get_mode_failed、
             SCAN 会返回 409)。
          2. 发 STATUS 直到能拿到可信状态,才算真正就绪。
        """
        deadline = time.monotonic() + timeout
        # 阶段一:等命令通道就绪。
        # 两条出路:启动横幅出现(开串口踹复位了,老路径),或者 PING 直接
        # 应答(DTR 修复后开串口不再复位,板子本来就在跑,横幅不会重来)。
        while time.monotonic() < deadline:
            if b"command channel ready" in self._seen_log:
                break
            r = self._request_once("@PING", 0.6)
            if r.ok and parse_ping(r.text):
                self.info = parse_ping(r.text)
                break
            time.sleep(0.15)
        # 阶段二:稀疏轮询 STATUS,直到 WiFi 基座可用
        attempt = 0
        info_ok = False
        while time.monotonic() < deadline:
            if not info_ok:
                r = self._request_once("@PING", 0.6)
                if r.ok:
                    info = parse_ping(r.text)
                    if info:
                        self.info = info
                        info_ok = True
                        if not need_wifi:
                            return True
                        continue
            else:
                r = self._request_once("@STATUS", 1.2)
                if r.ok:
                    return True
                # wifi_get_mode_failed / 409 之类都说明 WiFi 还没起来
            attempt += 1
            time.sleep(0.4 if attempt < 3 else 0.9)
        return False

    def close(self) -> None:
        """先停读线程,再关端口。

        顺序不能反:读线程可能正阻塞在 read() 里,先 close 端口会让它访问
        已释放的句柄 —— 实测偶发 process 段错误(Windows 上 USB-CDC 尤其明显)。
        所以先置停止标志,等线程自己从 read 超时里退出来,再关。
        """
        self._stop.set()
        if self._reader is not None:
            self._reader.join(timeout=2.0)
            self._reader = None
        try:
            self._tr.close()
        except Exception:
            pass

    # --- 读线程 ------------------------------------------------------
    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._tr.read(4096, 0.05)
            except Exception as exc:          # 端口被拔/板子复位
                if not self._stop.is_set():
                    if self.on_disconnect:
                        try:
                            self.on_disconnect(str(exc))
                        except Exception:
                            pass
                return
            if not chunk:
                continue
            buf += chunk
            # 留一份尾部字节给 wait_ready 判断"应用已就绪"
            self._seen_log += chunk
            if len(self._seen_log) > 32768:
                del self._seen_log[:-16384]
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = strip_ansi(raw.decode("utf-8", "replace").rstrip("\r"))
                if not line:
                    continue
                if line.lstrip().startswith("@"):
                    self._frames.put(line.strip())
                elif self.on_log:
                    try:
                        self.on_log(line)
                    except Exception:
                        pass

    # --- 请求/响应 ---------------------------------------------------
    # 重试策略:USB-Serial/JTAG 上日志字节可能吃掉命令字节,偶发无响应。
    # 只对幂等命令重试;@CONNECT 绝不盲重试(板子可能已经在连,重发会撞 423)。
    RETRYABLE = ("@PING", "@STATUS", "@SCAN", "@LIST")
    # 这些 408 是本地失败(端口没开/写不进去),重发没有意义
    _NO_RETRY_TEXT = ("port_closed", "write_failed")

    def request(self, line: str, timeout: float = TIMEOUT_DEFAULT,
                retries: Optional[int] = None) -> Reply:
        """发一条命令,收集 @DATA 直到 @OK / @ERR / 超时。

        超时(408)时按 RETRYABLE 决定是否重发。固件侧命令是串行的,一条命令
        没响应说明它压根没被解析,重发是安全的。
        """
        if retries is None:
            cmd = line.strip().split(None, 1)[0].upper() if line.strip() else ""
            retries = 2 if cmd in self.RETRYABLE else 0
        reply = Reply(ok=False, code=408, text="TIMEOUT")
        for attempt in range(retries + 1):
            reply = self._request_once(line, timeout)
            if not self._timed_out(reply):
                return reply
            if attempt < retries:
                if self.on_log:
                    try:
                        self.on_log("命令 %s 无响应,重发(%d/%d)"
                                    % (line.strip(), attempt + 1, retries))
                    except Exception:
                        pass
                time.sleep(0.25)
        return reply

    @classmethod
    def _timed_out(cls, reply: Reply) -> bool:
        """真正值得重发的超时:板子没在时限内回话。本地端口故障不算。"""
        if reply.ok or reply.code != 408:
            return False
        return not any(m in reply.text for m in cls._NO_RETRY_TEXT)

    def _request_once(self, line: str, timeout: float) -> Reply:
        with self._lock:
            if not self._tr.is_open:
                return Reply(ok=False, code=408, text="port_closed")
            # 写命令前先排空未读帧(已经躺在队列里的迟到应答/旧 @DATA)。
            # **但终结帧不能白排**:它正是用来销账的。实测漏掉这一点会出真
            # bug:残留帧在"上一条超时返回"和"下一条写出"之间到达(很常见,
            # 间隙有几百毫秒),被这里一扫而光 -> 欠账永远挂着 -> 下一条命令
            # 自己的真 @DATA 反被当成残留吃掉 -> 408 且重试两次全废。
            # 所以边排边数 @OK/@ERR,有几个就销几笔账。
            drained = 0
            terminators = 0
            while True:
                try:
                    frame = self._frames.get_nowait()
                except queue.Empty:
                    break
                drained += 1
                if frame.partition(" ")[0].upper() in ("@OK", "@ERR"):
                    terminators += 1
            if terminators and self._stale_owed:
                self._stale_owed = max(0, self._stale_owed - terminators)
            # 欠账过期就销账。过期依据是固件最长阻塞命令(@CONNECT 25s),
            # 给到 30s 足以覆盖任何真迟到;超期说明那笔账其实永远不会来了
            # (板子被复位/拔线),继续记着只会把后面的真应答当成迟到帧。
            if self._stale_owed and time.monotonic() > self._stale_expire:
                self._stale_owed = 0
            try:
                self._tr.write((line.rstrip("\r\n") + "\n").encode("utf-8"))
            except Exception as exc:
                return Reply(ok=False, code=408, text="write_failed: %s" % exc)

            deadline = time.monotonic() + timeout
            data: List[str] = []
            # 本条命令期间是否丢弃过"上一条的残留终结帧"。
            # 丢了就说明:上一条的应答确实到了(正常),或者它根本没来过而我方
            # 误吞了本条的应答。两者靠"本条是否也超时"来区分 —— 见超时分支。
            ate_stale = False
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    # 记欠账:本条命令的终结帧还没收到,它迟早会到,而那时
                    # 已经在等下一条命令了 —— 必须记着,下一条收到第一个
                    # 终结帧时把它当迟到帧丢掉。
                    #
                    # 只对"活着的设备"记账:一个字节都没回的口(探错 COM 口、
                    # 板子没上电)谈不上"应答还在路上",记了只会拖慢探测。
                    # 实测判据用 _ever_ok(曾成功应答过)时 probe_port 3.83s,
                    # 与改前一致;用"帧计数>0"会漏记(超时那刻迟到帧还没到)。
                    if self._ever_ok:
                        if ate_stale:
                            # **自愈**:本条期间已经丢掉过一个"上一条的残留
                            # 终结帧",而本条自己仍然超时 —— 说明那笔旧账其实
                            # 永远不会来了(板子复位 / 串口丢字节),刚才被吞掉
                            # 的就是本条自己的应答。
                            # 若此时继续 += 1 就进入永久死锁:
                            #   账恒为 1 -> 每条命令的首个终结帧都被当残留吃掉
                            #   -> 每条命令都 408 -> 只能重插 USB。
                            # 实测连续 5 条 PING 全 408,而板子一条都没漏回;
                            # 30s 过期也救不了,因为每次超时都把 expire 刷成
                            # now+30,永远等不到过期。
                            # 所以这里清零:代价是这一条失败一次,下一条恢复
                            # (实测 1 条失败后即恢复)。
                            self._stale_owed = 0
                            if self.on_log:
                                try:
                                    self.on_log("上一条超时命令的应答不会再来了,"
                                                "清空欠账(本条很可能误吞了自己的应答)")
                                except Exception:
                                    pass
                        else:
                            self._stale_owed += 1
                            self._stale_expire = time.monotonic() + 30.0
                    return Reply(ok=False, code=408, text="TIMEOUT", data=data)
                try:
                    frame = self._frames.get(timeout=min(remain, 1.0))
                except queue.Empty:
                    continue
                tag, _, body = frame.partition(" ")
                tag = tag.upper()
                if tag == "@DATA":
                    # 欠着账时,上一条的 @DATA 一定还没收完(它全在终结帧之前),
                    # 所以此时收到的是残留。固件串行 => 本条自己的 @DATA 绝不
                    # 会出现在上一条的终结帧之前。
                    if self._stale_owed:
                        if self.on_log:
                            try:
                                self.on_log("丢弃上一条超时命令的残留 @DATA")
                            except Exception:
                                pass
                        continue
                    data.append(body)
                elif tag in ("@OK", "@ERR"):
                    # 第一道:欠账必先还。固件串行 => 上一条超时命令的终结帧
                    # 一定先于本条的终结帧出现。收到它就把账销掉、丢掉这一帧。
                    # 这条不需要时间窗口,也不受"迟到多久"影响 —— 之前用固定
                    # 1.5s 结算窗口的写法,遇到 @CONNECT(板子要 25s)这种
                    # 长阻塞命令仍会串味,已实测复现后改成计数。
                    if self._stale_owed:
                        self._stale_owed -= 1
                        ate_stale = True
                        if self.on_log:
                            try:
                                self.on_log("丢弃上一条超时命令的残留 %s:%s"
                                            % (tag, body.strip()[:50]))
                            except Exception:
                                pass
                        continue
                    # 第二道:形态判别。挡"没有欠账但确实串了"的情况
                    # (比如工具刚启动、上一条是上一个进程发的)。
                    if not self._looks_like_reply_to(line, tag, body):
                        if self.on_log:
                            try:
                                self.on_log("丢弃一条疑似迟到的 %s 应答:%s"
                                            % (tag, body.strip()[:60]))
                            except Exception:
                                pass
                        continue
                    if tag == "@OK":
                        self._ever_ok = True
                        return Reply(ok=True, code=0, text=body.strip(), data=data)
                    m = RE_ERR.match(body.strip())
                    if m:
                        # @ERR 也是设备在说话 -> 证明它是活的
                        self._ever_ok = True
                        return Reply(ok=False, code=int(m.group(1)),
                                     text=m.group(2).strip(), data=data)
                    return Reply(ok=False, code=500, text=body.strip(), data=data)
                elif tag == "@EVT":
                    if self.on_log:
                        try:
                            self.on_log("[事件] " + body)
                        except Exception:
                            pass
                # 其他未知 '@' 行直接忽略

    @staticmethod
    def _cmd_name(line: str) -> str:
        s = line.strip()
        if s.startswith("@"):
            s = s[1:]
        return s.split(None, 1)[0].upper() if s else ""

    def _looks_like_reply_to(self, line: str, tag: str, body: str) -> bool:
        """终结帧的形态是否匹配刚发出的那条命令。

        为什么需要:固件命令是串行的,但**超时是 PC 侧的事**。上一条命令
        超时后它的应答仍可能在路上,而写前排空只能清掉"已经到队列"的那些。
        真机复现(2026-10-29):
          @STATUS 超时 0.5s → 用户点扫描 → @SCAN 写出 0.3s 后 STATUS 的
          "@OK mode=STA ssid=... ip=..." 到了 → 被当成 SCAN 的终结帧 →
          scan() 返回 0 个网络(真值 1)。
        加序号要动协议 + 固件同版本发,这里用"回复形态"做低成本的等价判别:
        每条命令的 @OK 都有固定开头,对不上就不是给我的。

        保守原则:拿不准就返回 True(宁可串味也别把正常应答丢掉)。
        """
        b = (body or "").strip()
        if tag == "@ERR":
            # 错误码是通用的,任何命令都可能收到;只在明显不可能是回复时丢
            return True
        cmd = self._cmd_name(line)
        # 已知命令的 @OK 首词(取自固件 dap_cmd.c 的 dap_cmd_ok 调用)。
        # 键是 _cmd_name() 的结果,**不带 @** —— 写成 "@SCAN" 会永远查不到,
        # 全部走保守分支,等于没做判断(第一版就是这么错的,自测抓到)。
        expect = {
            "PING": ("pong",),
            "STATUS": ("mode=",),
            "SCAN": ("scan",),
            "CONNECT": ("connected",),
            "LIST": ("count=",),
            "DEL": ("deleted",),
            "PRIO": ("prioritized",),
            "PIN": ("pin",),
            "MODE": ("mode=",),
            "LED": ("led",),
            "REBOOT": ("rebooting",),
            "FACTORY": ("rebooting",),
            # @AP 是 0.8.0 新增的(读/重置 AP 热点密码)。
            # 未登记时这里会走"保守放行",而唯一依赖它的场景就是
            # "上一条超时后的残留"——那条路上放行等于没保护,所以补上。
            "AP": ("ap ssid=",),
        }
        heads = expect.get(cmd)
        if not heads:
            return True          # 未登记的命令:不做判断
        low = b.lower()
        return any(low.startswith(h) for h in heads)

    # --- 高层命令 ----------------------------------------------------
    def ping(self, timeout: float = 2.0) -> DeviceInfo:
        r = self.request("@PING", timeout)
        if not r.ok:
            raise IOError("设备无响应(%s)" % (r.text or r.code))
        info = parse_ping(r.text)
        if info is None:
            raise IOError("响应格式不认识: %s" % r.text)
        self.info = info
        return info

    def status(self) -> Status:
        r = self.request("@STATUS", 3.0)
        if not r.ok:
            raise IOError("取状态失败(%s)" % (r.text or r.code))
        st = parse_status(r.text)
        if st is None:
            raise IOError("状态格式不认识: %s" % r.text)
        return st

    def scan(self) -> List[Network]:
        r = self.request("@SCAN", TIMEOUT_SCAN)
        if not r.ok:
            raise DapError(r)
        nets = [n for n in (parse_scan_data(b) for b in r.data) if n]
        # 固件已按 RSSI 降序去重;这里再兜一次,防止将来固件改动
        uniq: Dict[str, Network] = {}
        for n in nets:
            old = uniq.get(n.ssid)
            if old is None or n.rssi > old.rssi:
                uniq[n.ssid] = n
        return sorted(uniq.values(), key=lambda n: n.rssi, reverse=True)

    def connect(self, ssid: str, password: str) -> Reply:
        r = self.request("@CONNECT ssid=%s&pass=%s" % (enc(ssid), enc(password)),
                         TIMEOUT_CONNECT)
        return r

    def list_saved(self) -> Tuple[List[Tuple[int, str, str, int]], str]:
        r = self.request("@LIST", 5.0)
        if not r.ok:
            raise DapError(r)
        rows = [x for x in (parse_list_data(b) for b in r.data) if x]
        rows.sort(key=lambda x: x[0])
        m = RE_LIST_OK.search(r.text)
        source = m.group(2) if m else "?"
        return rows, source

    def delete(self, idx: int) -> Reply:
        return self.request("@DEL idx=%d" % idx, 5.0)

    def prioritize(self, idx: int) -> Reply:
        """把第 idx 条移到最前 —— 设备开机时按列表顺序尝试,首位最先。"""
        return self.request("@PRIO idx=%d" % idx, 5.0)

    def set_pin(self, idx: int, on: bool) -> Reply:
        """固定优先标记(粘性):连接轮转时 pinned 条目永远排在未 pinned 前,
        不会被下次「连接」的自动置顶覆盖。"""
        return self.request("@PIN idx=%d %s" % (idx, "on" if on else "off"), 5.0)

    def set_mode(self, mode: str) -> Reply:
        return self.request("@MODE %s" % mode.upper(), 15.0)

    def led_get(self) -> Optional[int]:
        """读当前状态灯亮度百分比;固件不支持或解析失败返回 None。"""
        r = self.request("@LED", 4.0)
        if not r.ok:
            return None
        m = re.search(r"pct=(\d+)", r.text)
        return int(m.group(1)) if m else None

    def led_set(self, pct: int) -> Reply:
        return self.request("@LED %d" % int(pct), 6.0)

    def ap_get(self) -> ApInfo:
        """读 AP 热点 SSID 与密码(v0.8+)。

        **总是**返回 ApInfo,失败时用 code/err 说明原因,不要返回 None:
          - 404 = 老固件没有 @AP(正常现象,界面该提示升级)
          - 408 = 命令发出去没回话(和固件新旧无关,该提示重试)
        两者混成一个 None 会让界面把超时说成"固件太老",指错方向。
        """
        return self._ap_request("@AP", 4.0)

    def ap_regen(self) -> ApInfo:
        """重新生成 AP 密码。成功时返回新密码,失败时 code/err 说明原因。

        注意:新密码要等下一次切模式/重启才真正生效 —— 固件在应答的
        note= 字段里写明这一点,界面必须如实转述,不能让用户以为立刻生效。
        """
        return self._ap_request("@AP REGEN", 8.0)

    def _ap_request(self, line: str, timeout: float) -> ApInfo:
        r = self.request(line, timeout)
        if not r.ok:
            return ApInfo(code=r.code or 500, err=r.text)
        info = parse_ap(r.text)
        if info is None:
            # 应答到了但看不懂 —— 既不是"没这个命令",也不是超时。
            # 单独给 500,别让它冒充 404 去劝人升级固件。
            return ApInfo(code=500, err="响应格式不认识: %s" % r.text)
        return info

    def factory_reset(self) -> Reply:
        r = self.request("@FACTORY", 5.0)
        return r

    def reboot(self) -> Reply:
        return self.request("@REBOOT", 5.0)


class DapError(Exception):
    def __init__(self, reply: Reply):
        self.reply = reply
        super().__init__("%d %s" % (reply.code, reply.text))


# ====================================================================== #
# 串口枚举 / 探测
# ====================================================================== #

# ESP32-S3 的 USB-Serial/JTAG 的 USB 标识。板子跑应用时也枚举成这个
# (不是只有下载模式),所以它只能用来**加快筛选**,不能当作"一定是"。
DAP_USB_VID_PID = ("303A", "1001")


def _is_likely_dap_port(hwid: str) -> bool:
    """hwid 里是否带 ESP32-S3 USB-Serial/JTAG 的 VID:PID。

    两种常见写法都要认:
      pyserial 在 Windows 给的是 "USB VID:PID=303A:1001 SER=..."
      设备路径形态是 "USB\\VID_303A&PID_1001\\..."
    """
    h = (hwid or "").upper().replace(" ", "")
    if not h:
        return False
    vid, pid = DAP_USB_VID_PID
    return (("VID:PID=%s:%s" % (vid, pid)) in h
            or ("VID_%s&PID_%s" % (vid, pid)) in h)


def enumerate_ports(with_hwid: bool = False):
    """列出串口。

    with_hwid=False -> [(device, description)](老接口,保持不变)
    with_hwid=True  -> [(device, description, hwid)]

    为什么要 hwid:探测一个非 DAP 的口要 6 秒左右(打开端口 + 等启动),
    而 hwid 里直接写着 USB VID:PID —— 带 303A:1001 的才可能是 DAP,
    其余的一眼就能跳过。自动探测从「端口数 × 6s」降到秒级。
    """
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    out = []
    for p in list_ports.comports():
        desc = p.description or ""
        if with_hwid:
            out.append((p.device, desc, getattr(p, "hwid", "") or ""))
        else:
            out.append((p.device, desc))
    out.sort(key=lambda x: (len(x[0]), x[0]))
    return out


def probe_port(device: str, timeout: float = 1.5,
               settle: float = 0.0) -> Optional[DeviceInfo]:
    """往一个串口发 @PING,回 pong 就是我们的设备。

    打开串口会让 ESP32-S3 复位,先等它起来 —— wait_ready 内部本来就在
    轮询 PING(每 0.15s,直到 timeout),外面再 sleep 就是纯浪费。
    实测(2026-10-29)探测一个非 DAP 的 COM 口:settle=2.5 时 6.34s,
    settle=0 时约 3.8s(剩下的都是 wait_ready 的超时)。默认已改为 0。
    """
    client = DapClient(SerialTransport(device))
    try:
        client.open()
    except Exception:
        return None
    try:
        if settle:
            time.sleep(settle)
        # 探测只关心"这是不是我们的设备",不必等 WiFi 初始化
        if client.wait_ready(max(2.0, timeout + 2.0), need_wifi=False):
            return client.info
        return None
    except Exception:
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass


def probe_all(progress: Optional[Callable[[str, str], None]] = None,
              use_hwid_filter: bool = True
              ) -> List[Tuple[str, DeviceInfo]]:
    """探测所有串口。

    串口枚举里常见蓝牙虚拟串口、CH343 之类的杂项口,对它们写数据可能让
    驱动层出问题(实测遇到过一次 process 段错误)。所以:
      - use_hwid_filter 时先按 hwid 里的 ESP32-S3 VID:PID 预筛;
      - 逐个探测之间留 0.2s 间隔,单个口的异常彻底吞掉。

    预筛只是**加速**:若一个都没匹配上(比如板子用了别的 USB 转串口芯片,
    或者 hwid 拿不到),自动退回全量探测,不会漏设备。
    """
    ports = enumerate_ports(with_hwid=True)
    if use_hwid_filter:
        likely = [p for p in ports if _is_likely_dap_port(p[2])]
        if likely:
            ports = likely
        # 一个都不匹配就保留全量 —— 宁可慢也不能漏

    found = []
    for device, desc, _hwid in ports:
        if progress:
            progress(device, desc)
        try:
            info = probe_port(device)
        except Exception:
            info = None
        if info:
            found.append((device, info))
        time.sleep(0.2)
    return found


