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

## 平板 / 小米手机 / 混合来源

顶部 `消息来源` 菜单提供 `平板`, `小米手机`, `混合`, 首次默认平板, 之后保留本机选择. 未连接的平板显示配对入口, 不会把旧小米连接当成平板. 切到小米可继续查看旧记录. 两个来源分别生成配对码: 小米在 Android Relay 导入, 平板由已配置的平板接入端导入; iPhone 本身不实现微信 iPad 协议登录.

每个来源独立保存 session, pending pairing, 通知预览设置, 消息密钥, 回复密钥, 加密缓存, 翻页/SSE 游标和服务端转发策略. 同一 APNs device token 可分别注册到两个 iOS session. 混合模式按时间合并显示, 来源标签始终保留, 同名联系人不会合并. 会话 ID 包含来源, pairId, 微信空间及名称, 回复, 主动联系, 附件请求和重试只路由至原来源. 设置中的混合管理入口要求先选一个来源再修改其配置或断开配对.

升级时只把旧 `session`, `pending-pairing`, `preview-enabled` 和 `inbox.sealed` 迁入小米来源. 先复制 Keychain/缓存, 成功后删除旧 session 入口, 重启不会复活已断开的配对. 新缓存名包含来源与 pairId, 原 AES-GCM AAD 与密钥不变; 历史会话及已读游标加载时增加命名空间. 冲突或读取失败保留已有内容并提示错误. 不重新配对小米, 不删除另一来源.

只有所选来源运行前台 SSE/轮询并注册 APNs, 切换后注销非所选来源的 token, 中继采集仍继续. 同步推送注册失败会保留错误并重试. 已在途/已送达通知无法从服务端撤回, NSE 不具备可靠静默丢弃 alert 的能力: 非所选来源仍可能短暂收到无内容提醒. 重新选择来源可能收到服务端排队的补推. NSE 按 payload `pairId` 唯一匹配 session, 未知或冲突 pair 不解密; 预览仍同时要求来源选择, 本机预览开关及 payload 预览许可. 通知点击和已送达通知的快捷回复始终沿原 pair 路由.

### 构建与验收接手

Mac 若有尚未推送的 build 6 或其他本地改动, 先保存这些改动再集成本次差异, 不直接覆盖目录. 本次从当前仓库已有 iOS 源码修改, 没有改 App 版本号, 签名或部署生产服务. 从仓库根目录执行:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test --package-path ios
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project ios/AuroraRelay.xcodeproj -scheme AuroraRelay -destination 'platform=iOS Simulator,name=iPhone 17 Pro' CODE_SIGN_IDENTITY=- test
```

新增 `RelaySourceTests` 覆盖默认选择, 同名会话隔离, 未知/歧义通知路由, 旧 Keychain 与缓存迁移及迁移冲突保留. UI 测试在 `--demo` 的两个独立虚构来源中发送不同回复, 验证切换隔离, 混合双会话和切回记录. 演示不访问服务或保存真实配对, 不能替代真实消息收发验收.

当前 Linux 环境没有 Swift/Xcode, 上述测试和 iOS 编译尚未在此环境运行. Mac/CI 构建后还需真机验证: 旧版升级保留小米历史, 平板独立配对, 混合同名好友分别回复/附件, 各来源通知预览与快捷回复, 切换时离线后恢复, 双来源分别断开和重新配对. 平板接入端是否已完成真实登录, 采集, 联系人和发送能力需独立验收; iPhone 不伪造这些来源端能力.

## v6 原始正文与附件

兼容旧预览/图片消息. 新消息通过 `hasNativeContent` 与独立 `nativeAssets` 声明原始内容, 消息列表不包含完整正文密文. App 使用当前来源的 pair/session 单独 GET `/api/v1/ios/messages/:messageId/content`, 校验 `phase2-content` AAD 后解密. 正文允许至 1 MiB, 不用 600-byte 通知预览替代正文. 前台每轮最多补取 8 条缺少正文的消息, 打开消息可立即请求该条. 正文保存在原有来源/pair 的加密 inbox cache 中.

会话身份使用原始稳定 `conversationId`, 加上 source, pairId 和微信空间, 同名或改名不会造成跨会话合并. 群内 `senderName` 独立显示. 可选 `isOutgoing=true` 表示本人已发送的原消息, 在右侧展示且不计未读; 缺省按收到消息处理. v6 只读, 不借用旧通知的回复入口. 完整文本和嵌套记录提供的文本直接展示, `rawXML` 可查看或分享, 始终按纯文本处理, 不运行 HTML/JavaScript. 没有提供的记录内嵌文件不会伪装成可下载附件.

原始附件走 GET `/api/v1/ios/assets/:assetId`, 校验消息/asset/设备/seq/时间/空间/kind/MIME/大小/role/derivedFrom 全部 AAD 字段, 校验实际字节数, 若加密正文提供 SHA-256 则同时验证. 每附件上限 8 MiB, 每消息至多 8 件. `original` 和 `playback` 分开显示, 衍生播放版本必须指向同消息中同种类的原件, 不覆盖原始字节. 图片原件可保存和分享; 屏幕预览降采样到 2048, 原件分享不降采样. 音频/视频使用 AVPlayer, PDF/文本等使用系统文件预览. iPhone 无法播放的原始编码 (例如 SILK) 明确提示保存原件, 不转写为文本, 不隐式转码.

附件 cache 仅保留密文 envelope, 按 source/pair/message/asset 分开, 每来源/pair 上限 128 MiB并淘汰较旧缓存. 播放/文件分享需要本机临时明文文件, 这些文件使用完整 Data Protection, 在下一次 App 启动以及清空记录/断开对应来源时清除. 临时文件名只取已解密名称的安全 basename. 用户通过系统分享另存的副本由所选目标应用管理.

已声明但未上传完成的附件返回 `409 ASSET_PENDING`, 到期返回 `410 ASSET_EXPIRED`. 图片显示时请求, 其他类型点下载后请求. pending 附件按 0/5/15 秒间隔重试, 每个可见卡片每轮前台至多 6 次; 前台同步完成可触发剩余次数, 手动重试或 App 重新进入前台重置次数. 不依赖新的消息 seq. 到期/其他失败不自动循环下载. 切后台/离开卡片会取消其等待或请求.

新增 `RelayNativeContentTests` 验证旧消息兼容, 全文和记录/XML保真, 自发消息方向, 稳定身份与source/pair隔离, 原始SILK字节保真, 元数据/AAD/密钥/摘要/长度拒绝, playback关系及409/410/404状态. `--demo --demo-native` 可查看纯虚构的完整引用/记录/文件, UI 测试检查完整正文, 安全XML文本, 原件下载/分享入口和混合来源隔离. 当前 Linux 无 Swift/Xcode, 这些新测试及原生编译仍需 macOS CI执行. 真实微信附件是否可提取, 原始格式是否可播放, APNs跳转和实际真机保存仍需来源端与真机联合验收.
