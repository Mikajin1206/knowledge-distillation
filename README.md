> Mac 本地交接入口：[HANDOFF-MAC.md](HANDOFF-MAC.md)。此导出包包含独立本地适配；云端原提交保留在 bundle 中。

# Knowledge Distillation

[简体中文](README.zh-CN.md) · [English](README.md)

**Company Knowledge and Business Agents** builds on this repository's Knowledge Distillation/RAG service. Stage one has three tasks: select the Agent platform, build the minimum shared foundation, and deliver the first WeCom presales workflow covering knowledge search, customer profiling, requirements analysis and a detailed proposal. Local minimum compatibility probes selected Spring AI Alibaba Agent Framework/Graph 1.1.2.2 for the Java/Spring Agent service; the existing Python knowledge MCP remains. Sales management and other role agents come later. RAG tuning and other application optimization are grouped in a separate final phase. The company model gateway and its employee credentials, quotas, usage and conversation management belong to an independent project. These new Agent features are not implemented.

**Current baseline, 2026-10-26:** the knowledge MCP declares version 1.5.6 and statically registers 38 tools. Original storage, lifecycle Word distillation, local review/publication/search/read/assets/withdrawal, and the independent Word product-manual corpus have acceptance records within their documented local scope. The count is a code inventory, not a fresh client loading test. Identity remains `local_development`, storage remains `local_versioned_store`, and company file services, remote authentication, production publication and multi-user permissions are not implemented. K16 production adaptation remains paused.

The shared corpus covers all six project lifecycle stages. Product manuals have an independent corpus; sales knowledge is a separate planned domain. Generated agent output does not automatically become public knowledge. Personal knowledge clients use the single knowledge MCP within their verified capabilities; company model gateway integration belongs to the independent project.

## Product and developer documentation

The canonical documents are in Chinese. See the [documentation index](docs/README.md):

- [Product requirements](docs/01-需求规格.md)
- [Architecture and development](docs/02-总体架构与开发文档.md)
- [Business-agent workflows](docs/03-业务Agent产品与开发方案.md)
- [Single iteration board](docs/产品迭代看板.md)
- [Current knowledge interface contract](docs/05-上传蒸馏与MD产出交接文档.md)
- [Local MCP and client acceptance records](docs/07-单MCP本地RAG开发与黑盒验收.md)
- [Downstream acceptance requirements](docs/下游开发须知与验收标准.md)
- [Project memory and iteration synchronization](docs/项目记忆.md) / [AGENTS.md](AGENTS.md)

Only document work is delivered in this iteration; no new agent, gateway, database migration or production deployment is claimed. Private customer files, credentials and runtime data are excluded from the repository.

## What it does

- Stores original files with project metadata, lifecycle stage, tags, uploader, SHA-256, and version history. Upload does not parse, distill, or index files.
- Distills only `.docx` and `.doc` files. Other file types may be retained as originals but are rejected as distillation materials.
- Reads Word structure on demand, including paragraphs, tables, and embedded images. Relevant images are returned to the client's multimodal model for direct visual understanding; the server does not perform OCR or visual inference.
- Excludes covers, tables of contents, audience notes, revision history, legends, headers, footers, and unrelated decorative images from the knowledge draft.
- Records source versions, missing evidence, human-provided context, and a configurable quality score. A low score is disclosed and does not prevent draft export.
- Exports a Markdown draft and JSON sidecar. The draft carries a SHA-256 hash that can be checked independently.
- Marks every Markdown section with an explicit routing ID: `body` or `exclude`. The routing parser uses those markers instead of guessing from heading text and fails closed when a marker is missing, invalid, duplicated, or uses a legacy route. Sources, quality evidence, and review/publishing records remain in the sidecar and service records.

## Section routing

Every `##` through `######` heading carries a stable section ID and route, for example:

```md
## Procurement workflow <!-- kd-section id=procurement_workflow rag=body -->
```

`body` becomes retrieval text and `exclude` is omitted. The parser does not infer a route from heading wording. Missing, malformed, duplicate, or legacy `metadata`/`audit` markers make the routing manifest invalid and return no retrieval text. The HTML comments are routing metadata and are removed from extracted retrieval text.

## Scope and data boundary

The workflow collects evidence across the full project lifecycle: initiation, discovery, solution/design, implementation/change, testing/acceptance, and operations/improvement. It does not collect evidence around a presales question. Presales, implementation, operations, and development agents can later retrieve and apply the shared project knowledge for their own tasks.

Originals and distilled knowledge have separate roles:

1. **Original library:** retained source files and metadata. The included adapter stores these locally under `data/`.
2. **Knowledge drafts:** selected Word materials transformed into Markdown and a JSON sidecar under `output/distilled/`.
3. **Local RAG development:** an explicitly requested local workflow can review and publish a specific knowledge revision, verify chunk read-back, search/read published knowledge, retrieve versioned diagram JSON, and withdraw a version. This is a development sandbox and does not establish production publication.

The local adapters are intended for development and single-machine use. `CKP_LOCAL_ACTOR` and `CKP_REVIEWER_IDS` bind a local development identity with `local_development` scope; they are not production authentication. The adapters are not a company file server, shared multi-user repository, backup system, or production authorization boundary. Keep `data/`, `output/`, and `tmp/` private and out of version control.

## Requirements

- macOS or Linux
- Python 3.12 recommended (Python 3.9+ is used by the core CLI)
- Python package `mcp>=2.3,<3` for the MCP server only
- LibreOffice is optional for `.doc` conversion and conversion of SVG, EMF, WMF, BMP, and TIFF images into MCP-readable PNGs. Without it, those conversion paths stop with an explicit error; originals remain unchanged.
- A WorkBuddy or compatible MCP client with a multimodal model is needed for visual interpretation of relevant embedded images.

## Install

```sh
git clone https://github.com/Mikajin1206/knowledge-distillation.git
cd knowledge-distillation
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-mcp.txt
chmod +x ckp.sh ckp-mcp.sh
```

The CLI itself uses only the Python standard library. Installing `requirements-mcp.txt` enables the MCP server.

## Command-line use

```sh
./ckp.sh --help
./ckp.sh project create "Example Project" --industry "Manufacturing" --products "ERP" --owner "Project Manager"
./ckp.sh upload "/absolute/path/to/solution.docx" --project "Example Project" --stage "方案与设计" --uploader "Name"
./ckp.sh file list --project "Example Project"
```

The distillation lifecycle is:

1. Create or select a project.
2. Upload originals. Upload records metadata and content hashes without parsing files.
3. Start a task for one of the six project stages and attach selected Word versions.
4. Read the task guidance once, read Word content through the MCP workflow, and have the client model prepare the knowledge draft and quality report.
5. Submit and export the draft. Verify the returned hash, then close the task to remove task-scoped working copies.

Useful CLI groups are `project`, `upload`, `file`, `distill`, and `draft`. Run `./ckp.sh <group> --help` for options. Exported drafts default to `output/distilled/`; source originals and database records default to `data/`. Set `CKP_DATA` and `CKP_OUTPUT` to choose private storage locations outside the checkout.

## WorkBuddy / stdio MCP setup

The server starts with:

```sh
./ckp-mcp.sh
```

Use the included [`knowledge-distillation.mcp.example.json`](knowledge-distillation.mcp.example.json) as a template. Replace the command and data/output paths with absolute paths on your machine, then merge the `mcpServers` entry into the MCP client's configuration. Restart or reconnect the MCP client after saving the configuration.

The Skill is in [`skills/knowledge-distillation/`](skills/knowledge-distillation/). Install that directory using the Skill installation mechanism provided by your client. The Skill guides the user through project selection, original upload, Word selection, lifecycle-stage distillation, image handling, scoring, export, and cleanup. It also instructs the model not to enumerate unrelated local files or bypass the MCP workflow.

Legacy `knowledge-rag-development` and `ai-generate-solution` modules remain for internal development compatibility only and are not separately registered as external MCP servers. The solution-generation entry is inactive; clients can use knowledge returned by the primary MCP in their own generation workflow.

The current MCP code declares 1.5.6 and statically registers 38 tools: 22 project/catalog/original/distillation tools, nine knowledge-lifecycle tools, and seven product-material tools. Historical 1.2.1 (30 tools), 1.3.0 (31 tools), and test counts retain their original evidence scope; later withdrawal and real Word-manual acceptance are recorded in docs/07. This inventory is not a new client-loading test. Schemas are defined in `src/ckp/mcp_server.py`, `knowledge_tools.py`, and `product_material_tools.py`.

## Image handling

Word images are first filtered by document structure and context. Only images in core content that may carry relevant knowledge should be sent for visual review. PNG, JPEG, GIF, and WebP can be delivered directly. SVG, EMF, WMF, BMP, and TIFF require LibreOffice conversion to PNG. The client-side multimodal model interprets relevant images and flowcharts; it should describe uncertain details as unresolved rather than inventing nodes or connections. OCR is not used.

## Quality scoring

Scoring weights are maintained in [`templates/蒸馏规范/评分权重.json`](templates/蒸馏规范/评分权重.json). Weights must total 100. A task captures an immutable copy of the policy when it starts, so edits apply to new tasks without changing historical scores. Client-owned dimensions are assessed by the client model; source traceability and structural checks are calculated by the service. Scores help downstream ranking and review; they do not represent project-manager approval.

## Privacy and security

- The MCP service can read files explicitly selected by its client. Configure it only for trusted local users and clients.
- Keep the data directory on a protected local volume. It contains uploaded originals, metadata, and task records.
- Set `CKP_DATA` and `CKP_OUTPUT` to private locations when running the service. Do not place confidential originals, generated drafts, databases, logs, or credentials in a public repository.
- Only local development identity and scope are implemented. Centralized or remote authentication, production multi-user authorization, and company file-server integration are not implemented.
- Do not commit real project files or generated customer-specific knowledge.

## Project layout

```text
ckp.sh                         CLI launcher
ckp-mcp.sh                     stdio MCP launcher
src/ckp/                       storage, distillation, Word inspection, export, MCP
skills/knowledge-distillation/ Client workflow Skill
templates/                     project knowledge template and distillation policy
requirements-mcp.txt            MCP server dependency
```

Runtime data (`data/`, `output/`, `tmp/`), Python environments, caches, and local MCP configuration are excluded from the release.

## Current limitations

- Only Word documents can be distilled. Excel, PowerPoint, PDF, and standalone images are stored as originals but not parsed or passed to the model.
- The local content-addressed storage adapter is single-machine and SQLite-backed.
- Export creates an explicit local file deliverable. Local review and RAG publication are available only in the development sandbox; company file-server adapters, remote authorization, and production publishing are not implemented.
- Image understanding depends on the connected client's multimodal model and its ability to consume MCP image content.

## License

This project is distributed under the MIT License. See [`LICENSE`](LICENSE).
