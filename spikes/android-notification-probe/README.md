# Android Notification Probe

Android 客户端监听用于工作的个人微信通知, 将加密消息上传到 Relay, 并接收来自 iPhone PWA 的加密回复. 诊断导出会脱敏, 不记录通知正文, 联系人, 图片, 截图或回复文本.

## 构建

需要 JDK 17 与 Android SDK. 将 `relayOrigin` 指向与 PWA `PUBLIC_ORIGIN` 相同的 HTTPS Origin:

```bash
./gradlew -PrelayOrigin=https://relay.example.com testDebugUnitTest assembleDebug
```

安装 debug APK 后, 在系统设置中授予 Notification Listener 权限, 再通过 PWA 配对. `RemoteInput` 会从活动的微信通知中取得, 是默认回复路径.

## 实验性锁屏回复

Android 16 / API 36+ 的 Xiaomi debug 构建包含显式开启的 Accessibility 实验回退. 它只用于研究, release 构建不会开启.

- `RemoteInput` 可用时优先使用. 回退只在它不可用时尝试.
- 当前微信版本无法提供可校验的会话标题, 收件人标题校验暂时禁用. 因此存在向错误会话发送消息的风险.
- 锁屏时只尝试一次本地加密 PIN. 任何失败, 超时, 歧义或取消都会停止 UI 操作并重新锁屏.
- 不使用 Root, Hook, Xposed, Frida, 微信数据库或坐标手势.

不要将该实验视为对所有 Android 设备, 系统版本或微信版本的支持保证.

## 媒体

图片能力通过已验证的微信界面截图传递, 不读取或传输原图, 也不会写入系统相册. 语音消息不支持. 界面变化, 歧义, 受保护窗口或失效通知会中止该次处理.
