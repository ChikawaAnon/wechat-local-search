# WeChat Local Search

面向**本人或已获得明确授权的数据**的微信 4.x 本地聊天记录检索与导出工具。项目读取用户自行准备的本地解密数据库副本，在本机完成结构化索引、组合过滤、附件解析、图片 OCR、语义向量召回以及 Word/TXT 导出；核心检索流程不访问微信服务器。

> 本仓库不包含任何真实聊天记录、联系人、账号标识、数据库密钥、API 密钥或解密产物。测试套件完全使用运行时生成的合成 SQLite 数据。

## 核心能力

- **组合检索**：关键词、任一/全部命中、日期范围、聊天对象、群内发送者联合过滤。
- **消息解析**：文本、图片、文件、公众号文章、链接、表情、语音与合并转发记录。
- **图片与语音增强**：可选离线 OCR、缩略图嵌入和离线语音转写。
- **语义检索**：可选向量索引，将自然语言查询与关键词检索、联系人和日期条件叠加。
- **导出交付**：生成带分组、命中高亮、超链接与图片缩略图的 DOCX，或导出纯文本。
- **本地优先**：检索缓存、OCR 库、向量索引和导出文件都保存在本机。

## 数据流

```text
已授权的本地解密数据库副本
          ↓
SQLite 表发现与消息解析
          ↓
本地检索缓存（联系人 / 消息 / 附件 / OCR / 语音）
          ↓
关键词检索或可选语义召回
          ↓
终端结果 / DOCX / TXT
```

## 快速开始

### 1. 安装依赖

```powershell
git clone https://github.com/ChikawaAnon/wechat-local-search.git
cd wechat-local-search
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

如需 OCR 或向量能力，再安装：

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements-optional.txt
```

### 2. 配置本地数据

复制示例配置：

```powershell
Copy-Item config.example.json config.json
```

然后将 `config.json` 中的 `db_dir`、`self` 和账号目录改为你自己已获授权的数据位置。`config.json` 已被 Git 忽略。

本仓库**不提供也不上传数据库密钥提取工具**。`sync/watch` 为本地适配扩展点，需使用者自行接入合法的数据准备流程。请仅对自己拥有或明确获授权的数据进行处理，并自行准备合法的本地解密副本。

### 3. 构建索引并检索

```powershell
python wx_search.py build
python wx_search.py search -k 项目 会议 -s 2026-01-01 -l 200
python wx_search.py search -k 项目 会议 --any
python wx_search.py search --semantic "讨论过硬件调试的问题"
```

也可以双击 `search.bat` 进入交互式检索。

## 命令概览

| 命令 | 作用 |
| --- | --- |
| `build` | 从本地数据库构建或重建检索缓存 |
| `search` | 组合过滤并导出 DOCX/TXT |
| `images` / `ocr` | 处理本地图片并建立 OCR 文本索引 |
| `voices` | 提取语音并执行可选离线转写 |
| `embed` | 构建可选语义向量索引 |
| `status` / `doctor` | 查看索引状态与环境自检 |
| `clean --dry-run` | 预览可清理的本地缓存与导出产物 |

## 测试与隐私

```powershell
python -m pytest -q
```

当前公开版包含 29 个自动化测试，覆盖内容解析、SQLCipher 页面算法、图片解码、合成数据库索引、关键词/联系人/日期过滤、群昵称、合并转发、DOCX/TXT 导出和语音提取。所有联系人名、账号 ID、群名、消息、文件名和时间均为明显标注的 fixture/demo 合成数据，测试不会扫描用户目录或真实微信数据库。

公开仓库通过以下规则保护隐私：

- 忽略 `config.json`、`data/`、`output/`、数据库、密钥、日志和导出文档；
- 不包含真实账号标识、聊天内容、联系人、图片、语音或向量索引；
- 不包含密钥提取工具和本机交接文档；
- API 密钥只允许放在被忽略的本地文件中。

## 安全与合规边界

- 仅处理本人数据或已取得明确授权的数据。
- 不连接微信服务器，不尝试绕过账号登录或平台访问控制。
- 导出文件可能包含敏感信息，应存放在加密磁盘并设置最小访问权限。
- 使用者负责遵守所在地法律、组织政策及平台条款。

## License

MIT
