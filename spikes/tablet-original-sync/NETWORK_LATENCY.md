# Relay 链路测速与迁移建议

2026-10-03. 先完成临时测速, 随后先生授权常驻改造. 当前已启用 Mini -> BWG -> TXY 的 Relay 专属通道. 未迁移服务、修改 DNS 或操作 iPhone.

## 常驻部署结果

`aurora-tablet-bwg.service` 使用既有 SSH 身份和私有 Unix socket. collector/replies 的独立 drop-in 设置 `AWR_RELAY_HTTPS_SOCKET`, 不影响其他应用. TLS/SNI/证书和请求签名保持原目标, 禁用此路径上的环境代理及 redirect, 故障不静默回退直连, 队列按原逻辑保留并重试.

Mini `python3 -m unittest discover -q`: 190 tests, 186 PASS/4 integration SKIP. systemd unit verify 通过. 常驻通道同一 3165459-byte 附件在启用后 1.819s、仅隧道主进程中断并自动恢复后 1.785s、正式 collector 切换后 1.888s, 均 exact idempotent ACK. 故障演练在业务切换前完成, 未暂停原采集服务. 正式切换仅短暂重启两个业务 units 应用环境, 三个 units active/running, 配对文件 checksum 一致.

备份: Mini `~/.local/share/aurora-tablet-relay-backups/bwg-route-20261003-01a10171`. 部署、恢复和回滚见 [BWG_ROUTE.md](../../deploy/tablet/BWG_ROUTE.md). 未清空历史/outbox, 未代发微信. 手机新文字/后台通知/图片/记录显示由先生亲自测试; 本轮 ACK 不替代该验收, HD 官方原图下载仍可能等待.

下一步目标按先生要求改为 TXY 服务迁到 BWG. 本次只记录目标, 尚未迁移数据库、附件、域名或 APNs 配置.

## 实际结果

对同一个已经上传成功的 GIF 附件重传完全相同的加密请求, 请求体 3165459 bytes. API 的幂等 ACK 校验了原附件 ID, 不新增聊天消息或通知.

| 路径 | 结果 | 请求耗时 |
| --- | --- | --- |
| Mini 宿主直连 TXY | socket timeout | 157.485s |
| ARM64 VM 直连 TXY | exact ACK, idempotent=true | 176.060s |
| Mini -> BWG -> TXY, 第一次 | exact ACK, idempotent=true | 1.794s |
| Mini -> BWG -> TXY, 第二次 | exact ACK, idempotent=true | 1.736s |

临时 SSH 通道建立另用 0.702s. Mini 复用既有 BWG SSH 身份, 只建立私有目录里的 Unix socket, 没有新增 TCP 监听. TLS 仍在 Mini 与 TXY 间完成, 保留原域名 SNI, 证书和请求签名校验. `finally` 已结束 tunnel 并清理 socket.

直连上传 TCP 快照曾出现约 220ms RTT, cwnd=3, 约 103kbps delivery rate 和持续重传. Mini 位于纽约, 不能仅凭 RTT 判断代理或断言 TXY 应用过载. 合成 3165459-byte 无认证请求从 BWG 到 TXY 1.538s 收到 HTTP 400, Mini 直连 46.469s 超时; 此项仅支持链路判断, 不等于有效附件验收.

## 建议

优先为现有 collector 配置仅面向 Relay 的 BWG 转发通道, 保持 TXY 数据库和现有 iPhone origin. 先前只把 collector 移进 VM 没有解决慢上传: 常驻源读取单样本 host SSH 5.85s/guest 6.05s. 直传候选和 ARM64 SILK 已准备并通过 149 项 guest 回归及真实语音输出一致性验证, 但尚未启动或迁移配对/outbox.

上述方案已实施, 见顶部常驻部署结果. 短时上传样本不代表长期 P95, 也不消除微信官方原图下载等待.

BWG 当前约 1GiB RAM, available 523MiB, 已运行生产 VPN;没有 Docker/MySQL/Redis, 标准 TCP443 已占用. 现有域名为 `auroraleelabs.com` 下的服务. 因此没有必要为了已可绕过的网络瓶颈立即迁移整个数据库. 新子域名和 Cloudflare 托管均未配置. Cloudflare Workers 对当前 APNs 使用的 node:http2 仅提供不可运行的 stub, 全量迁移需要适配; Containers 的磁盘默认临时, 持久数据也不能直接照搬.

参考: [Workers Node.js compatibility](https://developers.cloudflare.com/workers/runtime-apis/nodejs/), [Containers lifecycle](https://developers.cloudflare.com/containers/concepts/architecture/).

复核脚本保留于 Mac `/private/tmp/relay-bwg-route-check.py` 和 Mini `/tmp/relay-bwg-route-check.py`, 不含凭据或消息正文. 它仅用于本次已上传附件的限时幂等验证, 不是生产配置.
