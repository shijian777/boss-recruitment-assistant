# BOSS 直聘助手

独立的 Windows 桌面招聘辅助工具，通过可访问性控件连接已经登录的 BOSS 直聘客户端，支持候选人关键词筛选、去重、邀约控制、SQLite 记录和异常暂停。默认测试模式，先诊断页面再使用。

## 文件与功能

| 目录/文件 | 功能 |
| --- | --- |
| `main.py`、`app/ui.py` | 程序入口和桌面界面 |
| `app/desktop.py`、`app/selectors.py` | Windows UIA 连接、控件识别和页面诊断 |
| `app/candidate.py`、`app/automation.py` | 候选人筛选、去重及执行流程 |
| `app/safety.py`、`app/database.py` | 停止条件、操作限制与 SQLite 记录 |
| `config.example.json`、`config.json` | 配置模板与当前配置 |
| `tests/` | 配置、筛选、数据库和流程测试 |
| `install.bat`、`run.bat`、`build.bat` | Windows 安装、启动与打包入口 |

以下保留原有运行要求、配置和操作边界。本轮整理未执行任何招聘邀约或消息发送。

这是一个 Windows 单机版、纯 Python 的招聘邀约辅助工具。它连接用户已经打开并登录的 BOSS 直聘 Windows 客户端，通过 Windows UI Automation 读取可访问性控件，不使用固定屏幕坐标。

项目默认是测试模式：`config.json` 中的 `"dry_run": true` 会让程序只识别、筛选和记录匹配候选人，绝不点击沟通按钮，也不会填写或发送消息。只有用户手工改成 `false`，并在软件中再次确认，才可能进入正式发送流程。

## 功能范围

- 连接已经运行的 `boss-zhipin.exe`，用户自行登录并进入“推荐”或“搜索”候选人页；
- 识别带有“打招呼”“立即沟通”或“聊一聊”明确动作的候选人卡片；
- 按包含/排除关键词做简单筛选；
- 从稳定控件 ID、姓名/岗位/公司等稳定字段生成候选人 key；
- 使用 SQLite 记录 `dry_run_match`、`sent`、`filtered`、`duplicate`、`failed`、`manual_skip`；
- 仅将已验证的 `sent` 计入每日发送数量；
- 支持连接、页面诊断、开始/继续、暂停、停止和紧急停止；
- 发送间隔、批次暂停、每日上限和连续失败停止；
- 风险关键词检测、异常截图、滚动日志和单实例锁；
- PyInstaller 目录版打包和 ZIP 软件包输出。

这些节流配置用于正常操作节奏和防止误操作，不是“防封”措施。

## 明确不支持

本项目不绕过验证码或安全验证，不自动处理风控，不伪装浏览器/设备指纹，不轮换代理，不盗取 Cookie，不调用未经授权的接口，不隐藏自动化特征，不模拟鼠标抖动，不无限群发，也不得在候选人明确拒绝后继续发送。

软件检测到验证码、安全验证、操作频繁、账号异常或其他配置中的风险文字后会截图并停止。所有验证和账号操作必须由用户本人完成。

## 系统要求

- Windows 10/11 64 位；
- BOSS 直聘 Windows 客户端，进程名通常为 `boss-zhipin.exe`；
- 源码运行需要 Python 3.11 或 3.12；
- BOSS 客户端与本助手应使用相同 Windows 权限级别。通常都以普通用户运行，不要将其中一个单独以管理员运行。

## 安装与启动

源码方式：

1. 安装 Python 3.11/3.12，安装时勾选 `Add Python to PATH`。
2. 双击 `install.bat`，它会创建 `.venv` 并安装依赖。
3. 手动启动 BOSS 直聘客户端并登录。
4. 双击 `run.bat`。
5. 在助手中点击“连接 BOSS 客户端”。
6. 先进入候选人页，点击“页面诊断”，确认能识别卡片。
7. 保持测试模式，点击“开始/继续”。

命令行等价命令：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## 首次测试与页面诊断

首次使用不要修改 `dry_run`。打开客户端并进入候选人推荐/搜索结果后：

1. 点击“连接 BOSS 客户端”；
2. 点击“页面诊断”；
3. 在界面日志或 `logs/boss_inviter.log` 中检查窗口标题、进程 ID、可见按钮、控件类型数量、沟通文字匹配数和候选人识别数；
4. 查看 `screenshots/desktop_diagnostic_*.png`；
5. 若需要更详细的脱敏控件树，把 `diagnostics_save_tree` 改成 `true`，重新启动后诊断。JSON 会写入 `screenshots/`，手机号和邮箱会脱敏。

诊断模式只读取控件和截图，绝不发送消息。

若 GUI 无法打开，可在命令行执行一次性只读诊断：

```powershell
BossInviter.exe --diagnose-once
```

结果写入 `logs/diagnose_once.json`，截图写入 `screenshots/`。

## 配置说明

程序只读取 `config.json`，运行时不会覆盖它。`config.example.json` 是安全模板。

| 字段 | 含义 |
| --- | --- |
| `control_mode` | 当前固定为 `desktop` |
| `dry_run` | `true` 为测试模式；默认值且最安全 |
| `message` | 固定邀约话术，不能为空 |
| `daily_limit` | 每日已验证成功发送上限，1–200 |
| `interval_seconds` | 每次成功发送后的等待秒数 |
| `batch_size` | 每批成功发送数量 |
| `batch_pause_seconds` | 每批后的长暂停秒数 |
| `max_consecutive_failures` | 连续失败停止阈值 |
| `include_keywords` | 包含关键词数组；空数组表示不限制 |
| `exclude_keywords` | 排除关键词数组；任意命中即跳过 |
| `match_mode` | `any` 命中任一，`all` 必须全部命中 |
| `risk_keywords` | 任意命中即截图并停止 |
| `desktop_window_title` | BOSS 主窗口标题，默认 `BOSS直聘` |
| `desktop_process_name` | 客户端进程名，默认 `boss-zhipin.exe` |
| `selectors` | UIA 控件类型、按钮文字和输入框提示词 |

关键词匹配忽略大小写和多余空格。请勿添加性别、民族、宗教、婚育等筛选条件。

## 启用正式发送

只有完成多轮测试模式诊断后才考虑正式模式：

1. 关闭助手；
2. 手工编辑 `config.json`，将唯一的 `"dry_run": true` 改成 `false`；
3. 检查话术、每日上限和关键词；
4. 重新启动并连接客户端；
5. 点击“开始/继续”时核对弹窗中的正式发送警告并人工确认。

正式发送前程序会再次检查测试模式、每日上限、去重记录、页面风险、明确沟通按钮、消息输入框、完整话术和明确“发送”按钮。程序不会用回车键作为发送后备。发送后只有“聊天区出现完整消息且输入框清空”才会记为 `sent`；无法验证会记为 `failed`、截图并暂停，不计入每日数量。

## 暂停、停止和关闭

- “暂停”在当前不可中断的小操作完成后生效；所有计时等待可立即响应暂停或停止。
- “开始/继续”恢复处理。
- “停止”结束自动化工作线程并断开 UIA 连接，但不会强行关闭用户自己打开的 BOSS 客户端。
- 关闭助手窗口会请求停止并最多等待 5 秒，随后关闭短连接数据库生命周期。
- 单实例锁可防止同一目录重复启动两个助手。

## 数据、日志和截图

- 数据库：`data/boss_inviter.db`
- 日志：`logs/boss_inviter.log`，单文件 2 MB，最多保留 5 个轮转文件
- 截图：`screenshots/`

数据库表 `candidate_records` 包含：`candidate_key`、`candidate_name`、`candidate_url`（桌面客户端通常为空）、`candidate_summary`、`status`、`message`、`created_at`、`created_date`、`error_message`。只有 `sent` 计入每日发送数，并且数据库有候选人 `sent` 唯一索引。

## 常见故障

### 提示找不到 BOSS 客户端

确认客户端已启动、窗口标题是“BOSS直聘”，并且进程为 `boss-zhipin.exe`。不要让一个程序以管理员权限运行而另一个不是。

### 诊断显示 0 张候选人卡片

先确认已选择在线职位并进入“推荐”或“搜索”候选人结果。若客户端显示“暂无在线职位”，必须先在 BOSS 客户端内人工发布/选择职位。若页面确有候选人，开启 `diagnostics_save_tree` 后提供最新日志、诊断截图和脱敏 JSON。

### 找不到输入框或发送按钮

BOSS 客户端可能改版。立即停止正式模式，切回 `dry_run: true`，运行页面诊断并按日志调整 `selectors.message_input_*` 或按钮文字。不要用固定坐标补丁。

### 操作后状态无法验证

程序会记为 `failed`、截图并暂停。请人工检查实际聊天窗口；未确认前不要继续，也不要手工把数据库状态改为 `sent`。

### 配置错误

界面会显示中文校验错误，不会覆盖配置。可对照 `config.example.json` 修复 JSON 格式和字段类型。

## 页面/客户端改版处理

1. 立即保持或恢复 `dry_run: true`；
2. 打开目标候选人页并执行页面诊断；
3. 保存 `logs/boss_inviter.log`、最新诊断截图；
4. 如可接受，启用 `diagnostics_save_tree` 并提供已自动脱敏的 JSON；
5. 更新配置中的控件类型或文字后，仅在测试模式重新验证。

## 测试

离线测试不会连接真实 BOSS：

```powershell
python -m compileall .
python -m pytest
```

`tests/fixtures/mock_boss.html` 覆盖候选人卡片、沟通按钮、聊天输入框、发送按钮、下一页和风险提示；`tests/test_desktop.py` 使用假 UIA 控件树测试桌面候选人识别、明确按钮和发送后验证。

## 打包与分享

双击 `build.bat`，或运行：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --windowed --onedir --name BossInviter --additional-hooks-dir hooks --collect-submodules pywinauto --collect-all comtypes main.py
```

`build.bat` 会先跑测试，再生成目录版并复制可编辑配置，最终输出：

```text
dist/BossInviter-Windows-x64.zip
```

ZIP 中不包含真实登录数据、SQLite 数据库、日志或历史截图。朋友解压后先手动启动并登录自己的 BOSS 客户端，再运行 `BossInviter.exe`。首次使用必须保持测试模式。

## 风险与合规

平台页面结构、账号权限和使用规则可能变化。使用者需要自行确认招聘沟通具有合法目的、遵守平台条款与适用法律，并控制合理发送频率。该工具只辅助用户在可见桌面客户端内执行明确动作，不保证平台兼容性，也不提供任何规避平台风控的能力。
