> Mac 本地交接入口：[HANDOFF-MAC.md](HANDOFF-MAC.md)。此导出包包含独立本地适配；云端原提交保留在 bundle 中。

# Knowledge Distillation

[简体中文](README.zh-CN.md) · [English](README.md)

**Knowledge Distillation** is a local-first workflow for turning explicitly selected Word documents into traceable project knowledge drafts. The 1.2.1 release was accepted through WorkBuddy with 30 tools. The restored 1.3.0 implementation adds `metadata_catalog` on the same stdio MCP service, for 31 tools total; WorkBuddy verified the OA product/domain metadata path through revision, review, publish, and filtered retrieval. The full project test suite passes (138 tests). The review remains local development evidence, not business-content acceptance or production deployment.

RAG uses the six lifecycle stages in `templates/蒸馏规范/阶段材料清单.md` as its primary classification. The restored 1.3.0 implementation adds project-manager-selected product line (`products`, one string) and related domains (`domain`, a list) as independent filter dimensions. These values come from the metadata catalog, never from model inference; stage remains an orthogonal primary classification and stays out of Markdown frontmatter.

The project is designed for WorkBuddy and compatible stdio MCP clients. Local development includes review records, asynchronous publication tasks, chunk read-back verification, and versioned diagram JSON storage and retrieval. The storage adapter is `local_versioned_store`, and the identity scope is `local_development`; company file-server integration, remote authentication, and production publishing are not implemented.

## Developer documentation

The current product requirements, implementation handoff, and downstream acceptance criteria are available in Chinese:

- [Product requirements](docs/01-%E9%9C%80%E6%B1%82%E8%A7%84%E6%A0%BC.md)
- [Product iteration board (single development task board)](docs/%E4%BA%A7%E5%93%81%E8%BF%AD%E4%BB%A3%E7%9C%8B%E6%9D%BF.md)
- [Current implementation and RAG handoff contract](docs/05-%E4%B8%8A%E4%BC%A0%E8%92%B8%E9%A6%8F%E4%B8%8EMD%E4%BA%A7%E5%87%BA%E4%BA%A4%E6%8E%A5%E6%96%87%E6%A1%A3.md)
- [Single-MCP local RAG development and black-box acceptance](docs/07-%E5%8D%95MCP%E6%9C%AC%E5%9C%B0RAG%E5%BC%80%E5%8F%91%E4%B8%8E%E9%BB%91%E7%9B%92%E9%AA%8C%E6%94%B6.md)
- [Downstream development and acceptance standards](docs/%E4%B8%8B%E6%B8%B8%E5%BC%80%E5%8F%91%E9%A1%BB%E7%9F%A5%E4%B8%8E%E9%AA%8C%E6%94%B6%E6%A0%87%E5%87%86.md)

Historical test reports, process logs, customer files, and local configuration are intentionally excluded from this public repository.

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

The workflow collects evidence across the full project lifecycle: initiation, discovery, solution/design, implementation/change, testing/acceptance, and operations/improvement. It does not collect evidence around a presales question. Presales, implementation, and operations agents can later retrieve and apply the shared project knowledge for their own tasks.

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

The verified 1.2.1 MCP release exposed 30 tools. The 1.3.0 single MCP server exposes 31 tools, adding `metadata_catalog` to that set: 21 project, original-file, Word distillation, draft, and task tools plus `knowledge_status`, `submit_for_review`, `record_review`, `publish_knowledge`, `get_publish_task`, `search_knowledge`, `read_knowledge`, `get_asset`, and `withdraw_knowledge`. WorkBuddy verified the OA product/domain metadata flow through review, publish, and positive/negative filtered retrieval; project tests pass (138 tests). WorkBuddy withdrawal and several negative-input cases remain untested. Tool names and schemas are defined in `src/ckp/mcp_server.py` and `src/ckp/knowledge_tools.py`.

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
