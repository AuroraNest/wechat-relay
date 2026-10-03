# iPhone Web Push

这是安装到 iPhone 主屏幕的 PWA, 用于接收并回复 Android 上个人微信的工作消息. 它不在 iPhone 微信 App 内运行, 也不支持企业微信.

PWA 在本地生成并保存不可导出的 AES-GCM `CryptoKey`, 在本地解密预览并加密回复. 服务端仅处理密文信封, 配对元数据和 Push Subscription.

同一后端也支持原生 iPhone Relay. 原生 App 与 PWA 共用配对, 消息, 回复, 策略和资源 API, 但使用独立的 APNs destination. 当前服务要求 schema v9, 已有 Web Push subscription 无需迁移.

## 本地开发

需要 Node.js 24 和可用的 Docker daemon. 复制模板后, 在启动前填写所有空值:

```bash
cp .env.development.example .env.development
npm ci
node --input-type=module -e "import webpush from 'web-push'; console.log(webpush.generateVAPIDKeys())"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
npm test
npm run dev:db
npm run dev
```

将 VAPID 输出的两个字段填入 `.env.development`. 为 `MYSQL_PASSWORD`, `MYSQL_DEV_ROOT_PASSWORD`, `TEST_TOKEN` 和 `STORAGE_KEY` 各运行一次随机值命令, 并填入四个不同的输出. 这些开发值应保持不变, 否则已有开发数据和订阅可能失效.

`.env.development` 是 `npm run dev` 唯一读取的环境文件. 启动前会校验 `NODE_ENV=development`, `MYSQL_HOST=127.0.0.1`, `MYSQL_PORT=23306`, `MYSQL_DATABASE=wechat_relay_dev`, `DATA_DIR=data/development`, 并要求 `REDIS_URL` 为空. `npm run dev:db` 使用 `compose.development.yml` 创建独立 MySQL 8.4 volume. 初始迁移只会在新 volume 创建时导入. 已有 volume 应按版本应用增量迁移, 不要为重新初始化迁移删除数据.

当前服务严格要求 schema v9 和 MySQL `max_allowed_packet >= 16777216` (16 MiB). 既有数据库按版本依次应用尚未执行的迁移, 包括 `scripts/migrations/008-native-content.sql` 和 `scripts/migrations/009-native-asset-slots.sql`. 联系人表仍只保存每个 Android device 和 profile 的最新 AES-GCM 密文信封及校验元数据.

开发 Origin 只能用于本机检查. 若要在 iPhone 上安装 PWA 或接收 Web Push, 请配置自己的 HTTPS Origin, 并将它写入 development 配置的 `PUBLIC_ORIGIN`.

## 生产自托管

复制 `.env.production.example` 为 `.env.production`, 完整填写生产配置与 MySQL 连接. 该文件包含独立运行所需的 `PUBLIC_ORIGIN`, `PUBLIC_DIR`, `DATA_DIR`, VAPID, `STORAGE_KEY` 和 `TEST_TOKEN`.

VAPID, `STORAGE_KEY` 和 `TEST_TOKEN` 必须只生成一次并跨重启保留. 使用现有依赖生成后写入 `.env.production`:

```bash
node --input-type=module -e "import webpush from 'web-push'; console.log(webpush.generateVAPIDKeys())"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
```

第二和第三个命令分别生成 `STORAGE_KEY` 和 `TEST_TOKEN`. `TEST_TOKEN` 没有默认值. 先应用项目 SQL 迁移, 再构建和启动:

```bash
npm ci
npm run build
npm run start:production
```

`npm run start:production` 只读取 `.env.production`, 不继承业务环境变量, 并验证 `NODE_ENV=production` 与 HTTPS `PUBLIC_ORIGIN`. 它只启动当前机器上的服务, 不会自动部署. `PUBLIC_ORIGIN` 必须与 Android `relayOrigin` 完全相同.

`deploy/phase0a` 提供通用 Docker 和 Nginx 示例, 不会直接替换任何已有环境. 忽略规则不能代替对 `.env.production`, Push Subscription, 配对码和密钥的访问控制.

### schema v9 与 immutable media slots

升级前备份数据库并停止旧服务写入, 按顺序应用尚未执行的 migration 008/009, 确认 `SELECT MAX(version) FROM schema_migrations` 为 9, 并检查 `SELECT @@max_allowed_packet` 至少为 16 MiB. 服务启动会检查版本, 必要列和 packet 限制.

```bash
mysql --defaults-extra-file=/path/to/mysql.cnf "$MYSQL_DATABASE" < scripts/migrations/009-native-asset-slots.sql
```

v1-v7 消息保持兼容, v6 只读. v8 使用最多 8 个 immutable `nativeAssetSlots: [{id,kind,role,derivedFrom?}]`, 首包禁止 `nativeAssets`. 未知附件无需猜测 MIME 或长度, 正文和 slots 接受后不再改写. v8 `replyCapable` 沿用 v7 私聊边界, `conversationSendCapable` 必须为 false. 完整合同见 [`MEDIA_SLOTS.md`](../tablet-original-sync/MEDIA_SLOTS.md).

migration 009 新增 `native_asset_slots`, 并登记旧 `native_assets` ID; 新 v6/v7 声明也占用同一 PK, 防止不同 pair 并发跨版本复用 ID. 旧消息的列表顺序和 metadata 仍来自原 `assets_json`, 不改变旧投影. v8 在 `native_contents.assets_json` 保存 slots 顺序, 在 `native_asset_slots` 保存各 slot 的不可变身份和首次消息接收起 7 天的 expiry. 只有成功解析的 metadata 和 ciphertext 才进入 `native_assets` 和 `native_asset_payloads`.

v8 `POST /api/v1/android/messages/{messageId}/assets/{assetId}` body 为 `{metadata,envelope}`. Metadata 必须与 slot 的 ID/kind/role/derivedFrom 完全匹配, 单附件实际长度为 1 byte 至 8 MiB. 在 device transaction lock 下检查实际 256 MiB 配额, 并原子插入 metadata/ciphertext. v6/v7 继续在首包预留配额. Playback 可以先于 original 解析, 关系由 slots 校验.

Collector 必须在首次请求前持久化完整加密请求 bytes 并原样重试. 首次成功返回 `201 {id,idempotent:false}`, 完全相同 bytes 返回 `200 {id,idempotent:true}`, 改变 metadata/ciphertext 或重新序列化后的不同 bytes 返回 `409 {error:"NATIVE_ASSET_CONFLICT"}`. v6/v7 继续采用原 envelope 幂等规则. 成功事务提交后向所属 pair 发布 `asset-ready` SSE hint, 不增加消息 sequence.

v8 content/asset AAD 在版本 8 后插入 authenticated pairId, preview AAD 不变. `GET /api/v1/messages` 及 `GET /api/v1/ios/messages/{messageId}/content` 返回 immutable `nativeAssetSlots` 和已解析的 `nativeAssets` 子集. Content envelope 始终不变; v6/v7 content API 仍只返回 `{contentEnvelope}`. 过期清理只删除 ciphertext, 保留 metadata 和 slots.

`GET /api/v1/ios/assets/{assetId}` 在所属 pair 内 pending 返回 409, expired 返回 410; unknown 或其他 pair 返回 404. 客户端收到 `asset-ready` 或重连后重新获取同一消息的 manifest, 将新 metadata 合并到原消息而不清除正文或已读状态. 未解析 playback 不应隐藏 original.

schema v8 旧服务不能在 schema v9 启动. 回退应恢复升级前备份及对应旧服务并处理升级后的新数据, 不应仅删除 migration 记录或新表. 下节仅记录历史 v7 -> v6 流程.

### 历史 schema v7 升级与回退

升级前先备份数据库, 再在停止接收新 v4 reply 的窗口执行 migration 007 并确认 `schema_migrations` 的最大版本为 7. 例如可由部署环境的受控 MySQL 凭据执行:

```bash
mysql --defaults-extra-file=/path/to/mysql.cnf "$MYSQL_DATABASE" < scripts/migrations/007-contact-send-replies.sql
```

当时的 v7 镜像严格要求 schema v7, v6 镜像严格要求 schema v6, 因而不能在 schema v7 上直接作为回滚镜像启动.

如需回退到旧镜像, 先确认没有任何 v4 reply. 以下 SQL 每一步都以 `@awr_v4_rows = 0` 为前提; 非零时只执行 `DO 0`, 不会删除 v4 数据或改变 schema. 它只适用于 v4 未产生任何行的回退窗口, 不应在有 v4 数据时执行.

```sql
SELECT COUNT(*) INTO @awr_v4_rows FROM replies WHERE contact_snapshot_id IS NOT NULL;
SELECT @awr_v4_rows AS v4_reply_rows;

SET @awr_rollback_drop_check = IF(@awr_v4_rows = 0, 'ALTER TABLE replies DROP CHECK replies_exactly_one_target', 'DO 0');
PREPARE awr_rollback_stmt FROM @awr_rollback_drop_check;
EXECUTE awr_rollback_stmt;
DEALLOCATE PREPARE awr_rollback_stmt;

SET @awr_rollback_drop_contact = IF(@awr_v4_rows = 0, 'ALTER TABLE replies DROP COLUMN contact_snapshot_id', 'DO 0');
PREPARE awr_rollback_stmt FROM @awr_rollback_drop_contact;
EXECUTE awr_rollback_stmt;
DEALLOCATE PREPARE awr_rollback_stmt;

SET @awr_rollback_target = IF(@awr_v4_rows = 0, 'ALTER TABLE replies MODIFY target_message_id VARCHAR(191) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL', 'DO 0');
PREPARE awr_rollback_stmt FROM @awr_rollback_target;
EXECUTE awr_rollback_stmt;
DEALLOCATE PREPARE awr_rollback_stmt;

DELETE FROM schema_migrations WHERE version = 7 AND @awr_v4_rows = 0;
SELECT MAX(version) AS schema_version FROM schema_migrations;
```

## 原生 iPhone 与 APNs

APNs 是可选能力. 不配置时服务仍可为原生 App 创建 session, 完成配对并提供消息和回复 API. 未注册 device token 或 APNs 未配置时, Push outbox 保持待发送, 不会伪造成功或发起外部重试. 配置 APNs 时, `APNS_TEAM_ID`, `APNS_KEY_ID` 和 `APNS_PRIVATE_KEY_PATH` 必须同时存在. 私钥路径指向 Git 外的 Apple `.p8` 文件. `APNS_TOPIC` 默认是 `com.auroramaple.wechatrelay`, 只由服务端配置, 客户端不能提交 topic, host 或私钥.

原生 session, push 和 status 请求必须发送与 `PUBLIC_ORIGIN` 完全相同的 `Origin`. 其余 API 如下:

- `POST /api/v1/ios/sessions`: 使用 `X-AWR-Test-Token` 和 JSON object 创建 session. 可选字段为 `environment` 和 `previewEnabled`, 默认分别为 `sandbox` 和 `false`. 返回 `{ackToken}`.
- `POST /api/v1/pairings`: 继续使用既有 `X-AWR-Test-Token` 和 `X-AWR-Ack-Token` 流程.
- `PUT /api/v1/ios/push`: 使用 `X-AWR-Ack-Token` 和 `X-AWR-Pair-Id`, 提交 `{deviceToken,environment,previewEnabled}`. `deviceToken` 可以是 `null`.
- `GET /api/v1/ios/status`: 使用 `X-AWR-Ack-Token` 和 `X-AWR-Pair-Id`, 返回配对, Android 最近签名请求时间及 Push 状态.
- `GET /api/v1/ios/events?afterSeq=N`: 仅 native session 使用 `Origin`, `X-AWR-Ack-Token` 和 `X-AWR-Pair-Id` 建立前台 SSE. 服务端先发送 `ready` 和当前最新 sequence, 后续只发送 `message` sequence 或 `reply` UUID 提示. 客户端收到提示后仍通过既有受认证 HTTP API 同步密文和回复状态. 服务端约每 22 秒发送 heartbeat 并重新校验 session.
- `POST /api/v1/android/contacts`: 使用既有 Android 签名请求头, 提交 v1 或 v2 contacts envelope. 服务端验证 `AWR1|A2I_CONTACTS|{v}|{id}|{deviceId}|{capturedAt}|{wechatUserId}` AAD, 但不读取联系人明文. v1 继续可读取; v2 表示 Android 已保留可用于联系人发信的本地验证快照.
- `GET /api/v1/ios/contacts`: 仅 native session 使用 `X-AWR-Ack-Token` 和 `X-AWR-Pair-Id` 读取当前 pair 的至多两个 profile snapshot. 重配对后旧 device 的快照不可读取.
- `POST /api/v1/replies`: 原有 v1-v3 保持按 message target 工作. v4 必须带 `targetContactSnapshotId`, 不得带 `targetMessageId`, AAD 为 `AWR1|I2A|4|CONTACT_SEND|{pairId}|{replyId}|{deviceId}|{snapshotId}|{createdAt}|{wechatUserId}`. 服务端仅接受当前 pair、device、profile 的 v2 snapshot, 不读取 reply plaintext; plaintext 保持在 reply envelope 内的 `{body,conversationTitle}` schema.
- `GET /api/v1/android/replies`: legacy poll 明确不返回 v4, 避免旧 Android 降级消费联系人发信. `GET /api/v1/android/replies/v4` 使用同样的 Android 签名请求格式并以该精确 pathname 参与签名, 可领取所有 reply 版本. `POST /api/v1/android/replies/{id}/ack` 接受 terminal `CONTACT_SNAPSHOT_STALE`.

当 `previewEnabled=false` 时, APNs payload 不含 `previewEnvelope`. 通知扩展应从受认证的消息 API 获取完整密文记录. APNs 返回无效 token 时, 服务端只清除该原生 destination 的 token, 不撤销 session 或配对.

## 限制

Web Push 依赖 iPhone, Safari, 网络和 Apple Push Service 的实际状态. 请在目标设备上自行验证订阅, 通知, 解密和回复. 本项目不保证所有 iOS 版本或网络环境均可用.
