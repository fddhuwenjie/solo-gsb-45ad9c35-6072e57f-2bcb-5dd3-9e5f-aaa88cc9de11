"""HTTP 层测试：凭据换发接口。

需要 Flask（本地无依赖时可跳过；Docker 镜像内可直接运行）：
    python -m unittest test_reissue_api.py -v
"""
import os
import sqlite3
import tempfile
import unittest

_fd, _DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _DB

from app import app  # noqa: E402

PUB_DATE = "2026-10-04"
OLD_URL_A = "https://old.example.com/a"
OLD_URL_B = "https://old.example.com/b"
NEW_URL_A = "https://new.example.com/a"
NEW_URL_B = "https://new.example.com/b"


class ReissueApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def setUp(self):
        conn = sqlite3.connect(_DB)
        conn.execute("PRAGMA foreign_keys = ON")
        for table in ("licenses", "segments", "speakers", "interviews"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        c = self.client
        self.iid = c.post("/api/interviews", json={
            "title": "访谈", "duration_seconds": 600}).get_json()["id"]
        self.sp = c.post(f"/api/interviews/{self.iid}/speakers",
                         json={"name": "甲"}).get_json()["id"]
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp, "start_seconds": 0, "end_seconds": 600})
        c.post(f"/api/speakers/{self.sp}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})
        self.c1 = c.post(f"/api/interviews/{self.iid}/credentials", json={
            "publication_date": PUB_DATE, "medium": "audio"}).get_json()["id"]
        self.p1 = c.post(f"/api/credentials/{self.c1}/placements", json={
            "channel": "官网", "url": OLD_URL_A}).get_json()["id"]
        self.p2 = c.post(f"/api/credentials/{self.c1}/placements", json={
            "channel": "播客", "url": OLD_URL_B}).get_json()["id"]
        self.c2 = c.post(f"/api/interviews/{self.iid}/credentials", json={
            "publication_date": PUB_DATE, "medium": "audio"}).get_json()["id"]

    # 凭据 / 投放 / 换发记录不可删除（触发器保护），且各用例共享同一库，
    # 因此在每条用例动作前即时记录计数基线，按增量断言。
    def baseline(self):
        return (len(self.client.get("/api/placements").get_json()),
                len(self.client.get("/api/reissues").get_json()))

    def default_items(self):
        return [{"placement_id": self.p1, "new_url": NEW_URL_A},
                {"placement_id": self.p2, "new_url": NEW_URL_B}]

    def reissue(self, old=None, new=None, items=None):
        return self.client.post(
            f"/api/credentials/{old or self.c1}/reissue",
            json={"new_credential_id": new or self.c2,
                  "items": self.default_items() if items is None else items})

    def get_placement(self, pid):
        conn = sqlite3.connect(_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM channel_placements WHERE id = ?",
            (pid,)).fetchone()
        conn.close()
        return row

    # ---------------------------------------------------------- 成功

    def test_success_atomic_batch(self):
        base_p, base_r = self.baseline()
        resp = self.reissue()
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertTrue(data["reissued"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["interview_id"], self.iid)
        self.assertEqual(data["medium"], "audio")
        self.assertEqual(data["publication_date"], PUB_DATE)
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p + 2)
        self.assertEqual(
            len(self.client.get("/api/reissues").get_json()), base_r + 1)
        # 旧投放已撤下，原因与撤下时间保留
        for pid in (self.p1, self.p2):
            row = self.get_placement(pid)
            self.assertEqual(row["status"], "withdrawn")
            self.assertIn("换发", row["withdraw_reason"])
            self.assertIsNotNone(row["withdrawn_at"])
        # 逐条关联：新投放按原渠道、新链接登记并挂新凭据
        links = {l["old_placement_id"]: l for l in data["links"]}
        n1 = links[self.p1]["new_placement_id"]
        n2 = links[self.p2]["new_placement_id"]
        r1, r2 = self.get_placement(n1), self.get_placement(n2)
        self.assertEqual((r1["credential_id"], r1["channel"],
                          r1["url"], r1["status"]),
                         (self.c2, "官网", NEW_URL_A, "active"))
        self.assertEqual((r2["credential_id"], r2["channel"],
                          r2["url"], r2["status"]),
                         (self.c2, "播客", NEW_URL_B, "active"))

    def test_links_visible_in_list_detail_and_reissue_views(self):
        data = self.reissue().get_json()
        n1 = next(l["new_placement_id"] for l in data["links"]
                  if l["old_placement_id"] == self.p1)
        # 投放列表与详情含 reissue 关联
        items = {i["id"]: i
                 for i in self.client.get("/api/placements").get_json()}
        self.assertEqual(items[self.p1]["reissue"]["role"], "old")
        self.assertEqual(items[self.p1]["reissue"]["other_placement_id"], n1)
        self.assertEqual(items[self.p1]["reissue"]["other_url"], NEW_URL_A)
        self.assertEqual(items[n1]["reissue"]["role"], "new")
        self.assertEqual(items[n1]["reissue"]["other_url"], OLD_URL_A)
        detail = self.client.get(f"/api/placements/{n1}").get_json()
        self.assertEqual(detail["reissue"]["other_placement_id"], self.p1)
        # 换发列表与详情
        listing = self.client.get("/api/reissues").get_json()
        self.assertEqual(listing[-1]["id"], data["id"])
        self.assertEqual(listing[-1]["count"], 2)
        rd = self.client.get(f"/api/reissues/{data['id']}").get_json()
        self.assertEqual(rd["old_credential"]["id"], self.c1)
        self.assertEqual(rd["new_credential"]["id"], self.c2)

    def test_reissued_placements_remain_license_guarded(self):
        self.reissue()
        lid = self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"][0]["id"]
        resp = self.client.put(f"/api/licenses/{lid}", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": [], "deny_media": ["audio"]})
        self.assertEqual(resp.status_code, 409)
        affected = {a["placement_id"]
                    for a in resp.get_json()["affected_placements"]}
        # 已撤下的旧投放不在受影响集合；换发后的新投放受保护
        self.assertNotIn(self.p1, affected)
        self.assertNotIn(self.p2, affected)
        new_active = [i["id"] for i in
                      self.client.get("/api/placements").get_json()
                      if i["credential_id"] == self.c2
                      and i["status"] == "active"]
        self.assertTrue(new_active)
        self.assertTrue(set(new_active) <= affected)

    # ---------------------------------------------------------- 凭据校验

    def test_same_credential_rejected(self):
        base_p, base_r = self.baseline()
        resp = self.reissue(new=self.c1)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["reason"], "same_credential")
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p)
        self.assertEqual(
            len(self.client.get("/api/reissues").get_json()), base_r)

    def test_missing_new_credential_id_rejected(self):
        resp = self.client.post(
            f"/api/credentials/{self.c1}/reissue",
            json={"items": []})
        self.assertEqual(resp.status_code, 400)

    def test_old_or_new_credential_not_found(self):
        base_p, _ = self.baseline()
        resp = self.reissue(old=9999)
        self.assertEqual(resp.status_code, 404)
        resp = self.reissue(new=9999)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p)

    def test_mismatched_date_or_medium_rejected(self):
        base_p, _ = self.baseline()
        other = self.client.post(f"/api/interviews/{self.iid}/credentials",
                                 json={"publication_date": "2027-01-01",
                                       "medium": "audio"}).get_json()["id"]
        resp = self.reissue(new=other)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["reason"], "mismatch")
        # 仅有原 2 条投放，换发未产生新投放
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p)

    def test_new_credential_changed_materials_rejected(self):
        base_p, base_r = self.baseline()
        # 追加同样允许音频的授权：仍可发布，但新凭据与当前资料不一致
        self.client.post(f"/api/speakers/{self.sp}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充"})
        resp = self.reissue()
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["reason"], "new_invalid")
        self.assertEqual(data["changes"], ["授权"])
        # 整批失败：旧记录不变，也不产生新投放
        self.assertEqual(self.get_placement(self.p1)["status"], "active")
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p)
        self.assertEqual(
            len(self.client.get("/api/reissues").get_json()), base_r)

    # ---------------------------------------------------------- 清单校验

    def test_no_active_placements_rejected(self):
        base_p, base_r = self.baseline()
        resp = self.reissue(old=self.c2, new=self.c1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["reason"], "no_active")
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p)
        self.assertEqual(
            len(self.client.get("/api/reissues").get_json()), base_r)

    def test_missing_entry_rolls_back(self):
        base_p, base_r = self.baseline()
        resp = self.reissue(items=[
            {"placement_id": self.p1, "new_url": NEW_URL_A}])
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["reason"], "missing_entry")
        self.assertEqual(data["missing_placement_ids"], [self.p2])
        self.assertEqual(self.get_placement(self.p1)["status"], "active")
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p)
        self.assertEqual(
            len(self.client.get("/api/reissues").get_json()), base_r)

    def test_extra_or_duplicate_entry_rolls_back(self):
        base_p, base_r = self.baseline()
        extra = self.client.post(f"/api/credentials/{self.c2}/placements",
                                 json={"channel": "官网",
                                       "url": "https://extra.example"}
                                 ).get_json()["id"]
        # 多余记录（属于 c2，不在 c1 在投集合内）
        resp = self.reissue(items=self.default_items() + [
            {"placement_id": extra, "new_url": "https://z.example"}])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.get_json()["unexpected_placement_ids"], [extra])
        # 重复条目
        resp = self.reissue(items=[
            {"placement_id": self.p1, "new_url": NEW_URL_A},
            {"placement_id": self.p1, "new_url": "https://dup.example"},
            {"placement_id": self.p2, "new_url": NEW_URL_B}])
        self.assertEqual(resp.status_code, 400)
        # 空白新链接
        resp = self.reissue(items=[
            {"placement_id": self.p1, "new_url": "  "},
            {"placement_id": self.p2, "new_url": NEW_URL_B}])
        self.assertEqual(resp.status_code, 400)
        # 仅多出登记的那条 extra；换发均未落任何记录
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()), base_p + 1)
        self.assertEqual(
            len(self.client.get("/api/reissues").get_json()), base_r)

    def test_empty_items_rejected(self):
        resp = self.reissue(items=[])
        self.assertEqual(resp.status_code, 400)

    # ---------------------------------------------------------- 查阅 404

    def test_reissue_detail_not_found(self):
        self.assertEqual(
            self.client.get("/api/reissues/9999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
