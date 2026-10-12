"""kb_scope 的單元測試——重點是「範圍只能縮小、不能放大」這條安全性質。

用標準庫的 unittest，不引進新的相依（requirements.txt 目前沒有測試框架）：

    cd mcp_server && python -m unittest discover -s tests -v
"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.module import kb_scope  # noqa: E402


class TestValidate(unittest.TestCase):
    def test_accepts_the_three_safe_fields(self):
        scope = kb_scope.validate({"docType": "SOP", "metadataIds": "a,b", "allowDirect": "Y"})
        self.assertEqual(scope, {"docType": "SOP", "metadataIds": "a,b", "allowDirect": "Y"})

    def test_rejects_scope_fields_that_would_widen_permissions(self):
        # compCode/depCode/empID 的條件帶 'ALL'/'' 逃生門，拿來限縮會放大權限，
        # 所以必須擋下來，而不是默默忽略。
        for field in ("compCode", "depCode", "empID"):
            with self.subTest(field=field):
                with self.assertRaises(kb_scope.ScopeError) as ctx:
                    kb_scope.validate({field: "AVC"})
                self.assertIn(field, str(ctx.exception))

    def test_rejects_non_string_values(self):
        with self.assertRaises(kb_scope.ScopeError):
            kb_scope.validate({"docType": ["SOP"]})

    def test_blank_and_empty_normalize_to_none(self):
        self.assertIsNone(kb_scope.validate({}))
        self.assertIsNone(kb_scope.validate({"docType": "   "}))
        self.assertIsNone(kb_scope.validate(None))


class TestNarrow(unittest.TestCase):
    PERM = [{"compCode": "AVC", "depCode": "IT", "empID": "", "allowDirect": "Y"}]

    def test_no_scope_leaves_filter_untouched(self):
        self.assertEqual(kb_scope.narrow(self.PERM, None), self.PERM)

    def test_doc_type_is_added_when_group_has_none(self):
        out = kb_scope.narrow(self.PERM, {"docType": "SOP"})
        self.assertEqual(out[0]["docType"], "SOP")

    def test_conflicting_doc_type_drops_the_group(self):
        perm = [{"docType": "KM"}, {"docType": "SOP"}]
        out = kb_scope.narrow(perm, {"docType": "SOP"})
        self.assertEqual(out, [{"docType": "SOP"}])

    def test_empty_intersection_returns_none_not_empty_list(self):
        # 這是最重要的一條：空陣列送到 RAG 等於完全不過濾，會把整個知識庫開出去。
        out = kb_scope.narrow([{"docType": "KM"}], {"docType": "SOP"})
        self.assertIsNone(out)

    def test_metadata_ids_intersect(self):
        perm = [{"metadataIds": "a,b,c"}]
        out = kb_scope.narrow(perm, {"metadataIds": "b,c,d"})
        self.assertEqual(out[0]["metadataIds"], "b,c")

    def test_metadata_ids_without_overlap_drops_the_group(self):
        out = kb_scope.narrow([{"metadataIds": "a"}], {"metadataIds": "z"})
        self.assertIsNone(out)

    def test_metadata_ids_apply_when_group_is_unconstrained(self):
        out = kb_scope.narrow([{"compCode": "AVC"}], {"metadataIds": "a,b"})
        self.assertEqual(out[0]["metadataIds"], "a,b")

    def test_allow_direct_only_tightens(self):
        out = kb_scope.narrow([{"compCode": "AVC"}], {"allowDirect": "Y"})
        self.assertEqual(out[0]["allowDirect"], "Y")

    def test_scope_never_touches_the_escape_hatch_fields(self):
        # 安全性質：narrow() 對 compCode/depCode/empID 必須原封不動，
        # 這三個欄位一旦被改寫就可能讓人看到原本看不到的文件。
        out = kb_scope.narrow(self.PERM, {"docType": "SOP", "metadataIds": "a"})
        self.assertEqual(out[0]["compCode"], "AVC")
        self.assertEqual(out[0]["depCode"], "IT")
        self.assertEqual(out[0]["empID"], "")

    def test_does_not_mutate_the_caller_filter(self):
        perm = [{"compCode": "AVC"}]
        kb_scope.narrow(perm, {"docType": "SOP"})
        self.assertEqual(perm, [{"compCode": "AVC"}])

    def test_empty_permission_filter_becomes_one_scoped_group(self):
        # 權限是空陣列（fail-open 降級模式）時，範圍本身變成唯一的條件。
        out = kb_scope.narrow([], {"docType": "SOP"})
        self.assertEqual(out, [{"docType": "SOP"}])

    def test_each_permission_group_is_narrowed_independently(self):
        perm = [{"compCode": "AVC"}, {"compCode": "TW"}]
        out = kb_scope.narrow(perm, {"docType": "SOP"})
        self.assertEqual(len(out), 2)
        self.assertTrue(all(group["docType"] == "SOP" for group in out))


class TestStore(unittest.TestCase):
    def setUp(self):
        kb_scope._store.clear()

    def test_put_then_get(self):
        kb_scope.put("agent:2011994:abc", {"docType": "SOP"})
        self.assertEqual(kb_scope.get("agent:2011994:abc"), {"docType": "SOP"})

    def test_scopes_are_isolated_per_session(self):
        kb_scope.put("agent:2011994:abc", {"docType": "SOP"})
        self.assertIsNone(kb_scope.get("agent:2011994:other"))
        self.assertIsNone(kb_scope.get("agent:9999999:abc"))

    def test_put_none_clears(self):
        kb_scope.put("s", {"docType": "SOP"})
        kb_scope.put("s", None)
        self.assertIsNone(kb_scope.get("s"))

    def test_missing_session_key_is_no_scope(self):
        self.assertIsNone(kb_scope.get(None))
        self.assertIsNone(kb_scope.get(""))

    def test_expired_entry_is_dropped(self):
        kb_scope.put("s", {"docType": "SOP"})
        kb_scope._store["s"] = (time.time() - 1, {"docType": "SOP"})
        self.assertIsNone(kb_scope.get("s"))


if __name__ == "__main__":
    unittest.main()
