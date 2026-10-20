"""Configurable, non-blocking quality scoring for distilled knowledge."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


SCORE_POLICY_NAME = "评分权重.json"
SCORE_SNAPSHOT_DIR = "score_snapshots"


def load_score_policy(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"蒸馏评分权重配置不存在: {path}")
    raw = path.read_bytes()
    try:
        policy = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"蒸馏评分权重配置不是有效 UTF-8 JSON: {path}") from exc
    dimensions = policy.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ValueError("评分权重配置 dimensions 必须是非空数组")
    keys, total = set(), 0
    for item in dimensions:
        if not isinstance(item, dict):
            raise ValueError("评分维度必须为对象")
        key = str(item.get("key") or "").strip()
        weight = item.get("weight")
        if not key or key in keys:
            raise ValueError(f"评分维度 key 缺失或重复: {key or '(空)'}")
        if type(weight) is not int or weight < 0 or weight > 100:
            raise ValueError(f"评分维度 {key} 的 weight 必须是 0 到 100 的整数")
        keys.add(key)
        total += weight
    if total != 100:
        raise ValueError(f"评分权重合计必须为 100，当前为 {total}")
    version = str(policy.get("version") or "").strip()
    if not version:
        raise ValueError("评分权重配置缺少 version")
    source_hash = policy.pop("_snapshot_hash", None)
    policy["hash"] = source_hash or hashlib.sha256(raw).hexdigest()
    return policy


def write_score_snapshot(data_dir: Path, task_id: str, policy: dict) -> Path:
    folder = Path(data_dir) / SCORE_SNAPSHOT_DIR / task_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SCORE_POLICY_NAME
    payload = {key: value for key, value in policy.items() if key not in {"hash", "_snapshot_hash"}}
    payload["_snapshot_hash"] = policy["hash"]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_score_snapshot(data_dir: Path, task_id: str, current_path: Path) -> dict:
    snapshot = Path(data_dir) / SCORE_SNAPSHOT_DIR / task_id / SCORE_POLICY_NAME
    return load_score_policy(snapshot if snapshot.is_file() else current_path)


def score_quality(policy: dict, assessment: Any, *, source_score: int,
                  source_note: str, section_score: int, section_note: str,
                  asset_count: int, table_count: int) -> dict:
    """Score a draft without rejecting it for quality gaps or incomplete assessment."""
    assessment = assessment if isinstance(assessment, dict) else {}
    scores = assessment.get("scores") if isinstance(assessment.get("scores"), dict) else {}
    evidence = assessment.get("evidence") if isinstance(assessment.get("evidence"), dict) else {}
    flags = assessment.get("flags") if isinstance(assessment.get("flags"), list) else []
    dimensions_out = []
    weighted_total = 0.0
    coverage_weight = 0
    invalid_scores = []
    for dimension in policy["dimensions"]:
        key = dimension["key"]
        weight = dimension["weight"]
        if key == "source_traceability":
            score, note, origin = source_score, source_note, "server_checked"
        elif key == "structure_and_retrieval":
            score, note, origin = section_score, section_note, "server_checked"
        elif key == "media_handling" and asset_count == 0:
            score, note, origin = 5, "未发现正文图片资产，按不适用计满分", "server_checked"
        elif key == "table_handling" and table_count == 0:
            score, note, origin = 5, "未发现 DOCX 表格，按不适用计满分", "server_checked"
        else:
            raw = scores.get(key)
            if isinstance(raw, dict):
                raw = raw.get("score")
            try:
                numeric = float(raw)
                if not (0 <= numeric <= 5):
                    raise ValueError
                score = numeric
                note = str(evidence.get(key) or "客户端未提供评分依据").strip()
                origin = "client_assessed"
                coverage_weight += weight
            except (TypeError, ValueError):
                score, note, origin = 0, "未提供有效评分，暂按 0 分记录；草稿仍可导出", "unassessed"
                if raw is not None:
                    invalid_scores.append(key)
        weighted_total += float(score) / 5.0 * weight
        dimensions_out.append({"key": key, "label": dimension.get("label", key),
                              "weight": weight, "score": round(float(score), 2),
                              "weighted_score": round(float(score) / 5.0 * weight, 2),
                              "assessment": origin, "evidence": note})
    flags_out = [str(flag) for flag in flags if str(flag).strip()]
    if invalid_scores:
        flags_out.append("评分字段格式错误或超出 0-5，相关项按未评记录")
    total_score = round(weighted_total, 2)
    assessment_coverage = round(coverage_weight + sum(
        d["weight"] for d in dimensions_out if d["assessment"] == "server_checked"), 2)
    return {
        "status": "scored" if assessment_coverage else "unassessed",
        "score_version": policy["version"],
        "score_policy_hash": policy["hash"],
        "weighted_score": total_score,
        "assessment_coverage": assessment_coverage,
        "priority_band": next((band["label"] for band in policy.get("bands", [])
                                if total_score >= band.get("minimum", 101)), "待评估"),
        "dimensions": dimensions_out,
        "flags": flags_out,
        "unresolved_items": assessment.get("unresolved_items", []),
        "note": "评分用于后续知识排序与质量提示；不等于人工审核，也不阻止草稿产出。",
    }
