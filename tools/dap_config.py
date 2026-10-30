#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dap_config.py —— 无线调试器配网工具(产品化 P3)

给同事用的图形化配网界面:插上 USB、选 WiFi、填密码、看结果,不用记命令。

依赖:Python 3.8+ / tkinter / pyserial。都是 Windows 上现成的,零额外安装。

用法:
    python dap_config.py                 # 图形界面,自动探测设备
    python dap_config.py --port COM18    # 图形界面,指定串口
    python dap_config.py --mock          # 图形界面,连假设备(无板子也能用)
    python dap_config.py --list          # 命令行:枚举并探测所有串口
    python dap_config.py --raw "@SCAN"   # 命令行:直接发一条命令看响应
    python dap_config.py --selftest      # 无硬件自测(协议层 + 交互流程)

线协议(固件 main/dap_cmd.c):
    PC -> 板   @命令 [参数...]\\n
    板 -> PC   @OK ... / @ERR <码> <说明> / @DATA ... / @EVT ...
    板端 IDF 日志不带 '@' 前缀,本工具当"板端日志"显示,不当协议帧解析。

关于错误码:固件把 IDF 的断连原因码原样透传。**WPA2 密码错误在 IDF 里报的是
15(4WAY_HANDSHAKE_TIMEOUT),不是 202(AUTH_FAIL)** —— 所以 15/202/204 都按
"密码或加密方式不匹配"来解释。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
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
    """@AP 的应答。note 非空表示"密码已换、下次切模式生效"。"""
    ssid: str
    password: str
    note: str = ""


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


# ---------------------------------------------------------------------- #
# 假设备:没有板子也能把界面完整点一遍(--mock / --selftest)
# ---------------------------------------------------------------------- #

MOCK_FACTORY_NETS = [("MyWiFi", "12345678"), ("DAP", "12345678"), ("OTA", "12345678")]

# 前 4 项覆盖:普通 SSID、无密码、空格+中文、长名。密码统一 12345678。
MOCK_SCAN = [
    ("MyWiFi", -48, "WPA/WPA2", 6, "12345678"),
    ("DAP", -61, "WPA2", 1, "12345678"),
    ("\u5c0f\u7f8e\u7684 WiFi", -70, "WPA/WPA2", 3, "12345678"),
    ("HUAWEI-5G", -76, "WPA2", 11, "12345678"),
    ("RouterB", -83, "WPA2", 9, "12345678"),
    ("PublicWiFi", -88, "OPEN", 6, ""),
]


class MockTransport(Transport):
    """模拟固件行为的假设备。

    只实现 PC 工具用到的命令,行为尽量贴近真机:协商失败会给 15,连上会
    自动存进已保存列表,重启会静默一两秒。
    """

    name = "MOCK"

    def __init__(self, connect_delay: float = 1.0):
        self._open = False
        self._mode = "STA"
        self._ip = "192.168.7.125"
        self._ssid = "MyWiFi"
        self._rssi = -48
        self._ch = 6
        self._saved = [[s, p, 0] for (s, p) in MOCK_FACTORY_NETS]
        self._source = "factory"
        self._pending: List[Tuple[float, str]] = []
        self._lock = threading.Lock()
        self._connect_delay = connect_delay
        self._led_pct = 4
        # 模拟 v0.8+ 的随机 AP 密码(真机是首启生成的 10 位)
        self._ap_pass = "K7M4RTW9XY"

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    # --- 输出 --------------------------------------------------------
    def _emit(self, line: str, delay: float = 0.0) -> None:
        with self._lock:
            self._pending.append((time.monotonic() + delay, line))

    def read(self, size: int = 4096, timeout: float = 0.05) -> bytes:
        time.sleep(0.01)
        now = time.monotonic()
        with self._lock:
            ready = [ln for (t, ln) in self._pending if t <= now]
            self._pending = [(t, ln) for (t, ln) in self._pending if t > now]
        if not ready:
            return b""
        return ("\n".join(ready) + "\n").encode("utf-8")

    # --- 输入 --------------------------------------------------------
    def write(self, data: bytes) -> None:
        for line in data.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line:
                self._handle(line)

    def _handle(self, line: str) -> None:
        if not line.startswith("@"):
            return
        body = line[1:].strip()
        parts = body.split(None, 1)
        if not parts:
            return
        cmd = parts[0].upper()
        args = parts[1] if len(parts) > 1 else ""

        # 模拟一点板端日志,顺带验证日志窗格的过滤
        self._emit("I (1234) mock: got @%s" % cmd, 0.0)

        if cmd == "PING":
            self._emit("@OK pong fw=0.8.0 mac=AA:BB:CC:DD:EE:FC mode=%s" % self._mode, 0.02)
        elif cmd == "STATUS":
            if self._mode == "AP":
                self._emit("@OK mode=AP ip=%s ssid=DAP-EEFC" % AP_DEFAULT_IP, 0.02)
            else:
                self._emit("@OK mode=STA ssid=%s ip=%s rssi=%d ch=%d"
                           % (self._ssid, self._ip, self._rssi, self._ch), 0.02)
        elif cmd == "SCAN":
            if self._mode != "STA":
                self._emit("@ERR 409 scan_requires_STA_mode", 0.05)
                return
            for i, (ssid, rssi, auth, ch, _pw) in enumerate(MOCK_SCAN):
                self._emit("@DATA idx=%d&ssid=%s&rssi=%d&auth=%s&ch=%d"
                           % (i, ssid, rssi, auth, ch), 0.3)
            self._emit("@OK scan count=%d" % len(MOCK_SCAN), 0.4)
        elif cmd == "CONNECT":
            ssid = _kv(args, "ssid")
            pw = _kv(args, "pass") or ""
            self._do_connect(ssid, pw)
        elif cmd == "LIST":
            for i, (s, p, pin) in enumerate(self._saved):
                self._emit("@DATA idx=%d&ssid=%s&pass=%s&pin=%d"
                           % (i, s, "***" if p else "(open)", pin), 0.02)
            self._emit("@OK count=%d source=%s" % (len(self._saved), self._source), 0.03)
        elif cmd == "DEL":
            try:
                idx = int(_kv(args, "idx") or "-1")
            except ValueError:
                idx = -1
            if 0 <= idx < len(self._saved):
                del self._saved[idx]
                self._source = "nvs"
                self._emit("@OK deleted idx=%d remaining=%d" % (idx, len(self._saved)), 0.02)
            else:
                self._emit("@ERR 400 idx_out_of_range", 0.02)
        elif cmd == "PRIO":
            idx = -1
            if "ssid=" in args:
                name = _kv(args, "ssid")
                for i, (s, _p, _pin) in enumerate(self._saved):
                    if s == name:
                        idx = i
                        break
                if idx < 0:
                    self._emit("@ERR 404 ssid_not_in_list", 0.02)
                    return
            else:
                try:
                    idx = int(_kv(args, "idx") or "-1")
                except ValueError:
                    idx = -1
            if 0 <= idx < len(self._saved):
                item = self._saved.pop(idx)
                self._saved.insert(0, item)
                self._source = "nvs"
                self._emit("@OK prioritized ssid=%s idx=0" % item[0], 0.03)
            else:
                self._emit("@ERR 400 idx_out_of_range", 0.02)
        elif cmd == "PIN":
            # 与固件同构:idx 必须是 key=value;on/off 是裸 token。
            # on/off 是裸 token(最后一个空白分隔词),不是 key=value
            parts = args.split()
            tail = parts[-1] if parts else ""
            on = {"on": True, "off": False}.get(tail)
            # idx=2 on 没有 & 分隔,idx 段里混着裸 token,先剥掉再转 int
            idx_seg = next((p for p in parts if p.startswith("idx=")), "")
            try:
                idx = int(idx_seg[4:])
            except ValueError:
                idx = -1
            if on is None:
                self._emit("@ERR 400 usage: @PIN idx=N on|off", 0.02)
            elif not (0 <= idx < len(self._saved)):
                self._emit("@ERR 400 idx_out_of_range", 0.02)
            else:
                self._saved[idx][2] = 1 if on else 0
                self._source = "nvs"
                self._emit("@OK pin idx=%d %s" % (idx, "on" if on else "off"), 0.03)
        elif cmd == "MODE":
            want = args.strip().upper()
            if want not in ("STA", "AP"):
                self._emit("@ERR 400 mode_must_be_STA_or_AP", 0.02)
            elif want == "AP":
                self._mode = "AP"
                self._emit("@OK mode=AP ip=%s ssid=DAP-EEFC" % AP_DEFAULT_IP, 0.2)
            else:
                self._mode = "STA"
                self._emit("@OK mode=STA", 0.2)
        elif cmd == "LED":
            v = args.strip()
            if not v:
                self._emit("@OK led pct=%d" % self._led_pct, 0.02)
            elif not v.lstrip("-").isdigit():
                self._emit("@ERR 400 pct_must_be_number", 0.02)
            else:
                pct = int(v)
                if not (1 <= pct <= 100):
                    self._emit("@ERR 400 pct_out_of_range_1_100", 0.02)
                else:
                    self._led_pct = pct
                    self._emit("@OK led pct=%d saved" % pct, 0.05)
        elif cmd == "AP":
            sub = args.strip().upper()
            if not sub:
                self._emit("@OK ap ssid=DAP-EEFC pass=%s" % self._ap_pass, 0.02)
            elif sub == "REGEN":
                # 换一个和旧的不同的假密码,便于界面测试
                self._ap_pass = "P3QW8NJKTV"
                self._emit("@OK ap ssid=DAP-EEFC pass=%s "
                           "note=applies_on_next_mode_switch" % self._ap_pass, 0.05)
            else:
                self._emit("@ERR 400 usage: @AP or @AP REGEN", 0.02)
        elif cmd == "FACTORY":
            self._saved = [[s, p, 0] for (s, p) in MOCK_FACTORY_NETS]
            self._source = "factory"
            self._emit("@OK rebooting", 0.02)
            self._emit("I (1) mock: rebooting", 0.2)
        elif cmd == "REBOOT":
            self._emit("@OK rebooting", 0.02)
            self._emit("I (1) mock: rebooting", 0.2)
        else:
            self._emit("@ERR 404 %s" % cmd, 0.02)

    def _do_connect(self, ssid: str, pw: str) -> None:
        if self._mode != "STA":
            self._emit("@ERR 409 connect_requires_STA_mode", 0.05)
            return
        match = next((row for row in MOCK_SCAN if row[0] == ssid), None)
        d = self._connect_delay
        if match is None:
            self._emit("@ERR 201 NO_AP_FOUND", d)
            return
        if pw != match[4]:
            # 真机上 WPA2 密码错误报的是 15,这里如实模拟
            self._emit("@ERR 15 HANDSHAKE_TIMEOUT", d)
            return
        self._ssid, rssi, _a, ch, _p = match
        self._rssi, self._ch = rssi, ch
        # 连上后自动落 NVS(与固件行为一致);新条目不继承 pin
        self._saved = [x for x in self._saved if x[0] != ssid]
        self._saved.insert(0, [ssid, pw, 0])
        self._source = "nvs"
        self._emit("@OK connected ssid=%s ip=%s" % (ssid, self._ip), d)


def _kv(args: str, key: str) -> Optional[str]:
    """从 'k=v&k=v' 取已解码的值。"""
    for seg in (args or "").split("&"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            if k == key:
                return dec(v)
    return None


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

    def ap_get(self) -> Optional[ApInfo]:
        """读 AP 热点 SSID 与密码(v0.8+)。

        老固件没有 @AP,会回 404 —— 此时返回 None,让界面走"读不到"分支,
        而不是把 404 当成错误弹窗(对老固件来说是正常现象)。
        """
        r = self.request("@AP", 4.0)
        if not r.ok:
            return None
        return parse_ap(r.text)

    def ap_regen(self) -> Optional[ApInfo]:
        """重新生成 AP 密码。返回新密码,失败返回 None。

        注意:新密码要等下一次切模式/重启才真正生效 —— 固件在应答的
        note= 字段里写明这一点,界面必须如实转述,不能让用户以为立刻生效。
        """
        r = self.request("@AP REGEN", 8.0)
        if not r.ok:
            return None
        return parse_ap(r.text)

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


# ====================================================================== #
# 无硬件自测
# ====================================================================== #

def run_selftest() -> int:
    """不碰硬件,验证协议解析 + 完整交互流程。返回 0 表示全过。"""
    results: List[Tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail="") -> None:
        results.append((name, bool(cond), "" if detail is None else str(detail)))

    def expect_eq(name: str, got, want) -> None:
        ok = got == want
        check(name, ok, "" if ok else "got=%r want=%r" % (got, want))

    print("=" * 72)
    print("dap_config.py 自测 (工具版本 %s)" % TOOL_VERSION)
    print("=" * 72)

    # --- 1. 编码 -----------------------------------------------------
    print("\n[1] SSID/密码编码")
    for raw in ["MyWiFi", "小美的 WiFi", "a&b=c", "p+ss", "100%", "plain"]:
        expect_eq("enc/dec 往返 %r" % raw, dec(enc(raw)), raw)
    expect_eq("空格编成 %20", enc("a b"), "a%20b")
    expect_eq("加号不被当成空格", enc("a+b"), "a%2Bb")
    check("编码不产生裸 &", "&" not in enc("a&b"))
    check("编码不产生裸 +", "+" not in enc("a+b"))

    # --- 2. 帧解析 ---------------------------------------------------
    print("\n[2] 响应帧解析")
    n = parse_scan_data("idx=3&ssid=MyWiFi&rssi=-52&auth=WPA/WPA2&ch=6")
    check("扫描帧", n is not None and n.ssid == "MyWiFi" and n.rssi == -52
          and n.auth == "WPA/WPA2" and n.channel == 6, repr(n))
    n2 = parse_scan_data("idx=0&ssid=小美的 WiFi&rssi=-70&auth=WPA2&ch=3")
    check("扫描帧含空格+中文", n2 is not None and n2.ssid == "小美的 WiFi", repr(n2))
    n3 = parse_scan_data("idx=0&ssid=a&b=c&rssi=-60&auth=OPEN&ch=1")
    check("扫描帧含 &", n3 is not None and n3.ssid == "a&b=c", repr(n3))

    st = parse_status("mode=STA ssid=MyWiFi ip=192.168.7.125 rssi=-72 ch=11")
    check("STA 状态", st is not None and st.mode == "STA" and st.ip == "192.168.7.125"
          and st.rssi == -72 and st.channel == 11, repr(st))
    st2 = parse_status("mode=STA ssid=小美的 WiFi ip=192.168.7.9 rssi=-60 ch=6")
    check("STA 状态含空格 SSID", st2 is not None and st2.ssid == "小美的 WiFi", repr(st2))
    st3 = parse_status("mode=AP ip=192.168.4.1 ssid=DAP-EEFC")
    check("AP 状态", st3 is not None and st3.mode == "AP"
          and st3.ip == "192.168.4.1" and st3.ssid == "DAP-EEFC", repr(st3))
    st4 = parse_status("mode=STA ssid=(none) ip=0.0.0.0 rssi=0 ch=0 prov=1")
    check("STA 未配网态 prov=1", st4 is not None and st4.prov is True, repr(st4))
    st5 = parse_status("mode=STA ssid=MyWiFi ip=192.168.7.125 rssi=-50 ch=6 prov=0")
    check("STA prov=0 非未配网", st5 is not None and st5.prov is False, repr(st5))

    p = parse_ping("pong fw=0.8.0 mac=AA:BB:CC:DD:EE:FC mode=STA")
    check("PING 帧", p is not None and p.fw == "0.8.0"
          and p.mac == "AA:BB:CC:DD:EE:FC" and p.mode == "STA", repr(p))

    ck = RE_CONNECT_OK.match("connected ssid=MyWiFi ip=192.168.7.125")
    check("CONNECT 成功帧", ck is not None and ck.group(1) == "MyWiFi"
          and ck.group(2) == "192.168.7.125")
    ck2 = RE_CONNECT_OK.match("connected ssid=小美的 WiFi ip=192.168.7.9")
    check("CONNECT 成功帧含空格", ck2 is not None and ck2.group(1) == "小美的 WiFi", repr(ck2))

    em = RE_ERR.match("15 HANDSHAKE_TIMEOUT")
    check("ERR 帧", em is not None and int(em.group(1)) == 15
          and em.group(2) == "HANDSHAKE_TIMEOUT")
    em2 = RE_ERR.match("400 usage: @MODE STA|AP")
    check("ERR 帧带空格说明", em2 is not None and int(em2.group(1)) == 400
          and em2.group(2) == "usage: @MODE STA|AP", repr(em2))

    li = parse_list_data("idx=0&ssid=MyWiFi&pass=***&pin=0")
    check("LIST 帧(无 pin)", li == (0, "MyWiFi", "***", 0), repr(li))
    li2 = parse_list_data("idx=1&ssid=小美的 WiFi&pass=***&pin=1")
    check("LIST 帧(pin=1)", li2 == (1, "小美的 WiFi", "***", 1), repr(li2))
    li3 = parse_list_data("idx=2&ssid=DAP&pass=***")
    check("LIST 帧 老固件无 pin 字段", li3 == (2, "DAP", "***", 0), repr(li3))

    # --- 3. 错误码解释 ----------------------------------------------
    print("\n[3] 错误码解释")
    for code in (15, 202, 204):
        check("码 %d 归入密码类" % code, "密码" in explain(code).title
              or "密码" in explain(code).action or "密码" in explain(code).detail,
              explain(code).title)
    expect_eq("201 标题", explain(201).title, "找不到这个 WiFi")
    expect_eq("409 有 STA 提示", "STA" in explain(409).action, True)
    expect_eq("未知码兜底", explain(999).title, "未知错误")

    # 405 一码两义:必须按 message 分流,否则用户会被指去做错的事
    e_start = explain_reply(405, "wifi_still_starting")
    e_nonet = explain_reply(405, "no_saved_networks")
    check("405 + wifi_still_starting -> 让用户等一会儿",
          "等" in e_start.action, e_start.action)
    check("405 + no_saved_networks -> 让用户先去配网",
          "配" in e_nonet.action or "连接" in e_nonet.action, e_nonet.action)
    check("两条 405 给的动作不一样", e_start.action != e_nonet.action,
          "%r vs %r" % (e_start.action, e_nonet.action))
    check("无 message 覆盖时回落码表",
          explain_reply(409, "scan_requires_STA_mode").title == explain(409).title, "")
    check("explain_reply 能接受带参数的 message",
          explain_reply(423, "connect_in_progress") is _MSG_OVERRIDE["connect_in_progress"],
          "")
    expect_eq("信号条 -48", rssi_bar(-48), "\u25a0\u25a0\u25a0\u25a0")
    expect_eq("信号条 -90", rssi_bar(-90), "\u25a1\u25a1\u25a1\u25a1")
    expect_eq("信号条 -70 两格", rssi_bar(-70), "\u25a0\u25a0\u25a1\u25a1")

    # --- 4. 假设备端到端 --------------------------------------------
    print("\n[4] 假设备端到端流程")
    tr = MockTransport(connect_delay=0.05)
    client = DapClient(tr)
    client.open()
    try:
        info = client.ping()
        expect_eq("PING fw", info.fw, "0.8.0")
        st = client.status()
        check("初始 STA", st.mode == "STA", repr(st))

        nets = client.scan()
        expect_eq("扫描数量", len(nets), len(MOCK_SCAN))
        check("扫描按信号降序", all(nets[i].rssi >= nets[i + 1].rssi
                                    for i in range(len(nets) - 1)),
              [n.rssi for n in nets])
        check("扫描含中文 SSID", any(n.ssid == "小美的 WiFi" for n in nets),
              [n.ssid for n in nets])

        r = client.connect("不存在的网络", "x")
        check("未知 SSID -> 201", (not r.ok) and r.code == 201, "%s %s" % (r.code, r.text))

        r = client.connect("MyWiFi", "wrong-password")
        check("错密码 -> 15", (not r.ok) and r.code == 15, "%s %s" % (r.code, r.text))

        r = client.connect("小美的 WiFi", "12345678")
        check("带空格中文 SSID 能连上", r.ok, "%s %s" % (r.code, r.text))
        m = RE_CONNECT_OK.match(r.text)
        check("成功帧含 IP", m is not None and m.group(2) == "192.168.7.125", r.text)

        rows, source = client.list_saved()
        check("连上后自动入列表", any(row[1] == "小美的 WiFi" for row in rows), rows)
        expect_eq("列表来源 nvs", source, "nvs")
        check("密码已打码", all(row[2] in ("***", "(open)") for row in rows),
              [(r[1], r[2]) for r in rows])
        check("新连入条目不带 pin", all(row[3] == 0 for row in rows),
              [(r[1], r[3]) for r in rows])

        before = len(rows)
        r = client.delete(0)
        check("删第 0 条成功", r.ok, "%s %s" % (r.code, r.text))
        rows2, _ = client.list_saved()
        expect_eq("删除后条数 -1", len(rows2), before - 1)

        r = client.delete(99)
        check("越界删除 -> 400", (not r.ok) and r.code == 400, "%s %s" % (r.code, r.text))

        # --- @PRIO:置顶/稳定移动 ---
        rows3, _ = client.list_saved()
        n_before = len(rows3)
        if n_before >= 2:
            want = rows3[-1][1]                       # 拿最后一条,确保不是第 0 位
            r = client.prioritize(n_before - 1)
            check("置顶成功", r.ok and want in r.text, "%s %s" % (r.code, r.text))
            rows4, _ = client.list_saved()
            expect_eq("置顶后条数不变", len(rows4), n_before)
            expect_eq("置顶后首位", rows4[0][1], want)
            rest_before = [x[1] for x in rows3[:-1]]
            rest_after = [x[1] for x in rows4[1:]]
            expect_eq("其余顺序保持不变(稳定移动)", rest_after, rest_before)

            r = client.prioritize(0)
            check("置顶第 0 条是幂等的", r.ok, "%s %s" % (r.code, r.text))

        r = client.prioritize(99)
        check("越界置顶 -> 400", (not r.ok) and r.code == 400,
              "%s %s" % (r.code, r.text))

        # --- @PIN:粘性固定优先 ---
        rows5, _ = client.list_saved()
        if len(rows5) >= 2:
            tgt = rows5[-1]                     # 拿最后一条,远离首位
            r = client.set_pin(tgt[0], True)
            check("pin on 成功", r.ok and ("idx=%d" % tgt[0]) in r.text
                  and "on" in r.text, "%s %s" % (r.code, r.text))
            rows6, _ = client.list_saved()
            by_idx = {row[0]: row for row in rows6}
            expect_eq("pin 后条数不变", len(rows6), len(rows5))
            expect_eq("pin 标志已落", by_idx[tgt[0]][3], 1)
            expect_eq("pin 不动物理顺序", [x[1] for x in rows6],
                      [x[1] for x in rows5])

            r = client.set_pin(tgt[0], True)    # 幂等
            check("重复 pin 是幂等的", r.ok, "%s %s" % (r.code, r.text))

            r = client.set_pin(tgt[0], False)
            check("pin off 成功", r.ok and "off" in r.text,
                  "%s %s" % (r.code, r.text))
            rows7, _ = client.list_saved()
            by_idx7 = {row[0]: row for row in rows7}
            expect_eq("pin 已摘除", by_idx7[tgt[0]][3], 0)

        r = client.set_pin(99, True)
        check("越界 pin -> 400", (not r.ok) and r.code == 400,
              "%s %s" % (r.code, r.text))
        r = client.request("@PIN idx=0", 2.0)
        check("缺 on/off -> 400 usage", (not r.ok) and r.code == 400
              and "usage" in r.text, "%s %s" % (r.code, r.text))

        r = client.set_mode("AP")
        check("切 AP 成功", r.ok and "mode=AP" in r.text, "%s %s" % (r.code, r.text))
        st = client.status()
        check("状态变 AP", st.mode == "AP" and st.ip == AP_DEFAULT_IP, repr(st))

        r = client.request("@SCAN", TIMEOUT_SCAN)
        check("AP 下扫描 -> 409", (not r.ok) and r.code == 409, "%s %s" % (r.code, r.text))

        r = client.connect("DAP", "12345678")
        check("AP 下连接 -> 409", (not r.ok) and r.code == 409, "%s %s" % (r.code, r.text))

        r = client.set_mode("STA")
        check("切回 STA", r.ok, "%s %s" % (r.code, r.text))

        r = client.set_mode("XYZ")
        check("非法模式 -> 400", (not r.ok) and r.code == 400, "%s %s" % (r.code, r.text))

        r = client.factory_reset()
        check("恢复出厂回 rebooting", r.ok, "%s %s" % (r.code, r.text))
        time.sleep(0.3)
        rows3, source3 = client.list_saved()
        expect_eq("出厂后条数", len(rows3), len(MOCK_FACTORY_NETS))
        expect_eq("出厂后来源", source3, "factory")

        # --- @AP:随机 AP 密码(v0.8+)-----------------------
        ap = client.ap_get()
        check("AP 信息能读到", ap is not None and ap.ssid.startswith("DAP-"),
              repr(ap))
        check("AP 密码不是公开常量 12345678",
              ap is not None and ap.password != "12345678",
              repr(ap.password if ap else None))
        check("AP 密码长度 10", ap is not None and len(ap.password) == 10,
              repr(ap.password if ap else None))
        old_pass = ap.password if ap else None

        ap2 = client.ap_regen()
        check("REGEN 返回新密码", ap2 is not None and ap2.password != old_pass,
              "%r -> %r" % (old_pass, ap2.password if ap2 else None))
        check("REGEN 带 note 说明",
              ap2 is not None and "applies_on_next" in ap2.note, repr(ap2))
        # 生成后 get 必须返回新值(证明写进了板子,而不是只回一次)
        ap3 = client.ap_get()
        check("REGEN 后 get 返回新密码",
              ap3 is not None and ap3.password == (ap2.password if ap2 else ""),
              repr(ap3))

        r = client.request("@AP BOGUS", 2.0)
        check("@AP 未知子命令 -> 400", (not r.ok) and r.code == 400,
              "%s %s" % (r.code, r.text))

        # --- GUI 必须真的把密码显示出来 -------------------
        # 协议层能读到 ≠ 用户看得见。这两条走 GUI 的真实回调,不是复制逻辑:
        # 只验证"文字里含密码"和"显示的确实是当前密码",不钉排版细节。
        class _Screen:
            """假的结果面板:只记录被写进去的文字。"""
            def __init__(self):
                self.shown = []
                self.state = None

            def configure(self, **kw):
                pass

            def delete(self, *a):
                self.shown = []

            def yview_moveto(self, *_a):
                pass

            def insert(self, _where, text, *tags):
                self.shown.append(text)

            def text(self):
                return "".join(self.shown)

        class _FakeApp:
            _show_result = App._show_result
            _after_show_ap_password = App._after_show_ap_password
            _after_regen_ap = App._after_regen_ap
            _log = lambda self, msg, tag="sys": None

        fake = _FakeApp()
        fake.result = _Screen()
        fake._after_show_ap_password(ap3, None)
        shown = fake.result.text()
        check("GUI 显示 AP 密码", ap3 is not None and ap3.password in shown,
              "面板文字里找不到 %r: %r" % (ap3.password if ap3 else None, shown[:120]))
        check("GUI 显示的 SSID 正确", ap3 is not None and ap3.ssid in shown,
              repr(shown[:120]))

        fake.result = _Screen()
        fake._after_show_ap_password(None, None)
        old_fw = fake.result.text()
        # 老固件分支只允许把 12345678 当"别再用这个"的警告词,不允许当当前密码。
        # 判据用"有没有把它挂在『密码:』后面"——那才是用户会照着去连热点的位置。
        check("老固件不把 12345678 当当前密码",
              "密码:12345678" not in old_fw and "密码: 12345678" not in old_fw,
              "把公开常量当成当前密码显示了: %r" % old_fw[:160])
        check("老固件明确提示升级", "0.8" in old_fw, repr(old_fw[:120]))

        fake.result = _Screen()
        fake._after_regen_ap(ap2, None)
        regen_shown = fake.result.text()
        check("GUI 显示 REGEN 后的新密码",
              ap2 is not None and ap2.password in regen_shown, repr(regen_shown[:120]))

        r = client.request("@NOSUCHCMD", 2.0)
        check("未知命令 -> 404", (not r.ok) and r.code == 404, "%s %s" % (r.code, r.text))

        # --- 指示灯亮度 ---
        expect_eq("@LED 查询默认值", client.led_get(), 4)
        r = client.led_set(2)
        check("@LED 设 2%", r.ok and "pct=2" in r.text, "%s %s" % (r.code, r.text))
        expect_eq("@LED 回读 2%", client.led_get(), 2)
        r = client.led_set(0)
        check("@LED 0 被拒", (not r.ok) and r.code == 400, "%s %s" % (r.code, r.text))
        r = client.led_set(101)
        check("@LED 101 被拒", (not r.ok) and r.code == 400, "%s %s" % (r.code, r.text))
        r = client.request("@LED abc", 2.0)
        check("@LED 非数字被拒", (not r.ok) and r.code == 400, "%s %s" % (r.code, r.text))
        client.led_set(4)

        # 重试策略:本地端口故障不该被重发;@CONNECT 超时也不该被重发
        check("port_closed 不重发", DapClient._timed_out(
            Reply(ok=False, code=408, text="port_closed")) is False)
        check("write_failed 不重发", DapClient._timed_out(
            Reply(ok=False, code=408, text="write_failed: x")) is False)
        check("真超时算可重发", DapClient._timed_out(
            Reply(ok=False, code=408, text="TIMEOUT")) is True)
        check("@CONNECT 不在重试白名单", "@CONNECT" not in DapClient.RETRYABLE)
        check("@PING 在重试白名单", "@PING" in DapClient.RETRYABLE)

        # 重试次数实测:造一个只吞不回的话筒,数它被写了几次
        class SilentTransport(Transport):
            name = "SILENT"

            def __init__(self):
                self.writes = 0
                self._open = False

            def open(self):
                self._open = True

            def close(self):
                self._open = False

            @property
            def is_open(self):
                return self._open

            def write(self, data):
                self.writes += 1

            def read(self, size=4096, timeout=0.05):
                return b""

        silent = SilentTransport()
        sc = DapClient(silent)
        sc.open()
        r = sc.request("@PING", 0.15)
        check("幂等命令超时共发 3 次", silent.writes == 3, "writes=%d" % silent.writes)
        check("重试后仍超时 -> 408", (not r.ok) and r.code == 408, "%s" % r.text)
        silent.writes = 0
        r = sc.request("@CONNECT ssid=x&pass=y", 0.15)
        check("@CONNECT 超时只发 1 次", silent.writes == 1, "writes=%d" % silent.writes)
        sc.close()

        # 超时路径:端口关掉后 request 应该快速返回 408 而不是挂住
        client.close()
        r = client.request("@PING", 0.5)
        check("端口关闭 -> 408", (not r.ok) and r.code == 408, "%s %s" % (r.code, r.text))
    finally:
        try:
            client.close()
        except Exception:
            pass

    # --- 调试后端(只验路径与构造,不真的起进程)----------------------
    # 真起进程的自测会让 --selftest 变得又慢又依赖第三方工具是否装好,
    # 这里只验证"路径解析对不对、命令拼得对不对、缺文件时会不会老实报错"。
    try:
        import dap_backends as B
        root = B.tools_root()
        check("tools_root 可解析", isinstance(root, str) and bool(root), root)

        oc = B.OpenOcdBackend("dap.local", "target/stm32f1x.cfg")
        cmd = oc.command_line()
        check("OpenOCD 命令含设备地址", "dap.local" in cmd, cmd[:80])
        check("OpenOCD 命令含 target", "stm32f1x" in cmd, cmd[-40:])
        check("OpenOCD 命令显式指定 swd", "transport select swd" in cmd, "")
        check("未启动时 gdb_ready 为假", oc.gdb_ready() is False, "")

        ub = B.UsbipBackend("dap.local")
        check("usbip 命令用固定 busid", B.USBIP_BUSID in ub.command_line(),
              ub.command_line())
        # 明确断言:绝不能生成 usbip list —— 那条命令会永久挂起并拖死固件
        check("usbip 命令不含 list", " list" not in ub.command_line().lower(), "")

        el = B.ElaphureLinkBackend("dap.local")
        check("elaphureLink 配置文件路径可拼出",
              el.cfg.endswith("elaphureLink.Wpf.exe.config"), el.cfg)

        # 地址切换要贯穿三个后端,否则换网络后有两个连的还是旧地址
        mgr = B.BackendManager("dap.local")
        mgr.set_addr("10.0.0.9")
        check("切换地址后 openocd 同步",
              "10.0.0.9" in mgr.openocd.command_line(), mgr.openocd.command_line()[:70])
        check("切换地址后 usbip 同步", mgr.usbip.device_addr == "10.0.0.9",
              mgr.usbip.device_addr)
        check("切换地址后 elaphureLink 同步",
              mgr.elaphurelink.device_addr == "10.0.0.9",
              mgr.elaphurelink.device_addr)
        mgr.stop_all()   # 没有在跑的进程时也必须安全
        check("stop_all 在无进程时安全", True, "")

        # --- AT32(usbip + atopenocd)---
        base = B.find_atopenocd()
        check("找到 atopenocd", base is not None, base or "未找到")
        mgr2 = B.BackendManager("dap.local")
        if mgr2.has_at32():
            a = mgr2.at32_openocd
            check("AT32 后端用 usbip 形态", a.flavor == "usbip", a.flavor)
            cmd = a.command_line()
            # 关键断言:AT32 那条必须走 cmsis-dap(本地 USB),不能走
            # elaphurelink(那个没有 at32f4xx flash 驱动,烧不了)
            check("AT32 命令走 cmsis-dap", "interface/cmsis-dap.cfg" in cmd, cmd[:90])
            check("AT32 命令不含 elaphurelink", "elaphurelink" not in cmd, "")
            check("AT32 命令带 scripts 目录", "-s" in cmd, "")
            at = a.list_targets()
            check("AT32 target 列表非空", len(at) > 100, "共 %d 个" % len(at))
            check("AT32 有 at32f403axG", "at32f403axG" in at, "")
            # 反证:elaphurelink 那套 scripts 里没有 AT32,这正是必须分两条路的原因
            el_targets = B.OpenOcdBackend("dap.local").list_targets()
            check("elaphurelink 版没有 AT32 target",
                  not any(t.startswith("at32f4") for t in el_targets),
                  "有 %d 个 target" % len(el_targets))
        mgr2.stop_all()

        # --- 可达性检查(不联网也能验证逻辑)---
        ub2 = B.UsbipBackend("")
        ok_noaddr, _ = ub2.reachable(timeout=0.5)
        check("空地址时可达性返回失败", ok_noaddr is False, "")
        ub3 = B.UsbipBackend("192.0.2.1")   # TEST-NET,保证不可达
        ok_bad, msg_bad = ub3.reachable(timeout=0.5)
        check("不可达地址返回失败", ok_bad is False, "")
        check("不可达时提示网段问题", "网段" in msg_bad, msg_bad[:40])

        # --- 回归:OpenOCD 日志回调不能自递归 -------------------------
        # 曾经的 bug:start() 把 self.on_line 换成 _pump_wrap,stop() 不复原;
        # 第二次 start() 时 _user_on_line 取到的就是 _pump_wrap 自己,
        # 每行日志无限递归 → RecursionError 被吞 → 日志页永久静默。
        #
        # **必须真的走 start()**,不能手抄 start() 里那两行去"模拟":
        # 上一版就是这么写的,结果把 start() 改回 bug 版后自测依然全绿
        # (假绿)。这里把 exe 换成 cmd.exe(Windows 一定有)真起进程,
        # 反复启停三轮,断言的正是 start() 自己的行为。
        lines = []
        oc = B.OpenOcdBackend("dap.local", on_line=lines.append,
                              flavor="usbip", scripts_dir=None)
        import os as _os
        oc.exe = _os.path.join(_os.environ.get("SystemRoot", r"C:\Windows"),
                               "System32", "cmd.exe")
        oc.args = ["/c", "echo Info : Listening on port 3333 for gdb connections"]
        oc.cwd = None
        real = sys.platform == "win32" and _os.path.isfile(oc.exe)
        if real:
            for _ in range(3):          # 模拟反复点「启动」/进程自己退出后再点
                oc.start()
                time.sleep(0.4)
                oc.stop()
            oc.start()
            time.sleep(0.4)
            check("反复 start 后 _user_on_line 仍是用户回调(不是包装自己)",
                  oc._user_on_line is not oc._pump_wrap,
                  "_user_on_line=%r" % (oc._user_on_line,))
            try:
                oc._pump_wrap("普通一行")
                check("反复 start 后日志回调不自递归", True, "")
            except RecursionError:
                check("反复 start 后日志回调不自递归", False, "RecursionError")
            oc.stop()
        else:
            # 非 Windows:起不了进程,退化成只验证"包装不会套包装"
            oc.start = lambda: None          # 不真起
            for _ in range(3):
                if not oc._user_captured:
                    oc._user_on_line = oc.on_line
                    oc._user_captured = True
                oc.on_line = oc._pump_wrap
            check("反复 start 后 _user_on_line 仍是用户回调(不是包装自己)",
                  oc._user_on_line is not oc._pump_wrap, "")
        oc._pump_wrap("Listening on port 3333 for gdb connections")
        check("gdb 端口嗅探仍生效", oc.gdb_ready() is True, "")

        # --- 回归:超时后的迟到帧不能串到下一条命令 -------------------
        # 场景一:迟到帧在**写命令之前**就已在队列里 -> 靠写前排空。
        mt = MockTransport(connect_delay=0.0)
        c = DapClient(mt)
        c.open()
        c._frames.put("@OK mode=STA ssid=迟到帧 ip=1.2.3.4")  # 冒充残留
        info = c.ping(timeout=2.0)
        check("残留帧不影响下一条命令",
              info is not None and getattr(info, "fw", None) is not None,
              "got=%r" % (info,))
        c.close()

        # 场景二:迟到帧在**写命令之后**才到 -> 写前排空拦不住。
        # 真机可复现:板子卡 0.5s 才回 @STATUS,用户已经点了扫描,
        # STATUS 的 "mode=STA ..." 就成了 SCAN 的终结帧,scan() 返回 0。
        # 现在靠终结帧形态判别(_looks_like_reply_to)丢掉它。
        class _LateStatus(MockTransport):
            def write(self, data):
                line = data.decode("utf-8", "replace").strip()
                if line.upper().startswith("@SCAN"):
                    # SCAN 写出 50ms 后,上一条 STATUS 的迟到应答才到
                    self._emit("@OK mode=STA ssid=MyWiFi ip=192.168.7.125 "
                               "rssi=-48 ch=6 prov=0", 0.05)
                super().write(data)

        mt2 = _LateStatus(connect_delay=0.0)
        c2 = DapClient(mt2)
        c2.open()
        nets = c2.scan()
        check("写命令之后才到的迟到帧也不串味", len(nets) > 0,
              "scan() 返回 %d 个网络(应 >0;串味会返回 0)" % len(nets))
        c2.close()

        # --- 形态判别本身(直接测被测函数,不抄逻辑)-------------------
        # 第一版把 expect 字典的键写成 "@SCAN",而 _cmd_name() 返回的是
        # "SCAN",于是永远查不到 -> 全部走保守分支 -> 等于没做判断。
        m = MockTransport(connect_delay=0.0)
        c3 = DapClient(m)
        check("SCAN 不认 STATUS 的回复",
              not c3._looks_like_reply_to("@SCAN", "@OK", "mode=STA ssid=x"), "")
        check("SCAN 认自己的回复",
              c3._looks_like_reply_to("@SCAN", "@OK", "scan count=6"), "")
        check("STATUS 认自己的回复",
              c3._looks_like_reply_to("@STATUS", "@OK", "mode=STA ssid=x"), "")
        check("PING 认自己的回复",
              c3._looks_like_reply_to("@PING", "@OK", "pong fw=0.8.0"), "")
        check("未登记命令保守放行",
              c3._looks_like_reply_to("@FOO", "@OK", "whatever"), "")
        c3.close()

        # --- 回归:@DATA 迟到帧不能串(形态判别挡不住)------------------
        # 上一条 SCAN 超时后,它的 @DATA 会在下一条命令写出之后才到。
        # @DATA 是数据载体,没有可用形态 —— idx=...&ssid=... 对 SCAN 和 LIST
        # 长得一样,所以必须在写新命令前把上一条的应答收尾(_await_stale_reply)。
        #
        # 夹具说明:不能用 MockTransport —— 它在 write() 里立刻 _handle() 并
        # 应答,永远构造不出"超时"。这里用最小 Transport,时序全由测试掌控。
        class _Scripted(Transport):
            """按第 N 次 write 决定何时吐哪些帧。"""

            name = "SCRIPTED"

            def __init__(self, plan):
                self._plan = plan
                self._open = False
                self._q: List[Tuple[float, str]] = []
                self._writes = 0

            def open(self):
                self._open = True

            def close(self):
                self._open = False

            @property
            def is_open(self):
                return self._open

            def write(self, data):
                self._writes += 1
                for delay, items in self._plan.get(self._writes, []):
                    due = time.monotonic() + delay
                    for it in items:
                        self._q.append((due, it))

            def read(self, size=4096, timeout=0.05):
                time.sleep(0.01)
                now = time.monotonic()
                ready = [ln for (t0, ln) in self._q if t0 <= now]
                self._q = [(t0, ln) for (t0, ln) in self._q if t0 > now]
                if not ready:
                    return b""
                return ("\n".join(ready) + "\n").encode("utf-8")

        tr_stale = _Scripted({
            # 第 1 条:正常 PING。真机上 wait_ready() 一定会先做过这一步,
            # 它是"设备是活的"的证据 —— 欠账机制的前提(见 _ever_ok 说明)。
            1: [(0.02, ["@OK pong fw=0.8.0 mac=AA:BB:CC:DD:EE:FC mode=STA"])],
            # 第 2 条(@SCAN):应答 0.45s 才到,而 PC 只等 0.25s -> 超时
            2: [(0.45, ["@DATA idx=0&ssid=STALE_A&rssi=-40&auth=WPA2&ch=1",
                        "@DATA idx=1&ssid=STALE_B&rssi=-41&auth=WPA2&ch=2",
                        "@OK scan count=2"])],
            # 第 3 条(@SCAN):0.45s 才回,晚于迟到帧
            3: [(0.45, ["@DATA idx=0&ssid=RealNet&rssi=-50&auth=WPA2&ch=6",
                        "@OK scan count=1"])],
        })
        c4 = DapClient(tr_stale)
        c4.open()
        c4.ping()                       # 先证明设备活着(等价于 wait_ready)
        r = c4.request("@SCAN", 0.25, retries=0)
        check("第一次 SCAN 确实超时了(构造前提)", r.code == 408, "%s" % r.code)
        nets2 = c4.scan()
        check("迟到的 @DATA 不会串进下一次 SCAN",
              not any("STALE" in n.ssid for n in nets2),
              "%r" % [n.ssid for n in nets2])
        check("迟到的 @DATA 也不会吞掉真实结果",
              any(n.ssid == "RealNet" for n in nets2),
              "%r" % [n.ssid for n in nets2])
        c4.close()

        # --- 回归:迟到的 @ERR 不能被下一条命令认领 -------------------
        # @ERR 的码是通用的,任何命令都可能收到,形态判别对 @ERR 直接放行。
        # 实测:@CONNECT 超时后迟到的 "@ERR 15 HANDSHAKE_TIMEOUT" 被下一条
        # @STATUS 认领,status() 抛"取状态失败(HANDSHAKE_TIMEOUT)"。
        tr_err = _Scripted({
            1: [(0.02, ["@OK pong fw=0.8.0 mac=AA:BB:CC:DD:EE:FC mode=STA"])],
            2: [(0.40, ["@ERR 15 HANDSHAKE_TIMEOUT"])],
            3: [(0.70, ["@OK mode=STA ssid=Real ip=10.0.0.5 rssi=-50 ch=6 prov=0"])],
        })
        c5 = DapClient(tr_err)
        c5.open()
        c5.ping()                       # 先证明设备活着
        r = c5.request("@CONNECT ssid=a&pass=b", 0.25, retries=0)
        check("CONNECT 确实超时了(构造前提)", r.code == 408, "%s" % r.code)
        try:
            st = c5.status()
            check("迟到的 @ERR 不会被下一条命令认领", st.ssid == "Real", repr(st))
        except Exception as exc:
            check("迟到的 @ERR 不会被下一条命令认领", False, repr(exc))
        c5.close()

        # --- 回归:迟到帧晚于"结算窗口"也必须拦住 ---------------------
        # 上一版用固定的 1.5s 结算窗口(写新命令之前先等上一条的剩余应答)。
        # 那个窗口挡不住长阻塞命令:@CONNECT 在板子上要轮询 25s,而 PC 侧
        # 超时后下一个命令可能早就写出去了。实测(2026-10-29)迟到 2.5s 的
        # "@ERR 15 HANDSHAKE_TIMEOUT" 仍被 status() 认领。
        #
        # 现在改成计数欠账:固件串行 => 上一条的终结帧一定先于本条的任何
        # 输出,收到第一个终结帧就销一笔账,与"迟到多久"无关。
        tr_late = _Scripted({
            1: [(0.02, ["@OK pong fw=0.8.0 mac=AA:BB:CC:DD:EE:FC mode=STA"])],
            2: [],                       # CONNECT 在 1.5s 内一声不吭 -> 超时
            3: [(2.50, ["@ERR 15 HANDSHAKE_TIMEOUT",
                        "@OK mode=STA ssid=Real ip=10.0.0.5 rssi=-50 ch=6 prov=0"])],
        })
        c6 = DapClient(tr_late)
        c6.open()
        c6.ping()
        r = c6.request("@CONNECT ssid=a&pass=b", 0.25, retries=0)
        check("长阻塞命令确实超时(构造前提)", r.code == 408, "%s" % r.code)
        try:
            st = c6.status()
            check("迟到 2.5s 的 @ERR 仍被拦住", st.ssid == "Real", repr(st))
        except Exception as exc:
            check("迟到 2.5s 的 @ERR 仍被拦住", False, repr(exc))
        c6.close()

        # --- 回归:欠账不能永远挂着(否则每条命令都白等/吃错帧)---------
        # 板子被拔掉/复位时,欠的那笔账永远收不到。必须有作废机制,否则
        # 之后每条命令的第一帧都会被当成迟到帧吃掉。
        tr_dead = _Scripted({
            1: [(0.02, ["@OK pong fw=0.8.0 mac=AA:BB:CC:DD:EE:FC mode=STA"])],
            2: [],                       # 超时且永不回
            3: [(0.02, ["@OK mode=STA ssid=Real ip=10.0.0.5 rssi=-50 ch=6 prov=0"])],
        })
        c7 = DapClient(tr_dead)
        c7.open()
        c7.ping()
        c7.request("@STATUS", 0.2, retries=0)      # 超时,记一笔
        # 模拟"板子被复位、账已过期":直接把过期时刻推到过去
        c7._stale_expire = 0.0
        try:
            st = c7.status()
            check("欠账过期后不吃掉真应答", st.ssid == "Real", repr(st))
        except Exception as exc:
            check("欠账过期后不吃掉真应答", False, repr(exc))
        c7.close()

        # --- 回归:排空时不能把"销账用的终结帧"一起扔掉 -----------------
        # 残留帧若在"上一条超时返回"与"下一条写出"之间到达(间隙几百毫秒,
        # 很常见),它已经躺在队列里,会被写前的排空循环一扫而光 ——
        # 包括那个本该用来销账的 @OK。结果:欠账永远挂着,下一条命令自己的
        # 真 @DATA 反被当成残留吃掉 -> 408 且重试全废。
        # 这是实测发现的,不是推演。
        tr_drain = _Scripted({
            1: [(0.02, ["@OK pong fw=0.8.0 mac=AA:BB:CC:DD:EE:FC mode=STA"])],
            # 第 2 条 SCAN:PC 0.25s 超时,残留 0.30s 才到
            2: [(0.30, ["@DATA idx=0&ssid=STALE&rssi=-40&auth=WPA2&ch=1",
                        "@OK scan count=1"])],
            3: [(0.10, ["@DATA idx=0&ssid=RealNet&rssi=-50&auth=WPA2&ch=6",
                        "@OK scan count=1"])],
        })
        c8 = DapClient(tr_drain)
        c8.open()
        c8.ping()
        c8.request("@SCAN", 0.25, retries=0)
        time.sleep(0.40)                   # 残留帧此时已在队列里
        check("残留帧确实在队列里等着(构造前提)",
              c8._stale_owed == 1, "owed=%r" % c8._stale_owed)
        try:
            nets8 = c8.scan()
            check("排空残留帧时不能连销账帧一起扔",
                  [n.ssid for n in nets8] == ["RealNet"],
                  "%r" % [n.ssid for n in nets8])
        except Exception as exc:
            # 别让它炸掉整个 try 块 —— 那会让这条的具体名字消失,只报一句
            # 笼统的"调试后端模块可用",排查时看不出是哪条挂了。
            check("排空残留帧时不能连销账帧一起扔", False, repr(exc)[:80])
        c8.close()

        # --- 回归:关窗后不得再碰已销毁的 Tk 控件 ---------------------
        # 真实现场:用户在慢任务(@CONNECT 最多 32s / 恢复出厂)期间关窗,
        # worker 线程稍后仍投递 job_done,而 root 已 destroy —— 回调里一碰
        # Text/Button 就抛 TclError: invalid command name "..."。
        # 不修复的表现是:关窗瞬间控制台刷一段红色 Traceback。
        #
        # 这里必须**真调用** _drain_events / _on_close,不能读源码判断:
        # 上一版用 inspect.getsource(),在冻结 exe 里没有源码,直接
        # "could not get source code" 把整块测试炸掉 —— 而且那种写法
        # 只证明"代码里出现过 _closing 这三个字",不证明行为对。
        # 用一个假 self(只带 _closing/_events/root 桩)调真函数。
        class _FakeRoot:
            def __init__(self):
                self.after_called = 0

            def after(self, _ms, _fn=None):
                self.after_called += 1
                raise RuntimeError("root 已销毁")   # 模拟 Tk 的 TclError

        class _FakeApp:
            _drain_events = App._drain_events
            _on_close = App._on_close

            # POLL_MS 必须带上:_drain_events 末尾要 self.root.after(self.POLL_MS, ...),
            # 少了它会抛 AttributeError,而那句正好包在 try/except(Exception) 里,
            # 异常被静默吞掉 —— 表现是 after 根本没被调用,看起来像"功能坏了"。
            POLL_MS = 60

            def __init__(self, closing):
                self._closing = closing
                self.events = queue.Queue()
                self.events.put(("log", ("x", "sys")))
                self.root = _FakeRoot()

        # 1) 已关窗:_drain_events 必须立刻返回,不消费事件、不排 after
        fa = _FakeApp(closing=True)
        err = None
        try:
            fa._drain_events()
        except Exception as exc:
            err = exc
        check("_closing=True 时 _drain_events 不碰控件",
              err is None and fa.root.after_called == 0,
              "err=%r after=%d" % (err, fa.root.after_called))
        check("_closing=True 时事件不消费",
              fa.events.qsize() == 1, "qsize=%d" % fa.events.qsize())

        # 2) 未关窗:必须真的重排 after(证明上面那条不是因为函数空转才过)
        # after() 外面包着 try/except(Exception),桩里抛异常会被静默吞掉,
        # 所以桩**不能抛**,要正常返回并在返回前记账。
        class _FakeRootOK(_FakeRoot):
            def after(self, _ms, _fn=None):
                self.after_called += 1
                return "after#1"       # 正常返回,模拟成功重排

        fa2 = _FakeApp(closing=False)
        fa2.root = _FakeRootOK()
        fa2._append_log = lambda text, tag: None
        try:
            fa2._drain_events()
        except Exception:
            pass
        check("_closing=False 时 _drain_events 会重排 after",
              fa2.root.after_called == 1,
              "after_called=%d" % fa2.root.after_called)

        # 3) _on_close 必须置位 _closing(否则 1/2 两条都白搭)
        fa3 = _FakeApp(closing=False)
        fa3.jobs = queue.Queue()
        fa3.client = None
        fa3.server = None
        try:
            fa3._on_close()
        except Exception:
            pass                       # 关窗后半段会碰不存在的属性,不关心
        check("_on_close 置位 _closing", fa3._closing is True,
              "_closing=%r" % fa3._closing)

        # --- 用户设置持久化(设备地址 / target)-----------------------
        # 通过临时目录测,绝不碰用户真实的 %APPDATA%。
        # 这里直接调用 load_settings/save_settings —— 它们是模块级函数,
        # 测的是真实代码路径,不是复制一份逻辑。
        import tempfile as _tf
        import shutil as _sh
        _old_appdata = os.environ.get("APPDATA")
        _tmpdir = _tf.mkdtemp(prefix="dapcfg_")
        try:
            os.environ["APPDATA"] = _tmpdir
            check("设置路径在 %APPDATA% 下",
                  "无线调试器" in settings_path()
                  and os.path.dirname(settings_path()).startswith(_tmpdir),
                  settings_path())

            # 空环境:读出来必须是空 dict(不能抛)
            expect_eq("没有设置文件时返回空", load_settings(), {})

            ok, _p = save_settings({"addr": "192.168.7.125",
                                    "target": "stm32f4x",
                                    "at32_target": "at32f403axG"})
            check("保存设置成功", ok, _p)
            got = load_settings()
            expect_eq("地址读回", got.get("addr"), "192.168.7.125")
            expect_eq("target 读回", got.get("target"), "stm32f4x")
            expect_eq("at32 target 读回", got.get("at32_target"),
                      "at32f403axG")

            # 直接看**文件内容**,而不是只走 load_settings ——
            # 上一版只测了 load 的过滤,把 save 的白名单去掉(让它把所有
            # 传入键都写进磁盘)测试照样全绿。那等于"写进去脏东西但读不
            # 回来",虽然当前无害,但文件会被污染,而且将来若有别的读取方
            # (备份、诊断)就露出来了。
            import json as _json
            save_settings({"addr": "a", "evil": "should_not_survive"})
            with open(settings_path(), encoding="utf-8") as f:
                on_disk = _json.load(f)
            check("save 只写白名单键",
                  "evil" not in on_disk
                  and set(on_disk) <= set(SETTINGS_KEYS) | {"version"},
                  repr(sorted(on_disk)))

            # 读路径也要挡:手改文件塞进来的键不许出现
            with open(settings_path(), encoding="utf-8") as f:
                raw = _json.load(f)
            raw["evil"] = "should_not_survive"
            raw["version"] = SETTINGS_VERSION
            with open(settings_path(), "w", encoding="utf-8") as f:
                _json.dump(raw, f)
            expect_eq("白名单外的键被忽略", "evil" in load_settings(), False)

            # 损坏的 JSON:必须静默回落,不能抛
            with open(settings_path(), "w", encoding="utf-8") as f:
                f.write("{ this is not json")
            expect_eq("损坏文件返回空", load_settings(), {})

            # 版本不符:当作没有设置,但**不能删用户文件**
            with open(settings_path(), "w", encoding="utf-8") as f:
                _json.dump({"version": 999, "addr": "x"}, f)
            expect_eq("版本不符返回空", load_settings(), {})
            check("版本不符不删文件", os.path.exists(settings_path()), "")

            # 空字符串不该被保存(否则会把用户填的空当成有效设置)
            save_settings({"addr": "", "target": "  "})
            got2 = load_settings()
            check("空值不写入", not got2.get("addr") and not got2.get("target"),
                  repr(got2))

            # "全是默认值"必须删文件,而不是写一份和默认一样的 JSON。
            # 实测踩过:恢复默认删掉文件后,_apply_addr 里那句 _persist 会
            # 立刻把它写回来,用户看到文件还在,以为没重置成功。
            class _A:
                _persist_settings = App._persist_settings

                class V:
                    def __init__(self, v):
                        self.v = v

                    def get(self):
                        return self.v

                def __init__(self, addr, tgt, at32):
                    self._settings = {}
                    self._settings_saved = {"addr": "x"}   # 强制"有变化"
                    self.logs = []
                    self.addr_var = self.V(addr)
                    self.target_var = self.V(tgt)
                    self.at32_var = self.V(at32)

                def _log(self, msg, kind="sys"):
                    self.logs.append(msg)

            save_settings({"addr": "192.168.7.9"})   # 先造一个文件出来
            check("构造前提:设置文件已存在",
                  os.path.exists(settings_path()), settings_path())
            a = _A(DEFAULT_SETTINGS["addr"], DEFAULT_SETTINGS["target"],
                   DEFAULT_SETTINGS["at32_target"])
            a._persist_settings()
            check("全是默认值时删掉设置文件",
                  not os.path.exists(settings_path()), settings_path())
            check("删文件时记了日志", any("默认" in m for m in a.logs),
                  repr(a.logs))

            # 非默认值要写盘 —— 反证上面那条不是"永远删"
            b = _A("192.168.7.77", DEFAULT_SETTINGS["target"],
                   DEFAULT_SETTINGS["at32_target"])
            b._persist_settings()
            expect_eq("非默认值会写盘", load_settings().get("addr"),
                      "192.168.7.77")
        finally:
            if _old_appdata is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = _old_appdata
            _sh.rmtree(_tmpdir, ignore_errors=True)

        # --- 回归:usbip 驱动缓存必须可失效 ---------------------------
        # 曾经的坑:驱动状态"进程内只查一次",用户在界面上被提示去装驱动,
        # 装完回来仍是"驱动未装",只能重启工具。
        check("_usbip_driver_cached 支持 force 重新检测",
              "force" in App._usbip_driver_cached.__code__.co_varnames,
              str(App._usbip_driver_cached.__code__.co_varnames))
        check("有 _recheck_usbip 入口", hasattr(App, "_recheck_usbip"), "")

        # --- 回归:串口 hwid 预筛 -------------------------------------
        check("DAP 的 VID:PID 能被识别",
              _is_likely_dap_port("USB VID:PID=303A:1001 SER=AC:A7"),
              "")
        check("非 DAP 的串口不会被误判",
              not _is_likely_dap_port("USB VID:PID=1A86:55D3 SER=5C36"),
              "")
        check("空 hwid 不误判", not _is_likely_dap_port(""), "")
        check("探测默认不再附加 settle 等待",
              probe_port.__defaults__[1] == 0.0,
              "defaults=%r" % (probe_port.__defaults__,))
    except Exception as exc:
        check("调试后端模块可用", False, repr(exc))

    # --- 汇总 --------------------------------------------------------
    print("\n" + "=" * 72)
    bad = [r for r in results if not r[1]]
    for name, ok, detail in results:
        print(("  PASS  " if ok else "  FAIL  ") + name + (("   <- " + detail) if detail else ""))
    print("-" * 72)
    print("共 %d 项,%d 通过,%d 失败" % (len(results), len(results) - len(bad), len(bad)))
    if bad:
        print("自测未通过")
        return 1
    print("自测全部通过")
    return 0


# ====================================================================== #
# 命令行小工具
# ====================================================================== #

def run_list() -> int:
    ports = enumerate_ports()
    if not ports:
        print("没有找到任何串口。检查 USB 线是不是插在板子的 native USB 口上。")
        return 1
    print("串口列表:")
    for device, desc in ports:
        print("  %-8s %s" % (device, desc))
    print("\n逐个发 @PING 探测(每个最多 1.5 秒)…")
    found = []

    def progress(device: str, desc: str) -> None:
        print("  探测 %-8s …" % device, end="", flush=True)

    for device, desc in ports:
        info = probe_port(device)
        if info:
            found.append((device, info))
            print("\r  %-8s -> DAP  fw=%s mac=%s mode=%s"
                  % (device, info.fw, info.mac, info.mode))
        else:
            print("\r  %-8s -> 不是 DAP" % device)
    print("\n找到 %d 个无线调试器。" % len(found))
    return 0 if found else 2


def run_raw(port: Optional[str], line: str, timeout: float,
            mock: bool = False) -> int:
    if mock:
        print("使用假设备")
        tr: Transport = MockTransport(connect_delay=0.1)
    else:
        if not port:
            found = probe_all()
            if not found:
                print("没探测到设备,请用 --port 指定串口(或用 --mock 试假设备)。")
                return 2
            port = found[0][0]
            print("使用 %s" % port)
        tr = SerialTransport(port)
    client = DapClient(tr, on_log=lambda s: print("  [log] " + s))
    try:
        client.open()
    except Exception as exc:
        print("打开 %s 失败: %s" % (tr.name, exc))
        return 2
    try:
        # 开串口 = 复位板子;CDC 端点没就绪时 write 会满超时失败。
        # 不等就绪就发命令,拿到的一定是 write_failed。
        if not client.wait_ready(timeout=8.0, need_wifi=False):
            print("设备没就绪(等了 8 秒):检查是不是插在 native USB 口,"
                  "或者板子是不是正在狂重启。")
            return 2
        reply = client.request(line, timeout)
        for d in reply.data:
            print("  @DATA " + d)
        if reply.ok:
            print("  @OK " + reply.text)
        else:
            info = explain_reply(reply.code, reply.text)
            print("  @ERR %d %s" % (reply.code, reply.text))
            print("  -> %s:%s" % (info.title, info.detail))
        return 0 if reply.ok else 1
    finally:
        client.close()


# ====================================================================== #
# 图形界面
# ====================================================================== #

class App:
    """tkinter 界面。刻意不用 ttkbootstrap 之类的第三方主题,保证零依赖。"""

    POLL_MS = 60

    # ---- 配色 ----------------------------------------------------------
    # 浅色卡片式布局。刻意用一组固定色值而不是跟系统主题走,保证在
    # 别人的机器(尤其 Windows 深色模式)上也是同一个样子。
    C_BG        = "#f4f6f8"   # 窗口底色
    C_CARD      = "#ffffff"   # 卡片/表格底
    C_BORDER    = "#dfe3e8"   # 分隔线
    C_TEXT      = "#1f2933"   # 主文字
    C_TEXT_DIM  = "#6b7684"   # 次要文字
    C_PRIMARY   = "#2563eb"   # 主色(按钮/选中)
    C_PRIMARY_H = "#1d4ed8"
    C_OK        = "#15803d"
    C_OK_BG     = "#ecfdf3"
    C_ERR       = "#b91c1c"
    C_ERR_BG    = "#fef2f2"
    C_WARN      = "#a16207"
    C_WARN_BG   = "#fffbeb"
    C_INFO_BG   = "#f8fafc"

    def __init__(self, port: Optional[str] = None, mock: bool = False):
        import tkinter as tk
        from tkinter import font as tkfont, messagebox, ttk

        self._tk = tk
        self._ttk = ttk
        self._messagebox = messagebox
        self.mock = mock
        self.client: Optional[DapClient] = None
        self.status: Optional[Status] = None

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.jobs: "queue.Queue[Optional[tuple]]" = queue.Queue()

        self._scan_rows: List[Network] = []
        self._sort_col = "rssi"
        self._sort_desc = True
        self._busy = False
        self._closing = False
        self._log_lines = 0
        self._led_timer = None
        # 用户界面偏好(设备地址 / 两个 target)。文件缺失或损坏时是空 dict,
        # 界面各处用 .get(key, 默认值) 兜底 —— 见 load_settings() 的说明。
        self._settings = load_settings()
        # 已经写进磁盘的那份,用于"只在真的变了才写"
        self._settings_saved = dict(self._settings)

        self.root = tk.Tk()
        self.root.title("无线调试器 · 配网工具")
        self._base_title = self.root.title()
        self.root.geometry("920x760")
        self.root.minsize(820, 660)
        self.root.configure(bg=self.C_BG)

        # tk 变量必须在 Tk() 之后创建(否则 "no default root window")
        self.show_board_log = tk.BooleanVar(self.root, value=True)
        self.show_frames = tk.BooleanVar(self.root, value=True)
        self.autoscroll = tk.BooleanVar(self.root, value=True)

        mono = self._pick_font()
        ui = self._pick_ui_font(tkfont)
        self._mono = mono
        self._ui = ui
        self._style = ttk.Style()
        try:
            # clam 比 vista 好改色:vista 是系统主题,大部分样式改不动
            self._style.theme_use("clam")
        except Exception:
            pass
        self._configure_style(mono, ui)

        self._build_top()
        self._build_tabs(mono, ui)
        self._build_statusbar(ui)

        self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                        name="dap-worker")
        self._worker.start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(self.POLL_MS, self._drain_events)

        if mock:
            self._log("以假设备模式启动(--mock):密码一律填 12345678", "sys")
            self.submit("连接假设备", self._open_mock, self._after_connect)
        elif port:
            self.port_var.set(port)
            self.submit("连接 %s" % port, lambda: self._open_port(port),
                        self._after_connect)
        else:
            self.top_status.set("正在探测串口…")
            self.submit("探测设备", self._autodetect, self._after_autodetect)

    # --- 界面搭建 ----------------------------------------------------
    def _pick_font(self) -> str:
        """等宽字体:表格与日志用。"""
        try:
            from tkinter import font as tkfont
            fams = set(tkfont.families(self.root))
        except Exception:
            return "Consolas"
        for cand in ("Consolas", "Cascadia Mono", "DejaVu Sans Mono", "Courier New"):
            if cand in fams:
                return cand
        return "Courier New"

    def _pick_ui_font(self, tkfont) -> str:
        """界面字体:标签、按钮用。中文在等宽字体里很难看。"""
        try:
            fams = set(tkfont.families(self.root))
        except Exception:
            return "Segoe UI"
        for cand in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI",
                     "PingFang SC", "Noto Sans CJK SC", "Tahoma"):
            if cand in fams:
                return cand
        return "TkDefaultFont"

    def _configure_style(self, mono: str, ui: str) -> None:
        s = self._style
        s.configure(".", font=(ui, 10), background=self.C_BG,
                    foreground=self.C_TEXT)
        s.configure("TFrame", background=self.C_BG)
        s.configure("Card.TFrame", background=self.C_CARD)
        s.configure("TLabel", background=self.C_BG, foreground=self.C_TEXT)
        s.configure("Card.TLabel", background=self.C_CARD, foreground=self.C_TEXT)
        s.configure("Title.TLabel", background=self.C_BG, foreground=self.C_TEXT,
                    font=(ui, 13, "bold"))
        s.configure("Section.TLabel", background=self.C_CARD,
                    foreground=self.C_TEXT, font=(ui, 10, "bold"))
        s.configure("Hint.TLabel", background=self.C_BG,
                    foreground=self.C_TEXT_DIM, font=(ui, 9))
        s.configure("CardHint.TLabel", background=self.C_CARD,
                    foreground=self.C_TEXT_DIM, font=(ui, 9))
        s.configure("Mono.TLabel", background=self.C_BG,
                    foreground=self.C_TEXT, font=(mono, 10))
        s.configure("CardMono.TLabel", background=self.C_CARD,
                    foreground=self.C_TEXT, font=(mono, 10))

        # 按钮:对比度优先 —— 危险操作必须是整块红色,次级动作用蓝字蓝框,
        # 普通按钮白底实边。半透明浅灰那种"看着像标签"的样式全部淘汰。
        s.configure("TButton", font=(ui, 10), padding=(12, 6),
                    background="#ffffff", foreground=self.C_TEXT,
                    borderwidth=1, relief="solid", bordercolor="#c3ccd6",
                    focusthickness=0)
        s.map("TButton",
              background=[("active", "#eef4ff"), ("pressed", "#e0eaff"),
                          ("disabled", "#f3f4f6")],
              foreground=[("disabled", "#a8b0ba")],
              bordercolor=[("active", "#8fb0e8"), ("disabled", "#e3e6ea")])
        s.configure("Primary.TButton", font=(ui, 10, "bold"), padding=(16, 7),
                    background=self.C_PRIMARY, foreground="#ffffff",
                    borderwidth=0, relief="flat")
        s.map("Primary.TButton",
              background=[("active", self.C_PRIMARY_H), ("pressed", "#1e3fa8"),
                          ("disabled", "#93b4ec")],
              foreground=[("active", "#ffffff"), ("pressed", "#ffffff"),
                          ("disabled", "#eef2fb")])
        s.configure("Danger.TButton", font=(ui, 10, "bold"), padding=(12, 6),
                    background="#dc2626", foreground="#ffffff",
                    borderwidth=0, relief="flat")
        s.map("Danger.TButton",
              background=[("active", "#b91c1c"), ("pressed", "#991b1b"),
                          ("disabled", "#eba3a3")],
              foreground=[("active", "#ffffff"), ("pressed", "#ffffff"),
                          ("disabled", "#fdf3f3")])
        # 次级动作(固定优先):白底蓝字蓝框,和 Primary 一个色系但分清主次
        s.configure("Secondary.TButton", font=(ui, 10, "bold"), padding=(12, 6),
                    background="#ffffff", foreground=self.C_PRIMARY_H,
                    borderwidth=1, relief="solid", bordercolor=self.C_PRIMARY)
        s.map("Secondary.TButton",
              background=[("active", "#eff6ff"), ("pressed", "#dbeafe")],
              foreground=[("active", "#1e3fa8"), ("disabled", "#a8b0ba")],
              bordercolor=[("active", "#1e3fa8"), ("disabled", "#d8dde4")])

        # 表格
        s.configure("Treeview", font=(mono, 10), rowheight=30,
                    background=self.C_CARD, fieldbackground=self.C_CARD,
                    foreground=self.C_TEXT, borderwidth=0)
        s.map("Treeview",
              background=[("selected", "#dbeafe")],
              foreground=[("selected", self.C_TEXT)])
        s.configure("Treeview.Heading", font=(ui, 10, "bold"), padding=(8, 8),
                    background="#e8edf3", foreground="#3d4a5c",
                    relief="flat", borderwidth=0)
        s.map("Treeview.Heading", background=[("active", "#dbe3ec")])

        s.configure("TNotebook", background=self.C_BG, borderwidth=0,
                    tabmargins=(0, 6, 0, 0))
        s.configure("TNotebook.Tab", font=(ui, 10), padding=(18, 9),
                    background="#e8ecf1", foreground=self.C_TEXT_DIM,
                    borderwidth=0)
        s.map("TNotebook.Tab",
              background=[("selected", self.C_CARD)],
              foreground=[("selected", self.C_PRIMARY)],
              expand=[("selected", (0, 0, 0, 0))])
        s.configure("TCheckbutton", background=self.C_CARD,
                    foreground=self.C_TEXT, font=(ui, 9),
                    focusthickness=0)
        s.map("TCheckbutton", background=[("active", self.C_CARD)])
        s.configure("TEntry", padding=(6, 5), fieldbackground="#ffffff",
                    borderwidth=1, relief="flat")
        s.configure("TCombobox", padding=(6, 4))
        s.configure("TScrollbar", background="#d6dce4", troughcolor=self.C_CARD,
                    borderwidth=0, arrowsize=12)
        s.map("TScrollbar", background=[("active", "#c2cad4")])
        s.configure("TSeparator", background=self.C_BORDER)

    def _card(self, parent, title: Optional[str] = None,
              subtitle: Optional[str] = None):
        """造一张带标题的白色卡片,返回 (外框, 内容区)。

        用 tk.Frame + 1px 描边模拟卡片,比 ttk.LabelFrame 干净得多。
        """
        tk = self._tk
        outer = tk.Frame(parent, bg=self.C_BORDER)
        inner = tk.Frame(outer, bg=self.C_CARD)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        if title:
            head = tk.Frame(inner, bg=self.C_CARD)
            head.pack(fill="x", padx=14, pady=(11, 0))
            tk.Label(head, text=title, bg=self.C_CARD, fg=self.C_TEXT,
                     font=(self._ui, 10, "bold")).pack(side="left")
            if subtitle:
                tk.Label(head, text=subtitle, bg=self.C_CARD,
                         fg=self.C_TEXT_DIM, font=(self._ui, 9)).pack(
                    side="left", padx=(8, 0))
        body = tk.Frame(inner, bg=self.C_CARD)
        body.pack(fill="both", expand=True, padx=14, pady=(8, 12))
        return outer, body

    def _build_top(self) -> None:
        tk, ttk = self._tk, self._ttk
        wrap = tk.Frame(self.root, bg=self.C_BG)
        wrap.pack(fill="x", padx=14, pady=(14, 0))

        card, body = self._card(wrap)
        card.pack(fill="x")

        # --- 第一行:设备连接 ---
        row1 = tk.Frame(body, bg=self.C_CARD)
        row1.pack(fill="x")

        tk.Label(row1, text="设备", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(self._ui, 10, "bold")).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(row1, textvariable=self.port_var, width=11,
                                       state="readonly", font=(self._mono, 10))
        self.port_combo.pack(side="left", padx=(10, 6))
        ttk.Button(row1, text="自动探测", width=10,
                   command=lambda: self.submit("探测设备", self._autodetect,
                                               self._after_autodetect)).pack(side="left")
        ttk.Button(row1, text="连接", width=7,
                   command=self._connect_selected).pack(side="left", padx=(6, 0))

        # 右侧连接状态徽标
        self.conn_badge = tk.Label(row1, text="  未连接  ", bg="#eceff3",
                                   fg=self.C_TEXT_DIM,
                                   font=(self._ui, 10, "bold"), padx=10, pady=4)
        self.conn_badge.pack(side="right")
        self.conn_label = self.conn_badge     # 兼容旧引用

        # --- 第二行:状态摘要 ---
        status_bar = tk.Frame(body, bg=self.C_INFO_BG)
        status_bar.pack(fill="x", pady=(11, 0))
        self.top_status = tk.StringVar(value="正在探测串口…")
        self.status_label = tk.Label(status_bar, textvariable=self.top_status,
                                     bg=self.C_INFO_BG, fg=self.C_TEXT,
                                     font=(self._mono, 10), anchor="w",
                                     padx=10, pady=7)
        self.status_label.pack(fill="x")

        # --- 第三行:模式与固件 ---
        row3 = tk.Frame(body, bg=self.C_CARD)
        row3.pack(fill="x", pady=(12, 0))

        tk.Label(row3, text="模式", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(self._ui, 10, "bold")).pack(side="left")
        self.mode_btn_sta = ttk.Button(row3, text="切换 STA", width=10,
                                       style="Secondary.TButton",
                                       command=lambda: self._set_mode("STA"))
        self.mode_btn_sta.pack(side="left", padx=(10, 5))
        self.mode_btn_ap = ttk.Button(row3, text="切换 AP", width=9,
                                      style="Secondary.TButton",
                                      command=lambda: self._set_mode("AP"))
        self.mode_btn_ap.pack(side="left")
        ttk.Button(row3, text="重启设备", width=9,
                   command=self._reboot).pack(side="left", padx=(14, 0))
        # AP 密码是随机生成的、存在板子里 —— 用户必须有个地方能看到它,
        # 否则"连不上自家热点"就没救了。也提供重新生成(带确认)。
        ttk.Button(row3, text="AP 密码", width=9,
                   command=self._show_ap_password).pack(side="left", padx=(6, 0))
        ttk.Button(row3, text="重新生成", width=9,
                   command=self._regen_ap_password).pack(side="left", padx=(2, 0))

        # 状态灯亮度:灯太刺眼是常见抱怨,给个滑块当场调,不用重烧固件
        self.fw_var = tk.StringVar(value="—")
        self.fw_label = tk.Label(row3, textvariable=self.fw_var, bg=self.C_CARD,
                                 fg=self.C_TEXT_DIM, font=(self._mono, 9))
        self.fw_label.pack(side="right")

        led_box = tk.Frame(row3, bg=self.C_CARD)
        led_box.pack(side="right", padx=(0, 18))
        tk.Label(led_box, text="指示灯", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(self._ui, 10, "bold")).pack(side="left")
        self.led_var = tk.IntVar(self.root, value=4)
        self.led_scale = ttk.Scale(led_box, from_=1, to=20, orient="horizontal",
                                   length=96, variable=self.led_var,
                                   command=self._on_led_slide)
        self.led_scale.pack(side="left", padx=(8, 6))
        self.led_pct_var = tk.StringVar(value="4%")
        tk.Label(led_box, textvariable=self.led_pct_var, bg=self.C_CARD,
                 fg=self.C_TEXT_DIM, font=(self._mono, 9), width=4,
                 anchor="w").pack(side="left")

    def _build_tabs(self, mono: str, ui: str) -> None:
        tk, ttk = self._tk, self._ttk
        holder = tk.Frame(self.root, bg=self.C_BG)
        holder.pack(fill="both", expand=True, padx=14, pady=(12, 0))
        self.nb = ttk.Notebook(holder)
        self.nb.pack(fill="both", expand=True)

        # ---- 配网页 ----
        tab1 = tk.Frame(self.nb, bg=self.C_CARD)
        self.nb.add(tab1, text="配网")

        bar = tk.Frame(tab1, bg=self.C_CARD)
        bar.pack(fill="x", padx=14, pady=(14, 10))
        ttk.Button(bar, text="扫描 WiFi", style="Primary.TButton",
                   command=self._do_scan).pack(side="left")
        tk.Label(bar, text="密码", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="left", padx=(18, 8))
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(bar, textvariable=self.pass_var, show="*",
                                    width=22, font=(mono, 10))
        self.pass_entry.pack(side="left")
        self.pass_entry.bind("<Return>", lambda e: self._do_connect())
        self.show_pass = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="显示", variable=self.show_pass,
                        command=self._toggle_pass).pack(side="left", padx=(8, 0))
        self.connect_btn = ttk.Button(bar, text="连接选中", style="Primary.TButton",
                                      command=self._do_connect)
        self.connect_btn.pack(side="left", padx=(16, 0))
        ttk.Button(bar, text="刷新列表", command=self._do_scan).pack(side="right")

        cols = ("ssid", "bar", "rssi", "auth", "ch")
        tree_wrap = tk.Frame(tab1, bg=self.C_CARD)
        # 表格按内容高度(最多 8 行)取空间,剩下的都给下面的结果面板 ——
        # 扫描结果通常不到 10 行,而"下一步怎么调试"那段说明有 20 行。
        tree_wrap.pack(fill="x", padx=14)
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings",
                                 selectmode="browse", height=7)
        heads = (("ssid", "WiFi 名称", 300, "w"), ("bar", "信号", 90, "center"),
                 ("rssi", "强度", 80, "center"), ("auth", "加密方式", 130, "w"),
                 ("ch", "信道", 70, "center"))
        for key, text, width, anchor in heads:
            self.tree.heading(key, text=text,
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "ssid"))
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_scan_select)
        self.tree.bind("<Double-1>", lambda e: self._do_connect())
        # 斑马纹:行多时眼睛跟着列走不串行
        self.tree.tag_configure("odd", background="#f6f8fb")

        # 结果面板
        res_head = tk.Frame(tab1, bg=self.C_CARD)
        res_head.pack(fill="x", padx=14, pady=(14, 6))
        tk.Label(res_head, text="提示与结果", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10, "bold")).pack(side="left")
        self.res_hint = tk.Label(res_head, text="", bg=self.C_CARD,
                                 fg=self.C_TEXT_DIM, font=(ui, 9))
        self.res_hint.pack(side="right")

        res_holder = tk.Frame(tab1, bg=self.C_BORDER)
        res_holder.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.result = tk.Text(res_holder, height=8, wrap="word", relief="flat",
                              borderwidth=0, font=(mono, 10), padx=14, pady=12,
                              state="disabled", cursor="arrow",
                              bg=self.C_INFO_BG, fg=self.C_TEXT,
                              selectbackground="#cfe1fd",
                              spacing1=1, spacing2=3, spacing3=3)
        res_vsb = ttk.Scrollbar(res_holder, orient="vertical",
                                command=self.result.yview)
        self.result.configure(yscrollcommand=res_vsb.set)
        self.result.pack(side="left", fill="both", expand=True, padx=(1, 0), pady=1)
        res_vsb.pack(side="right", fill="y", pady=1)
        for tag, color in (("ok", self.C_OK), ("err", self.C_ERR),
                           ("warn", self.C_WARN), ("info", self.C_TEXT),
                           ("dim", self.C_TEXT_DIM)):
            self.result.tag_configure(tag, foreground=color)
        self.result.tag_configure("h1", font=(ui, 11, "bold"),
                                  foreground=self.C_TEXT, spacing3=6)
        self.result.tag_configure("mono", font=(mono, 9))
        self.result.tag_configure("cmd", font=(mono, 9),
                                  foreground=self.C_PRIMARY,
                                  lmargin1=18, lmargin2=18)

        # ---- 已保存网络 ----
        tab2 = tk.Frame(self.nb, bg=self.C_CARD)
        self.nb.add(tab2, text="已保存网络")

        bar2 = tk.Frame(tab2, bg=self.C_CARD)
        bar2.pack(fill="x", padx=14, pady=(14, 10))
        ttk.Button(bar2, text="刷新", command=self._do_list).pack(side="left")
        ttk.Button(bar2, text="置顶选中", style="Primary.TButton",
                   command=self._do_prioritize).pack(side="left", padx=(8, 0))
        self.pin_btn = ttk.Button(bar2, text="固定优先", style="Secondary.TButton",
                                  width=10, command=self._do_toggle_pin)
        self.pin_btn.pack(side="left", padx=(8, 0))
        ttk.Button(bar2, text="删除选中", style="Danger.TButton",
                   command=self._do_delete).pack(side="left", padx=(8, 0))
        ttk.Button(bar2, text="恢复出厂设置", style="Danger.TButton",
                   command=self._do_factory).pack(side="left", padx=(8, 0))
        self.src_var = tk.StringVar(value="")
        tk.Label(bar2, textvariable=self.src_var, bg=self.C_CARD,
                 fg=self.C_TEXT_DIM, font=(ui, 9)).pack(side="right")

        cols2 = ("idx", "pin", "ssid", "pass")
        save_wrap = tk.Frame(tab2, bg=self.C_CARD)
        save_wrap.pack(fill="both", expand=True, padx=14)
        self.save_tree = ttk.Treeview(save_wrap, columns=cols2, show="headings",
                                      height=10)
        for key, text, width, anchor in (("idx", "#", 50, "center"),
                                         ("pin", "固定", 56, "center"),
                                         ("ssid", "WiFi 名称", 330, "w"),
                                         ("pass", "密码", 140, "w")):
            self.save_tree.heading(key, text=text)
            # pin 列不许 stretch:📌(宽)和空串(窄)渲染宽度不同,
            # 列宽跟着内容抖会让整张表横向跳
            self.save_tree.column(key, width=width, minwidth=width,
                                  anchor=anchor, stretch=(key == "ssid"))
        vsb2 = ttk.Scrollbar(save_wrap, orient="vertical",
                             command=self.save_tree.yview)
        self.save_tree.configure(yscrollcommand=vsb2.set)
        self.save_tree.pack(side="left", fill="both", expand=True)
        vsb2.pack(side="right", fill="y")
        self.save_tree.bind("<<TreeviewSelect>>", self._on_saved_select)
        self.save_tree.tag_configure("odd", background="#f6f8fb")
        # 键盘流:选中行按 Delete 直接走删除确认,和资源管理器一致
        self.save_tree.bind("<Delete>", lambda e: self._do_delete())

        note = ("主网模型:设备开机只连「主网」一条 —— 即 pin 过的最靠前条目,"
                "没 pin 就是最近连的那条;连不上只重试它,不会自动换网。\n"
                "「固定优先」= 把这条钉成主网(📌),连过别的网络也不会被顶掉;\n"
                "「连接」新网络成功后,那条会成为最近使用的(未 pin 时即主网)。\n"
                "「恢复出厂设置」清空全部已存网络并回到未配网状态,"
                "需要重新插 USB 配网。")
        tk.Label(tab2, text=note, bg=self.C_CARD, fg=self.C_TEXT_DIM,
                 font=(ui, 9), justify="left",
                 anchor="w").pack(fill="x", padx=14, pady=(10, 14))

        # ---- 调试接入 ----
        self._build_debug_tab(ttk, ui, mono)

        # ---- 日志 ----
        tab3 = tk.Frame(self.nb, bg=self.C_CARD)
        self.nb.add(tab3, text="日志")

        bar3 = tk.Frame(tab3, bg=self.C_CARD)
        bar3.pack(fill="x", padx=14, pady=(14, 10))
        ttk.Checkbutton(bar3, text="板端日志", variable=self.show_board_log
                        ).pack(side="left")
        ttk.Checkbutton(bar3, text="协议帧", variable=self.show_frames
                        ).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(bar3, text="自动滚动", variable=self.autoscroll
                        ).pack(side="left", padx=(14, 0))
        ttk.Button(bar3, text="复制日志",
                   command=self._copy_log).pack(side="right", padx=(0, 8))
        ttk.Button(bar3, text="清空", command=self._clear_log).pack(side="right")

        cmdbar = tk.Frame(tab3, bg=self.C_CARD)
        cmdbar.pack(side="bottom", fill="x", padx=14, pady=(10, 14))
        tk.Label(cmdbar, text="手动命令", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="left")
        self.cmd_var = tk.StringVar()
        e = ttk.Entry(cmdbar, textvariable=self.cmd_var, font=(mono, 10))
        e.pack(side="left", fill="x", expand=True, padx=(10, 8))
        e.bind("<Return>", lambda ev: self._send_manual())
        ttk.Button(cmdbar, text="发送", style="Primary.TButton",
                   command=self._send_manual).pack(side="right")

        log_holder = tk.Frame(tab3, bg=self.C_BORDER)
        log_holder.pack(fill="both", expand=True, padx=14)
        self.log = tk.Text(log_holder, wrap="none", font=(mono, 9),
                           state="disabled", relief="flat", borderwidth=0,
                           background="#0f1419", foreground="#c9d1d9",
                           insertbackground="#c9d1d9", padx=14, pady=12,
                           spacing1=1, spacing2=2, spacing3=3)
        self.log.pack(fill="both", expand=True, padx=1, pady=1)
        self.log.tag_configure("proto", foreground="#79b8ff")
        self.log.tag_configure("err", foreground="#ff7b72")
        self.log.tag_configure("ok", foreground="#7ee787")
        self.log.tag_configure("board", foreground="#8b949e")
        self.log.tag_configure("openocd", foreground="#a5d6ff")
        self.log.tag_configure("sys", foreground="#e3b341")

    def _build_debug_tab(self, ttk, ui: str, mono: str) -> None:
        """调试接入页:三条路各自一个开关。

        为什么做成三个独立开关而不是"一键全开":usbip 和 Keil/OpenOCD 会抢
        同一个 DAP 会话(固件只支持单连接)。一键全开必然打架,而让用户
        自己选反而清晰 —— 页底那行红字就是在说这件事。
        """
        tk = self._tk
        tab = tk.Frame(self.nb, bg=self.C_CARD)
        self.nb.add(tab, text="调试接入")

        # 顶部:当前设备地址(决定三个后端都连哪)
        bar = tk.Frame(tab, bg=self.C_CARD)
        bar.pack(fill="x", padx=14, pady=(14, 8))
        tk.Label(bar, text="设备地址", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="left")
        # 启动时的默认值来自 settings.json(用户上次填的),没有才用 dap.local
        self.addr_var = tk.StringVar(
            value=self._settings.get("addr", DEFAULT_SETTINGS["addr"]))
        e = ttk.Entry(bar, textvariable=self.addr_var, width=24, font=(mono, 10))
        e.pack(side="left", padx=(10, 6))
        e.bind("<Return>", lambda ev: self._apply_addr())
        ttk.Button(bar, text="应用", command=self._apply_addr).pack(side="left")
        tk.Label(bar, text="(mDNS 名,IP 变了也不用改)", bg=self.C_CARD,
                 fg=self.C_TEXT_DIM, font=(ui, 9)).pack(side="left", padx=(10, 0))
        # 恢复默认 + 显示设置文件位置:用户得知道工具往哪儿写了东西
        ttk.Button(bar, text="恢复默认", width=9,
                   command=self._reset_settings).pack(side="right")

        # 四张卡片。usbip 与 at32 是**同一套底座**(usbip 把设备挂成本地 USB,
        # at32 用带 AT32 flash 驱动的 openocd 去烧),所以放相邻并注明依赖关系。
        self._backend_rows = {}
        # usbip 状态缓存:驱动装没装(进程内缓存一次)、可达性(5s TTL),
        # 避免每次刷新都在 UI 线程跑 pnputil / DNS 超时把界面卡住。
        self._usbip_driver_state = None      # Optional[bool]
        self._usbip_reach_result = None      # Optional[(ok, msg, ts)]
        self._usbip_attached_state = None    # Optional[bool]
        self._usbip_attached_ts = 0.0
        self._usbip_attached_running = False
        self._usbip_probe_running = False
        for key, title, sub in (
                ("keil", "Keil µVision", "elaphureLink · 免驱动,走 TCP 直连"),
                ("openocd", "OpenOCD / VSCode", "openocd-elaphurelink · 免驱动 · 仅 STM32"),
                ("usbip", "USB 桥接", "usbip-win · 需要管理员权限 · 下面两项的前提"),
                ("at32", "AT32 烧录", "atopenocd(雅特力 fork)· 必须先完成上面的 USB 桥接"),
        ):
            card, body = self._card(tab, title, sub)
            card.pack(fill="x", padx=14, pady=(0, 8))
            row = tk.Frame(body, bg=self.C_CARD)
            row.pack(fill="x")

            state = tk.Label(row, text="检测中…", bg=self.C_CARD,
                             fg=self.C_TEXT_DIM, font=(ui, 9))
            state.pack(side="left")

            btn = ttk.Button(row, text="启动", style="Primary.TButton",
                             width=8, command=lambda k=key: self._toggle_backend(k))
            btn.pack(side="right")
            copy_btn = ttk.Button(row, text="复制命令", width=9,
                                  command=lambda k=key: self._copy_cmd(k))
            copy_btn.pack(side="right", padx=(0, 8))

            self._backend_rows[key] = {"state": state, "btn": btn,
                                       "copy": copy_btn}
            if key == "usbip":
                # 用户会被提示"去装 usbip 驱动",装完必须能立刻刷出结果 ——
                # 没有这个按钮就只能重启工具(驱动状态有缓存)。
                ttk.Button(row, text="重新检测", width=10,
                           command=self._recheck_usbip).pack(
                    side="right", padx=(0, 8))

        # OpenOCD 的 target 选择 —— 没有这个选项的话,用户不知道该连什么芯片
        trow = tk.Frame(tab, bg=self.C_CARD)
        trow.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(trow, text="OpenOCD 目标芯片", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="left")
        self.target_var = tk.StringVar(
            value=self._settings.get("target", DEFAULT_SETTINGS["target"]))
        self.target_combo = ttk.Combobox(trow, textvariable=self.target_var,
                                         width=22, font=(mono, 10))
        self.target_combo.pack(side="left", padx=(10, 6))
        self.target_combo.bind("<<ComboboxSelected>>",
                               lambda ev: self._on_target_change())
        tk.Label(trow, text="(config 名,如 stm32f1x / stm32f4x)",
                 bg=self.C_CARD, fg=self.C_TEXT_DIM,
                 font=(ui, 9)).pack(side="left", padx=(6, 0))

        # AT32 的 target 选择(用 atopenocd 的 scripts,和上面那套不通用)
        arow = tk.Frame(tab, bg=self.C_CARD)
        arow.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(arow, text="AT32 目标芯片", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="left")
        self.at32_var = tk.StringVar(
            value=self._settings.get("at32_target", DEFAULT_SETTINGS["at32_target"]))
        self.at32_combo = ttk.Combobox(arow, textvariable=self.at32_var,
                                       width=22, font=(mono, 10))
        self.at32_combo.pack(side="left", padx=(10, 6))
        self.at32_combo.bind("<<ComboboxSelected>>",
                             lambda ev: self._on_at32_target_change())
        tk.Label(arow, text="(atopenocd 自带 349 个,含 AT32 全系)",
                 bg=self.C_CARD, fg=self.C_TEXT_DIM,
                 font=(ui, 9)).pack(side="left", padx=(6, 0))

        # 底部警告
        warn = tk.Frame(tab, bg=self.C_WARN_BG)
        warn.pack(fill="x", padx=14, pady=(4, 14))
        tk.Label(warn,
                 text="注意:usbip 与 Keil/OpenOCD 抢同一个 DAP 连接,不要同时开。\n"
                      "切换之前先把另一个关掉,否则表现为「连上了但没反应」。",
                 bg=self.C_WARN_BG, fg=self.C_WARN, font=(ui, 9),
                 justify="left", anchor="w").pack(fill="x", padx=12, pady=8)

        self._refresh_backends()

    # --- 调试接入页的行为 ---------------------------------------------
    def _recheck_usbip(self) -> None:
        """「重新检测」:作废两个缓存并立刻重跑一次后台探测。

        这是驱动状态的唯一刷新入口 —— 用户在别的窗口装完驱动回来点这个,
        马上就能看到「驱动已装」,不必重启工具。
        """
        self._usbip_driver_state = None
        self._usbip_reach_result = None
        self._usbip_attached_state = None
        self._usbip_attached_ts = 0.0
        self._log("重新检测 usbip 驱动与设备状态…", "sys")
        self._refresh_backends()

    def _usbip_driver_cached(self, ub, force: bool = False) -> bool:
        """驱动装没装 —— 缓存结果(pnputil 每次 250-300ms)。

        缓存必须有失效手段:用户正是在这个界面上被提示"去装驱动",装完
        回来了必须能立刻看到变化。原实现"进程内只查一次"会让用户装完
        驱动后仍被拦在"驱动未装",只能重启工具 —— 所以这里给 force,
        由「USB 桥接」卡片上的「重新检测」按钮触发。
        """
        if force:
            self._usbip_driver_state = None
        if self._usbip_driver_state is None:
            try:
                self._usbip_driver_state = bool(ub.driver_installed())
            except Exception:
                self._usbip_driver_state = False
        return self._usbip_driver_state

    def _usbip_reach_cached(self):
        """可达性缓存,5s TTL。返回 (ok, msg) 或 None(需要新探测)。"""
        c = self._usbip_reach_result
        if c is not None and (time.monotonic() - c[2]) < 5.0:
            return (c[0], c[1])
        return None

    def _usbip_attached_cached(self, ub):
        """设备是否已挂成本地 USB。返回缓存值;过期时 **后台** 刷新。

        attached() 要起一次 PowerShell 查设备树(实测 1.6-2.0s),
        绝不能同步放在 UI 线程 —— 那和原来 reachable() 卡 3s 是同一类问题。
        语义:先返回上一次的值(没有就是 False),同时丢一个后台任务去刷新,
        结果回来后再 _refresh_backends 一次。
        """
        now = time.monotonic()
        fresh = (self._usbip_attached_state is not None
                 and now - self._usbip_attached_ts < 3.0)
        if not fresh and not self._usbip_attached_running:
            self._usbip_attached_running = True

            def work():
                try:
                    state = bool(ub.attached())
                except Exception:
                    state = False
                self.events.put(("usbip_attached", state))

            threading.Thread(target=work, daemon=True,
                             name="usbip-attached").start()
        return bool(self._usbip_attached_state)

    def _on_usbip_attached(self, state: bool) -> None:
        self._usbip_attached_running = False
        self._usbip_attached_state = bool(state)
        self._usbip_attached_ts = time.monotonic()
        self._refresh_backends()

    def _usbip_probe_async(self, ub) -> None:
        """后台线程探测可达性,结果回主线程刷新。

        reachable() 在 dap.local 解析失败时要 3.0-3.35s(DNS gaierror
        本身 3.09s),绝不能在 UI 线程同步调用。
        """
        if self._usbip_probe_running:
            return
        self._usbip_probe_running = True

        def work():
            try:
                ok, msg = ub.reachable(timeout=1.5)
            except Exception as exc:
                ok, msg = False, str(exc)
            self.events.put(("usbip_probe", (ok, msg)))

        threading.Thread(target=work, daemon=True, name="usbip-probe").start()

    def _on_usbip_probe(self, ok: bool, msg: str) -> None:
        self._usbip_probe_running = False
        self._usbip_reach_result = (bool(ok), msg, time.monotonic())
        self._refresh_backends()

    def _backends(self):
        """惰性创建后端管理器 —— 没有第三方工具时不该影响其他功能。"""
        if getattr(self, "_bm", None) is None:
            import dap_backends
            self._bm = dap_backends.BackendManager(
                self.addr_var.get().strip() or "dap.local",
                on_line=lambda s: self.events.put(("log", (s, "openocd"))))
        return self._bm

    def _refresh_backends(self) -> None:
        """刷新三张卡片的状态文字与按钮。"""
        try:
            bm = self._backends()
        except Exception as exc:
            for row in self._backend_rows.values():
                row["state"].configure(text="加载失败:%s" % exc, fg=self.C_ERR)
            return

        # Keil
        row = self._backend_rows["keil"]
        el = bm.elaphurelink
        if el.missing():
            row["state"].configure(text="未找到 %s" % el.EXE, fg=self.C_ERR)
            row["btn"].configure(state="disabled")
        elif el.running():
            row["state"].configure(text="运行中", fg=self.C_OK)
            row["btn"].configure(text="停止")
        else:
            row["state"].configure(text="已就绪,未启动", fg=self.C_TEXT_DIM)
            row["btn"].configure(text="启动", state="normal")

        # OpenOCD
        row = self._backend_rows["openocd"]
        oc = bm.openocd
        if oc.missing():
            row["state"].configure(text="未找到 openocd.exe", fg=self.C_ERR)
            row["btn"].configure(state="disabled")
        elif oc.running():
            # 区分"起来了"和"真连上目标" —— 只有后者才开 3333 端口。
            # 不区分的话,用户看到"运行中"却连不上 gdb,完全无从下手。
            if oc.gdb_ready():
                row["state"].configure(text="已连上目标 · gdb :3333", fg=self.C_OK)
            else:
                row["state"].configure(
                    text="运行中,但还没连上目标芯片(查 SWD 接线)", fg=self.C_WARN)
            row["btn"].configure(text="停止")
        else:
            row["state"].configure(text="已就绪,未启动", fg=self.C_TEXT_DIM)
            row["btn"].configure(text="启动", state="normal")

        # usbip
        row = self._backend_rows["usbip"]
        ub = bm.usbip
        if ub.missing():
            row["state"].configure(text="未找到 usbip.exe", fg=self.C_ERR)
            row["btn"].configure(state="disabled", text="挂载")
        else:
            # driver_installed() 要起 pnputil(~250-300ms),reachable() 在
            # dap.local 解析失败时要 3.0-3.35s(DNS gaierror 本身 3.09s)。
            # 这两个都在 UI 线程同步跑会把界面卡死,所以:
            #   驱动状态按进程缓存一次(装没装不会变),
            #   可达性 5s TTL 缓存 + 后台线程探测(先显示上一次结果)。
            inst = self._usbip_driver_cached(ub)
            if not inst:
                row["state"].configure(
                    text="驱动未装,首次需运行 install_driver.bat", fg=self.C_WARN)
                row["btn"].configure(text="挂载", state="normal")
            else:
                # 挂载状态(设备树里有没有 CMSIS-DAP)决定按钮是「挂载」还是
                # 「卸载」。attached() 要起 PowerShell(~0.5-8s),所以缓存 3s;
                # 绝不每帧同步查 —— 那和 P1-5 的卡顿是同一类问题。
                at = self._usbip_attached_cached(ub)
                if at:
                    row["btn"].configure(text="卸载", state="normal")
                else:
                    row["btn"].configure(text="挂载", state="normal")
                cached = self._usbip_reach_cached()
                if cached is None:
                    row["state"].configure(
                        text="驱动已装 · 正在探测设备…", fg=self.C_TEXT_DIM)
                    self._usbip_probe_async(ub)
                elif cached[0]:
                    row["state"].configure(text="驱动已装 · %s" % cached[1],
                                           fg=self.C_OK)
                else:
                    row["state"].configure(
                        text="驱动已装,但设备不可达(多半是不同网段)", fg=self.C_WARN)

        # AT32
        row = self._backend_rows["at32"]
        aoc = bm.at32_openocd
        if aoc is None:
            row["state"].configure(
                text="未找到 atopenocd(雅特力 OpenOCD)", fg=self.C_ERR)
            row["btn"].configure(state="disabled")
        elif aoc.missing():
            row["state"].configure(text="openocd.exe 不存在", fg=self.C_ERR)
            row["btn"].configure(state="disabled")
        elif aoc.running():
            if aoc.gdb_ready():
                row["state"].configure(
                    text="已连上目标 · 可烧录(gdb :3333)", fg=self.C_OK)
            else:
                row["state"].configure(
                    text="运行中,还没连上目标(先做 USB 桥接)", fg=self.C_WARN)
            row["btn"].configure(text="停止")
        else:
            row["state"].configure(text="已就绪,未启动", fg=self.C_TEXT_DIM)
            row["btn"].configure(text="启动", state="normal")

        # target 下拉
        try:
            targets = oc.list_targets()
            if targets:
                self.target_combo.configure(values=targets)
                if self.target_var.get() not in targets:
                    prefer = "stm32f1x" if "stm32f1x" in targets else targets[0]
                    self.target_var.set(prefer)
        except Exception:
            pass

        # AT32 的 target 下拉(来自 atopenocd 的 scripts)
        try:
            if aoc is not None:
                at = aoc.list_targets()
                if at:
                    self.at32_combo.configure(values=at)
                    if self.at32_var.get() not in at:
                        self.at32_var.set(
                            "at32f403axG" if "at32f403axG" in at else at[0])
        except Exception:
            pass

    def _on_target_change(self) -> None:
        """换 target 要重建后端对象(args 在构造时就固定了)。"""
        bm = self._backends()
        was_running = bm.openocd.running()
        if was_running:
            bm.openocd.stop()
        import dap_backends
        bm.openocd = dap_backends.OpenOcdBackend(
            bm.device_addr,
            "target/%s.cfg" % self.target_var.get(),
            on_line=lambda s: self.events.put(("log", (s, "openocd"))))
        self._log("OpenOCD 目标改为 %s" % self.target_var.get(), "sys")
        self._persist_settings()
        self._refresh_backends()

    def _persist_settings(self) -> None:
        """把当前的地址/target 存进 settings.json。

        只在值真的变了才写 —— _apply_addr 之类会被频繁调用,每次都写盘
        既没必要也会刷 mtime(备份软件会以为文件一直在变)。

        **全部是默认值时删掉文件**,而不是写一份"和默认一样"的 JSON:
        用户在「恢复默认」之后该真的回到"没有设置文件"的状态,否则下次
        启动虽然行为一样,但文件还在,看起来像没重置成功。
        (实测过:恢复默认删掉文件后 _apply_addr 会立刻把它写回来。)

        写失败**不弹窗**:设置存不下来不影响本次使用(内存里已经生效),
        而且每次改地址都弹一次会很烦。只在日志里留一行。
        """
        cur = {
            "addr": self.addr_var.get().strip(),
            "target": self.target_var.get().strip(),
            "at32_target": self.at32_var.get().strip(),
        }
        if cur == self._settings_saved:
            return
        if cur == dict(DEFAULT_SETTINGS):
            ok, detail = delete_settings_file()
            if ok:
                self._settings = {}
                self._settings_saved = {}
                self._log("设置已回到默认,设置文件已删除", "sys")
            else:
                self._log("设置文件删除失败(本次仍用默认值):%s" % detail, "sys")
            return
        ok, detail = save_settings(cur)
        if ok:
            self._settings = cur
            self._settings_saved = dict(cur)
            self._log("已记住设置(下次启动沿用):%s" % detail, "sys")
        else:
            self._log("设置没能存下来(本次仍然有效):%s" % detail, "sys")


    def _reset_settings(self) -> None:
        """恢复默认:删掉 settings.json 并把界面变量恢复出厂值。

        不重启工具 —— 用户点完就能看到地址框变回 dap.local。已经建好的
        后端对象要跟着重建,否则内部还连着旧地址。
        """
        try:
            p = settings_path()
            if os.path.exists(p):
                os.remove(p)
        except Exception as exc:
            self._show_result("删除设置文件失败:%s" % exc, "err")
            return
        self.addr_var.set(DEFAULT_SETTINGS["addr"])
        self.target_var.set(DEFAULT_SETTINGS["target"])
        self.at32_var.set(DEFAULT_SETTINGS["at32_target"])
        self._settings = {}
        self._settings_saved = {}
        # 让后端真的切回默认地址
        try:
            self._apply_addr()
        except Exception:
            pass
        self._log("已恢复默认设置(设备地址 dap.local / target stm32f1x、"
                  "at32f403axG),设置文件已删除", "ok")

    def _apply_addr(self) -> None:
        addr = self.addr_var.get().strip()
        if not addr:
            self.addr_var.set("dap.local")
            addr = "dap.local"
        bm = self._backends()
        bm.set_addr(addr)
        # target 也要重新应用(重建 OpenOcdBackend 时用了默认 target)
        import dap_backends
        bm.openocd = dap_backends.OpenOcdBackend(
            addr, "target/%s.cfg" % self.target_var.get(),
            on_line=lambda s: self.events.put(("log", (s, "openocd"))))
        self._log("设备地址改为 %s" % addr, "sys")
        self._persist_settings()
        self._refresh_backends()

    def _on_at32_target_change(self) -> None:
        bm = self._backends()
        if not bm.has_at32():
            return
        bm.set_at32_target("target/%s.cfg" % self.at32_var.get())
        self._log("AT32 目标改为 %s" % self.at32_var.get(), "sys")
        self._persist_settings()
        self._refresh_backends()

    def _copy_cmd(self, key: str) -> None:
        bm = self._backends()
        if key == "openocd":
            cmd = bm.openocd.command_line()
        elif key == "at32":
            cmd = (bm.at32_openocd.command_line() if bm.has_at32()
                   else "(未找到 atopenocd)")
        elif key == "usbip":
            cmd = bm.usbip.command_line()
        else:
            cmd = "启动 elaphureLink.Wpf.exe,Keil 里 Debug 选 elaphureLink"
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self._log("已复制:%s" % cmd, "sys")
        self._show_result("已复制到剪贴板:\n\n%s" % cmd, "dim")

    def _toggle_backend(self, key: str) -> None:
        bm = self._backends()
        if key == "keil":
            el = bm.elaphurelink
            if el.running():
                ok, msg = el.stop()
            else:
                ok, msg = el.start()
            self._log(msg, "ok" if ok else "err")
            if not ok:
                self._show_result(msg, "err")
            self.root.after(400, self._refresh_backends)
            return

        if key == "openocd":
            oc = bm.openocd
            if oc.running():
                ok = oc.stop()
                msg = "OpenOCD 已停止" if ok else "OpenOCD 停止失败"
                self._log(msg, "ok" if ok else "err")
            else:
                try:
                    oc.start()
                    self._log("OpenOCD 启动:%s" % oc.command_line(), "sys")
                except Exception as exc:
                    self._log("OpenOCD 启动失败:%s" % exc, "err")
                    self._show_result("启动失败:%s" % exc, "err")
            self.root.after(900, self._refresh_backends)
            # OpenOCD 连上目标可能要几秒(gdb 端口在握手成功后才开),
            # 单次刷新不够 —— 轮询几次直到出结果或超时。
            self.root.after(2500, self._poll_openocd_ready)
            return

        if key == "at32":
            aoc = bm.at32_openocd
            if aoc is None:
                self._show_result("未找到 atopenocd。AT32 烧录需要雅特力 fork 的 OpenOCD,"
                                  "它自带 at32f4xx flash 驱动,上游版本没有。", "err")
                return
            if aoc.running():
                ok = aoc.stop()
                self._log("AT32 OpenOCD 已停止" if ok else "停止失败",
                          "ok" if ok else "err")
            else:
                # 烧 AT32 的前提是设备已经挂成本地 USB。没挂就直接起,
                # OpenOCD 会报"no adapter found",用户看不出是桥接没做。
                if not self._usbip_driver_cached(bm.usbip):
                    self._show_result(
                        "还没装 usbip 驱动。AT32 烧录的顺序是:\n\n"
                        "  1. 装 usbip 驱动(上面的 USB 桥接卡片)\n"
                        "  2. 点「挂载」把设备变成本地 USB\n"
                        "  3. 回来点这里的「启动」\n\n"
                        "为什么必须先桥接:atopenocd 用的是 interface/cmsis-dap.cfg,"
                        "它找的是本地 USB 设备,不是网络地址。", "warn")
                    return
                try:
                    aoc.start()
                    self._log("AT32 OpenOCD 启动:%s" % aoc.command_line(), "sys")
                except Exception as exc:
                    self._log("AT32 OpenOCD 启动失败:%s" % exc, "err")
                    self._show_result("启动失败:%s" % exc, "err")
            self.root.after(900, self._refresh_backends)
            # 轮询从 600ms 起步而不是 2500ms:openocd 找不到适配器时会**1 秒内
            # 直接退出**(实测 "unable to find a matching CMSIS-DAP device")。
            # 等 2.5 秒再第一次检查,界面会在这段时间里一直显示"运行中",
            # 而进程其实早死了 —— 用户点完看到"运行中"就去开 Keil,结果连不上。
            self.root.after(600, lambda: self._poll_openocd_ready(key="at32"))
            return

        if key == "usbip":
            # usbip 是「一次动作」:没挂就挂,已挂就卸 —— 按钮文案跟着状态走
            # (见 _refresh_backends 里的 attached 判断)。
            if not self._usbip_driver_cached(bm.usbip):
                ok = self._messagebox.askyesno(
                    "需要先装驱动",
                    "usbip 的 vhci 驱动还没装。\n\n"
                    "现在打开装驱动的说明?(需要管理员权限)")
                if ok:
                    self._show_result(
                        "装驱动步骤:\n\n"
                        "1. 打开目录:%s\n"
                        "2. 右键 install_driver.bat → 以管理员身份运行\n"
                        "3. 装完回来点「挂载」\n\n"
                        "卸载驱动:uninstall_driver.bat" % bm.usbip.root, "warn")
                return

            # 用缓存值而不是同步查设备树:attached() 要起一次 PowerShell,
            # 实测 1.1-2.0s,放在按钮点击里就是"点了没反应"。缓存值由
            # _refresh_backends 的后台线程维护(3s TTL),点按钮时最多差 3 秒。
            if self._usbip_attached_state:
                ok, msg = bm.usbip.detach_all()
                self._log(msg, "ok" if ok else "err")
                if not ok:
                    self._show_result(msg, "err")
                # 两个缓存都要立刻作废:否则 3 秒内按钮还显示「卸载」,
                # 用户会以为没卸掉而再点一次。
                self._usbip_attached_state = False
                self._usbip_attached_ts = time.monotonic()
                self._usbip_reach_result = None
                self.root.after(1200, self._refresh_backends)
                return

            # 挂载前先确认设备真的可达。usbip 自己超时很难懂,而"不同网段"
            # 是最常见的原因 —— 提前拦下来说清楚。
            # 但 reachable() 在 dap.local 解析失败时要 3.0-3.35s,不能同步卡 UI:
            # 先用缓存;缓存没有(或已过期)就起后台探测,并提示用户再点一次。
            cached = self._usbip_reach_cached()
            if cached is None:
                self._usbip_probe_async(bm.usbip)
                self._log("正在探测设备连通性,请稍候再点一次「挂载」", "sys")
                self._show_result(
                    "正在探测设备能不能连上(最多 3 秒),结果出来后再点一次"
                    "「挂载」。\n\n"
                    "为什么要先探:usbip 自己超时给的错误很难懂,而\"PC 和设备"
                    "不在同一网段\"是最常见的原因。", "info")
                return
            ok_r, msg_r = cached
            if not ok_r:
                self._log("设备不可达,取消挂载", "err")
                self._show_result(msg_r, "err")
                return
            ok, msg = bm.usbip.attach()
            self._log(msg, "ok" if ok else "err")
            self._show_result(msg, "ok" if ok else "warn")
            # 挂载是异步的(提权后新起进程),等设备树出现再刷新按钮文案。
            # 必须把 attached 缓存打旧:否则 2.5s 后那次刷新仍在 3s TTL 内,
            # 不会重新探测,按钮就一直停在「挂载」——用户以为没挂上又点一次。
            self._usbip_attached_ts = 0.0
            self.root.after(2500, self._refresh_backends)
            self.root.after(5000, self._refresh_backends)
            return

    def _poll_openocd_ready(self, tries: int = 0, key: str = "openocd") -> None:
        """轮询 gdb 端口状态。最多约 12 秒,超时后停止刷新并给出提示。"""
        bm = self._backends()
        oc = bm.at32_openocd if key == "at32" else bm.openocd
        if oc is None:
            return

        # 进程已经退出 —— 和"起来了但没握手"是两件完全不同的事,必须分开报。
        # 实测最常见的退出原因:AT32 那条在没做桥接时,openocd 1 秒内就报
        # "unable to find a matching CMSIS-DAP device" 然后退出。
        if not oc.running():
            self._refresh_backends()
            if key == "at32":
                self._show_result(
                    "AT32 的 OpenOCD 启动后立即退出了。\n\n"
                    "最常见的原因是**没做 USB 桥接**,或者桥接已断开:\n"
                    "  openocd 报 \"unable to find a matching CMSIS-DAP device\"\n\n"
                    "按顺序查:\n"
                    "  1. 上面「USB 桥接」卡片是不是显示可达?不可达先解决网络\n"
                    "  2. 点「挂载」,等设备管理器出现 Wireless ESP CMSIS-DAP\n"
                    "  3. 再回来点这里的「启动」\n\n"
                    "日志页能看到 openocd 的原始错误。", "err")
            else:
                self._show_result(
                    "OpenOCD 启动后立即退出了。日志页看原始错误。\n\n"
                    "常见原因:\n"
                    "  · 设备地址不对(上面的「设备地址」)\n"
                    "  · 目标芯片 cfg 不存在\n"
                    "  · 端口 3333/4444 被别的 openocd 占用", "err")
            return

        if oc.gdb_ready() or tries >= 6:
            self._refresh_backends()
            if oc.running() and not oc.gdb_ready() and tries >= 6:
                target = (self.at32_var.get() if key == "at32"
                          else self.target_var.get())
                if key == "at32":
                    self._show_result(
                        "AT32 的 OpenOCD 起来了,但没等到 gdb 端口 3333。\n\n"
                        "按顺序查:\n"
                        "  1. USB 桥接做了吗(上面那张卡片,必须显示「挂载」成功)\n"
                        "  2. 设备管理器里能看到 CMSIS-DAP 设备吗\n"
                        "  3. SWD 四根线:SWCLK / SWDIO / GND / 3V3\n"
                        "  4. 目标芯片选得对不对(当前 %s)\n\n"
                        "日志页能看到 OpenOCD 的原始输出。" % target, "warn")
                else:
                    self._show_result(
                        "OpenOCD 起来了,但没等到 gdb 端口 3333。\n\n"
                        "这几乎总是「目标芯片没握上手」,按顺序查:\n"
                        "  1. SWD 四根线:SWCLK / SWDIO / GND / 3V3(只接两根不共地必不通)\n"
                        "  2. 目标板是否上电(调试器不供电给目标)\n"
                        "  3. 目标芯片选得对不对(当前 %s)\n"
                        "  4. 复位脚接了吗(某些芯片需要 nRESET)\n\n"
                        "日志页能看到 OpenOCD 的原始输出。" % target, "warn")
            return
        self._refresh_backends()
        self.root.after(2000, lambda: self._poll_openocd_ready(tries + 1, key))

    def _build_statusbar(self, ui: str) -> None:
        tk, ttk = self._tk, self._ttk
        bar = tk.Frame(self.root, bg=self.C_BG)
        bar.pack(fill="x", padx=14, pady=(10, 12))
        self.busy_var = tk.StringVar(value="就绪")
        self.busy_label = tk.Label(bar, textvariable=self.busy_var, bg=self.C_BG,
                                   fg=self.C_TEXT_DIM, font=(ui, 9))
        self.busy_label.pack(side="left")
        tk.Label(bar, text="v%s" % TOOL_VERSION, bg=self.C_BG,
                 fg=self.C_TEXT_DIM, font=(ui, 9)).pack(side="right")

    # --- 线程与事件 --------------------------------------------------
    def submit(self, label: str, fn: Callable[[], object],
               done: Optional[Callable[[object, Optional[BaseException]], None]] = None
               ) -> None:
        """把一件事排到工作线程。协议是串行的,所以所有 I/O 都走这一条队列。"""
        if self._busy:
            self._log("上一步还没完成,先等一下(%s)" % label, "sys")
            return
        self._busy = True
        self.busy_var.set("%s …" % label)
        self._set_buttons(False)
        self.jobs.put((label, fn, done))

    def _worker_loop(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            label, fn, done = job
            try:
                result = fn()
                err = None
            except Exception as exc:
                result, err = None, exc
            self.events.put(("job_done", (label, result, err, done)))

    def _drain_events(self) -> None:
        if getattr(self, "_closing", False):
            # 窗口正在关/已关:不再消费事件,也不再重排 after。
            # 队列里剩下的事件属于已销毁的控件,丢弃是对的行为。
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    text, tag = payload
                    self._append_log(text, tag)
                elif kind == "disconnect":
                    self._on_serial_disconnect(payload)
                elif kind == "usbip_probe":
                    ok, msg = payload
                    try:
                        self._on_usbip_probe(ok, msg)
                    except Exception as exc:
                        self._log("usbip 探测回调异常: %r" % (exc,), "err")
                elif kind == "usbip_attached":
                    try:
                        self._on_usbip_attached(payload)
                    except Exception as exc:
                        self._log("usbip 挂载状态回调异常: %r" % (exc,), "err")
                elif kind == "job_done":
                    label, result, err, done = payload
                    self._busy = False
                    self._set_buttons(True)
                    self.busy_var.set("就绪")
                    if done is not None:
                        try:
                            done(result, err)
                        except Exception as exc:
                            self._log("回调异常: %r" % (exc,), "err")
                    elif err is not None:
                        self._show_result("失败:%s" % err, "err")
        except queue.Empty:
            pass
        finally:
            # 关闭过程中 root 可能已经 destroy,after() 会抛 TclError
            if not getattr(self, "_closing", False):
                try:
                    self.root.after(self.POLL_MS, self._drain_events)
                except Exception:
                    pass

    def _set_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in (self.mode_btn_sta, self.mode_btn_ap, self.connect_btn):
            try:
                btn.configure(state=state)
            except Exception:
                pass
        if enabled:
            # 恢复"当前模式的按钮是禁用"这一状态 —— 上面的循环刚把它打开了
            st = self.status
            if st is not None:
                try:
                    (self.mode_btn_sta if st.mode == "STA"
                     else self.mode_btn_ap).configure(state="disabled")
                except Exception:
                    pass

    # --- 连接管理 ----------------------------------------------------
    def _make_client(self, transport: Transport) -> DapClient:
        return DapClient(
            transport,
            on_log=lambda s: self.events.put(("log", (s, "board"))),
            on_disconnect=lambda s: self.events.put(("disconnect", s)),
        )

    def _open_mock(self) -> DeviceInfo:
        """只在工作线程跑:不许碰 tk 控件(见 _after_connect 里的端口回填)。"""
        self._close_client()
        self.client = self._make_client(MockTransport())
        self.client.open()
        info = self.client.ping()
        self._log("假设备已连接(密码一律 12345678)", "sys")
        return info

    def _open_port(self, port: str) -> DeviceInfo:
        self._close_client()
        self.client = self._make_client(SerialTransport(port))
        self.client.open()
        # 打开端口会让板子复位,等它起来再发命令。
        # 板子刚被反复复位过时启动会慢一些(实测最慢约 12s),给足余量。
        if not self.client.wait_ready(15.0):
            raise IOError("设备没有响应(打开串口后它会重启,15 秒内没能通信上)。\n"
                          "确认 USB 插在板子上标 USB 的 native 口,并且没有别的程序占着 %s。" % port)
        return self.client.info

    def _autodetect(self) -> List[Tuple[str, DeviceInfo]]:
        return probe_all()

    def _after_autodetect(self, found, err) -> None:
        ports = [p for p, _ in enumerate_ports()]
        self.port_combo.configure(values=ports)
        if err is not None:
            self._show_result("枚举串口失败:%s" % err, "err")
            return
        found = found or []
        if not found:
            self._set_conn("none")
            self.top_status.set("没探测到无线调试器")
            self._show_result(
                "没找到设备。\n\n"
                "  1) USB 线要插板子上标 USB 的 native 口(不是 CH343 那个,\n"
                "     插错那个会一直把芯片拽进下载模式)\n"
                "  2) 确认这个串口没被别的程序占着(串口助手 / 烧录工具 / 另一个本工具)\n"
                "  3) 拔插一次 USB,再点「刷新」\n\n"
                "已知端口:%s" % ("、".join(ports) or "(一个都没有)"), "warn")
            return
        if len(found) > 1:
            self._log("探测到 %d 个设备,用第一个" % len(found), "sys")
        port, info = found[0]
        self.port_var.set(port)
        self.port_combo.configure(values=[p for p, _ in found] + ports)
        # 探测阶段端口已经开过又关了,再开一次会重新给板子复位。先显示已
        # 探测到的信息,让用户马上看到"找到了",再在后台建正式连接。
        self._set_conn("ok")
        self.fw_var.set("dap-%s  (%s)" % (info.fw, info.mac))
        self._log("探测到 %s fw=%s,正在建立连接…" % (port, info.fw), "sys")
        self.submit("连接 %s" % port, lambda: self._open_port(port),
                    self._after_connect)

    def _connect_selected(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            self._show_result("先选一个串口。点「刷新」会自动探测。", "warn")
            return
        self.submit("连接 %s" % port,
                    lambda: self._open_port(port),
                    self._after_connect)

    def _on_serial_disconnect(self, reason: str) -> None:
        self._set_conn("lost")
        self._log("串口断开: %s" % reason, "err")

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

    def _after_connect(self, info, err) -> None:
        if err is not None:
            self._set_conn("error")
            self._show_result(
                "连接失败:%s\n\n"
                "常见原因:\n"
                "  · USB 线插在了 CH343 那个口 —— 要插板子上标 USB 的 native 口\n"
                "  · 另一个程序占着这个串口(串口助手 / 另一个本工具实例)\n"
                "  · 板子没上电" % err, "err")
            return
        assert isinstance(info, DeviceInfo)
        self._set_conn("ok")
        self.fw_var.set("dap-%s  (%s)" % (info.fw, info.mac))
        name = self.client.port_name if self.client else "?"
        # 假设备没有真实串口,回填个占位名,免得下拉框空着让人以为没连上
        if self.mock:
            self.port_var.set(name)
            self.port_combo.configure(values=[name])
        self._log("已连接 %s fw=%s mac=%s mode=%s"
                  % (name, info.fw, info.mac, info.mode), "ok")
        self.submit("读取状态", self._refresh_state, self._after_state_initial)
        if version_tuple(info.fw) < (0, 6):
            self._log("警告:固件版本 %s 可能不含配网命令,扫描/连接会报 404。" % info.fw, "sys")

    def _after_state_initial(self, result, err) -> None:
        """刚连上设备:先落状态,再自动扫一次。

        目标是"插上 USB 就看到 WiFi 列表",而不是让用户自己去找"扫描"按钮。
        AP 模式下扫不了,跳到 _after_state 让它显示 AP 该有的提示。
        """
        self._after_state(result, err)
        if err is not None:
            return
        st, _rows, _src, led = result
        self._apply_led_value(led)
        if st.mode != "STA":
            return
        self.submit("扫描 WiFi", self._scan_worker, self._after_scan)

    def _refresh_state(self):
        assert self.client is not None
        st = self.client.status()
        rows, source = None, None
        try:
            rows, source = self.client.list_saved()
        except Exception:
            pass
        # 顺带读一次亮度(老固件没有 @LED 命令,返回 None 就算了)
        led = None
        try:
            led = self.client.led_get()
        except Exception:
            pass
        return st, rows, source, led

    def _after_state(self, result, err, keep_result: bool = False) -> None:
        if err is not None:
            self._log("读取状态失败: %s" % err, "err")
            return
        st, rows, source, led = result
        self._apply_status(st)
        self._apply_led_value(led)
        if rows is not None:
            self._fill_saved(rows, source)
        # 连接成功后刚展示过"下一步怎么调试",别让例行的状态刷新把它冲掉
        if keep_result:
            return
        if st.mode == "AP":
            self._show_ap_hint(st)
        else:
            self._show_result(self._sta_summary(st), "info")

    def _apply_status(self, st: Status) -> None:
        self.status = st
        # 模式按钮带当前态:当前模式的按钮变 Primary 且禁用(再点无意义),
        # 另一个保持可点。没有这个指示,用户看不出现在在哪的模式里。
        cur = "AP" if st.mode == "AP" else "STA"
        self.mode_btn_sta.configure(
            text=("● STA(当前)" if cur == "STA" else "切换 STA"),
            style="Primary.TButton" if cur == "STA" else "Secondary.TButton",
            state="disabled" if cur == "STA" else "normal")
        self.mode_btn_ap.configure(
            text=("● AP(当前)" if cur == "AP" else "切换 AP"),
            style="Primary.TButton" if cur == "AP" else "Secondary.TButton",
            state="disabled" if cur == "AP" else "normal")
        if st.mode == "AP":
            self.top_status.set("AP 模式  ·  热点 %s  ·  地址 %s  ·  无外网"
                                % (st.ssid, st.ip))
            self.status_label.configure(bg="#eff6ff", fg="#1e40af")
        elif st.ip and st.ip != "0.0.0.0":
            self.top_status.set("STA 已连接  ·  %s  ·  %s  ·  %d dBm"
                                % (st.ssid, st.ip, st.rssi))
            self.status_label.configure(bg=self.C_OK_BG, fg=self.C_OK)
        elif st.prov:
            self.top_status.set("未配网  ·  还没有保存任何 WiFi  ·  等待扫描并连接")
            self.status_label.configure(bg=self.C_WARN_BG, fg=self.C_WARN)
        else:
            self.top_status.set("STA 未连接  ·  正在重试主网 %s(不会自动换网)"
                                % (st.ssid or "—"))
            self.status_label.configure(bg=self.C_WARN_BG, fg=self.C_WARN)

    def _set_conn(self, state: str) -> None:
        """右上角状态徽标。四种状态各自一套底色/文字色。"""
        text, fg, bg = {
            "ok":    ("已连接",   "#ffffff", self.C_OK),
            "error": ("连接失败", "#ffffff", self.C_ERR),
            "lost":  ("已断开",   "#ffffff", self.C_ERR),
            "none":  ("未连接",   self.C_TEXT_DIM, "#eceff3"),
        }.get(state, ("未连接", self.C_TEXT_DIM, "#eceff3"))
        self.conn_badge.configure(text="  %s  " % text, fg=fg, bg=bg)
        # 标题带串口号:多开窗口/发截图求助时一眼分清连的是哪块板
        port = ""
        try:
            if self.client is not None and self.client.is_open:
                port = self.client.port_name
        except Exception:
            port = ""
        self.root.title("%s — %s" % (self._base_title, port)
                        if port else self._base_title)

    # --- 配网 --------------------------------------------------------
    def _do_scan(self) -> None:
        if not self._require_client():
            return
        if self.status is not None and self.status.mode == "AP":
            self._show_result(
                "当前是 AP 模式,扫不了 WiFi。\n\n"
                "AP 模式下板子自己就是热点,IDF 驱动不支持同时扫描 —— 这是硬件/驱动层的限制,"
                "不是 bug。要扫描请先点上面的「切到 STA」。", "warn")
            return
        self.submit("扫描 WiFi", self._scan_worker, self._after_scan)

    def _scan_worker(self) -> List[Network]:
        assert self.client is not None
        return self.client.scan()

    def _after_scan(self, nets, err) -> None:
        if err is not None:
            if isinstance(err, DapError):
                r = err.reply
                info = explain_reply(r.code, r.text)
                self._show_result(
                    "扫描失败(%d %s)\n\n%s\n%s\n\n下一步:%s"
                    % (r.code, r.text, info.title, info.detail, info.action), "err")
            else:
                self._show_result("扫描失败:%s" % err, "err")
            return
        self._scan_rows = list(nets)
        self._sort_col, self._sort_desc = "rssi", True
        self._render_scan()
        if not nets:
            self._show_result("扫描完了,一个 WiFi 都没扫到。\n\n"
                              "确认路由器 2.4GHz 频段是开着的(ESP32 只支持 2.4GHz),"
                              "并且板子离路由器不要太远。", "warn")
        else:
            self._show_result(
                "扫到 %d 个网络,按信号从强到弱排列。\n"
                "在列表里选一个,填密码,点「连接选中」。" % len(nets), "info")

    def _render_scan(self) -> None:
        key = {"ssid": lambda n: n.ssid.lower(), "bar": lambda n: n.rssi,
               "rssi": lambda n: n.rssi, "auth": lambda n: n.auth.lower(),
               "ch": lambda n: n.channel}.get(self._sort_col, lambda n: n.rssi)
        rows = sorted(self._scan_rows, key=key, reverse=self._sort_desc)
        self.tree.delete(*self.tree.get_children())
        for i, n in enumerate(rows):
            tags = ("odd",) if i % 2 else ()
            self.tree.insert("", "end", tags=tags, values=(
                n.ssid, rssi_bar(n.rssi), "%d" % n.rssi, n.auth, "%d" % n.channel))

    def _sort_by(self, col: str) -> None:
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            # 文本列默认升序,数值列默认降序更符合直觉
            self._sort_desc = col in ("rssi", "bar", "ch")
        self._render_scan()

    def _selected_net(self) -> Optional[Network]:
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0], "values")
        ssid = vals[0]
        return next((n for n in self._scan_rows if n.ssid == ssid), None)

    def _on_scan_select(self, _evt=None) -> None:
        net = self._selected_net()
        if net is None:
            return
        self._show_result(
            "已选中:%s\n\n加密:%s   信号:%d dBm(%s)   信道:%d\n\n%s"
            % (net.ssid, net.auth, net.rssi, rssi_word(net.rssi), net.channel,
               "这是开放网络,不用填密码。直接点「连接选中」。"
               if net.auth.upper() == "OPEN"
               else "在右边「密码」框里填 WiFi 密码,然后点「连接选中」(或直接按回车)。"),
            "info")
        if net.auth.upper() != "OPEN":
            self.pass_entry.focus_set()

    def _toggle_pass(self) -> None:
        self.pass_entry.configure(show="" if self.show_pass.get() else "*")

    def _do_connect(self) -> None:
        if not self._require_client():
            return
        net = self._selected_net()
        if net is None:
            self._show_result("先在列表里选一个 WiFi(点一下那一行)。", "warn")
            return
        pw = self.pass_var.get()
        if net.auth.upper() != "OPEN" and not pw:
            self._show_result("「%s」需要密码,密码框还空着。" % net.ssid, "warn")
            return
        self._show_result("正在连接「%s」,最多等 25 秒…\n"
                          "(板子要先断开当前连接再连目标网络)" % net.ssid, "info")
        self.busy_var.set("连接 %s …" % net.ssid)
        self.submit("连接 %s" % net.ssid,
                    lambda: self.client.connect(net.ssid, pw) if self.client else None,
                    lambda r, e: self._after_connect_attempt(net, r, e))

    def _after_connect_attempt(self, net: Network, reply, err) -> None:
        if err is not None:
            self._show_result("连接过程出错:%s" % err, "err")
            return
        assert isinstance(reply, Reply)
        if not reply.ok:
            info = explain_reply(reply.code, reply.text)
            self._show_result(
                "连接「%s」失败:%s\n\n"
                "%s\n\n"
                "下一步:%s\n\n"
                "技术细节:%d %s"
                % (net.ssid, info.title, info.detail, info.action,
                   reply.code, reply.text), "err")
            self._log("CONNECT %s -> @ERR %d %s" % (net.ssid, reply.code, reply.text), "err")
            return

        # 成功:把 IP 挖出来
        m = RE_CONNECT_OK.match(reply.text)
        ip = m.group(2) if m else "?"
        ssid = m.group(1) if m else net.ssid
        self._log("CONNECT %s -> @OK %s" % (net.ssid, reply.text), "ok")
        self._show_done(ssid, ip)
        # 顺带刷新顶部状态栏,但保留刚展示的"下一步"提示
        self.submit("刷新状态", self._refresh_state,
                    lambda r, e: self._after_state(r, e, keep_result=True))

    def _show_done(self, ssid: str, ip: str) -> None:
        text = (
            "已连上「%s」 · 设备地址 %s\n"
            "\n"
            "下面三选一,都是复制即可用:\n"
            "\n"
            "usbip 虚拟 USB(OpenOCD / pyOCD / ocdup)\n"
            "usbip.exe attach -r %s -b 1-1\n"
            "警告:别执行 usbip list -r %s —— 本固件不支持,\n"
            "会永久挂起并把设备的 TCP 服务拖死。\n"
            "\n"
            "elaphureLink(Keil MDK)\n"
            "打开 elaphureLink.Wpf.exe,地址填 %s\n"
            "Keil → Options for Target → Debug → 选 elaphureLink\n"
            "Reset 必须选 SYSRESETREQ(选 Autodetect 会报不支持)\n"
            "\n"
            "串口桥(给目标板串口)\n"
            "TCP %s:%d,首包发波特率行,例如 19200E\n"
            "\n"
            "链路自检:ping %s,再测端口 %d / %d。"
            % (ssid, ip, ip, ip, ip, ip, PORT_UART_BRIDGE, ip,
               PORT_USBIP, PORT_UART_BRIDGE)
        )
        self._show_result(text, "ok")
        if hasattr(self, "res_hint"):
            self.res_hint.configure(text="下面这段可直接复制")

    def _sta_summary(self, st: Status) -> str:
        if st.prov:
            return ("当前:还没有配置任何 WiFi(出厂/恢复出厂后的状态)。\n\n"
                    "在列表里选一个网络、填密码、点「连接选中」—— 连上的这条\n"
                    "就是板子的主网,以后开机只试它一条,连不上也不会自动换网。\n"
                    "要换主网:重新连接别的网络,或在「已保存网络」里对某条点「固定优先」。")
        if st.ip and st.ip != "0.0.0.0":
            return ("当前:已连「%s」,地址 %s,信号 %d dBm(这是主网)。\n\n"
                    "板子开机只试这一条,连不上就按退避间隔重试,不会自动换别的网。\n"
                    "想换主网:点「扫描 WiFi」连一个新的;或者拔了路由器它就一直重试这一条。"
                    % (st.ssid, st.ip, st.rssi))
        return ("当前:STA 模式但主网连不上,板子正在按 1/2/5/10/15/30 秒的间隔重试同一条,"
                "不会自动换网。\n\n"
                "检查路由器;或者点「扫描 WiFi」手动换一个网络。" if st.mode == "STA" else "")

    def _show_ap_hint(self, st: Status) -> None:
        """AP 模式的结果面板。

        密码不再硬编码:向板子要 @AP。老固件(<=0.7.x)没有这条命令,
        会回 404 —— 那时只能退回"提示用户看板子标签/手册"的说法,绝不能
        再把 12345678 当成"当前密码"显示出来(那正是我们要消掉的隐患)。
        """
        info = None
        try:
            if self.client is not None:
                info = self.client.ap_get()
        except Exception:
            info = None

        if info is not None:
            pwd_line = "  密码:%s   (本机随机生成,已存在板子里)\n" % info.password
            step_line = "  1) 用电脑或手机的 WiFi 连上「%s」(密码 %s)\n" % (
                info.ssid, info.password)
            extra = ""
        else:
            pwd_line = ("  密码:这一版固件不支持 @AP,读不到随机密码。\n"
                        "        请升级到 v0.8.0+,或看板子背面的标签。\n")
            step_line = "  1) 用电脑或手机的 WiFi 连上「%s」\n" % st.ssid
            extra = ("\n> 老固件(≤0.7.x)的 AP 密码是公开常量 12345678,\n"
                     "> 强烈建议升级到 v0.8.0 使用随机密码。\n")

        self._show_result(
            "当前:AP 模式(板子自己发热点)\n"
            "\n"
            "  热点:%s\n"
            "%s"
            "  板子地址:%s\n"
            "\n"
            "接下来:\n"
            "%s"
            "  2) 调试端口照常:%s:%d(usbip)、%s:%d(串口桥)\n"
            "%s"
            "\n"
            "注意:\n"
            "  · AP 模式没有外网,连上后不能上网,这是正常的\n"
            "  · AP 模式下扫不了 WiFi(自己就是热点),要扫描请先切回 STA\n"
            "  · 密码是设备身份,恢复出厂不会改它;要换用「重新生成 AP 密码」"
            % (st.ssid, pwd_line, st.ip, step_line,
               st.ip, PORT_USBIP, st.ip, PORT_UART_BRIDGE, extra), "info")

    def _show_ap_password(self) -> None:
        """读并显示当前 AP 热点密码(用户主动点才查,不放进例行刷新)。"""
        if self.client is None:
            self._show_result("还没连上设备", "err")
            return
        self.submit("读取 AP 密码", self.client.ap_get,
                    self._after_show_ap_password)

    def _after_show_ap_password(self, result, err) -> None:
        if err is not None:
            self._show_result("读取失败:%s" % err, "err")
            return
        info = result
        if info is None:
            self._show_result(
                "这块板子的固件不支持 @AP(需要 v0.8.0+)。\n\n"
                "老固件的 AP 密码是公开常量 12345678 —— 也就是说谁都能连上\n"
                "它的热点。建议升级固件,用随机密码。", "warn")
            return
        self._show_result(
            "当前 AP 热点\n"
            "\n"
            "  名称:%s\n"
            "  密码:%s\n"
            "\n"
            "这个密码是板子首次开机时随机生成的,存在板子里,断电不丢。\n"
            "恢复出厂设置**不会**改它(密码属于设备身份,不是用户配置)。\n"
            "\n"
            "注意:只有切到 AP 模式时这个热点才存在;STA 模式下它不广播。"
            % (info.ssid, info.password), "info")

    def _regen_ap_password(self) -> None:
        """重新生成 AP 密码(带确认)。

        为什么需要确认:换掉之后**所有已经连上这个热点的设备都会掉线**,
        而且旧密码立刻作废。这不是一个该被误点的按钮。
        """
        if self.client is None:
            self._show_result("还没连上设备", "err")
            return
        if not self._messagebox.askyesno(
                "重新生成 AP 密码",
                "确定要换一个新的热点密码吗?\n\n"
                "换掉之后,所有已经连上这个热点的设备都会掉线,\n"
                "旧密码立刻作废,需要重新输入新密码。\n\n"
                "（新密码会存在板子里,断电不丢）"):
            return
        self.submit("重新生成 AP 密码", self.client.ap_regen,
                    self._after_regen_ap)

    def _after_regen_ap(self, result, err) -> None:
        if err is not None:
            self._show_result("重新生成失败:%s" % err, "err")
            return
        info = result
        if info is None:
            self._show_result(
                "板子不支持 @AP REGEN。\n\n"
                "这条命令是 v0.8.0 新增的,请升级固件后重试。", "err")
            return
        self._log("AP 密码已重新生成:%s" % info.password, "ok")
        self._show_result(
            "新的 AP 热点密码:%s\n"
            "\n"
            "板子上的密码已经换好了。\n"
            "%s"
            "\n"
            "下一步:\n"
            "  · 如果板子现在是 AP 模式:先切一次模式(或重启板子),新密码生效\n"
            "  · 如果板子现在是 STA 模式:下次切到 AP 模式时自动用新密码\n"
            "\n"
            "记得把新密码记下来 —— 恢复出厂不会还原密码,丢了只能用\n"
            "USB 连上板子再生成一个。"
            % (info.password,
               "(固件已提示:applies_on_next_mode_switch)"
               if info.note else ""), "ok")

    # --- 指示灯亮度 --------------------------------------------------
    def _apply_led_value(self, pct: Optional[int]) -> None:
        """把设备上的亮度回填到滑块(不触发下发)。"""
        if pct is None or not (1 <= pct <= 100):
            return
        try:
            # 滑块量程只到 20,超出的部分夹到上限显示
            self.led_var.set(min(pct, 20))
            self.led_pct_var.set("%d%%" % pct)
        except Exception:
            pass

    def _on_led_slide(self, _value=None) -> None:
        """滑块拖动时只更新数字,松手才发命令。

        ttk.Scale 没有"松手"事件,用 after() 做一个 350ms 的防抖:
        拖动过程中不断重置计时器,停下后才真正下发。
        """
        pct = int(round(self.led_var.get()))
        self.led_pct_var.set("%d%%" % pct)
        if self._led_timer is not None:
            try:
                self.root.after_cancel(self._led_timer)
            except Exception:
                pass
        self._led_timer = self.root.after(350, lambda: self._apply_led(pct))

    def _apply_led(self, pct: int) -> None:
        self._led_timer = None
        if self.client is None or not self.client.is_open:
            return
        self.submit("设置亮度", lambda: self.client.led_set(pct),
                    lambda r, e: self._after_led(pct, r, e))

    def _after_led(self, pct: int, reply, err) -> None:
        if err is not None:
            self._log("设置亮度失败: %s" % err, "err")
            return
        if not getattr(reply, "ok", False):
            self._log("设置亮度被拒: %s %s"
                      % (getattr(reply, "code", "?"), getattr(reply, "text", "")), "err")
            return
        self._log("指示灯亮度 -> %d%%(已保存)" % pct, "sys")

    # --- 模式 --------------------------------------------------------
    def _set_mode(self, mode: str) -> None:
        if not self._require_client():
            return
        self.submit("切到 %s" % mode, lambda: self.client.set_mode(mode),
                    lambda r, e: self._after_mode(mode, r, e))

    def _after_mode(self, mode: str, reply, err) -> None:
        if err is not None:
            self._show_result("切换失败:%s" % err, "err")
            return
        assert isinstance(reply, Reply)
        if not reply.ok:
            info = explain_reply(reply.code, reply.text)
            self._show_result("切换失败:%s\n\n%s\n\n下一步:%s\n\n技术细节:%d %s"
                              % (info.title, info.detail, info.action,
                                 reply.code, reply.text), "err")
            return
        self._log("@MODE %s -> @OK %s" % (mode, reply.text), "ok")
        if mode == "AP":
            st = parse_status(reply.text) or Status(mode="AP", ip=AP_DEFAULT_IP,
                                                    ssid="?")
            self._apply_status(st)
            self._show_ap_hint(st)
        else:
            self._show_result("已切到 STA 模式,板子开始按已保存网络逐个重连…", "info")
        self.submit("刷新状态", self._refresh_state, self._after_state)

    def _reboot(self) -> None:
        if not self._require_client():
            return
        self._show_result("正在重启板子,等它回来后自动重连…", "info")
        self.submit("重启板子", self._reboot_worker, self._after_reboot)

    def _reboot_worker(self):
        assert self.client is not None
        port = self.client.port_name
        if not self.mock:
            self.client.reboot()
        time.sleep(1.2)
        deadline = time.monotonic() + 25
        last = None
        while time.monotonic() < deadline:
            try:
                if self.mock:
                    self.client.ping(timeout=1.0)
                    return self.client.ping()
                self._close_client()
                self.client = self._make_client(SerialTransport(port))
                self.client.open()
                return self.client.ping(timeout=2.0)
            except Exception as exc:
                last = exc
                time.sleep(1.0)
        raise IOError("板子 25 秒内没回来(%s)。拔插一次 USB 后点「刷新」。" % last)

    def _after_reboot(self, info, err) -> None:
        if err is not None:
            self._set_conn("lost")
            self._show_result("重连失败:%s" % err, "err")
            return
        self._set_conn("ok")
        self.fw_var.set("dap-%s  (%s)" % (info.fw, info.mac))
        self._log("板子已回来 fw=%s mode=%s" % (info.fw, info.mode), "ok")
        self.submit("读取状态", self._refresh_state, self._after_state)

    # --- 已保存网络 --------------------------------------------------
    def _do_list(self) -> None:
        if not self._require_client():
            return
        self.submit("读取已保存网络", self._list_worker,
                    lambda r, e: self._after_list(r, e))

    def _list_worker(self):
        assert self.client is not None
        return self.client.list_saved()

    def _after_list(self, result, err) -> None:
        if err is not None:
            self._show_result("读取已保存网络失败:%s" % err, "err")
            return
        rows, source = result
        self._fill_saved(rows, source)

    def _fill_saved(self, rows, source) -> None:
        self.save_tree.delete(*self.save_tree.get_children())
        for i, (idx, ssid, pw, pin) in enumerate(rows):
            tags = ("odd",) if i % 2 else ()
            self.save_tree.insert("", "end", tags=tags, values=(
                idx, "\U0001F4CC" if pin else "", ssid, pw))
        src = {"factory": "来源:固件出厂列表(还没保存过任何网络)",
               "nvs": "来源:NVS(用户配网保存的)"}.get(source, "来源:%s" % source)
        self.src_var.set("%d 个  ·  %s" % (len(rows), src))

    def _on_saved_select(self, _event=None) -> None:
        """选中行变化时切换「固定优先」按钮的文字与状态。"""
        sel = self.save_tree.selection()
        if not sel:
            self.pin_btn.configure(text="固定优先")
            return
        vals = self.save_tree.item(sel[0], "values")
        if len(vals) >= 2 and vals[1] == "\U0001F4CC":
            self.pin_btn.configure(text="取消固定")
        else:
            self.pin_btn.configure(text="固定优先")

    def _do_toggle_pin(self) -> None:
        if not self._require_client():
            return
        sel = self.save_tree.selection()
        if not sel:
            self._show_result("先在列表里选一条要设置固定优先的记录。", "warn")
            return
        vals = self.save_tree.item(sel[0], "values")
        idx, ssid = int(vals[0]), vals[2] if len(vals) >= 4 else vals[1]
        pinned = len(vals) >= 2 and vals[1] == "\U0001F4CC"
        self.submit("固定优先 #%d" % idx,
                    lambda: self.client.set_pin(idx, not pinned),
                    lambda r, e: self._after_toggle_pin(r, e, not pinned, ssid))

    def _after_toggle_pin(self, reply, err, now_on, ssid) -> None:
        if err is not None:
            self._show_result("设置固定优先失败:%s" % err, "err")
            return
        assert isinstance(reply, Reply)
        if not reply.ok:
            info = explain_reply(reply.code, reply.text)
            self._show_result("设置固定优先失败:%s\n\n%s\n下一步:%s"
                              % (info.title, info.detail, info.action), "err")
            return
        self._log("@PIN -> @OK %s" % reply.text, "ok")
        if now_on:
            self._show_result(
                "已固定:%s\n\n设备开机永远先试这一条,也不会被「连接」的"
                "自动置顶顶掉。\n同一网络下多台设备漫游时,这条优先级最高。" % ssid,
                "ok")
        else:
            self._show_result("已取消固定:%s\n\n它回到普通优先级,按最近使用的顺序轮转。"
                              % ssid, "ok")
        self.submit("刷新", self._list_worker, lambda r, e: self._after_list(r, e))

    def _do_delete(self) -> None:
        if not self._require_client():
            return
        sel = self.save_tree.selection()
        if not sel:
            self._show_result("先在列表里选一条要删的记录。", "warn")
            return
        vals = self.save_tree.item(sel[0], "values")
        idx, ssid = int(vals[0]), vals[2]
        ok = self._messagebox.askyesno(
            "确认删除", "从板子的已保存网络里删掉这条?\n\n#%d  %s" % (idx, ssid))
        if not ok:
            return
        self.submit("删除 #%d" % idx, lambda: self.client.delete(idx),
                    lambda r, e: self._after_delete(r, e))

    def _after_delete(self, reply, err) -> None:
        if err is not None:
            self._show_result("删除失败:%s" % err, "err")
            return
        assert isinstance(reply, Reply)
        if not reply.ok:
            info = explain_reply(reply.code, reply.text)
            self._show_result("删除失败:%s\n\n%s\n下一步:%s"
                              % (info.title, info.detail, info.action), "err")
            return
        self._log("@DEL -> @OK %s" % reply.text, "ok")
        self._show_result("删除成功:%s" % reply.text, "ok")
        self.submit("刷新", self._list_worker, lambda r, e: self._after_list(r, e))

    def _do_prioritize(self) -> None:
        if not self._require_client():
            return
        sel = self.save_tree.selection()
        if not sel:
            self._show_result("先在列表里选一条要置顶的记录。", "warn")
            return
        vals = self.save_tree.item(sel[0], "values")
        idx, ssid = int(vals[0]), vals[2]
        if vals[1] == "\U0001F4CC":
            self._show_result(
                "%s 已固定优先,开机永远第一个试,不需要再置顶。" % ssid, "dim")
            return
        if idx == 0:
            self._show_result("%s 已经是第一位了,不用动。" % ssid, "dim")
            return
        self.submit("置顶 #%d" % idx, lambda: self.client.prioritize(idx),
                    lambda r, e: self._after_prioritize(r, e))

    def _after_prioritize(self, reply, err) -> None:
        if err is not None:
            self._show_result("置顶失败:%s" % err, "err")
            return
        assert isinstance(reply, Reply)
        if not reply.ok:
            info = explain_reply(reply.code, reply.text)
            self._show_result("置顶失败:%s\n\n%s\n下一步:%s"
                              % (info.title, info.detail, info.action), "err")
            return
        self._log("@PRIO -> @OK %s" % reply.text, "ok")
        self._show_result(
            "已置顶:%s\n\n设备开机后会先试这一条。\n"
            "注意:这只是挪顺序,连过别的网络后会被顶下去;要固定优先请用「固定优先」。"
            % reply.text, "ok")
        self.submit("刷新", self._list_worker, lambda r, e: self._after_list(r, e))

    def _do_factory(self) -> None:
        if not self._require_client():
            return
        ok = self._messagebox.askyesno(
            "确认恢复出厂",
            "会清空板子里保存的所有 WiFi 凭据,并重启板子。\n\n"
            "之后板子回到「未配网」状态:不连任何 WiFi,红色灯快闪,\n"
            "必须重新插 USB 用本工具配网。确定吗?")
        if not ok:
            return
        self._show_result("正在恢复出厂并重启,等板子回来后自动重连…", "info")
        self.submit("恢复出厂", self._factory_worker, self._after_reboot)

    def _factory_worker(self):
        assert self.client is not None
        port = self.client.port_name
        if not self.mock:
            self.client.factory_reset()
        time.sleep(1.2)
        deadline = time.monotonic() + 25
        last = None
        while time.monotonic() < deadline:
            try:
                if not self.mock:
                    self._close_client()
                    self.client = self._make_client(SerialTransport(port))
                    self.client.open()
                return self.client.ping(timeout=2.0)
            except Exception as exc:
                last = exc
                time.sleep(1.0)
        raise IOError("板子 25 秒内没回来(%s)。拔插一次 USB 后点「刷新」。" % last)

    # --- 日志 --------------------------------------------------------
    def _log(self, text: str, tag: str = "sys") -> None:
        """任何线程都能调,统一投递到 UI 队列。"""
        self.events.put(("log", (text, tag)))

    def _append_log(self, text: str, tag: str) -> None:
        if tag == "board" and not self.show_board_log.get():
            return
        if tag.startswith("proto") and not self.show_frames.get():
            return
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n", tag)
        self._log_lines += 1
        if self._log_lines > 4000:
            self.log.delete("1.0", "1200.0")
            self._log_lines -= 1200
        self.log.configure(state="disabled")
        if self.autoscroll.get():
            self.log.see("end")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._log_lines = 0

    def _copy_log(self) -> None:
        """把整个日志页复制到剪贴板 —— 求助时让用户把日志整段发过来。"""
        text = self.log.get("1.0", "end").rstrip()
        if not text:
            self._show_result("日志是空的,没什么可复制。", "dim")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._show_result("日志已复制(%d 行),直接粘贴发给对方。"
                          % text.count("\n"), "ok")

    def _send_manual(self) -> None:
        line = self.cmd_var.get().strip()
        if not line:
            return
        if not line.startswith("@"):
            line = "@" + line
        self.cmd_var.set("")
        if not self._require_client():
            return
        self._append_log(">>> %s" % line, "proto")
        self.submit("发送命令", lambda: self.client.request(line, TIMEOUT_CONNECT),
                    self._after_manual)

    def _after_manual(self, reply, err) -> None:
        if err is not None:
            self._append_log("!!! %s" % err, "err")
            return
        assert isinstance(reply, Reply)
        for d in reply.data:
            self._append_log("<<< @DATA %s" % d, "proto")
        if reply.ok:
            self._append_log("<<< @OK %s" % reply.text, "ok")
        else:
            self._append_log("<<< @ERR %d %s" % (reply.code, reply.text), "err")

    # --- 结果面板 ----------------------------------------------------
    def _show_result(self, text: str, tag: str = "info") -> None:
        """结果面板。

        做一点轻量排版:第一行当标题(加大加粗);命令行行(缩进或有明显
        命令特征)按等宽蓝字渲染并加左缩进;以"警告"开头的行标红。
        目的是让"下一步怎么做"那几段命令能一眼看到,不用在正文里找。
        """
        self.result.configure(state="normal")
        self.result.delete("1.0", "end")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if i == 0 and line.strip():
                self.result.insert("end", line + "\n", (tag, "h1"))
                continue
            s = line.strip()
            if not s:
                self.result.insert("end", "\n", tag)
            elif s.startswith(("警告", "注意:", "别执行")):
                self.result.insert("end", line + "\n", "err")
            elif s.startswith(("usbip", "elaphureLink", "TCP ", "ping ",
                               "python ", "Keil", ">", "usbip.exe")):
                self.result.insert("end", line + "\n", (tag, "cmd"))
            else:
                self.result.insert("end", line + "\n", tag)
        self.result.configure(state="disabled")
        self.result.yview_moveto(0.0)

    def _require_client(self) -> bool:
        if self.client is None or not self.client.is_open:
            self._show_result(
                "还没连上设备。\n\n"
                "1) 把 USB 线插在板子上标 USB 的 native 口(不是 CH343 那个)\n"
                "2) 点左上角「刷新」\n\n"
                "如果「刷新」后串口列表里有端口但探测不到,可能是这个口被别的程序占着"
                "(串口助手、烧录工具、另一个本工具窗口)。", "warn")
            return False
        return True

    def _on_close(self) -> None:
        # 置关闭标志:此后 _drain_events 不再碰任何控件。
        # 没有这一步会有真实的红色 Traceback:用户在一个慢任务(@CONNECT 最多
        # 32s、恢复出厂等)期间关窗,worker 线程稍后仍会投递 job_done,
        # 而窗口已经 destroy —— 回调里一碰 Text/Button 就抛
        # TclError: invalid command name "..."
        self._closing = True
        try:
            self.jobs.put(None)
        except Exception:
            pass
        # 必须收掉子进程:否则用户关掉本窗口后,openocd 还占着 3333 端口、
        # elaphureLink 还在后台跑,下次启动会撞端口或出现"两个代理"。
        try:
            bm = getattr(self, "_bm", None)
            if bm is not None:
                bm.stop_all()
        except Exception:
            pass
        self._close_client()
        self.root.destroy()

    # --- 主循环 ------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台默认 GBK,自测里的中文输出会炸;尽量切到 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description="无线调试器配网工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python dap_config.py                自动探测并打开界面\n"
               "  python dap_config.py --mock         用假设备试界面\n"
               "  python dap_config.py --list         只探测串口\n"
               "  python dap_config.py --raw \"@SCAN\"  直接发一条命令\n")
    ap.add_argument("--port", help="指定串口,例如 COM18")
    ap.add_argument("--mock", action="store_true", help="连接假设备,不需要板子")
    ap.add_argument("--list", action="store_true", help="枚举并探测所有串口后退出")
    ap.add_argument("--raw", metavar="CMD", help="直接发一条命令并打印响应")
    ap.add_argument("--selftest", action="store_true", help="无硬件自测")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_DEFAULT,
                    help="--raw 的超时秒数")
    ap.add_argument("--version", action="version", version="dap_config %s" % TOOL_VERSION)
    args = ap.parse_args(argv)

    if args.selftest:
        return run_selftest()
    if args.list:
        return run_list()
    if args.raw:
        return run_raw(args.port, args.raw, args.timeout, args.mock)

    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        print("这个 Python 没有 tkinter,装一个带 tk 的 Python 再跑。\n(%s)" % exc)
        return 3

    app = App(port=args.port, mock=args.mock)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
