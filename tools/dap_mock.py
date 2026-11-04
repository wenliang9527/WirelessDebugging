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
import zlib
from typing import Dict, List, Optional, Tuple

from dap_proto import AP_DEFAULT_IP, Transport, dec, enc

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
        # 离线烧录(0.9.0):8 槽位镜像 + 烧录进度机。预置一个演示镜像,
        # 无板也能把「烧录文件」页完整点一遍。
        self._files: Dict[int, dict] = {
            0: {"name": "demo-blink.bin", "size": 2048,
                "crc": "0a0b0c0d", "family": "PY32",
                "addr": 0x08000000, "seq": 1},
        }
        self._cur = 0
        self._seq = 1
        self._fput = None          # 进行中的 USB 兜底上传会话
        self._flash = {"state": "idle", "slot": -1, "pct": 0,
                       "idcode": "00000000", "err": 0, "msg": ""}

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
            self._emit("@OK pong fw=0.9.0 mac=AA:BB:CC:DD:EE:FC mode=%s" % self._mode, 0.02)
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
        elif cmd == "FLIST":
            self._mock_flist()
        elif cmd == "FSTAT":
            self._mock_fstat(args)
        elif cmd == "FDEL":
            self._mock_fdel(args)
        elif cmd == "FSEL":
            self._mock_fsel(args)
        elif cmd == "FNAME":
            self._mock_fname(args)
        elif cmd == "FPUT":
            self._mock_fput(args)
        elif cmd == "FLASH":
            self._mock_flash(args)
        elif cmd == "FPROG":
            f = self._flash
            if f["state"] == "fail":
                self._emit("@DATA state=%s slot=%d pct=%d idcode=%s err=%d msg=%s"
                           % (f["state"], f["slot"], f["pct"], f["idcode"],
                              f["err"], f["msg"]), 0.02)
            else:
                self._emit("@DATA state=%s slot=%d pct=%d idcode=%s"
                           % (f["state"], f["slot"], f["pct"], f["idcode"]),
                           0.02)
            self._emit("@OK prog", 0.02)
        else:
            self._emit("@ERR 404 %s" % cmd, 0.02)

    # --- 离线烧录(0.9.0)----------------------------------------------

    def _fmeta_line(self, slot: int, meta: dict) -> str:
        """与固件 fmeta_row 同构:name 走 URL 编码,cur 独立计算。"""
        return ("slot=%d name=%s size=%d crc=%s family=%s addr=0x%08x seq=%d"
                " cur=%d" % (slot, enc(meta["name"]), meta["size"], meta["crc"],
                             meta["family"], meta["addr"], meta["seq"],
                             1 if slot == self._cur else 0))

    def _mock_flist(self) -> None:
        for slot in sorted(self._files):
            self._emit("@DATA " + self._fmeta_line(slot, self._files[slot]), 0.02)
        self._emit("@OK files=%d cur=%d" % (len(self._files), self._cur), 0.03)

    def _mock_fstat(self, args: str) -> None:
        slot = self._slot_arg(args)
        meta = self._files.get(slot) if slot is not None else None
        if meta is None:
            self._emit("@ERR 431 slot_empty", 0.02)
            return
        self._emit("@DATA " + self._fmeta_line(slot, meta), 0.02)
        self._emit("@OK slot=%d" % slot, 0.02)

    def _mock_fdel(self, args: str) -> None:
        slot = self._slot_arg(args)
        if slot is None:
            self._emit("@ERR 400 usage: @FDEL slot=N", 0.02)
            return
        if slot not in self._files:
            self._emit("@ERR 431 slot_empty", 0.02)
            return
        del self._files[slot]
        if self._cur == slot:
            # 与固件同构:删的是当前槽 → 回落最旧(seq 最小),全空则 -1
            self._cur = (min(self._files, key=lambda s: self._files[s]["seq"])
                         if self._files else -1)
        self._emit("@OK deleted slot=%d cur=%d" % (slot, self._cur), 0.02)

    def _mock_fsel(self, args: str) -> None:
        slot = self._slot_arg(args)
        if slot is None:
            self._emit("@ERR 400 usage: @FSEL slot=N", 0.02)
            return
        if slot not in self._files:
            self._emit("@ERR 431 slot_empty", 0.02)
            return
        self._cur = slot
        self._emit("@OK current slot=%d" % slot, 0.02)

    def _mock_fname(self, args: str) -> None:
        slot = self._slot_arg(args)
        name = _kv(args, "name") or ""
        if slot is None or not name or len(name.encode("utf-8")) >= 32:
            self._emit("@ERR 400 usage: @FNAME slot=N name=<enc>", 0.02)
            return
        if slot not in self._files:
            self._emit("@ERR 431 slot_empty", 0.02)
            return
        self._files[slot]["name"] = name
        self._emit("@OK renamed slot=%d" % slot, 0.02)

    def _slot_arg(self, args: str) -> Optional[int]:
        try:
            slot = int(_kv(args, "slot") or "-99")
        except ValueError:
            return None
        return slot if 0 <= slot < 8 else None

    def _mock_fput(self, args: str) -> None:
        parts = args.split(None, 1)
        sub = parts[0].upper() if parts else ""
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "BEGIN":
            slot = self._slot_arg(rest)
            name = _kv(rest, "name") or ""
            family = _kv(rest, "family") or ""
            try:
                size = int(_kv(rest, "size") or "0")
                crc = int(_kv(rest, "crc") or "0", 16)
                addr = int(_kv(rest, "addr") or "0", 0)
            except ValueError:
                size, crc, addr = 0, 0, -1
            if (slot is None or not name or len(name.encode("utf-8")) >= 32
                    or size <= 0 or size % 2 or not family
                    or len(family) >= 8 or addr < 0):
                self._emit("@ERR 400 need slot_and_name", 0.02)
                return
            self._fput = {"slot": slot, "name": name, "size": size,
                          "crc": crc, "family": family, "addr": addr, "buf": bytearray()}
            self._emit("@OK begin slot=%d size=%d" % (slot, size), 0.02)
        elif sub == "DATA":
            if not self._fput:
                self._emit("@ERR 405 no_upload_in_progress", 0.02)
                return
            hexpart = rest.strip()
            try:
                if len(hexpart) % 2 or len(hexpart) // 2 > 128:
                    raise ValueError
                block = bytes.fromhex(hexpart)
            except ValueError:
                self._fput = None
                self._emit("@ERR 400 bad_hex_or_too_long", 0.02)
                return
            if len(self._fput["buf"]) + len(block) > self._fput["size"]:
                self._fput = None
                self._emit("@ERR 400 size_exceeds_declared", 0.02)
                return
            self._fput["buf"] += block
            self._emit("@OK written=%d" % len(block), 0.02)
        elif sub == "END":
            if not self._fput:
                self._emit("@ERR 405 no_upload_in_progress", 0.02)
                return
            up, self._fput = self._fput, None
            if len(up["buf"]) != up["size"]:
                self._emit("@ERR 400 size_mismatch", 0.02)
                return
            if zlib.crc32(bytes(up["buf"])) & 0xFFFFFFFF != up["crc"]:
                self._emit("@ERR 432 crc_mismatch", 0.02)
                return
            self._seq += 1
            self._files[up["slot"]] = {
                "name": up["name"], "size": up["size"],
                "crc": "%08x" % up["crc"], "family": up["family"],
                "addr": up["addr"], "seq": self._seq}
            self._emit("@OK saved crc_ok=1", 0.03)
        elif sub == "ABORT":
            self._fput = None
            self._emit("@OK aborted", 0.02)
        else:
            self._emit("@ERR 400 usage: @FPUT begin|data|end|abort ...", 0.02)

    def _mock_flash(self, args: str) -> None:
        if self._flash["state"] == "running":
            self._emit("@ERR 423 flash_in_progress", 0.02)
            return
        slot = self._slot_arg(args)
        if slot is None:
            slot = self._cur
        if slot is None or slot < 0:
            self._emit("@ERR 431 no_current_image", 0.02)
            return
        meta = self._files.get(slot)
        if meta is None:
            self._emit("@ERR 431 slot_empty", 0.02)
            return
        self._flash = {"state": "running", "slot": slot, "pct": 0,
                       "idcode": "00000000", "err": 0, "msg": ""}
        self._emit("@OK started slot=%d name=%s size=%d"
                   % (slot, meta["name"], meta["size"]), 0.02)
        # 后台推进假进度曲线:~3s 走完全程,idcode 在 3% 时"连上"
        threading.Thread(target=self._flash_worker, args=(meta,), daemon=True).start()

    def _flash_worker(self, meta: dict) -> None:
        for pct in range(0, 101, 2):
            time.sleep(0.06)
            self._flash["pct"] = pct
            if pct == 4:
                self._flash["idcode"] = "0bc11477"  # PY32F0 的 DP IDCODE
        self._flash["state"] = "done"
        self._flash["msg"] = "ok"

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


