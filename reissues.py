"""渠道内容迁移后的凭据换发：同一写事务内整批撤旧、登新并保存关联。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

换发（reissue_placements）：提交旧凭据 ID、新凭据 ID，以及旧凭据
每条在投记录对应的新投放链接。BEGIN IMMEDIATE 后在同一写事务中：

1. 新旧凭据都必须存在且不是同一条；二者必须对应同一访谈、发布日期
   与媒介；新凭据还须与当前资料一致且整段可发布（同登记投放口径）；
2. 旧凭据必须有「投放中」记录，提交清单必须**恰好覆盖**其全部在投
   记录 —— 每条只出现一次（缺漏 / 重复 / 夹带他人在投记录均失败）；
3. 校验通过才把旧投放逐条标记「已撤下」（撤下原因注明凭据换发与
   新链接），按原渠道与新链接登记新投放，并逐条保存前后关联与
   批次编号；
4. 任一条件不满足即回滚整批 —— 旧记录不变，也不产生新投放。

换发后的新投放仍是普通「投放中」记录，继续参与授权变更提交前
复核（license_guard）；历史（旧投放、凭据、关联）全部保留。

查阅（list_reissues / placement_link）：换发历史与投放前后关联
均在一致的读取快照（同一事务）中给出；关联随投放列表 / 详情展示。
"""
import credentials
import placements

# 撤下原因前缀（旧投放撤下时注明凭据换发去向）
WITHDRAW_REASON_PREFIX = "凭据换发：内容已迁至 "

FETCH_CREDENTIAL = "SELECT * FROM release_credentials WHERE id = ?"

# 批次列表（含逐条关联与新旧投放当时信息；状态为台账当前状态）
LIST_BATCHES = """
SELECT b.id AS batch_id, b.created_at AS batch_created_at,
       b.old_credential_id, b.new_credential_id,
       c.interview_id, c.interview_title, c.publication_date, c.medium,
       r.id AS reissue_id, r.old_placement_id, r.new_placement_id,
       po.channel AS old_channel, po.url AS old_url,
       po.status AS old_status, po.withdrawn_at AS old_withdrawn_at,
       pn.channel AS new_channel, pn.url AS new_url,
       pn.status AS new_status
FROM placement_reissue_batches b
JOIN release_credentials c ON c.id = b.new_credential_id
JOIN placement_reissues r ON r.batch_id = b.id
JOIN channel_placements po ON po.id = r.old_placement_id
JOIN channel_placements pn ON pn.id = r.new_placement_id
ORDER BY b.id, r.id
"""

# 单条投放的前后关联（作为旧投放被换出 / 作为新投放换入）
LINK_AS_OLD = """
SELECT r.batch_id, r.old_placement_id, r.new_placement_id,
       r.old_credential_id, r.new_credential_id,
       p.channel, p.url, p.status, c.interview_title
FROM placement_reissues r
JOIN channel_placements p ON p.id = r.new_placement_id
JOIN release_credentials c ON c.id = r.new_credential_id
WHERE r.old_placement_id = ?
"""

LINK_AS_NEW = """
SELECT r.batch_id, r.old_placement_id, r.new_placement_id,
       r.old_credential_id, r.new_credential_id,
       p.channel, p.url, p.status, c.interview_title
FROM placement_reissues r
JOIN channel_placements p ON p.id = r.old_placement_id
JOIN release_credentials c ON c.id = r.old_credential_id
WHERE r.new_placement_id = ?
"""


def _credential(conn, credential_id):
    row = conn.execute(FETCH_CREDENTIAL, (credential_id,)).fetchone()
    return credentials._credential_dict(row) if row is not None else None


def _parse_items(raw):
    """校验换发清单形状：[{placement_id, url}]。

    合法返回 [(placement_id, url), ...]（保持提交顺序）；
    非法返回 (None, 错误消息)：必须为非空列表，每条 placement_id
    须为正整数、url 须为非空字符串，且 placement_id 不得重复。
    """
    if not isinstance(raw, list) or not raw:
        return None, "换发清单必须为非空列表，每条含在投投放 ID 与新投放链接"
    items = []
    seen = set()
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "换发清单每条必须是对象"
        pid = entry.get("placement_id")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None, "换发清单每条的 placement_id 必须为正整数"
        raw_url = entry.get("url")
        if not isinstance(raw_url, str):
            return None, "换发清单每条的新投放链接必须为字符串"
        url = raw_url.strip()
        if not url:
            return None, "换发清单每条的新投放链接不能为空"
        if pid in seen:
            return None, f"换发清单中投放 #{pid} 重复出现，每条在投记录只能出现一次"
        seen.add(pid)
        items.append((pid, url))
    return items, None


def _new_credential_problem(review):
    """新凭据复核（与登记投放同口径）：与当前资料一致且整段可发布。

    不能换入时返回原因响应体（reason 加 new_ 前缀）；可换入返回 None。
    """
    base = {"new_credential_id": review["id"]}
    if review["status"] == credentials.STATUS_MISSING:
        return {**base,
                "reason": "new_missing",
                "error": "新凭据来源缺失：原访谈已删除，不能换发"}
    if review["changes"]:
        return {**base,
                "reason": "new_changed",
                "changes": review["changes"],
                "error": "新凭据与当前资料不一致（"
                         + "、".join(review["changes"])
                         + "有变化），需处理后才能换发"}
    blocked = review["current"]["blocked"]
    if blocked:
        return {**base,
                "reason": "new_blocked",
                "blocked": blocked,
                "error": f"新凭据当前仍有 {len(blocked)} 个不可发布区间，"
                         "不能换发"}
    return None


def _mismatch_result(old, new, message):
    return {
        "reissued": False,
        "old_credential_id": old["id"],
        "new_credential_id": new["id"],
        "reason": "mismatch",
        "error": message,
    }, 409


def reissue_placements(conn, old_credential_id, new_credential_id, raw_items):
    """在同一 SQLite 写事务中为同一访谈的在投投放整批换发凭据。

    参数:
        old_credential_id / new_credential_id: 旧 / 新凭据主键
        raw_items: [{placement_id, url}] —— 旧凭据每条在投记录的
                   新投放链接（须恰好覆盖全部在投记录，每条一次）。

    返回 (result, status)：
        201 —— 换发完成，result 含批次编号、逐条前后关联与新投放；
        400 —— 清单形状非法（非列表 / 字段缺失 / placement_id 重复）；
        404 —— 旧或新凭据不存在；
        409 —— 新旧凭据相同、不同访谈 / 发布日期 / 媒介、新凭据失效、
               旧凭据无在投记录或清单未恰好覆盖其在投集合
               （整批回滚，旧记录不变，也不产生新投放）。
    """
    items, problem = _parse_items(raw_items)
    if problem:
        return {"reissued": False, "error": problem}, 400
    conn.execute("BEGIN IMMEDIATE")
    try:
        old = _credential(conn, old_credential_id)
        if old is None:
            conn.rollback()
            return {"error": "旧凭据不存在"}, 404
        new = _credential(conn, new_credential_id)
        if new is None:
            conn.rollback()
            return {"error": "新凭据不存在"}, 404

        # 新旧凭据必须不同，且对应同一访谈、发布日期与媒介
        if old["id"] == new["id"]:
            conn.rollback()
            return _mismatch_result(old, new, "新旧凭据必须不同")
        if new["interview_id"] != old["interview_id"]:
            conn.rollback()
            return _mismatch_result(
                old, new, "新旧凭据必须对应同一访谈")
        if new["publication_date"] != old["publication_date"]:
            conn.rollback()
            return _mismatch_result(
                old, new, "新旧凭据必须对应同一发布日期")
        if new["medium"] != old["medium"]:
            conn.rollback()
            return _mismatch_result(
                old, new, "新旧凭据必须对应同一媒介")

        # 新凭据须与当前资料一致、整段可发布（同登记投放口径）
        new_review = credentials._review(conn, new)
        new_problem = _new_credential_problem(new_review)
        if new_problem is not None:
            conn.rollback()
            return {"reissued": False,
                    "old_credential_id": old["id"],
                    **new_problem}, 409

        # 旧凭据的全部在投记录
        active = conn.execute(
            "SELECT * FROM channel_placements WHERE credential_id = ?"
            " AND status = 'active' ORDER BY id",
            (old["id"],)).fetchall()
        if not active:
            conn.rollback()
            return {
                "reissued": False,
                "old_credential_id": old["id"],
                "new_credential_id": new["id"],
                "reason": "no_active",
                "error": "旧凭据没有在投记录，无需换发",
            }, 409
        active_map = {row["id"]: row for row in active}

        # 清单须恰好覆盖旧凭据的全部在投记录，每条只出现一次
        submitted = [pid for pid, _ in items]
        submitted_set = set(submitted)
        missing_ids = sorted(set(active_map) - submitted_set)
        extra_ids = sorted(submitted_set - set(active_map))
        if missing_ids or extra_ids:
            conn.rollback()
            return {
                "reissued": False,
                "old_credential_id": old["id"],
                "new_credential_id": new["id"],
                "reason": "placements_mismatch",
                "missing_placement_ids": missing_ids,
                "extra_placement_ids": extra_ids,
                "error": "换发清单必须恰好覆盖旧凭据的全部在投记录"
                         "（缺漏或重复均整批失败）",
            }, 409

        # 全部校验通过：登记批次 -> 逐条撤旧、登新、保存关联
        batch_cur = conn.execute(
            "INSERT INTO placement_reissue_batches"
            " (old_credential_id, new_credential_id) VALUES (?, ?)",
            (old["id"], new["id"]))
        batch_id = batch_cur.lastrowid
        reissued_items = []
        for pid, new_url in items:
            old_row = active_map[pid]
            reason = WITHDRAW_REASON_PREFIX + new_url
            conn.execute(
                "UPDATE channel_placements SET status = 'withdrawn',"
                " withdraw_reason = ?, withdrawn_at = datetime('now')"
                " WHERE id = ?",
                (reason, pid))
            withdrawn_at = conn.execute(
                "SELECT withdrawn_at FROM channel_placements WHERE id = ?",
                (pid,)).fetchone()["withdrawn_at"]
            new_cur = conn.execute(
                "INSERT INTO channel_placements (credential_id, channel, url)"
                " VALUES (?, ?, ?)",
                (new["id"], old_row["channel"], new_url))
            new_pid = new_cur.lastrowid
            conn.execute(
                "INSERT INTO placement_reissues (batch_id, old_placement_id,"
                " new_placement_id, old_credential_id, new_credential_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (batch_id, pid, new_pid, old["id"], new["id"]))
            reissued_items.append({
                "old_placement_id": pid,
                "new_placement_id": new_pid,
                "channel": old_row["channel"],
                "old_url": old_row["url"],
                "new_url": new_url,
                "old_status": placements.STATUS_WITHDRAWN,
                "new_status": placements.STATUS_ACTIVE,
                "old_withdrawn_at": withdrawn_at,
            })
        batch = conn.execute(
            "SELECT created_at FROM placement_reissue_batches WHERE id = ?",
            (batch_id,)).fetchone()
        rel_rows = conn.execute(
            "SELECT id, old_placement_id FROM placement_reissues"
            " WHERE batch_id = ? ORDER BY id", (batch_id,)).fetchall()
        rel_id_of = {r["old_placement_id"]: r["id"] for r in rel_rows}
        for it in reissued_items:
            it["reissue_id"] = rel_id_of[it["old_placement_id"]]
        conn.commit()
        created_at = batch["created_at"]
    except Exception:
        conn.rollback()
        raise
    return {
        "reissued": True,
        "batch_id": batch_id,
        "old_credential_id": old["id"],
        "new_credential_id": new["id"],
        "interview_id": new["interview_id"],
        "interview_title": new["interview_title"],
        "publication_date": new["publication_date"],
        "medium": new["medium"],
        "created_at": created_at,
        "count": len(reissued_items),
        "items": reissued_items,
    }, 201


# ---------------------------------------------------------------- 查阅

def _batch_view(conn):
    """在调用方只读事务内汇总全部换发批次（按批次、条次排序）。"""
    batches = {}
    order = []
    for row in conn.execute(LIST_BATCHES).fetchall():
        bid = row["batch_id"]
        if bid not in batches:
            order.append(bid)
            batches[bid] = {
                "id": bid,
                "created_at": row["batch_created_at"],
                "old_credential_id": row["old_credential_id"],
                "new_credential_id": row["new_credential_id"],
                "interview_id": row["interview_id"],
                "interview_title": row["interview_title"],
                "publication_date": row["publication_date"],
                "medium": row["medium"],
                "items": [],
            }
        batches[bid]["items"].append({
            "reissue_id": row["reissue_id"],
            "old_placement_id": row["old_placement_id"],
            "new_placement_id": row["new_placement_id"],
            "old_channel": row["old_channel"],
            "old_url": row["old_url"],
            "old_status": row["old_status"],
            "old_withdrawn_at": row["old_withdrawn_at"],
            "new_channel": row["new_channel"],
            "new_url": row["new_url"],
            "new_status": row["new_status"],
        })
    return [batches[bid] for bid in order]


def list_reissues(conn):
    """列出全部凭据换发批次（一致读取快照；含逐条前后关联）。"""
    conn.execute("BEGIN")
    try:
        items = _batch_view(conn)
        conn.rollback()  # 只读事务：释放快照
    except Exception:
        conn.rollback()
        raise
    return items


def placement_link(conn, placement_id):
    """查阅单条投放的换发前后关联（调用方事务内执行）。

    返回 {"as_old": {...}|None, "as_new": {...}|None}：
    该投放作为旧投放被换出时 as_old 指向新投放；
    作为新投放换入时 as_new 指向旧投放；均无关联时两个字段皆为 None。
    """
    def fetch(sql, as_old):
        row = conn.execute(sql, (placement_id,)).fetchone()
        if row is None:
            return None
        other_id = (row["new_placement_id"] if as_old
                    else row["old_placement_id"])
        return {
            "batch_id": row["batch_id"],
            "other_placement_id": other_id,
            "old_placement_id": row["old_placement_id"],
            "new_placement_id": row["new_placement_id"],
            "old_credential_id": row["old_credential_id"],
            "new_credential_id": row["new_credential_id"],
            # 对端投放当时登记的渠道、链接与当前状态
            "channel": row["channel"],
            "url": row["url"],
            "status": row["status"],
            "interview_title": row["interview_title"],
        }
    return {"as_old": fetch(LINK_AS_OLD, True),
            "as_new": fetch(LINK_AS_NEW, False)}
