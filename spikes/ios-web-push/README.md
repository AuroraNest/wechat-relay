# iPhone Web Push

这是安装到 iPhone 主屏幕的 PWA, 用于接收并回复 Android 上个人微信的工作消息. 它不在 iPhone 微信 App 内运行, 也不支持企业微信.

PWA 在本地生成并保存不可导出的 AES-GCM `CryptoKey`, 在本地解密预览并加密回复. 服务端仅处理密文信封, 配对元数据和 Push Subscription.

同一后端也支持原生 iPhone Relay. 原生 App 与 PWA 共用配对, 消息, 回复, 策略和资源 API, 但使用独立的 APNs destination. 两种客户端可以继续使用既有数据库结构, 已有 Web Push subscription 无需迁移.

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

## 原生 iPhone 与 APNs

APNs 是可选能力. 不配置时服务仍可为原生 App 创建 session, 完成配对并提供消息和回复 API. 未注册 device token 或 APNs 未配置时, Push outbox 保持待发送, 不会伪造成功或发起外部重试. 配置 APNs 时, `APNS_TEAM_ID`, `APNS_KEY_ID` 和 `APNS_PRIVATE_KEY_PATH` 必须同时存在. 私钥路径指向 Git 外的 Apple `.p8` 文件. `APNS_TOPIC` 默认是 `com.auroramaple.wechatrelay`, 只由服务端配置, 客户端不能提交 topic, host 或私钥.

原生 App 的新增 API 如下. 每次调用都必须发送与 `PUBLIC_ORIGIN` 完全相同的 `Origin`:

- `POST /api/v1/ios/sessions`: 使用 `X-AWR-Test-Token` 和 JSON object 创建 session. 可选字段为 `environment` 和 `previewEnabled`, 默认分别为 `sandbox` 和 `false`. 返回 `{ackToken}`.
- `POST /api/v1/pairings`: 继续使用既有 `X-AWR-Test-Token` 和 `X-AWR-Ack-Token` 流程.
- `PUT /api/v1/ios/push`: 使用 `X-AWR-Ack-Token` 和 `X-AWR-Pair-Id`, 提交 `{deviceToken,environment,previewEnabled}`. `deviceToken` 可以是 `null`.
- `GET /api/v1/ios/status`: 使用 `X-AWR-Ack-Token` 和 `X-AWR-Pair-Id`, 返回配对, Android 最近签名请求时间及 Push 状态.

当 `previewEnabled=false` 时, APNs payload 不含 `previewEnvelope`. 通知扩展应从受认证的消息 API 获取完整密文记录. APNs 返回无效 token 时, 服务端只清除该原生 destination 的 token, 不撤销 session 或配对.

## 限制

Web Push 依赖 iPhone, Safari, 网络和 Apple Push Service 的实际状态. 请在目标设备上自行验证订阅, 通知, 解密和回复. 本项目不保证所有 iOS 版本或网络环境均可用.
