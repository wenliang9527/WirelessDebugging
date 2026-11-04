#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dap_gui.py —— tkinter 界面(自 dap_config.py 拆出,#8)

给同事用的图形化配网界面:插上 USB、选 WiFi、填密码、看结果。
tkinter 仍只在 App.__init__ 里惰性导入 —— 命令行模式与 --selftest
不需要 GUI 栈,保持零依赖自测性质。
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from dap_proto import (
    AP_DEFAULT_IP, BAUD, DapClient, DEFAULT_SETTINGS, PORT_UART_BRIDGE,
    PORT_USBIP, RE_CONNECT_OK, RE_ERR, RE_LIST_DATA, RE_LIST_OK, RE_PING,
    RE_SCAN_DATA, RE_SCAN_OK, RE_STATUS_AP, RE_STATUS_STA,
    SETTINGS_KEYS, SETTINGS_VERSION,
    TIMEOUT_CONNECT, TIMEOUT_DEFAULT, TIMEOUT_SCAN, TOOL_VERSION,
    ApInfo, DeviceInfo, DapError, ErrInfo, FlashImage, Network, Reply,
    SerialTransport, Status, Transport,
    dec, delete_settings_file, enc, enumerate_ports, explain, explain_reply,
    load_settings, parse_ap, parse_list_data, parse_ping, parse_scan_data,
    parse_status, probe_all, probe_port, rssi_bar, rssi_word, save_settings,
    settings_path, version_tuple,
)
import dap_upload
import zlib
from dap_mock import MockTransport

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
    C_RAIL      = "#eef1f5"   # 侧栏底色(方案三)

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
        self._busy_label = ""
        self._closing = False
        self._cmd_buttons: List[object] = []   # 所有会发串口命令的按钮
        self._log_lines = 0
        self._led_timer = None
        self._led_suspend = False   # _apply_led_value 程序回填期间挂起防抖
        # 用户界面偏好(设备地址 / 两个 target)。文件缺失或损坏时是空 dict,
        # 界面各处用 .get(key, 默认值) 兜底 —— 见 load_settings() 的说明。
        self._settings = load_settings()
        # 已经写进磁盘的那份,用于"只在真的变了才写"
        self._settings_saved = dict(self._settings)

        # DPI 感知(体检 P2-5):不开的话 tk 按 96 DPI 排版再被系统位图拉伸,
        # 文字发虚;920x760 在 150% 缩放的 1080p 屏上物理高度超出屏幕,
        # 状态栏直接看不见。必须在 Tk() 创建之前设置。
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)   # Win 8.1+
            except Exception:
                try:
                    ctypes.windll.user32.SetProcessDPIAware()     # Win 7 兜底
                except Exception:
                    pass
        self.root = tk.Tk()
        self.root.title("无线调试器 · 配网工具")
        self._base_title = self.root.title()
        try:
            self._dscale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:
            self._dscale = 1.0
        self.root.geometry("%dx%d" % (self._px(920), self._px(760)))
        self.root.minsize(self._px(820), self._px(660))
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

        self._build_shell()

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
    def _px(self, n: float) -> int:
        """像素尺寸按 DPI 缩放。字体走磅值会自己跟 DPI,像素值不会。"""
        return max(1, int(round(n * getattr(self, "_dscale", 1.0))))

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
        # 描边红:破坏性次级动作(删除/恢复出厂)。整块大红留给"正在发生的
        # 错误";这类按钮要醒目,但不该和主操作抢视觉权重(体检 H 维)。
        s.configure("DangerOutline.TButton", font=(ui, 10, "bold"),
                    padding=(12, 6), background="#ffffff",
                    foreground=self.C_ERR, borderwidth=1, relief="solid",
                    bordercolor="#e3a1a1", focusthickness=0)
        s.map("DangerOutline.TButton",
              background=[("active", "#fef2f2"), ("disabled", "#f3f4f6")],
              foreground=[("active", "#991b1b"), ("disabled", "#a8b0ba")],
              bordercolor=[("active", "#dc2626"), ("disabled", "#e3e6ea")])
        # 次级动作(固定优先):白底蓝字蓝框,和 Primary 一个色系但分清主次
        s.configure("Secondary.TButton", font=(ui, 10, "bold"), padding=(12, 6),
                    background="#ffffff", foreground=self.C_PRIMARY_H,
                    borderwidth=1, relief="solid", bordercolor=self.C_PRIMARY)
        s.map("Secondary.TButton",
              background=[("active", "#eff6ff"), ("pressed", "#dbeafe")],
              foreground=[("active", "#1e3fa8"), ("disabled", "#a8b0ba")],
              bordercolor=[("active", "#1e3fa8"), ("disabled", "#d8dde4")])

        # 表格
        s.configure("Treeview", font=(mono, 10), rowheight=self._px(30),
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

    # --- 界面搭建(方案三:左侧导航栏 + 右侧主区)----------------------
    # 页面栈用 grid + tkraise 切换,代替顶部 Notebook。理由:
    #   1) 四个页名左对齐列出来,一眼扫完,不用认"第几个标签";
    #   2) 调试接入页底部那句"不要同时开"的安全警告不再被窗口下边缘裁掉
    #      (体检 P2-2:旧布局页内容超出可用高度且无滚动条,拉都拉不到)。
    NAV_ITEMS = (("config", "配网"), ("saved", "已保存网络"),
                 ("offline", "烧录文件"), ("debug", "调试接入"), ("log", "日志"))

    def _build_shell(self) -> None:
        tk, ttk = self._tk, self._ttk
        self._nav_rows = {}
        shell = tk.Frame(self.root, bg=self.C_BG)
        shell.pack(fill="both", expand=True)

        # --- 左侧导航栏 ---
        rail = tk.Frame(shell, bg=self.C_RAIL, width=self._px(172))
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        tk.Label(rail, text="无线调试器", bg=self.C_RAIL, fg=self.C_TEXT,
                 font=(self._ui, 11, "bold"), anchor="w",
                 padx=14, pady=6).pack(fill="x")
        for key, text in self.NAV_ITEMS:
            row = tk.Label(rail, text=text, bg=self.C_RAIL, fg=self.C_TEXT,
                           font=(self._ui, 10), anchor="w", padx=14, pady=8,
                           cursor="hand2")
            row.pack(fill="x")
            row.bind("<Button-1>", lambda _e, k=key: self._show_page(k))
            self._nav_rows[key] = row
        verbox = tk.Frame(rail, bg=self.C_RAIL)
        verbox.pack(side="bottom", fill="x", padx=14, pady=8)
        self.fw_var = tk.StringVar(value="—")
        self.fw_label = tk.Label(verbox, textvariable=self.fw_var,
                                 bg=self.C_RAIL, fg=self.C_TEXT_DIM,
                                 font=(self._mono, 9), anchor="w")
        self.fw_label.pack(fill="x")
        tk.Label(verbox, text="v%s" % TOOL_VERSION, bg=self.C_RAIL,
                 fg=self.C_TEXT_DIM, font=(self._ui, 9),
                 anchor="w").pack(fill="x")

        # --- 右侧主区:设备行 + 状态条 + 页面栈 + 忙碌条 ---
        main = tk.Frame(shell, bg=self.C_BG)
        main.pack(side="left", fill="both", expand=True)
        top = tk.Frame(main, bg=self.C_BG)
        top.pack(fill="x", padx=self._px(14), pady=(self._px(12), 0))

        row1 = tk.Frame(top, bg=self.C_BG)
        row1.pack(fill="x")
        tk.Label(row1, text="设备", bg=self.C_BG, fg=self.C_TEXT,
                 font=(self._ui, 10, "bold")).pack(side="left")
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(row1, textvariable=self.port_var,
                                       width=11, state="readonly",
                                       font=(self._mono, 10))
        self.port_combo.pack(side="left", padx=(10, 6))
        b_probe = ttk.Button(row1, text="自动探测", width=10,
                             command=lambda: self.submit("探测设备", self._autodetect,
                                                         self._after_autodetect))
        b_probe.pack(side="left")
        self._cmd_buttons.append(b_probe)
        b_conn = ttk.Button(row1, text="连接", width=7,
                            command=self._connect_selected)
        b_conn.pack(side="left", padx=(6, 0))
        self._cmd_buttons.append(b_conn)
        self.conn_badge = tk.Label(row1, text="  未连接  ", bg="#eceff3",
                                   fg=self.C_TEXT_DIM,
                                   font=(self._ui, 10, "bold"), padx=10, pady=4)
        self.conn_badge.pack(side="right")
        self.conn_label = self.conn_badge     # 兼容旧引用

        # 状态条:UI 字体(体检 P3-1 —— 中文走等宽字体的回退渲染发虚)
        self.top_status = tk.StringVar(value="正在探测串口…")
        self.status_label = tk.Label(top, textvariable=self.top_status,
                                     bg=self.C_INFO_BG, fg=self.C_TEXT,
                                     font=(self._ui, 10), anchor="w",
                                     padx=10, pady=7)
        self.status_label.pack(fill="x", pady=(self._px(10), 0))

        self.page_stack = tk.Frame(main, bg=self.C_BG)
        self.page_stack.pack(fill="both", expand=True,
                             padx=self._px(14), pady=(self._px(10), 0))
        self.page_stack.rowconfigure(0, weight=1)
        self.page_stack.columnconfigure(0, weight=1)
        self._pages = {}
        for key, _text in self.NAV_ITEMS:
            page = tk.Frame(self.page_stack, bg=self.C_BG)
            page.grid(row=0, column=0, sticky="nsew")
            self._pages[key] = page

        self.busy_var = tk.StringVar(value="就绪")
        tk.Label(main, textvariable=self.busy_var, bg=self.C_BG,
                 fg=self.C_TEXT_DIM, font=(self._ui, 9), anchor="w").pack(
            fill="x", padx=self._px(14), pady=(self._px(8), self._px(10)))

        self._build_page_config(self._pages["config"])
        self._build_page_saved(self._pages["saved"])
        self._build_page_offline(self._pages["offline"])
        self._build_page_debug(self._pages["debug"])
        self._build_page_log(self._pages["log"])
        self._show_page("config")

    def _show_page(self, key: str) -> None:
        """切页面:导航项高亮 + tkraise。页面常驻内存,切换零开销。"""
        for k, row in self._nav_rows.items():
            active = (k == key)
            row.configure(
                bg=self.C_PRIMARY if active else self.C_RAIL,
                fg="#ffffff" if active else self.C_TEXT,
                font=(self._ui, 10, "bold") if active else (self._ui, 10))
        self._pages[key].tkraise()

    def _build_page_config(self, parent) -> None:
        """配网页:设备控制卡(模式/指示灯)+ 扫描工具条 + 列表 + 结果面板。"""
        tk, ttk = self._tk, self._ttk
        mono, ui = self._mono, self._ui

        # 模式与指示灯:从旧顶部卡第三行迁来,独占一卡不再七组控件挤一行
        card, body = self._card(parent)
        card.pack(fill="x")
        row3 = tk.Frame(body, bg=self.C_CARD)
        row3.pack(fill="x")
        tk.Label(row3, text="模式", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10, "bold")).pack(side="left")
        self.mode_btn_sta = ttk.Button(row3, text="切换 STA", width=10,
                                       style="Secondary.TButton",
                                       command=lambda: self._set_mode("STA"))
        self.mode_btn_sta.pack(side="left", padx=(10, 5))
        self.mode_btn_ap = ttk.Button(row3, text="切换 AP", width=9,
                                      style="Secondary.TButton",
                                      command=lambda: self._set_mode("AP"))
        self.mode_btn_ap.pack(side="left")
        b_reboot = ttk.Button(row3, text="重启设备", width=9,
                              command=self._reboot)
        b_reboot.pack(side="left", padx=(14, 0))
        self._cmd_buttons.append(b_reboot)
        # AP 密码是随机生成的、存在板子里 —— 用户必须有个地方能看到它,
        # 否则"连不上自家热点"就没救了。也提供重新生成(带确认)。
        b_ap = ttk.Button(row3, text="AP 密码", width=9,
                          command=self._show_ap_password)
        b_ap.pack(side="left", padx=(6, 0))
        self._cmd_buttons.append(b_ap)
        b_regen = ttk.Button(row3, text="重新生成", width=9,
                             command=self._regen_ap_password)
        b_regen.pack(side="left", padx=(2, 0))
        self._cmd_buttons.append(b_regen)

        # 指示灯滑块:经典 tk.Scale —— clam 主题的 ttk.Scale 槽条和白卡
        # 几乎同色,滑块等于隐形(体检 P2-4)。浅蓝槽 + 主色活动态,一眼可见。
        ledrow = tk.Frame(body, bg=self.C_CARD)
        ledrow.pack(fill="x", pady=(10, 0))
        tk.Label(ledrow, text="指示灯", bg=self.C_CARD, fg=self.C_TEXT,
                 font=(ui, 10, "bold")).pack(side="left")
        self.led_var = tk.IntVar(self.root, value=4)
        self.led_scale = tk.Scale(ledrow, from_=1, to=20, orient="horizontal",
                                  showvalue=False, length=self._px(130),
                                  variable=self.led_var,
                                  command=self._on_led_slide,
                                  troughcolor="#dbeafe", bg=self.C_CARD,
                                  fg=self.C_TEXT,
                                  activebackground=self.C_PRIMARY,
                                  highlightthickness=0, borderwidth=0,
                                  sliderlength=self._px(16))
        self.led_scale.pack(side="left", padx=(8, 6))
        self.led_pct_var = tk.StringVar(value="4%")
        tk.Label(ledrow, textvariable=self.led_pct_var, bg=self.C_CARD,
                 fg=self.C_TEXT_DIM, font=(mono, 9), width=4,
                 anchor="w").pack(side="left")
        tk.Label(ledrow, text="灯太刺眼可当场调低,不用重烧固件",
                 bg=self.C_CARD, fg=self.C_TEXT_DIM,
                 font=(ui, 9)).pack(side="left", padx=(8, 0))

        # --- 扫描工具条:左"扫描/刷新",右"密码组 + 连接" ---
        bar = tk.Frame(parent, bg=self.C_BG)
        bar.pack(fill="x", pady=(self._px(10), self._px(8)))
        b_scan = ttk.Button(bar, text="扫描 WiFi", style="Primary.TButton",
                            command=self._do_scan)
        b_scan.pack(side="left")
        self._cmd_buttons.append(b_scan)
        b_rescan = ttk.Button(bar, text="刷新列表", command=self._do_scan)
        b_rescan.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(b_rescan)
        self.connect_btn = ttk.Button(bar, text="连接选中",
                                      style="Primary.TButton",
                                      command=self._do_connect)
        self.connect_btn.pack(side="right")
        self._cmd_buttons.append(self.connect_btn)
        self.show_pass = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="显示", variable=self.show_pass,
                        command=self._toggle_pass).pack(side="right",
                                                        padx=(0, 8))
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(bar, textvariable=self.pass_var, show="*",
                                    width=22, font=(mono, 10))
        self.pass_entry.pack(side="right")
        self.pass_entry.bind("<Return>", lambda e: self._do_connect())
        tk.Label(bar, text="密码", bg=self.C_BG, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="right", padx=(0, 8))

        cols = ("ssid", "bar", "rssi", "auth", "ch")
        tree_wrap = tk.Frame(parent, bg=self.C_BG)
        # 表格按内容高度(最多 8 行)取空间,剩下的都给下面的结果面板 ——
        # 扫描结果通常不到 10 行,而"下一步怎么调试"那段说明有 20 行。
        tree_wrap.pack(fill="x")
        self.tree = ttk.Treeview(tree_wrap, columns=cols, show="headings",
                                 selectmode="browse", height=7)
        heads = (("ssid", "WiFi 名称", 300, "w"), ("bar", "信号", 90, "center"),
                 ("rssi", "强度", 80, "center"), ("auth", "加密方式", 130, "w"),
                 ("ch", "信道", 70, "center"))
        for key, text, width, anchor in heads:
            self.tree.heading(key, text=text,
                              command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=self._px(width), anchor=anchor,
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
        res_head = tk.Frame(parent, bg=self.C_BG)
        res_head.pack(fill="x", pady=(self._px(10), self._px(6)))
        tk.Label(res_head, text="提示与结果", bg=self.C_BG, fg=self.C_TEXT,
                 font=(ui, 10, "bold")).pack(side="left")
        self.res_hint = tk.Label(res_head, text="", bg=self.C_BG,
                                 fg=self.C_TEXT_DIM, font=(ui, 9))
        self.res_hint.pack(side="right")
        # 「下一步怎么做」给的就是要复制去用的命令,却没有复制按钮 —— 补一个
        # (体检 P3-6;求助场景同样受益)。
        ttk.Button(res_head, text="复制", width=6,
                   command=self._copy_result).pack(side="right", padx=(0, 10))

        res_holder = tk.Frame(parent, bg=self.C_BORDER)
        res_holder.pack(fill="both", expand=True)
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

    def _build_page_saved(self, parent) -> None:
        """已保存网络页:置顶/固定/删除 + 统计;恢复出厂收在右下角。"""
        tk, ttk = self._tk, self._ttk
        ui = self._ui

        bar2 = tk.Frame(parent, bg=self.C_BG)
        bar2.pack(fill="x", pady=(0, self._px(8)))
        b_prio = ttk.Button(bar2, text="置顶选中", style="Primary.TButton",
                            command=self._do_prioritize)
        b_prio.pack(side="left")
        self._cmd_buttons.append(b_prio)
        self.pin_btn = ttk.Button(bar2, text="固定优先", style="Secondary.TButton",
                                  width=10, command=self._do_toggle_pin)
        self.pin_btn.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(self.pin_btn)
        b_del = ttk.Button(bar2, text="删除选中", style="DangerOutline.TButton",
                           command=self._do_delete)
        b_del.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(b_del)
        b_list = ttk.Button(bar2, text="刷新", command=self._do_list)
        b_list.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(b_list)
        # 统计独立成行右对齐:和按钮挤同一行的话,窄页宽下文字会被裁
        # (方案三目检发现;来源说明较长,一行放不下)
        self.src_var = tk.StringVar(value="")
        tk.Label(parent, textvariable=self.src_var, bg=self.C_BG,
                 fg=self.C_TEXT_DIM, font=(ui, 9), anchor="e").pack(
            fill="x", pady=(0, self._px(4)))

        cols2 = ("idx", "pin", "ssid", "pass")
        save_wrap = tk.Frame(parent, bg=self.C_BG)
        save_wrap.pack(fill="both", expand=True)
        self.save_tree = ttk.Treeview(save_wrap, columns=cols2, show="headings",
                                      height=10)
        for key, text, width, anchor in (("idx", "#", 50, "center"),
                                         ("pin", "固定", 56, "center"),
                                         ("ssid", "WiFi 名称", 330, "w"),
                                         ("pass", "密码", 140, "w")):
            self.save_tree.heading(key, text=text)
            # pin 列不许 stretch:📌(宽)和空串(窄)渲染宽度不同,
            # 列宽跟着内容抖会让整张表横向跳
            self.save_tree.column(key, width=self._px(width),
                                  minwidth=self._px(width),
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
        bottom = tk.Frame(parent, bg=self.C_BG)
        bottom.pack(fill="x", pady=(self._px(8), 0))
        # 全窗最具破坏性的动作:描边红 + 收在右下,不和普通删除并排同权重
        # (体检 H 维;确认框之前的 busy 闸门见 _do_factory)
        b_factory = ttk.Button(bottom, text="恢复出厂设置",
                               style="DangerOutline.TButton",
                               command=self._do_factory)
        b_factory.pack(side="right", padx=(10, 0))
        self._cmd_buttons.append(b_factory)
        tk.Label(bottom, text=note, bg=self.C_BG, fg=self.C_TEXT_DIM,
                 font=(ui, 9), justify="left",
                 anchor="w").pack(side="left", fill="x", expand=True)

    def _build_page_offline(self, parent) -> None:
        """烧录文件页:镜像管理 + 一键离线烧录(第二阶段)。"""
        tk, ttk = self._tk, self._ttk
        ui = self._ui

        bar = tk.Frame(parent, bg=self.C_BG)
        bar.pack(fill="x", pady=(0, self._px(6)))
        b_up = ttk.Button(bar, text="上传镜像…", style="Primary.TButton",
                          command=self._do_upload)
        b_up.pack(side="left")
        self._cmd_buttons.append(b_up)
        b_cur = ttk.Button(bar, text="设为当前", style="Secondary.TButton",
                           width=9, command=self._do_fsel)
        b_cur.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(b_cur)
        b_ren = ttk.Button(bar, text="改名", width=6,
                           command=self._do_frename)
        b_ren.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(b_ren)
        b_del = ttk.Button(bar, text="删除选中", style="DangerOutline.TButton",
                           command=self._do_fdel)
        b_del.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(b_del)
        b_ref = ttk.Button(bar, text="刷新", command=self._do_flist)
        b_ref.pack(side="left", padx=(8, 0))
        self._cmd_buttons.append(b_ref)

        # 上传参数(新镜像的元数据):家族跟着固件算法注册表走,当前只有 PY32
        row_meta = tk.Frame(parent, bg=self.C_BG)
        row_meta.pack(fill="x", pady=(0, self._px(6)))
        tk.Label(row_meta, text="目标家族", bg=self.C_BG, fg=self.C_TEXT_DIM,
                 font=(ui, 9)).pack(side="left")
        self.off_family = tk.StringVar(value="PY32")
        cb = ttk.Combobox(row_meta, textvariable=self.off_family, width=8,
                          state="readonly", values=["PY32"])
        cb.pack(side="left", padx=(4, 12))
        tk.Label(row_meta, text="烧录基址", bg=self.C_BG, fg=self.C_TEXT_DIM,
                 font=(ui, 9)).pack(side="left")
        self.off_addr = tk.StringVar(value="0x08000000")
        ent = ttk.Entry(row_meta, textvariable=self.off_addr, width=12)
        ent.pack(side="left", padx=(4, 0))
        self.off_hint = tk.Label(row_meta, text="", bg=self.C_BG,
                                 fg=self.C_TEXT_DIM, font=(ui, 9), anchor="e")
        self.off_hint.pack(side="right")

        cols = ("slot", "cur", "name", "size", "family", "addr", "seq")
        wrap = tk.Frame(parent, bg=self.C_BG)
        wrap.pack(fill="both", expand=True)
        self.off_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                     height=8)
        for key, text, width, anchor in (
                ("slot", "槽", 36, "center"),
                ("cur", "当前", 50, "center"),
                ("name", "镜像名称", 260, "w"),
                ("size", "大小", 76, "e"),
                ("family", "家族", 64, "center"),
                ("addr", "基址", 96, "center"),
                ("seq", "上传序", 60, "center")):
            self.off_tree.heading(key, text=text)
            self.off_tree.column(key, width=self._px(width),
                                 minwidth=self._px(width), anchor=anchor,
                                 stretch=(key == "name"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.off_tree.yview)
        self.off_tree.configure(yscrollcommand=vsb.set)
        self.off_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.off_tree.tag_configure("odd", background="#f6f8fb")

        # 烧录区:一键烧录 + 进度条。进度条上传/烧录共用(两个阶段不会重叠)
        row_flash = tk.Frame(parent, bg=self.C_BG)
        row_flash.pack(fill="x", pady=(self._px(8), 0))
        self.b_flash = ttk.Button(row_flash, text="一键烧录(当前镜像)",
                                  style="Primary.TButton",
                                  command=self._do_flash)
        self.b_flash.pack(side="left")
        self._cmd_buttons.append(self.b_flash)
        self.off_bar = ttk.Progressbar(row_flash, maximum=100, length=220)
        self.off_bar.pack(side="left", padx=(12, 8), fill="x", expand=True)
        self.off_state = tk.StringVar(value="空闲")
        tk.Label(row_flash, textvariable=self.off_state, bg=self.C_BG,
                 fg=self.C_TEXT_DIM, font=(ui, 9), width=30,
                 anchor="w").pack(side="left")

        note = ("镜像存在调试器自己的存储区(最多 8 个);「一键烧录」由板上的\n"
                "烧录引擎执行,不需要 PC 端 OpenOCD/usbip。没有电脑时也可:\n"
                "板上 BOOT 键长按 ≥2 秒 = 烧「当前」镜像(绿=成功,红=失败)。\n"
                "上传走 WiFi(快,需设备已连网);连不上自动回落 USB(慢,应急)。")
        tk.Label(parent, text=note, bg=self.C_BG, fg=self.C_TEXT_DIM,
                 font=(ui, 9), justify="left", anchor="w").pack(
            fill="x", pady=(self._px(8), 0))

    # --- 离线烧录动作 ---------------------------------------------------

    def _err_text(self, err: BaseException) -> str:
        """worker 抛错的统一文本:DapError 给人话,其他给异常本身。"""
        if isinstance(err, DapError):
            info = explain_reply(err.reply.code, err.reply.text)
            return "%s(%s)" % (info.title, err.reply.text or err.reply.code)
        return str(err)

    def _ask_open_file(self, title: str, patterns) -> str:
        from tkinter import filedialog
        return filedialog.askopenfilename(
            parent=self.root, title=title,
            filetypes=patterns + [("所有文件", "*.*")])

    def _ask_string(self, title: str, prompt: str, initial: str = "") -> Optional[str]:
        from tkinter import simpledialog
        return simpledialog.askstring(title, prompt, parent=self.root,
                                      initialvalue=initial)

    def _offline_gate(self) -> bool:
        """离线命令的公共闸门:已连接 + 固件 ≥0.9。返回 False 时已给出提示。"""
        if self.client is None:
            self._show_result("先连接设备(右上「连接」)。", "warn")
            return False
        info = getattr(self.client, "info", None)
        if info and version_tuple(info.fw) < (0, 9):
            self._show_result(
                "板上固件 %s 不含离线烧录功能,需要 0.9.0 或更新。\n"
                "升级见 03_firmware/固件烧录指南.md(这次升级要连分区表一起烧)。" % info.fw,
                "warn")
            return False
        return True

    def _selected_slot(self) -> Optional[int]:
        sel = self.off_tree.selection()
        if not sel:
            self._show_result("先在表格里选中一行。", "warn")
            return None
        return int(self.off_tree.item(sel[0], "values")[0])

    def _do_flist(self) -> None:
        if not self._offline_gate():
            return
        self.submit("刷新镜像列表", self._flist_job, self._after_flist)

    def _flist_job(self) -> List[FlashImage]:
        return self.client.flash_list()

    def _after_flist(self, imgs, err) -> None:
        if err is not None:
            self._show_result("读镜像列表失败:%s" % self._err_text(err), "err")
            return
        self._fill_offline_tree(imgs or [])

    def _fill_offline_tree(self, imgs: List[FlashImage]) -> None:
        self.off_tree.delete(*self.off_tree.get_children())
        for k, img in enumerate(sorted(imgs, key=lambda x: x.slot)):
            size = "%d KB" % (img.size // 1024) if img.size >= 1024 else "%d B" % img.size
            self.off_tree.insert("", "end",
                                 values=(img.slot, "📌" if img.current else "",
                                         img.name, size, img.family,
                                         "0x%08X" % img.addr, "#%d" % img.seq),
                                 tags=("odd",) if k % 2 else ())
        self.off_hint.configure(
            text="%d/8 槽位" % len(imgs) +
                 (" · 无当前镜像" if imgs and not any(i.current for i in imgs)
                  else ""))

    def _do_fsel(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            return
        self.submit("设为当前",
                    lambda: self.client.flash_select(slot),
                    lambda r, e: self._after_simple(r, e, "设为当前", self._do_flist))

    def _do_fdel(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            return
        name = self.off_tree.item(self.off_tree.selection()[0], "values")[2]
        if not self._messagebox.askyesno(
                "确认删除",
                "删除槽位 %d 的「%s」?\n板上的镜像文件会一起删掉,不可恢复。"
                % (slot, name)):
            return
        self.submit("删除镜像",
                    lambda: self.client.flash_del(slot),
                    lambda r, e: self._after_simple(r, e, "删除", self._do_flist))

    def _do_frename(self) -> None:
        slot = self._selected_slot()
        if slot is None:
            return
        cur = self.off_tree.item(self.off_tree.selection()[0], "values")[2]
        name = self._ask_string("改名", "新名字(UTF-8 编码后要小于 32 字节):", cur)
        if name is None or not name.strip():
            return
        name = name.strip()
        self.submit("改名",
                    lambda: self.client.flash_rename(slot, name),
                    lambda r, e: self._after_simple(r, e, "改名", self._do_flist))

    def _after_simple(self, reply, err, what: str, refresh) -> None:
        """flash_select/del/rename 的公共收尾:失败给人话,成功刷新列表。"""
        if err is not None:
            self._show_result("%s失败:%s" % (what, self._err_text(err)), "err")
            return
        if not reply.ok:
            info = explain_reply(reply.code, reply.text)
            self._show_result("%s失败:%s\n%s" % (what, info.title, info.action),
                              "err")
            return
        refresh()

    # -- 上传 ----------------------------------------------------------

    def _do_upload(self) -> None:
        if not self._offline_gate():
            return
        path = self._ask_open_file("选择镜像文件(.bin)",
                                   [("BIN", "*.bin"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            self._show_result("读文件失败:%s" % exc, "err")
            return
        if not data:
            self._show_result("空文件。", "warn")
            return
        if len(data) > 4 * 1024 * 1024:
            self._show_result("镜像超过 4MB,当前家族的分区装不下。", "warn")
            return
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            family = self.off_family.get().strip()
            addr = int(self.off_addr.get().strip(), 16)
        except ValueError:
            self._show_result("烧录基址要是十六进制,如 0x08000000。", "warn")
            return
        if len(data) % 2:
            self._show_result("镜像大小是奇数字节(%d B)。目标按 16 位半字编程,"
                              "工具不做静默补零 —— 请从源头生成偶数镜像。" % len(data),
                              "warn")
            return
        self.submit("上传镜像 %s" % name,
                    lambda: self._upload_job(data, name, family, addr),
                    self._after_upload)

    def _upload_job(self, data: bytes, name: str, family: str,
                    addr: int) -> Tuple[str, int]:
        """工作线程里跑:选空槽 → WiFi 上传为主,失败回落 USB @FPUT。"""
        used = {i.slot for i in self.client.flash_list()}
        slot = next((s for s in range(8) if s not in used), None)
        if slot is None:
            raise RuntimeError("8 个槽位都满了,先删一个再传")

        # WiFi 优先:快(几百 KB 秒级)。需要设备已连网且拿到 IP。
        if not self.mock:
            ip = None
            try:
                st = self.client.status()
                ip = st.ip if getattr(st, "ip", "") else (
                    AP_DEFAULT_IP if st.mode == "AP" else None)
            except Exception:
                ip = None
            if ip:
                try:
                    dap_upload.upload_image(
                        ip, slot, name, family, addr, data,
                        progress=lambda p: self.events.put(
                            ("offline_progress", (p, "上传中(WiFi)"))))
                    return "wifi", slot
                except (dap_upload.UploadError, OSError) as exc:
                    self._log("WiFi 上传失败(%s),回落 USB 兜底…" % exc, "sys")

        # USB 兜底:每行 128 字节,几百 KB 要传一两分钟,界面先说清楚
        self.events.put(("offline_progress", (0, "上传中(USB 兜底,较慢)")))
        crc = zlib.crc32(data) & 0xFFFFFFFF
        r = self.client.fput_begin(slot, name, len(data), crc, family, addr)
        if not r.ok:
            raise DapError(r)
        try:
            step = max(1, len(data) // 100)
            for off in range(0, len(data), 128):
                r = self.client.fput_data(data[off:off + 128])
                if not r.ok:
                    raise DapError(r)
                if off % step < 128:
                    self.events.put(("offline_progress",
                                     (off * 100 // len(data), "上传中(USB 兜底)")))
            r = self.client.fput_end()
            if not r.ok:
                raise DapError(r)
        except BaseException:
            try:
                self.client.fput_abort()
            except Exception:
                pass
            raise
        return "usb", slot

    def _after_upload(self, result, err) -> None:
        if err is not None:
            self.events.put(("offline_progress", (0, "上传失败")))
            if isinstance(err, DapError):
                info = explain_reply(err.reply.code, err.reply.text)
                self._show_result("上传失败:%s\n%s" % (info.title, info.action), "err")
            else:
                self._show_result("上传失败:%s" % self._err_text(err), "err")
            return
        via, slot = result
        self._show_result("已存入槽位 %d(%s)。烧录前可在表格里核对 CRC/大小。"
                          % (slot, "WiFi" if via == "wifi" else "USB 兜底"), "ok")
        self.events.put(("offline_progress", (100, "上传完成")))
        self._do_flist()

    # -- 烧录与进度轮询 --------------------------------------------------

    def _do_flash(self) -> None:
        if not self._offline_gate():
            return
        sel = self.off_tree.selection()
        slot = int(self.off_tree.item(sel[0], "values")[0]) if sel else None
        self.submit("触发烧录",
                    lambda: self.client.flash_start(slot),
                    lambda r, e: self._after_flash_start(r, e))

    def _after_flash_start(self, reply, err) -> None:
        if err is not None:
            self._show_result("触发失败:%s" % self._err_text(err), "err")
            return
        if not reply.ok:
            info = explain_reply(reply.code, reply.text)
            self._show_result("烧录没能开始:%s\n%s" % (info.title, info.action),
                              "err")
            return
        self.events.put(("offline_progress", (0, "烧录中")))
        self._log("烧录已开始,进度轮询中…", "sys")
        self.root.after(500, self._poll_flash)

    def _poll_flash(self) -> None:
        """500ms 一拍地查 @FPROG。走 submit_bg(不占 busy 闸门,按钮保持可用,
        用户此刻点别的操作只会在这条串行队列里排队,不会互相踩)。"""
        if getattr(self, "_closing", False):
            return
        self.submit_bg("烧录进度", self.client.flash_progress,
                       self._after_flash_poll)

    def _after_flash_poll(self, prog, err) -> None:
        if getattr(self, "_closing", False):
            return
        if err is not None or prog is None:
            # 偶发超时不终止轮询:烧录还在板上跑,下一拍多半就回来了
            self._poll_miss = getattr(self, "_poll_miss", 0) + 1
            if self._poll_miss >= 6:
                self.events.put(("offline_progress", (0, "进度丢失")))
                self._show_result("连续 3 秒取不到进度。烧录可能仍在板上进行,"
                                  "稍后点「刷新」看结果,或看板上灯:绿=成/红=败。",
                                  "warn")
                self._poll_miss = 0
                return
            self.root.after(500, self._poll_flash)
            return
        self._poll_miss = 0
        self.events.put(("offline_progress",
                         (prog.pct, "烧录中" if prog.running else prog.state)))
        if prog.running:
            self.root.after(500, self._poll_flash)
        elif prog.state == "done":
            self._show_result("烧录完成(100%%,回读校验通过)。目标已复位运行。",
                              "ok")
        else:
            info = explain_reply(prog.err, prog.msg)
            self._show_result("烧录失败:%s\n%s\n(目标 IDCODE=%s)"
                              % (info.title, info.action, prog.idcode or "?"),
                              "err")

    def _build_page_log(self, parent) -> None:
        """日志页:黑底彩色日志 + 手动命令。"""
        tk, ttk = self._tk, self._ttk
        mono, ui = self._mono, self._ui

        bar3 = tk.Frame(parent, bg=self.C_BG)
        bar3.pack(fill="x", pady=(0, self._px(8)))
        ttk.Checkbutton(bar3, text="板端日志", variable=self.show_board_log
                        ).pack(side="left")
        ttk.Checkbutton(bar3, text="协议帧", variable=self.show_frames
                        ).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(bar3, text="自动滚动", variable=self.autoscroll
                        ).pack(side="left", padx=(14, 0))
        ttk.Button(bar3, text="复制日志",
                   command=self._copy_log).pack(side="right", padx=(0, 8))
        ttk.Button(bar3, text="清空", command=self._clear_log).pack(side="right")

        log_holder = tk.Frame(parent, bg=self.C_BORDER)
        log_holder.pack(fill="both", expand=True)
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

        cmdbar = tk.Frame(parent, bg=self.C_BG)
        cmdbar.pack(fill="x", pady=(self._px(8), 0))
        tk.Label(cmdbar, text="手动命令", bg=self.C_BG, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="left")
        self.cmd_var = tk.StringVar()
        e = ttk.Entry(cmdbar, textvariable=self.cmd_var, font=(mono, 10))
        e.pack(side="left", fill="x", expand=True, padx=(10, 8))
        e.bind("<Return>", lambda ev: self._send_manual())
        ttk.Button(cmdbar, text="发送", style="Primary.TButton",
                   command=self._send_manual).pack(side="right")

    def _build_page_debug(self, parent) -> None:
        """调试接入页:三条路各自一个开关。

        为什么做成三个独立开关而不是"一键全开":usbip 和 Keil/OpenOCD 会抢
        同一个 DAP 会话(固件只支持单连接)。一键全开必然打架,而让用户
        自己选反而清晰 —— 页底那行红字就是在说这件事。
        """
        tk, ttk = self._tk, self._ttk
        ui, mono = self._ui, self._mono
        tab = parent

        # 顶部:当前设备地址(决定三个后端都连哪)
        bar = tk.Frame(tab, bg=self.C_BG)
        bar.pack(fill="x", pady=(0, 6))
        tk.Label(bar, text="设备地址", bg=self.C_BG, fg=self.C_TEXT,
                 font=(ui, 10)).pack(side="left")
        # 启动时的默认值来自 settings.json(用户上次填的),没有才用 dap.local
        self.addr_var = tk.StringVar(
            value=self._settings.get("addr", DEFAULT_SETTINGS["addr"]))
        e = ttk.Entry(bar, textvariable=self.addr_var, width=24, font=(mono, 10))
        e.pack(side="left", padx=(10, 6))
        e.bind("<Return>", lambda ev: self._apply_addr())
        ttk.Button(bar, text="应用", command=self._apply_addr).pack(side="left")
        tk.Label(bar, text="(mDNS 名,IP 变了也不用改)", bg=self.C_BG,
                 fg=self.C_TEXT_DIM, font=(ui, 9)).pack(side="left", padx=(10, 0))
        # 恢复默认 + 显示设置文件位置:用户得知道工具往哪儿写了东西
        ttk.Button(bar, text="恢复默认", width=9,
                   command=self._reset_settings).pack(side="right")

        # 四张卡片。usbip 与 at32 是**同一套底座**(usbip 把设备挂成本地 USB,
        # at32 用带 AT32 flash 驱动的 openocd 去烧),所以放相邻并注明依赖关系。
        self._backend_rows = {}
        # usbip 状态缓存:驱动装没装(进程内缓存一次)、可达性(5s TTL),
        # 避免每次刷新都在 UI 线程跑 pnputil / DNS 超时把界面卡住。
        self._usbip_driver_state = None      # Optional[bool];None=还没检测到
        self._usbip_driver_checking = False  # 后台检测中(pnputil 不进 UI 线程)
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
            card.pack(fill="x", pady=(0, 6))
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
        trow = tk.Frame(tab, bg=self.C_BG)
        trow.pack(fill="x", pady=(0, 6))
        tk.Label(trow, text="OpenOCD 目标芯片", bg=self.C_BG, fg=self.C_TEXT,
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
        arow = tk.Frame(tab, bg=self.C_BG)
        arow.pack(fill="x", pady=(0, 6))
        tk.Label(arow, text="AT32 目标芯片", bg=self.C_BG, fg=self.C_TEXT,
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

        # 底部警告:方案三布局里这行永远在可视区内(体检 P2-2)
        warn = tk.Frame(tab, bg=self.C_WARN_BG)
        warn.pack(fill="x", pady=(6, 0))
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

    def _usbip_driver_cached(self, ub, force: bool = False):
        """驱动装没装 —— 只回缓存,pnputil 一律后台跑(体检 P2-3)。

        pnputil 常态 250-300ms、subprocess timeout 是 20s:放在 UI 线程,
        首帧和每次「重新检测」都会卡一下,最坏把界面冻住 20 秒。同文件里
        reachable()/attached() 都已经后台化,这里是最后一个漏网的。

        返回 None = 还在检测(调用方显示"正在检测",按钮先禁用);
        force 由「重新检测」按钮触发,作废缓存让 _refresh_backends 重查。
        """
        if force:
            self._usbip_driver_state = None
        if self._usbip_driver_state is None and not self._usbip_driver_checking:
            self._usbip_driver_checking = True

            def work():
                try:
                    state = bool(ub.driver_installed())
                except Exception:
                    state = False
                self.events.put(("usbip_driver", state))

            threading.Thread(target=work, daemon=True,
                             name="usbip-driver").start()
        return self._usbip_driver_state

    def _on_usbip_driver(self, state: bool) -> None:
        self._usbip_driver_checking = False
        self._usbip_driver_state = bool(state)
        self._refresh_backends()

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
            if inst is None:
                row["state"].configure(text="正在检测驱动…", fg=self.C_TEXT_DIM)
                row["btn"].configure(text="挂载", state="disabled")
            elif not inst:
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
                drv = self._usbip_driver_cached(bm.usbip)
                if drv is None:
                    self._show_result(
                        "正在检测 usbip 驱动状态,几秒后再点「启动」。", "info")
                    return
                if not drv:
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
            drv = self._usbip_driver_cached(bm.usbip)
            if drv is None:
                self._show_result("正在检测 usbip 驱动状态,几秒后再点「挂载」。", "info")
                return
            if not drv:
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

    # --- 线程与事件 --------------------------------------------------
    def submit(self, label: str, fn: Callable[[], object],
               done: Optional[Callable[[object, Optional[BaseException]], None]] = None
               ) -> None:
        """把一件事排到工作线程。协议是串行的,所以所有 I/O 都走这一条队列。"""
        if self._busy:
            # 只写日志的话用户无感(尤其刚在确认框里点了「确定」的),体检 P2-1:
            # 必须在结果面板给一句,不然就是"确认了却什么都没发生"。
            self._show_result(
                "上一步还没完成(%s),等它结束后再点「%s」。"
                % (getattr(self, "_busy_label", "") or "…", label), "warn")
            self._log("上一步还没完成,先等一下(%s)" % label, "sys")
            return
        self._busy = True
        self._busy_label = label
        self.busy_var.set("%s …" % label)
        self._set_buttons(False)
        self.jobs.put((label, fn, done))

    def submit_bg(self, label: str, fn: Callable[[], object],
                  done: Optional[Callable[[object, Optional[BaseException]], None]] = None
                  ) -> None:
        """排队但**不占 busy 闸门**:给轻量周期轮询用(@FPROG 500ms 一拍)。

        与 submit() 共用同一条 worker 队列,串行 I/O 的保证不变;区别只是
        不禁按钮、不拦后续 submit —— 轮询期间用户点别的操作只会自然排队,
        不会互相踩。只准只读快命令走这里(写操作仍走 submit,防连点重入)。"""
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
                elif kind == "usbip_driver":
                    try:
                        self._on_usbip_driver(payload)
                    except Exception as exc:
                        self._log("usbip 驱动检测回调异常: %r" % (exc,), "err")
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
                elif kind == "offline_progress":
                    pct, text = payload
                    try:
                        self.off_bar["value"] = pct
                        self.off_state.set("%s %d%%" % (text, pct)
                                           if text else "%d%%" % pct)
                    except Exception:
                        pass  # 页面控件还在(常驻),到这说明快关窗了,丢弃即可
                elif kind == "job_done":
                    label, result, err, done = payload
                    self._busy = False
                    self._busy_label = ""
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
        # 禁用面 = 所有会发串口命令的按钮(_build_* 时注册进 _cmd_buttons)。
        # 只禁三个的话,busy 期间用户还能点「恢复出厂设置」然后被 submit
        # 静默吞掉 —— 禁用面必须与"会发命令"的面一致(体检 P2-1)。
        for btn in getattr(self, "_cmd_buttons", ()):
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
        # 断开就把 client 收掉(体检 P3-4):否则它还挂着 is_open=True 的死句柄,
        # 后续操作要等 write 超时才暴露;收掉后 _require_client 给出明确指引。
        self._close_client()
        self._show_result("串口已断开(%s)。\n\n点上方「自动探测」重新连接。" % reason,
                          "warn")

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
        st, _rows, _src, led, _ap = result
        self._apply_led_value(led)
        # 顺带后台拉一次镜像列表(0.9+ 固件才有;老固件静默跳过,
        # 不弹错误 —— 配网功能在老固件上必须照常可用)
        info = getattr(self.client, "info", None)
        if info and version_tuple(info.fw) >= (0, 9):
            self.submit_bg("刷新镜像列表", self._flist_job, self._after_flist)
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
        # AP 模式下顺便把密码读回来。
        # **必须在这里读,不能在 _show_ap_hint 里读**:后者由 _drain_events /
        # _after_mode 调用,两者都在 Tk 主线程上,而 @AP 是一条要等板子回话
        # 的串口命令(超时 4s)。在主线程上等 4s = 界面完全冻住,连窗口都拖
        # 不动。submit() 的注释写得很清楚:所有 I/O 都走这一条 worker 队列。
        ap = None
        if st.mode == "AP":
            try:
                ap = self.client.ap_get()
            except Exception as exc:
                ap = ApInfo(code=500, err=str(exc))
        return st, rows, source, led, ap

    def _after_state(self, result, err, keep_result: bool = False) -> None:
        if err is not None:
            self._log("读取状态失败: %s" % err, "err")
            return
        st, rows, source, led, ap = result
        self._apply_status(st)
        self._apply_led_value(led)
        if rows is not None:
            self._fill_saved(rows, source)
        # 连接成功后刚展示过"下一步怎么调试",别让例行的状态刷新把它冲掉
        if keep_result:
            return
        if st.mode == "AP":
            self._show_ap_hint(st, ap)
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
        # 只在密码框还空着时才把焦点递过去(体检 P3-5):用户正用方向键
        # 浏览列表时,无条件抢焦点会打断键盘流。
        if net.auth.upper() != "OPEN" and not self.pass_var.get():
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
            "TCP %s:%d,首包发波特率帧,例如 19200E(无空格无换行)\n"
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

    def _show_ap_hint(self, st: Status, info: Optional[ApInfo] = None) -> None:
        """AP 模式的结果面板。

        **这里只排版,不做 I/O。** 密码由调用方在 worker 线程读好传进来
        (见 _refresh_state)。本函数经 _after_state 由 _drain_events 调用,
        而那在 Tk 主线程上 —— 在这儿读串口 = 界面冻住等板子回话。

        三种情况必须分开说,因为用户要做的事完全不同:
          读到了     -> 显示密码
          404        -> 老固件没有 @AP,该升级(这才是"固件太老")
          ＞其它     -> 没读到(超时/格式错),和固件新旧无关,该重试
        绝不能把后两者都说成"请升级 v0.8.0+" —— 用户升完级还是连不上。
        """
        if info is not None and info.ok:
            pwd_line = "  密码:%s   (本机随机生成,已存在板子里)\n" % info.password
            step_line = "  1) 用电脑或手机的 WiFi 连上「%s」(密码 %s)\n" % (
                info.ssid, info.password)
            extra = ""
        elif info is not None and info.missing_command:
            pwd_line = ("  密码:这一版固件不支持 @AP,读不到随机密码。\n"
                        "        请升级到 v0.8.0+,或看板子背面的标签。\n")
            step_line = "  1) 用电脑或手机的 WiFi 连上「%s」\n" % st.ssid
            extra = ("\n> 老固件(≤0.7.x)的 AP 密码是公开常量 12345678,\n"
                     "> 强烈建议升级到 v0.8.0 使用随机密码。\n")
        else:
            # 没读到 ≠ 固件太老。把真实原因说出来,别劝人做无用功。
            reason = ("板子没回话(超时)" if (info is None or info.code == 408)
                      else "读取出错(%d %s)" % (info.code, info.err))
            pwd_line = ("  密码:这次没读到 —— %s\n"
                        "        固件可能支持 @AP,只是这一条没通过。\n" % reason)
            step_line = "  1) 用电脑或手机的 WiFi 连上「%s」\n" % st.ssid
            extra = ("\n> 密码可能要重试一次才能读到(点「AP 密码」按钮),\n"
                     "> 或者看板子背面的标签。\n")

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
        """按钮:主动读一次并发显示 AP 热点密码。

        走 submit 排队,I/O 在 worker 线程 —— 和例行刷新用同一条路径。
        """
        if self.client is None:
            self._show_result("还没连上设备", "err")
            return
        self.submit("读取 AP 密码", self.client.ap_get,
                    self._after_show_ap_password)

    def _after_show_ap_password(self, result, err) -> None:
        if err is not None:
            self._show_result("读取失败:%s" % err, "err")
            return
        info = result if isinstance(result, ApInfo) else None
        if info is None or not info.ok:
            if info is not None and info.missing_command:
                self._show_result(
                    "这块板子的固件不支持 @AP(需要 v0.8.0+)。\n\n"
                    "老固件的 AP 密码是公开常量 12345678 —— 也就是说谁都能连上\n"
                    "它的热点。建议升级固件,用随机密码。", "warn")
            else:
                # 没读到 ≠ 固件太老。把真实原因说出来,别劝人做无用功。
                why = ("板子没回话(超时)" if info is None or info.code == 408
                       else "读取出错(%d %s)" % (info.code, info.err))
                self._show_result(
                    "这次没读到 AP 密码:%s\n\n"
                    "板子能正常应答别的命令的话,固件多半是支持 @AP 的,\n"
                    "重试一次通常就好(再点一次「AP 密码」)。" % why, "err")
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
        if self._busy:
            self._show_result("上一步还没完成(%s),等它结束后再重新生成。"
                              % (getattr(self, "_busy_label", "") or "…"), "warn")
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
        info = result if isinstance(result, ApInfo) else None
        if info is None or not info.ok:
            if info is not None and info.missing_command:
                self._show_result(
                    "板子不支持 @AP REGEN。\n\n"
                    "这条命令是 v0.8.0 新增的,请升级固件后重试。", "err")
            else:
                why = ("板子没回话(超时)" if info is None or info.code == 408
                       else "出错(%d %s)" % (info.code, info.err))
                self._show_result(
                    "这次没能换密码:%s\n\n"
                    "**旧密码没有变**,板子上的密码仍是原来那个,可以放心。\n"
                    "过几秒再点一次通常就好。" % why, "err")
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
            # 滑块量程只到 20,超出的部分夹到上限显示。
            # tk.Scale 的 command 在程序赋值时同样触发;挂起标志保证这次
            # 回填不会走防抖下发(否则每次状态刷新都会多发一条 @LED)。
            self._led_suspend = True
            try:
                self.led_var.set(min(pct, 20))
                self.led_pct_var.set("%d%%" % pct)
            finally:
                self._led_suspend = False
            if self._led_timer is not None:
                try:
                    self.root.after_cancel(self._led_timer)
                except Exception:
                    pass
                self._led_timer = None
        except Exception:
            pass

    def _on_led_slide(self, _value=None) -> None:
        """滑块拖动时只更新数字,松手才发命令。

        tk.Scale 也没有"松手"事件,用 after() 做一个 350ms 的防抖:
        拖动过程中不断重置计时器,停下后才真正下发。
        """
        if getattr(self, "_led_suspend", False):
            return   # _apply_led_value 的程序回填,不是用户拖动
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
            # 这里**不**直接排版 AP 面板:面板要显示密码,而密码得走一条
            # 串口命令(@AP,超时 4s)。本函数在 Tk 主线程上跑,当场读就是
            # 冻住界面。先给一句"正在读",把读密码的活交给紧随其后的
            # _refresh_state(worker 线程),它读完会调 _show_ap_hint。
            self._show_result("已切到 AP 模式,正在读取热点信息…", "info")
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
        if self._busy:
            self._show_result("上一步还没完成(%s),等它结束后再删。"
                              % (getattr(self, "_busy_label", "") or "…"), "warn")
            return
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
        # 闸门必须在确认框之前(体检 P2-1):确认完再被 submit 静默吞掉,
        # 比不确认更糟 —— 用户会以为恢复成功了。
        if self._busy:
            self._show_result("上一步还没完成(%s),等它结束后再点「恢复出厂设置」。"
                              % (getattr(self, "_busy_label", "") or "…"), "warn")
            return
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

    def _copy_result(self) -> None:
        """复制结果面板全文(「下一步怎么做」里的命令就是要拿去用的)。"""
        text = self.result.get("1.0", "end").rstrip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._log("结果面板已复制(%d 行)" % text.count("\n"), "sys")

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
        # 超时按命令分流(体检 P3-3):@CONNECT 板上最多等 25s,其余 5s 足够 ——
        # 一刀切 32s 的话,板子不回话时 busy 要挂满 32 秒。
        to = TIMEOUT_CONNECT if line.upper().startswith("@CONNECT") else TIMEOUT_DEFAULT
        self.submit("发送命令", lambda: self.client.request(line, to),
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
                "2) 点上方「自动探测」重新连接\n\n"
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
