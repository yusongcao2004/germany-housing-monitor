# 德国找房监控器

[English README](README.md)

这是一个在本机运行、重视隐私和人工审批的德国找房项目。它可以按照 JSON 中的租金、房间数、入住时间、目标地点和搜索区域筛选房源，使用 SQLite 去重，并通过 Telegram 或邮件通知。

## 默认安全状态

- 示例配置中的 Telegram、邮件、官方提醒邮件、模型个性化和联系草稿全部关闭。
- 仓库不包含真实房东提交适配器；现有模拟适配器不会向任何平台发送信息。
- `state/`、`browser-profile/`、`backups/`、`.env` 和 `config.json` 均被 Git 忽略。
- 申请人姓名、地址、邮箱、财务说明和证明材料只能保存在忽略的本地档案中。
- 外部房源文字和房东回复永远按不可信数据处理，不会被当作指令执行。

## 快速开始

```bash
npm install
cp examples/config.example.json config.json
mkdir -p state
cp examples/contact_profile.example.json state/contact_profile.json
cp .env.example .env
python3 -m unittest discover -v
python3 scripts/preflight.py
python3 housing_workflow.py simulate
```

示例申请档案使用虚构人物，不能直接用于真实申请。编辑后的 `config.json` 和 `state/contact_profile.json` 只留在本机。

首次真实扫描前先建立静默基线：

```bash
python3 monitor.py --baseline-only --no-jitter
```

普通扫描也必须显式执行一次：

```bash
python3 monitor.py --run-once
```

直接运行 `python3 monitor.py` 只会显示用法并退出，不会启动浏览器或扫描房源。

## 联系流程边界

程序把发现房源、生成草稿、人工审核、批准和发送拆成不同阶段。审核会核对用户配置的户型、入住、通勤、周边设施和总租金条件。批准必须绑定房源 ID、当前草稿哈希、审核证据、批准人和有效期；草稿一旦修改，旧批准自动失效。发送结果不确定时会冻结，不会自动重试。

当前公开版没有真实房东发送适配器，因此即使误改配置，也不能直接联系房东。

## 上传前检查

```bash
python3 -m compileall -q .
python3 -m unittest discover -v
python3 scripts/preflight.py
```

隐私检查会拒绝个人邮箱、macOS 用户绝对路径、常见 Token 格式、数据库、日志、浏览器资料、备份目录、大文件和异常二进制文件。测试可使用保留示例域名和明确列入允许名单的房源平台服务域名。

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。
