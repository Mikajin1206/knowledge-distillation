# 云端开发与验证

合并工作区：`/workspace/knowledge-distillation-cloud`，分支 `cloud/rag-integration`。
最新蒸馏基准为 `97ec6fe`；`src/ckp`、模板、Skill 与原有 MCP 工具保持该提交的实现。
原工作区 `/workspace/knowledge-distillation` 保留不动。

交付 ZIP 与全部解压内容分别保存在 `/workspace/delivery/original` 和 `/workspace/delivery/extracted`。
解压原件包含开发人员的 macOS 虚拟环境；云端另建 Linux Python 环境，不复用它。

## 合并内容

保留开发交付的 MongoDB、Milvus、OpenAI 兼容 embedding、文件服务适配器与方案生成模块。
入库入口改用最新 CKP 的正文哈希、章节路由和 sidecar 契约。只支持 `body`/`exclude`；
旧 `metadata`/`audit` 标记、无效或重复标记拒绝入库，不猜测、不自动改写。

原来的全目录扫描与删除集合已移除。现在显式传入一个 MD，必须存在同名 JSON。
向量保留完整来源版本、知识版本、章节路径、位置偏移和保护资产引用。
MD/JSON 归档在本机 `.cloud/archive`，MongoDB 保存清洁检索视图及完整 sidecar。
这里的源定位是导出中的版本引用；本次没有连接公司文件服务器重新验证 Word 原件。

MongoDB 使用 `kd_dev` 库，Milvus 集合使用 `kd_dev_fixture_`、`kd_dev_local_` 或旧适配器的 `kd_dev_qwen_` 前缀。
所有内容均为开发沙箱，允许验证 draft；不表示项目经理审核通过，不提供正式发布。
不写入生产集合。原有版本和审计记录保留，默认读取/检索只使用当前活动版本。

入库先准备新版本，全部向量写入并 flush 后才切换 MongoDB 活动指针；
失败重试使用稳定 chunk ID，部分写入不进入默认检索。活动指针跨知识类型共享，
变更类型也会让旧集合版本退出默认检索。MongoDB 租约防止多个进程同时写同一知识，
运行期间续租，进程中断后最多约 10 分钟自动过期。

## 服务

- MongoDB 4.0.28：`mongodb://127.0.0.1:27017`
- Milvus 2.6.24：`http://127.0.0.1:19530`
- 开发 HTTP API：`http://127.0.0.1:8001/docs`

这些地址都位于云电脑本机。HTTP API 无身份验证，仅绑定 loopback，不应直接公开。

```bash
cd /workspace/knowledge-distillation-cloud
./dev-services.sh start
./dev-services.sh status
./dev-services.sh stop
# 数据库容器管理
/workspace/databases/manage.sh ps
```

HTTP `/rag` 接受 `action=ingest|search|read|withdraw`，参数与下方 MCP 一致。
`/generate_solution` 和流式接口保留开发交付的方案 Agent；在当前 codex-client 测试模式下明确停用，不会向旧 DeepSeek 服务发请求。
默认 HTTP 服务使用 fixture RAG；自动方案生成不会模拟回答，交互生成使用 Codex 客户端。
HTTP 的普通进程不会随云电脑重启自动恢复，重启后执行 start。

## MCP 与 CLI

`cloud.mcp.example.json` 提供云端进程使用的绝对路径配置。它适用于云电脑内的兼容 MCP 客户端，
不是在 Mac 上直接执行云端路径的远程连接配置。

- `ckp-mcp.sh`：最新蒸馏 MCP，原有 21 个工具。
- `rag-mcp.sh`：单独的开发 RAG MCP，仅一个工作流工具 `rag_knowledge`，不改变蒸馏工具集。
- `ai-generate-solution-mcp.sh`：可选方案生成 Agent；未加入默认 MCP 配置。

```bash
cd /workspace/knowledge-distillation-cloud
./rag.sh --embedding fixture ingest /absolute/path/to/export.md
./rag.sh --embedding fixture search '采购审批' --knowledge-type '方案设计'
./rag.sh --embedding fixture read '方案设计' k-example
./rag.sh --embedding fixture read '方案设计' k-example --revision 1
./rag.sh --embedding fixture withdraw '方案设计' k-example
```

`read --revision` 是显式历史回读；撤回只退出默认读与检索，归档与历史仍保留。
同一修订内容变化会拒绝，必须通过最新蒸馏流程创建新修订。
入库失败后可重复同一命令；已成功的同版本重复导入返回 unchanged；重复导入不会解除撤回。

`fixture` 使用确定性测试向量，只验证切片、数据库和版本流程，不能评价语义质量。
`local` 使用本机 BGE-small-zh-v1.5（512 维），另存于 `kd_dev_local_` 集合；
旧 `qwen` 适配器保留但不用于本次测试。不同向量空间不混用。

## 当前模型方案（用户指定）

生成/交互蒸馏：在 **Codex 客户端选择 GPT-6 luna，中等推理强度**，通过最新蒸馏 MCP
和开发 RAG MCP 测试。Codex 订阅不等于应用 API；当前未提供应用网关。
`LLM_PROVIDER=codex-client` 会在自动方案生成入口明确报未接通，不调用 DeepSeek。
`LLM_MODEL_NAME=gpt-6-luna` / `LLM_REASONING_EFFORT=medium` 记录测试意图，
不会替用户切换客户端模型，也不证明该名称是任何供应商已验证的 API model ID。
原 DeepSeek 配置只保留在私有 `.env.vendor`，当前 `.env` 不复用其 API key。

向量化替代：**BAAI/bge-small-zh-v1.5**，中文、512 维、MIT 许可；采用 FastEmbed +
ONNX Runtime CPU（2 线程，batch 8）。官方说明见
[FlagEmbedding](https://github.com/FlagOpen/FlagEmbedding/tree/master/research/baai_general_embedding)。
模型发行权重来自 [Qdrant 的 ONNX 包](https://huggingface.co/Qdrant/bge-small-zh-v1.5)，
包目录约 0.09 GB。分节仍由最新 CKP 显式章节标记完成，不需要大模型。
长节再用该模型 tokenizer 按 512-token 上限分片；不会静默截断正文。

本机运行适合当前 3 CPU / 约 9.7 GiB 内存的开发环境，但本次尚未完成权重下载，
因此不能宣称 BGE 已成功出向量或检索质量已验收。FastEmbed/ONNX 运行依赖已安装。
云端代理对 `huggingface.co` 的探测同样返回 CONNECT 403。
需要放行模型下载域名（及实际重定向域名），或者离线上传同一个 ONNX 模型目录。
模型下载成功后运行不再依赖外部模型 API。

```bash
cd /workspace/knowledge-distillation-cloud
.venv/bin/python validation/install_local_model.py
./rag.sh --embedding local ingest /absolute/path/to/export.md
./rag.sh --embedding local search '需要验证的业务问题'
./dev-services.sh stop
KD_EMBEDDING_MODE=local ./dev-services.sh start
```

`KD_LOCAL_MODEL_PATH` 指向 `.cloud/models/bge-small-zh-v1.5`。缺少权重时明确报错，
不自动降级到 Qwen 或 fixture。旧测试向量需重新生成本机 BGE 向量，不能复用。

源码中原有硬编码 OCR 凭据已清除，不调用 OCR；建议更换原先硬编码的凭据。
`.env`、`.env.vendor`、客户 MD/JSON、归档、数据库和日志不进入代码提交。
公司文件服务适配器保留，但默认使用本机归档，尚未验证公司文件服务与保护图资产。

## 本次验证与复现

2026-10-22：99 项自动化测试通过；pip check 通过。
最新蒸馏 MCP + 独立 RAG MCP + 真实数据库通过完整流程及失败路径检查。
合成 Word 和合成模型草稿用于验证，不代表真实多模态模型已完成蒸馏。
交付的 9 份 MD/JSON 中 7 份通过契约与入库/检索/回读检查；2 份旧路由拒绝并保留原样。

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python validation/cloud_e2e.py
.venv/bin/python validation/delivery_samples.py
.venv/bin/python validation/http_check.py
.venv/bin/python validation/model_preflight.py
```

证据：`.cloud/validation/result.json`、`.cloud/delivery-samples.json`，以及
`/workspace/cloud-setup/knowledge-distillation/reports/` 中的 merged-tests.log、cloud-e2e.log、
delivery-samples.log、http-check.log、model-preflight.json。

尚未通过：本机 BGE 权重安装及真实语义向量、Codex GPT-6 luna 的客户端实测、真实 Word 图像模型蒸馏、公司文件服务、
正式审核发布及多人权限。不得把测试向量的命中解释为检索质量验收。

## 重建环境

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-cloud.lock.txt
```

锁文件来自本次 Linux/Python 3.12 环境。`requirements-cloud.txt` 为直接依赖及关键兼容版本。
保留原交付依赖清单于本机 `vendor-requirements.txt`，没有安装与现有运行入口无关的完整桌面依赖。
MongoDB 驱动固定为 4.10.1，以兼容本次指定的 MongoDB 4.0。
