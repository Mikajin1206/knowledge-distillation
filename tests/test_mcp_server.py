"""MCP 服务端端到端：stdio JSON-RPC 握手、工具清单、完整业务流与错误路径。

以独立子进程运行服务端（与 WorkBuddy 连接方式一致），数据目录用临时 CKP_DATA。
"""
from __future__ import annotations

import json
import base64
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests._path import *  # noqa: F401,F403

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "ckp-mcp.sh"

BODY = """# 知识

## 业务结论 <!-- kd-section id=business_conclusion rag=body -->

保留可复用的业务事实。

## 冲突、缺口与待确认项 <!-- kd-section id=uncertainties rag=body -->

缺口：合同未上传，效力待确认。
"""


class McpServerEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ckp-mcp-"))
        self.env = dict(os.environ,
                        CKP_DATA=str(self.tmp / "data"),
                        CKP_OUTPUT=str(self.tmp / "out"),
                        PATH=os.environ.get("PATH", "/usr/bin:/bin"))
        self.proc = subprocess.Popen(
            [str(LAUNCHER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=self.env, cwd=str(REPO))
        self.next_id = 0
        self.notifications = []

    def tearDown(self):
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.proc.kill()
            self.proc.wait(timeout=5)
        finally:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream:
                    stream.close()

    def _send(self, payload) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params=None) -> dict:
        self.next_id += 1
        request = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            request["params"] = params
        self._send(request)
        while True:
            line = self.proc.stdout.readline()
            self.assertTrue(line, msg=f"服务端无响应: {method}")
            response = json.loads(line)
            if 'id' in response:
                self.assertEqual(self.next_id, response['id'])
                return response
            self.notifications.append(response)

    def _call(self, tool: str, arguments: dict) -> dict:
        response = self._request("tools/call", {"name": tool, "arguments": arguments})
        self.assertNotIn("error", response, msg=f"{tool} 协议错误: {response.get('error')}")
        result = response["result"]
        self.assertFalse(result.get("isError"), msg=f"{tool} 执行失败: {result}")
        return json.loads(result["content"][0]["text"])

    def _call_expect_error(self, tool: str, arguments: dict) -> str:
        response = self._request("tools/call", {"name": tool, "arguments": arguments})
        result = response["result"]
        self.assertTrue(result.get("isError"), msg=f"{tool} 应失败: {result}")
        return result["content"][0]["text"]

    @staticmethod
    def _unwrap(payload) -> dict:
        return payload if isinstance(payload, dict) else json.loads(payload)

    def test_full_flow(self):
        # 1 握手
        init = self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ckp-e2e", "version": "0"}})
        server_info = init["result"]["serverInfo"]
        self.assertEqual("Knowledge Distillation", server_info["name"])
        self.assertEqual("1.5.6", server_info["version"])
        instructions = init["result"].get("instructions", "")
        self.assertIn("distill_word_content", instructions)
        self.assertIn("distill_word_image", instructions)
        self.assertIn("质量分低", instructions)
        self.assertIn("图例", instructions)
        self.assertIn("多模态模型直接识别", instructions)
        self.assertIn("不用 OCR", instructions)
        self.assertIn("distill_quality_report_template", instructions)
        self.assertIn("一次提交来源和完整 quality_report", instructions)
        self.assertIn("本机目录枚举/文件搜索", instructions)
        self.assertIn("draft_verify", instructions)
        self.assertIn("project_list", instructions)
        self.assertIn("同一项目沿用清单规范名称并再次确认", instructions)
        self.assertIn("若确认是不同项目，保留项目经理提供的新名称", instructions)
        self.assertIn("项目名称未提供或项目归属未确认前不得上传", instructions)
        self.assertIn("项目名称未提供或项目归属未确认前不得上传", instructions)
        self.assertIn("足够时直接回答，不调用 read_knowledge", instructions)
        self.assertIn("命中少或分数低本身不是读全文理由", instructions)
        self.assertIn("完整还原须保留节点、边、条件、方向、分组及 unresolved_items", instructions)
        self.assertIn("产品资料属于独立逻辑资料库", instructions)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # 2 工具清单
        tools = self._request("tools/list")["result"]["tools"]
        tool_specs = {item["name"]: item for item in tools}
        names = {tool["name"] for tool in tools}
        schemas = {tool["name"]: tool.get("inputSchema", {}) for tool in tools}
        expected = {"project_create", "project_list", "metadata_catalog", "upload_original", "file_list", "file_read_version",
                    "distill_stages", "distill_start", "distill_attach", "distill_gap",
                    "distill_input", "distill_guidance", "distill_material", "distill_show",
                    "distill_quality_report_template",
                    "distill_word_content", "distill_word_image",
                    "distill_close", "draft_submit", "draft_show", "draft_export",
                    "draft_verify", "knowledge_status", "submit_for_review", "record_review",
                    "publish_knowledge", "get_publish_task", "search_knowledge",
                    "read_knowledge", "get_asset", "withdraw_knowledge"}
        expected.update({"product_material_ingest", "product_material_list",
                         "search_product_materials", "read_product_material",
                         "read_product_material_image", "product_material_export_original",
                         "withdraw_product_material"})
        self.assertEqual(expected, names)
        self.assertNotIn('ctx', schemas['distill_word_image'].get('properties', {}))
        self.assertNotIn('ctx', schemas['get_publish_task'].get('properties', {}))
        self.assertEqual(20, schemas['get_publish_task']['properties']['wait_seconds']['default'])
        self.assertIn("quality_report", schemas["draft_submit"].get("properties", {}))
        self.assertIn("name", schemas["project_create"].get("required", []))
        self.assertIn("path", schemas["upload_original"].get("required", []))
        self.assertIn("project", schemas["upload_original"].get("required", []))
        self.assertEqual(60, schemas["distill_word_content"]["properties"]["limit"]["default"])
        self.assertIn("同一修订合并 section_ids", tool_specs["read_knowledge"]["description"])
        self.assertIn("protected_asset_refs", tool_specs["get_asset"]["description"])
        self.assertIn("section_ids 互斥", tool_specs["read_knowledge"]["description"])
        self.assertIn("section_id", schemas["get_asset"].get("required", []))
        self.assertIn("section_id 与 asset_id 必须来自同一相关命中章节", tool_specs["get_asset"]["description"])
        preflight = self._call("distill_stages", {})
        self.assertEqual(6, len(preflight["stages"]))
        info = preflight["server_info"]
        self.assertEqual("1.5.6", info["server_version"])
        self.assertEqual("ckp-office-inspector-v0.4", info["parser_version"])
        self.assertEqual("ckp-emf-lo-primary-frame-guard-bitmap-fallback-v8", info["image_renderer_version"])
        self.assertEqual(["svg", "emf", "wmf", "bmp", "tif", "tiff"],
                         info["image_formats_converted_to_png"])
        self.assertFalse(info["ocr"])
        # Product materials are routed through this same MCP, with explicit
        # category/product/domain inputs; an empty read-only list works on a
        # fresh data directory without initializing persistent storage.
        self.assertEqual(["category", "domain", "path", "product"],
                         sorted(schemas["product_material_ingest"]["required"]))
        training_rejection = self._call_expect_error("product_material_ingest", {
            "path": "/path/not/read.docx", "category": "培训资料",
            "product": "NCC", "domain": ["财务会计"],
        })
        self.assertIn("当前版本仅接受规范产品手册", training_rejection)
        self.assertIn("full_document", schemas["read_product_material"]["properties"])
        self.assertEqual(["figure_id", "material_id", "revision", "source_sha256"],
                         sorted(schemas["read_product_material_image"]["required"]))
        product_materials = self._call("product_material_list", {})
        self.assertEqual(0, product_materials["count"])
        self.assertEqual([], product_materials["materials"])
        self.assertEqual(["产品手册", "培训资料"], product_materials["categories"])
        # 3 业务流
        missing_name = self._call_expect_error("project_create", {"name": ""})
        self.assertIn("项目名称不能为空", missing_name)
        empty_projects = self._request(
            "tools/call", {"name": "project_list", "arguments": {}})["result"]
        self.assertEqual([], empty_projects.get("structuredContent", {}).get("result", []))
        self.assertIn("未找到项目（0 项）", empty_projects["content"][0]["text"])
        project = self._unwrap(self._call(
            "project_create", {"name": "MCP演示", "owner": "张三"}))["project_id"]
        catalog = self._call("metadata_catalog", {"product": "致远 OA"})
        self.assertEqual(["办公协同"], catalog["domains"])
        self.assertEqual(["产品手册"], info["classification"]["product_material_categories"])
        project_response = self._request(
            "tools/call", {"name": "project_list", "arguments": {}})["result"]
        projects = project_response.get("structuredContent", {}).get("result", [])
        self.assertTrue(any(item["project_id"] == project and item["name"] == "MCP演示"
                            for item in projects))
        src = write_minimal_docx(self.tmp / "材料.docx", "# 需求纪要\n关键规则：示例。")
        missing_project = self._call_expect_error(
            "upload_original", {"path": str(src)})
        self.assertIn("project", missing_project)
        empty_files = self._request(
            "tools/call", {"name": "file_list", "arguments": {}})["result"]
        self.assertEqual([], empty_files.get("structuredContent", {}).get("result", []))
        self.assertIn("未找到文件（0 个）", empty_files["content"][0]["text"])
        upload = self._unwrap(self._call(
            "upload_original", {"path": str(src), "project": project,
                                "uploader": "李四", "stage": "调研与需求",
                                "tags": "需求"}))
        self.assertIn("蒸馏仅支持 Word 文件 .doc/.docx", upload["note"])
        file_id = upload["uploaded"][0]["file_id"]
        version_id = upload["uploaded"][0]["version_id"]
        # 幂等：同 request_id 两次开任务只产生一个
        task = self._unwrap(self._call(
            "distill_start", {"project": project, "stage": "调研与需求",
                              "request_id": "mcp-t1"}))
        task_replay = self._unwrap(self._call(
            "distill_start", {"project": project, "stage": "调研与需求",
                              "request_id": "mcp-t1"}))
        self.assertEqual(task["task_id"], task_replay["task_id"])
        task_id = task["task_id"]
        self._call("distill_attach", {"task_id": task_id, "file_id": file_id})
        guidance = self._call("distill_guidance", {"task_id": task_id})
        self.assertEqual("distill-rules-v0.10", guidance["rules_version"])
        self.assertEqual(["立项与范围", "调研与需求", "方案与设计", "实施与变更", "测试与验收", "运维与改进"],
                         guidance["stage_options"])
        self.assertEqual(["方案设计", "调研报告", "概要设计", "开发功能说明书"],
                         guidance["document_type_options"])
        self.assertEqual("distill-score-v2", guidance["score_policy"]["version"])
        material = self._call("distill_material", {"task_id": task_id, "file_id": file_id})
        self.assertTrue(material["verified"])
        self.assertNotIn("path", material)
        content = self._call("distill_word_content", {
            "task_id": task_id, "file_id": file_id, "offset": 0, "limit": 1})
        self.assertEqual("docx", content["format"])
        self.assertEqual(1, content["total_blocks"])
        self.assertIn("关键规则：示例", content["blocks"][0]["text"])
        self.assertFalse(content["has_more"])
        self.assertNotIn("manifest_path", content)
        self.assertNotIn("local_path", json.dumps(content, ensure_ascii=False))
        self._call("distill_gap", {"task_id": task_id, "description": "合同未上传"})
        draft = self._unwrap(self._call(
            "draft_submit",
            {"task_id": task_id, "title": "调研结论", "knowledge_type": "方案设计",
             "body_md": BODY, "model": "mcp-e2e-stub",
             "sources": [{"version_id": version_id, "location": "全文",
                          "supports": "示例结论"}],
             "quality_report": {"asset_reviews": [], "flowcharts": [], "table_checks": []},
             "products": "致远 OA", "domain": ["办公协同"]}))
        knowledge_id = draft["knowledge_id"]
        self.assertEqual("draft", draft["status"])
        self.assertNotIn('body_md', self._call('draft_show', {'knowledge_id': knowledge_id}))
        self.assertEqual(BODY, self._call('draft_show', {'knowledge_id': knowledge_id, 'include_body': True})['body_md'])
        exported = self._call("draft_export", {"knowledge_id": knowledge_id})
        self.assertTrue(exported["verify"]["match"], msg=str(exported["verify"]))
        self.assertTrue(exported["verify_sidecar"]["match"], msg=str(exported["verify_sidecar"]))
        self.assertEqual("致远 OA", exported["frontmatter"]["products"])
        self.assertEqual(["办公协同"], exported["frontmatter"]["domain"])
        verified = self._call("draft_verify", {"md_path": exported["md"]})
        self.assertTrue(verified["match"])
        pair_verified = self._call("draft_verify", {
            "md_path": exported["md"], "sidecar_path": exported["json"]})
        self.assertTrue(pair_verified["match"])
        sidecar_path = Path(exported["json"])
        original_sidecar = sidecar_path.read_bytes()
        tampered_sidecar = json.loads(original_sidecar)
        tampered_sidecar["title"] = "tampered"
        sidecar_path.write_text(json.dumps(tampered_sidecar), encoding="utf-8")
        tampered_check = self._call("draft_verify", {
            "md_path": exported["md"], "sidecar_path": exported["json"]})
        self.assertFalse(tampered_check["match"])
        sidecar_path.write_bytes(original_sidecar)
        self._call("distill_close", {"task_id": task_id})
        self.assertFalse((REPO / "tmp" / "distill" / task_id).exists())
        # 4 错误路径：业务错误以 isError 返回，不是协议错误
        message = self._call_expect_error(
            "distill_start", {"project": project, "stage": "错误阶段"})
        self.assertIn("阶段", message)

    def test_standalone_verify_does_not_create_db(self):
        md = self.tmp / "x.md"
        md.write_text("---\ncontent_hash: \"0\"\n---\n正文\n", encoding="utf-8")
        init = self._request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "ckp-e2e", "version": "0"}})
        self.assertIn("protocolVersion", init["result"])
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = self._request("tools/call",
                                 {"name": "draft_verify", "arguments": {"md_path": str(md)}})
        result = response["result"]
        self.assertFalse(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertFalse(payload["match"])
        self.assertFalse((self.tmp / "data").exists(),
                         msg="只读校验工具不应创建数据库目录")

    def test_office_inspection_runs_only_after_attach(self):
        self._request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "ckp-office-test", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        project = self._call("project_create", {"name": "Office MCP 测试"})["project_id"]
        source = self.tmp / "minimal.docx"
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/"
            "pyoAAAAASUVORK5CYII=")
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>材料正文一</w:t></w:r></w:p><w:p><w:r><w:t>材料正文二</w:t></w:r></w:p><w:p><w:r><a:blip r:embed="rId1"/></w:r></w:p></w:body></w:document>')
            archive.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/></Relationships>')
            archive.writestr("word/media/image1.png", tiny_png)
        uploaded = self._call("upload_original", {"path": str(source), "project": project})
        file_id = uploaded["uploaded"][0]["file_id"]
        task_id = self._call("distill_start", {"project": project, "stage": "方案与设计"})["task_id"]
        self._call("distill_attach", {"task_id": task_id, "file_id": file_id})
        scan = self._call("distill_word_content", {
            "task_id": task_id, "file_id": file_id, "offset": 0, "limit": 100})
        self.assertEqual("docx", scan["format"])
        self.assertEqual(2, scan["summary"]["paragraphs"])
        self.assertEqual("ckp-office-inspector-v0.4", scan["parser_version"])
        self.assertEqual(1, len(scan["assets"]))
        self.assertNotIn("manifest_path", scan)
        self.assertNotIn("assets_dir", scan)
        self.assertNotIn("local_path", json.dumps(scan, ensure_ascii=False))
        template = self._call("distill_quality_report_template", {"task_id": task_id})
        self.assertEqual(1, len(template["quality_report"]["asset_reviews"]))
        self.assertEqual([], template["quality_report"]["irrelevant_asset_ids"])
        self.assertEqual(file_id, template["quality_report"]["asset_reviews"][0]["asset_id"].split(":")[0])
        self.assertNotIn("local_path", json.dumps(template, ensure_ascii=False))
        excluded_template = self._call("distill_quality_report_template", {
            "task_id": task_id,
            "excluded_asset_ids": [template["quality_report"]["asset_reviews"][0]["asset_id"]]})
        self.assertEqual([], excluded_template["quality_report"]["asset_reviews"])
        self.assertEqual([template["quality_report"]["asset_reviews"][0]["asset_id"]],
                         excluded_template["quality_report"]["irrelevant_asset_ids"])
        content = self._call("distill_word_content", {
            "task_id": task_id, "file_id": file_id, "offset": 0, "limit": 1})
        self.assertEqual("材料正文一", content["blocks"][0]["text"])
        self.assertTrue(content["has_more"])
        self.assertEqual(1, content["next_offset"])
        self.assertNotIn("local_path", json.dumps(content, ensure_ascii=False))
        next_page = self._call("distill_word_content", {
            "task_id": task_id, "file_id": file_id, "offset": content["next_offset"], "limit": 1})
        self.assertEqual("材料正文二", next_page["blocks"][0]["text"])
        self.assertFalse(next_page["has_more"])
        self.assertEqual([], next_page["assets"])
        image_result = self._request("tools/call", {"name": "distill_word_image", "arguments": {
            "task_id": task_id, "file_id": file_id,
            "asset_id": scan["assets"][0]["asset_id"]},
            "_meta": {"progressToken": "image-progress"}})["result"]
        self.assertEqual("image", image_result["content"][0]["type"])
        image_context = json.loads(image_result['content'][1]['text'])
        self.assertEqual('minimal.docx', image_context['filename'])
        self.assertEqual(1, image_context['image_position'])
        self.assertFalse(image_context['reviewed_by_model'])
        self.assertNotIn('local_path', image_result['content'][1]['text'])
        progress = [n['params'] for n in self.notifications if n['method'] == 'notifications/progress']
        self.assertEqual([0, 1], [p['progress'] for p in progress])
        self.assertTrue(all(p['progressToken'] == 'image-progress' for p in progress))
        self.assertIn('minimal.docx', progress[-1]['message'])
        self.assertEqual("image/png", image_result["content"][0]["mimeType"])
        self._call("distill_close", {"task_id": task_id})

    def test_non_word_upload_is_preserved_but_distillation_is_refused(self):
        self._request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "ckp-nonword-test", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        project = self._call("project_create", {"name": "非Word拒绝测试"})["project_id"]
        task_id = self._call("distill_start", {"project": project, "stage": "调研与需求"})["task_id"]
        word = write_minimal_docx(self.tmp / "方案.docx", "Word 正文")
        word_upload = self._call("upload_original", {"path": str(word), "project": project})
        word_id = word_upload["uploaded"][0]["file_id"]
        self._call("distill_attach", {"task_id": task_id, "file_id": word_id})
        for name, content in (("问题清单.xlsx", b"spreadsheet"),
                              ("验收汇报.pptx", b"presentation"),
                              ("扫描材料.pdf", b"pdf")):
            source = self.tmp / name
            source.write_bytes(content)
            upload = self._call("upload_original", {"path": str(source), "project": project})
            self.assertEqual(1, upload["accepted_files"])
            message = self._call_expect_error(
                "distill_attach", {"task_id": task_id, "file_id": upload["uploaded"][0]["file_id"]})
            self.assertIn("仅支持 Word 文件", message)
            self.assertIn("不解析、不提供给本地模型蒸馏", message)
            self.assertIn("询问用户是否只用 Word 继续", message)
        task = self._call("distill_show", {"task_id": task_id})
        self.assertEqual([word_id], [material["file_id"] for material in task["materials"]])
        self.assertFalse((REPO / "tmp" / "distill" / task_id).exists())


if __name__ == "__main__":
    unittest.main()
