# Android Notification Probe

Android 客户端监听用于工作的个人微信通知, 将加密消息上传到 Relay, 并接收来自 iPhone PWA 的加密回复. 诊断导出会脱敏, 不记录通知正文, 联系人, 图片, 截图或回复文本.

## 构建环境

需要 JDK 17 与 Android SDK. 默认环境是 development. 复制本地配置模板, 填写自己的 HTTPS Origin 后构建:

```bash
cp relay.development.properties.example relay.development.properties
./gradlew -PrelayEnvironment=development testDebugUnitTest assembleDebug
```

`relay.development.properties` 只保存本机开发环境的 `relayOrigin`. 构建会从 properties 读取该值. `-PrelayOrigin=https://relay.example.com` 可显式覆盖它.

要构建 production 环境, 使用独立模板并显式选择:

```bash
cp relay.production.properties.example relay.production.properties
./gradlew -PrelayEnvironment=production testDebugUnitTest assembleDebug
```

环境只选择 Relay 地址. 它不改变 applicationId, 签名或构建产物覆盖规则. 不要将任意 debug APK 当作已有安装包的替代品.

安装 debug APK 后, 在系统设置中授予 Notification Listener 权限, 再通过 PWA 配对. PWA 的 `PUBLIC_ORIGIN` 与 Android 的 `relayOrigin` 必须完全相同. iPhone 与 Android 实际测试需要你自己的 HTTPS Origin.

## 实验性锁屏回复

Android 16 / API 36+ 的 Xiaomi debug 构建包含显式开启的 Accessibility 实验回退. 它只用于研究, release 构建不会开启.

- `RemoteInput` 可用时优先使用. 回退只在它不可用时尝试.
- 通过联系人快照发起会话时, 搜索结果、进入会话后的标题、输入框和发送按钮都必须唯一且匹配. 任一校验失败会停止操作.
- 锁屏时只尝试一次本地加密 PIN. 任何失败, 超时, 歧义或取消都会停止 UI 操作并重新锁屏.
- 不使用 Root, Hook, Xposed, Frida, 微信数据库或坐标手势.

不要将该实验视为对所有 Android 设备, 系统版本或微信版本的支持保证.

## 媒体

图片能力通过已验证的微信界面截图传递, 不读取或传输原图, 也不会写入系统相册. 语音消息不支持. 界面变化, 歧义, 受保护窗口或失效通知会中止该次处理.
