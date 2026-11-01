"""
一键打包交付目录。

用法:
    python build_package.py            # 打包到 dist/无线调试器/
    python build_package.py --outdir X # 指定输出目录

产物结构(见 00_preparation/产品化方案-交付形态与桌面集成.md §3.2):
    无线调试器/
    ├─ 无线调试器.exe              PyInstaller --onedir 的启动 exe(_internal/ 在旁)
    ├─ tools/
    │  ├─ elaphureLink/            BSD-2(Keil 插件,免驱动)
    │  ├─ openocd-elaphurelink/v0.4.0/   GPL-2(独立进程)
    │  ├─ usbip-win/v0.3.6-dev/    GPL-3.0(独立进程)
    │  └─ atopenocd/               GPL-2(雅特力 fork,含 AT32 flash 驱动)
    ├─ firmware/                   自家固件(MIT)+ 烧录说明
    ├─ 快速上手.md
    └─ 许可与开源声明.txt

为什么 exe 放在根目录而工具在 tools/:dap_backends.app_root() 冻结后取
exe 所在目录,tools_root() 找 <exe目录>/tools —— 打包脚本按这个契约组装。

GPL 组件一律整目录复制 + 附 COPYING 与源码获取说明,不与自家代码混排;
flash_download_tool 是乐鑫专有软件,绝不入包。
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))          # .../tools(本仓库)
REPO = os.path.dirname(HERE)                               # 文档仓库根

# 开发机上的第三方工具源(交付脚本只在开发机跑,写死绝对路径是可接受的)
SRC_TOOLS_ROOT = r"D:\ARMGCCTool\WirelessDebuggingtool"
SRC_ATOPENOCD = r"D:\ARMGCCTool\atopenocd"
SRC_FIRMWARE = os.path.join(SRC_TOOLS_ROOT, "firmware-src",
                            "wireless-esp8266-dap", "build")
SRC_FIRMWARE_DOCS = os.path.join(REPO, "03_firmware", "固件烧录指南.md")
SRC_MANUAL = os.path.join(REPO, "04_user", "用户手册.md")

# 每个第三方目录里必须带的说明文件(名字对用户友好,内容指回上游)
SOURCES = {
    "openocd-elaphurelink": (
        "openocd-elaphurelink v0.4.0",
        "https://github.com/windowsair/elaphureLink/releases",
        "GPL-2.0",
    ),
    "usbip-win": (
        "usbip-win 0.3.6-dev",
        "https://github.com/cezanne/usbip-win",
        # 2026-10-29 修正:GitHub API license.spdx_id = GPL-3.0,不是 2.0。
        # 此前全项目(含本文档、dap_backends.py、用户手册)误标 GPL-2。
        "GPL-3.0",
    ),
    "atopenocd": (
        "openocd 0.11.0+dev (Artery AT32 fork)",
        "https://gitee.com/amoxu/openocd-at32 (branch at32-dev)",
        "GPL-2.0",
    ),
    "elaphureLink": (
        "elaphureLink 1.4.0",
        "https://github.com/windowsair/elaphureLink/releases",
        "BSD-2-Clause",
    ),
}


def copytree(src: str, dst: str, skip_pdb: bool = False) -> None:
    """整目录复制。skip_pdb 时丢掉 .pdb(调试符号,占体积且用户用不上)。"""
    if skip_pdb:
        shutil.copytree(src, dst,
                        ignore=shutil.ignore_patterns("*.pdb", "*.zip"))
    else:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.zip"))


# 许可证全文内置在本仓库 tools/licenses/ 下,打包时复制到各组件目录。
# 为什么不从组件源码树里捞:实测四棵树(usbip-win/openocd-elaphurelink/
# elaphureLink/atopenocd)的发行包里**都没有** COPYING/LICENSE 文件,
# 原实现找不到就静默跳过 —— 交付包只有"源码获取.txt",没有许可证全文,
# 与文档承诺的"每个 GPL 目录附 COPYING"不符,对外分发是实质合规缺口。
LICENSE_DIR = os.path.join(HERE, "licenses")
LICENSE_BASENAMES = ("COPYING", "COPYING.txt", "LICENSE", "LICENSE.txt")


def write_tool_notice(tools_dir: str) -> list:
    """每个第三方目录放 源码获取.txt + 许可证全文。返回缺失告警列表。

    GPL 义务:分发二进制必须(a)告知源码获取方式,(b)随附许可证全文。
    缺任何一项都要**显式报错**,不能静默跳过 —— 静默跳过正是上一版的 bug。
    """
    warnings = []
    for name, (title, url, lic) in SOURCES.items():
        d = os.path.join(tools_dir, name)
        if not os.path.isdir(d):
            warnings.append("组件目录不存在,未写声明:%s" % name)
            continue
        with open(os.path.join(d, "源码获取.txt"), "w", encoding="utf-8") as f:
            f.write(
                "%s\n许可证:%s\n上游源码:%s\n\n"
                "本组件以独立进程方式被「无线调试器.exe」调用,未与其代码链接。\n"
                "按许可证要求随包提供获取源码的方式(如上链接)。\n" % (title, lic, url))

        # 许可证全文:先看组件目录里有没有,没有就从仓库内置副本复制。
        has_copying = any(
            os.path.isfile(os.path.join(d, n)) for n in LICENSE_BASENAMES)
        if not has_copying:
            src = None
            for n in LICENSE_BASENAMES:
                cand = os.path.join(LICENSE_DIR, "%s.%s" % (lic, n))
                if os.path.isfile(cand):
                    src = cand
                    break
            # GPL-3.0 允许以 GPL-2.0 的全文兜底?不行,版本不同条款不同。
            # 这里严格按组件声明的 lic 找;找不到就记告警由 smoke_check 拦。
            if src:
                shutil.copyfile(src, os.path.join(d, "COPYING"))
            else:
                warnings.append(
                    "%s 缺 %s 许可证全文(请放到 tools/licenses/%s.COPYING)"
                    % (name, lic, lic))
    return warnings


LICENSE_SUMMARY = """\
许可与开源声明
==============

本软件(「无线调试器」配网/调试工具,下称"本工具")与固件均以 MIT 许可
发布(全文见源码仓库 LICENSE 与交付包 firmware/LICENSE):允许使用、复制、
修改、合并、再分发与商用,唯一义务是在副本中保留版权与许可声明。

本工具按「独立进程」方式调用以下第三方组件,不与其代码链接:

  组件                      许可证      用途
  ------------------------  ----------  ----------------------
  elaphureLink              BSD-2      Keil 无线调试插件(免驱动)
  openocd-elaphurelink      GPL-2      OpenOCD(STM32 无线 SWD)
  usbip-win                 GPL-3.0    USB over IP 桥接
  atopenocd                 GPL-2      OpenOCD 雅特力 fork(AT32 烧录)

各组件的许可证全文见 tools/<组件>/ 目录(COPYING 或 源码获取.txt)。
GPL-2.0 / GPL-3.0 组件以独立进程运行、经标准接口(命令行/网络)与本工具
通信,依据 GPL 与 FSF 惯例,该使用方式不使本工具成为衍生作品。

固件基于 windowsair/esp8266-dap(MIT),经二次开发,版权声明见固件包内
LICENSE 文件。乐鑫 flash_download_tool 为乐鑫专有软件,本包不含;
如需使用请自行从乐鑫官网下载。

免责声明:本软件按"现状"提供,不附带任何明示或默示的担保。使用本工具
调试硬件时请注意保存目标板数据,因烧录/擦除操作造成的数据丢失概不负责。
"""


def build_exe(outdir: str) -> str:
    """PyInstaller --onedir 打包 dap_config.py,返回 onedir 目录。"""
    name = "无线调试器"
    work = os.path.join(outdir, "_build")
    dist = os.path.join(outdir, "_dist")
    for d in (work, dist):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", name,
        "--distpath", dist, "--workpath", work, "--specpath", work,
        # console 保留:--raw/--selftest 都要从命令行走,而且报错能看见
        "--console",
        "--hidden-import", "dap_backends",
        os.path.join(HERE, "dap_config.py"),
    ]
    print("[1/4] PyInstaller 打包中…")
    subprocess.run(cmd, check=True, cwd=HERE)
    src = os.path.join(dist, name)
    # _internal(PyInstaller 6 的运行时目录)整体搬进交付目录
    dst = os.path.join(outdir, name)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.move(src, dst)
    return dst


def assemble(pkg_root: str) -> None:
    tools = os.path.join(pkg_root, "tools")
    # 重跑必须从干净状态开始:copytree 目标已存在会直接抛错
    shutil.rmtree(tools, ignore_errors=True)
    for d in ("firmware",):
        shutil.rmtree(os.path.join(pkg_root, d), ignore_errors=True)
    os.makedirs(tools, exist_ok=True)
    print("[2/4] 收集第三方工具…")
    copytree(os.path.join(SRC_TOOLS_ROOT, "openocd-elaphurelink", "v0.4.0"),
             os.path.join(tools, "openocd-elaphurelink", "v0.4.0"))
    copytree(os.path.join(SRC_TOOLS_ROOT, "usbip-win", "v0.3.6-dev"),
             os.path.join(tools, "usbip-win", "v0.3.6-dev"))
    copytree(os.path.join(SRC_TOOLS_ROOT, "elaphureLink"),
             os.path.join(tools, "elaphureLink"), skip_pdb=True)
    copytree(SRC_ATOPENOCD, os.path.join(tools, "atopenocd"),
             skip_pdb=True)   # 21MB,整树(含 scripts/ 的 349 个 target)

    # usbip-win 的装/卸驱动脚本必须和 v0.3.6-dev/ 同级:
    # 脚本用 "%~dp0v0.3.6-dev" 相对定位 usbip.exe。界面/文档让用户
    # 「打开 <usbip.root> → 右键 install_driver.bat」,而
    # UsbipBackend.root 指的正是 tools/usbip-win/(见 dap_backends.py REL)。
    # 原实现只复制了 v0.3.6-dev 子目录,交付包里根本没有这两个 bat。
    src_scripts = os.path.join(HERE, "usbip_scripts")
    if not os.path.isdir(src_scripts):
        src_scripts = os.path.join(SRC_TOOLS_ROOT, "usbip-win")  # 开发机回退
    for bat in ("install_driver.bat", "uninstall_driver.bat"):
        s = os.path.join(src_scripts, bat)
        if os.path.isfile(s):
            shutil.copyfile(s, os.path.join(tools, "usbip-win", bat))
    print("[3/4] 写许可与源码获取说明…")
    warn = write_tool_notice(tools)
    for w in warn:
        print("  [警告] %s" % w)

    print("[3/4] 固件与文档…")
    fw = os.path.join(pkg_root, "firmware")
    os.makedirs(fw, exist_ok=True)
    # IDF 的 build 布局:app 在 build/ 根,bootloader/partition 在各自子目录
    for rel in ("wireless_esp_dap.bin",
                os.path.join("bootloader", "bootloader.bin"),
                os.path.join("partition_table", "partition-table.bin")):
        shutil.copyfile(os.path.join(SRC_FIRMWARE, rel),
                        os.path.join(fw, os.path.basename(rel)))
    # 固件是 MIT(继承 windowsair),MIT 的唯一义务就是"副本里保留版权声明
    # 和许可声明"。LICENSE_SUMMARY 也写着"版权声明见固件包内 LICENSE 文件",
    # 所以这份必须真的进包 —— 原实现漏了,交付包里的 firmware/ 没有 LICENSE。
    fw_license = os.path.join(SRC_FIRMWARE, "..", "LICENSE")
    fw_license = os.path.normpath(fw_license)
    if os.path.isfile(fw_license):
        shutil.copyfile(fw_license, os.path.join(fw, "LICENSE"))
    else:
        print("  [警告] 找不到固件 LICENSE:%s" % fw_license)
    shutil.copyfile(SRC_FIRMWARE_DOCS, os.path.join(fw, "固件烧录指南.md"))
    shutil.copyfile(SRC_MANUAL, os.path.join(pkg_root, "快速上手.md"))
    with open(os.path.join(pkg_root, "许可与开源声明.txt"), "w",
              encoding="utf-8") as f:
        f.write(LICENSE_SUMMARY)

    readme = os.path.join(pkg_root, "README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "无线调试器\n"
            "==========\n\n"
            "1. 双击「无线调试器.exe」,USB 线插板子上标 USB 的 native 口\n"
            "2. 按界面提示选 WiFi 填密码(详细步骤见「快速上手.md」)\n"
            "3. 调试接入在「调试接入」页:Keil / OpenOCD / AT32 烧录三张卡片\n\n"
            "首次运行 Windows 可能弹 SmartScreen:点「更多信息」→「仍要运行」。\n"
            "如果杀毒软件拦截,添加信任即可(工具不联网上传任何数据)。\n")


def _builtin_firmware_literals(main_dir: str) -> set:
    """固件源码里**本来就有**的字符串字面量(不含 wifi_credentials.h)。

    用来区分"用户自己的 WiFi 凭据"和"固件内置常量":
      AP 热点密码 12345678 写在 wifi_handle.c 的 #define 里,
      mDNS / 版本横幅里的 "dap"、"DAP"、"OTA" 同理。
    这些出现在 bin 里是正常的,不能报成凭据泄露。
    """
    import re as _re
    lits = set()
    try:
        names = os.listdir(main_dir)
    except Exception:
        return lits
    for name in names:
        if not name.endswith((".c", ".h")) or name == "wifi_credentials.h":
            continue
        try:
            text = open(os.path.join(main_dir, name), encoding="utf-8",
                        errors="replace").read()
        except Exception:
            continue
        for m in _re.finditer(r'"([^"\\\n]{3,})"', text):
            lits.add(m.group(1))
    return lits


def is_builtin_literal(s: str, builtin: set) -> bool:
    """s 是否其实是固件自带的东西(内置常量,不是用户私有凭据)。

    提成模块级函数而不是 check_no_credentials 里的闭包,是为了让
    --selftest 能**直接调用被测逻辑**。上一版把判据抄进测试里,
    把 bug 注入回去测试照样全绿 —— 假绿。现在测试调用本函数。

    判据是**单向**的:s 必须是某个内置字面量的子串。
      "DAP"      ⊂ "DAP-"          -> 内置
      "OTA"      ⊂ "corsacOTA"     -> 内置
      "12345678" == AP_PASSWORD    -> 内置
    反过来(b 是 s 的子串)绝对不行:那会让 "dap_my_router" 因为含 "dap"、
    "password123" 因为含 "pass" 被当成内置常量而**漏报** —— 这恰恰是
    这个检查最该抓住的那类私有凭据(用户起名常带 dap/wifi/password)。
    """
    if s in builtin:
        return True
    return any(s in b for b in builtin if len(b) >= 3)


def check_no_credentials(fw_bin: str, secrets=None) -> list:
    """确认要发出去的固件 bin 里**不含**本机私有 WiFi 凭据。

    为什么必须有这一步:main/wifi_credentials.h 是 gitignore 的本地文件,
    编译时经 __has_include 编进 bin。开发机上一次"带着自己家 WiFi 编"的
    产物会被 assemble() 原样抓进交付包 —— 那就等于把家里的 SSID/密码
    随软件发给所有人。这类事故无法靠 code review 发现,只能机器校验。

    判据:凭据文件里的每个 SSID/密码,若**不在**固件其余源码的字面量集合里
    (即它是这个文件独有的,也就是用户私有的),就不许出现在 bin 里。
    这样 12345678(AP 热点密码)、DAP/OTA(内置常量)不会被误报。

    secrets:仅供自测注入假的凭据列表;为 None 时从 wifi_credentials.h 读。
    有这个参数是为了让 --selftest 能走**真实代码路径**验判据 —— 把判据
    抄进测试里就是假绿。

    返回问题列表(空 = 干净)。找不到凭据文件时跳过(交付机上通常没有)。
    """
    problems = []
    main_dir = os.path.normpath(os.path.join(SRC_FIRMWARE, "..", "main"))
    cred = os.path.join(main_dir, "wifi_credentials.h")
    if not os.path.isfile(fw_bin):
        return problems
    if secrets is None:
        if not os.path.isfile(cred):
            return problems
        try:
            text = open(cred, encoding="utf-8", errors="replace").read()
        except Exception:
            return problems
        import re as _re
        secrets = []
        for m in _re.finditer(r'\.ssid\s*=\s*"([^"]*)"', text):
            secrets.append(m.group(1))
        for m in _re.finditer(r'\.password\s*=\s*"([^"]*)"', text):
            secrets.append(m.group(1))
    builtin = _builtin_firmware_literals(main_dir)
    blob = open(fw_bin, "rb").read()

    for s in secrets:
        if len(s) < 3 or is_builtin_literal(s, builtin):
            continue      # 太短 / 固件内置常量,不是私有凭据
        if s.encode("utf-8") in blob:
            problems.append(
                "固件 bin 里含本机私有凭据 %r —— 请先删掉 main/wifi_credentials.h "
                "重新编译再打包(否则会随交付包泄露)" % s)
    return problems


def smoke_check(pkg_root: str) -> None:
    """验证打包产物与 dap_backends 的查找契约吻合。"""
    print("[4/4] 结构自检…")
    exe = os.path.join(pkg_root, "无线调试器.exe")
    problems = []
    if not os.path.isfile(exe):
        problems.append("缺 exe:" + exe)
    marker = os.path.join(pkg_root, "tools", "openocd-elaphurelink", "v0.4.0",
                          "bin", "openocd.exe")
    if not os.path.isfile(marker):
        problems.append("缺 openocd:" + marker)
    for rel in (os.path.join("usbip-win", "v0.3.6-dev", "usbip.exe"),
                os.path.join("elaphureLink", "elaphureLink.Wpf.exe"),
                os.path.join("atopenocd", "bin", "openocd.exe"),
                os.path.join("atopenocd", "scripts", "target")):
        p = os.path.join(pkg_root, "tools", rel)
        if not os.path.exists(p):
            problems.append("缺 " + rel)
    for f in ("bootloader.bin", "wireless_esp_dap.bin"):
        if not os.path.isfile(os.path.join(pkg_root, "firmware", f)):
            problems.append("缺 firmware/" + f)
    # 固件 MIT 的版权声明必须随包(见 assemble 里的说明)
    if not os.path.isfile(os.path.join(pkg_root, "firmware", "LICENSE")):
        problems.append("缺 firmware/LICENSE(固件 MIT 版权声明)")
    for f in ("快速上手.md", "许可与开源声明.txt", "README.txt"):
        if not os.path.isfile(os.path.join(pkg_root, f)):
            problems.append("缺 " + f)
    # usbip 装驱动脚本必须在 <pkg>/tools/usbip-win/ 下(与 v0.3.6-dev 同级)。
    # 界面直接让用户"打开这个目录 → 右键 install_driver.bat",少了它
    # AT32 烧录的前置步骤直接断掉。
    for f in ("install_driver.bat", "uninstall_driver.bat"):
        p = os.path.join(pkg_root, "tools", "usbip-win", f)
        if not os.path.isfile(p):
            problems.append("缺 tools/usbip-win/" + f)
    for name, (_t, _u, lic) in SOURCES.items():
        n = os.path.join(pkg_root, "tools", name, "源码获取.txt")
        if not os.path.isfile(n):
            problems.append("缺 tools/%s/源码获取.txt" % name)
        # GPL 组件必须带许可证全文;BSD-2 的短声明也一并复制,统一检查
        if lic.startswith("GPL"):
            c = os.path.join(pkg_root, "tools", name, "COPYING")
            if not os.path.isfile(c):
                problems.append("缺 tools/%s/COPYING(%s 全文)" % (name, lic))
            else:
                # 防止拿错版本:GPL-2 全文里有 "Version 2",GPL-3 有 "Version 3"
                head = open(c, encoding="utf-8", errors="replace").read(2048)
                want = "Version 3" if lic == "GPL-3.0" else "Version 2"
                if want not in head:
                    problems.append(
                        "tools/%s/COPYING 版本不符(应为 %s)" % (name, lic))
    # 最后一道:不许把本机私有 WiFi 凭据随包发出去(见 check_no_credentials)。
    # 和上面的结构检查合并成一次报错,免得用户改一处跑一次。
    problems += check_no_credentials(
        os.path.join(pkg_root, "firmware", "wireless_esp_dap.bin"))
    if problems:
        for p in problems:
            print("  !! " + p)
        raise SystemExit("结构自检未通过")
    total = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _dn, fns in os.walk(pkg_root) for f in fns)
    print("  结构完整,共 %.0f MB" % (total / 1e6))


def run_selftest() -> int:
    """check_no_credentials 的双向自测(不需要真固件 bin,用临时文件造假)。

    为什么单独放在这个文件里:dap_config.py 的 --selftest 只 import
    dap_config 自己,看不到 build_package 的检查逻辑。凭据泄露检查一旦
    回归就是"把自家 WiFi 密码发给所有客户"级别的事故,必须有能抓住它的
    测试 —— 而且必须**双向**:既能抓到真泄露,也不能把固件内置常量
    误报成泄露(第一版就是全误报)。
    """
    import tempfile
    tin = []
    tout = []

    def chk(name, ok, detail=""):
        tin.append(1)
        if not ok:
            tout.append((name, detail))

    main_dir = os.path.normpath(os.path.join(SRC_FIRMWARE, "..", "main"))
    cred = os.path.join(main_dir, "wifi_credentials.h")
    if not os.path.isfile(cred):
        print("跳过:开发机上没有 %s,无法测凭据检查" % cred)
        print("(该文件是 gitignore 的本地文件,交付机上本来就没有)")
        return 0

    import re as _re
    text = open(cred, encoding="utf-8", errors="replace").read()
    real = [m.group(1) for m in
            _re.finditer(r'\.(?:ssid|password)\s*=\s*"([^"]*)"', text)]
    builtin = _builtin_firmware_literals(main_dir)

    tmp = os.path.join(tempfile.gettempdir(), "_bp_selftest.bin")
    try:
        # --- 方向一:bin 里真有私有凭据 -> 必须报出来 -------------------
        priv = [s for s in real if len(s) >= 3
                and not is_builtin_literal(s, builtin)]
        if priv:
            open(tmp, "wb").write(b"\x00".join(s.encode("utf-8") for s in real))
            got = check_no_credentials(tmp)
            chk("真泄露能被抓到", bool(got), "priv=%r got=%r" % (priv, got))
            chk("报出的是私有那条", any(priv[0] in g for g in got), str(got))
        else:
            chk("真泄露能被抓到", False, "凭据文件里没有可用的私有项")

        # --- 方向二:bin 只含内置常量 -> 不许误报 -----------------------
        clean = "".join(sorted(builtin, key=len, reverse=True)[:40])
        open(tmp, "wb").write(clean.encode("utf-8"))
        got2 = check_no_credentials(tmp)
        chk("内置常量不误报", not got2, str(got2))

        # --- 方向三:含内置词的私有凭据仍要报(端到端)-------------------
        # 回归:第一版用 "b in s or s in b" 双向子串判断,"dap_my_router"
        # 因为含 "dap"、"password123" 因为含 "pass" 都被当成内置而漏报。
        # 这里必须走 check_no_credentials 的真路径(secrets 参数注入),
        # 不能把判据抄进测试里 —— 那正是审核点名的"假绿"。
        fakes = ["dap_xxxxx", "password123"]
        open(tmp, "wb").write(b"\x00".join(s.encode() for s in fakes))
        got3 = check_no_credentials(tmp, secrets=fakes)
        chk("含内置词的私有凭据不被漏报",
            len(got3) == len(fakes),
            "got=%r" % (got3,))

        # --- 方向四:内置常量即使真在 bin 里也不报(回归误报)-----------
        builtin_hits = ["DAP", "OTA", "12345678"]
        open(tmp, "wb").write(b"\x00".join(s.encode() for s in builtin_hits))
        got4 = check_no_credentials(tmp, secrets=builtin_hits)
        chk("内置常量即使真在 bin 里也不报", not got4, str(got4))
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass

    print("=" * 72)
    for name, detail in tout:
        print("  FAIL  " + name + (("   <- " + detail) if detail else ""))
    print("共 %d 项,%d 通过,%d 失败"
          % (len(tin), len(tin) - len(tout), len(tout)))
    return 1 if tout else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=os.path.join(HERE, "dist"))
    ap.add_argument("--skip-exe", action="store_true",
                    help="只组装工具目录,不重跑 PyInstaller(调打包布局用)")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑凭据泄露检查的双向自测,不打包")
    args = ap.parse_args()
    if args.selftest:
        return run_selftest()
    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    t0 = time.time()
    if args.skip_exe:
        pkg_root = os.path.join(outdir, "无线调试器")
    else:
        pkg_root = build_exe(outdir)
    assemble(pkg_root)
    smoke_check(pkg_root)
    print("完成:%s(%.0f 秒)" % (pkg_root, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
