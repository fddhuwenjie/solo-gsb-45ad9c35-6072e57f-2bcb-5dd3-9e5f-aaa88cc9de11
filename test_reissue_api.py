"""HTTP 层测试：渠道内容迁移后的凭据换发接口。

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
CHANNEL_A = "官网专题页"
CHANNEL_B = "播客平台"
URL_A1 = "https://example.com/old/a"
URL_B1 = "https://example.com/old/b"
URL_A2 = "https://example.com/new/a"
URL_B2 = "https://example.com/new/b"


class ReissueApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def setUp(self):
        conn = sqlite3.connect(_DB)
        conn.execute("PRAGMA foreign_keys = ON")
        for table in ("placement_reissues", "placement_reissue_batches",
                      "channel_placements", "release_credentials",
                      "licenses", "segments", "speakers", "interviews"):
            # 换发 / 投放 / 凭据均有不可删除触发器，逐表 DELETE 失败即跳过；
            # 依赖业务表（访谈级联 + 顺序）清空夹具数据
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.IntegrityError:
                conn.rollback()
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
            "allow_media": ["audio", "text"], "deny_media": [],
            "embargo_until": None, "note": ""})
        self.seal = lambda medium="audio", date=PUB_DATE: c.post(
            f"/api/interviews/{self.iid}/credentials",
            json={"publication_date": date, "medium": medium}
        ).get_json()["id"]
        self.old_cid = self.seal()
        self.pid_a = c.post(f"/api/credentials/{self.old_cid}/placements",
                            json={"channel": CHANNEL_A, "url": URL_A1}
                            ).get_json()["id"]
        self.pid_b = c.post(f"/api/credentials/{self.old_cid}/placements",
                            json={"channel": CHANNEL_B, "url": URL_B1}
                            ).get_json()["id"]
        self.new_cid = self.seal()

    def reissue(self, old=None, new=None, items=...):
        body = {
            "old_credential_id": old or self.old_cid,
            "new_credential_id": new or self.new_cid,
        }
        if items is not ...:
            body["items"] = items
        return self.client.post("/api/placements/reissue", json=body)

    def default_items(self):
        return [{"placement_id": self.pid_a, "url": URL_A2},
                {"placement_id": self.pid_b, "url": URL_B2}]

    def placements(self):
        return {p["id"]: p
                for p in self.client.get("/api/placements").get_json()}

    def own_placements(self):
        # 凭据 / 投放 / 换发记录只增不删：只统计本夹具两条凭据的投放
        plcs = self.placements()
        return [p for p in plcs.values()
                if p["credential_id"] in (self.old_cid, self.new_cid)]

    def placement_count(self):
        return len(self.own_placements())

    # ---------------------------------------------------------- 成功路径

    def test_reissue_success_atomically_swaps_and_links(self):
        before = self.placement_count()
        resp = self.reissue(items=self.default_items())
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertTrue(data["reissued"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["interview_title"], "访谈")
        self.assertEqual(data["medium"], "audio")
        self.assertEqual(self.placement_count(), before + 2)

        plcs = self.placements()
        by_old = {it["old_placement_id"]: it for it in data["items"]}
        for old_pid, channel, old_url, new_url in (
                (self.pid_a, CHANNEL_A, URL_A1, URL_A2),
                (self.pid_b, CHANNEL_B, URL_B1, URL_B2)):
            old = plcs[old_pid]
            self.assertEqual(old["status"], "withdrawn")
            self.assertEqual(old["status_label"], "已撤下")
            self.assertEqual(old["channel"], channel)
            self.assertEqual(old["url"], old_url)
            self.assertTrue(old["withdrawn_at"])
            self.assertEqual(old["withdraw_reason"],
                             f"凭据换发：内容已迁至 {new_url}")
            self.assertIsNone(old["reissue"]["as_new"])
            link = old["reissue"]["as_old"]
            self.assertEqual(link["other_placement_id"],
                             by_old[old_pid]["new_placement_id"])
            self.assertEqual(link["new_credential_id"], self.new_cid)
            new = plcs[link["other_placement_id"]]
            self.assertEqual(new["status"], "active")
            self.assertEqual(new["credential_id"], self.new_cid)
            self.assertEqual(new["channel"], channel)
            self.assertEqual(new["url"], new_url)
            self.assertEqual(new["risk"], "ok")
            self.assertIsNone(new["reissue"]["as_old"])
            self.assertEqual(new["reissue"]["as_new"]["other_placement_id"],
                             old_pid)
            self.assertEqual(new["reissue"]["as_new"]["url"], old_url)

        # 详情接口同样给出关联
        detail = self.client.get(
            f"/api/placements/{self.pid_a}").get_json()
        self.assertEqual(detail["reissue"]["as_old"]["url"], URL_A2)

    def test_reissue_history_endpoint_lists_batches(self):
        r1 = self.reissue(items=self.default_items()).get_json()
        resp = self.client.get("/api/reissues")
        self.assertEqual(resp.status_code, 200)
        batches = resp.get_json()
        self.assertEqual(len(batches), 1)
        b = batches[0]
        self.assertEqual(b["id"], r1["batch_id"])
        self.assertEqual(b["old_credential_id"], self.old_cid)
        self.assertEqual(b["new_credential_id"], self.new_cid)
        self.assertEqual(b["interview_id"], self.iid)
        self.assertEqual(b["publication_date"], PUB_DATE)
        self.assertEqual(b["medium"], "audio")
        self.assertEqual(len(b["items"]), 2)
        item = next(i for i in b["items"]
                    if i["old_placement_id"] == self.pid_a)
        self.assertEqual(item["old_status"], "withdrawn")
        self.assertEqual(item["old_status_label"], "已撤下")
        self.assertEqual(item["new_status"], "active")
        self.assertEqual(item["new_status_label"], "投放中")
        self.assertEqual(item["new_channel"], CHANNEL_A)
        self.assertEqual(item["new_url"], URL_A2)

    def test_new_placement_participates_in_license_guard(self):
        data = self.reissue(items=self.default_items()).get_json()
        new_pid = data["items"][0]["new_placement_id"]
        lic = self.client.get(f"/api/interviews/{self.iid}").get_json()[
            "licenses"][0]["id"]
        resp = self.client.put(f"/api/licenses/{lic}", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": [], "deny_media": ["audio"],
            "embargo_until": None, "note": ""})
        self.assertEqual(resp.status_code, 409)
        affected = {p["placement_id"]
                    for p in resp.get_json()["affected_placements"]}
        self.assertIn(new_pid, affected)
        self.assertNotIn(self.pid_a, affected)
        # 新投放仍在投（整笔授权变更已回滚）
        self.assertEqual(self.placements()[new_pid]["status"], "active")

    # ---------------------------------------------------------- 请求校验

    def test_missing_or_invalid_id_fields_are_400(self):
        for body in (
            {"new_credential_id": self.new_cid, "items": self.default_items()},
            {"old_credential_id": self.old_cid,
             "items": self.default_items()},
            {"old_credential_id": "1", "new_credential_id": self.new_cid,
             "items": self.default_items()},
            {"old_credential_id": -1, "new_credential_id": self.new_cid,
             "items": self.default_items()},
        ):
            self.assertEqual(
                self.client.post("/api/placements/reissue",
                                 json=body).status_code, 400, body)
        self.assertEqual(self.placement_count(), 2)

    def test_items_shape_errors_are_400(self):
        for items in (None, [], [{"url": URL_A2}],
                      [{"placement_id": self.pid_a}],
                      [{"placement_id": self.pid_a, "url": "  "}],
                      [{"placement_id": self.pid_a, "url": URL_A2},
                       {"placement_id": self.pid_a, "url": URL_B2}]):
            resp = self.reissue(items=items)
            self.assertEqual(resp.status_code, 400, items)
        self.assertEqual(self.placement_count(), 2)

    # ---------------------------------------------------------- 凭据校验

    def test_unknown_credentials_are_404(self):
        resp = self.reissue(old=9999, items=self.default_items())
        self.assertEqual(resp.status_code, 404)
        resp = self.reissue(new=9999, items=self.default_items())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.placement_count(), 2)

    def test_same_credential_is_409(self):
        resp = self.reissue(new=self.old_cid, items=self.default_items())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["reason"], "mismatch")

    def test_different_interview_date_medium_are_409(self):
        # 不同媒介
        text_cid = self.seal(medium="text")
        resp = self.reissue(new=text_cid, items=self.default_items())
        self.assertEqual(resp.status_code, 409)
        self.assertIn("同一媒介", resp.get_json()["error"])
        # 不同发布日期
        date_cid = self.seal(date="2027-01-01")
        resp = self.reissue(new=date_cid, items=self.default_items())
        self.assertEqual(resp.status_code, 409)
        self.assertIn("同一发布日期", resp.get_json()["error"])
        # 不同访谈
        iid2 = self.client.post("/api/interviews", json={
            "title": "另一个", "duration_seconds": 600}).get_json()["id"]
        sp2 = self.client.post(f"/api/interviews/{iid2}/speakers",
                               json={"name": "乙"}).get_json()["id"]
        self.client.post(f"/api/interviews/{iid2}/segments", json={
            "speaker_id": sp2, "start_seconds": 0, "end_seconds": 600})
        self.client.post(f"/api/speakers/{sp2}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})
        other_cid = self.client.post(
            f"/api/interviews/{iid2}/credentials",
            json={"publication_date": PUB_DATE, "medium": "audio"}
        ).get_json()["id"]
        resp = self.reissue(new=other_cid, items=self.default_items())
        self.assertEqual(resp.status_code, 409)
        self.assertIn("同一访谈", resp.get_json()["error"])
        self.assertEqual(self.placement_count(), 2)

    def test_new_credential_changed_is_409_and_leaves_nothing(self):
        self.client.post(f"/api/speakers/{self.sp}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充"})
        resp = self.reissue(items=self.default_items())
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["reissued"])
        self.assertEqual(data["reason"], "new_changed")
        self.assertEqual(data["changes"], ["授权"])
        self.assertEqual(self.placement_count(), 2)
        self.assertEqual(self.placements()[self.pid_a]["status"], "active")

    # ---------------------------------------------------------- 清单校验

    def test_missing_list_entry_is_409_with_id_lists(self):
        resp = self.reissue(items=[
            {"placement_id": self.pid_a, "url": URL_A2}])
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["reason"], "placements_mismatch")
        self.assertEqual(data["missing_placement_ids"], [self.pid_b])
        self.assertEqual(data["extra_placement_ids"], [])
        self.assertEqual(self.placement_count(), 2)

    def test_extra_entry_belonging_to_other_credential_is_409(self):
        other_cid = self.seal()
        other_pid = self.client.post(
            f"/api/credentials/{other_cid}/placements",
            json={"channel": "别的渠道", "url": "https://x/o"}
        ).get_json()["id"]
        resp = self.reissue(items=self.default_items() + [
            {"placement_id": other_pid, "url": "https://x/o2"}])
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["extra_placement_ids"], [other_pid])
        # 夹带的他凭据在投投放未被动到，旧凭据仍只有 2 条在投
        self.assertEqual(self.placement_count(), 2)
        self.assertEqual(self.placements()[other_pid]["status"], "active")

    def test_no_active_placement_is_409(self):
        for pid in (self.pid_a, self.pid_b):
            self.client.post(f"/api/placements/{pid}/withdraw",
                             json={"reason": "下线"})
        resp = self.reissue(items=self.default_items())
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["reason"], "no_active")

    def test_active_set_change_between_calls_is_detected(self):
        # 第一次成功后用同样的旧清单再换一次：旧凭据已无在投记录
        first = self.reissue(items=self.default_items())
        self.assertEqual(first.status_code, 201)
        second = self.reissue(items=self.default_items())
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["reason"], "no_active")


if __name__ == "__main__":
    unittest.main()
