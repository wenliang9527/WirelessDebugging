# 项目体检报告 —— WirelessDebugging(无线调试器)

- 日期:2026-11-01
- 范围:仓库 A(PC 工具+文档,`D:\WORK_VSCODE\Vibe-coding\WirelessDebugging`)+ 仓库 B(固件,`D:\ARMGCCTool\WirelessDebuggingtool\firmware-src\wireless-esp8266-dap`)+ 第三方组件使用方式
- 方法:只读审查 + 三类自测实跑 + 5 组变异验证;每条发现带 file:line 证据
- 基线:工具分支 `fix/ap-gui-thread-and-error-layering` @ `f40e9fa`(工作区干净);固件 `feature/productization` @ `28476c6`(0.8.0,工作区干净)

---

## ① 执行摘要

**总体健康分 76 / 100**。没有 P0(无正在发生的崩溃/丢数据级缺陷)。这是一个测试真实、协议对齐、错误处理极其讲究的项目;主要欠账不在代码,而在**工程保障**(固件成果零远程备份)和**两处会误导用户的文档错误**。

| 维度 | 分 | 一句话结论 |
| --- | --- | --- |
| A 固件并发/资源 | 7.0 | 一处确认的无锁共享状态 + DAP_Thread 2048 栈的同款风险;其余扎实 |
| B 工具架构/质量 | 8.5 | 线程模型纪律性极强;4900 行单文件是唯一结构性欠账 |
| C 协议一致性 | 10 | 固件 dap_cmd.c ↔ 工具解析逐字段核对**全对齐**,含老固件回退 |
| D 测试质量 | 9.0 | 206+5 项自测全绿(源码/冻结 exe 同步);**5/5 变异全被抓住,测试是真的** |
| E 安全与隐私 | 8.0 | AP 密码随机+仅 USB 可读;凭据不出仓库有机器门禁;无鉴权端口是已声明取舍 |
| F 许可合规 | 7.0 | GPL 义务履行完整;**交付包声明与仓库 MIT 自相矛盾** |
| G 工程卫生 | 5.5 | **固件 17 个二开提交仅存本地**;根目录垃圾;无 CI |
| H 文档一致性 | 6.0 | tools/README 质量极高;但用户手册有两处会带偏排障方向的硬错误 |

---

## ② 分级发现总表

| 级别 | 数量 | 编号 |
| --- | --- | --- |
| P0 | 0 | — |
| P1 | 3 | G-1, H-1, H-2 |
| P2 | 6 | A-1, A-2, F-1, H-3, G-2, B-1 |
| P3 | 12 | A-3 ~ A-6, B-2 ~ B-7, H-4, G-3 |

---

## ③ 各维度详情

### A. 固件侧(C/ESP-IDF)

**A-1(P2,确认)配置内存镜像无锁并发读写**
- 证据:`dap_config.c:44-50` 的 `s_aps/s_count/s_mode_pref/s_led_pct/s_ap_pass` 是全局镜像,无任何互斥;写路径有二:`cmd_task`(USB 命令,`dap_cmd.c:707` 起,经 @DEL/@PIN/@CONNECT 成功落盘)与 **WiFi 事件回调**(`wifi_handle.c:196` GOT_IP → `dap_config_add()` → `save_all()` 整表重写;`ssid_change()` 在事件回调里读)。
- 触发条件:@CONNECT 成功拿到 IP 的同一时刻,PC 侧并发发 @LIST/@DEL(工具的 worker 队列理论上串行,但状态刷新与用户操作可交叠)。后果是撕裂读/写,概率低但存在。
- 修法:加一把 `SemaphoreHandle_t` 包住全部 `dap_config_*` 读写(条目≤5、调用频率低,开销可忽略)。工作量:**现成,半天**。

**A-2(P2,疑似)DAP_Thread 栈 2048 = uart_server 爆栈同款风险**
- 证据:`main.c:167` `xTaskCreatePinnedToCore(DAP_Thread, "DAP_Task", 2048, ...)`;对照 `main.c:170-182` 的注释——uart_server 同为 2048 时"只要 client 被 accept 就爆栈",lwIP 调用要 0.5-1KB 临时栈。
- 区别:DAP_Thread 里 recv 发生在 tcp_server 任务(4096),DAP_Thread 本身只做 ringbuf 收发 + DAP_ProcessCommand;未观测到溢出。但它是全项目最后一个 2048 级任务,属于"同一类子弹"。
- 修法:抬到 4096(S3 RAM 充裕)并真机跑一轮大流量烧录验证。工作量:30 分钟 + 真机。

**A-3(P3)DAP_handle 死检查**:`DAP_handle.c:172` 与 `:189` 两处都写 `dap_dataIN_handle == NULL || dap_dataIN_handle == NULL`(IN 检了两遍,OUT 从未检查)。OUT 创建失败时会带着 NULL 走到 `xRingbufferSend` → crash。上游遗产,内存充裕时几乎不触发;改为检查 OUT 即可。

**A-4(P3)usbip 分支命名与协议直觉相反,勿"修复"**:`tcp_server.c:134-139` 把 0x8005(OP_REQ_IMPORT)映射到 `WAIT_DEVLIST`、0x8003 映射到 `WAIT_IMPORT`,与命名直觉相反;**实测 usbip attach 全链路可用**(协议偏移与 usbip-win 实现自洽),而已知的 `usbip list -r` 挂死也由此分支路径解释。建议只加一段注释说明"该映射实测正确,勿按协议字面改名",防止未来好心修复破坏它。

**A-5(P3)超长行残段会当新命令分发**:`dap_cmd.c:725-731` 行超 255 时清零重报 line_too_long,但该行剩余字节会继续累积并在 `\n` 到来时被分发。仅 USB 控制台可达,风险低;应改为丢弃直到 `\n`。

**A-6(P3)杂项**:`dispatch_line` 里 `*name='\0'` 连写两次死代码(`dap_cmd.c:672-677`);`cmd_pin` 用 `strstr(args,"on")` 找开关词,遇到参数里含 "on" 的杂散形态会误判(`dap_cmd.c:467`,现网参数形态固定,仅理论);`ap_pass_generate` 注释说"剩 31 个字符"实为 27 个(`dap_config.c:499`,熵 47.6bit 仍远超 WPA2 需求);`monitor.c` 整文件死代码(无引用);`timer.c` 在 S3 上 `get_timer_count()` 恒返回 0(上游行为);boot 期间 `wifi_init()` 最长阻塞 20s,期间 3240/1234 尚未监听(排障表可补一句)。

已核实为**刻意设计、不报问题**:uart_server 栈 6144、use_nonblocking、@FACTORY 清成未配网、主网不轮转、扫描前无条件断开、`%.31s` 精度限定、ESP8266 分支保留。

### B. 工具侧(Python)

**B-1(P2)`dap_config.py` 4903 行单文件**
设置持久化、编解码、协议解析、传输层、假设备、客户端、206 项自测、GUI 全在一个文件。当前能跑、注释极好,但:① GUI 改动要通读协议层才能动手;② `MockTransport` 是协议的"第二实现",固件改格式时靠人肉同步(历史证明会漂移,@AP 就是补上的)。
- 修法:机械拆四块 `dap_proto.py`(编解码+解析+DapClient)/ `dap_mock.py` / `dap_gui.py` / `dap_selftest.py`,主文件只留 main。**现成,1 天**,拆完顺带把"mock 应答字面量 ↔ dap_cmd.c 格式串"做成一张对照自测。

**B-2(P3)`ElaphureLinkBackend.start` 返回值恒 True**:`dap_backends.py:551` `return True, "已启动(%s)" % msg if ok else "已启动,但%s" % msg` —— 三元只作用于消息,布尔恒 True。调用方只用 msg,无实害,但语义错了;加括号即可。

**B-3(P3)`reachable()` 探到 3240 即成功**:`dap_backends.py:343-351` 循环里第一个端口连上就 return,不验 1234;而失败文案说"端口都连不上(3240/1234)"。挂载确实只需要 3240,故仅 P3;建议改成分别报告两个端口的状态。

**B-4(P3)指示灯滑块量程 1-20,固件支持 1-100**:`dap_config.py:2955` 滑块 `from_=1,to=20`;固件 `DAP_CFG_LED_MAX_PCT=100`。>20 只能 `--raw "@LED 60"`。要么放开滑块,要么在 UI 注明"滑块只到 20"。

**B-5(P3)小噪音**:`SerialTransport.read` 的 `try/except: raise` 纯冗余(`dap_config.py:548-551`);`Backend.stop()` 后读线程不 join(进程退出前短暂残留);`OpenOcdBackend` usbip 形态借用 `device_addr` 传 exe 路径(dap_backends.py:226,有注释但 API 脆)。

**B-6(P3)根 README 定位过期**:根 `README.md` 把 `tools/` 描述为"辅助脚本(非交付物)",且"当前状态"停在 P0(10-28),落后于 0.8.0。

**B-7(P3)mock 协议漂移防护**:@AP 的教训(形态表/mock 都要跟固件同步)值得制度化——见 B-1 的对照自测。

已核实为**刻意设计**:worker+events 双队列线程模型(主线程零 I/O,有测试钉住)、`_busy` 串行化、设置只存 addr/target 三键、`_persist_settings` 全默认即删文件、reachable/attached 的缓存+后台探测。

### C. 协议一致性(固件 dap_cmd.c ↔ 工具 dap_config.py)

逐字段核对结果:**全对齐**。

| 命令 | 固件应答(格式串) | 工具解析 | 判定 |
| --- | --- | --- | --- |
| @PING | `pong fw=%s mac=%02X.. mode=%s` | RE_PING | ✓ |
| @STATUS | STA: `mode=.. ssid=.. ip=.. rssi=%d ch=%d prov=%d`;AP: `mode=AP ip=.. ssid=..` | RE_STATUS_STA(prov 可选=老固件兼容)/ RE_STATUS_AP | ✓ |
| @SCAN | `@DATA idx=&ssid=&rssi=&auth=&ch=` + `scan count=%d` | RE_SCAN_DATA(锚定正则防 SSID 撕裂) | ✓ |
| @CONNECT | `connected ssid=%s ip=%s` / 原因码透传 15/201/202/204/205 | RE_CONNECT_OK + ERR_INFO 表 | ✓ |
| @LIST | `idx=&ssid=&pass=***&pin=%d` + `count=%d source=%s` | RE_LIST_DATA + 老固件无 pin 回退 | ✓ |
| @DEL | `deleted idx=%d remaining=%d` / 400 | 期望串一致 | ✓ |
| @PRIO | `prioritized ssid=%s idx=0` / 404 / 400 / 405 | 期望串一致 | ✓ |
| @PIN | `pin idx=%d %s` / 400 | 期望串一致(on/off 裸 token 两端同构) | ✓ |
| @MODE | `mode=AP ip=192.168.4.1 ssid=..` / `mode=STA` / 405 wifi_still_starting | explain_reply 精确分流 | ✓ |
| @LED | `led pct=%d[ saved]` / 400 / 500 | pct=(\d+) | ✓ |
| @AP(/REGEN) | `ap ssid=%s pass=%s [note=applies_on_next_mode_switch]` | RE_AP_OK + note 字段 | ✓ |
| @FACTORY/@REBOOT | `rebooting` | 期望串一致 | ✓ |
| 错误码 | 400/404/405/408/409/423/500 + WiFi 原因码透传(dap_cmd.h:18-31) | ERR_INFO 完整覆盖,405 一码两义按 message 分流 | ✓ |
| 形态判别表 | dap_cmd.c 各 dap_cmd_ok 首词 | `_looks_like_reply_to` expect 表 13 条全对(键不带 @,历史 bug 已修) | ✓ |

唯一注意点:错误码分层(404=老固件 vs 408=超时 vs 500=格式不认)固件/工具/界面三层语义一致且有回归测试,这是本项目做得最好的部分。

### D. 测试质量

- 实跑结果(2026-11-01):`dap_config.py --selftest` **206/206**;`build_package.py --selftest` **5/5**;冻结 exe `无线调试器.exe --selftest` **206/206**(与源码同步)。
- 结构:协议解析(约 60 项)→ 假设备端到端(约 90 项)→ 后端路径(约 30 项)→ 关窗/设置/持久化(约 26 项)。核心价值在于"必须真调用被测代码、禁止抄逻辑进测试"的纪律,多处在注释里点名了上一版假绿。
- **变异验证(5/5 被抓住,工作区已还原干净)**:

| # | 注入的变异 | 期望 | 实际 |
| --- | --- | --- | --- |
| M1 | `parse_list_data` pin 恒置 0 | LIST 相关变红 | ✅ 2 项 FAIL(204/206) |
| M2 | 形态判别表键写成 `@SCAN`(历史 bug) | 形态判别变红 | ✅ 端到端 SCAN 自吃应答,DapError 408 整套崩溃退出 |
| M3 | `save_settings` 去掉白名单 | 白名单测试变红 | ✅ 1 项 FAIL(205/206) |
| M4 | `is_builtin_literal` 改回双向子串(历史假绿) | 凭据检查变红 | ✅ 1 项 FAIL(4/5) |
| M5 | GUI 不再显示真实 AP 密码 | GUI 显示层变红 | ✅ 1 项 FAIL(205/206) |

- 盲区:① 固件侧没有等价自测(C 代码零单测,靠工具侧 mock 隔空覆盖);② mock 与固件的应答字面量无机器对照(B-1/B-7);③ `dap_backends` 的真进程行为只测了 cmd.exe 回归一条(合理取舍)。

### E. 安全与隐私

- AP 密码:首启随机 10 位(去歧义字符表)存 NVS;@AP 只在 USB 命令通道可达(`dap_cmd.c:592-595` 明确注释"固件不暴露 TCP 版命令通道")——**知道密码的门槛 = 物理接触 + USB 线**,设计正确。@FACTORY 不换密码(设备身份),@AP REGEN 带确认。
- 凭据:`wifi_credentials.h` 已 gitignore(固件仓 `.gitignore:9`),仓库只留 .example;出厂列表为空(`wifi_configuration.h:34-41`,WIFI_LIST_SIZE=0);`check_no_credentials` 在打包最后一道做机器校验,且双向自测(M4 证明真能拦)。
- 网络暴露面:3240(DAP/elaphureLink/usbip)、1234(串口桥)均无鉴权,随 mDNS `dap.local` 广播。**结论:家庭/办公室网场景可接受**(工具手册已如实告知);若未来进更开放网络,可选加固为"首次配对 PSK",现阶段不建议做(复杂度>收益)。
- GUI 明文显示 AP 密码是功能需要(用户要看),已有"别把 12345678 当当前密码"的回归测试。

### F. 许可合规

- 仓库根 `LICENSE` = MIT(整仓自有代码)+ 第三方清单,与"整仓 MIT"决策一致。
- GPL 义务:四组件整目录复制、不与自家代码混排、每个 GPL 目录附 COPYING 且 smoke_check 核对版本全文(`build_package.py:389-400`);`flash_download_tool` 专有软件明确不入包。**这部分做得对**。
- **F-1(P2)交付声明自相矛盾**:`build_package.py:128-133` 生成的《许可与开源声明.txt》写"本工具为**专有分发**,允许个人与内部商用",而仓库 LICENSE 是 MIT(允许任意使用/再分发/售卖)。根因:`00_preparation/开源协议说明.md:54` 时代决策"PC 工具协议自定(可闭源)",后来整仓转 MIT,交付声明没跟上。用户拿到的声明**低估**了自己的权利。
- 修法:统一为 MIT 表述(一行改动 + 重打包);若有意双轨(源码 MIT、exe 专有),则需在 LICENSE 与声明里明写双轨条款。工作量:15 分钟。

### G. 工程卫生与交付

**G-1(P1)固件 17 个二开提交零远程备份**
- 证据:固件仓 origin = `https://github.com/windowsair/wireless-esp8266-dap.git`(上游),`git branch -a` 只有本地 `feature/productization`;18 个提交中 17 个是本地作者(温良),涵盖 0.5.0→0.8.0 全部二开成果。
- 风险:开发机磁盘/误操作 = 二开工作全灭,且无法向同事分发源码历史。
- 修法:加一个私有远程(private GitHub/Gitee)`git remote add backup ... && git push backup feature/productization`。工作量:**现成,10 分钟**。这是全项目 ROI 最高的一件事。

**G-2(P2)仓库 A 未推送 + 根目录垃圾**
- 2 个提交未推送(`origin/main..HEAD` = 2:f40e9fa、690f814)。
- 磁盘垃圾(均已被 .gitignore 兜住,不进 git,但占地方且碍眼):`%SystemDrive%/`(误展开目录,**48MB**)、`build_log*.txt` ×11、`flash_app_readback.bin`(641KB)、`nvs_dump.bin`、`jj400host_flash.bin`、`dumps/`、`tools/__pycache__/`。
- 修法:一次性清单删除(保留 dumps 里若还有用的一次性 dump 先确认);推送 2 个提交。工作量:10 分钟。

**G-3(P3)无 CI**:目前靠 selftest 兜底尚能活(测试真实、运行零硬件);最小 CI(GitHub Actions:两个 `--selftest` + `py_compile`)30 分钟可设,价值是防"改了协议忘跑自测"。

**G-4(P3)开发机绝对路径**:`build_package.py:37-38`、`dap_backends.py:44-45,74` 写死 `D:\ARMGCCTool\...`(注释自称可接受)。单机交付工具确实能活;若换机器打包会静默缺件——smoke_check 会拦(缺文件报错),可接受;长远可改环境变量 + 默认值。

### H. 文档一致性

**H-1(P1,疑似-高置信)用户手册 §2 两个 USB-C 口标签写反**
- 手册 `04_user/用户手册.md:25-28`:标 **COM** 的 = native USB(配网烧录插这个)/ 标 **USB** 的 = CH343。
- 工具内三处文案相反:`dap_config.py:3990`、`:3947`、`:4818` 都是"USB 线要插板子上标 **USB** 的 native 口(不是 CH343 那个)"。
- 仓库内文档(`01_hardware/开发板资料.md:177-182`)只记录了 COM18=native / COM19=CH343 的电气事实,**没有记录丝印与口的对应**——无法从仓内裁定谁对,但 3:1 的证据 + YD-ESP32-S3 常见丝印(USB=native)指向**手册写反**。此错误直接触发最高频售后问题(CH343 口一插一按 = 下载模式假死)。
- 修法:实物看一眼丝印 → 修手册 §2/§3/§5.4 → 把"丝印↔口"写进开发板资料.md 固化。30 分钟。

**H-2(P1)用户手册 §4.3 串口桥首包格式错误**
- 手册 `:98-105`:首包发 `115200 E 1`(带空格)或 `b"115200\n"`(带换行)。
- 固件实际(`uart_bridge.c:313-343`):协商行必须是**单个 2-7 字节 netbuf**、末字符 E/O/N、其余全数字且 `num_digits(baudrate)==len`——即 `115200E` 这种无空格无换行的单 token。手册的两个示例都会协商失败,**这串字节会被原样发给目标板**(相当于目标串口收到 "115200 E 1\n" 垃圾数据),波特率也不生效。
- 工具侧是正确的(`dap_config.py:4261` "首包发波特率行,例如 19200E")。
- 修法:手册改为 `115200E` / `b"115200E"`;示例代码同步改。30 分钟。

**H-3(P2)"出厂网络"相关三处过期/矛盾**
- 手册 §7 `:185`"回到固件内置的出厂网络"——实际出厂列表为空(=未配网,`wifi_configuration.h:34-41`;GUI 文案 `dap_config.py:3099` 是对的)。
- 烧录指南 `:26`"没有凭据文件时,固件使用出厂默认列表(DAP / OTA,密码 12345678)"——同源过期,现在回落到**未配网**。
- 手册 §5.5 `:159`"蓝灯慢闪……等 2 分钟没人连会自己回到 STA"——固件**没有** AP 自动回退逻辑(C2 约束:模式只经命令切换,`wifi_handle.c` 全文无此路径)。
- 修法:三处统一为"出厂=未配网,插 USB 配网"。

**H-4(P3)其余小项**:手册 §6 交付树缺 `atopenocd/`(AT32 烧录卡片的依赖);§4.2 把 AT32 流程并入"OpenOCD 卡片"描述(实际 GUI 是独立卡片);§3 "扫描连接页"应为"配网"页;"快速上手.md = 本文精简版"实为整篇拷贝。根 README 目录表同 B-6。

`tools/README.md` 本身与代码一致性极好(错误码表、@LIST 不做百分号编码、三态 OpenOCD、两条 OpenOCD 不可互替——均与实现核对相符),是全项目文档标杆。

---

## ④ 优化路线图(Top 10,按 ROI 排序)

| # | 事项 | 级别 | 工作量 | 类型 |
| --- | --- | --- | --- | --- |
| 1 | **固件仓加私有远程并推送 17 个提交** | P1 | 10 分钟 | 现成 |
| 2 | 手册 §4.3 串口桥首包格式改 `115200E` | P1 | 30 分钟 | 现成 |
| 3 | USB-C 丝印实物核对 + 修手册 §2 + 固化进开发板资料 | P1 | 30 分钟 | 现成(需看板子) |
| 4 | 交付包《许可与开源声明》对齐 MIT | P2 | 15 分钟 | 现成 |
| 5 | 仓库 A 推送 2 提交 + 根目录垃圾清理(48MB %SystemDrive% 等) | P2 | 10 分钟 | 现成 |
| 6 | `dap_config.c` 加互斥保护配置镜像 | P2 | 半天 | 现成(需重烧固件) |
| 7 | DAP_Thread 栈 2048→4096 + 真机大流量验证 | P2 | 1 小时 | 现成(需真机) |
| 8 | 拆分 `dap_config.py` 四模块 + mock↔固件格式对照自测 | P2 | 1 天 | 需重构 |
| 9 | 手册/烧录指南"出厂网络"三处统一为"出厂=未配网" | P2 | 1 小时 | 现成 |
| 10 | 最小 CI(两个 --selftest + py_compile) | P3 | 30 分钟 | 现成 |

建议节奏:#1-#5 一次做完(合计约 1.5 小时,全部是文档/运维层,零代码风险);#6/#7 合并成一次固件重烧;#8 找一个不被打扰的半天;#10 随手。

---

## ⑤ 变异验证记录

见 ③D 表。5 组变异均以 `python -c` 精确字符串替换注入(每处唯一匹配断言),跑自测取证后 `git checkout --` 还原,终态 `git status` 两仓库干净。结论:**206 项自测具备真实检出力,不存在"改了还全绿"的假绿**。

---

## ⑥ 建议人工真机验证清单(本次未执行)

1. **丝印核对**:板上 "USB"/"COM" 丝印各对应哪个口(native / CH343)——裁定 H-1。
2. **串口桥协商矩阵**:`115200E`(应协商成功)、`115200\n` 与 `115200 E 1`(应被当数据透传)——给 H-2 修订背书。
3. **DAP_Thread 栈**:usbip 挂载 + JJ400 全片擦写大流量 + 挂机 30 分钟,看有无栈溢出告警(A-2)。
4. **0.8.0 未配网首启**(板子当前正处于 @FACTORY 后的未配网态):应红灯快闪、`prov=1`、@AP 能读到随机密码。
5. **@AP REGEN 链路**:REGEN → note=applies_on_next_mode_switch → 切模式后新密码生效。
6. **1234 双客户端**:第二个连接应被 kick(`busy: kicked newcomer`)。
7. **boot 窗口**:上电后前 20 秒内连 3240/1234 被拒属正常(wifi_init 阻塞期),可补进手册排障表。

---

## ⑦ 被否决的疑点(防重复怀疑)

| 疑点 | 否决理由 |
| --- | --- |
| usbip 0x8003/0x8005 状态映射"疑似写反" | 实测 usbip attach 全链路可用,偏移与 usbip-win 实现自洽;上游遗产,只加注释勿改(A-4) |
| `usbip list -r` 挂死是 bug | 已知行为,根因是 worker 在 DEVLIST 应答后仍阻塞等 40 字节;工具侧已用固定 busid 1-1 + 界面警告双重规避 |
| `cmd_pin` 的 strstr("on") 误判 | GUI 只发 `idx=N on\|off` 固定形态,现实不可达 |
| 扫描前无条件 disconnect(300ms) | 硬约束(IDF connecting 态禁扫),注释完整,取舍正确 |
| scan 结果上限 24 条 | 配网场景够用,calloc 有界,不修 |
| selftest 里真起 cmd.exe 三轮 | 反假绿的刻意设计(注释点名上一版 inspect.getsource 炸冻结 exe) |
| 可达性 5s TTL / attached 3s 缓存可能短暂陈旧 | UX 取舍,后台线程会刷新,按钮有打旧时间戳逻辑 |
| 设置不存串口号/端口/波特率 | 正确设计(注释写明理由) |
| `wifi_init` 20s 阻塞期间无调试端口 | 刻意排序(命令通道先起,保证离线时可切 AP);仅建议手册补一句 |
| `--raw`/手动命令用 TIMEOUT_CONNECT 超时 | 手动命令含 @CONNECT 场景,放宽合理 |

---

*报告生成:ZCode 全项目体检(只读 + 自测实跑 + 变异验证);变异后工作区已确认干净。*
