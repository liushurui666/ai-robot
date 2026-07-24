# 飞书消息汇总提醒机器人

基于 [nanobot](https://github.com/HKUDS/nanobot) 的公司私有化机器人。nanobot 负责飞书接入、模型、会话、WebUI 和 Cron；本项目通过原生身份工具与 MCP 查询扩展提供跨日期消息存储、汇总草稿、人工确认和飞书群投递。

## 当前能力

- 明确指令才记录消息，普通聊天不落入业务库。
- 按主题跨天保存，来源消息 ID 去重。
- 按主题和时间范围读取原始记录。
- 支持“上次成功汇总之后”的默认时间范围，以及原始记录修改、取消和归档。
- 创建汇总草稿并绑定其引用的原始消息。
- 必须显式确认草稿后才能发送；确认令牌只在创建时返回，且当前飞书发送者必须与草稿创建人一致。
- 记录、编辑、草稿、确认和取消由 nanobot 原生工具直接绑定可信请求上下文，模型不能填写或伪造用户 ID。
- 支持飞书内部群 `chat_id` 和外部群自定义机器人 Webhook。
- 短暂网络/限流故障最多重试 3 次；内部群发送携带草稿 UUID 防止重复。
- SQLite WAL 持久化；容器重启后记录和任务仍保留。
- 未来汇总复用 nanobot 原生 Cron，业务数据不依赖聊天上下文。
- 循环任务支持排他的 `until` 截止时间；到点及之后不会执行或在服务重启后补发。
- 飞书收到请求后立即显示“正在处理”卡片，完成后在同一张卡片中原地更新最终回复；发送或更新失败时回退为普通消息。
- 支持“给 Kilian 发消息你好”这类自然语言直发指令：按公司通讯录姓名、英文名或昵称解析，唯一命中时直接发送，只有重名或无结果时才追问。
- 支持“今天 14 点约大只开会”或“预约会议室一和大只开会”：检查发起人和参会人忙闲；指定会议室时额外检查并锁定会议室，未指定或明确说不需要会议室时直接创建无会议室日程；未说时长时默认 30 分钟。
- 支持在后续对话中说“把这个会议加一下会议室一”：验证当前用户属于原日程，检查会议室空闲后直接修改原日程，不会重复创建会议。
- 支持“把本周事情提炼出来”：按当前飞书私聊用户隔离读取指定时间段内保留的真实对话，并合并同一会话的压缩摘要后归纳；群聊不能读取个人历史，也不会访问其他同事的会话。

## 快速部署

要求：Linux 公司服务器、Docker 与 Docker Compose，以及可创建企业自建应用的飞书账号。

```bash
./scripts/bootstrap.sh
vim .env
docker compose up -d --build
docker compose logs -f nanobot
```

运行不显示密钥的上线自检：

```bash
./scripts/readiness.sh
```

脚本检查本地配置、容器健康、飞书长连接、身份与单卡片补丁、七个身份绑定业务工具、访问策略、投递目标和真实飞书入站记录。飞书开放平台中的订阅方式仍需管理员人工确认。

代码升级后若模板新增了必需配置，可以显式刷新运行时配置再重启；该操作会覆盖对 `data/nanobot/config.json` 的手工修改：

```bash
./scripts/bootstrap.sh --refresh-config
docker compose up -d
```

首次启动前，在飞书开放平台创建企业自建应用并启用机器人：

1. 开启长连接事件订阅 `im.message.receive_v1`。
2. 添加接收消息、读取单聊、发送机器人消息、读取资源和获取通讯录基本信息等权限，并把需要按姓名搜索的员工纳入应用通讯录权限范围。会议预约还需应用身份的“更新日历及日程信息”、“获取日历、日程及忙闲信息”和“获取会议室信息”权限。
3. 把 App ID 和 App Secret 写入 `.env`。
4. 公司内部版本默认设置 `allowFrom=["*"]`，所有能访问该企业自建应用的租户用户均可私聊；同时保持 `groupPolicy=mention`，群内只有 @ 机器人才响应。

如只允许少数员工使用，把 `data/nanobot/config.json` 的 `allowFrom` 改为对应飞书 `open_id` 列表，不要使用姓名或手机号代替。

当前 `.env` 可以复用 deploy-gateway 的模型与飞书凭据，代码和运行服务仍全部位于本项目。图片和截图理解依赖视觉模型；当前活动配置固定使用 `qwen3.6-plus`，不要改回仅文本的 `qwen3-max`。需要注意：飞书同一应用的事件订阅方式需要在“使用长连接接收事件”和“发送至开发者服务器”之间选择；如果该应用继续把事件回调给 deploy-gateway，本项目的长连接不会收到消息。正式并行运行时应给本项目单独创建飞书应用，或者明确把该应用的事件订阅切换到长连接。

单卡片状态使用普通交互卡片和“更新已发送的消息卡片”接口，不依赖 `cardkit:card:write`。当前复用的飞书应用已通过真实创建与更新验证；若更换应用且更新接口提示无权限，可开通 `im:message:update`，或使用已包含该能力的 `im:message` / `im:message:send_as_bot`。

本项目固定 `nanobot-ai==0.2.2`，镜像构建时会应用窄范围补丁：将飞书 SDK 已验证的发送者 ID 写入当前工具请求 metadata，增加同一消息卡片的处理中/完成状态更新，并为循环 Cron 增加截止边界。补丁带源码结构校验；升级 nanobot 后若上游代码发生变化，构建会明确失败，必须先复核身份边界、卡片和调度行为。

## 配置投递目标

内部群使用飞书 `chat_id`：

```bash
docker compose run --rm --entrypoint reminder-admin nanobot \
  add-target 项目群 --kind feishu_chat --recipient oc_xxx
```

外部群使用自定义机器人。Webhook 和签名密钥只写入 `.env` 的受控 JSON 映射，保持单行：

```dotenv
FEISHU_TARGET_SECRETS_JSON={"CUSTOMER_GROUP_WEBHOOK":"https://open.feishu.cn/open-apis/bot/v2/hook/xxx","CUSTOMER_GROUP_SECRET":"xxx"}
```

注册的只是环境变量名称，不会把密钥暴露给模型：

```bash
docker compose run --rm --entrypoint reminder-admin nanobot \
  add-target 客户项目群 --kind feishu_webhook \
  --endpoint-env CUSTOMER_GROUP_WEBHOOK --secret-env CUSTOMER_GROUP_SECRET
```

查看目标：

```bash
docker compose run --rm --entrypoint reminder-admin nanobot list-targets
```

## 使用示例

```text
用户：记录到“项目进展”：登录功能已经完成
机器人：已保存到「项目进展」，记录编号 msg_xxx

用户：记录到“项目进展”：支付接口联调存在超时
机器人：已保存到「项目进展」，记录编号 msg_yyy

用户：汇总 6 月 12 日到 6 月 17 日的项目进展，发到项目群
机器人：返回汇总预览和 draft_xxx，等待确认

用户：确认发送 draft_xxx
机器人：投递到管理员登记的项目群
```

也可以说“汇总上次成功发送之后的项目进展”；机器人会从最后一个已发送批次的结束时间后一秒开始取数，避免周期汇总重复包含上一期内容。

未来执行：

```text
用户：6 月 17 日下午 5 点整理 6 月 12 日至 17 日的项目进展，发到项目群
```

nanobot 创建一次性 Cron。到点只生成草稿并向创建人展示预览，不会绕过人工确认直接对外发送。

## 本地审计后台

审计后台只在管理员电脑上运行，不需要安装到线上服务器。它通过现有 nanobot WebUI 的管理员密钥只读同步会话，线上服务不会被修改。

在本地 `.env` 配置：

```dotenv
AUDIT_REMOTE_URL=http://43.135.13.58:8765
AUDIT_ALLOW_INSECURE_REMOTE=true
AUDIT_ADMIN_TOKEN=一个独立的32位以上随机密码
```

`AUDIT_ADMIN_TOKEN` 未设置时会临时复用 `NANOBOT_WEBUI_SECRET`。启动本地后台：

```bash
docker compose --profile local-admin up -d --build audit-admin
```

打开 [http://127.0.0.1:8780](http://127.0.0.1:8780)，用 `AUDIT_ADMIN_TOKEN` 登录。关闭：

```bash
docker compose --profile local-admin stop audit-admin
```

本地归档位于 `data/audit/`，不会提交到 Git。已经被 nanobot 压缩的旧聊天只能显示摘要；从本地后台开始同步后，已采集的逐字记录会保留在独立 SQLite 中。

图片和文件会在本地后台显示缩略图或下载入口。若线上接收时附件下载失败，本地后台会尝试通过原始飞书消息补拉；需要在飞书开放平台为当前应用开通应用身份权限 `im:message:readonly`。该权限只用于读取已有消息及其附件，不会改变线上部署。

> 当前线上地址是未加密 HTTP，所以需要显式设置 `AUDIT_ALLOW_INSECURE_REMOTE=true`。后续建议给线上 WebUI 加 HTTPS，再将该值改回 `false`。

有截止时间的循环发送：

```text
用户：每隔 5 分钟给艾伦发一次“请看一下文档”，到今天 16:00 结束
```

机器人会把 16:00 作为排他截止时间：最后一次只能发生在 16:00 之前，16:00 整及之后不再发送。

## 数据与安全

- 运行数据位于 `data/nanobot/`，该目录已被 Git 忽略。镜像内进程使用 UID/GID `1000:1000`；若 Linux 服务器出现权限错误，执行 `sudo chown -R 1000:1000 data/nanobot`。
- `.env` 不进入版本库；生产环境应设置为仅部署用户可读。
- 飞书用户 ID 只从服务端事件上下文取得，不接受模型参数或用户文本提供的身份值。
- WebUI 默认只映射到服务器 `127.0.0.1:8765`，建议通过 VPN 或 SSH 隧道访问。
- nanobot 的 shell 执行工具在示例配置中关闭。
- 备份时停止容器并备份整个 `data/nanobot/` 目录。

## 本地测试

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
```
