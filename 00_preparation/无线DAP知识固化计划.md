# 无线 DAP 知识固化计划(2026-10-27)

> 起因:s403-AT 会话遇掉线,因看不见本项目会话 memory 里的运维规则,按"有线探头"常识误判为"重插 USB 线"。
> 拍板结论(2026-10-27,三审通过):**skill 根治断层 + 旧文档两刀;probe_doctor 撤项不立项**(ocdup doctor 已覆盖通用诊断层,无线专属知识不进 MCP)。

## 1. 改什么 / 不改什么

| 对象 | 动作 |
| --- | --- |
| 用户级 skill `wireless-dap` | **新建**,双写 `C:\Users\<user>\.zcode\skills\` 与 `C:\Users\<user>\.agents\skills\`(内容一致) |
| 《usbip-ocdup联动计划.md》 | **两处小修**(见 §3) |
| ocdup MCP 本体 | **零改动** |
| ocdup-zcode 插件 | **零改动**(无代码变更,不涉及版本号/重启) |
| ocd.json / 烧录流程 / ocdup 工具集 | 零改动 |

## 2. 改动一:skill 草案(审这个)

目录:`...\skills\wireless-dap\SKILL.md` × 2 处。全文如下——

```markdown
---
name: wireless-dap
description: 无线 DAP(ESP固件+usbip桥接)的掉线诊断与重挂。当 OpenOCD/ocdup 报 "unable to find a matching CMSIS-DAP device"、设备树里 CMSIS-DAP 消失、或需要挂载/卸载无线调试器时使用。有线探头(AT-Link/ST-Link 等USB直连)不适用。
---

# 无线 DAP 运维(usbip 桥接)

## 0. 拓扑与铁律(先读)

- 链路:PC ⇄ WiFi ⇄ 路由器(MyWiFi AP 域,192.168.7.x) ⇄ 无线 DAP(192.168.7.125,
  MAC aa:bb:cc:dd:ee:fc,DHCP 已静态绑定) ⇄ SWD ⇄ 目标板
- **Windows 设备树里的 CMSIS-DAP 是 usbip 虚拟的**——PC 与 DAP 之间没有 USB 线,
  任何"重插 USB 线"建议都是架构误判
- **铁律 1:永远不要 `usbip list -r`**——windowsair 固件不实现 DEVLIST,该命令永久挂起
  且拖死固件 TCP 服务(3240 不再接受连接),只能给 DAP 断电恢复。一律直接 attach
- **铁律 2:attach 进程常驻**——attach 成功后 usbip.exe 后台常驻,进程退出 = 桥接断 =
  设备树 CMSIS-DAP 消失。任何一次 TCP 断开(DAP 重启/WiFi 抖动/PC 网络切换)都会使其退出
- 铁律 3:bash 直跑 usbip.exe 会被拦(Permission denied),须经 `cmd //c` 或 bat/vbs 运行;
  usbip 命令均需管理员

## 1. 掉线诊断树(OpenOCD 找不到 CMSIS-DAP 时,按序三条只读检查定案)

1. `ping 192.168.7.125` —— 不通 = 网络层问题:查 PC 是否连在 MyWiFi 的 AP 域(§4),
   `arp -a | findstr "ac-a7-04"` 找 IP 是否漂移
2. `powershell Test-NetConnection 192.168.7.125 -Port 3240` ——
   - **OPEN** = 固件 usbip 服务活着,掉的是 PC 侧 attach 进程 → 直接 §2 重挂(无需动 DAP)
   - **不通/挂起** = 固件 usbip 服务卡死(大概率有人跑过 list -r)→ 先给 DAP 断电重启,再 §2 重挂
3. `powershell Get-PnpDevice | Where-Object FriendlyName -match 'CMSIS-DAP'` ——
   有设备 = 桥接在,问题在别处(如 elaphureLink/Keil 抢占,§5);无设备 + 前两项正常 = 确诊 attach 掉了

`usbip port`(需管理员)仅作残留确认,不作主判据(attach 进程退出后它本来就该是空的)。

## 2. 重挂 / 拆桥

- 挂:双击 `D:\ARMGCCTool\WirelessDebuggingtool\usbip-win\attach_dap_hidden.vbs`
  (UAC 点"是",无黑窗;默认 IP 192.168.7.125,IP 变了可传参)。日志:同目录 attach_hidden_log.txt
- 验证:设备树出现 "Wireless ESP CMSIS-DAP"(USBDevice 类)→ ocdup connect 即可
- 拆:同目录 detach_dap.bat(管理员)
- 保守规则:拆桥只走 detach_dap.bat,**不要 taskkill usbip 进程**(杀提权 usbip 进程会连坐桥接会话)

## 3. 串口锚定(认 VID+PID,不认 COM 号)

| 用途 | 锚(VID+PID) | COM 号行为 |
| --- | --- | --- |
| 无线 DAP 的 CDC 串口 | VID_303A&PID_1001 | **会漂**(实例无序列号,号随 usbip port 变,勿写死) |
| 目标板串口(CH343) | VID_1A86&PID_55D3,序列号 5C36142497 | 恒定(当前 COM19) |

查询当前号:`powershell Get-PnpDevice | Where-Object InstanceId -match 'VID_303A&PID_1001'`
注意:"USB 串行设备"友好名不唯一(本机 5 个实例);另有 VID_303A&PID_4001 是其他 ESP 板,
必须认到 VID+PID 这一级,只认 VID 会撞。

## 4. 双路由拓扑陷阱

PC 必须连 MyWiFi 的 AP 域(192.168.7.x)才能到达 DAP。
[待补:另一台路由/域是什么,连错域的具体症状与判别方法]

## 5. 排外与共存

- elaphureLink GUI / Keil 调试会话与 usbip 抢同一 DAP 会话:用 ocdup 前先关
- pyOCD 看不到桥接设备(枚举为 USBDevice 类而非 HID),已知限制,勿折腾
- ocdup 实际后端是 atopenocd-py32(非 ocd.json 头注释写的 atopenocd)
- 烧录防呆:ocdup flash_write 自带读回校验,**末行 `OCDUP-RESULT: flash-write=OK` 才算数**;
  重要固件烧前先 dump_memory 备份
- 速度基线:无线 dump ≈10 KiB/s(裸读);无线烧录 506KB 擦+写+校验+复位一次通过(2026-10-26)

## 6. 参考

历史细节与实测数据:`D:\WORK_VSCODE\Vibe-coding\WirelessDebugging\00_preparation\usbip-ocdup联动计划.md`
(网络语境部分已过时,以本 skill 为准)
```

## 3. 改动二:《usbip-ocdup联动计划.md》两刀

1. **顶部横幅**(第 3 行引用块之后插入):
   > ⚠️ 2026-10-27 起本文档网络语境过时:热点 192.168.137.x → 路由器 192.168.7.x(DAP 静态绑定 .125)。
   > usbip 日常运维的权威指引 = 用户级 skill `wireless-dap`,本文仅作历史记录。
2. **阶段 2-2 第一条命令**(第 46 行)改为:
   `~~usbip.exe list -r 192.168.137.253~~` **❌ 已禁用——会永久挂起并拖死固件服务(见 3.1 #1),直接执行下一条 attach**

其余内容(含 3.1 的 8 条已知问题)不动——它们是 skill 的素材来源,保留作历史记录。

## 4. 开放项(执行前请补三条,或授权我按占位写法先发)

1. **双路由拓扑细节**(skill §4 占位中):MyWiFi 之外另一台路由/域是什么?连错域的具体症状?
2. **杀 elevated usbip 连坐**:确切表现是什么?(当前草案按保守规则"只走 detach_dap.bat 不 taskkill"落,若另有细节可补)
3. **读回 md5 制度**:准确定义?(当前草案落成"flash_write 末行 OCDUP-RESULT 标记 + 烧前 dump 备份";若你们实践是烧后 dump+md5 归档,告诉我归档到哪)

## 5. 验证方式

- skill:双目录文件 diff 一致;新开会话问"ocdup 连不上 CMSIS-DAP 怎么办",确认按诊断树走、不再出"重插 USB 线"
- 文档:通读确认无自相矛盾(步骤段与 3.1 不再打架)
