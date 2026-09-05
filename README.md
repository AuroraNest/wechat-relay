# WeChat Relay

一个自托管的加密消息中继原型: Android 上用于工作的个人微信通知显示在 iPhone 主屏幕安装的 PWA 中, 并可从 PWA 回复. 它不支持企业微信, 也不是在 iPhone 微信 App 内使用.

消息正文和回复在端侧使用 AES-256-GCM 加密. 服务端仅保存密文, 必要元数据和 Push Subscription. PWA 按会话展示消息, Android 通过仍有效的微信通知 `RemoteInput` 发送回复.

## 限制

- `RemoteInput` 是首选回复路径, 仅在原始微信通知仍有效时可用.
- Android 16 / API 36+ 的 Xiaomi Accessibility 回退仅限显式开启的 debug 实验. 当前收件人标题校验被禁用, 有误发给同名或错误会话的风险.
- 锁屏实验只尝试一次 PIN. 失败会停止后续 UI 操作并重新锁屏. 不要把它当作通用设备支持.
- 图片是 Android 界面截图, 不是原图. 语音消息不支持.
- 不保证所有机型, 微信版本, 通知样式或锁屏状态可用.

## 本地开发

服务端要求 Node.js 24 和可用的 MySQL. 创建一个空数据库和仅拥有该库权限的用户, 在 `spikes/ios-web-push` 中依次执行迁移:

```bash
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/001-baseline.sql
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/002-multi-pair-browser-sessions.sql
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/003-message-reply-capability.sql
mysql -h 127.0.0.1 -u relay_user -p relay < scripts/migrations/004-conversation-send-capability.sql
```

复制 `spikes/ios-web-push/.env.example` 为 `.env`, 填写 `MYSQL_*`. 本地启动脚本会使用本地 Origin 和临时 VAPID, 存储密钥:

```bash
cd spikes/ios-web-push
npm ci
npm test
node --env-file=.env scripts/run-local.mjs
```

这等同于 `npm run dev`, 但会加载 `.env` 中的 MySQL 配置. 每次启动都会更换 VAPID 和存储密钥, 因而已订阅的浏览器和已有本地数据会失效. 仅使用可丢弃的开发数据库.

Android 调试构建:

```bash
cd spikes/android-notification-probe
./gradlew -PrelayOrigin=https://relay.example.com testDebugUnitTest assembleDebug
```

将 PWA 的 `PUBLIC_ORIGIN` 和 Android 的 `relayOrigin` 设为同一个 HTTPS 地址后再配对. iPhone Web Push 需要可公开访问的 HTTPS Origin.

## 自托管

复制 `spikes/ios-web-push/.env.example` 为运行时环境文件, 填写 `PUBLIC_ORIGIN`, VAPID, `STORAGE_KEY`, `TEST_TOKEN` 与 `MYSQL_*`. VAPID, `STORAGE_KEY` 和 `TEST_TOKEN` 必须跨重启保持不变. 在 `spikes/ios-web-push` 中生成一次并写入 `.env`:

```bash
node --input-type=module -e "import webpush from 'web-push'; console.log(webpush.generateVAPIDKeys())"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
```

第二和第三个命令分别生成 `STORAGE_KEY` 和 `TEST_TOKEN`. `TEST_TOKEN` 没有默认值, 必须设置后才能使用配对接口.

先应用上面的四个 MySQL 迁移. 可通过项目提供的 Docker 配置运行, 或直接运行:

```bash
npm ci
npm run build
node --env-file=.env dist/server.js
```

`.env.example` 的 `PUBLIC_DIR=/app/public` 和 `DATA_DIR=/data` 用于 Docker. 独立运行时请改为 `PUBLIC_DIR=public` 和可写的 `DATA_DIR=data`. 在反向代理前提供 HTTPS, 并将 `PUBLIC_ORIGIN` 设为该公开 Origin. 部署细节和 Nginx 示例见 `deploy/phase0a`.

不要提交运行时环境文件, 配对码, Push Subscription, PIN 或任何密钥.

## 目录

- `spikes/ios-web-push`: iPhone Home Screen PWA 与加密 Push 服务端.
- `spikes/android-notification-probe`: Android 通知监听和回复客户端.
- `deploy/phase0a`: 通用 Docker 与 Nginx 部署示例.

## 许可

本项目采用 [MIT License](LICENSE).
