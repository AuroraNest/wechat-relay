# 平板原始消息同步

从已登录的 Mini Android 平板微信只读采集消息和原附件, 通过 Relay v6 端到端加密通道交给原生 iPhone. 不把缩略图当原图, 不用语音转写替代音频. 当前适配的是 ARM64 QEMU guest / redroid 14 / 官方微信 8.0.78 的已验证实例, 不是所有微信版本的通用适配器.

## 组件

- `source_reader.py`: 在 guest 的私有 mount namespace 内建立只读视图. 源 DB/WAL/SHM 的可写打开应返回 EROFS, SQLCipher 4.5.6 使用 `mode=ro`, `query_only` 和短读事务. 不使用 `immutable=1`, 不 checkpoint/rekey, 不重启微信. 媒体路径拒绝 symlink, 校验大小, 文件签名和可用哈希.
- `collector.py`: 在 Mini 保存私有 SQLite outbox, 使用稳定消息身份去重, 对未完成附件轮转回扫. 首次发送前持久化 seq 和密文, 重试复用同一密文. 遇到账号变化或已有消息身份冲突时停止该轮采集.
- `audio_codec.py`: 原始 SILK 独立保留, 在受限子进程中生成 WAV 播放副本. 二者有不同 assetId, WAV 通过 `derivedFrom` 关联原件. 依赖说明见 [SILK-NOTICE.md](third-party/SILK-NOTICE.md).
- [PROTOCOL.md](PROTOCOL.md): 完整内容与预览分离, 原附件独立加密上传. Server 只保存密文. iOS 提供全文, 音频播放, 视频/文件预览和记录内附件.

## 当前验证

2026-10-02, 本地真实样本已验证语音原件 5543 bytes, WAV 171884 bytes / 3580 ms, PDF 2796797 bytes, MP4 4997631 bytes. 在一次性私有 outbox 中加密后解密, 原件 SHA-256 与只读源一致. 这些结果不等于已经到达 iPhone.

独立的 Python -> Node -> MySQL -> authenticated GET -> 解密集成测试已实际运行通过, 覆盖全文/XML, 原件与播放副本, 迟到附件, 密文幂等和 pair 隔离. iOS Core tests, App/NSE build 和来源 UI tests 已通过 GitHub Actions. 实际 iPhone 收件, 播放, 导出文件哈希和长时间运行仍需配对后验收.

图片只有在完整原图落盘且符合原图 metadata 时才就绪. 微信 `iscomplete=1` 不足以证明原件存在. 视频, 文件和合并记录子附件可能需要先在官方微信中打开/下载; 当前采集器不会自动操作微信 UI 或自行请求未知 CDN 协议. 未下载及无法可靠识别的表情资源保留等待状态.

## 安全配对和运行

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
- 平板采集目前只读, 所有 v6 消息明确关闭回复和会话发送能力.
- 宿主采集 unit 可恢复进程故障, 不代表现有 QEMU/Android 的宿主重启恢复已验收. 保留当前 VM, disk, NVRAM 和登录数据; 不运行首次初始化脚本重建 VM.

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
