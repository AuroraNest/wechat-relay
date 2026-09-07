# WeChat Relay

一个自托管的加密消息中继原型: Android 上用于工作的个人微信通知显示在 iPhone 主屏幕安装的 PWA 中, 并可从 PWA 回复. 它不支持企业微信, 也不是在 iPhone 微信 App 内使用.

消息正文和回复在端侧使用 AES-256-GCM 加密. 服务端仅保存密文, 必要元数据和 Push Subscription. PWA 按会话展示消息, Android 通过仍有效的微信通知 `RemoteInput` 发送回复.

iPhone PWA 的设置页可手动暂停转发, 或按中国标准时间设置每周重复的暂停时段. Server 是最终门禁, Android 同步规则后也会在通知入口停止图片, 语音和聊天记录处理. 暂停期间的新消息不会保存或在恢复后补发.

已开启 Accessibility 实验时, 新收到的 `[聊天记录]` 通知会触发微信原生转发到同一微信账号的 `聊天记录中继` 群. 请先建好该群并加入接收端微信. 普通消息与聊天记录提示仍同步到 PWA, 完整聊天记录在接收端微信的群里查看. 来自中转群的通知不会再次触发转发.

新收到的 `[语音]` 通知会进入同一自动处理队列, 定位来信语音并调用微信原生转文字. PWA 先收到语音提示, 随后收到带原通知时间的转写或失败提示; 长转写分段加密同步. 此过程不播放录音, 不在微信发送或转发消息.

微信部分转写结果不暴露 Accessibility 文字节点, 此时自动长按该条转写文字并点击微信 `复制`, 再短暂打开 Relay 前台页面读取剪贴板. 只接受本次复制后 10 秒内的单条纯文本, 沿用原任务的发送方和微信账号同步到原会话. 不读取旧剪贴板内容, 不清空剪贴板. 打开会话时先等待列表稳定再定位语音, 同步完成后返回桌面或恢复锁屏.

也可手动复制文字, 在 Relay 点击 `补传语音转写到原会话`, 选择对应失败语音并核对预览后同步. 手动入口超长内容拒绝发送, 不截断, 不增加微信回复入口. `重试最近失败语音` 会重新操作最近失败任务对应的会话, 仅在确认没有更新语音时使用; 否则发送新语音触发自动处理.

## 限制

- `RemoteInput` 是首选回复路径, 仅在原始微信通知仍有效时可用.
- Android 16 / API 36+ 的 Xiaomi Accessibility 回退仅限显式开启的 debug 实验. 当前收件人标题校验被禁用, 有误发给同名或错误会话的风险.
- 锁屏实验只尝试一次 PIN. 失败会停止后续 UI 操作并重新锁屏. 不要把它当作通用设备支持.
- 图片是 Android 界面截图, 不是原图. 语音仅支持微信转写文本, 不同步原声音频, 不保证识别准确率.
- 连续快速发送语音仍可能使自动处理卡住, 当前建议逐条发送并等待结果; 微信原生转文字失败时会同步失败提示.
- 不保证所有机型, 微信版本, 通知样式或锁屏状态可用.
- 聊天记录转发采用加密持久化 FIFO 队列, 打开记录后使用右上角菜单转发. 只处理 2 分钟内的新通知, 来源头像, 卡片或唯一目标群无法确认时停止. 失败任务保留状态, 发送结果不明时不自动重发. 同名群请先改名消除歧义.
- 打开微信期间若普通文本没有产生通知, 会尝试从当前卡片后方的可见消息补录并去重. 不扫描不可见历史消息, 不保证跨微信与 PWA 的到达顺序或所有消息零丢失.

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
