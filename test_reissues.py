"""凭据换发事务编排（reissues 模块）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
同一写事务中把旧在投投放标记已撤下、按原渠道与新链接登记新投放
并保存逐条关联；新旧凭据必须不同且对应同一访谈、发布日期与媒介；
新凭据须与当前资料一致、整段可发布；清单必须恰好覆盖旧凭据的
全部在投记录、每条只出现一次。任一条件不满足整批回滚：
旧记录不变，也不产生新投放。换发后的投放继续参与授权变更保护。
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
)
from reissues import (
    REASON_MISMATCH,
    REASON_MISSING_ENTRY,
    REASON_NEW_INVALID,
    REASON_NO_ACTIVE,
    REASON_SAME,
    WITHDRAW_REASON,
    attach_links,
    list_reissues,
    reissue_detail,
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
CREATE TABLE placement_reissues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    new_credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (old_credential_id <> new_credential_id)
);
CREATE TABLE placement_reissue_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reissue_id INTEGER NOT NULL REFERENCES placement_reissues(id),
    old_placement_id INTEGER NOT NULL UNIQUE
        REFERENCES channel_placements(id),
    new_placement_id INTEGER NOT NULL UNIQUE
        REFERENCES channel_placements(id),
    channel TEXT NOT NULL,
    old_url TEXT NOT NULL,
    new_url TEXT NOT NULL
);
CREATE TRIGGER placement_reissues_no_delete
BEFORE DELETE ON placement_reissues
BEGIN SELECT RAISE(ABORT, '换发批次不可删除'); END;
CREATE TRIGGER placement_reissues_no_update
BEFORE UPDATE ON placement_reissues
BEGIN SELECT RAISE(ABORT, '换发批次不可修改'); END;
CREATE TRIGGER placement_reissue_items_no_delete
BEFORE DELETE ON placement_reissue_items
BEGIN SELECT RAISE(ABORT, '换发关联不可删除'); END;
CREATE TRIGGER placement_reissue_items_no_update
BEFORE UPDATE ON placement_reissue_items
BEGIN SELECT RAISE(ABORT, '换发关联不可修改'); END;
"""

PUB_DATE = "2026-10-04"
OLD_URL_A = "https://old.example.com/a"
OLD_URL_B = "https://old.example.com/b"
NEW_URL_A = "https://new.example.com/a"
NEW_URL_B = "https://new.example.com/b"


class ReissueTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频。
    已封存两张对应同一访谈、日期、媒介的凭据 c1/c2，
    c1 有两条在投投放（不同渠道、不同链接）。"""

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
        self.sp = 1
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 1, 0, 600)")
        c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                  " end_seconds, allow_media, deny_media, embargo_until,"
                  " note) VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '')")
        c.commit()
        r1, st = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")
        assert st == 201
        self.c1 = r1["id"]
        self.p1 = register_placement(
            self.conn, self.c1, "官网", OLD_URL_A)[0]["id"]
        self.p2 = register_placement(
            self.conn, self.c1, "播客", OLD_URL_B)[0]["id"]
        r2, st = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")
        assert st == 201
        self.c2 = r2["id"]

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def items(self, *, drop=(), extra=()):
        """默认清单覆盖 p1/p2；drop 中的旧投放从清单删除，
        extra 追加 (placement_id, new_url) 条目。"""
        data = [(self.p1, NEW_URL_A), (self.p2, NEW_URL_B)]
        data = [(pid, url) for pid, url in data if pid not in drop]
        data.extend(extra)
        return [{"placement_id": pid, "new_url": url}
                for pid, url in data]

    def reissue(self, old=None, new=None, items=None):
        return reissue_placements(
            self.conn, old or self.c1, new or self.c2,
            self.items() if items is None else items)
    def placement_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM channel_placements").fetchone()["n"]

    def reissue_counts(self):
        return (self.conn.execute(
                    "SELECT COUNT(*) AS n FROM placement_reissues")
                .fetchone()["n"],
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM placement_reissue_items")
                .fetchone()["n"])

    def get_placement(self, pid):
        return self.conn.execute(
            "SELECT * FROM channel_placements WHERE id = ?",
            (pid,)).fetchone()

    # ---------------------------------------------------------- 成功

    def test_success_withdraws_old_and_registers_new_in_one_batch(self):
        result, status = self.reissue()
        self.assertEqual(status, 201)
        self.assertTrue(result["reissued"])
        self.assertEqual(result["old_credential_id"], self.c1)
        self.assertEqual(result["new_credential_id"], self.c2)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["created_at"])
        # 旧投放全部已撤下（原因与撤下时间保留）
        for pid in (self.p1, self.p2):
            row = self.get_placement(pid)
            self.assertEqual(row["status"], STATUS_WITHDRAWN)
            self.assertEqual(row["withdraw_reason"], WITHDRAW_REASON)
            self.assertIsNotNone(row["withdrawn_at"])
        # 新投放按原渠道与新链接登记，关联新凭据，投放中
        by_old = {l["old_placement_id"]: l for l in result["links"]}
        self.assertEqual(set(by_old), {self.p1, self.p2})
        link1 = by_old[self.p1]
        self.assertEqual(link1["channel"], "官网")
        self.assertEqual(link1["old_url"], OLD_URL_A)
        self.assertEqual(link1["new_url"], NEW_URL_A)
        new1 = self.get_placement(link1["new_placement_id"])
        self.assertEqual(new1["credential_id"], self.c2)
        self.assertEqual(new1["channel"], "官网")
        self.assertEqual(new1["url"], NEW_URL_A)
        self.assertEqual(new1["status"], STATUS_ACTIVE)
        self.assertIsNone(new1["withdrawn_at"])
        link2 = by_old[self.p2]
        new2 = self.get_placement(link2["new_placement_id"])
        self.assertEqual((new2["channel"], new2["url"]), ("播客", NEW_URL_B))
        # 台账新增恰好两条
        self.assertEqual(self.placement_count(), 4)
        rb, ib = self.reissue_counts()
        self.assertEqual((rb, ib), (1, 2))

    def test_success_links_queryable_from_list_and_detail(self):
        result, _ = self.reissue()
        links = {l["old_placement_id"]: l for l in result["links"]}
        n1 = links[self.p1]["new_placement_id"]
        n2 = links[self.p2]["new_placement_id"]
        # 换发批次列表与详情
        batches = list_reissues(self.conn)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["count"], 2)
        detail = reissue_detail(self.conn, result["id"])
        self.assertEqual(detail["old_credential"]["id"], self.c1)
        self.assertEqual(detail["new_credential"]["id"], self.c2)
        self.assertEqual(detail["old_credential"]["publication_date"], PUB_DATE)
        self.assertEqual(detail["new_credential"]["medium"], "audio")
        # attach_links：旧投放 role=old，新投放 role=new，互相指向
        views = attach_links_in_tx(self.conn, [self.p1, self.p2, n1, n2])
        old_link = views[self.p1]["reissue"]
        new_link = views[n1]["reissue"]
        self.assertEqual(old_link["role"], "old")
        self.assertEqual(old_link["other_placement_id"], n1)
        self.assertEqual(old_link["other_url"], NEW_URL_A)
        self.assertEqual(old_link["reissue_id"], result["id"])
        self.assertEqual(new_link["role"], "new")
        self.assertEqual(new_link["other_placement_id"], self.p1)
        self.assertEqual(new_link["other_url"], OLD_URL_A)
        # 未参与换发的投放 reissue 为 None
        p3 = register_placement(self.conn, self.c2, "第三个渠道",
                                "https://x.example")[0]["id"]
        self.assertIsNone(attach_links_in_tx(self.conn, [p3])
                          [p3]["reissue"])

    def test_reissued_active_placements_still_guarded_by_license_change(self):
        # 换发后新投放（c2）在投；把授权改为拒绝音频应被提交前复核拦截
        self.reissue()
        result, status = update_license(self.conn, 1, {
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": [], "deny_media": ["audio"],
            "embargo_until": None, "note": ""})
        self.assertEqual(status, 409)
        affected = {a["placement_id"] for a in result["affected_placements"]}
        # 受影响的是换发后的新投放；已撤下的旧投放不阻拦
        views = {v["id"]: v for v in list_placements(self.conn)}
        new_active = [pid for pid, v in views.items()
                      if v["credential_id"] == self.c2 and v["status"] == "active"]
        self.assertTrue(new_active)
        self.assertTrue(set(new_active) <= affected)
        self.assertNotIn(self.p1, affected)
        self.assertNotIn(self.p2, affected)
        # 回滚后授权不变、新投放仍在投
        self.assertEqual(self.get_placement(new_active[0])["status"],
                         STATUS_ACTIVE)

    # ---------------------------------------------------------- 凭据校验

    def test_same_credential_rejected(self):
        result, status = self.reissue(new=self.c1)
        self.assertEqual(status, 400)
        self.assertEqual(result["reason"], REASON_SAME)
        self._assert_unchanged()

    def test_unknown_old_or_new_credential_returns_404(self):
        result, status = reissue_placements(
            self.conn, 9999, self.c2, self.items())
        self.assertEqual(status, 404)
        result, status = reissue_placements(
            self.conn, self.c1, 9999, self.items())
        self.assertEqual(status, 404)
        self._assert_unchanged()

    def test_different_interview_rejected(self):
        self.conn.execute(
            "INSERT INTO interviews (title, duration_seconds)"
            " VALUES ('另一个访谈', 600)")
        self.conn.execute(
            "INSERT INTO speakers (interview_id, name) VALUES (2, '乙')")
        self.conn.execute(
            "INSERT INTO segments (interview_id, speaker_id,"
            " start_seconds, end_seconds) VALUES (2, 2, 0, 600)")
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (2, 0, 600, '[\"audio\"]', '[]', NULL, '')")
        self.conn.commit()
        other = create_credential(
            self.conn, interview_id=2,
            publication_date=PUB_DATE, medium="audio")[0]["id"]
        result, status = self.reissue(new=other)
        self.assertEqual(status, 400)
        self.assertEqual(result["reason"], REASON_MISMATCH)
        self._assert_unchanged()

    def test_different_date_or_medium_rejected(self):
        other_date = create_credential(
            self.conn, interview_id=1,
            publication_date="2027-01-01", medium="audio")[0]["id"]
        result, status = self.reissue(new=other_date)
        self.assertEqual(status, 400)
        self.assertEqual(result["reason"], REASON_MISMATCH)
        self._assert_unchanged()
        # 追加一条同时允许文字的授权（不改变既有音频资料）-> 可封存文字凭据
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\", \"text\"]', '[]', NULL, '')")
        self.conn.commit()
        other_medium = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="text")[0]["id"]
        result, status = self.reissue(new=other_medium)
        self.assertEqual(status, 400)
        self.assertEqual(result["reason"], REASON_MISMATCH)
        self._assert_unchanged()

    def test_new_credential_changed_materials_rejected(self):
        # 新凭据封存后追加授权：仍可发布但资料变化 -> 新凭据失效
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '补充')")
        self.conn.commit()
        result, status = self.reissue()
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], REASON_NEW_INVALID)
        self.assertEqual(result["changes"], ["授权"])
        self._assert_unchanged()

    def test_new_credential_blocked_rejected(self):
        self.conn.execute(
            "UPDATE licenses SET allow_media = '[]',"
            " deny_media = '[\"audio\"]' WHERE speaker_id = 1")
        self.conn.commit()
        result, status = self.reissue()
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], REASON_NEW_INVALID)
        # 资料变化优先说明（与台账登记口径一致）；整批失败、旧记录不变
        self._assert_unchanged()

    # ---------------------------------------------------------- 清单校验

    def test_old_credential_without_active_placements_rejected(self):
        # c2 从未登记投放
        result, status = self.reissue(old=self.c2, new=self.c1)
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], REASON_NO_ACTIVE)
        self._assert_unchanged()

    def test_missing_entry_rolls_back(self):
        items = [{"placement_id": self.p1, "new_url": NEW_URL_A}]
        result, status = self.reissue(items=items)
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], REASON_MISSING_ENTRY)
        self.assertEqual(result["missing_placement_ids"], [self.p2])
        self.assertEqual(result["unexpected_placement_ids"], [])
        self._assert_unchanged()

    def test_extra_entry_rolls_back(self):
        # c2 登记一条投放，作为「不属于 c1 在投集合」的多余记录
        extra = register_placement(
            self.conn, self.c2, "官网", "https://extra.example")[0]["id"]
        result, status = self.reissue(
            items=self.items(extra=[(extra, "https://z.example")]))
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], REASON_MISSING_ENTRY)
        self.assertEqual(result["missing_placement_ids"], [])
        self.assertEqual(result["unexpected_placement_ids"], [extra])
        self._assert_unchanged(count=3)

    def test_withdrawn_entry_is_outside_active_set(self):
        # 先撤下 p1，再尝试把它写进清单：它已不属于在投集合
        from placements import withdraw_placement
        withdraw_placement(self.conn, self.p1, "人工撤下")
        result, status = self.reissue()
        self.assertEqual(status, 409)
        self.assertEqual(result["reason"], REASON_MISSING_ENTRY)
        self.assertIn(self.p1, result["unexpected_placement_ids"])
        # c1 在投集合只剩 p2，而清单按原夹具含 p1、p2：p2 恰好覆盖
        self.assertEqual(result["missing_placement_ids"], [])
        # 人工撤下原因不被改写；换发未产生新投放
        self.assertEqual(self.get_placement(self.p1)["status"],
                         STATUS_WITHDRAWN)
        self.assertEqual(
            self.get_placement(self.p1)["withdraw_reason"], "人工撤下")
        self.assertEqual(self.get_placement(self.p2)["status"],
                         STATUS_ACTIVE)
        self.assertEqual(self.placement_count(), 2)
        self.assertEqual(self.reissue_counts(), (0, 0))

    def test_duplicate_entry_rejected(self):
        items = [
            {"placement_id": self.p1, "new_url": NEW_URL_A},
            {"placement_id": self.p1, "new_url": "https://dup.example"},
            {"placement_id": self.p2, "new_url": NEW_URL_B},
        ]
        result, status = self.reissue(items=items)
        self.assertEqual(status, 400)
        self._assert_unchanged()

    def test_bad_shape_rejected(self):
        bad_cases = (
            None, [], "x",
            [{"placement_id": self.p1}],           # 缺新链接
            [{"placement_id": "1",                 # ID 非整数
              "new_url": NEW_URL_A}],
            [{"placement_id": self.p1,             # 空白新链接
              "new_url": "   "}],
        )
        for bad in bad_cases:
            result, status = reissue_placements(
                self.conn, self.c1, self.c2, bad)
            self.assertEqual(status, 400, msg=bad)
        self._assert_unchanged()

    # ---------------------------------------------------------- 台账保护

    def test_reissue_records_are_append_only(self):
        result, _ = self.reissue()
        rid = result["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM placement_reissues")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE placement_reissues SET new_credential_id = ?",
                (self.c1,))
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM placement_reissue_items")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE placement_reissue_items SET new_url = 'x'")
        self.conn.rollback()
        rb, ib = self.reissue_counts()
        self.assertEqual((rb, ib), (1, 2))

    def test_detail_unknown_returns_none(self):
        self.assertIsNone(reissue_detail(self.conn, 9999))

    # ---------------------------------------------------------- 断言辅助

    def _assert_unchanged(self, count=2):
        """整批失败：旧记录不变（仍在投），不产生新投放与换发记录。"""
        for pid in (self.p1, self.p2):
            row = self.get_placement(pid)
            self.assertEqual(row["status"], STATUS_ACTIVE)
            self.assertIsNone(row["withdrawn_at"])
        self.assertEqual(self.placement_count(), count)
        self.assertEqual(self.reissue_counts(), (0, 0))


def attach_links_in_tx(conn, pids):
    """用 placement_review 的 enrich 钩子在各只读快照内取关联视图。"""
    views = [placement_review(conn, pid, enrich=attach_links)
             for pid in pids]
    return {v["id"]: v for v in views}


if __name__ == "__main__":
    unittest.main()
