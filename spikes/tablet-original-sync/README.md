# 平板原始消息同步研究

2026-10-02 的目标是从已登录的 Mini Android 平板微信读取原始消息和附件, 经 Relay 加密通道交给原生 iPhone 客户端. 包括原始语音, 不以转写, 截图或转发到共同群替代. 本目录记录研究结论, 尚无正式采集服务.

## 已取得的本地证据

运行环境为 Mini 上 ARM64 QEMU guest 内的 redroid 14, 官方签名微信 8.0.78. 用户确认平板登录成功且手机和 Mac 会话保留. 长期在线和宿主重启恢复尚未验收.

官方 SQLCipher 4.5.6 已读取当前 `EnMicroMsg.db` 的私有诊断副本, `sqlite_master` 查询与 `PRAGMA quick_check` 通过. 数据格式使用 1024 字节页, PBKDF2-HMAC-SHA1/4000 和 HMAC off. 密钥在本地派生, 仅经进程 stdin 传入, 不保存到代码或日志. 此证据针对当前实例, 不能推广为所有微信版本都可读取.

| 类型 | 当前证据 | 尚未完成 |
| --- | --- | --- |
| 文字 | `message` 中存在并可查询 | 增量采集和 iPhone 真实收件 |
| 图片 | `ImgInfo2` 可关联消息, 找到 JPEG 缩略图 | 原图下载, 原文件关联和逐字节验收 |
| 表情, 引用, 链接 | 存在对应消息/XML类型 | 完整解析, 资源获取和原生展示 |
| 语音 | `voiceinfo.MsgLocalId` 提供关联字段, 当时0行 | 实际样本, 原始音频获取和播放 |
| 视频 | `videoinfo2.msglocalid` 提供关联字段, 当时0行 | 实际样本, 原文件获取和播放 |
| 文件 | `appattach.msgInfoId` 提供关联字段, 当时0行 | 实际样本, 原文件获取和打开/分享 |
| 合并聊天记录 | 尚无完整样本 | 嵌套消息, 媒体关联及展示 |

`iscomplete=1` 不能单独证明原图已经下载. 数据库可读也不能证明远端媒体已存在于平板文件系统. 缺少原附件时应保持等待状态, 不把缩略图标为原图.

SQLCipher 使用 Ubuntu noble 官方 `sqlcipher/libsqlcipher1 4.5.6-1build2`, 仅解压到 guest 的私有诊断目录. 该版本复用已有 OpenSSL 3, 通过官方兼容参数读取旧格式, 没有系统安装或重启服务, 尚未成为 Relay 运行依赖.

## 采集路线和一致性边界

平板本地消息库/媒体 -> Mini 只读增量采集 -> 扩展既有端到端加密协议 -> iPhone 原生展示.

诊断时只读取得 DB/WAL, 再重复读取比较, 在私有临时目录内查询副本, 结束即删除. 双读相同和 `quick_check` 通过可以支持格式研究, 不能保证在线事务绝无遗漏. 私有只读 bind 视图试验未成功, SQLCipher 在线只读事务未执行. 未修改源库, 未 checkpoint/rekey, 未暂停或重启微信.

正式读取应使用 `SQLITE_OPEN_READONLY`/`mode=ro` 和短事务, 或经过验证的 Online Backup API. 源目录应提供只读视图, 遇到锁或只读限制就退出重试, 不降级为可写访问. 不在活跃数据库上使用 `immutable=1`.

增量不能只按 `rowid` 递增. 稳定消息键, 重叠回扫和待下载附件状态必须覆盖旧消息更新及迟到附件. 原文件按字节保留, 用大小与 SHA-256 验证传输一致性. 若 iOS 播放需要派生格式, 原文件仍应保留, 派生文件不能冒充原始附件.

## 与本次 iOS 修改的关系

[iOS 来源选择](../../ios/README.md#平板--小米手机--混合来源)已提供平板/小米手机/混合, 默认平板, 两端独立配对与缓存, 混合视图按来源路由回复和附件. 来源选择不产生平板采集能力, 当前还不能通过平板配对码完成原始收件.

现有 Relay 附件协议和 iOS 解码仅接受 JPEG/PNG/WebP 图片. 原始语音, 视频, 文件和合并记录还需版本化的完整内容/通用附件协议及原生展示. 服务器继续只接收密文, 不把微信数据库, 登录材料或媒体明文上传. 本地库读取也不提供微信原生发送能力, 不应据此启用未实现的回复标志.

下一阶段先验证在线只读事务和真实原媒体样本, 再实现一条原始文字加原文件的端到端链路. 验收包括会话/发送者/顺序, 源文件与 iPhone 导出文件一致性, 连续来信, 断线补收, 迟到附件和重复事件去重. 原始收件完成前, 不把来源菜单或数据库诊断视为替代小米成功.

## 上游依据

- [SQLCipher 密钥验证](https://www.zetetic.net/sqlcipher/sqlcipher-api/#testing-the-key): 实际查询 schema 才能验证密钥, 设置 PRAGMA 本身不足.
- [SQLite 只读 WAL](https://www.sqlite.org/wal.html#read_only_databases)与[Online Backup API](https://www.sqlite.org/backup.html): 正式一致性读取的依据.
- [WeChat_backup](https://github.com/mkcs121/WeChat_backup)仅用于格式研究, 未见明确许可证, 没有复制代码或执行暂停微信的脚本.
- [wechat-dump](https://github.com/ppwwyyxx/wechat-dump)为 GPL-3.0 解析参考, 未拷入 MIT Relay, 不作为当前8.0.78的兼容保证.
