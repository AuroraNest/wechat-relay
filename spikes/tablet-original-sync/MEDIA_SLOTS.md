# Native v8 media slots

v8 keeps message identity, sequence, encrypted content and attachment slots immutable. A slot exists before its bytes, MIME type or length are known. Resolving one slot never waits for another slot, changes the message body or allocates a new message sequence. v6/v7 remain unchanged.

## Wire contract

- First message: `v: 8`, `nativeAssetSlots: [{id, kind, role, derivedFrom?}]`, `contentEnvelope` and the existing preview/context fields. Omit `nativeAssets` in the upload. Slots have the existing five kinds and two roles, UUID IDs, at most eight entries. A playback slot references an original slot of the same kind. Encrypted content attachments bind one-to-one to slots, including record item indices.
- `POST /api/v1/android/messages/:messageId/assets/:assetId`: v8 body is `{metadata, envelope}`. Metadata is the existing strict `NativeAsset` shape with actual MIME and positive byte length. Its ID/kind/role/derivedFrom must match the immutable slot. Persist exact encrypted request bytes before sending, and retry those bytes.
- First successful upload atomically inserts metadata and ciphertext. Identical retries succeed; changed metadata or ciphertext conflicts. Enforce pair/device ownership, slot expiry, the 8 MiB asset limit and the existing 256 MiB quota at resolution under the device transaction lock. Publish pair-scoped `asset-ready` after commit.
- Message listing and `GET /api/v1/ios/messages/:id/content` expose immutable `nativeAssetSlots` and a `nativeAssets` subset containing only resolved metadata. Content returns the same encrypted envelope on every read. No zero lengths or guessed MIME values represent unresolved files.
- `GET /api/v1/ios/assets/:id` returns 409 for an unresolved owned slot, 410 after expiry and 404 for another pair. Resolved assets keep the existing response shape.

## Authentication and storage

For v8, insert pairId immediately after the protocol version in content and asset AAD:

```text
AWR1|A2I_CONTENT|8|pairId|messageId|deviceId|seq|createdAt|wechatUserId
AWR1|A2I_ASSET|8|pairId|messageId|assetId|deviceId|seq|createdAt|wechatUserId|kind|mimeType|byteLength|role|derivedFrom
```

The preview AAD is unchanged. A slot is single-assignment; the v8 discriminator, immutable encrypted slot references and exact metadata in asset AAD provide version and representation binding. No mutable global manifest revision is needed.

Migration 009 adds `native_asset_slots` with immutable ID, message ID, ordinal, kind, role, derived_from and expiry. The existing `native_assets` and payload tables contain resolved assets only. Check collisions against legacy asset IDs as well as slots. Store v8 slots in `native_contents.assets_json` too for ordered projection; v8 APIs expose them as slots, never as legacy metadata.

## Client behavior

The iPhone renders text and record order from the initial encrypted content. Unresolved original slots show a source-download placeholder. Resolved metadata must match slots before it can be merged into the same cached message, preserving content/read state/conversation. An `asset-ready` hint refreshes only its owning message manifest and wakes that asset. Reconnect refreshes cached messages with unresolved or pending slots. Neither path requires a new message sequence or periodic full polling.

Derived playback slots may remain unresolved if no conversion applies. Display the available original; show a playback card only once resolved. Never hide an original solely because a pending playback slot exists.

New HD images also use v8 with one original slot and one derived playback image slot. A complete ordinary-quality JPEG may resolve the preview slot after exact source/ImgInfo2 linkage, download counts, cache key and stable-file checks. It never satisfies the original's HD checks or stops original-download retries. The iPhone shows `普通清晰度预览, 原图仍待下载`, labels preview actions accordingly, and prefers the original once resolved. Changing the displayed asset ID resets the view's file/image state; the message and encrypted content remain unchanged. Already-published v7 messages are not rewritten.

When the ordinary JPEG is missing, the collector first opens the official viewer with `downloadPreview: true`, waits up to 45 seconds for source-verified ordinary bytes and does not request HD. This stage has an independent retry identity. Ordinary content tasks precede HD upgrades; a later source read queues HD once the preview is available. HD button acquisition is limited to 30 seconds before a request starts, while a started request retains the existing 150-second total wait budget. These are stage budgets, excluding viewer startup and cleanup. Deploy the guest executor before the collector so an older executor cannot ignore the preview flag. Official viewer cancellation and later HD completion still require device verification.

If the medium JPEG is missing, an existing JPEG thumbnail may fill the preview slot immediately, without opening the viewer. The message's `imgPath` and its source-linked `ImgInfo2.thumbImgPath` must declare the same `THUMBNAIL_DIRPATH` basename, with exactly one file in the account's mapped views. Readonly, no-follow, format, size and stable-file checks still apply. This representation is `native_db_thumbnail` with `originalBytesVerified: false`; it never resolves the original. Once a preview slot has bytes, its ciphertext and metadata remain immutable. A later full original replaces the displayed preview using the separate original slot.

## Acceptance

An unknown-size mixed record arrives before either attachment. One attachment can resolve and display while another remains pending. Repeated uploads preserve exact ciphertext; changed metadata, cross-pair access, v7/v8 relabeling and mismatched AAD fail closed. Reconnect fills a missed resolution into the same message. Validate legacy v6/v7 independently.

## 2026-10-03 部署与真实验证

- TXY 已更新 `tablet-v8-20261003-01a10115`, schema 8 -> 9. 迁移前后原业务表计数一致, 13 个 legacy asset slot 回填完整. 备份位于 `/etc/aurora-wechat-relay/backups/tablet-v8-20261003-01a10115`. schema9 回滚需处理新增数据并恢复完整备份, 不能仅切换旧镜像.
- Mini 宿主/guest 已更新, 代码哈希一致, pair 配置未变, 两个 user units active/running/NRestarts0. 一致数据库、配置和旧代码备份位于 `~/.local/share/aurora-tablet-relay-backups/media-v8-release-20261003-01a10115`.
- iPhone 已覆盖安装并复核 `1.0(13)`, process launch 成功. 保留原配对和历史, 未代发微信消息.
- 官方记录图片实际请求约 50.9s 后由 reader 校验为 `IMAGE_AVAILABLE`, JPEG 123540 bytes. 记录表情初次请求后缓存校验失败; 实证发现微信本地缓存前1024 bytes有独立AES-128-ECB加密层. reader只读获取同账号现存EmojiInfo catalog153 key,在内存中解码后仍严格匹配primary MD5/长度/格式,不改源文件或输出key. 本条原件为PNG 90195 bytes,300x300,单帧.
- 新真实 v8 混合记录已上传文字/结构和 2 个稳定 slots. 图片先独立上传,表情读取修复后在同一记录补齐. 本人已确认图片完整和表情出现. 真实动态表情及 P95 延迟尚未验收.
- build12实际预览暴露UIImageView intrinsic size超出SwiftUI frame导致裁剪,build13新增sizeThatFits接受既有fit frame. Simulator横/竖及静/动画4例修前裁边、修后bounds及四边像素全部通过,本人已确认图片完整.
- 更新前已上传的 v7 记录保持原密文和身份, 不追溯改成 v8. 普通 HD 的完整官方下载仍有未解决项; `d2y` 保存路径仅在特定条件下请求 HD 并会导出相册, 本版未纳入默认自动化.
- 验证: 最终Mini `python3 -B -m unittest discover -q` 149 项, 147 PASS/2 SKIP; 独立 MySQL Node 集成 1 PASS, Python 跨语言 2 PASS, migration009 回填/重跑/legacy API 通过. Swift 41 项通过(3 个既有 Keychain sandbox 测试跳过), 最后 14 项定向测试通过, Release build13真机签名构建通过. Mac 既有 audio RLIMIT_AS fixture 不适用 Darwin, Linux 同项通过.

## 2026-10-03 HD 普通预览补充

- 先生明确选择先显示普通清晰度预览并标明原图待下载. 真实待处理单图的v8消息和JPEG63741 bytes普通预览均已上传, 原图声明179286 bytes仍pending. 保留HD原件校验与重试, 不猜坐标或点击隐藏控件.
- Mini已备份更新collector/source_reader, pair配置哈希不变, 两个units恢复active. 备份 `~/.local/share/aurora-tablet-relay-backups/hd-preview-20261003-01a10115`. 当前ImgInfo2无msgTalker列时, 通过严格local/server IDs与reserved1关联同账号message.talker; 有原生talker时仍拒绝不一致项.
- Linux最终159项156PASS/3无集成环境SKIP. 独立MySQL环境Python跨语言3/3PASS,覆盖preview先传、original后补且同message/envelope不变. iOS独立Simulator实证去掉`.id`会复用旧预览缓存, 修复后切换到原图; audio和legacy回归通过.
- Release build14签名构建成功. 首次覆盖安装因iPhone连接超时失败; 随后按先生要求重试成功, devicectl复核1.0(14)并启动成功. 新预览真机显示待先生确认, build13混合记录验收仍有效.
