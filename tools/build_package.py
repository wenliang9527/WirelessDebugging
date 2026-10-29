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
    │  ├─ usbip-win/v0.3.6-dev/    GPL-2(独立进程)
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
        "GPL-2.0",
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


def write_tool_notice(tools_dir: str) -> None:
    """每个第三方目录放一个 源码获取.txt:GPL 义务要求随包告知源码位置。"""
    for name, (title, url, lic) in SOURCES.items():
        d = os.path.join(tools_dir, name)
        if not os.path.isdir(d):
            continue
        with open(os.path.join(d, "源码获取.txt"), "w", encoding="utf-8") as f:
            f.write(
                "%s\n许可证:%s\n上游源码:%s\n\n"
                "本组件以独立进程方式被「无线调试器.exe」调用,未与其代码链接。\n"
                "按许可证要求随包提供获取源码的方式(如上链接)。\n" % (title, lic, url))
        # GPL-2 全文;BSD-2 放短声明
        if lic == "GPL-2.0":
            copying = os.path.join(d, "COPYING")
            if not os.path.isfile(copying):
                # 源码树里通常有;没有就从 usbip-win 的复制一份兜底
                for cand in (os.path.join(tools_dir, "usbip-win", "COPYING"),
                             os.path.join(SRC_ATOPENOCD, "COPYING"),
                             os.path.join(SRC_TOOLS_ROOT, "usbip-win",
                                          "v0.3.6-dev", "COPYING")):
                    if os.path.isfile(cand):
                        shutil.copyfile(cand, copying)
                        break


LICENSE_SUMMARY = """\
许可与开源声明
==============

本软件(「无线调试器」配网/调试工具,下称"本工具")为专有分发,允许个人与
内部商用使用与再分发;固件部分以 MIT 许可发布。

本工具按「独立进程」方式调用以下第三方组件,不与其代码链接:

  组件                      许可证      用途
  ------------------------  ----------  ----------------------
  elaphureLink              BSD-2      Keil 无线调试插件(免驱动)
  openocd-elaphurelink      GPL-2      OpenOCD(STM32 无线 SWD)
  usbip-win                 GPL-2      USB over IP 桥接
  atopenocd                 GPL-2      OpenOCD 雅特力 fork(AT32 烧录)

各组件的许可证全文见 tools/<组件>/ 目录(COPYING 或 源码获取.txt)。
GPL-2 组件以独立进程运行、经标准接口(命令行/网络)与本工具通信,
依据 GPL 与 FSF 惯例,该使用方式不使本工具成为衍生作品。

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
    print("[3/4] 写许可与源码获取说明…")
    write_tool_notice(tools)

    print("[3/4] 固件与文档…")
    fw = os.path.join(pkg_root, "firmware")
    os.makedirs(fw, exist_ok=True)
    # IDF 的 build 布局:app 在 build/ 根,bootloader/partition 在各自子目录
    for rel in ("wireless_esp_dap.bin",
                os.path.join("bootloader", "bootloader.bin"),
                os.path.join("partition_table", "partition-table.bin")):
        shutil.copyfile(os.path.join(SRC_FIRMWARE, rel),
                        os.path.join(fw, os.path.basename(rel)))
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
    for f in ("快速上手.md", "许可与开源声明.txt", "README.txt"):
        if not os.path.isfile(os.path.join(pkg_root, f)):
            problems.append("缺 " + f)
    for name in SOURCES:
        n = os.path.join(pkg_root, "tools", name, "源码获取.txt")
        if not os.path.isfile(n):
            problems.append("缺 tools/%s/源码获取.txt" % name)
    if problems:
        for p in problems:
            print("  !! " + p)
        raise SystemExit("结构自检未通过")
    total = sum(
        os.path.getsize(os.path.join(dp, f))
        for dp, _dn, fns in os.walk(pkg_root) for f in fns)
    print("  结构完整,共 %.0f MB" % (total / 1e6))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=os.path.join(HERE, "dist"))
    ap.add_argument("--skip-exe", action="store_true",
                    help="只组装工具目录,不重跑 PyInstaller(调打包布局用)")
    args = ap.parse_args()
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
