from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests._path import *  # noqa: F401,F403
from ckp.catalog import list_choices, validate_selection


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ckp-catalog-"))
        config = self.root / "config" / "product_domain_catalog.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({
            "schema": "ckp-product-domain-catalog/v1",
            "products": [
                {"name": "YonBIP 旗舰版", "domains": ["财务会计", "供应链"]},
                {"name": "致远 OA", "domains": ["办公协同"]},
            ],
        }, ensure_ascii=False), encoding="utf-8")

    def test_domains_are_filtered_by_selected_product(self):
        self.assertEqual(["办公协同"], list_choices(self.root, "致远 OA")["domains"])

    def test_product_is_single_and_domain_selection_must_match_mapping(self):
        product, domains = validate_selection(
            self.root, "YonBIP 旗舰版", ["财务会计", "供应链"])
        self.assertEqual("YonBIP 旗舰版", product)
        self.assertEqual(["财务会计", "供应链"], domains)
        with self.assertRaisesRegex(ValueError, "不属于产品线"):
            validate_selection(self.root, "致远 OA", ["财务会计"])

    def test_selection_is_required_and_unknown_product_rejected(self):
        with self.assertRaisesRegex(ValueError, "必须由项目经理"):
            validate_selection(self.root, "", ["办公协同"])
        with self.assertRaisesRegex(ValueError, "未知产品线"):
            validate_selection(self.root, "未知产品", ["领域"])
        with self.assertRaisesRegex(ValueError, "只能是单个产品线"):
            validate_selection(self.root, ["YonBIP 旗舰版", "致远 OA"], ["财务会计"])

    def test_updated_configuration_is_visible_without_restart(self):
        path = self.root / "config" / "product_domain_catalog.json"
        updated = {"schema": "ckp-product-domain-catalog/v1",
                   "products": [{"name": "新产品线", "domains": ["新领域"]}]}
        path.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(["新产品线"], list_choices(self.root)["products"])
        self.assertEqual(["新领域"], list_choices(self.root, "新产品线")["domains"])

    def test_external_catalog_is_hot_read(self):
        external = self.root / "external.json"
        external.write_text(json.dumps({
            "schema": "ckp-product-domain-catalog/v1",
            "products": [{"name": "外部产品", "domains": ["外部领域"]}],
        }, ensure_ascii=False), encoding="utf-8")
        with patch.dict("os.environ", {"CKP_PRODUCT_DOMAIN_CATALOG": str(external)}):
            self.assertEqual(["外部产品"], list_choices(self.root)["products"])
            external.write_text(json.dumps({
                "schema": "ckp-product-domain-catalog/v1",
                "products": [{"name": "更新产品", "domains": ["更新领域"]}],
            }, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(["更新产品"], list_choices(self.root)["products"])
            self.assertEqual(["更新领域"], list_choices(self.root, "更新产品")["domains"])

    def test_catalog_rejects_duplicate_product_names(self):
        path = self.root / "config" / "product_domain_catalog.json"
        path.write_text(json.dumps({
            "schema": "ckp-product-domain-catalog/v1",
            "products": [{"name": "A", "domains": ["D"]},
                         {"name": "A", "domains": ["E"]}],
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "名称重复"):
            list_choices(self.root)


if __name__ == "__main__":
    unittest.main()
