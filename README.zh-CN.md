# 德国找房监控器

[English README](README.md)

这是一个在本机运行、重视隐私和人工审批的德国找房监控项目。它扫描配置好的 ImmoScout 搜索页，可以接入部分平台经过验证的保存搜索提醒邮件，用 SQLite 对房源去重，并把通知排队发到 Telegram 或邮箱。可选的联系流程会生成不可篡改的草稿，但每一条发给房东的联系都必须先经过逐条人工审核和批准。

## 默认安全状态

- 示例配置中的 Telegram、邮件、官方提醒邮件接入、模型个性化和联系草稿全部关闭。
- 仓库不包含真实的房东发送通道。自带的联系通道是模拟实现，无法向任何找房平台提交消息。
- 申请人档案、凭据、浏览器 Cookie、日志、备份和 SQLite 文件均被 Git 忽略。
- 官方保存搜索邮件只有在发件域名、Gmail DMARC 验证、平台身份和房源链接平台四者一致时才被信任。
- 房源文字和房东回复一律按不可信数据处理，只会被展示或摘要，绝不会被当作指令执行。
- 浏览器调试端口只监听本机回环地址，遇到已存在的调试端点会直接拒绝，而不是复用。

## 当前范围

| 能力 | 状态 |
| --- | --- |
| ImmoScout 房源发现 | 已实现，通过独立的 Chrome 会话运行 |
| WG-Gesucht / Immowelt 保存搜索邮件 | 已实现，在过滤器验证通过前保持关闭 |
| 跨平台身份识别与快照历史 | 已实现 |
| Telegram 和邮件通知 | 已实现，默认关闭 |
| 审批门控的联系草稿 | 已实现，默认关闭 |
| 真实房东提交 | 不包含 |
| Kleinanzeigen 接入 | 失败即拒绝 / 未集成 |

搜索页结构和平台规则可能变化。依赖某个平台前请重新验证，并遵守平台的条款和速率限制。

## 环境要求

- Python 3.11 或更新版本
- Node.js 和 npm
- Google Chrome for Testing，或用 `HOUSING_MONITOR_CHROME_PATH` 指定的兼容浏览器
- Apple Mail 和 launchd 集成需要 macOS；SMTP 通知代码本身跨平台

Python 运行时只用标准库。浏览器 CLI 通过 `package.json` 本地安装。

## 快速开始

```bash
git clone YOUR_REPOSITORY_URL
cd germany-housing-monitor
npm install
cp examples/config.example.json config.json
mkdir -p state
cp examples/contact_profile.example.json state/contact_profile.json
cp .env.example .env
python3 -m unittest discover -v
python3 scripts/preflight.py
python3 housing_workflow.py simulate
```

在本机编辑 `config.json` 和 `state/contact_profile.json`，两者都被 Git 忽略。示例档案使用虚构人物，不能用于真实申请。

首次真实扫描前先建立静默基线，避免已有房源刷屏通知：

```bash
python3 monitor.py --baseline-only --no-jitter
```

显式执行一次普通扫描：

```bash
python3 monitor.py --run-once
```

不带参数直接运行 `python3 monitor.py` 只会显示用法并退出，不会启动浏览器或扫描房源。

如果启用了补扫功能，需要先初始化一次更深的扫描边界：

```bash
python3 monitor.py --initialize-catch-up-baseline
```

## 配置

重要的环境变量：

| 变量 | 用途 |
| --- | --- |
| `HOUSING_MONITOR_CONFIG` | 私有运行时配置的路径 |
| `HOUSING_MONITOR_STATE_DIR` | SQLite、发件队列、日志和锁文件 |
| `HOUSING_MONITOR_BROWSER_PROFILE_DIR` | 独立的浏览器资料目录 |
| `HOUSING_MONITOR_CONTACT_PROFILE` | 私有申请人档案 |
| `HOUSING_MONITOR_ENV_FILE` | 被 Git 忽略的 dotenv 文件 |
| `HOUSING_MONITOR_CHROME_PATH` | Chrome 可执行文件 |
| `AGENT_BROWSER_PATH` | `agent-browser` 可执行文件覆盖 |

Telegram 凭据可通过进程环境变量或被忽略的 `.env` 提供，即 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID`。可选的 DeepSeek 个性化功能使用 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` 和 `DEEPSEEK_BASE_URL`。只有在显式开启个性化时才会把公开房源文字发给模型；申请人数据不会被发送。

邮件支持已有的 Apple Mail OAuth 账户，或者把应用专用密码存进 macOS 钥匙串后走 SMTP。不要把邮箱密码写进 JSON 或 `.env`。

## 联系安全模型

流程把发现、起草、批准和发送拆成独立阶段：

1. 房源必须先通过粗筛的房间数和已验证暖租过滤。
2. 本地草稿会计算哈希并保存为不可修改的版本。
3. 操作者必须核对配置的户型、入住时间、通勤、周边设施和总租金条件。
4. 批准会绑定房源 ID、草稿哈希、审核证据、批准消息、批准人和有效期。
5. 草稿一旦修改，旧批准自动失效。
6. 发送结果不明确时会冻结，而不是自动重试。

即使把配置改成真实模式，本仓库也没有真实的平台发送通道。要添加一个，必须先经过单独的安全评审和验收测试。

## launchd

仓库里的 plist 文件只包含占位符。不要直接编辑模板，用脚本渲染本地副本：

```bash
python3 scripts/render_launchd.py \
  --python "$(command -v python3)" \
  --project-dir "$PWD"
```

加载前先检查生成的 `.local.plist` 文件。渲染脚本本身不会调用 `launchctl`。

## 验证

```bash
python3 -m compileall -q .
python3 -m unittest discover -v
python3 scripts/preflight.py
```

隐私预检会拒绝私有运行时目录、本地配置、个人邮箱地址、macOS 用户路径、常见密钥格式、数据库、日志、大文件和异常二进制文件。保留示例域名下的地址，以及明确列入允许名单的找房平台服务域名，在测试中仍然允许。

## 仓库规范

每次公开推送前立即运行隐私预检。绝不提交 `state/`、`browser-profile/`、`backups/`、`.env`、`config.json`、导出的邮件、已登录页面的截图或申请材料。

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。
