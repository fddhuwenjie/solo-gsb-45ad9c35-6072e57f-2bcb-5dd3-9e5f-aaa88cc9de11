"""凭据换发事务编排（reissues 模块）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
同一写事务内整批换发 —— 新旧凭据不同且对应同一访谈 / 发布日期 /
媒介、新凭据与当前资料一致且整段可发布、清单恰好覆盖旧凭据全部在投
记录（每条一次），通过后旧投放标记已撤下、按原渠道与新链接登记新投放
并逐条保存前后关联；任一条件不满足即整批回滚（旧记录不变，也不产生
新投放）；换发后的投放继续参与授权变更保护；换发记录只增不删。
"""
import os
import sqlite3
import tempfile
import unittest

from credentials import create_credential
from license_guard import update_license
from placements import (
    STATUS_ACTIVE,
    STATUS_WITHDRAWN,
    list_placements,
    placement_review,
    register_placement,
    withdraw_placement,
)
from reissues import (
    WITHDRAW_REASON_PREFIX,
    list_reissues,
    placement_link,
    reissue_placements,
)

# 与 app.py 中 SCHEMA 保持一致（测试夹具，独立维护）
SCHEMA = """
CREATE TABLE interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0)
);
CREATE TABLE speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    name TEXT NOT NULL
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    allow_media TEXT NOT NULL DEFAULT '[]',
    deny_media TEXT NOT NULL DEFAULT '[]',
    embargo_until TEXT,
    note TEXT NOT NULL DEFAULT '',
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE release_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL,
    interview_title TEXT NOT NULL,
    medium TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER release_credentials_no_update
BEFORE UPDATE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可修改'); END;
CREATE TRIGGER release_credentials_no_delete
BEFORE DELETE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可删除'); END;
CREATE TABLE channel_placements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    channel TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'withdrawn')),
    withdraw_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    withdrawn_at TEXT
);
CREATE TRIGGER channel_placements_no_delete
BEFORE DELETE ON channel_placements
BEGIN SELECT RAISE(ABORT, '投放记录不可删除'); END;
CREATE TRIGGER channel_placements_sealed_fields
BEFORE UPDATE ON channel_placements
WHEN OLD.credential_id <> NEW.credential_id
  OR OLD.channel <> NEW.channel
  OR OLD.url <> NEW.url
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, '投放记录的凭据、渠道、链接与登记时间不可修改'); END;
CREATE TRIGGER channel_placements_no_reopen
BEFORE UPDATE ON channel_placements
WHEN OLD.status = 'withdrawn'
BEGIN SELECT RAISE(ABORT, '投放记录已撤下，不可再修改'); END;
CREATE TABLE placement_reissue_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    new_credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE placement_reissues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES placement_reissue_batches(id),
    old_placement_id INTEGER NOT NULL UNIQUE REFERENCES channel_placements(id),
    new_placement_id INTEGER NOT NULL UNIQUE REFERENCES channel_placements(id),
    old_credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    new_credential_id INTEGER NOT NULL REFERENCES release_credentials(id)
);
CREATE TRIGGER placement_reissue_batches_no_update
BEFORE UPDATE ON placement_reissue_batches
BEGIN SELECT RAISE(ABORT, '凭据换发批次不可修改'); END;
CREATE TRIGGER placement_reissue_batches_no_delete
BEFORE DELETE ON placement_reissue_batches
BEGIN SELECT RAISE(ABORT, '凭据换发批次不可删除'); END;
CREATE TRIGGER placement_reissues_no_update
BEFORE UPDATE ON placement_reissues
BEGIN SELECT RAISE(ABORT, '凭据换发关联不可修改'); END;
CREATE TRIGGER placement_reissues_no_delete
BEFORE DELETE ON placement_reissues
BEGIN SELECT RAISE(ABORT, '凭据换发关联不可删除'); END;
"""

PUB_DATE = "2026-10-04"
CHANNEL_A = "官网专题页"
CHANNEL_B = "播客平台"
URL_A1 = "https://example.com/old/a"
URL_B1 = "https://example.com/old/b"
URL_A2 = "https://example.com/new/a"
URL_B2 = "https://example.com/new/b"


def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


class ReissueTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频与文字
    -> 音频版凭据 #1 已封存，凭据 #1 下有两条在投投放（不同渠道）；
    凭据 #2 为同一访谈 / 发布日期 / 媒介封存的新凭据。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        c = self.conn
        c.execute("INSERT INTO interviews (title, duration_seconds)"
                  " VALUES ('访谈', 600)")
        c.execute("INSERT INTO speakers (interview_id, name) VALUES (1, '甲')")
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 1, 0, 600)")
        c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                  " end_seconds, allow_media, deny_media, embargo_until,"
                  " note) VALUES (1, 0, 600, '[\"audio\",\"text\"]', '[]',"
                  " NULL, '')")
        c.commit()
        c1, st1 = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")
        assert st1 == 201
        self.old_cid = c1["id"]
        self.pid_a = register_placement(
            self.conn, self.old_cid, CHANNEL_A, URL_A1)[0]["id"]
        self.pid_b = register_placement(
            self.conn, self.old_cid, CHANNEL_B, URL_B1)[0]["id"]
        c2, st2 = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")
        assert st2 == 201
        self.new_cid = c2["id"]

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    _DEFAULT = object()

    def reissue(self, old=None, new=None, items=_DEFAULT):
        if items is self._DEFAULT:
            items = [{"placement_id": self.pid_a, "url": URL_A2},
                     {"placement_id": self.pid_b, "url": URL_B2}]
        return reissue_placements(
            self.conn,
            old_credential_id=old or self.old_cid,
            new_credential_id=new or self.new_cid,
            raw_items=items)

    def placement(self, pid):
        return _row(self.conn,
                    "SELECT * FROM channel_placements WHERE id = ?", (pid,))

    def counts(self):
        return (
            _row(self.conn,
                 "SELECT COUNT(*) AS n FROM channel_placements")["n"],
            _row(self.conn,
                 "SELECT COUNT(*) AS n FROM placement_reissue_batches")["n"],
            _row(self.conn,
                 "SELECT COUNT(*) AS n FROM placement_reissues")["n"],
        )

    def license_id(self):
        return _row(self.conn, "SELECT id FROM licenses ORDER BY id")["id"]

    # ---------------------------------------------------------- 成功路径

    def test_success_withdraws_old_and_registers_new_in_one_batch(self):
        result, status = self.reissue()
        self.assertEqual(status, 201)
        self.assertTrue(result["reissued"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["old_credential_id"], self.old_cid)
        self.assertEqual(result["new_credential_id"], self.new_cid)
        self.assertEqual(result["interview_id"], 1)
        self.assertEqual(result["publication_date"], PUB_DATE)
        self.assertEqual(result["medium"], "audio")
        self.assertTrue(result["batch_id"])
        self.assertTrue(result["created_at"])

        # 旧投放：已撤下，撤下原因注明凭据换发与新链接，渠道 / 旧链接不动
        for pid, new_url in ((self.pid_a, URL_A2), (self.pid_b, URL_B2)):
            old = self.placement(pid)
            self.assertEqual(old["status"], STATUS_WITHDRAWN)
            self.assertTrue(old["withdrawn_at"])
            self.assertEqual(old["withdraw_reason"],
                             WITHDRAW_REASON_PREFIX + new_url)

        # 新投放：投放中、沿用原渠道、使用新链接、挂新凭据
        items = {it["old_placement_id"]: it for it in result["items"]}
        for old_pid, channel, new_url in (
                (self.pid_a, CHANNEL_A, URL_A2),
                (self.pid_b, CHANNEL_B, URL_B2)):
            it = items[old_pid]
            self.assertEqual(it["channel"], channel)
            self.assertEqual(it["old_url"],
                             URL_A1 if old_pid == self.pid_a else URL_B1)
            self.assertEqual(it["new_url"], new_url)
            self.assertEqual(it["old_status"], STATUS_WITHDRAWN)
            self.assertEqual(it["new_status"], STATUS_ACTIVE)
            self.assertTrue(it["reissue_id"])
            new_row = self.placement(it["new_placement_id"])
            self.assertEqual(new_row["status"], STATUS_ACTIVE)
            self.assertEqual(new_row["credential_id"], self.new_cid)
            self.assertEqual(new_row["channel"], channel)
            self.assertEqual(new_row["url"], new_url)
            self.assertIsNone(new_row["withdrawn_at"])

        # 2 条旧 + 2 条新投放；1 个批次 2 条关联
        self.assertEqual(self.counts(), (4, 1, 2))

    def test_linkage_saved_per_item_and_queryable_both_directions(self):
        result, _ = self.reissue()
        it_a, it_b = sorted(result["items"],
                            key=lambda x: x["old_placement_id"])
        # 旧投放 -> 新投放
        link = placement_link(self.conn, self.pid_a)
        self.conn.rollback()  # placement_link 不自行开事务
        self.assertIsNone(link["as_new"])
        side = link["as_old"]
        self.assertEqual(side["other_placement_id"], it_a["new_placement_id"])
        self.assertEqual(side["old_credential_id"], self.old_cid)
        self.assertEqual(side["new_credential_id"], self.new_cid)
        self.assertEqual(side["url"], URL_A2)
        self.assertEqual(side["channel"], CHANNEL_A)
        self.assertEqual(side["status"], STATUS_ACTIVE)
        self.assertEqual(side["batch_id"], result["batch_id"])
        # 新投放 -> 旧投放
        link2 = placement_link(self.conn, it_b["new_placement_id"])
        self.conn.rollback()
        self.assertIsNone(link2["as_old"])
        side2 = link2["as_new"]
        self.assertEqual(side2["other_placement_id"], self.pid_b)
        self.assertEqual(side2["url"], URL_B1)
        self.assertEqual(side2["channel"], CHANNEL_B)
        self.assertEqual(side2["status"], STATUS_WITHDRAWN)

    def test_linkage_visible_in_placement_list_and_detail(self):
        result, _ = self.reissue()
        new_pid = result["items"][0]["new_placement_id"]
        items = list_placements(self.conn)
        old_item = next(i for i in items if i["id"] == self.pid_a)
        new_item = next(i for i in items if i["id"] == new_pid)
        self.assertEqual(old_item["status"], STATUS_WITHDRAWN)
        self.assertIsNotNone(old_item["reissue"]["as_old"])
        self.assertIsNone(old_item["reissue"]["as_new"])
        self.assertEqual(
            old_item["reissue"]["as_old"]["other_placement_id"], new_pid)
        self.assertEqual(old_item["reissue"]["as_old"]["status_label"], "投放中")
        self.assertIsNone(new_item["reissue"]["as_old"])
        self.assertIsNotNone(new_item["reissue"]["as_new"])
        self.assertEqual(
            new_item["reissue"]["as_new"]["other_placement_id"], self.pid_a)
        detail = placement_review(self.conn, new_pid)
        self.assertEqual(detail["reissue"]["as_new"]["url"], URL_A1)

    def test_reissue_history_groups_items_in_batches(self):
        first, _ = self.reissue()
        # 再次换发：新投放再迁到凭据 #3（先封存凭据 #3）
        c3, st = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")
        self.assertEqual(st, 201)
        prev = {it["channel"]: it["new_placement_id"]
                for it in first["items"]}
        second_items = [
            {"placement_id": prev[CHANNEL_A], "url": "https://x/3a"},
            {"placement_id": prev[CHANNEL_B], "url": "https://x/3b"}]
        second, st2 = reissue_placements(
            self.conn, self.new_cid, c3["id"], second_items)
        self.assertEqual(st2, 201)

        batches = list_reissues(self.conn)
        self.assertEqual([b["id"] for b in batches],
                         [first["batch_id"], second["batch_id"]])
        self.assertEqual(len(batches[0]["items"]), 2)
        self.assertEqual(len(batches[1]["items"]), 2)
        b1 = batches[0]["items"][0]
        self.assertEqual(b1["old_status"], STATUS_WITHDRAWN)
        # 关联查询反映投放当前状态：第一批的新投放经第二次换发已撤下
        self.assertEqual(b1["new_status"], STATUS_WITHDRAWN)
        # 链式：第一批的新投放是第二批的旧投放
        first_new_ids = {it["new_placement_id"] for it in batches[0]["items"]}
        second_old_ids = {it["old_placement_id"] for it in batches[1]["items"]}
        self.assertEqual(first_new_ids, second_old_ids)

    def test_new_placement_still_protected_by_license_guard(self):
        result, _ = self.reissue()
        new_pid = result["items"][0]["new_placement_id"]
        # 授权改为拒绝音频：换发后的在投投放仍应拦截变更并回滚
        blocked, status = update_license(self.conn, self.license_id(), {
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": [], "deny_media": ["audio"],
            "embargo_until": None, "note": ""})
        self.assertEqual(status, 409)
        affected = {p["placement_id"] for p in blocked["affected_placements"]}
        self.assertIn(new_pid, affected)
        self.assertNotIn(self.pid_a, affected)  # 旧投放已撤下，不参与
        # 授权未改动，新投放仍在投
        self.assertEqual(self.placement(new_pid)["status"], STATUS_ACTIVE)

    # ---------------------------------------------------------- 凭据校验

    def test_unknown_old_credential_is_404(self):
        result, status = self.reissue(old=9999)
        self.assertEqual(status, 404)
        self.assertIn("旧凭据", result["error"])
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_unknown_new_credential_is_404(self):
        result, status = self.reissue(new=9999)
        self.assertEqual(status, 404)
        self.assertIn("新凭据", result["error"])
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_same_credential_aborts(self):
        result, status = self.reissue(new=self.old_cid)
        self.assertEqual(status, 409)
        self.assertFalse(result["reissued"])
        self.assertEqual(result["reason"], "mismatch")
        self.assertIn("不同", result["error"])
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_different_interview_aborts(self):
        self.conn.execute(
            "INSERT INTO interviews (title, duration_seconds)"
            " VALUES ('另一个', 600)")
        self.conn.execute(
            "INSERT INTO speakers (interview_id, name) VALUES (2, '乙')")
        self.conn.execute(
            "INSERT INTO segments (interview_id, speaker_id, start_seconds,"
            " end_seconds) VALUES (2, 2, 0, 600)")
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (2, 0, 600, '[\"audio\"]', '[]', NULL, '')")
        self.conn.commit()
        other = create_credential(
            self.conn, interview_id=2,
            publication_date=PUB_DATE, medium="audio")[0]
        result, status = self.reissue(new=other["id"])
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], "mismatch")
        self.assertIn("同一访谈", result["error"])
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_different_publication_date_aborts(self):
        other = create_credential(
            self.conn, interview_id=1,
            publication_date="2027-01-01", medium="audio")[0]
        result, status = self.reissue(new=other["id"])
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], "mismatch")
        self.assertIn("同一发布日期", result["error"])

    def test_different_medium_aborts(self):
        other = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="text")[0]
        result, status = self.reissue(new=other["id"])
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], "mismatch")
        self.assertIn("同一媒介", result["error"])

    def test_new_credential_changed_from_current_materials_aborts(self):
        # 封存凭据 #2 后追加授权：资料变化（仍可发布）-> 新凭据待复核
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '补充')")
        self.conn.commit()
        result, status = self.reissue()
        self.assertEqual(status, 409)
        self.assertFalse(result["reissued"])
        self.assertEqual(result["reason"], "new_changed")
        self.assertEqual(result["changes"], ["授权"])
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_new_credential_currently_blocked_aborts(self):
        # 资料变化导致新凭据版本当前不可发布（变化优先说明，同登记口径）
        self.conn.execute(
            "UPDATE licenses SET allow_media = '[]',"
            " deny_media = '[\"audio\"]' WHERE id = ?",
            (self.license_id(),))
        self.conn.commit()
        result, status = self.reissue()
        self.assertEqual(status, 409)
        self.assertIn(result["reason"], ("new_changed", "new_blocked"))
        # 旧投放原样保留（仍在投），不产生新投放
        self.assertEqual(self.placement(self.pid_a)["status"], STATUS_ACTIVE)
        self.assertEqual(self.counts(), (2, 0, 0))

    # ---------------------------------------------------------- 清单校验

    def test_missing_item_aborts_and_lists_missing_ids(self):
        result, status = self.reissue(items=[
            {"placement_id": self.pid_a, "url": URL_A2}])
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], "placements_mismatch")
        self.assertEqual(result["missing_placement_ids"], [self.pid_b])
        self.assertEqual(result["extra_placement_ids"], [])
        self.assertEqual(self.counts(), (2, 0, 0))
        self.assertEqual(self.placement(self.pid_a)["status"], STATUS_ACTIVE)

    def test_extra_item_from_other_credential_aborts(self):
        other_cid = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")[0]["id"]
        other_pid = register_placement(
            self.conn, other_cid, "别的渠道", "https://x/other")[0]["id"]
        result, status = self.reissue(items=[
            {"placement_id": self.pid_a, "url": URL_A2},
            {"placement_id": self.pid_b, "url": URL_B2},
            {"placement_id": other_pid, "url": "https://x/other2"}])
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], "placements_mismatch")
        self.assertEqual(result["extra_placement_ids"], [other_pid])
        self.assertEqual(self.counts(), (3, 0, 0))
        self.assertEqual(self.placement(other_pid)["status"], STATUS_ACTIVE)

    def test_extra_item_that_was_withdrawn_aborts(self):
        withdraw_placement(self.conn, self.pid_b, "已下线")
        result, status = self.reissue(items=[
            {"placement_id": self.pid_a, "url": URL_A2},
            {"placement_id": self.pid_b, "url": URL_B2}])
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], "placements_mismatch")
        self.assertEqual(result["extra_placement_ids"], [self.pid_b])
        self.assertEqual(result["missing_placement_ids"], [])
        # 手工撤下原因保留，整批未动
        self.assertEqual(
            self.placement(self.pid_b)["withdraw_reason"], "已下线")

    def test_duplicate_placement_in_list_is_400(self):
        result, status = self.reissue(items=[
            {"placement_id": self.pid_a, "url": URL_A2},
            {"placement_id": self.pid_b, "url": URL_B2},
            {"placement_id": self.pid_a, "url": "https://x/dup"}])
        self.assertEqual(status, 400)
        self.assertIn("重复", result["error"])
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_old_credential_without_active_placements_is_409(self):
        withdraw_placement(self.conn, self.pid_a, "下线")
        withdraw_placement(self.conn, self.pid_b, "下线")
        result, status = self.reissue()
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], "no_active")
        self.assertIn("没有在投记录", result["error"])
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_malformed_item_shapes_are_400(self):
        bad_payloads = [
            None, [], "x",
            [{"url": URL_A2}],
            [{"placement_id": "1", "url": URL_A2}],
            [{"placement_id": 0, "url": URL_A2}],
            [{"placement_id": True, "url": URL_A2}],
            [{"placement_id": self.pid_a}],
            [{"placement_id": self.pid_a, "url": "   "}],
            [{"placement_id": self.pid_a, "url": 123}],
            ["not-a-dict"],
        ]
        for raw in bad_payloads:
            result, status = self.reissue(items=raw)
            self.assertEqual(status, 400, f"应拒绝清单形状: {raw!r}")
            self.assertFalse(result["reissued"])
        self.assertEqual(self.counts(), (2, 0, 0))

    # ---------------------------------------------------------- 台账保护

    def test_reissue_records_are_append_only(self):
        self.reissue()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM placement_reissues")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM placement_reissue_batches")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE placement_reissues SET new_placement_id = 1")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE placement_reissue_batches SET new_credential_id = 1")
        self.conn.rollback()
        self.assertEqual(self.counts(), (4, 1, 2))

    def test_old_placements_keep_channel_and_original_url(self):
        result, _ = self.reissue()
        for it in result["items"]:
            old = self.placement(it["old_placement_id"])
            self.assertEqual(old["channel"], it["channel"])
            self.assertEqual(old["url"], it["old_url"])
            self.assertEqual(old["credential_id"], self.old_cid)

    def test_unrelated_credential_placements_untouched(self):
        # 凭据 #3（同访谈同版本）下另有投放，换发 #1 -> #2 不应影响它
        cid3 = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")[0]["id"]
        other_pid = register_placement(
            self.conn, cid3, "别的渠道", "https://x/other")[0]["id"]
        self.reissue()
        row = self.placement(other_pid)
        self.assertEqual(row["status"], STATUS_ACTIVE)
        self.assertIsNone(row["withdrawn_at"])
        self.assertEqual(row["credential_id"], cid3)


if __name__ == "__main__":
    unittest.main()
