# Mac 本地代码交接（2026-10-22）

此包包含真实 Git 历史 bundle、由该提交导出的代码快照、独立本地适配补丁、非机密配置和验证证据。
**本次只制作交接包，没有修改云端工作树或运行服务。**
当前本地方案使用现有仓库 `/Users/moc/Claude/Company Brian/knowledge-distillation`；
其 HEAD 为 `97ec6fe`，31 项未提交变更必须保留。不使用 Downloads 开发源码，不复制或覆盖该 dirty 工作区。

## 身份与补丁

- 云端分支：`cloud/rag-integration`
- 云端合并提交：`afcfffff56c8a69dc9b4a3bef40250eab2d026be`
- 最新蒸馏基准：`97ec6feea37b853e0259ece1b1e67e80b3d4b992`
- bundle 是单一云端分支的完整可达历史：15 个提交。没有 `.git/config`、工作树配置、凭据或其他本地 refs。
- `handoff/mac-local.patch` 是 afcffff 之后的本地部署适配，包含恢复 DeepSeek/Qwen、部署脚本和文档。
  应应用到独立克隆的 `local/mac-deepseek-qwen` 分支；这不是原 afcffff 的内容，不得给它冒用该哈希。
- 包内 `src/ckp` 11 个 Python 文件与最新基准完全相同，蒸馏工具仍为原有 21 个。
- `handoff/upstream-diff-stat.txt`：云端相对基准 37 files / 2638 insertions。
- `handoff/commits.txt`、`handoff/evidence.json`、`handoff/cloud-tests-99.log` 提供提交与验证记录。
- `handoff/history-audit.json` 为可达 Git 对象扫描范围和结果；模式扫描不能保证消除所有未知形式的敏感内容。

## 1. 校验包与导入 Git

先在 ZIP 所在目录运行 `shasum -a 256 -c knowledge-distillation-mac-handoff.zip.sha256`。
解压后在包根目录运行 `shasum -a 256 -c SHA256SUMS`。
`SHA256SUMS` 覆盖包内文件（清单自身除外）；外层 sha256 覆盖整个 ZIP。

下面脚本只在独立克隆中导入提交对象/新 refs、建立已忽略的独立嵌套克隆、应用补丁；不会 checkout/reset/clean 原工作区。
会在前后核对原 HEAD 和 NUL 分隔的 git status 是否完全一致。目标目录已存在时拒绝覆盖。

```bash
python3 deploy/import_git.py \
  --repo '/Users/moc/Claude/Company Brian/knowledge-distillation' \
  --destination '/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration'
```

等价的关键 Git 操作如下（不要与上面的自动脚本重复执行）：

```bash
# 在解压包目录，先确认父仓库已通过 .git/info/exclude 忽略 cloud-rag-integration/。
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation' check-ignore cloud-rag-integration/
git clone --branch cloud/rag-integration handoff/cloud-rag-integration.bundle \
  '/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration'
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration' switch -c local/mac-deepseek-qwen
# 这里的补丁参数必须是解压包内补丁的绝对路径；不要改父仓库 tracked .gitignore。
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration' apply --index /absolute/path/to/unpacked/handoff/mac-local.patch
```

父仓库使用已设置的本地 `.git/info/exclude`，不修改其 tracked .gitignore。导出包自身也排除
`cloud-rag-integration/`、`.local/`、私有 env。原根仓库不 fetch、不 checkout、不创建新 ref。

导入后检查：

```bash
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation' rev-parse HEAD
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation' status --short
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration' show-ref refs/heads/cloud/rag-integration
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration' rev-parse HEAD
git -C '/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration' diff --cached --stat
```

原仓库 HEAD 及 31 项变更应保持原样；独立克隆 HEAD 应为真实 afcffff，本地适配补丁已暂存。
本地验证后可以在新分支正常创建新提交，保留这两个原始提交的可检出性。

## 2. Python 与模型配置

后续命令在独立克隆 `/Users/moc/Claude/Company Brian/knowledge-distillation/cloud-rag-integration` 执行。
推荐 Python 3.12。`requirements-macos.lock.txt` 来自云端验证的版本固定，去掉不再需要的本机 BGE 运行依赖，
保留 tokenizer 边界测试依赖；**这不是已在 Mac 验证过的锁文件**，需本地解析相应 macOS wheel、pip check 和测试。
原 Linux 全量锁 `requirements-cloud.lock.txt` 同时保留作证据，不能用 Linux wheel 强装 Mac。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-macos.lock.txt
.venv/bin/python -m pip check
```

模型约定如下；DeepSeek 模型 ID 是开发交付配置的原值，远端是否仍提供该 ID 尚未验证，不能擅自换名后声称已验收。

| 用途 | 配置键 | 值 |
| --- | --- | --- |
| 生成接入方式 | LLM_PROVIDER | api |
| DeepSeek endpoint | LLM_BASE_URL | https://api.deepseek.com |
| DeepSeek model ID | LLM_MODEL_NAME | deepseek-v4-flash（开发原值，需 API 实测确认） |
| DeepSeek key | LLM_API_KEY | 仅本机安全文件导入 |
| 向量化模式 | KD_EMBEDDING_MODE | qwen |
| Qwen endpoint | QWEN_BASE_URL | https://dashscope.aliyuncs.com/compatible-mode/v1 |
| Qwen model ID | QWEN_MODEL_NAME | text-embedding-v3 |
| Qwen key | QWEN_API_KEY | 仅本机安全文件导入 |
| 编码 | QWEN_ENCODING_FORMAT | float |
| 向量维度 | 适配器请求 dimensions | 1024；Milvus 同为 1024，集合前缀 kd_dev_qwen_ |

不要给 DeepSeek 默认发送 GPT 专用 reasoning_effort=medium；本地补丁只在显式设置 LLM_REASONING_EFFORT 时传递。
缺省保留开发原有 temperature=0。单测中的 GPT 选项属于旧模式边界回归，不是运行中的模型配置。

从本机**已经存在的私有配置文件**直接读取绑定；不要求 Downloads 源码，不通过聊天传递任何密钥。
将下方路径替换为本机已有有效配置的真实路径；不要把其内容显示在日志、回复或包里。

```bash
.venv/bin/python deploy/import_local_env.py --source '/absolute/path/to/existing/private.env'
```

脚本只复制模型绑定，并强制本地隔离数据库、独立数据目录和 qwen/api 模式；生成本机 MinIO 随机密码。
目标 `.env` 使用 0600，已存在时拒绝覆盖。不要 source 执行不可信 .env，不把模型密钥复制到 MCP JSON。
包内 `.env.example` 所有密码/密钥为空，不包含实际 MinIO 密码。

## 3. Apple Silicon 与数据库

已对 Docker registry manifest 核实：
MongoDB 4.0.28 提供 linux/arm64/v8；Milvus 2.6.24、etcd 3.5.25、配套 MinIO 提供 linux/arm64。
详见 `handoff/image-platforms.json`。Docker Desktop 应优先选原生 ARM64，不必先强制 linux/amd64/Rosetta。
这只证明镜像存在，**没有在 Apple Silicon 执行过**。旧 MongoDB 的实际启动、卷权限、健康检查和驱动读写仍须本机验证。
不要改装其他 MongoDB 版本；不要因启动失败自动切换模拟架构、删除数据卷或改生产配置。

建议 Docker Desktop 至少分配 4 CPU / 8 GiB 内存，保留约 15–20 GiB 可用磁盘；不足时先实际评估。
Compose 自动选本机架构，项目名 `kd-local`，不固定 container_name，数据只在独立克隆的 `.local/volumes`。
所有端口仅绑定 127.0.0.1；若 27017/19530/9091/9000/9001 已占用，先确认现有服务归属，不能停止他人服务。

```bash
./deploy/db.sh config --quiet
./deploy/db.sh pull
./deploy/db.sh up -d --wait --wait-timeout 300
./deploy/db.sh ps
./deploy/db.sh logs --tail 100 standalone mongodb
```

只使用 `config --quiet` 检查，不打印展开后的 compose（会显示本机生成的密码）。
停止用 `./deploy/db.sh stop`；恢复用 start。不要删除 `.local/volumes`。
当前隔离配置不提供生产鉴权，禁止将端口改为公网暴露。

### MinIO 原版本静态镜像交接

云端运行中的实际镜像为 `milvusdb/minio:RELEASE.2024-12-18T13-15-44Z`，不是已撤下的
`minio/minio:RELEASE.2024-05-28T17-19-04Z`。不使用不可匿名拉取的 Quay。
默认 Compose 固定为当前实际版本及多架构 index digest：
`sha256:1dce27c494a16bae114774f1cec295493f3613142713130c2d22dd5696be6ad3`。
本地已拉取 etcd 3.5.18 与云端不同；本包仍使用云端实际版本 3.5.25，须另拉取。

云端另导出纯镜像 archive，不导出容器 writable layer、volumes 或对象数据：

| 平台 | 内容 | image config ID |
| --- | --- | --- |
| ARM64 | 同 tag/index 的 ARM64 原生变体，专供 Apple Silicon；只拉取/保存，未在云端执行 | sha256:8ce3a2b6c83521fabbde58b4f06e5fb7f95e64bfc05bc10def264479327c407b |
| AMD64 | 云端正在运行的原镜像，仅作精确备份 | sha256:6aed1b6949018d1659310e66dc076711c9a9efeffa578b337ac3d1b82e0e9153 |

两者版本相同，平台构建不同。ARM64 digest 为
`sha256:dca4c608031e569387ebb9dbc5738542a4ddd68f57464c1f7ad5a2d31cefcb8d`；
AMD64 digest 为 `sha256:34c8e2f52a5984492555427fee07254c80036bdb7079bb91679232abd7a4fa20`。
registry 记录见 `handoff/minio-registry-manifest.json`。

优先直接按包中固定 digest 拉取。如果需要离线 archive，将 ARM64 tar.gz（或其全部分片）
及外部 SHA256 清单搬到 Mac；不要把 AMD64 包当成本机原生镜像。

```bash
# 若下载的是分片：先核验全部分片，再按编号合并（不能混入 AMD64 分片）。
shasum -a 256 -c minio-linux-arm64-parts.sha256
cat minio-RELEASE.2024-12-18T13-15-44Z-linux-arm64.tar.gz.part-* > minio-RELEASE.2024-12-18T13-15-44Z-linux-arm64.tar.gz
shasum -a 256 -c minio-RELEASE.2024-12-18T13-15-44Z-linux-arm64.tar.gz.sha256
gzip -dc minio-RELEASE.2024-12-18T13-15-44Z-linux-arm64.tar.gz | docker load
docker image inspect kd-handoff/minio:RELEASE.2024-12-18T13-15-44Z-arm64 --format '{{.Id}} {{.Architecture}}'
```

核验上述 ARM64 image ID 后，在本机 `.env` 设置（不是密钥）：
`KD_MINIO_IMAGE=kd-handoff/minio:RELEASE.2024-12-18T13-15-44Z-arm64`，
使用 `./deploy/db.sh up -d --pull never minio` 或
`./deploy/db.sh up -d --pull never --wait --wait-timeout 300`（其余镜像也须已拉取）。
无需改变 MongoDB/Milvus 版本。替换的仅为同版本 MinIO 的离线本机标签，S3 兼容存储职责不变。

## 4. MCP 与 API

```bash
.venv/bin/python deploy/mcp_config.py
./deploy/api.sh
```

MCP 配置生成于 `.local/mcp.local.json`，绝对路径动态产生，支持当前包含空格的工作区路径。
包含最新蒸馏 21 工具、单一开发 RAG 工作流，以及可选方案生成 Agent；不含模型密钥。
用本地 Codex MCP 配置机制加载，并实际验证握手。不要直接使用带 /workspace 路径的 cloud.mcp.example.json。
`deploy/api.sh` 前台运行 HTTP 服务 `http://127.0.0.1:8001/docs`，Ctrl-C 停止；无需 macOS 不自带的 setsid。
原 dev-services.sh / CLOUD-README.md 保留为云端历史说明，不作为 Mac 启动入口。

## 5. 验证边界与顺序

云端原提交 99 项通过；本地适配导出副本也在 Linux/Python 3.12 用既有依赖环境重跑 99 项通过（非 Mac 执行证据，见 handoff/mac-export-tests.log）。
云端使用真实 MongoDB 4.0.28 / Milvus 2.6.24，但向量为 fixture。
覆盖上传/Word 解析、合成模型草稿、导出哈希、body 切片、来源版本、入库/回读、幂等、部分失败、
重试、版本切换、跨知识类型切换、并发同知识互斥及撤回。7 份交付样本通过；2 份旧路由拒绝，客户样本未打包。
真实 DeepSeek/Qwen 调用在云端被代理 CONNECT 403 阻断，未验证密钥、模型 ID、真实语义检索或生成质量。
公司文件服务、真实多模态蒸馏、正式审核发布及多人权限均未验收。

先执行（不调用外部模型）：

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python validation/cloud_e2e.py
```

cloud_e2e 是显式 fixture 模式，可重复验证隔离数据库，但不能据此声明 Qwen/DeepSeek 已可用。
已装入本机配置后，用合成短文本做两次真实模型调用（会产生少量 API 用量）：

```bash
.venv/bin/python validation/model_preflight.py
.venv/bin/python validation/api_model_smoke.py
```

preflight 只打印配置存在性/非机密 endpoint/model；smoke 不打印 key 或原始认证错误内容。
成功后才用用户指定的脱敏 Word 做真实客户端蒸馏与 Qwen 入库/检索、DeepSeek 生成。
Word 原件只保存不自动解析/索引；只对明确选定的 Word 启动蒸馏。草稿入开发库不等于正式发布。

## 排除与交接

见 `handoff/EXCLUSIONS.txt`。zip、bundle 和补丁都不包含真实 .env、.env.vendor、客户文档、运行数据、
数据库、卷、虚拟环境、缓存、Git 本地配置、模型权重或历史生成的私有产物。
包只导出 afcffff 可达的代码/规范/模板/测试/文档历史；不含其他本地分支或远程认证设置。
