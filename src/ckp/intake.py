"""文件清单、内容哈希与 Office 元数据提取。

改造自旧工程 company-brain/app/common/intake.py（SHA-256 275722b0560b316bb9ac0401a7e619f740ddf03552d2df4ffd45c32cacdc8eee）：
- 保留 inventory 的目录遍历与安全处理、流式 sha256、Office docProps 元数据提取。
- 移除旧解析流水线的扩展名排除表与分类规则（classify_document/_rules/_FORM_PATTERNS）：
  V2 上传只保存原件，不因“不可解析”丢弃文件。
- 移除旧身份模型（document_id=version_id=sha256）；身份由 V2 存储层分配。
- 排除规则收窄为符号链接、Office 临时文件与系统隐藏文件。
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_file(path: Path) -> str:
    """流式计算文件 SHA-256，返回小写十六进制。"""
    sha256 = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def _relative(path: Path, root: Path, is_single_file: bool) -> str:
    if is_single_file:
        return path.name
    return path.relative_to(root).as_posix()


def inventory(source: Path) -> dict[str, Any]:
    """遍历 *source*（文件或目录），每个文件要么收录要么带原因排除。

    V2 保存语义：不按文件类型过滤；仅排除符号链接（不追随）、
    Office 临时文件（~$ 开头）与 .DS_Store 类系统文件。隐藏目录正常遍历。
    """
    root = Path(source).expanduser()
    if not root.exists() and not root.is_symlink():
        raise FileNotFoundError(str(root))
    is_single_file = root.is_symlink() or not root.is_dir()
    excluded: list[dict[str, Any]] = []
    if is_single_file:
        candidates = [root]
    else:
        candidates: list[Path] = []

        def visit(directory: Path) -> None:
            try:
                entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError:
                excluded.append({"relative": _relative(directory, root, False),
                                 "size": 0, "reason": "unreadable_directory"})
                return
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        candidates.append(path)
                    elif entry.is_dir(follow_symlinks=False):
                        visit(path)
                    elif entry.is_file(follow_symlinks=False):
                        candidates.append(path)
                except OSError:
                    candidates.append(path)

        visit(root)

    files: list[dict[str, Any]] = []
    for path in candidates:
        rel = _relative(path, root, is_single_file)
        try:
            size = path.lstat().st_size
        except OSError:
            size = 0
        reason = None
        if path.is_symlink():
            reason = "symlink"
        elif path.name.startswith("~$"):
            reason = "office_temporary"
        elif path.name == ".DS_Store":
            reason = "system_file"
        if reason:
            excluded.append({"relative": rel, "size": size, "reason": reason})
            continue
        try:
            files.append({"path": str(path), "relative": rel, "size": size,
                          "sha256": hash_file(path)})
        except OSError:
            excluded.append({"relative": rel, "size": size, "reason": "unreadable"})

    return {
        "files": files,
        "excluded": excluded,
        "total_files": len(files) + len(excluded),
        "total_bytes": sum(item["size"] for item in files) + sum(item["size"] for item in excluded),
        "accepted_files": len(files),
        "accepted_bytes": sum(item["size"] for item in files),
    }


_CORE_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_DCTERMS_NS = "http://purl.org/dc/terms/"


def office_properties(path: Path) -> tuple[dict[str, str], list[str]]:
    """提取 Office 文件 docProps/core.xml 的作者与时间元数据；缺失返回空。

    这是上传时的元数据登记，不是内容解析；提取失败不影响保存。
    """
    props: dict[str, str] = {}
    sources: list[str] = []
    path = Path(path)
    if path.suffix.lower() not in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        return props, sources
    try:
        with zipfile.ZipFile(path) as archive:
            member = "docProps/core.xml"
            if member not in archive.namelist():
                return props, sources
            root = ElementTree.fromstring(archive.read(member))
        fields = {
            "author": f"{{{_DC_NS}}}creator",
            "last_modified_by": f"{{{_CORE_NS}}}lastModifiedBy",
            "created_at": f"{{{_DCTERMS_NS}}}created",
            "modified_at": f"{{{_DCTERMS_NS}}}modified",
        }
        for key, tag in fields.items():
            element = root.find(tag)
            value = element.text.strip() if element is not None and element.text else ""
            if value:
                props[key] = value
                sources.append(f"{key}:docProps/core.xml")
    except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError):
        pass
    return props, sources
