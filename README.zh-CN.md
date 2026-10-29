# Knowledge Distillation（知识蒸馏）

[简体中文](README.zh-CN.md) · [English](README.md)

**公司知识与业务智能体**以本仓库的知识蒸馏/RAG为基础。第一阶段主线：明确 Agent 平台选型 → 建设最小公共底座 → 通过本地或隔离入口交付售前首个闭环（搜知识、客户画像、需求分析、详细售前方案）。平台经本地最小兼容验证并于同日修订选定 Python LangGraph（复用现有 Python 代码栈，原 Java/Spring AI Alibaba 定案作废），现有 Python 知识 MCP 保留；销售管理与其他岗位应用后续安排，RAG 调优和其他应用优化统一归最后优化类别。公司模型代理及员工模型授权、配额用量、公司对话管理已移出本项目，另作独立项目。企业微信租户接入作为所有 Agent 的最后统一任务，当前不做。公共底座知识只读适配已有本地开发切片，新业务应用尚未实现。

**当前基线（2026-10-26）：**知识MCP代码1.5.6，静态注册38工具。原件、六阶段Word蒸馏、本地审核/发布/检索/读取/图形/撤回，以及产品Word手册独立库和按图号/位置回原图已在看板约定范围内验收；历史证据见docs/07。38是代码清点，不是本轮客户端加载实测。身份仍是`local_development`、文件适配仍是`local_versioned_store`；公司文件服务器、远程认证、生产发布和多人权限未实现，K16继续暂停。

RAG主分类仍为项目六阶段，PM从目录单选产品线并多选关联领域，不从材料推断；产品手册是独立资料库、不套阶段。销售画像与销售知识将独立分域，不自动写入公共RAG。本项目仅做获准国产 API 的最小模型调用适配，个人知识客户端继续按已有授权范围使用单 MCP。

完整产品、开发和迭代规则从[文档导航](docs/README.md)进入。新规划不表示应用开发、采购、个人配置修改或部署已经完成。

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

工作流按照项目整体生命周期收集证据：立项与范围、调研与需求、方案与设计、实施与变更、测试与验收、运维与改进。收集过程不围绕售前问题组织。售前、实施、运维和开发 Agent 可以在后续任务中检索并使用这套共同的项目知识。

原始材料和蒸馏知识承担不同职责：

1. **原件库：**保存原始文件及其元数据。当前适配器默认将其保存在本机 `data/` 目录。
2. **知识草稿：**从选定的 Word 材料生成 Markdown 和 JSON sidecar，默认保存在 `output/distilled/`。
3. **本地 RAG 开发：**用户明确请求时，可在本地开发沙箱审核并发布指定知识版本，执行逐块读回校验、检索/读取、读取版本化图形 JSON 和撤回版本。这不代表生产发布。

本地适配器适用于开发和单机使用。`CKP_LOCAL_ACTOR` 与 `CKP_REVIEWER_IDS` 绑定 `local_development` 本机身份，不是生产认证。它不是公司文件服务器、多人共享库、备份系统或生产级权限边界。请将 `data/`、`output/` 和 `tmp/` 作为私有数据目录管理，不要提交到版本库。

## 下游开发文档

- [总产品需求](docs/01-需求规格.md)
- [总体架构与开发文档](docs/02-总体架构与开发文档.md)
- [四类业务Agent方案](docs/03-业务Agent产品与开发方案.md)
- [产品迭代看板（唯一任务看板）](docs/产品迭代看板.md)
- [当前知识代码契约](docs/05-上传蒸馏与MD产出交接文档.md)
- [本地RAG与WorkBuddy验收记录](docs/07-单MCP本地RAG开发与黑盒验收.md)
- [下游开发验收标准](docs/下游开发须知与验收标准.md)
- [项目记忆与文档同步约定](docs/项目记忆.md) / [AGENTS.md](AGENTS.md)

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

当前MCP代码版本1.5.6，静态38工具：`mcp_server.py`中22个项目/目录/原件/蒸馏工具，`knowledge_tools.py`中9个生命周期工具，`product_material_tools.py`中7个产品资料工具。历史1.2.1的30工具、1.3.0的31工具及对应测试数保留原版本口径；后续撤回与真实产品手册验收已完成。当前客户端加载列表和未来Agent/代理需各自验证，见docs/05、docs/07。

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


### Agent 隔离本地 API（开发中）

独立依赖见 `requirements-agent.txt`；当前 macOS ARM64 固定版本见 `requirements-agent-macos.lock.txt`，生产和其他平台尚未验收。服务只绑定 `127.0.0.1`。先在进程环境中设置 `AGENT_LOCAL_ORG`、`AGENT_LOCAL_EMPLOYEE`、`AGENT_LOCAL_TOKEN`、`AGENT_LOCAL_DB`，再运行 `PYTHONPATH=src python -m agent_foundation.local_server`。模型默认关闭；若需运行本地模型任务，还需显式设置 `AGENT_LOCAL_MODEL_ENABLED=1`、`AGENT_MODEL_BASE_URL`、`AGENT_MODEL_NAME` 和 `AGENT_MODEL_API_KEY`，并单独运行 `PYTHONPATH=src python -m agent_foundation.local_model_worker` 处理一个任务。实际企业身份、企微、生产数据库和知识权限未接通。
