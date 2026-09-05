# WeChat Relay

一个自托管的加密消息中继原型: Android 上用于工作的个人微信通知显示在 iPhone 主屏幕安装的 PWA 中, 并可从 PWA 回复. 它不支持企业微信, 也不是在 iPhone 微信 App 内使用.

消息正文和回复在端侧使用 AES-256-GCM 加密. 服务端仅保存密文, 必要元数据和 Push Subscription. PWA 按会话展示消息, Android 通过仍有效的微信通知 `RemoteInput` 发送回复.

## 限制

- `RemoteInput` 是首选回复路径, 仅在原始微信通知仍有效时可用.
- Android 16 / API 36+ 的 Xiaomi Accessibility 回退仅限显式开启的 debug 实验. 当前收件人标题校验被禁用, 有误发给同名或错误会话的风险.
- 锁屏实验只尝试一次 PIN. 失败会停止后续 UI 操作并重新锁屏. 不要把它当作通用设备支持.
- 图片是 Android 界面截图, 不是原图. 语音消息不支持.
- 不保证所有机型, 微信版本, 通知样式或锁屏状态可用.

## 开发

服务端要求 Node.js 24 和可用的 Docker daemon. 在 `spikes/ios-web-push` 中创建仅用于本机的 `.env.development`. 模板中的 `MYSQL_PASSWORD`, `MYSQL_DEV_ROOT_PASSWORD`, `TEST_TOKEN`, `STORAGE_KEY` 和 VAPID 字段为空, 必须在启动前分别生成并填入:

```bash
cp .env.development.example .env.development
npm ci
node --input-type=module -e "import webpush from 'web-push'; console.log(webpush.generateVAPIDKeys())"
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
npm test
npm run dev:db
npm run dev
```

将 VAPID 输出的两个字段填入 VAPID 配置. 为 `MYSQL_PASSWORD`, `MYSQL_DEV_ROOT_PASSWORD`, `TEST_TOKEN` 和 `STORAGE_KEY` 各运行一次随机值命令, 使用四个不同的输出. 这些开发值也应保持不变, 否则已有开发数据和订阅可能失效.

`npm run dev` 只读取 `.env.development`, 不继承业务环境变量. 该环境固定使用 `NODE_ENV=development`, `MYSQL_HOST=127.0.0.1`, `MYSQL_PORT=23306`, `MYSQL_DATABASE=wechat_relay_dev`, `DATA_DIR=data/development`, 且不设置 `REDIS_URL`. `npm run dev:db` 使用 `compose.development.yml` 创建独立 MySQL 8.4 volume. 初始迁移只会在新 volume 创建时导入. 已有 volume 应按版本应用增量迁移, 不要为重新初始化迁移删除数据.

Android 默认选择 development. 在 `spikes/android-notification-probe` 中复制 `relay.development.properties.example` 为 `relay.development.properties`, 设置自己的 HTTPS `relayOrigin`, 再构建:

```bash
./gradlew -PrelayEnvironment=development testDebugUnitTest assembleDebug
```

将 PWA 的 `PUBLIC_ORIGIN` 和 Android 的 `relayOrigin` 设为同一个 Origin 后再配对. iPhone 与 Android 的实际测试需要你自己的 HTTPS Origin.

## 生产自托管

复制 `spikes/ios-web-push/.env.production.example` 为 `.env.production`, 填写生产 Origin, MySQL, VAPID, `STORAGE_KEY` 和 `TEST_TOKEN`. VAPID, `STORAGE_KEY` 和 `TEST_TOKEN` 必须跨重启保持不变. 完成配置与迁移后, 手动运行:

```bash
cd spikes/ios-web-push
npm ci
npm run build
npm run start:production
```

该命令只读取 `.env.production`, 不继承业务环境变量, 并验证 `NODE_ENV=production` 与 HTTPS `PUBLIC_ORIGIN`. 它不会部署到任何环境. `deploy/phase0a` 提供通用 Docker 和 Nginx 自托管示例, 不会直接替换现有生产环境. 忽略规则不能代替你对配置文件, 密钥和运行数据的保护.

生产 Android 使用 `relay.production.properties.example` 对应的本地配置, 并显式选择环境:

```bash
./gradlew -PrelayEnvironment=production testDebugUnitTest assembleDebug
```

`-PrelayOrigin=https://relay.example.com` 可覆盖 properties 中的 `relayOrigin`. 环境选择只决定 Relay 地址, 不改变 applicationId, 签名或构建产物覆盖规则.

## 目录

- `spikes/ios-web-push`: iPhone Home Screen PWA 与加密 Push 服务端.
- `spikes/android-notification-probe`: Android 通知监听和回复客户端.
- `deploy/phase0a`: 通用 Docker 与 Nginx 自托管示例.

## 许可

本项目采用 [MIT License](LICENSE).
