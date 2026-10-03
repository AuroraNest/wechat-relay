# ARM64 Ubuntu guest

collector 和 replies 以 `User=probe` 直接运行, source_reader 直接读取 guest 内 Android Docker, 使用迁移的 `device.json` 连接现有 Relay Server. 协议、pairing 和 iPhone 保持现状. 本次不更新或操作 iPhone.

代码位于 `/home/probe/tablet-original-sync`, state 位于 `/home/probe/.local/share/aurora-tablet-relay`, ARM64 SILK decoder 位于 `/home/probe/.local/share/aurora-tablet-relay-tools/decoder`. 将本目录 `execute-reply.sh` 安装到代码目录并设为 `0700`, 两份 service 安装到 `/etc/systemd/system/`. 代码依赖和 decoder 由部署方在 guest 验证. 保留 probe 现有的最小 sudo unshare/docker 权限, 不使用 root 运行 collector, 不启用 `NoNewPrivileges=true`.

## 受控迁移

1. 先备份 host 的代码、units 和私有 state, guest units 暂不启动. 停止 host 的 collector 和 replies 两个 writer, 同时确认 guest 的同名 units 已停止. 确认没有遗留 collector、reply consumer 或手动 executor 正在运行.
2. 所有 writer 停止后, 从 host 的 `outbox.sqlite` 通过 SQLite backup API 或 `sqlite3` 的 `.backup` 生成一致快照. 完整迁移同一时点的 `device.json` 和其他 state 文件, 用 backup 结果作为 guest 的 `outbox.sqlite`, 不将原数据库的 `-wal`/`-shm` sidecar 混入快照. 保留原 state 和快照用于回滚, 不清 outbox, 不重新 pair.
3. 将 guest 私有目录设为 probe 所有且 `0700`, 私有文件设为 `0600`. 比对 device.json 的本地 checksum, SQLite `PRAGMA integrity_check`, sources/messages/assets 和未确认队列数量. 不打印配置正文或密钥.
4. 在 guest 执行 `systemd-analyze verify` 检查两份 units, 验证 probe 可直接运行 source_reader、wrapper 和 ARM64 decoder. 执行 `systemctl daemon-reload`, 再启动 guest 两个 units. host 两个 units 保持停止并禁止自动重启, 每种工作仅允许一个 host 或 guest writer.
5. 用新消息确认 guest 源读取、原图 MD5/格式/大小验证、Relay Server 的真实上传 ACK 和既有客户端收件. 当前 iPhone 由先生后续自行验收; service active 和 HTTP 成功不能替代收件验证.

## 回滚

先停止 guest 两个 units 并确认无遗留任务, 再对 guest 最新 outbox 做 SQLite backup. 若 guest 已入队或收到 ACK, 将最新完整 state 一致迁回 host 后恢复旧代码和 units, 防止丢失队列或重复覆盖确认状态. 只有确认 guest 尚未写入时才可直接使用迁移前快照. 回滚也保持每种工作单 writer, 不重新 pair 或清队列.

本机若没有 `systemd-analyze`, unit 验证必须在 guest 完成, 不将模板视为已经通过运行时验证.
