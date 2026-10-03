# 平板原始消息同步

从已登录的 Mini Android 平板微信只读采集消息和附件, 通过 Relay v6/v7 端到端加密通道交给原生 iPhone. 不把缩略图当原图, 不用语音转写替代音频. 当前适配的是 ARM64 QEMU guest / redroid 14 / 官方微信 8.0.78 的实例, 不是所有微信版本的通用适配器. 后续开发和测试仅针对 Mini 平板, 停止 Xiaomi 路线, 保留既有历史和配对.

2026-10-03 新增 v7 单聊回复, 同 pair 加密 reply-key bootstrap 和独立 UiAutomation helper, TXY/Mini 已更新, iPhone 已覆盖安装并复核 Build 11, 保留配对与历史. 后续删除全部消息/媒体定时扫描: DB/WAL 事件唤醒文字快读, 递归媒体文件事件触发附件读取/上传, 空闲 heartbeat 不查源; iPhone Build 11 删除定时刷新, 由 SSE 更新和重连补齐. 仅失败任务保留退避重试. iOS native content 后台回补避免阻塞刷新. 先前文字源到服务器实测 6181ms, 先生确认 iPhone 很快显示, 不外推附件或长期延迟. 原件未由官方微信下载时仍需等待, 真实微信回复未验收. 具体范围, 验证和运行边界见 [REPLIES.md](REPLIES.md), 完整 wire contract 见 [PROTOCOL.md](PROTOCOL.md).

## 组件

- `source_reader.py`: 在 guest 的私有 mount namespace 内建立只读视图. `--serve` 通过私有 SSH stdio 接收有界请求, 常驻进程仅缓存路径和容器 PID/start-time, 每次在只读视图内重新核对账号, 查询后再次确认账号未变化. 源 DB/WAL/SHM 的可写打开应返回 EROFS, SQLCipher 4.5.6 使用 `mode=ro`, `query_only` 和短读事务. 不使用 `immutable=1`, 不 checkpoint/rekey, 不重启微信. 媒体路径拒绝 symlink, 校验大小, 文件签名和可用哈希.
- `reader_transport.py`: 文字和媒体读取各自复用常驻 reader, SSH 使用私有 ControlMaster socket. 请求有 ID, 长度及超时限制, 失败后关闭连接并在下次请求重建. guest 图片下载中的源核验也复用 reader, 不将诊断输出混入 UI adapter 响应. 不新增监听端口.
- `collector.py`: 在 Mini 保存私有 SQLite outbox, 使用稳定消息身份去重, 由媒体事件唤醒未完成附件读取. 图片下载使用独立 worker, 避免旧图 UI 等待阻塞新文字和已缓存图片. 新图优先, 新下载和到期重试交替; 媒体读取与单附件上传交替, 防止积压上传长期占用读取队列. 首次发送前持久化 seq 和密文, 重试复用同一密文. 遇到账号变化或已有消息身份冲突时停止该轮采集.
- `audio_codec.py`: 原始 SILK 独立保留, 在受限子进程中生成 WAV 播放副本. 二者有不同 assetId, WAV 通过 `derivedFrom` 关联原件. 依赖说明见 [SILK-NOTICE.md](third-party/SILK-NOTICE.md).
- [PROTOCOL.md](PROTOCOL.md): 完整内容与预览分离, 原附件独立加密上传. Server 只保存密文. iOS 提供全文, 音频播放, 视频/文件预览和记录内附件.

## 当前验证

2026-10-02, 本地真实样本已验证语音原件 5543 bytes, WAV 171884 bytes / 3580 ms, PDF 2796797 bytes, MP4 4997631 bytes. 在一次性私有 outbox 中加密后解密, 原件 SHA-256 与只读源一致. 这些结果不等于已经到达 iPhone.

独立的 Python -> Node -> MySQL -> authenticated GET -> 解密集成测试已实际运行通过, 覆盖全文/XML, 原件与播放副本, 迟到附件, 密文幂等和 pair 隔离. iOS Core tests, App/NSE build 和来源 UI tests 已通过 GitHub Actions. 实际 iPhone 收件, 播放, 导出文件哈希和长时间运行仍需配对后验收.

图片优先按 XML 声明的内容 MD5 和对应资源长度验证原件, HD 记录通过 `reserved1` 关联. 官方微信也会把下载内容转码为 JPEG, `ImgInfo2.origImgMD5` 此时是 MD5(path-size), 不代表内容 MD5. 这类缓存只有在精确消息/会话和最高 quality row 匹配, native 完成计数等于实际文件长度, path-size key 匹配, 稳定读取及 JPEG 签名检查通过后才可采集, 并计算实际 SHA-256. 加密内容明确记录 `sourceRepresentation=native_db_decoded`, XML 声明的 `sourceByteLength` 和 `originalBytesVerified=false`, 文件名为 `wechat-decoded.jpg`. 实际 JPEG 长度可以与 XML 源长度不同, 不声称二者逐字节相同.

后台图片下载请求通过 guest 源身份和官方窗口校验, 点击不代表已下载; 必须重新读取并验证落盘文件. `iscomplete=1` 单独不足以证明完整图片存在. Native View Hierarchy fallback 绑定当前 fresh ActivityRecord 和精确原图 Button, 拒绝隐藏/禁用/多重按钮, 并在 source 复核前后要求对象与几何一致. 视频, 文件和合并记录子附件可能需要先在官方微信中打开/下载, 视频和表情不自动下载; 未下载及无法可靠识别的资源保留等待状态. 不自行请求未知 CDN 协议.

2026-10-03 图片修复实测: 已补回并加密上传两张官方完整 JPEG 缓存, 首张 45485 bytes 已由先生确认在 iPhone 平板来源显示. 第二张 260011 bytes 已上传, 手机显示未单独确认. Mini 全量 Python 110 项中 109 PASS/1 集成环境 SKIP, Mac 定向 104 项中 97 PASS/7 Linux SKIP; Mac 全量存在旧 audio limit fixture 失败, 同一失败在未改动基线复现. 本次未改 iOS 或 Server API, 不等于全部媒体验收. 当前仍有 5 张照片和 1 个表情等待, 原图按钮的父容器实际隐藏, 自动化拒绝点击; 官方 viewer 下载与剩余图片闭环仍未完成.

2026-10-03 延迟优化测量: 同一 Mini 的空结果文字读取原先一次 33.203s, 分段 profile 显示多次 Android exec 占主要时间. 合并首次元数据读取并改用常驻连接和 native 只读账号核验后, 最终发布前连续查询为 17.110s 首次连接, 2.386s/2.413s 常驻读取. 首次初始化仍有 Android 路径发现和连接开销, 故障重连也需要重新初始化. 这些是源读取耗时, 不等于 iPhone 显示时延或完整图片下载耗时; 源读取和 HTTP 完成日志分别只记录类别, 字节数和耗时. Linux 全量 124 项中 123 PASS/1 集成环境 SKIP, Mac 定向 118 项中 111 PASS/7 Linux SKIP, 包括旧下载阻塞时新缓存图片继续上传, 新图优先与重试公平, 账号切换拒绝及私有 reader 超时/重连检查.

## 安全配对和运行

待下载图片的 `requiresHD` 必须与精确源 XML 的资源选择一致. HD 在共享 150s deadline 内保持官方 viewer, 等待准确 native 按钮出现后请求一次, 避免中图仍下载时过早关闭窗口. 没有独立 HD 的普通图片直接等待官方自动下载, 不启动 HD instrumentation. 合并记录的离屏表情通过准确 activity/task 和可见列表的有界滚动触发, 最多 12 次且共享等待预算. 所有路径仍以精确来源和文件校验确认可用, 点击或打开窗口不能代替原件证据.

2026-10-03 后续验证: 图片调度优先处理新任务再处理旧重试, 文字和附件上传仍独立于 UI 下载. Mac 定向 149 项通过其中 7 项 Linux 检查跳过, Mini 和 ARM64 guest 均 149/149 通过. 记录两张 PNG 及 2373753-byte GIF 已由 Server 确认全部补齐, 最新普通图 40737-byte 预览已上传, HD 原件和新附件的 iPhone 显示仍待验收. [链路对比与迁移边界](NETWORK_LATENCY.md)记录公网慢上传与 BWG 临时转发的独立证据.

宿主使用 `/usr/bin/python3` 及已安装的 `cryptography` 41.0.7. 不将配对码, 私钥, 消息正文或源库写入 Git, 命令参数或日志. 状态默认在 `~/.local/share/aurora-tablet-relay`, 目录 0700, 文件 0600. 日志仅记录固定状态和数量.

1. iPhone 原生 Relay 选择平板来源, 连接自己的 Relay 服务并生成独立配对码. 不共用小米来源配对.
2. 在本人 Mac 的 SSH 终端运行 `bash deploy/tablet/pair-tablet.sh` (从仓库根目录). iPhone 可主动选择 "复制到我的其他 Apple 设备", 然后在隐藏输入提示处粘贴. 默认复制按钮仍只限 iPhone 本机. 不把配对码发到聊天或截图.
3. 脚本完成配对后启动已安装的 `aurora-tablet-relay.service`. 用 `systemctl --user status aurora-tablet-relay.service` 查看服务, 用 `journalctl --user -u aurora-tablet-relay.service -n 10` 查看聚合状态.

配对需要实际运行的 v6 Relay backend, guest reader, 私有 guest SSH 凭据以及已安装的宿主 service/decoder. 仓库中的 unit 是当前 Mini 布局的部署模板, 不会自行安装. guest SQLCipher 使用 Ubuntu noble 官方 4.5.6-1build2 解压版本, 保留在 guest 的私有工具目录.

配对请求发生网络错误时会保留原 deviceId/密钥以便重试, 不自动删掉重建. 已配对的状态不会被新码覆盖. 若服务已接受但响应丢失, 应先确认服务端配对状态, 不盲目删除本地密钥. `device.json` 与 outbox 必须一起保留; 删除会丢失解密/去重连续性.

## 已知边界

- 每条完整内容最多 1 MiB, 最多 8 个附件, 每个附件最多 8 MiB. 超限有明确本地状态, 不截断原文或伪造文件大小.
- 首次消息限最近 48 小时. 更早历史不改时间冒充新消息. 原附件未就绪时可能暂缓整条消息, 以保持不可变 metadata 的真实性.
- Server 原件保留 7 天, 每个 pair 声明额度 256 MiB. iPhone 缓存与 Server 保留期不同. 本地 outbox 当前保留已确认密文和去重索引, 需监测磁盘; 自动压缩/归档尚未实现.
- 所有 v6 消息明确关闭回复和会话发送能力. 新 v7 仅在密钥, 独立 helper 和单聊身份条件就绪时声明回复能力; 群聊发送不支持. 图片自动下载请求已编码, UI selector, 实际下载和真实发送留待后续验收.
- 宿主采集 unit 可恢复进程故障, 不代表现有 QEMU/Android 的宿主重启恢复已验收. 保留当前 VM, disk, NVRAM 和登录数据; 不运行首次初始化脚本重建 VM.

## 平板好友名单与新一轮测试

消息中的联系人名称不等于好友快照. 平板源的快读在启动及DB变化时额外读取完整普通微信好友名单, 不依赖新消息游标. 筛选依据当前8.0.78 APK的ContactsStorage及ContactCountView: `type&1!=0`, `type&32=0`, `type&8=0`, `verifyFlag&8=0`, `deleteFlag=0`, 排除系统项、自己及包含`@`的群/OpenIM等独立类别. 自己的username只在同一个只读事务中读取`userinfo.id=2`并用于排除, 不输出到诊断.

名单通过现有contacts API端到端加密上传, 使用v1只读快照, 不声明平板尚未实现的v4主动发好友消息能力. `settings.contacts_state`只保存绑定摘要、内容摘要、计数和待上传密文; 名单未变不重传, 网络失败和重启复用同一密文, 旧ACK不能清掉新队列. 账号变化、schema不完整、显示名冲突、超过10000项或1MiB时保留旧快照并报告固定状态, 不静默合并重名好友. 联系人上传复用后台worker, 不在文字快读路径增加HTTP等待.

准备重新测试时, iPhone顶部只选`平板`, 在`设置 -> 清空本机记录`确认. 此操作只清理所选来源的本机消息、附件缓存和回复状态, 保留配对、好友缓存、微信数据、Server/Mini记录和另一来源历史. 不选择混合来源, 不使用断开重新配对或卸载. 本机清理按已见seq建立水位, 尚未进入列表的旧待处理消息仍可能迟到; 使用新一轮测试标记区分, 不通过删除源DB/outbox解决.

每次只测试一种内容, 依次文字、图片、语音、视频、合并记录, 收件及显示/播放确认后再进入下一项. 图片普通预览与原图等待分开验收; 语音/视频以实际播放为准; 合并记录核对顺序、文字、图片、表情及迟到附件. 不以源读取、上传ACK、安装或HTTP健康代替手机验收.

2026-10-03 实测: Mini只读筛选得到32位普通好友, 快照已取得上传ACK, TXY原生设备有快照计数从0变为1. 两个units active/running/NRestarts0, pair配置未变;宿主/guest reader一致. 备份 `~/.local/share/aurora-tablet-relay-backups/contacts-20261003-01a10115`. Linux172项168PASS/4集成SKIP,独立MySQL协议4/4PASS,包括无消息时联系人上传/解密/幂等/空名单替换/pair隔离. iPhone build14无需再次安装,本人下拉后已明确确认好友名单出现. 用户已选择只清理Relay平板本机记录,尚未确认实际清理;微信和其他来源未改动.

## 检查

在本目录运行:

```bash
/usr/bin/python3 -m unittest discover -p 'test_*.py' -v
```

无集成环境时 `test_protocol_integration.py` 明确 skip. 对一次性测试环境设置 `AWR_INTEGRATION_ORIGIN` 和 `AWR_INTEGRATION_TEST_TOKEN` 才会实际执行, 不对正式 pair 运行 fixture 测试.

解码器由 `build-silk-decoder.sh <output-directory>` 构建, 固定 upstream commit 和 archive SHA-256, 保留两套许可证, 临时源码自动删除. 仅在源文件发生变更或需要重新部署时重建.

## 依据

- [SQLite 只读 WAL](https://www.sqlite.org/wal.html#read_only_databases), [SQLCipher 密钥验证](https://www.zetetic.net/sqlcipher/sqlcipher-api/#testing-the-key).
- [iOS 来源隔离](../../ios/README.md#平板--小米手机--混合来源).
- `WeChat_backup` 和 `wechat-dump` 仅为早期格式研究参考, 未将其代码拷入本 MIT 项目.
