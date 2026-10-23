# Knowledge Distillation（知识蒸馏）

[简体中文](README.zh-CN.md) · [English](README.md)

**Knowledge Distillation** 是一个本地优先的工作流，用于把用户明确指定的 Word 文档整理成有来源、可追溯的项目知识草稿。1.2.1 版已通过 WorkBuddy 验收，共 30 个工具。1.3.0 在同一 stdio MCP 服务新增 `metadata_catalog`，共 31 个工具；WorkBuddy 已验证 OA 产品/领域元数据贯穿修订、审核、发布与正反向检索，项目完整测试 138 项通过。本次审核仅为本地开发链路证据，不代表业务内容验收或生产部署。

RAG 主分类采用 `templates/蒸馏规范/阶段材料清单.md` 的六个生命周期阶段。1.3.0 已恢复 PM 从 metadata catalog 单选产品线（`products` 为单个字符串），再从关联项多选领域（`domain` 为数组），作为独立筛选维度；服务拒绝缺选、多产品和产品/领域错配，不从材料推断选择。`stage` 仍是主分类，来自受审核 sidecar，不加入 MD 最小头。项目面向 WorkBuddy 及兼容 stdio 的 MCP 客户端。本地开发已具备审核记录、异步发布任务、逐块读回校验，以及图形 JSON 的版本化独立存储和读取。当前文件适配器为 `local_versioned_store`，身份范围为 `local_development`；公司文件服务器、远程认证和生产发布尚未实现。

## 功能

- 按项目保存原件及其元数据，包括生命周期阶段、标签、上传人、SHA-256 和版本历史。上传过程不解析、不蒸馏、不索引。
- 仅蒸馏 `.docx` 和 `.doc`。其他格式可以作为原件保存，但会被蒸馏入口拒绝。
- 按需读取 Word 结构，包括段落、表格和内嵌图片。相关图片由客户端多模态模型直接理解；服务端不执行 OCR 或视觉推理。
- 从知识草稿中排除封面、目录、使用对象说明、版本记录、图例、页眉页脚和无关装饰图片。
- 记录来源版本、材料缺口、人工补充和可配置质量评分。低分会如实披露，但不会阻止草稿导出。
- 导出 Markdown 草稿和 JSON sidecar。草稿包含 SHA-256，可独立校验。
- 每个 Markdown 章节带明确路由标识：`body` 或 `exclude`。解析器只按标识决定检索正文或排除内容；标识缺失、非法、重复或仍使用旧路由时拒绝生成检索正文。来源、质量证据和审核发布记录保存在 sidecar 与服务记录中。

## 章节路由

每个 `##` 至 `######` 标题都带稳定章节 ID 和路由标识，例如：

```md
## 采购流程 <!-- kd-section id=procurement_workflow rag=body -->
```

`body` 进入检索正文，`exclude` 不进入检索输出。解析器不从标题文字猜用途；标识缺失、格式错误、重复或旧版 `metadata`/`audit` 时，路由清单无效且不产生检索正文。HTML 注释只作路由元数据，抽取正文时会移除。

## 业务范围与数据边界

工作流按照项目整体生命周期收集证据：立项与范围、调研与需求、方案与设计、实施与变更、测试与验收、运维与改进。收集过程不围绕售前问题组织。售前、实施和运维 Agent 可以在后续任务中检索并使用这套共同的项目知识。

原始材料和蒸馏知识承担不同职责：

1. **原件库：**保存原始文件及其元数据。当前适配器默认将其保存在本机 `data/` 目录。
2. **知识草稿：**从选定的 Word 材料生成 Markdown 和 JSON sidecar，默认保存在 `output/distilled/`。
3. **本地 RAG 开发：**用户明确请求时，可在本地开发沙箱审核并发布指定知识版本，执行逐块读回校验、检索/读取、读取版本化图形 JSON 和撤回版本。这不代表生产发布。

本地适配器适用于开发和单机使用。`CKP_LOCAL_ACTOR` 与 `CKP_REVIEWER_IDS` 绑定 `local_development` 本机身份，不是生产认证。它不是公司文件服务器、多人共享库、备份系统或生产级权限边界。请将 `data/`、`output/` 和 `tmp/` 作为私有数据目录管理，不要提交到版本库。

## 下游开发文档

以下文档说明产品目标、当前代码契约和后续验收要求：

- [需求规格](docs/01-需求规格.md)
- [产品迭代看板（唯一开发任务看板）](docs/产品迭代看板.md)
- [当前代码契约与 RAG 接续标准](docs/05-上传蒸馏与MD产出交接文档.md)
- [单 MCP 本地 RAG 开发与黑盒验收](docs/07-单MCP本地RAG开发与黑盒验收.md)
- [下游开发须知与验收标准](docs/下游开发须知与验收标准.md)

历史测试报告、过程日志、客户文件和本地配置不纳入公开仓库。

## 环境要求

- macOS 或 Linux
- 推荐 Python 3.12（核心 CLI 支持 Python 3.9 及以上版本）
- MCP 服务端需要 Python 包 `mcp>=2.3,<3`
- LibreOffice 可选：用于转换 `.doc`，以及把 SVG、EMF、WMF、BMP、TIFF 图片转为 MCP 客户端可读取的 PNG。未安装时，相关转换会明确报错，原件保持不变。
- 需要通过 WorkBuddy 或兼容的 MCP 客户端连接支持多模态的模型，才能识别相关内嵌图片。

## 安装

```sh
git clone https://github.com/Mikajin1206/knowledge-distillation.git
cd knowledge-distillation
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-mcp.txt
chmod +x ckp.sh ckp-mcp.sh
```

CLI 本身只使用 Python 标准库。安装 `requirements-mcp.txt` 后即可运行 MCP 服务端。

## 命令行使用

```sh
./ckp.sh --help
./ckp.sh project create "示例项目" --industry "制造业" --products "ERP" --owner "项目经理"
./ckp.sh upload "/绝对路径/方案.docx" --project "示例项目" --stage "方案与设计" --uploader "姓名"
./ckp.sh file list --project "示例项目"
```

蒸馏流程如下：

1. 创建项目或选择已有项目。
2. 上传原件。上传只登记元数据和内容哈希，不解析文件。
3. 按六个项目阶段之一创建蒸馏任务，并关联选定的 Word 文件版本。
4. 每个任务读取一次蒸馏指引，通过 MCP 工作流读取 Word 内容，再由客户端模型生成知识草稿和质量报告。
5. 提交并导出草稿。核验返回的哈希后关闭任务，清理任务临时副本。

CLI 命令分组包括 `project`、`upload`、`file`、`distill` 和 `draft`。使用 `./ckp.sh <命令分组> --help` 查看选项。导出文件默认写入 `output/distilled/`；原件和数据库默认写入 `data/`。可以设置 `CKP_DATA` 和 `CKP_OUTPUT`，将数据保存到仓库目录之外的私有位置。

## 配置 WorkBuddy / stdio MCP

服务端启动命令：

```sh
./ckp-mcp.sh
```

可参考 [`knowledge-distillation.mcp.example.json`](knowledge-distillation.mcp.example.json) 配置。将 command、数据目录和导出目录改为本机的绝对路径，再把 `mcpServers` 配置合并到 MCP 客户端配置中。保存后重新启动或重连 MCP 客户端。

Skill 位于 [`skills/knowledge-distillation/`](skills/knowledge-distillation/)。使用客户端提供的 Skill 安装方式安装该目录。Skill 会引导用户完成项目选择、原件上传、Word 选择、生命周期阶段蒸馏、图片处理、评分、导出和清理；同时要求模型只处理用户指定的文件，不枚举无关本地文件，也不绕过 MCP 工作流。

旧 `knowledge-rag-development` 与 `ai-generate-solution` 模块仅为内部开发兼容保留，不再单独对外注册 MCP 服务。方案生成独立入口已停用；客户端可使用主 MCP 提供的知识工具结果自行组织生成流程。

已通过 WorkBuddy 验收的 1.2.1 版为 30 个工具；1.3.0 单一 MCP 服务提供 31 个工具，新增 `metadata_catalog`：原有 21 个项目、原件、Word 蒸馏、草稿和任务工具，以及 `metadata_catalog`、`knowledge_status`、`submit_for_review`、`record_review`、`publish_knowledge`、`get_publish_task`、`search_knowledge`、`read_knowledge`、`get_asset`、`withdraw_knowledge`。WorkBuddy 已完成 OA 正向入库和分类正反向检索验收，项目完整自动化套件 138 项通过；撤回和若干负向输入尚未做 WorkBuddy 黑盒验收。工具结构分别见 `src/ckp/mcp_server.py` 与 `src/ckp/knowledge_tools.py`。

## 图片处理

系统会先根据文档结构和上下文筛选 Word 图片。只有位于核心正文且可能承载知识的图片才应提交视觉复核。PNG、JPEG、GIF 和 WebP 可直接传递；SVG、EMF、WMF、BMP 和 TIFF 需要 LibreOffice 转成 PNG。客户端多模态模型负责理解相关图片和流程图；对不确定内容应标注待确认，不得臆造节点或连线。系统不使用 OCR。

## 质量评分

评分权重由 [`templates/蒸馏规范/评分权重.json`](templates/蒸馏规范/评分权重.json) 维护，权重合计必须为 100。任务启动时会固定一份不可变的评分配置快照；后续修改只影响新任务，不会改变历史评分。客户端负责评估 `owner=client` 的维度，来源可追溯性和结构检查由服务端计算。评分用于后续排序和风险提示，不代表项目经理审核或发布许可。

## 隐私与安全

- MCP 服务能够读取客户端明确选定的文件。请仅在可信用户和客户端环境中配置服务。
- 数据目录包含上传原件、元数据和任务记录，应放在受保护的本机存储位置。
- 运行服务时，将 `CKP_DATA` 和 `CKP_OUTPUT` 设置为私有目录。不要把机密原件、生成的草稿、数据库、日志或凭据放进公开仓库。
- 当前只实现本机开发身份和 `local_development` 范围；集中或远程认证、生产多人授权和公司文件服务器接入尚未实现。
- 不要提交真实项目文件或包含客户信息的知识草稿。

## 项目结构

```text
ckp.sh                         CLI 启动脚本
ckp-mcp.sh                     stdio MCP 启动脚本
src/ckp/                       存储、蒸馏、Word 检查、导出和 MCP 服务
skills/knowledge-distillation/ 客户端工作流 Skill
templates/                     项目知识模板与蒸馏规范
requirements-mcp.txt           MCP 服务端依赖
```

运行数据（`data/`、`output/`、`tmp/`）、Python 环境、缓存和本地 MCP 配置均不包含在仓库中。

## 当前限制

- 仅支持蒸馏 Word 文档。Excel、PowerPoint、PDF 和独立图片可以作为原件保存，但不会解析或传递给模型。
- 本地内容寻址存储基于 SQLite，适用于单机使用。
- 导出通过本地文件适配器返回显式导出路径，属于现有交付契约。服务器模式后续迁移为经授权的下载链接。本地审核和 RAG 发布仅限开发沙箱；公司文件服务器、远程认证和生产发布尚未实现。
- 图片理解依赖所连接客户端的多模态模型及其对 MCP 图片内容的支持。

## 许可证

本项目采用 MIT License。详见 [`LICENSE`](LICENSE)。
