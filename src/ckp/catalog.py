"""Configurable product-line and product-domain choices for draft metadata."""
from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA = "ckp-product-domain-catalog/v1"


def catalog_path(repo_root: Path) -> Path:
    configured = os.environ.get("CKP_PRODUCT_DOMAIN_CATALOG")
    return Path(configured).expanduser() if configured else Path(repo_root) / "config" / "product_domain_catalog.json"


def load_catalog(repo_root: Path) -> dict:
    path = catalog_path(repo_root)
    if not path.is_file():
        raise ValueError(f"产品线/领域清单不存在: {path}")
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"产品线/领域清单不是有效 UTF-8 JSON: {path}: {exc}") from exc
    if not isinstance(catalog, dict) or catalog.get("schema") != SCHEMA:
        raise ValueError(f"产品线/领域清单 schema 必须是 {SCHEMA}")
    products = catalog.get("products")
    if not isinstance(products, list) or not products:
        raise ValueError("产品线/领域清单 products 必须是非空数组")
    seen_products: set[str] = set()
    normalized = []
    for index, item in enumerate(products, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个产品线配置必须是对象")
        name = item.get("name")
        domains = item.get("domains")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"第 {index} 个产品线名称不能为空")
        name = name.strip()
        if name in seen_products:
            raise ValueError(f"产品线名称重复: {name}")
        seen_products.add(name)
        if (not isinstance(domains, list) or not domains
                or any(not isinstance(domain, str) or not domain.strip() for domain in domains)):
            raise ValueError(f"产品线 {name} 必须关联至少一个非空领域")
        clean_domains = [domain.strip() for domain in domains]
        if len(set(clean_domains)) != len(clean_domains):
            raise ValueError(f"产品线 {name} 的领域存在重复值")
        normalized.append({"name": name, "domains": clean_domains})
    return {"schema": SCHEMA, "products": normalized}


def list_choices(repo_root: Path, product: str = "") -> dict:
    catalog = load_catalog(repo_root)
    products = catalog["products"]
    result = {"schema": SCHEMA, "products": [item["name"] for item in products]}
    if product:
        selected = next((item for item in products if item["name"] == product), None)
        if selected is None:
            raise ValueError(f"未知产品线: {product}；请先从产品线清单中选择")
        result.update({"product": selected["name"], "domains": selected["domains"]})
    else:
        result["domains_by_product"] = {item["name"]: item["domains"] for item in products}
    return result


def validate_selection(repo_root: Path, product: str, domains: list[str] | None) -> tuple[str, list[str]]:
    if not isinstance(product, str):
        raise ValueError("products 只能是单个产品线名称，不能多选")
    product = product.strip()
    selected_domains = domains if isinstance(domains, list) else []
    if not product:
        raise ValueError("必须由项目经理从清单中选择一个产品线，不能从文档推断")
    if any(not isinstance(domain, str) for domain in selected_domains):
        raise ValueError("domain 必须是领域名称数组")
    selected_domains = list(dict.fromkeys(domain.strip() for domain in selected_domains if domain.strip()))
    choices = list_choices(repo_root, product)
    if not selected_domains:
        raise ValueError("必须由项目经理从该产品线关联的领域清单中至少选择一个领域")
    invalid = [domain for domain in selected_domains if domain not in choices["domains"]]
    if invalid:
        raise ValueError(f"所选领域不属于产品线 {product}: {', '.join(invalid)}；可选领域: {', '.join(choices['domains'])}")
    return product, selected_domains
