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

# ----------------------------------------------------------------------
# #8 拆分(2026-11-01):本文件原是 4900 行单体,现按层拆为
#   dap_proto.py    线协议(常量/错误码/解析/Transport/DapClient/探测)
#   dap_mock.py     假设备 MockTransport(格式与 dap_cmd.c 交叉核对)
#   dap_gui.py      tkinter 界面
#   dap_selftest.py 无硬件自测(206+11 项,含 mock↔固件格式交叉核对)
# 本文件只留命令行入口(run_list/run_raw/main),对外行为不变:
# 入口路径、--mock/--list/--raw/--selftest、PyInstaller 打包、
# build_package 的验收流程都还认 dap_config.py。
# ----------------------------------------------------------------------


from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from dap_proto import (
    TIMEOUT_DEFAULT, TOOL_VERSION,
    DapClient, SerialTransport, Transport,
    enumerate_ports, explain_reply, probe_all, probe_port,
)
from dap_mock import MockTransport
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
        import dap_selftest  # 惰性导入:--version/--list 不该拉起全部模块
        return dap_selftest.run_selftest()
    if args.list:
        return run_list()
    if args.raw:
        return run_raw(args.port, args.raw, args.timeout, args.mock)

    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        print("这个 Python 没有 tkinter,装一个带 tk 的 Python 再跑。\n(%s)" % exc)
        return 3

    from dap_gui import App  # 惰性导入:命令行模式不需要 tkinter
    app = App(port=args.port, mock=args.mock)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
