# Relay iOS

原生 iPhone 客户端最低支持 iOS 17. `AuroraRelay` 产物显示为 `Relay`, `NotificationService` 是通知内容扩展. 两者共享本地 Swift Package `RelayCore` 和 Keychain access group `$(AppIdentifierPrefix)com.auroramaple.wechatrelay`.

## 本地构建

每次命令行构建指定完整 Xcode, 不需要修改全局 Command Line Tools 设置:

```sh
cd /Users/aurora/Aurora/wechat-relay
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project ios/AuroraRelay.xcodeproj -scheme AuroraRelay -sdk iphonesimulator -destination 'platform=iOS Simulator,name=iPhone 17 Pro' CODE_SIGNING_ALLOWED=NO build
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --package-path ios
```

首次打开工程时, Xcode 会解析 `ios/Package.swift` 的本地 `RelayCore`. App 和扩展使用 filesystem synchronized groups. 模拟器名称需与 `xcrun simctl list devices available` 的本机列表一致.

Debug 加 `--demo` 启动参数可使用纯本机虚构数据检查界面, 不连接服务或保存真实配对. Release 不启用此入口. 普通启动显示欢迎与配对流程.

开发版欢迎页也提供 `浏览界面演示`. 模拟器交互检查:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project ios/AuroraRelay.xcodeproj -scheme AuroraRelay -destination 'platform=iOS Simulator,name=iPhone 17 Pro' CODE_SIGN_IDENTITY=- test
```

欢迎页和配对测试需保留模拟器 ad-hoc 签名, 共享 Keychain 不接受无签名 App. 上面的无签名 build 仅验证编译.

## 真机安装

1. 用 Xcode 打开 `ios/AuroraRelay.xcodeproj`, 为 App 和扩展选择同一 Development Team.
2. 保持 Automatic Signing, 连接已信任的 iPhone, 开启手机的 Developer Mode, 选择 `AuroraRelay` scheme 后 Run.
3. Developer Portal 的 App ID 需要启用 Push Notifications. Debug 使用 sandbox APNs, Release 使用 production APNs.

可在已忽略的 `ios/Configuration/Signing.local.xcconfig` 中写 `DEVELOPMENT_TEAM = 你的TeamID`. Debug/Release 配置会自动包含此文件, 不需要替换 base configuration. 不要提交密钥、描述文件或账户信息.

## Push 接入边界

原生端首次配对生成自己的新密钥, 因为旧 PWA 的密钥不可导出. Android 切换后使用新配对; 原 PWA 历史保留在原处, 不迁入本机. 配对流程不要求先授予通知权限.

服务端需部署本次接口并配置 APNs signing key、Team ID、Key ID 与 App topic, 参见 [服务端说明](../spikes/ios-web-push/README.md). App 自动注册 device token. 使用普通 alert push + Notification Service Extension, 不依赖常驻后台或 silent push.

默认通知不携带可解密预览. 开启预览后, 通知扩展仍检查本地隐私开关并验证消息 AAD. 清空本机记录不删除服务器记录, 不撤销已提交的回复. 图片按显示需要降采样至最长边 2048, 保存和分享的是该显示版本.

安装成功不等于真实收发成功. 真机需分别验收配对、前台同步、锁屏通知、预览关闭、快捷回复、Android 微信实际执行、断网恢复和双微信. `已发往微信` 不代表接收方已读.
