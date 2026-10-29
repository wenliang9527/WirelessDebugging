"""调试后端进程管理:OpenOCD / usbip / elaphureLink。

设计原则(为什么这么写):

1. **不把第三方代码编进本程序**。OpenOCD 与 usbip-win 是 GPL-2,elaphureLink
   是 BSD-2 —— 本模块只做 subprocess 调度,从不 import 它们的任何代码,因此
   本程序不是它们的衍生作品,可以闭源分发。代价是它们必须以独立目录随包提供,
   并各自附带许可证(见 tools/*/COPYING)。

2. **所有子进程必须能被收干净**。用户直接点窗口右上角关闭时,后台的
   openocd/usbip 不能变成孤儿进程 —— 否则下次启动会撞端口、usbip 还会占着
   设备不放。Gui 侧负责在退出时调用 stop_all(),本模块提供 idempotent 的 stop。

3. **只有在真的管进程时才碰 Win32**。提权用 ShellExecute runas(和现成的
   attach_dap_hidden.vbs 同一思路),不引入 pywin32 依赖 —— 保持零第三方依赖,
   这是这个工具能从源码直接跑的根基。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Callable, List, Optional

# 这些路径相对「程序根目录」解析。开发时 = tools 的上一级;
# 打包后 = exe 所在目录(PyInstaller --onedir 把 tools/ 放在 exe 旁边)。
TOOLS_DIRNAME = "tools"

# windowsair 固件的 busid 固定是 1-1,不能自动探测:固件不实现 DEVLIST,
# 跑 `usbip list -r` 会永久挂起并拖死固件 TCP 服务(2026-10-26 实测)。
USBIP_BUSID = "1-1"


def app_root() -> str:
    """程序根目录:打包后是 exe 所在目录,开发时是 tools/ 的上一级。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 开发机上第三方工具不在项目目录里,而在工具集目录下。按顺序找,第一个
# 存在的胜出。交付包不依赖这个列表 —— 打包时把 tools/ 放到 exe 旁边即可。
_FALLBACK_ROOTS = [
    r"D:\ARMGCCTool\WirelessDebuggingtool",
]


def tools_root() -> str:
    """第三方工具所在根目录。

    查找顺序:
      1. <程序根>/tools            —— 交付包结构,也是最终形态
      2. <开发机工具集目录>          —— 开发时用,避免把 300MB 第三方
                                      二进制复制进文档仓库

    判据是「目录里真的有工具」而不是「目录存在」:开发仓库里 tools/ 就是本程序
    的源码目录,它当然存在,但里面没有 openocd —— 只看存在会永远选中它。
    """
    marker = os.path.join("openocd-elaphurelink", "v0.4.0", "bin", "openocd.exe")
    candidates = [os.path.join(app_root(), TOOLS_DIRNAME)] + _FALLBACK_ROOTS
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, marker)):
            return cand
    # 一个都没命中:返回交付包位置,让 missing() 如实报缺(报错路径要可预期)
    return os.path.join(app_root(), TOOLS_DIRNAME)


# 雅特力 AT32 用的 OpenOCD。它与 openocd-elaphurelink 是两份不同的构建:
# 这份把 at32f4xx flash 驱动编译进了自己的 openocd.exe,所以只有它能烧 AT32。
# 查找顺序:交付包自带(atopenocd/,打包脚本负责放)→ 开发机固定位置。
# 注意必须用函数惰性求值:tools_root() 依赖 app_root(),而后者在冻结进程里
# import 时才定;写成模块级常量会把 import 时刻的值焊死。
_ATOPENOCD_FALLBACK = r"D:\ARMGCCTool\atopenocd"


def find_atopenocd() -> Optional[str]:
    candidates = [os.path.join(tools_root(), "atopenocd"), _ATOPENOCD_FALLBACK]
    for base in candidates:
        exe = os.path.join(base, "bin", "openocd.exe")
        if os.path.isfile(exe):
            return base
    return None


class Backend:
    """一个可启停的后端子进程(openocd)。"""

    def __init__(self, name: str, exe: str, args: List[str],
                 cwd: Optional[str] = None,
                 on_line: Optional[Callable[[str], None]] = None):
        self.name = name
        self.exe = exe
        self.args = args
        self.cwd = cwd
        self.on_line = on_line
        self.proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stopping = False

    # --- 状态 ---
    def running(self) -> bool:
        with self._lock:
            return self.proc is not None and self.proc.poll() is None

    def missing(self) -> bool:
        return not os.path.isfile(self.exe)

    # --- 启停 ---
    def start(self) -> None:
        """启动。已在跑则什么都不做(幂等)。"""
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self._stopping = False
            flags = 0
            if sys.platform == "win32":
                # 不给子进程分配控制台窗口 —— 否则会弹黑框。
                # 用 CREATE_NO_WINDOW 而不是 DETACHED_PROCESS:后者会让
                # stdout 管道行为异常,拿不到日志。
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.proc = subprocess.Popen(
                [self.exe] + self.args,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
                bufsize=1,
                universal_newlines=True,
                errors="replace",
            )
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name="pump-%s" % self.name)
        self._reader.start()

    def _pump(self) -> None:
        """把子进程输出逐行转给回调。on_line 必须线程安全(通常是投队列)。"""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                if self._stopping:
                    break
                if self.on_line:
                    try:
                        self.on_line(line.rstrip("\r\n"))
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def stop(self, timeout: float = 5.0) -> bool:
        """停止。返回 True 表示现在确实不在跑了。幂等。"""
        with self._lock:
            proc = self.proc
            self._stopping = True
        if proc is None:
            return True
        if proc.poll() is not None:
            with self._lock:
                self.proc = None
            return True
        try:
            if sys.platform == "win32":
                # OpenOCD 没有优雅退出信号(控制台程序收不到 WM_CLOSE),
                # terminate() = TerminateProcess。能接受:它不写持久状态,
                # 目标板的调试状态由目标板自己保持。
                proc.terminate()
            else:
                proc.terminate()
            proc.wait(timeout=timeout)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                return False
        with self._lock:
            self.proc = None
        return True


class OpenOcdBackend(Backend):
    """OpenOCD。两种接口形态,对应两条完全不同的路:

      elaphurelink  : TCP 直连,免驱动。但没有 AT32 flash 驱动。
      usbip         : 桥接成本地 USB,吃任何标准 cmsis-dap。
                      AT32 烧录必须走这条 —— 雅特力的 fork(atopenocd)
                      把 at32f4xx flash 驱动编进了自己的 openocd.exe,
                      上游版本没有,cfg 脚本也补不出来。

    为什么合成一个类:除了 exe 路径和 interface 参数,两者行为一致
    (起进程、抓日志、嗅探 gdb 端口),分成两个类只会复制粘贴。
    """

    PORT_GDB = 3333

    # elaphurelink 那份的安装位置(相对 tools_root())
    REL = os.path.join("openocd-elaphurelink", "v0.4.0")

    def __init__(self, device_addr: str, target_cfg: str = "target/stm32f1x.cfg",
                 on_line: Optional[Callable[[str], None]] = None,
                 flavor: str = "elaphurelink",
                 scripts_dir: Optional[str] = None):
        self.flavor = flavor
        self.target_cfg = target_cfg
        self.device_addr = device_addr
        self.scripts_dir = scripts_dir
        self._saw_gdb_port = False
        self._user_on_line: Optional[Callable[[str], None]] = on_line

        if flavor == "usbip":
            # 雅特力 fork:走本地 USB(usbip 挂载出来的 CMSIS-DAP),
            # 自带 at32f4xx flash 驱动。exe 路径由调用方给,因为它可能
            # 装在机器上任何位置(用户已有的 ocdup 环境里就有一份)。
            exe = device_addr          # 这个形态下第一个参数借用为 exe 路径
            args = ["-f", "interface/cmsis-dap.cfg"]
            if scripts_dir:
                args += ["-s", scripts_dir]
            args += ["-f", target_cfg]
            cwd = os.path.dirname(exe) or None
        else:
            root = os.path.join(tools_root(), self.REL)
            exe = os.path.join(root, "bin", "openocd.exe")
            args = [
                "-f", "interface/elaphurelink.cfg",
                # 地址写在这里而不是改 cfg 文件:cfg 是上游文件,改了以后升级冲突
                "-c", "cmsis-dap elaphurelink addr %s" % device_addr,
                # 显式指定 transport,消掉上游那条 DEPRECATED 警告
                "-c", "transport select swd",
                "-f", target_cfg,
            ]
            cwd = root

        super().__init__("openocd", exe, args, cwd=cwd, on_line=on_line)

    def _pump_wrap(self, line: str) -> None:
        if str(self.PORT_GDB) in line and "Listening" in line:
            self._saw_gdb_port = True
        if self._user_on_line:
            self._user_on_line(line)

    def start(self) -> None:
        """启动前把原始回调存起来,换成会嗅探 gdb 端口的包装。

        不能把包装值写回 self.on_line 再在包装里调 self.on_line —— 那会
        自己调自己,首行就无限递归。
        """
        self._saw_gdb_port = False
        self._user_on_line = self.on_line
        self.on_line = self._pump_wrap
        super().start()

    def gdb_ready(self) -> bool:
        """gdb 端口是否已开 = 目标芯片握手成功。"""
        return self._saw_gdb_port

    def command_line(self) -> str:
        """给人看/复制的完整命令(VSCode cortex-debug 那种场景要用)。"""
        return '"%s" %s' % (self.exe, " ".join(
            ('"%s"' % a if " " in a else a) for a in self.args))

    def list_targets(self) -> List[str]:
        """列出可用的 target cfg(去掉 .cfg 后缀)。"""
        if self.scripts_dir:
            d = os.path.join(self.scripts_dir, "target")
        else:
            d = os.path.join(tools_root(), self.REL, "share", "openocd",
                             "scripts", "target")
        try:
            return sorted(f[:-4] for f in os.listdir(d) if f.endswith(".cfg"))
        except Exception:
            return []


class UsbipBackend:
    """usbip vhci 桥接。与 openocd 不同:它是「一次动作」不是「常驻服务」。

    为什么单独建类而不是复用 Backend:attach 的进程确实会常驻(实测 0.3.6
    UDE 下如此),但它的生命周期语义是「挂着就别关」,而且必须提权、必须能
    detach。硬套 Backend 会让 stop 的语义变得含糊。
    """

    REL = os.path.join("usbip-win", "v0.3.6-dev")

    def __init__(self, device_addr: str):
        self.root = os.path.join(tools_root(), self.REL)
        self.exe = os.path.join(self.root, "usbip.exe")
        self.device_addr = device_addr
        self._attach_proc: Optional[subprocess.Popen] = None

    def missing(self) -> bool:
        return not os.path.isfile(self.exe)

    def reachable(self, timeout: float = 3.0) -> tuple:
        """设备 TCP 端口是否可达。返回 (ok, message)。

        为什么必须在挂载前查:usbip attach 连不上时会长时间卡住然后报一个
        很难懂的错,用户完全不知道原因。最常见的原因是 PC 和 DAP 板不在同一
        网段(DAP 连着 192.168.1.x,PC 在 192.168.7.x)—— 这种错误必须在
        这里拦下来并说清楚,而不是让 usbip 自己超时。

        用 TCP 而不是 ping:Windows 防火墙默认拦 ICMP,ping 不通不代表端口
        不通;而 3240 才是真正要用的端口。

        mDNS 的坑:解析出来的可能是**过期缓存**。实测板子从 192.168.7.125
        漂到 192.168.7.105 后,dap.local 仍返回 .125 而那个地址已经没人应答。
        所以成功时把实际测通的 IP 报出来 —— 用户看到 IP 和预期不符就知道
        是缓存问题,而不是以为"网络坏了"。
        """
        if not self.device_addr:
            return False, "没有设备地址"
        # mDNS 名字先解析成 IP,后面的错误信息才有用
        import socket
        try:
            ip = socket.gethostbyname(self.device_addr)
        except Exception:
            return False, ("解析不出 %s。检查设备是否在线,或者把地址换成 IP"
                           % self.device_addr)

        for port, label in ((3240, "usbip"), (1234, "串口桥")):
            s = socket.socket()
            s.settimeout(timeout)
            try:
                s.connect((ip, port))
                s.close()
                if self.device_addr != ip:
                    return True, "%s(%s):%d 可达" % (self.device_addr, ip, port)
                return True, "%s:%d 可达" % (ip, port)
            except Exception:
                pass
            finally:
                try:
                    s.close()
                except Exception:
                    pass

        return False, (
            "%s(%s)上的端口都连不上(3240/1234)。\n\n"
            "两个最常见的原因:\n"
            "  ① 设备换了网络,但 mDNS 还缓存着旧地址\n"
            "     —— 实测板子从 192.168.7.x 漂到 192.168.1.x 后,dap.local\n"
            "        仍返回旧 IP,而那个地址已经没人应答\n"
            "  ② PC 和设备不在同一网段,或路由器开了客户端隔离\n\n"
            "排查:\n"
            "  1. 在「配网」页看设备现在连的是哪个 WiFi、拿到什么 IP\n"
            "  2. 如果 IP 变了,把上面的「设备地址」改成新 IP(或 IP 直填)\n"
            "  3. 确认 PC 和那个 IP 在同一网段\n"
            "  4. 有些路由器开了「客户端隔离」,同一 SSID 下设备之间也不通"
            % (self.device_addr, ip))

    def driver_installed(self) -> bool:
        """vhci 驱动是否已装。用 pnputil 查,避免依赖 devcon。

        注意编码:pnputil 在中文 Windows 上输出的是 GBK 不是 UTF-8。指定
        encoding="utf-8" 会抛 UnicodeDecodeError;让 subprocess 返回 bytes
        再由我们自己容错解码,中文/英文系统都不会炸。
        """
        if sys.platform != "win32":
            return False
        try:
            res = subprocess.run(
                ["pnputil", "/enum-drivers"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return False
        raw = res.stdout or b""
        text = ""
        for enc in ("utf-8", "gbk", "cp936", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        low = text.lower()
        return "usbip" in low or "vhci" in low

    def attach(self) -> tuple:
        """提权挂载。返回 (ok, message)。

        为什么要提权:vhci 驱动是内核模式的,attach 要写设备节点。
        用 ShellExecuteW + runas 而不是 subprocess:只有 Shell 这一层能弹 UAC。
        """
        if self.missing():
            return False, "usbip.exe 不存在:%s" % self.exe
        if sys.platform != "win32":
            return False, "仅支持 Windows"
        import ctypes
        # SW_SHOWNORMAL=1;要完全无窗得走 vbs,但那会让错误信息拿不到,
        # 首次使用时留个小窗反而有助于排错。
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "cmd.exe", '/c cd /d "%s" && usbip.exe attach -r %s -b %s'
            % (self.root, self.device_addr, USBIP_BUSID), None, 1)
        if rc <= 32:
            # 常见:5 = 用户点了「否」取消了 UAC
            if rc == 5:
                return False, "已取消(需要管理员权限才能挂载)"
            return False, "提权失败(ShellExecute 返回 %d)" % rc
        return True, "挂载命令已发出,请等待设备管理器出现 CMSIS-DAP"

    def detach_all(self) -> tuple:
        """卸载所有已挂端口。需要管理员,同样提权。"""
        if self.missing():
            return False, "usbip.exe 不存在"
        if sys.platform != "win32":
            return False, "仅支持 Windows"
        # 不先 port 查再 detach:port 也要管理员,两次提权很烦。
        # detach -p 0 在没挂载时会报错但无害;这里直接对 0..3 各发一次。
        script = ("cd /d \"%s\" && " % self.root) + " & ".join(
            "usbip.exe detach -p %d" % i for i in range(4))
        import ctypes
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "cmd.exe", "/c " + script, None, 1)
        if rc <= 32:
            if rc == 5:
                return False, "已取消"
            return False, "提权失败(%d)" % rc
        return True, "卸载命令已发出"

    def command_line(self) -> str:
        return "usbip.exe attach -r %s -b %s" % (self.device_addr, USBIP_BUSID)


class ElaphureLinkBackend:
    """elaphureLink(Keil 用)。它没有命令行模式,只能起 GUI 进程。

    为什么不自己实现代理:elaphureLink 的 TCP 代理协议是公开的(上游
    docs/proxy_protocol.md),但简版代理 + 往 Keil 装 AGDI 驱动比直接调它
    麻烦得多。这里只做「按需启停」,窗口由它自己管 —— 属于路线 A
    (不碰它的代码,只调度进程)。
    """

    REL = os.path.join("elaphureLink")
    EXE = "elaphureLink.Wpf.exe"
    CFG = "elaphureLink.Wpf.exe.config"

    def __init__(self, device_addr: str):
        self.root = os.path.join(tools_root(), self.REL)
        self.exe = os.path.join(self.root, self.EXE)
        self.cfg = os.path.join(self.root, self.CFG)
        self.device_addr = device_addr
        self._proc: Optional[subprocess.Popen] = None

    def missing(self) -> bool:
        return not os.path.isfile(self.exe)

    def write_address(self) -> tuple:
        """把设备地址写进它的 userSettings.deviceAddress。

        实测:这个键是普通 XML 文本,改完下次启动就生效。这是唯一能做到
        「用户不用手填地址」的地方 —— 否则每换一个网络都得进它的 GUI 改。
        """
        if not os.path.isfile(self.cfg):
            return False, "配置文件不存在:%s" % self.cfg
        try:
            with open(self.cfg, "r", encoding="utf-8-sig") as f:
                text = f.read()
            import re
            pat = re.compile(
                r'(<setting name="deviceAddress"[^>]*>\s*<value>)([^<]*)(</value>)')
            if not pat.search(text):
                return False, "配置里找不到 deviceAddress(版本可能不一致)"
            new = pat.sub(lambda m: m.group(1) + self.device_addr + m.group(3), text)
            if new == text:
                return True, "地址已是 %s,无需修改" % self.device_addr
            # 直写同一个文件:这是用户目录外的程序文件,失败会抛异常 → 报错给用户
            with open(self.cfg, "w", encoding="utf-8-sig") as f:
                f.write(new)
            return True, "已写入地址 %s" % self.device_addr
        except Exception as exc:
            return False, "写入失败:%s" % exc

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> tuple:
        if self.missing():
            return False, "elaphureLink 不存在:%s" % self.exe
        if self.running():
            return True, "已在运行"
        ok, msg = self.write_address()
        try:
            self._proc = subprocess.Popen([self.exe], cwd=self.root)
        except Exception as exc:
            return False, "启动失败:%s" % exc
        return True, "已启动(%s)" % msg if ok else "已启动,但%s" % msg

    def stop(self) -> tuple:
        if not self.running():
            self._proc = None
            return True, "未在运行"
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                return False, "无法结束进程"
        self._proc = None
        return True, "已停止"


class BackendManager:
    """把四个后端收在一起,提供统一的 stop_all —— 退出时务必调用。

    四个而不是三个:OpenOCD 有两种互斥的接口形态,它们不是同一个东西:

      openocd       elaphurelink TCP 直连,免驱动,但没有 AT32 flash 驱动
      at32_openocd  usbip 桥接 + 雅特力 fork,唯一能烧 AT32 的
    """

    def __init__(self, device_addr: str = "dap.local",
                 on_line: Optional[Callable[[str], None]] = None):
        self.device_addr = device_addr
        self.openocd = OpenOcdBackend(device_addr, on_line=on_line)
        self.usbip = UsbipBackend(device_addr)
        self.elaphurelink = ElaphureLinkBackend(device_addr)
        self.at32_openocd = self._make_at32_backend(on_line)
        self._on_line = on_line

    def _make_at32_backend(self, on_line) -> Optional[OpenOcdBackend]:
        """构建 AT32 烧录用的后端。找不到 atopenocd 就返回 None。"""
        base = find_atopenocd()
        if not base:
            return None
        exe = os.path.join(base, "bin", "openocd.exe")
        scripts = os.path.join(base, "scripts")
        return OpenOcdBackend(
            exe,                       # usbip 形态下借用首参传 exe 路径
            "target/at32f403axG.cfg",  # 默认值,可被 UI 改
            on_line=on_line,
            flavor="usbip",
            scripts_dir=scripts,
        )

    def has_at32(self) -> bool:
        return self.at32_openocd is not None

    def set_addr(self, addr: str) -> None:
        """设备地址变了(换网络后),带地址的后端都要跟着换。"""
        self.device_addr = addr
        if self.openocd.running():
            self.openocd.stop()
        self.openocd = OpenOcdBackend(addr, self.openocd.target_cfg,
                                      on_line=self._on_line)
        self.usbip.device_addr = addr
        self.elaphurelink.device_addr = addr
        # at32 那个走本地 USB,不吃地址 —— 它依赖的是 usbip 挂载结果

    def set_at32_target(self, target_cfg: str) -> None:
        """换 AT32 的目标芯片 cfg。运行中的要先停。"""
        if self.at32_openocd is None:
            return
        if self.at32_openocd.running():
            self.at32_openocd.stop()
        base = find_atopenocd()
        if not base:
            self.at32_openocd = None
            return
        self.at32_openocd = OpenOcdBackend(
            os.path.join(base, "bin", "openocd.exe"),
            target_cfg,
            on_line=self._on_line,
            flavor="usbip",
            scripts_dir=os.path.join(base, "scripts"),
        )

    def stop_all(self) -> None:
        """退出时调用。每个都幂等,重复调用安全。"""
        for be in (self.openocd, self.at32_openocd):
            if be is None:
                continue
            try:
                be.stop(timeout=3.0)
            except Exception:
                pass
        try:
            self.elaphurelink.stop()
        except Exception:
            pass
        # usbip 不在这里 detach:detach 需要提权,退出时弹 UAC 很讨厌。
        # 而且 usbip attach 进程随系统重启自然消失。文档里写明即可。
