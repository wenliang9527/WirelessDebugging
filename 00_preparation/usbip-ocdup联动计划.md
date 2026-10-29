# usbip 驱动 + ocdup 无线联动计划(2026-10-26)

> 目标:装 usbip-win 驱动,把无线 DAP 桥接成本地 USB 设备,让 ocdup 的 AT32/PY32 烧录调试工具链整体无线化。
> 路由器迁移本轮**暂缓**(用户决定),全程使用 Windows 移动热点网络(192.168.137.x)。

> ⚠️ **2026-10-27 起本文档网络语境过时**:热点 192.168.137.x → 路由器 192.168.7.x(DAP 静态绑定 .125)。
> usbip 日常运维的权威指引 = 用户级 skill `wireless-dap`(掉线诊断树/list -r 禁令/重挂方法),本文仅作历史记录。

## 0. 已核实的事实(2026-10-26 探查)

| 事实 | 内容 |
| --- | --- |
| ocdup 配置 | `D:\WORK_VSCODE\Vibe-coding\Ocdup\ocd.json` 已是 `interface_cfg = interface/cmsis-dap.cfg`、`target_cfg = target/at32f403axG.cfg`,权限齐全(flash 路径不限、dump 到 `Ocdup\dumps\`)——**零改动**即可联动 |
| 目标芯片 | DEV_ID 0x344 = **AT32F403A**(ocd.json chip_id_map 自带映射,与 target_cfg 吻合);"型号待确认"一项关闭 |
| usbip-win | v0.3.6-dev 已就位 `D:\ARMGCCTool\WirelessDebuggingtool\usbip-win\v0.3.6-dev\`,attestation 签名**免测试模式**;包内无 README,命令经官方仓库核实 |
| 脚本目录 | ocdup 实际用 `OCD_BIN=D:\ARMGCCTool\atopenocd\bin\openocd.exe` + `OCD_SCRIPTS=D:\ARMGCCTool\atopenocd\scripts`(ocd.json 头注释);atopenocd 与 atopenocd-py32 两份均**不动** |

**官方核实的命令**(cezanne/usbip-win README):
```powershell
# 管理员 PowerShell/CMD,在驱动目录内执行
usbip.exe install          # 安装 vhci 驱动(UDE 优先;-u 仅 UDE,-w 仅 WDM)
usbip.exe uninstall        # 卸载
usbip.exe attach -r <服务端IP> -b <busid>   # 挂载远端 USB 设备
```

## 1. 什么必须一直开着(2026-10-26 答疑)

| 使用场景 | elaphureLink GUI | Windows 移动热点 |
| --- | --- | --- |
| Keil 无线烧录/调试 | **必须开** | 必须开 |
| openocd-elaphurelink | 不用开 | 必须开 |
| usbip + ocdup/pyOCD | **不用开**(建议关掉防抢占) | 迁路由器前仍需开 |

usbip 与 elaphureLink/Keil 会抢同一个 DAP 会话:用 ocdup 前先关 elaphureLink 开关、退出 Keil 调试会话。

## 2. 执行步骤

### 阶段 2-1:安装 usbip-win 驱动(系统级,一次)
```powershell
cd D:\ARMGCCTool\WirelessDebuggingtool\usbip-win\v0.3.6-dev
# 管理员身份:
usbip.exe install
```
- 验证:`usbip.exe --version`;设备管理器出现 usbip vhci 设备
- 回退:`usbip.exe uninstall`(随时完整卸掉)

### 阶段 2-2:桥接 + 冒烟(热点网络,IP 192.168.137.253)
```powershell
usbip.exe list -r 192.168.137.253     # ❌ 已禁用!会永久挂起并拖死固件服务(见 3.1 #1)——直接执行下一条 attach
usbip.exe attach -r 192.168.137.253 -b 1-1
usbip.exe port                          # 查看已挂载端口
usbip.exe detach -p <端口>              # 卸载
```
- 挂载成功 → 设备管理器出现 CMSIS-DAP 设备
- ocdup(MCP):connect → identify_chip(预期 AT32F403A/0x344)→ halt → read_memory → resume
- 可选:`pyocd list` 验证 pyOCD 路线

### 阶段 2-3:备份 + 无线烧录实测(可现场跳过)
1. `dump_memory` 全片备份 AT32F403A flash 到 `Ocdup\dumps\`(回退保险)
2. ocdup `flash_write` 重烧 Fillingtooling 固件 + verify,记录无线烧录耗时(对比 USB 基准 3.1s/428KB)
3. 按目标板 RST,确认工装程序正常跑

### 阶段 2-4:助手脚本
`attach_dap.bat` / `detach_dap.bat` 放 `WirelessDebuggingtool\usbip-win\`,右键"以管理员身份运行"即挂/卸。

## 3. 记录表

| 项目 | 结果 | 日期 |
| --- | --- | --- |
| usbip 驱动安装 | ✅ `vhci(ude) driver installed successfully`,设备管理器 OK | 2026-10-26 |
| usbip list(1-1 可见) | ❌ 固件不支持——**list 会卡死并拖死固件 TCP 服务**,只能按 DAP RST 恢复;一律直接 attach | 2026-10-26 |
| attach 成功/设备管理器 | ✅ `succesfully attached to port 0`(busid **1-1** 正确);设备"**Wireless ESP CMSIS-DAP**"枚举 OK | 2026-10-26 |
| ocdup identify(AT32F403A) | ✅ DEV_ID 0x344 / rev 0x7005 → AT32F403AXG,双 bank 各 512KB | 2026-10-26 |
| ocdup halt/read/resume | ✅ halt 后读向量表正常(初始 SP 0x20002B30、复位向量 0x0800026D),resume 正常 | 2026-10-26 |
| 无线 dump 速度 | 64KB/6.3s ≈ **10 KiB/s**(裸读;flash 驱动写走块操作快得多) | 2026-10-26 |
| 无线烧录(对比 USB 3.1s/428KB) | ✅ **506KB axf 擦+写+读回校验+复位 一次通过**,未超 60s 命令超时 | 2026-10-26 |
| 工装程序重烧后运行 | ✅ reset run 后 PC=0x08001dc4、LR=0x080051c9,应用区正常执行 | 2026-10-26 |

## 3.1 实测发现的已知问题(2026-10-26)

1. **`usbip list -r` 禁用**:windowsair v0.4.0 固件不实现 DEVLIST,该命令永久挂起且把固件 TCP 服务拖死(3240 不再接受新连接)——只能按 DAP 板 RST 恢复。**直接 `attach -r <IP> -b 1-1`**
2. **热点 DHCP 会换 IP**:.253 → .187(重启/租约到期后可能再变)。attach 前用 `arp -a | findstr ac-a7-04` 找当前 IP;attach_dap.bat 支持传参
3. **attach 不持久**:PC 重启/DAP 重启后需重挂(助手脚本:`WirelessDebuggingtool\usbip-win\` 下 install/uninstall/attach/detach_dap.bat,右键管理员运行)
4. **pyOCD 暂不可用**:`pyocd list` 看不到桥接设备——"Wireless ESP CMSIS-DAP"枚举为 USBDevice 类而非 HIDClass,pyocd 的 hidapi 不认;需另绑 WinUSB/libusb 驱动才可能支持(未深究)。**ocdup/OpenOCD 路线不受影响**
5. **ocdup 实际后端是 `atopenocd-py32\bin\openocd.exe`**(connect 输出实证);ocd.json 头注释写的 `atopenocd` 路径已过时——两份安装均未改动
6. bash 直跑 usbip.exe 会被拦(Permission denied),经 `cmd //c` 或 bat 正常
7. **attach 进程常驻**:usbip-win 0.3.6(UDE)下 `attach` 成功后会保持一个 usbip.exe 进程及其父 cmd 窗口(黑色空窗口,**属正常现象**)——关窗/杀进程 = 桥接断开(设备消失),重跑 attach_dap.bat 即恢复;`usbip port`/`list` 同样要管理员才有输出
8. 2026-10-26 收尾时实况:并存两个 TCP 会话(wedged 时代幸存的残留进程 + 正式 attach),实测互不影响(506KB 烧录在双会话下完成);下次 PC/DAP 重启后**只做一次干净 attach** 即可,不必复刻双会话

## 4. 风险与对策

- usbip 命令需管理员:统一走 UAC/管理员 bat
- **attach 不持久**(PC/DAP 重启即掉):助手脚本 + 文档写明,每轮测试前重挂
- 设备枚举异常:detach 重挂;驱动级故障 uninstall 重装;usbip-win2(vadimgrn)为备选升级路线
- 烧录/单步变慢(USBIP+WiFi 往返):预期内,如实记录数据
- 官方自述 "not yet ready for production use":原型验证用途可接受

## 5. 下一轮待办(本轮不做)

- 路由器迁移:加 2.4G 专用 SSID「DAP」/12345678(访客或多 SSID,**关客户端隔离**),MAC aa:bb:cc:dd:ee:fc 做 DHCP 静态绑定;迁移后关 Windows 热点,重跑 attach + ocdup 全链路
- 30 分钟稳定性测试、距离测试(0.5m/5m/隔墙)
- 无线单步/断点体验评分(1-5)

## 不变的约束

不动 atopenocd / atopenocd-py32 本体;4 份 OpenOCD 均不进 PATH;flash_download_tool 不外发;目标板 3.3V 电平。
