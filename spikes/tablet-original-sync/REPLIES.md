# 平板回复代码和后续验收

## 2026-10-04 夜间收尾: 输入已确认, 发送仍受空控件树阻塞

先生确认 Build 20 可以输入. 争用修复后的新 v7 命令真实返回 WECHAT_ACTION_CHANGED, 尚无本版发送成功证据. 无发送的 inspect-recipient 复用同一联系人导航和官方 alias 门禁, 复现失败在 search_open. 微信窗口存在且 Android Awake, 但 UiAutomation 返回不可见、0x0、0 children 的空 root, 尚未到输入正文或发送点击. 旧错误提示不能证明微信入口真的改名或升级.

Java helper 已按原签名覆盖更新: 固定失败阶段, 仅内部 inspect-recipient 的脱敏控件/root 诊断; 全部 UI 操作启用完整控件读取和缓存刷新, 启动后有界等候有效 root. 现场复核仍为空, 现在明确拒绝为 AUTOMATION_NOT_READY / launch_root_ready / clicked=false. 不放宽收件人身份校验, 不猜坐标, 不自动重发旧失败消息. App 继续保留已修复的输入能力, 配对、密钥、历史和 outbox 保留.

Java 构建和旧签名验签通过, Python 发送/图片定向 81 项通过, 但不能替代真实发送. 脱敏证据保留在 `/private/tmp/relay-recipient-diagnostic-result.json` 与 `/private/tmp/relay-recipient-flags-result.json`; guest 原 helper 备份为 `~/.local/share/aurora-tablet-relay-backups/recipient-diagnostic-20261004/helper.apk`. 用户要求本版提交推送、维护知识库后停止, 今晚不再扩展排查或要求测试. 后续先恢复并验证有效的微信控件读取能力, 再做无发送导航和本人实际发送验收.

## 2026-10-04 Build 20 真机输入与发送争用修复

Build 19 部署后先生确认仍无法输入. 真实 iPhone 加密缓存仅作聚合诊断, 当时 contacts v1, bootstrap v6 停在 SUBMITTING, 18 条缓存消息均不可回复. App 普通刷新未拉 contacts, 输入框又直接绑定发送能力. 另一个独立根因是 Server 对不存在的 reply 返回 400, 而 App 只有收到 404 才提交初始化.

Build 20 已同 bundle 原位覆盖安装, 实际版本和启动复核成功, 先生确认现在可以输入. App 自动拉取 contacts v3, 未完成的旧 v6 bootstrap 切到同 key 的 v8; 显式刷新可恢复 terminal 初始化失败, 仅在确定 404 未接收时更新过期的控制命令. 草稿与发送 gate 分开, 保留身份/alias/密钥检查, 旧 name route 必须显式重新选择好友. Server 仅将已授权 GET 的缺失 reply 改为 404+no-store, wrong pair 同样不可读取.

TXY 正式镜像为 `tablet-input-ready-20261004-01a1023c`, image ID `389130976fc2e38b91e0e19588de6b907f1afba7e543a2e8b0064febb10a59ee`, readiness 正常. Mini host/guest/helper 已保留原签名和配对更新. 真机 contacts v3 共 32 名好友, 28 名有 alias; 手机缓存和 Mini 均确认 REPLY_KEY_INSTALLED, kI2A 已保存. 用户随后一条 v7 发送返回 AUTOMATION_NOT_READY; 当前 probe 确认 ui_executor_busy, 图片操作占用同一 UI 执行锁.

争用补修已原位部署: send 在 guest 有界等待锁, 新 media 为等待发送让位; host 仅对精确 ui_executor_busy+clicked=false 在原命令 120s TTL 内继续尝试, 每次复核 policy、当前命令和时效. 已运行图片操作自然结束, 结果不确定或其他 UI 失败绝不自动重发. 只短暂停回复 consumer, collector 未停止, helper 和 source_reader 未再次修改. 原配对、密钥、历史及 outbox 保留. 已请先生重新发送验收, 本次尚未获得争用修复后的真实微信收件确认.

验证: `xcodebuild ... -only-testing:AuroraRelayUITests/TabletInputRegressionTests test` 3 PASS, 最后导航修正专项 1 PASS; fixture 使用 demo:false 和 explicit URLProtocol, 覆盖待初始化输入/草稿保留、真实 contacts 解密和 bootstrap ACK 后开放发送、无 alias 禁发、旧入口重选. `npm test` 29 PASS, Mini 随机 MySQL 集成 1 PASS, 覆盖缺失 GET404/自身200/跨pair404/未认证拒绝. `python3 -B -m unittest test_reply_executor test_image_download -q` 在 Mac 和 Mini Linux 均 81 PASS. Build 20 Release development 签名与 Sandbox APNs 一致, `git diff --check` 和研究/维护仓库文件校验通过. 测试和部署不等同于真实微信收件.

证据: `/private/tmp/relay-input-ui-fixed.xcresult`, `/private/tmp/relay-input-ui-final.xcresult`, `/private/tmp/relay-build20-device.log`, `/private/tmp/relay-build20-install.json`, `/private/tmp/relay-build20-after-apps.json`. Mini 争用修复前备份 `~/.local/share/aurora-tablet-relay-backups/send-contention-20261004-01a1023c`, 初次跨端更新备份 `contact-send-20261003-01a1023c`; TXY 备份 `/etc/aurora-wechat-relay/backups/tablet-contact-send-20261003-01a1023c`. 临时脚本和测试产物保留用于复现, 私有诊断缓存副本已删除. 无 commit/push, 未迁移域名或重新配对.

## 2026-10-03 回复和好友主动发送候选

本轮候选修复回复执行器拒绝 native v8 消息的问题. 联系人快照新增独立 v3, 加密正文携带账号指纹、稳定 conversationId 和官方微信 alias, 显示名不作为收件人身份. 同名好友按稳定身份区分; 缺少 alias 时可以显示, 但不能发送.

联系人发送使用 command v7 `TABLET_CONTACT_SEND`, 无消息历史的密钥初始化使用 command v8 `TABLET_CONTACT_REPLY_KEY_BOOTSTRAP`, 两者绑定当前 pair/device/contact snapshot, 只进入 tablet consumer. 旧 phone v4 联系人发送和消息目标 v5/v6 保持兼容. App 仅在 Server 明确发布 `tabletContactSendAvailable` 后启用新路径, 不重新配对或替换已有密钥.

执行前和临点击时重新核对当前账号、原生普通好友身份及唯一 alias. 发送仍要求官方资料页 alias 精确匹配, 持久 journal 防止重复点击, 官方 UI 接受后还必须出现 watermark 后的新 outgoing source row 和非零 server ID. 群聊保持禁用, 不确定结果不自动重发. 本段描述候选协议, 不代表已部署或真实发送验收通过.

候选验收: Python 210 项中 198 PASS/12 环境或 opt-in SKIP, 回复专项 32 PASS; Swift Core 51 PASS, 两项 demo UI smoke PASS并查看回复/好友发送截图; Node 29 PASS, Mini 隔离 MySQL 集成 1 PASS. 同一临时 Server/MySQL 下, 无消息历史的 Python -> Node 联系人 bootstrap/发送/ACK 合同另行 1 PASS, source 和 UI 为合成夹具, 不是真实微信发送. Java helper 复用原构建 key 成功, 与旧构建 APK 证书相同, 尚未与已安装 helper 复核. Build 19 App/NSE Release 签名构建通过, development 签名和 Sandbox APNs 注册一致. 候选保留 canonical Build 18 的图片点击、迟到 manifest 恢复及测试, 不回退媒体修复.

发送确认按固定观测 sourceMaxId 扫完分页后才判断唯一 outgoing, 多条匹配、分页断档或 20 页预算耗尽均返回 SEND_UNCONFIRMED. policy 仍在启动 executor 前复核, 导航期间停用不立即撤销已启动操作, 临点击账号/收件人核验不等于 policy 的原子撤销.

正式部署待先生安排. 建议先同 bundle 覆盖安装兼容旧 Server 的 Build 19, 再更新支持联系人 v3 的 Server, 最后在官方 UI 空闲且没有执行中发送时更新 guest/helper 与 host collector/replies. 先备份原代码、units、配对配置和完整 SQLite 状态, 保持唯一 writer和原签名, 不清数据或重新配对. 切换后由先生分别测试已有单聊回复和无历史好友首条消息; 服务状态、测试 ACK 和 demo 不替代真实收件. 构建与 UI 证据保留在 `/private/tmp/relay-build19-device.log`, `/private/tmp/relay-ios-ui-smoke-20261003.xcresult`, 本轮改动前的维护源码备份为 `/private/tmp/relay-send-canonical-before`.

2026-10-03 已在保留原 pair/device/outbox 的前提下更新 TXY backend, Mini collector/独立 reply consumer/guest reader/helper, 并覆盖安装 iPhone Build 10. 未发送任何微信测试消息. helper readiness 和构建通过不构成真实回复验收; 本次检查时 Mini 尚未收到 reply-key bootstrap.

## 已编码的范围

- 原 pair/device/outbox 保留. iOS 在服务端明确支持后, 通过端到端加密控制命令补入原来的 `kI2A`, 不重新配对, 不删除本地数据.
- 新 v7 单聊消息可声明 reply capability. 必须存在唯一源联系人和非空 alias, collector 已有 reply key, 且独立 helper 可用. 旧 v6/已封装 v7 消息的能力保持不可变; 初始化后需新消息产生新的可回复目标.
- reply consumer 独立于媒体上传, 私有 journal 记录不可重复执行边界. 发送前复核 source account/msgId/msgSvrId/talker/alias 和当前 relay policy, 拒绝超时请求. 中文正文通过 `ACTION_SET_TEXT` 写入; 不用 `adb input text`.
- helper 只使用 Android API 34 平台 `UiAutomation` 和官方微信 UI, 固定当前 `8.0.78`. 搜索后的聊天必须通过资料页微信号与源 alias 精确匹配, 返回同一 composer 后检查正文和唯一发送按钮. 名称只用于导航, 不能代替身份.
- Java helper 和 host journal 都在点击前持久化. 任何点击后的不确定结果都不自动重发; UI 接受后继续只读核对新 outgoing source row 的 server ID.
- 发送前 source reader 在同一只读事务返回 `sourceMaxId`. helper 临点击时通过私有文件请求 guest 再次核验账号和目标, grant 绑定原始 command bytes SHA-256, reply ID 和一次性 nonce, 有效期最多 2 秒. helper 复核原 composer 和发送按钮后才提交 journal 并点击. Host 只接受 grant watermark 之后的新 outgoing row, 不把已有同正文消息算作本次成功.
- source gate 拒绝后让 helper 退出并尝试清理仍属于本命令的未发送 draft. 只清理同一 composer/window 中仍完全等于本命令正文的输入, 用户已编辑或窗口变化时保留. adapter 超时只停止独立 helper, 并有界结束本次 docker exec process group; 不停止微信, 不将不确定发送改成可重试状态.

群聊发送没有可靠 chatroom ID 的官方 UI 证据, 保持禁用. Mini 图片补齐已编码为媒体 worker 请求官方 viewer 下载, guest 再次验证源账号/消息/会话, helper 验证当前官方窗口. 点击结果不能确认下载成功, 后续必须重新读取并校验图片文件; 官方 UI 图片路径尚未验收, 缺失图片继续等待并对失败任务退避, 不恢复定时全扫描. 视频和表情不自动下载. 后续开发和测试仅针对 Mini, 停止 Xiaomi 路线, 保留原历史和配对. 旧消息缺失会话名时显示 "群聊" 或 "微信会话", 不等于已经恢复真实群名; 后续真实 source 名称或新消息需要另行验证.

source reader 保留 XML 内容 MD5/对应资源长度的原件分支, 并通过 `reserved1` 关联 HD 记录. 官方转码 JPEG 缓存按精确消息/会话和最高 quality row, native 完成计数等于实际长度, MD5(path-size), 稳定读取及 JPEG 签名共同验证, 计算实际 SHA-256. 这类附件命名 `wechat-decoded.jpg`, 加密内容记录 `sourceRepresentation=native_db_decoded`, `sourceByteLength` 为 XML 源声明长度, `originalBytesVerified=false`. native 长度可以更新为实际 JPEG 长度, 不能用 XML 长度差异或 `origImgMD5` 单独判断内容原件. 新字段不改变 Server API, 旧 iPhone 可忽略.

2026-10-03 图片修复: Mini 已补回并加密上传两张完整 JPEG 缓存, 首张 45485 bytes 已由先生确认 iPhone 平板来源可见, 第二张 260011 bytes 手机显示未单独确认. 聚合快照为 30 messages/9 assets, 仍有 5 照片/1 表情等待. Native fallback 只接受 current fresh ActivityRecord 中可见且可点击的官方 `cnb` Button, 所有 ancestor 可见且边界有效, source 复核前后对象/几何不变才点击. 实际 HD Button 的 `OperationLayerWrapper` 为 GONE, 已正确拒绝, 不加入盲点图片/固定坐标操作. 临时 touch exploration 设置未恢复可访问节点, 已移除. 图片自动下载和全部旧图片仍未完成; 单张实际显示不能替代剩余验收. Mini 全量 Python 110 项中 109 PASS/1 集成环境 SKIP; Mac 定向 104 项中 97 PASS/7 Linux SKIP, Mac 全量旧 audio limit fixture 在未改动基线同样失败. 原 pair/device/outbox/VM 和历史保留, 未代发微信或改 Server API/iOS.

本次同时补上 iOS 媒体消息缺少原件时的正文/类型占位和详细信息入口, 避免空气泡. 当前 source reader, guest adapter, host consumer 和 helper 必须一起更新; 旧 reader/helper 缺少 watermark/grant 证据时发送失败关闭. 临点击 source 核验缩小账号切换窗口, 不是官方 UI 自身账号的原子身份凭证. helper 异常被强制停止时 draft 可能保留, 不自动清除来源不明的输入. 兼容和失败分支的隔离 fixture 已运行, 官方微信 UI 的真实发送仍未实测.

## 部署顺序和运行边界

1. 先审阅和验证新版 backend 与 iOS. Backend 支持 v6/v7, 独立 tablet queue 和新的 ACK, 并在 iOS status 发布能力标记. iOS 对旧 backend 不发 bootstrap.
2. 保留 Mini 的 `device.json`, `outbox.sqlite` 和原 pair. 更新 collector/source_reader/reply_executor 代码, 然后使用独立 `aurora-tablet-replies.service` 模板; 在 helper 尚未安装时仍可处理 bootstrap, 发送保持禁用. 收件 unit 和 reply unit 使用各自锁与 SQLite connection.
3. 在具备已安装 Android SDK 34/build-tools 34.0.0 和 JDK 的 Mac 上运行 `bash spikes/tablet-original-sync/android-reply-runner/build.sh`. 默认查找 JDK 17, 可显式设置 `JAVA_HOME`; 本次用已有 OpenJDK 25 构建. 脚本只使用现有平台工具, 输出 ignored `build/aurora-tablet-ui.apk`, 不下载依赖. helper 使用私有 debug key 以支持受控的 shell `run-as`, 没有对外 exported 发送入口.
4. 将 APK 覆盖安装到当前已登录的 guest Android, 把 `guest_reply.py` 与 source_reader 放在同一目录. 不重新初始化 VM, 不清微信数据. `deploy/tablet/execute-reply.sh` 把私有 stdin 原样经现有 guest SSH 传递; guest 将 payload 写入 helper 的私有 files 目录, `am instrument` 参数只带组件名, 完成后清除临时 payload/result. Docker exec 需显式设置 Android/APEX runtime 环境, classpath 只读取 init 导出的两个字段.
5. 部署模板中的 collector `--reply-executor` 和独立 reply unit, 先检查 `probe`/`inspect` 的聚合输出. `probe` 只确认 helper/版本, 不证明资料页 selector 或实际发送已通过. `inspect` 不导航、不填字、不点击, 仅统计当前微信可访问节点. helper `send` 会实际操作官方微信, 必须留给明确安排的真实验收.

图片补齐 helper 使用 versionCode 3, 固定官方微信 `8.0.78` / versionCode 3180. reader, collector, guest adapter 和 helper 需一起更新; readiness 不替代官方 UI 下载和落盘校验.

## 检查和已完成验证

2026-10-03 事件同步接续: guest 使用 Linux inotify 监听只读视图中的 DB/WAL 和递归媒体目录, 150ms 合并写入事件, 固定 sourceChanged/mediaChanged 通知经私有 SSH 唤醒 collector. 删除15秒补查和定时媒体扫描, idle heartbeat 为 false,false 且不查源. 事件不携带正文/账号/路径; 每次实际读取仍验证源账号和消息身份. watcher 中断/overflow/目录失效重建全树, 不把文件事件本身视为消息提交或手机收件. 媒体事件对当次 pending snapshot 分20条消费一次, 覆盖旧附件但不空闲重复扫描.

iPhone Build 11 删除前台4秒/30秒刷新循环, 首次启动/连接读取后建立 SSE, ready/message/asset 事件刷新, 重连补齐遗漏. pending pair 在事件到达时重新确认状态. 保留连接失败重试和已请求附件失败重试, 不定时发现新消息. Build11已签名构建; 初次手机 unavailable 安装失败, 先生恢复连接后原位覆盖安装成功, 设备元数据复核1.0(11), 保留原数据. 真实新版媒体收件未验收.

collector 文字快读不扫描媒体目录/哈希/文件字节, 非 type1 消息只登记原 source ID 和等待状态, 后台全量 reader 补齐, 不伪造附件 metadata. 旧媒体读取和附件上传在一个后台 worker 中执行, 消息 ingest/seq/密文封装仍由主线程串行完成. 附件上传失败单独退避 15 秒, 不占住新文字或媒体读取. 已有 reply key 时首批消息前确认 helper readiness, 后续后台更新 readiness; 实际发送仍重新执行原安全检查.

Mini 本次全量 Python 76 项中 75 PASS/1 无集成环境 SKIP, 包含 Linux 原生 DB/WAL 写入/文件替换/监听失效和旧媒体阻塞期间文字独立上传的检查. 两个 units active/NRestarts0, guest watcher 实际启动通过. 最新真实文字 seq26 从源时间到 TXY 接收为 6181ms, 先生确认 iPhone 平板来源很快显示, 本次文字收件链路通过. 此前 seq24/25 为旧逻辑和更新时段样本, 分别约 41/66 秒; 不混作新版稳态指标. 诊断时并发只读快读约 18-24 秒, 尚非长期负载/后台推送/所有媒体端到端验收. 本次备份在 Mini `~/.local/share/aurora-tablet-relay-backups/event-sync-20261003-01a0ffb1`, 未改 iPhone Build 10 或 TXY 代码.

```bash
cd spikes/tablet-original-sync
python3 -B -m unittest test_image_download test_reply_executor test_collector test_source_reader -q
# Full Linux regression includes native watcher and audio resource limits.
python3 -B -m unittest discover -q
```

```bash
cd spikes/ios-web-push
npm test
```

后端 MySQL integration fixture 需要一次性测试数据库, 不对正式 pair 运行. 新增 fixture 覆盖 v6/v7 版本绑定、隔离旧 Android consumer、同 pair bootstrap、重复 claim/ACK 与 terminal 状态不可回退. Python fixture 覆盖 key 冲突、账号变化、群聊拒绝、source 读取后请求过期、policy 暂停、点击中断不重发和缺少 source 成功证据. Swift Core fixture 覆盖旧 server capability 默认关闭、v6 只读、bootstrap 派生密钥和 account/talker 加密绑定.

2026-10-03 接续增加 watermark, 原有同正文 outgoing, 临点击 gate digest/nonce/expiry 和账号变化的 Python fixture. Mini Linux 全量 Python 63 项中 62 PASS/1 无集成环境 SKIP; 独立 Python -> Node -> MySQL 集成另行 1 PASS. Node 28 PASS/1 集成 SKIP, 独立 MySQL 集成另行 1 PASS, `npm run build` 通过. Swift Core 36 PASS, App/NSE 模拟器及真机构建和签名核验通过. 5 项基础 UI 检查分两次通过, 紧凑 Inbox/来源切换追加两项通过. Mac 全量 Python 的音频资源限制检查因 Darwin 缺少 `RLIMIT_AS` 失败, Linux 同一检查通过.

文字同步修复: collector 在回扫旧待下载附件之前上传本轮新文字, 未收到 reply key 时跳过 helper readiness probe; 对应顺序回归 13 项 collector 检查通过. iOS 将 native content 回补移到可取消的单任务, metadata 刷新和 event stream 不再等待串行旧内容请求. 最新两条真实文字已由 Mini 上传, TXY native 序号达到 23; iPhone Build 10 实际展示仍待确认. 分页最新页没有更早记录时清除旧 `hasMore`, 消息页去掉重复来源 badge, 保留全局来源选择和搜索.

TXY 使用本机 image `tablet-v7-20261003-01a0ffb1`, schema 8 未变, 公网 readiness 通过. 未推送 image 或新 Git commit. 配置和 MySQL 备份在 `/etc/aurora-wechat-relay/backups/tablet-v7-20261003-01a0ffb1`. Mini 两个 user units active, helper protocol 2 READY; `inspect` 当前可访问微信节点为 0, 不能据此声称 selector 已验收. Mini 状态备份分别在 `~/.local/share/aurora-tablet-relay-backups/tablet-v7-20261003-01a0ffb1` 和 `text-sync-20261003-01a0ffb1`.

```bash
cd ios
swift test
```

Swift 需要与当前 SDK 匹配的工具链及可写 module cache. Java helper 还需构建和目标环境只读 UI selector 检查. 最后再安排单聊中文发送、同名联系人、切换账号、UI 打断、网络丢 ACK 和原件显示的端到端验收. 未经这些步骤, 不宣称平板回复和图片原件已经全部解决.
