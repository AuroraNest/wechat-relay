# iPhone Web Push

这是安装到 iPhone 主屏幕的 PWA, 用于接收并回复 Android 上个人微信的工作消息. 它不在 iPhone 微信 App 内运行, 也不支持企业微信.

PWA 在本地生成并保存不可导出的 AES-GCM `CryptoKey`, 在本地解密预览并加密回复. 服务端仅处理密文信封, 配对元数据和 Push Subscription.

## 本地开发

需要 Node.js 24 与 MySQL. 创建空数据库和仅拥有该库权限的用户, 然后按顺序导入 `scripts/migrations/001-baseline.sql` 到 `scripts/migrations/004-conversation-send-capability.sql`.

```bash
cp .env.example .env
# 在 .env 填写 MYSQL_HOST, MYSQL_DATABASE, MYSQL_USER, MYSQL_PASSWORD
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/001-baseline.sql
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/002-multi-pair-browser-sessions.sql
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/003-message-reply-capability.sql
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/004-conversation-send-capability.sql
npm ci
npm test
node --env-file=.env scripts/run-local.mjs
```

这等同于 `npm run dev`, 但会加载 `.env` 中的 MySQL 配置. 每次启动都会更换 VAPID 和存储密钥, 因而已订阅的浏览器和已有本地数据会失效. 仅使用可丢弃的开发数据库.

## 配置与运行

复制 `.env.example` 为仅限运行环境读取的配置文件. 填写 `PUBLIC_ORIGIN`, VAPID 密钥, `STORAGE_KEY`, `TEST_TOKEN` 和 `MYSQL_*`. 生成一次密钥并保存到 `.env`, 不要在重启时更换:

```bash
node --input-type=module -e "import webpush from 'web-push'; console.log(webpush.generateVAPIDKeys())"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
```

第二和第三个命令分别生成 `STORAGE_KEY` 和 `TEST_TOKEN`. `TEST_TOKEN` 没有默认值, 必须设置后才能使用配对接口.

先应用全部 MySQL 迁移. 可通过项目提供的 Docker 配置运行, 或直接执行:

```bash
npm ci
npm run build
node --env-file=.env dist/server.js
```

`.env.example` 的 `PUBLIC_DIR=/app/public` 和 `DATA_DIR=/data` 用于 Docker. 独立运行时改为 `PUBLIC_DIR=public` 和可写的 `DATA_DIR=data`.

生产环境必须使用 HTTPS. `PUBLIC_ORIGIN` 必须与 Android 的 `relayOrigin` 完全相同, 然后才能配对和接收 Web Push. iPhone 上请使用 Safari 打开该 Origin 并添加到主屏幕.

不要提交配置文件, 测试令牌, 配对码, Push Subscription 或密钥.

## 限制

Web Push 依赖 iPhone, Safari, 网络和 Apple Push Service 的实际状态. 请在目标设备上自行验证订阅, 通知, 解密和回复. 本项目不保证所有 iOS 版本或网络环境均可用.
