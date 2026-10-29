# 03_固件

设备端固件(ESP-IDF)。

## 规划(选型确认后细化)

- 起步:直接使用 windowsair/wireless-esp8266-dap 或 kerms/wireless-esp32-tools 的固件(本地编译或官方 release);第一步在 ESP32-C3、S3 两块开发板上分别刷入实测
- 二开方向(按需求优先级):
  - 配网方式(WiFi 凭据配置、AP 模式、mDNS 命名)
  - 按键/LED 交互逻辑
  - UART 透传开关与波特率
  - OTA 升级流程
  - (S3 路线)原生 USB 有线 DAP 兜底模式

## 待定项

- [ ] 基线仓库选择(windowsair 原版 vs kerms 分支)
- [ ] ESP-IDF 版本(windowsair 用 v4.4.2,kerms 更新)
