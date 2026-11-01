#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dap_mock.py —— 假设备 MockTransport(自 dap_config.py 拆出,#8)

无板子时供 GUI / --raw --mock / --selftest 使用:模拟 @ 命令协议的全套应答、
板端日志、连接延迟与错误路径。**回复格式必须与固件 main/dap_cmd.c 一致**,
dap_selftest.py 的「mock↔固件格式交叉核对」盯这件事(正则 + 实文字面量双保险)。
"""

from __future__ import annotations

import time
import threading
from typing import List, Optional, Tuple

from dap_proto import AP_DEFAULT_IP, Transport, dec

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
                # 0.8.1 固件 @STATUS(STA) 恒带 prov(交叉核对项盯着;未配网=1)
                self._emit("@OK mode=STA ssid=%s ip=%s rssi=%d ch=%d prov=%d"
                           % (self._ssid, self._ip, self._rssi, self._ch,
                              0 if self._saved else 1), 0.02)
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


