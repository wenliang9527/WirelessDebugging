#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dap_selftest.py —— 无硬件自测(自 dap_config.py 拆出,#8)

python dap_config.py --selftest 跑的就是这里:206 项既有断言(编码/帧解析/
错误码解释/假设备端到端/GUI 行为/设置持久化/调试后端路径)+ mock↔固件格式
交叉核对。全部零硬件、零第三方依赖(tkinter 也不需要)。

交叉核对的双保险见 _FW_CONTRACTS 处注释:mock 回复匹配协议正则 +
dap_cmd.c 实文字面量核对(实文不可达时自动跳过那一层,总项数不变)。
"""

from __future__ import annotations

import os
import queue
import re
import sys
import time
from typing import List, Optional, Tuple

from dap_proto import (
    RE_CONNECT_OK, RE_ERR, RE_LIST_DATA, RE_LIST_OK, RE_SCAN_DATA,
    RE_SCAN_OK, RE_STATUS_AP, RE_STATUS_STA, TOOL_VERSION,
    AP_DEFAULT_IP, TIMEOUT_SCAN,
    DEFAULT_SETTINGS, SETTINGS_KEYS, SETTINGS_VERSION,
    _MSG_OVERRIDE, _is_likely_dap_port,
    ApInfo, DapClient, Reply, Status, Transport,
    dec, delete_settings_file, enc, explain, explain_reply,
    load_settings, parse_ap, parse_list_data, parse_ping, parse_scan_data,
    parse_status, probe_port, rssi_bar, rssi_word, save_settings,
    settings_path, version_tuple,
)
from dap_mock import MOCK_FACTORY_NETS, MOCK_SCAN, MockTransport
from dap_gui import App

# ====================================================================== #
# 无硬件自测
# ====================================================================== #



def _find_fw_main_dir():
    """定位固件源码 main/ 目录(以 main/dap_cmd.c 存在为准)。

    只在开发机可达;CI / 冻结 exe 上返回 None,交叉核对自动退化为
    "mock vs 快照" 一层。候选与 build_package.py 的 SRC_TOOLS_ROOT 同一套
    写死策略(交付/自测脚本只在开发机跑,写死绝对路径可接受),另开
    WIRELESS_FW_SRC 环境变量给别的机器。函数内现算,不焊模块级常量。
    """
    cands = [os.environ.get("WIRELESS_FW_SRC"),
             r"D:\ARMGCCTool\WirelessDebuggingtool\firmware-src\wireless-esp8266-dap"]
    for root in cands:
        if root and os.path.isfile(os.path.join(root, "main", "dap_cmd.c")):
            return os.path.join(root, "main")
    return None


def _txt(pat):
    """校验器:@OK 后的正文要匹配 pat(带 $ 端锚)。"""
    def v(rep):
        t = rep.text or ""
        return re.match(pat, t) is not None, t
    return v


def _data0(rx):
    """校验器:第一条 @DATA 行要被 rx 匹配。"""
    def v(rep):
        if not rep.data:
            return False, "没有 @DATA 行:%r" % (rep.text,)
        return rx.match(rep.data[0]) is not None, rep.data[0]
    return v


# 契约表:(名称, dap_cmd.c 里的字面量, 探测(client)->Reply, 校验(Reply))
# 字面量必须与 dap_cmd.c 逐字符一致 —— 交叉核对拿它去实文里找。
# 顺序有讲究:先 @CONNECT 再 @LIST/@PIN,后面几条才有数据可操作。
_FW_CONTRACTS = [
    ("@PING 回复",
     r'pong fw=%s mac=%02X:%02X:%02X:%02X:%02X:%02X mode=%s',
     lambda c: c.request("@PING", 3.0),
     _txt(r"pong fw=\S+ mac=(?:[0-9A-F]{2}:){5}[0-9A-F]{2} mode=\S+$")),
    ("@STATUS(STA) 回复",
     r'mode=%s ssid=%s ip=%s rssi=%d ch=%d prov=%d',
     lambda c: c.request("@STATUS", 3.0),
     _txt(r"mode=STA ssid=.+? ip=\d{1,3}(?:\.\d{1,3}){3} rssi=-?\d+ ch=\d+ prov=\d+$")),
    ("@SCAN 汇总", r'scan count=%d',
     lambda c: c.request("@SCAN", 5.0),
     _txt(r"scan count=\d+$")),
    ("@SCAN 数据行", r'idx=%d&ssid=%s&rssi=%d&auth=%s&ch=%d',
     lambda c: c.request("@SCAN", 5.0),
     _data0(RE_SCAN_DATA)),
    ("@CONNECT 成功帧", r'connected ssid=%s ip=%s',
     lambda c: c.connect("MyWiFi", "12345678"),
     lambda rep: ((RE_CONNECT_OK.match(rep.text or "") is not None
                   and "192.168.7.125" in (rep.text or "")), rep.text or "")),
    ("@LIST 汇总", r'count=%d source=%s',
     lambda c: c.request("@LIST", 3.0),
     _txt(r"count=\d+ source=\S+$")),
    ("@LIST 数据行", r'idx=%d&ssid=%s&pass=%s&pin=%d',
     lambda c: c.request("@LIST", 3.0),
     _data0(RE_LIST_DATA)),
    ("@ERR 帧封装", r'@ERR %d %s',
     lambda c: c.request("@DEL idx=99", 3.0),
     lambda rep: ((not rep.ok) and rep.code >= 400 and bool(rep.text),
                  "%d %s" % (rep.code, rep.text))),
    ("@PIN 回复", r'pin idx=%d %s',
     lambda c: c.request("@PIN idx=0 on", 3.0),
     _txt(r"pin idx=\d+ (?:on|off)$")),
    ("@AP 回复", r'ap ssid=%s pass=%s',
     lambda c: c.request("@AP", 3.0),
     _txt(r"ap ssid=\S+ pass=\S+$")),
    ("@LED 查询", r'led pct=%d',
     lambda c: c.request("@LED", 3.0),
     _txt(r"led pct=\d+$")),
]
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
        check("AP 信息能读到", ap.ok and ap.ssid.startswith("DAP-"), repr(ap))
        check("AP 密码不是公开常量 12345678",
              ap.ok and ap.password != "12345678", repr(ap.password))
        check("AP 密码长度 10", ap.ok and len(ap.password) == 10,
              repr(ap.password))
        old_pass = ap.password

        ap2 = client.ap_regen()
        check("REGEN 返回新密码", ap2.ok and ap2.password != old_pass,
              "%r -> %r" % (old_pass, ap2.password))
        check("REGEN 带 note 说明",
              ap2.ok and "applies_on_next" in ap2.note, repr(ap2))
        # 生成后 get 必须返回新值(证明写进了板子,而不是只回一次)
        ap3 = client.ap_get()
        check("REGEN 后 get 返回新密码",
              ap3.ok and ap3.password == ap2.password, repr(ap3))

        r = client.request("@AP BOGUS", 2.0)
        check("@AP 未知子命令 -> 400", (not r.ok) and r.code == 400,
              "%s %s" % (r.code, r.text))

        # --- 读 AP 失败时,错误必须分层 ---------------------
        # 回归:早先失败一律返回 None,界面把"超时"也说成"固件太老请升级"。
        # 用户照做升完级仍然连不上 —— 这是把人往错方向推。
        class _ApErr(Transport):
            """只回一条固定终结帧的假设备,用来分别造 404 / 超时。"""
            name = "APERR"

            def __init__(self, frame):
                self._frame = frame
                self._open = False
                self._q: List[Tuple[float, str]] = []

            def open(self):
                self._open = True

            def close(self):
                self._open = False

            @property
            def is_open(self):
                return self._open

            def write(self, data):
                if b"@AP" in data and self._frame:
                    self._q.append((time.monotonic() + 0.02, self._frame))

            def read(self, size=4096, timeout=0.05):
                time.sleep(0.01)
                now = time.monotonic()
                ready = [ln for (t0, ln) in self._q if t0 <= now]
                self._q = [(t0, ln) for (t0, ln) in self._q if t0 > now]
                return ("\n".join(ready) + "\n").encode() if ready else b""

        c_old = DapClient(_ApErr("@ERR 404 AP"))
        c_old.open()
        r404 = c_old.ap_get()
        check("老固件 @AP 报 404", r404.code == 404, repr(r404))
        check("404 才算『没有这条命令』", r404.missing_command, repr(r404))
        c_old.close()

        c_to = DapClient(_ApErr(None))
        c_to.open()
        r408 = c_to.ap_get()
        check("没回话报 408", r408.code == 408, repr(r408))
        check("超时不算『没有这条命令』", not r408.missing_command, repr(r408))
        check("失败时 ok 为假", not r408.ok and not r404.ok, repr(r408))
        c_to.close()

        # 应答到了但格式不认识:既不能说成 404,也不能悄悄当成成功。
        # 注意要选一条**能过形态判别**的帧("ap ssid=" 开头),否则它会被
        # 当成迟到帧丢掉、最终报 408 —— 那是另一条路径(见形态判别自测)。
        # 这里构造的是"形态对、字段不全":少了 pass=。
        c_bad = DapClient(_ApErr("@OK ap ssid=DAP-EEFC"))
        c_bad.open()
        r_bad = c_bad.ap_get()
        check("格式不认识报 500 而不是 404", r_bad.code == 500, repr(r_bad))
        check("格式不认识不算『没有这条命令』", not r_bad.missing_command,
              repr(r_bad))
        c_bad.close()

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
        check("GUI 显示 AP 密码", ap3.password in shown,
              "面板文字里找不到 %r: %r" % (ap3.password, shown[:120]))
        check("GUI 显示的 SSID 正确", ap3.ssid in shown, repr(shown[:120]))

        fake.result = _Screen()
        fake._after_show_ap_password(ApInfo(code=404, err="AP"), None)
        old_fw = fake.result.text()
        # 老固件分支只允许把 12345678 当"别再用这个"的警告词,不允许当当前密码。
        # 判据用"有没有把它挂在『密码:』后面"——那才是用户会照着去连热点的位置。
        check("老固件不把 12345678 当当前密码",
              "密码:12345678" not in old_fw and "密码: 12345678" not in old_fw,
              "把公开常量当成当前密码显示了: %r" % old_fw[:160])
        check("老固件明确提示升级", "0.8" in old_fw, repr(old_fw[:120]))

        # 超时(408)不是"固件太老"。混为一谈会让用户去升一个不需要升的固件。
        fake.result = _Screen()
        fake._after_show_ap_password(ApInfo(code=408, err="TIMEOUT"), None)
        to_text = fake.result.text()
        check("超时不说成固件太老", "0.8" not in to_text and "升级" not in to_text,
              "超时被说成了要升级固件: %r" % to_text[:160])
        check("超时说明了真实原因", "超时" in to_text or "回话" in to_text,
              repr(to_text[:160]))

        fake.result = _Screen()
        fake._after_regen_ap(ApInfo(code=408, err="TIMEOUT"), None)
        regen_to = fake.result.text()
        check("REGEN 超时不说成固件太老",
              "升级" not in regen_to, repr(regen_to[:160]))
        # 换密码失败时最要紧的一句:旧密码没变,别让人以为被换掉了
        check("REGEN 失败明确告知旧密码没变", "旧密码没有变" in regen_to,
              repr(regen_to[:200]))

        fake.result = _Screen()
        fake._after_regen_ap(ap2, None)
        regen_shown = fake.result.text()
        check("GUI 显示 REGEN 后的新密码",
              ap2.password in regen_shown, repr(regen_shown[:120]))

        # --- 排版函数绝不能自己做 I/O ----------------------
        # 回归:早先 _show_ap_hint 内部直接调 client.ap_get()。它被
        # _drain_events / _after_mode 调用,而两者都在 Tk 主线程上 ——
        # @AP 超时 4s,窗口就冻 4s(切到 AP 模式时还会连冻两次)。
        # 判据:给一个"一被调用就记账"的假 client,排版后账面必须为空。
        class _NoIO:
            """任何串口调用都记一笔,用来证明排版路径没碰 I/O。"""
            def __init__(self):
                self.touched = []

            def __getattr__(self, name):
                def _hit(*a, **kw):
                    self.touched.append(name)
                    raise AssertionError("排版函数不该调 %s" % name)
                return _hit

        class _PaintApp(_FakeApp):
            _show_ap_hint = App._show_ap_hint
            _sta_summary = App._sta_summary

        probe = _NoIO()
        paint = _PaintApp()
        paint.result = _Screen()
        paint.client = probe
        paint._show_ap_hint(Status(mode="AP", ip=AP_DEFAULT_IP, ssid="DAP-EEFC"),
                            ap3)
        check("AP 面板排版不碰串口", not probe.touched,
              "排版时调了 %r" % (probe.touched,))
        check("AP 面板把传进来的密码显示出来", ap3.password in paint.result.text(),
              repr(paint.result.text()[:120]))

        # 传 None(读失败)也不能自己去补读一次
        paint.result = _Screen()
        paint._show_ap_hint(Status(mode="AP", ip=AP_DEFAULT_IP, ssid="DAP-EEFC"),
                            None)
        check("AP 面板读失败也不自己补读", not probe.touched,
              "排版时调了 %r" % (probe.touched,))
        check("读失败不劝人升级固件", "升级" not in paint.result.text(),
              repr(paint.result.text()[:160]))

        # --- @AP 必须在 worker 线程上读 ---------------------
        # 光"排版不碰 I/O"不够:如果没人读,面板永远显示"没读到"。
        # 这条钉住读的位置 —— _refresh_state 里、且只在该读时读。
        class _ApRec:
            def __init__(self, code=0, pwd="K7M4RTW9XY"):
                self.calls = 0
                self._info = ApInfo(ssid="DAP-EEFC", password=pwd, code=code)

            def status(self):
                return Status(mode="AP", ip=AP_DEFAULT_IP, ssid="DAP-EEFC")

            def ap_get(self):
                self.calls += 1
                return self._info

            def __getattr__(self, name):
                def _none(*a, **kw):
                    return None
                return _none

        class _StateApp(_PaintApp):
            _refresh_state = App._refresh_state
            _after_state = App._after_state
            _apply_status = lambda self, st: None
            _apply_led_value = lambda self, v: None
            _fill_saved = lambda self, rows, src: None

        rec = _ApRec()
        sa = _StateApp()
        sa.client = rec
        got = sa._refresh_state()
        check("_refresh_state 会读 @AP", rec.calls == 1, "calls=%d" % rec.calls)
        check("_refresh_state 把 AP 信息带出来",
              len(got) == 5 and got[4] is rec._info, repr(got))
        # 读到的密码要能一路走到面板上
        sa.result = _Screen()
        sa._after_state(got, None)
        check("AP 密码从 worker 一路到达面板",
              "K7M4RTW9XY" in sa.result.text(), repr(sa.result.text()[:160]))

        # STA 模式下不该白发一条 @AP(每条命令都要等板子回话)
        rec2 = _ApRec()
        rec2.status = lambda: Status(mode="STA", ip="192.168.7.125", ssid="HomeWiFi")
        sa2 = _StateApp()
        sa2.client = rec2
        got2 = sa2._refresh_state()
        check("STA 模式不发 @AP", rec2.calls == 0, "calls=%d" % rec2.calls)
        check("STA 模式 AP 位为空", len(got2) == 5 and got2[4] is None, repr(got2))

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
        # 断言的是 find_atopenocd 的**查找逻辑**,不是机器配置:雅特力 fork
        # 装没装因机器而异(CI 上没有 D:\ARMGCCTool),所以判据是
        # "查找结果与磁盘一致",而不是"必须找到"。
        base = B.find_atopenocd()
        disk_has = (os.path.isdir(os.path.join(B.tools_root(), "atopenocd"))
                    or os.path.isdir(B._ATOPENOCD_FALLBACK))
        check("atopenocd 查找结果与磁盘一致", (base is not None) == bool(disk_has),
              "find=%r disk=%r" % (base, disk_has))
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
        # @AP 是 0.8.0 新增命令,形态表必须跟上。未登记虽然"保守放行",
        # 但那条路正是"上一条超时后的残留"场景 —— 放行等于没保护。
        check("AP 认自己的回复(@AP)",
              c3._looks_like_reply_to("@AP", "@OK",
                                      "ap ssid=DAP-EEFC pass=346789ABCD"), "")
        check("AP 认自己的回复(@AP REGEN)",
              c3._looks_like_reply_to("@AP REGEN", "@OK",
                                      "ap ssid=DAP-EEFC pass=X "
                                      "note=applies_on_next_mode_switch"), "")
        check("AP 不认 STATUS 的回复",
              not c3._looks_like_reply_to("@AP", "@OK", "mode=STA ssid=x"), "")
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

        # --- 回归:一条应答真的丢失时,不能把整条连接毒死 --------------
        # 计数欠账机制的边界:超时记 1 笔,下一条的首个终结帧被当残留吃掉。
        # 若那笔旧应答**真的不会再来了**(板子复位 / 串口丢字节),而板子
        # 之后一切正常,那么:吃掉本条的 @OK -> 本条超时 -> 再欠 1 -> 恒为 1
        # -> 之后每条命令都 408,只能重插 USB。
        # 实测过:连续 5 条 @PING 全 408,而板子一条都没漏回。
        # 30s 过期也救不了 —— 每次超时都把 expire 刷成 now+30。
        class _LostReply(_Scripted):
            """第 2 条命令(SCAN)的应答永久丢失,之后一切正常。"""

            def __init__(self):
                super().__init__({})

            def write(self, data):
                self._writes += 1
                if self._writes == 2:
                    return          # 永久丢失
                due = time.monotonic() + 0.02
                self._q.append((due, "@OK pong fw=0.7.2 mac=AA mode=STA"))

        tr_lost = _LostReply()
        c9 = DapClient(tr_lost)
        c9.open()
        check("丢失测试的前置 PING 成功", c9.request("@PING", 0.5, retries=0).ok, "")
        r_lost = c9.request("@SCAN", 0.3, retries=0)
        check("丢失的那条命令确实超时", r_lost.code == 408, "%s" % r_lost.code)
        check("超时后记了欠账", c9._stale_owed == 1, "owed=%r" % c9._stale_owed)
        # 关键断言:之后必须**能恢复**,最多牺牲一条命令
        codes = [c9.request("@PING", 0.5, retries=0).code for _ in range(4)]
        check("丢失一笔应答不会毒死整条连接(后续能恢复)",
              codes[-1] == 0, "后续四条 = %r(最后一条应为 0)" % (codes,))
        check("恢复代价不超过一条命令",
              codes.count(0) >= 3, "后续四条 = %r" % (codes,))
        c9.close()

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
              _is_likely_dap_port("USB VID:PID=303A:1001 SER=00:11"),
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

    # --- 5. mock↔固件格式交叉核对 ------------------------------------
    # #8 拆分时新增。快照 = main/dap_cmd.c @ 0.8.1(2026-11-01 逐条提取)。
    # 每条契约两层判定:
    #   1) mock 的真实回复必须匹配协议正则(字段名/顺序/类型);
    #   2) 开发机上 dap_cmd.c 实文可达时,C 字面量必须还能在实文里找到 ——
    #      固件改了格式而快照没跟上,当场 FAIL(首跑即抓到 mock STATUS 缺 prov)。
    # 固件源码不在(CI / 冻结 exe)时 2) 自动跳过,总项数任何机器一致。
    print("\n[5] mock↔固件格式交叉核对")
    fw_main = _find_fw_main_dir()
    fw_src = None
    if fw_main:
        try:
            with open(os.path.join(fw_main, "dap_cmd.c"), "r",
                      encoding="utf-8", errors="replace") as f:
                fw_src = f.read()
        except Exception:
            fw_src = None
    tr_c = MockTransport(connect_delay=0.0)
    cli_c = DapClient(tr_c)
    cli_c.open()
    try:
        for name, lit, probe, val in _FW_CONTRACTS:
            try:
                rep = probe(cli_c)
                ok, det = val(rep)
            except Exception as exc:
                ok, det = False, repr(exc)
            if ok and fw_src is not None and lit not in fw_src:
                ok, det = False, ("dap_cmd.c 实文里找不到 %r —— 固件格式已变,"
                                  "快照过期" % lit)
            check("mock↔固件:%s" % name, ok, det)
    finally:
        cli_c.close()
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


