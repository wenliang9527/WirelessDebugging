# PC 端环境搭建(Windows)

> 三个工具按需安装,互不冲突。Keil 走 elaphureLink、OpenOCD 走 openocd-elaphurelink 都免驱动;只有 pyOCD 需要装 usbip-win。

## 1. elaphureLink(Keil MDK,TCP 直连,免驱动)

- 版本:v1.4.0(2025-01);µVision 已测试 v5.12 ~ v5.40
- 要求:Windows 7+ x64,.NET Framework 4.7.2(Win10/11 一般自带)
- 安装:✅ **已就位**,直接运行 `D:\ARMGCCTool\WirelessDebuggingtool\elaphureLink\elaphureLink.Wpf.exe`(免安装)
- 使用:
  1. 运行 `elaphureLink.Wpf.exe`,GUI 里先执行**驱动安装**(把 elaphureLink 的 µVision 驱动装入 Keil),再连接设备(默认地址 `dap.local`)
  2. Keil → Options for Target → Debug → 下拉框选择 **elaphureLink**(它是独立的 µVision 驱动,不是 Keil 自带的 CMSIS-DAP 选项)
  3. 正常烧录/调试
- 注意:仅支持 **µVision**(传统 MDK);Keil Studio(VSCode 版 MDK)不适用
- 已知限制(2026-10-25 实测):Debug 设置里 **Reset 不能选 `Autodetect`**——elaphureLink v1.4.0 未实现自动检测复位,会弹"does not implement this feature yet"警告;**改成 `SYSRESETREQ`** 即可。Max Clock 名义值 10MHz 可用,不稳就降 5/1MHz(实际瓶颈是 WiFi 往返)
- 已知限制 2:**烧录后的"HW Chip Reset after flash load"同样未实现**(烧录本身 Erase/Program/Verify 全部正常)——每次下载完**手动按目标板 RST**(或 Keil 工具栏 RST)即可;可去官方 repo 提 issue 催实现
- 来源:https://github.com/windowsair/elaphureLink

## 2. openocd-elaphurelink(OpenOCD,TCP 直连,免驱动)

- 版本:**v0.4.0**(2026-10 经 GitHub API 确认的最新版,基于 OpenOCD 0.12.0+dev,2026-09 构建,本机已验证可运行)
- ✅ **已就位**:`D:\ARMGCCTool\WirelessDebuggingtool\openocd-elaphurelink\v0.4.0\`(bin + scripts 完整)
- 使用(独立运行,用完整路径):
  ```shell
  D:\ARMGCCTool\WirelessDebuggingtool\openocd-elaphurelink\v0.4.0\bin\openocd.exe -f interface/elaphurelink.cfg -f target/stm32f1x.cfg
  ```
  默认连接 `dap.local`;设备 IP 不是这个时改地址:
  ```shell
  openocd.exe -f interface/elaphurelink.cfg -c "cmsis-dap elaphurelink addr 192.168.x.x" -f target/xxx.cfg
  ```
- 与 VSCode 集成:cortex-debug 插件的 `serverpath` 指向这份 openocd,interface 用 `elaphurelink.cfg` 即可
- 注意:这是带补丁的独立 OpenOCD,**与系统里已有的原版 OpenOCD 共存即可,不要互相覆盖**
- 来源:https://github.com/windowsair/openocd-elaphurelink

### 多版本 OpenOCD 共存(本机实况,2026-10-25 核对)

本机共 4 份 OpenOCD 构建,用途正交、互不冲突:

| 构建 | 位置 | 用途 |
| --- | --- | --- |
| atopenocd-py32 v0.4.4 | `D:\ARMGCCTool\atopenocd-py32\` | ocdup 调试服务专用(USB 适配器;带 PY32 烧录驱动),**不动它** |
| openocd-elaphurelink v0.4.0 | `D:\ARMGCCTool\WirelessDebuggingtool\openocd-elaphurelink\v0.4.0\` | 无线 DAP 专用(TCP 直连 `dap.local`) |
| ATopenocd V2.0.6 | `D:\TOOLpacksave\ATopenocd\*.zip` | 备件,保持压缩状态 |
| openocd 20240916 快照 | `D:\TOOLpacksave\openocd\*.7z` | 备件,保持压缩状态 |

**共存规则**:OpenOCD 是绿色便携程序(不写注册表、无服务、无共享组件),多份并存零冲突;运行时注意三点——
1. **PATH 谁都不进**,调用一律用完整路径;VSCode cortex-debug 用 `serverpath` 按项目指定,ocdup 照旧走 `OCD_BIN` 环境变量
2. **不要同时跑两个实例抢同一资源**:elaphurelink 版走 WiFi、atopenocd-py32 走 USB,天然错开;若同时手动开两个 gdb server 会撞端口(3333/4444/6666),不同时跑即可
3. **别混用 scripts 目录**:每个构建配自己的 `share/openocd/scripts`;但 **cfg 文件可以跨版本复用**——用 elaphurelink 版调 PY32 目标时,上游不带 PY32 配置,加参数借用 ocdup 的现成 cfg:
   ```shell
   openocd.exe -f interface/elaphurelink.cfg -s D:\WORK_VSCODE\Vibe-coding\Ocdup\cfgs -f 你的py32目标.cfg
   ```
- AT32 目标:**上游 OpenOCD 没有 Artery 的 AT32 flash 驱动**(openocd-elaphurelink 实测脚本目录无 at32f43x.cfg)——无线 OpenOCD 对 AT32 可以**内核级调试**(halt/单步/断点),但**烧录**要么走 Keil 路线(elaphureLink + Keil 的 AT32 器件包,Keil 自己带 flash 算法),要么把 elaphurelink 补丁合入雅特力 fork 自编译(后置任务)
- PY32 目标:上游同样不带 PY32,用 `-s` 借 ocdup 的 cfg(见下)

## 3. usbip-win(pyOCD 及其他只认 USB 的工具,通用兜底)

- 版本(2026-10 核实 releases):最新 **v0.3.6-dev**,vhci 驱动已通过微软 attestation 签名,**无需测试模式**;求稳可用 v0.3.5(修复过 detach 时 BSOD、支持 USB3.0)
- ✅ **已就位**:`D:\ARMGCCTool\WirelessDebuggingtool\usbip-win\v0.3.6-dev\`(usbip.exe + 驱动文件)
- ⚠️ **命令已变**:**0.3.3 起 `install_ude` / `attach_ude` 命令已移除**,统一为 `install` / `attach`(windowsair README 上写的 `attach_ude` 是 0.3.0 时代的旧用法)
- 安装(2026-10-26 已装):管理员运行 `usbip.exe install`(在 v0.3.6-dev 目录内),输出 `vhci(ude) driver installed successfully`;卸载 `usbip.exe uninstall`。**助手脚本已就位**:`WirelessDebuggingtool\usbip-win\` 下 `install_driver.bat` / `uninstall_driver.bat` / `attach_dap.bat [IP]` / `detach_dap.bat`,右键"以管理员身份运行";**无窗口挂载**用 `attach_dap_hidden.vbs`(双击,UAC 点"是",后台常驻无黑窗)
- 使用(0.3.3+ 命令,2026-10-26 全程实测):
  ```shell
  # ⚠️ 不要跑 list!windowsair 固件不支持 DEVLIST,会卡死且拖死固件 TCP 服务(3240 拒绝新连接),
  #    只能按 DAP 板 RST 恢复。直接 attach:
  usbip.exe attach -r <设备IP> -b 1-1     # busid 实测就是 1-1
  usbip.exe port                          # 查看已挂载端口
  usbip.exe detach -p <端口>              # 卸载
  ```
- 设备 IP 会变(热点 DHCP:.253→.187),attach 前用 `arp -a | findstr ac-a7-04` 找当前 IP;attach 不持久,PC/DAP 重启后重挂
- **与 ocdup 项目联动(2026-10-26 ✅ 实测通过)**:`usbip attach -r 192.168.137.187 -b 1-1` 后设备管理器出现"**Wireless ESP CMSIS-DAP**",ocdup **零配置改动**直连(ocd.json 本来就是 `interface/cmsis-dap.cfg` + `target/at32f403axG.cfg`):identify 出 AT32F403AXG(DEV_ID 0x344)、halt/read/resume 正常、**506KB axf 无线烧录+读回校验+复位一次通过**。裸读速度约 10 KiB/s(64KB/6.3s),flash 驱动走块操作快得多。终极方案(后置):elaphurelink 补丁合入雅特力 fork 自编译合体版,免 usbip 直连
- ⚠️ pyOCD 路线暂不可用:`pyocd list` 看不到桥接设备(枚举为 USBDevice 类,非 HIDClass),需另绑 WinUSB/libusb 驱动,待需要时再深究;ocdup/OpenOCD 路线不受影响
- 来源:https://github.com/cezanne/usbip-win

## 常见问题

- **ping 不通 dap.local**:mDNS 仅限同网段;路由器开了 AP 隔离会失效,临时可改用设备 IP 直连
- **elaphureLink 连接后 Keil 报 RDDI-DAP ERROR**:先查四线(含 GND)和 TVCC,再查网络稳定性
- **usbip 挂载后设备频繁掉线**:多因网络丢包;参考选型文档"网络环境敏感"一节
- 防火墙首次运行各工具时放行本地网络访问
