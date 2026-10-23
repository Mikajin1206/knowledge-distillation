# 单 MCP 本地 RAG 开发与黑盒验收

## 范围与当前状态

1.2.1 已验收主链通过 WorkBuddy：单一 `knowledge-distillation` / `ckp-mcp.sh` 显示 30/30 工具启用。下述 20:39/20:56 结果均为 1.2.1 历史验收，不改写为后续版本证据。1.3.0 在同一服务新增 `metadata_catalog`，共 31 工具；产品线/领域契约已恢复至草稿、导出、sidecar、RAG 元数据和查询筛选，并于 2026-10-23 完成 WorkBuddy 正向黑盒验收。旧版 21 个项目/原件/蒸馏工具名称未在本次 UI 记录中逐项抄录，下表列出此前 9 个生命周期工具及 1.3.0 新工具：

| 工具 | 用途 |
|---|---|
| `metadata_catalog` (1.3.0) | 返回产品线及关联领域目录 |
| `knowledge_status` | 查看知识版本状态 |
| `submit_for_review` | 提交指定知识版本审核 |
| `record_review` | 记录用户授权的审核结论 |
| `publish_knowledge` | 启动发布任务 |
| `get_publish_task` | 等待/读取发布任务状态 |
| `search_knowledge` | 检索已发布知识 |
| `read_knowledge` | 读取命中章节或授权的完整正文 |
| `get_asset` | 按精确版本引用读取受保护图形 JSON |
| `withdraw_knowledge` | 撤回指定知识版本 |

本机开发身份范围为 `local_development`，文件适配器为 `local_versioned_store`。本地发布和资产读取不代表公司 file server、remote auth 或生产发布已接通。

**2026-10-22 20:39 WorkBuddy V5.8.3 完成 1.2.1 主 OA 发布与检索验收；20:56 完成目录、sections 与发布任务三项定向只读复测，均 PASS，UI 显示绿点 30/30。2026-10-23 完成 1.3.0 产品/领域正向黑盒验收，MCP 管理器显示 31/31 工具启用。** rev2 的第一次提交漏传 `quality_report`，造成 partial/55 分；该修订已独立审核发布且保留为历史记录。随后从 rev1 原样取回评分材料并提交 rev3，正文 hash 不变、分类正确，rev3 完整评分 91.0、65/65 chunks verified，Mongo/Milvus 检索正反过滤通过。服务端按本次任务自身资产清单将媒体/表格维度标为“不适用”满分，因此 91 与 rev1 的 89 不同；未修改 rev1/rev2，也未重用 rev1 审核。原 revision 1 快照 hash 经恢复代码重算仍为 `acad93e6fa295d19c9ada30ad5532f7f412e12bd98eb5fe1feb4ee23c6e2b82c`。撤回仍仅有 unit 覆盖。图片契约旧任务通过图片返回和临时清理；本轮 rev2/rev3 任务的 `distill_close` 返回 `tmp_removed=false`，MCP 没有只读查询临时副本状态的工具，故不声称已清理。

K01–K03 的 WorkBuddy 正向黑盒覆盖来自上述 OA 主流程：指定 Word 经读取、评分和草稿提交后完成 Markdown/sidecar 导出与校验；rev3 返回完整评分覆盖和分类元数据。该证据证明这份 OA 材料的主路径可用，不代表所有评分边界和非法章节标记都完成了客户端负向验收。

唯一真实验收原件：

- 文件：`/Users/moc/Documents/01 若邻工作/01 项目工作/52-HGKF/02-方案蓝图/后谷控股业务解决方案-OA系统V1.1.docx`
- 原件 SHA-256：`2bba268dad75964e81d3029fa23c6f90538c888c03313c173589b0f4e46807aa`（本次验证后保持不变）

主稿身份为 `k-2bdb26a1` rev1，正文 hash `49771248dc2c93df238b59082e4930a8ed08aa084fe837c928952b8b74a7abb5`；审核 `review-1a4fefca`，发布任务 `publish-eb837981`。`stage=方案与设计` 已在 review snapshot 内，补充属性 `knowledge_type=项目知识`。

## 需求与分类约定

RAG 主分类使用 `templates/蒸馏规范/阶段材料清单.md` 中确认的六个生命周期阶段；`knowledge_type` 是补充文档类型，不能限制阶段集合。`stage` 来自已审核 sidecar 路由集合及 metadata，不写入 MD 最小头。正文章节路由仍限 `body` / `exclude`。

| 六阶段主分类 | 典型范围 |
|---|---|
| 立项与范围 | 背景、目标、合同范围、交付边界、约束 |
| 调研与需求 | 现状流程、角色、需求、业务规则、需求确认 |
| 方案与设计 | 业务流程、架构、功能、接口、数据关系、设计取舍 |
| 实施与变更 | 配置、迁移、集成、操作步骤、变更原因、问题解决 |
| 测试与验收 | 测试场景、缺陷修复、验收结论、实际交付范围 |
| 运维与改进 | 环境版本、故障诊断、根因、处置验证与改进 |

产品线/领域元数据为 1.2.0 已有契约，1.3.0 恢复，不是新增业务定义：PM 从 `config/product_domain_catalog.json` 单选一条产品线，`products` 为单字符串；再从该产品线关联领域中多选，`domain` 为字符串数组。缺选、多产品及产品/领域错配在提交草稿时硬拒绝。配置路径由 `CKP_PRODUCT_DOMAIN_CATALOG` 指定时，目录查询和新草稿校验读取最新清单；已提交的分类值固定在草稿与审核快照中，不因后续清单变更阻断发布。PM 选择值需一致保存到草稿、JSON sidecar 与 RAG metadata，并由 `draft_export.frontmatter` 读回；检索可组合 stage、knowledge_type、products 和 domain 筛选。项目 `.venv` 完整套件最近运行 138 项通过。WorkBuddy 已完成正向写入、审核发布和正反向 RAG 筛选；本轮未通过 WorkBuddy 做缺选、多产品及错配负向提交。新 revision 必须重新审核，不能挪用 revision 1 的旧审核。

验收要求摘要：原件上传不自动解析、蒸馏或索引；仅对用户明确选定的 Word 蒸馏。草稿正文与 sidecar 分别校验并绑定知识版本；审核结论仅按用户授权记录。发布逐块写入并读回核验，完成后才切换活动版本；重复发布保持幂等。检索按命中章节、针对性补充章节、必要时完整正文逐层读取；图形 JSON 仅按精确资产 ID/版本/hash 按需读取，不随普通章节结果返回。错误和 RAG 输出不得泄露内部路径；本地 `draft_export` 显式导出路径仍是既有交付契约。

## 黑盒验收结果

| 验收项 | 本次观察 | 结果与边界 |
|---|---|---|
| 单 MCP 工具面 | WorkBuddy V5.8.3 显示本主 MCP 31/31 工具启用；1.3.0 加入 `metadata_catalog`。无独立 RAG/方案生成服务参与本次流程。 | WorkBuddy 本次通过；不据此判断别的会话已刷新配置 |
| 审核与发布 | `t-9d612cf9` 对应主稿 `k-2bdb26a1` rev1；使用审核 `review-1a4fefca` 和任务 `publish-eb837981`。状态 `published`。 | 通过 |
| 发布逐块读回 | 发布状态显示 65 chunks / 65 verified；重复 publish 返回同一 task。 | 本次通过；覆盖该版本和任务 |
| 阶段与类型 | `stage=方案与设计`；`knowledge_type=项目知识`；集合 `kd_dev_qwen_cb_solution_design`。 | 正确按六阶段路由；不把文档类型当主分类 |
| 检索：第一阶段 | 查询“第一阶段”命中 `requirements`（score 0.618）及 `scope`。 | 本次命中 |
| 检索：流程/表单 | 查询“出差请假加班”命中 `forms-office`、`forms-hr`；材料没有审批节点信息。 | 不推断审批流程；本次语义边界符合要求 |
| 检索：终端 | 查询“PC/mobile”命中 `mobile`、`scope`。 | 本次命中 |
| 普通章节读取 | `read_knowledge` sections 返回正文章节，不附带完整图形 JSON。 | 20:39 WorkBuddy 通过 |
| 受保护图形：HR 图 | 来源 `f-cf88f4ca:word/document.xml:rId19:10`；资产 `f-0d8e7d80` / `v-8eb22e40`；SHA-256 `fd7cbbc88b3acfb6f44b59c225d9d02f4e9c69bffea3acd1b5d1350a7c4698f0`；10 nodes / 8 edges / 2 unresolved items。 | 指定版本与哈希读取通过；保留未确认项，不声称源图无歧义完整 |
| 受保护图形：阶段图 | 来源 `rId11:2`；资产 `f-78345871` / `v-fd34a7d9`；SHA-256 `614e7a46b25b5d1924b64a41eba6911e7e889581c355318ba26286d676b321f4`；10 nodes / 9 edges / 2 unresolved items。 | 指定版本与哈希读取通过；保留未确认项，不声称源图无歧义完整 |
| 错误哈希 | `get_asset` 使用错误 content hash 请求被拒绝。 | 通过；只证明该错误输入被拒绝 |
| 图片返回契约 | 独立 task `t-37ef1e81` 读取分页 `0→60→96`。HR 图返回 image，含 filename、image_position=10、total=18、location、nearby_text；phase=`image_returned`，`reviewed_by_model=false`。 | 图片已返回；没有模型视觉核验结论，不计作已核验图片 |
| 临时文件清理 | 图片任务关闭后返回 `tmp_removed=true`。 | 本次通过 |
| 原件哈希 | 唯一 OA 原件 SHA-256 验证前后不变。 | 通过 |
| MongoDB / Milvus | 独立开发侧 `readback.json`：Mongo `ready/active`；Milvus 有 65 行，精确 IDs 与 metadata 对应；图形 JSON 不在向量数据中。四个容器均 healthy。 | 开发环境读回通过；不等同于生产服务或 WorkBuddy UI 观察 |
| 首启空列表 | 临时将 WorkBuddy MCP 的 `CKP_DATA` 指向不存在的本地测试目录；串行调用 `project_list`、`file_list`，分别明确返回“未找到项目（0 项）”和“未找到文件（0 个）”。恢复配置后再次只读调用 `project_list`，返回原库 3 个项目。 | WorkBuddy 黑盒确认空库查询可用且空结果可见；原测试库配置已恢复，MCP 显示 31/31 工具启用 |
| 路径脱敏 | WorkBuddy 对不存在文件调用 `draft_verify`，错误为 `[Errno 2] No such file or directory: '<文件>'`，没有暴露服务器绝对路径。 | 该错误分支的路径脱敏通过；不扩张为所有错误类型已覆盖 |
| Word-only 蒸馏边界 | 自动化边界测试拒绝非 Word 蒸馏。 | 自动化通过；WorkBuddy 未执行非 Word 反例 |
| MD 正文单项篡改（K06.1.2，2026-10-23） | `draft_export` 导出 `k-2bdb26a1@3` 到 `tmp/k06_1_2`，正文与 sidecar 原始校验均匹配，content hash 为 `49771248dc2c93df238b59082e4930a8ed08aa084fe837c928952b8b74a7abb5`。仅在导出副本追加测试标记后，WorkBuddy 单独调用 `draft_verify`：顶层 `match=false`，错误为“Markdown 正文哈希校验失败”；MD `content_hash_claimed` 不匹配，sidecar `sidecar_sha256` 匹配，`document_ref_match=true`。 | 通过；只改 MD 的情况下正文校验失败，sidecar 自身未受影响 |
| sidecar 单项篡改（K06.1.2，2026-10-23） | 仅在另一份导出副本的 JSON sidecar 增加测试字段后，WorkBuddy 单独调用 `draft_verify`：顶层 `match=false`，无工具异常；`sidecar_sha256_claimed` 与 `sidecar_sha256_actual` 不同，`markdown_match=true`、`document_ref_match=true`。 | 通过；只改 sidecar 的情况下 sidecar 哈希失败，正文和版本身份交叉校验通过 |
| K06.1.3 上传与发布隔离（2026-10-23） | WorkBuddy 原件重传前后知识状态均为 3 个已发布修订，唯一短语检索结果一致；后续 rev3 经过明确的审核与发布任务后成为活动修订，RAG 筛选命中 `k-2bdb26a1@3`。 | 通过；上传动作未新增知识或改变检索，发布通过显式审核/发布链路发生 |
| 导出与核验状态字段（K06.1.4，2026-10-23） | WorkBuddy 顺序读取 `knowledge_status` 与 `draft_show`：前者 revision 3 `status=published`；后者 `status=published`、`draft_status=draft`，review 与 publish task IDs 一致。对照实现确认 `draft_export.verify` / `draft_verify` 的 `status` 从 Markdown frontmatter 读取，表示导出草稿文件状态；`draft_show.status` 来自 lifecycle，`draft_status` 来自草稿记录。 | 通过；不同状态字段来源与含义已确认，无发布状态冲突；WorkBuddy 本项在尝试读取任务 memory 后被取消，所需两次 MCP 只读调用已先返回 |
| K06.1.1 原件重传与 RAG 隔离（2026-10-23） | WorkBuddy 串行调用 `knowledge_status`、`search_knowledge`、`upload_original`、`knowledge_status`、`search_knowledge`。上传到既有“测试项目”，返回 `action=reused`，复用相同 `file_id/version_id/SHA-256`。前后均为 3 个已发布修订；同一唯一短语均命中 5 个相同 chunk，分数相同；没有 revision 4 或新发布任务。 | 通过；确认该次重复上传没有改变知识修订、发布任务、向量索引或检索结果。上传审计记录是否实际新增未能从当前 MCP 只读工具核验；重传已发生，不能声称无副作用 |
| K04.3 无项目参数拒绝（2026-10-23） | WorkBuddy 通过 `upload_original` 的参数 schema 进行一次精确负向调用：正确提供 `path`，省略必填 `project`。客户端返回 `must have required property 'project'`，请求未传到 MCP 服务。 | 通过客户端接口边界验收；未触发服务端，未创建项目或写入。该结果只证明 WorkBuddy 当前连接所用工具 schema 的必填约束 |
| K04.2 项目查重（2026-10-23） | `project_list` 返回两项；“测试项目”精确同名候选为 `p-6cd27afa`（active，备注“本地开发黑盒验收专用项目（1.2.0）”），无名称相近候选。未调用 `project_create`。同一 WorkBuddy 任务界面还显示 MCP 以外的本地 memory 文件读取与编辑动作（`2026-10-23.md`，`+4/-0`）；其内容未读取。 | 查重通过、未产生重名项目；新项目创建分支未验收，需唯一名称或确认复用。WorkBuddy MCP-only 边界未通过，暂停后续 WorkBuddy 黑盒调用，先处理/隔离本地 memory 自动访问行为 |
| K04.2 新项目创建（2026-10-23） | 用户指定唯一名称“测试项目 2”后，WorkBuddy 串行调用 `project_create`，返回 `p-7892f5d5`；随后验收使用该项目完成非 Word 文件边界测试。 | 通过；创建分支已验收 |
| K04.10 空库只读列表（2026-10-23） | 临时空目录下串行调用 `project_list` 与 `file_list` 各一次，WorkBuddy 分别显示 0 个项目和 0 个文件，并呈现明确空结果提示。恢复原数据目录后，单独调用 `project_list` 返回 3 个项目。 | 通过 WorkBuddy 黑盒验收；测试后已恢复原 MCP 配置 |
| K04.11 错误路径脱敏（2026-10-23） | WorkBuddy 对不存在文件调用 `draft_verify`；服务错误包含 `[Errno 2] No such file or directory: '<文件>'`，没有返回服务器本机路径。 | 通过该错误分支的黑盒核验 |
| 撤回 | 仅有自动化 unit 覆盖；本次未通过 WorkBuddy 调用 `withdraw_knowledge`。 | 未完成黑盒验收 |
| 紧凑目录（20:56） | WorkBuddy 返回 `mode=directory`、28 sections、根级 `source_refs` 1 条；无正文、无重复 refs，直接内联且未截断/落盘。旧版曾返回 65,626 bytes，根因是重复 refs 而非默认全文；本地修复读回为 7,291 bytes。 | WorkBuddy 定向复测 PASS；该模式结果与此前本地结果分别记录 |
| sections / 发布状态复测（20:56） | `mobile` 返回 `mode=sections`，其 `content_sha` 与目录一致；原 `publish-eb837981` 仍为 `published`、65/65 verified、无错误。 | WorkBuddy 定向复测 PASS |
| 1.3.0 MCP 工具面与目录 | WorkBuddy MCP 管理器显示 `knowledge-distillation` 31/31 工具启用；`distill_stages` 返回 1.3.0 与六阶段；`metadata_catalog("致远 OA")` 只返回“办公协同”。 | WorkBuddy 黑盒正向通过；本次未在 WorkBuddy 做缺选、多产品或错配的负向提交 |
| 产品线/领域写入 | PM 已确认 `products="致远 OA"`、`domain=["办公协同"]`。rev3 frontmatter 与 sidecar 均一致，`classification_status=confirmed`；RAG metadata 带两字段。 | WorkBuddy 黑盒通过 |
| 新修订与评分修复 | rev2 正文 hash 与 rev1 相同、分类正确、导出 `match=true`，但漏传 `quality_report`，故 `assessment_status=partial`、55 分；rev2 仍单独审核发布并保留历史。rev3 从 rev1 原样传入 `quality_report`，正文 hash 仍相同，`assessment_status=complete`、coverage 100%、无未评维度，分数 91.0。 | rev3 正向通过；91 与 rev1 89 的 2 分差异来自服务端依据新任务资产清单将 media/table 维度改为“不适用”并各计 5 分。实际质检字段已在 WorkBuddy 返回中说明 |
| rev3 独立审核和入库 | `review-b8dedec2` → approved（备注限定为本地开发元数据集成测试，不代表 OA 业务事实验收）；`publish-73563ed1` → published，65 chunks / 65 verified；rev3 成为当前活动修订。 | WorkBuddy 黑盒通过；rev1/rev2 记录均保留，未原地改写或撤回 |
| RAG 正反筛选 | `stage=方案与设计` + `products=致远 OA` + `domain=办公协同` 查询命中 `k-2bdb26a1@3`，结果含 requirements/mobile/scope；替换为 `products=YonBIP 旗舰版` 后结果为空。 | WorkBuddy 黑盒通过；只证明本测试集合与筛选条件 |
| K07 按需读取（2026-10-23） | 只通过 WorkBuddy 调用 `read_knowledge`：rev3 目录 `mode=directory`、28 sections、根级唯一来源；`mobile` 精确读取 `mode=sections` 且仅返该节正文；全文 `mode=full_document` 且 body 非空。全文读取响应不提供 UTF-8 字节数字段，未测长度；此前 `draft_export` 的同 hash 记录为 23,445 bytes，不冒充本次测量。 | K07 本地读取黑盒通过；同修订/hash 根级绑定；普通章节不含完整图形 JSON |
| K07 读取拒绝边界 | 错 hash 返回“必须提供匹配的 revision 和 content_hash”；已被替代的 rev2 返回“知识版本未发布、已替代或已撤回”；不存在分节返回“分节不存在或不属于可读取的 body 章节”。三条错误均无绝对路径；不存在分节没有回退为全文。 | K07 黑盒通过；该接口不提供历史已发布修订读取 |
| 本轮任务关闭/临时副本 | WorkBuddy 黑盒调用 `distill_close(t-0e64e9d6)` 返回 `status=closed`、`tmp_removed=false`；此前 `t-9182d3be` 也返回 false，图片契约任务 `t-37ef1e81` 返回 true。代码实现中 false 对应任务临时目录不存在；已补充返回 `tmp_cleanup_status=not_present` 以表达该状态。 | WorkBuddy 实测确认任务关闭、关闭时无任务临时目录；此次 WorkBuddy 响应没有新字段，因为改动发生在复测之后。代码语义由本地测试验证 |

## MongoDB / Milvus 开发侧读回

该结果来自独立开发侧读取 `readback.json`，不是 WorkBuddy MCP 的可见结果：Mongo 状态 `ready/active`；Milvus 65 rows，向量记录 IDs 和 metadata 与发布内容对应；diagram JSON 未进入向量集合；四个容器健康。不能据此声称公司数据库服务已接通。

## WorkBuddy 图片/进度展示证据边界

只读检查 WorkBuddy 5.8.3 已安装客户端及 MCP SDK：`/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/renderer/assets/contexts-BNN7VV-R.js` 中 MCP request 仅在调用方传入 `onprogress` 时附加 `_meta.progressToken`，并将进度通知派发给注册的 callback；`mcp-server-loader-DFUSVF9N.js` 保留 tool 对象字段。随包 SDK 的 `@modelcontextprotocol/sdk/dist/cjs/types.js` 定义 tool title/annotations、progress 与包含 Text/Image 的结果块结构。MCP 服务已对正文读取和发布提供阶段进度；客户端未请求 progress token 时协议不会发送通知。

1.3.0 OA 修订验收时，WorkBuddy 界面实际逐项展示了 `draft_submit`、`draft_export`、审核、发布、检索等 MCP 调用，并出现“深度思考”等客户端状态。服务提示词已要求仅在阶段变化时说明进展；WorkBuddy 最终报告按阶段汇总，但客户端工具调用卡片仍可见。MCP 服务无法隐藏客户端必须呈现的工具调用，也不能保证客户端显示标准 progress 通知。因此优化边界是减少模型重复自然语言、用文件名/阶段/offset/任务状态提供有效进度；UI 对工具步骤的显示属于 WorkBuddy 客户端行为。若要减少可见卡片，需由 WorkBuddy 客户端提供展示设置，或改变其工具调用/编排逻辑；单改 MCP 描述无法关闭。MCP image 返回更不等于模型已视觉核验；图片工具显式给出 `reviewed_by_model=false`。官方协议参考：[Progress](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress)、[Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[Schema](https://modelcontextprotocol.io/specification/2025-11-25/schema)。

## 嵌入策略记录

Qwen 当前没有交付精确 tokenizer，因此按 UTF-8 字节边界切分：默认上限 **512 bytes**，标题计入边界，可配置范围 **512–4096 bytes**。不得称其为精确 512 tokens。BGE 使用精确 tokenizer，边界为 **512 tokens**。

## K08 WorkBuddy 场景验收

本地服务已明确读取策略；下表定义 WorkBuddy 客户端调用验收标准。调用结果与当前完成状态以项目唯一迭代看板为准。

## K15 产品资料逻辑库本地 MCP 验收

2026-10-23 通过本地 MCP stdio 客户端调用 1.4.0 服务，未调用蒸馏工具。服务注册 37 个工具，其中新增 6 个产品资料工具。产品资料清单初始返回 0 项；`metadata_catalog("NCC")` 提供领域清单，按项目经理确认的 `产品手册 / NCC / 财务会计` 处理指定 Word。

第一次索引发现解析结果只有一个“正文”节，因此增加 Word 样式继承标题级别与常见编号标题识别，解析器升为 `ckp-office-inspector-v0.4`。重新索引后返回 83 个章节、499 个切片，MongoDB/Milvus 逐块读回核验 499/499；原件 SHA-256 在两个解析修订中一致，活动修订为 2。原件含 79 个嵌入图片，当前产品资料路径不做 OCR 或图形语义提取，响应明确记录该边界。

按“NCC / 财务会计 / 产品手册”过滤的检索命中“应用场景 / 4.2 总账日常核算（凭证管理）”；用命中中的 `material_id + revision + source_sha256 + section_id` 定向读取，返回同版目标章节。重复入库返回 `unchanged`，切片数未增加；通过 MCP 导出原件后哈希核验通过，测试导出副本已清理。WorkBuddy 当前工具清单尚未刷新至 1.4.0，因此以上是本地 MCP 协议和真实材料验证，不计作 WorkBuddy 黑盒验收；该项由唯一开发看板 K15.3 跟踪。

后续按 WorkBuddy 本机数据目录复核时，发现共享 MongoDB/Milvus 中存在其他本地数据目录的活动记录；旧检索曾返回无法由当前 SQLite 修订读取的候选。修复后，检索候选必须与当前数据目录的活动资料、版本 ID、revision、类别和源 SHA-256 一致；幂等入库也会核对目标索引记录，缺失时可建立新修订重新索引。用本机 MCP 1.4.0（37 个工具）串行复验指定 Word：列表显示 `NCC / 产品手册 / 财务会计`、修订 1、活动；重复入库返回 `unchanged`；检索返回 5 条候选，最高命中为“应用场景 / 4.2 总账日常核算（凭证管理）/ 4.2.1 业务描述”；用命中精确三元组读取同版分节，返回 609 字，revision 与源哈希一致。

WorkBuddy 刷新至 MCP 1.4.0 后，串行完成该 Word 的黑盒验收：清单确认 `NCC / 产品手册 / 财务会计`；重复入库返回 `unchanged`，材料 ID、修订号、哈希和 499 个已索引切片保持一致；按产品与领域检索得到 5 条结果，最高命中为“应用场景 / 4.2 总账日常核算（凭证管理）/ 4.2.1 业务描述”；使用命中三元组读取同版分节成功；原件导出文件名、大小和 SHA-256 一致，`verified=true`。导出副本已删除。图片 OCR 不在本次验收范围，原件中的 79 张嵌入图片仍按既定边界不做 OCR 或图像语义索引。K15 验收完成。

补验发现 WorkBuddy 对 `unchanged` 分支只能返回 `indexed_chunks`，未给出分节数、核验依据和图片状态。服务已补齐该分支的 `sections/chunks/verified_chunks/embedded_images_not_ocr_processed`，并返回 `verification_basis=original_ingest`、`readback_performed_this_call=false`，明确这些核验计数来自首次成功入库且本次没有重读 Milvus。基于同一指定 Word 串行复验一次：返回 `unchanged`、83 节、499 切片、499 个首次入库核验块、79 张未 OCR 图片，material/version/revision/hash 与既有清单一致；未创建新修订。K15.3 完成。

| 场景 | 问题形态 | 预期调用 | 通过条件 |
|---|---|---|---|
| 分节足够 | 询问一个可由单个命中章节直接回答的具体事实 | `search_knowledge` | 直接基于命中分节回答；不调用 `read_knowledge`、`get_asset` |
| 定向补读 | 命中章节缺少必要前提、限制或关联步骤 | `search_knowledge` → `read_knowledge(section_ids=[相关章节])` | `knowledge_id/revision/content_hash` 与命中一致；只读相关章节后回答 |
| 全文核对 | 问题明确涉及整体方案、跨章节关系或冲突，且定向章节不足 | `search_knowledge` → 必要时定向补读 → `read_knowledge(full_document=true)` | 全文读取使用同一修订；答案只采用相关证据，不把全文无差别重复注入 |
| 图形复原 | 问题需要流程/系统图中的节点关系或用户要求完整还原 | `search_knowledge` → `get_asset(section_id=命中章节, asset_id=该节引用)` | 使用同一知识修订和 hash；服务端核验该节确实引用资产；返回完整图形结构，保留节点、边、条件及待确认项；普通问答不取资产 |

### K08 WorkBuddy 黑盒结果（2026-10-23）

| 场景 | 实际观察 | 结果 |
|---|---|---|
| 命中分节足够 | WorkBuddy 仅调用 `search_knowledge`，基于 `mobile` 命中分节回答，没有调用 `read_knowledge` 或 `get_asset` | 通过 |
| 定向补读 | WorkBuddy 先检索，再以同一 `knowledge_id/revision/content_hash` 读取所需章节；未请求全文 | 通过 |
| 跨章节全文核对 | WorkBuddy 对整体/跨章问题读取目录、相关分节及同一修订全文，回答仅引用相关内容 | 通过 |
| 图形资产正向读取 | WorkBuddy 对同一修订 `hr-flow` 节引用的 HR 流程图调用 `get_asset`，返回完整 10 节点、8 边结构、单泳道和 2 个 unresolved items；没有条件/分支结构 | 通过；该图为 HR 员工管理流程，不是 OA 审批流程图本体 |
| 图形资产归属负向校验 | 同一知识修订及 asset_id，故意将 section_id 设为 `mobile`；MCP 原样拒绝并返回“该资产未被指定正文分节引用”，未返回 asset 或 payload | 通过；没有重试、换参或绕过 |

## 记录范围

本文件保留实际黑盒验收结果，不维护开发待办或进度。2026-10-23 WorkBuddy 已实测 1.3.0 产品目录、分类写入、新 revision 审核发布、65/65 分块读回、RAG 正反筛选、K07/K08 按需读取策略、原件重复上传不改变 RAG 结果，以及 K06.1.2 对正文和 sidecar 的独立篡改检测；K06.1.4 状态字段来源已通过黑盒输出及实现核对厘清。K04.2 后续在用户指定唯一名称后完成创建分支验收；K04.1–K04.9 均已按看板更新为完成。K01–K03 的黑盒证据覆盖 OA 主流程正向路径，不扩张为全部负向边界验收。WorkBuddy 黑盒调用已恢复并按单次串行执行。撤回及产品/领域负向输入仍未验收；撤回不属于 K09 前范围，产品/领域负向已由 K04.4–K04.6 单独验收。`distill_close` 的 `tmp_removed=false` 表示关闭时任务临时目录不存在。公司 file server、remote auth 和生产发布未接入；后续工作的状态以[产品迭代看板](产品迭代看板.md)为唯一准据。
