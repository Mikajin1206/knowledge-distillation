# Knowledge Distillation（知识蒸馏）

[简体中文](README.zh-CN.md) · [English](README.md)

**Knowledge Distillation** 是一个本地优先的工作流，用于把选定的 Word 文档整理成有来源、可追溯的项目知识草稿。系统保存项目原件，引导客户端模型按项目全生命周期处理证据，并导出带来源元数据和可校验内容哈希的 Markdown。

项目面向 WorkBuddy 及兼容 stdio 的 MCP 客户端。当前包含的是本地存储适配器；公司文件服务器接入和 RAG 发布属于独立的后续工作。

## 功能

- 按项目保存原件及其元数据，包括生命周期阶段、标签、上传人、SHA-256 和版本历史。上传过程不解析、不蒸馏、不索引。
- 仅蒸馏 `.docx` 和 `.doc`。其他格式可以作为原件保存，但会被蒸馏入口拒绝。
- 按需读取 Word 结构，包括段落、表格和内嵌图片。相关图片由客户端多模态模型直接理解；服务端不执行 OCR 或视觉推理。
- 从知识草稿中排除封面、目录、使用对象说明、版本记录、图例、页眉页脚和无关装饰图片。
- 记录来源版本、材料缺口、人工补充和可配置质量评分。低分会如实披露，但不会阻止草稿导出。
- 导出 Markdown 草稿和 JSON sidecar。草稿包含 SHA-256，可独立校验。

## 业务范围与数据边界

工作流按照项目整体生命周期收集证据：立项与范围、调研与需求、方案与设计、实施与变更、测试与验收、运维与改进。收集过程不围绕售前问题组织。售前、实施和运维 Agent 可以在后续任务中检索并使用这套共同的项目知识。

原始材料和蒸馏知识承担不同职责：

1. **原件库：**保存原始文件及其元数据。当前适配器默认将其保存在本机 `data/` 目录。
2. **知识草稿：**从选定的 Word 材料生成 Markdown 和 JSON sidecar，默认保存在 `output/distilled/`。
3. **RAG：**系统不会自动发布到 RAG。导出文件仍是草稿，需要经过独立审核和发布流程后才能进入正式检索。

本地适配器适用于开发和单机使用。它不是公司文件服务器、多人共享库、备份系统或生产级权限边界。请将 `data/`、`output/` 和 `tmp/` 作为私有数据目录管理，不要提交到版本库。

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

MCP 服务将项目和原件管理、蒸馏指引与材料读取、Word 正文和图片传递、质量报告骨架、草稿提交与导出、哈希校验及任务清理组合为一套工作流。工具名称和参数结构见 `src/ckp/mcp_server.py`，调用顺序以服务端 instructions 为准。

## 图片处理

系统会先根据文档结构和上下文筛选 Word 图片。只有位于核心正文且可能承载知识的图片才应提交视觉复核。PNG、JPEG、GIF 和 WebP 可直接传递；SVG、EMF、WMF、BMP 和 TIFF 需要 LibreOffice 转成 PNG。客户端多模态模型负责理解相关图片和流程图；对不确定内容应标注待确认，不得臆造节点或连线。系统不使用 OCR。

## 质量评分

评分权重由 [`templates/蒸馏规范/评分权重.json`](templates/蒸馏规范/评分权重.json) 维护，权重合计必须为 100。任务启动时会固定一份不可变的评分配置快照；后续修改只影响新任务，不会改变历史评分。客户端负责评估 `owner=client` 的维度，来源可追溯性和结构检查由服务端计算。评分用于后续排序和风险提示，不代表项目经理审核或发布许可。

## 隐私与安全

- MCP 服务能够读取客户端明确选定的文件。请仅在可信用户和客户端环境中配置服务。
- 数据目录包含上传原件、元数据和任务记录，应放在受保护的本机存储位置。
- 运行服务时，将 `CKP_DATA` 和 `CKP_OUTPUT` 设置为私有目录。不要把机密原件、生成的草稿、数据库、日志或凭据放进公开仓库。
- 当前本地实现不提供集中身份认证、多人访问控制、远程传输或公司文件服务器授权能力。
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
- 导出结果是草稿；当前没有审核状态机，也不会自动接入 RAG。
- 图片理解依赖所连接客户端的多模态模型及其对 MCP 图片内容的支持。

## 许可证

本项目采用 MIT License。详见 [`LICENSE`](LICENSE)。
