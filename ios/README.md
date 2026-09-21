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

前台通过 `/api/v1/ios/events` 接收 SSE 新消息和回复状态提示, 再从原有鉴权 API 拉取规范数据. 连接就绪和重连后补拉, 进入后台取消连接, 锁屏通知继续使用 APNs. SSE 正常时约每 30 秒核对一次消息、回复和设备状态; 断线时回退原有轮询并退避重连, 不自动重发回复. 流内不携带消息正文或密钥, 旧 PWA 事件接口不变.

原生端首次配对生成自己的新密钥, 因为旧 PWA 的密钥不可导出. Android 切换后使用新配对; 原 PWA 历史保留在原处, 不迁入本机. 配对流程不要求先授予通知权限.

服务端需部署本次接口并配置 APNs signing key、Team ID、Key ID 与 App topic, 参见 [服务端说明](../spikes/ios-web-push/README.md). App 自动注册 device token. 使用普通 alert push + Notification Service Extension, 不依赖常驻后台或 silent push.

默认通知不携带可解密预览. 开启预览后, 通知扩展仍检查本地隐私开关并验证消息 AAD. 清空本机记录不删除服务器记录, 不撤销已提交的回复. 图片按显示需要降采样至最长边 2048, 保存和分享的是该显示版本.

## 好友同步

底部为消息、好友、设置. 设备与连接入口位于设置中. 在已解锁的小米 Relay 中点击 `同步好友`, 选择微信后读取完整通讯录; 扫描期间保持微信前台. 名称数量与微信底部好友总数一致后才加密上传, 扫描失败保留上次名单. iPhone 打开好友页或下拉刷新读取最新快照.

名单按微信空间和唯一备注/昵称区分, 不保证识别改名关系. 当前每份快照最多 10000 人、每个名字 512 UTF-8 字节、正文 1 MiB, 超限拒绝而不截断. 联系人缓存加密保存在本机, 清空消息保留好友, 断开配对清除好友. 已同步好友均可进入聊天, 没有来信也能发送首条消息并在消息页继续会话.

主动发送要求服务端 schema 7 和 Android 0.6.29, 升级后在小米重新同步一次好友以生成 v2 快照. 老 v1 名单仍可查看和使用原消息回复. v4 发送请求绑定配对、设备、微信空间与快照 ID, 正文和目标名称一起加密; Android 使用保留的完整名单核对目标后执行既有会话发送流程. 名单过期或目标无法唯一确认时拒绝发送, 不自动重发. 首次同步及真实名单完整性由真机验收确认.

build 5 固定 v3/v4 加密前 JSON 字段顺序, 兼容 Android 现有解析器. Android 0.6.30 的联系人发送会区分锁屏自动化未就绪和等待微信窗口超时, 不再统一显示为不支持通知回复; 保护触发后需在小米处理, 不自动解除保护或重发.

安装成功不等于真实收发成功. 真机需分别验收配对、前台同步、锁屏通知、预览关闭、快捷回复、Android 微信实际执行、断网恢复和双微信. `已发往微信` 不代表接收方已读.
