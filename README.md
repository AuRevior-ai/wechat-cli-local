# WeChat CLI Local

用于共同学习的本地微信数据工具，提供命令行与浏览器界面。Python 查询、媒体处理和网页接口共享同一份核心实现。

## 安装

使用 Python 3.10 或更新版本。在仓库根目录创建虚拟环境并安装源码。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\wechat-cli --help
```

macOS 和 Linux 的虚拟环境命令位于 `.venv/bin/`。源代码保留三个平台的扫描器，当前整理验收以 Windows 为主。具体兼容性取决于本机微信版本与数据格式。

已有同名命令时，请使用虚拟环境中的命令路径，以便明确执行的源码版本。

## 初始化与使用

保持本机微信运行并完成登录。初始化需要进程读取权限，请按工具提示使用合适权限运行。

```powershell
.venv\Scripts\wechat-cli init
.venv\Scripts\wechat-cli sessions --limit 10
.venv\Scripts\wechat-cli history "联系人备注" --limit 30 --format text
.venv\Scripts\wechat-cli search "关键词" --chat "联系人备注"
.venv\Scripts\wechat-cli web --open
```

网页默认地址为 `http://127.0.0.1:8787`，本地邀请归档看板位于 `/launch`。首次访问可以先使用网页完成初始化，再重新启动网页服务以加载邀请归档。

配置与密钥位于各自电脑的 `~/.wechat-cli/`。群聊、朋友圈、媒体、导出和邀请溯源的完整参数可通过各命令的 `--help` 查看。根命令的 `--config` 可指定其他配置文件。

## 可选功能

`ai-package` 支持在本机整理聊天资料和语音转写。语音组件与模型首次使用时按固定地址下载并校验哈希，后续使用本地缓存。组件与模型信息见 THIRD_PARTY_NOTICES.md。

Windows 文本发送功能依赖可选扩展。

```powershell
.venv\Scripts\python -m pip install -e ".[outbound]"
.venv\Scripts\wechat-cli send-text --probe
```

实际发送通过 `send-text --to "联系人备注" --text "正文"` 显式触发。`draft-identity-probe` 会临时修改当前聊天草稿并恢复。请先阅读两个命令的帮助说明。

## 阅读源码

- `wechat_cli/main.py` 注册命令。
- `wechat_cli/commands/` 处理参数和输出。
- `wechat_cli/core/` 读取数据库并解析消息、联系人和媒体。
- `wechat_cli/keys/` 提取本机数据库密钥。
- `wechat_cli/web/server.py` 将网页请求映射到受限 CLI 参数。
- `wechat_cli/web/static/` 提供原生 HTML、CSS 和 JavaScript 页面。
- `wechat_cli/launch_ops/` 保存本地邀请关系与归档看板逻辑。
- `wechat_cli/outbound/` 提供可选 Windows UI 发送实现。

## 测试与协作

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

测试使用临时目录、构造数据库和 mock。修改功能时同步更新相关测试，再通过分支与 Pull Request 一起审阅。维护范围见 docs/MAINTENANCE.md，源码来源见 docs/SOURCE.md。

examples/config.example.json 演示配置格式，实际路径由初始化命令生成。真实配置、密钥、聊天记录、媒体、日志和模型缓存保存在各自电脑。

## 来源

作者 Au Revior。整理自 AuRevior-ai/wechat-cli，来源提交与整理差异记录在 docs/SOURCE.md。许可证见 LICENSE，第三方组件声明见 THIRD_PARTY_NOTICES.md。
