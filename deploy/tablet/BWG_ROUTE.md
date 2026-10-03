# Relay 专属 BWG 通道

Mini 的 collector 和 reply consumer 使用私有 Unix socket 经 BWG 连接 `relay.auroramaple.com:443`. TLS/SNI/证书校验仍在 Mini 与 TXY 间完成, API origin 和请求签名不变. 不新增 TCP 监听, 不改 DNS、VPN、整机代理、配对或 outbox.

## 安装

前提: Mini 当前用户的 `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes bwg true` 成功. 复用现有 SSH 身份, 不增加密钥. 新版 collector 支持 `AWR_RELAY_HTTPS_SOCKET`; 未配置时保持原传输方式.

将 `aurora-tablet-bwg.service` 安装至 `~/.config/systemd/user/`, 将 `bwg-route.conf` 分别安装至 `aurora-tablet-relay.service.d/` 和 `aurora-tablet-replies.service.d/`. 先备份源码和 unit 文件, 验证新 collector 与 unit 后再执行:

```sh
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user daemon-reload
systemctl --user enable --now aurora-tablet-bwg.service
test -S "$XDG_RUNTIME_DIR/aurora-tablet-bwg/relay.sock"
systemctl --user restart aurora-tablet-relay.service aurora-tablet-replies.service
```

socket 位于 mode 0700 的 RuntimeDirectory, socket mask 0177. 两个业务 unit 只 Wants/After tunnel, 隧道重连不重启源采集器. SSH 使用专用连接、固定远端目标和主机校验; 失联探测后由 systemd 间隔 5s 重启. 启动瞬间或断线时请求失败仍保留既有队列, 不静默切回慢直连. `ExitOnForwardFailure` 仅保证转发监听建立, 不能代替实际 HTTPS/API 验证, 见 [OpenSSH 文档](https://man.openbsd.org/ssh_config.5).

## 验收与回滚

检查三个 units active、socket 权限、两个进程的 socket 环境, 再用已上传附件的同密文幂等请求核对 exact ACK. 在业务切换前仅终止新 tunnel 的主进程, 验证 systemd 自动恢复及新 TLS 请求成功. 不通过发送真实微信或清空队列验收.

回滚只删除本次两个 `bwg-route.conf`, daemon-reload 后重启两个业务 units, 再 disable --now tunnel. 未设置环境变量的新 collector 与原传输兼容, 无需还原数据库. 若需恢复源码, 使用本次部署备份并复核没有后续改动.

手机验收由先生完成: 新文字及后台通知、普通图片、含图片/表情的合并记录; 区分预览出现与 HD 原图就绪. 服务运行和幂等 ACK 不代表 iPhone 收件.

下一步目标是把 TXY 服务迁到 BWG. 迁移前需确认 BWG 内存、现有 443/VPN 共存、数据库和附件完整备份、API 域名/APNs 配置、恢复与回滚; 本次未迁服务或数据库.
