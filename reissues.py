"""凭据换发：渠道内容迁至新链接后，同一访谈在投投放的整批换发。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

换发（reissue_placements）：BEGIN IMMEDIATE -> 读取新旧凭据并复核
来源 -> 校验新旧凭据对应同一访谈、发布日期与媒介 -> 读取旧凭据的
全部在投记录，与提交清单逐一核对（恰好覆盖、不重不漏，每条只出现
一次，每条给出非空新投放链接）-> 复用台账登记的来源复核口径确认
新凭据与当前资料一致、整段可发布 -> 在同一写事务内把旧投放逐条
标记「已撤下」（原因记为凭据换发），按原渠道和新链接逐条登记新
投放，并保存逐条前后关联后提交；任一条件不满足即回滚整批：
旧记录不变，也不产生新投放与关联。

查阅（list_reissues / reissue_detail）：列出换发批次与逐条前后
关联；attach_links 为投放列表 / 详情补充该投放是否参与过换发、
换发批次与对端投放链接，换发后的投放仍以普通在投投放身份参与
授权变更保护（license_guard 只看 status='active'，无需特殊处理）。
"""
import placements

# 换发失败的原因码
REASON_SHAPE = "shape"          # 请求格式问题（HTTP 400）
REASON_SAME = "same_credential"  # 新旧凭据相同（HTTP 400）
REASON_MISMATCH = "mismatch"    # 新旧凭据不对应同一访谈 / 日期 / 媒介（HTTP 400）
REASON_NO_ACTIVE = "no_active"  # 旧凭据没有在投记录（HTTP 409）
REASON_MISSING_ENTRY = "missing_entry"    # 清单引用了不属于旧凭据在投集合的记录（HTTP 409）
REASON_NEW_INVALID = "new_invalid"        # 新凭据来源复核未通过（HTTP 409）

WITHDRAW_REASON = "凭据换发：渠道内容迁至新链接"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------- 换发

def _parse_items(raw_items):
    """解析并校验清单形状，返回 ({placement_id: new_url}, 错误响应体)。

    形状错误（HTTP 400）：清单不是非空列表、条目不是对象、
    placement_id 不是整数、new_url 为空、同一 placement_id 重复。
    """
    if not isinstance(raw_items, list) or not raw_items:
        return None, {"error": "换发清单不能为空，须逐条列出旧凭据的每条在投投放"}
    parsed = {}
    for idx, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            return None, {"error": f"第 {idx} 条换发记录格式错误"}
        placement_id = item.get("placement_id")
        if not _is_int(placement_id):
            return None, {"error": f"第 {idx} 条换发记录的投放 ID 必须为整数"}
        new_url = str(item.get("new_url") or "").strip()
        if not new_url:
            return None, {"error": f"第 {idx} 条换发记录的新投放链接不能为空"}
        if placement_id in parsed:
            return None, {
                "reason": REASON_SHAPE,
                "error": f"旧投放 #{placement_id} 在清单中重复出现，每条只能出现一次",
            }
        parsed[placement_id] = new_url
    return parsed, None


def _new_credential_problem(review):
    """新凭据复核：必须与当前资料一致、整段可发布；否则返回错误响应体。"""
    base = {
        "new_credential_id": review["id"],
        "interview_title": review["interview_title"],
        "reissued": False,
    }
    if review["status"] == placements.credentials.STATUS_MISSING:
        return {**base, "reason": REASON_NEW_INVALID,
                "error": "新凭据来源缺失：原访谈已删除，不能办理换发"}
    if review["changes"]:
        return {**base, "reason": REASON_NEW_INVALID,
                "changes": review["changes"],
                "error": "新凭据与当前资料不一致（"
                         + "、".join(review["changes"])
                         + "有变化），不能办理换发"}
    blocked = review["current"]["blocked"]
    if blocked:
        return {**base, "reason": REASON_NEW_INVALID,
                "blocked": blocked,
                "error": f"新凭据当前仍有 {len(blocked)} 个不可发布区间，不能办理换发"}
    return None


def reissue_placements(conn, old_credential_id, new_credential_id, raw_items):
    """在同一 SQLite 写事务中办理整批凭据换发。

    参数:
        old_credential_id: 旧凭据 ID（须有在投投放记录）。
        new_credential_id: 新凭据 ID（与旧凭据不同，对应同一访谈、
            发布日期与媒介，且与当前资料一致、整段可发布）。
        raw_items: [{"placement_id": 旧在投投放 ID, "new_url": 新链接}]，
            须恰好覆盖旧凭据的全部在投记录，每条只出现一次。

    返回 (result, status)：
        201 —— 换发完成：旧投放全部已撤下，新投放按原渠道与新链接登记，
               逐条关联已保存（result 含批次、明细与时间）；
        400 —— 请求形状问题 / 新旧凭据相同 / 新旧凭据不对应；
        404 —— 旧凭据或新凭据不存在；
        409 —— 旧凭据无在投记录 / 清单与在投集合不符（缺漏或多余）/
               新凭据来源复核未通过。整批回滚，旧记录不变，
               也不产生新投放。
    """
    if not _is_int(old_credential_id) or not _is_int(new_credential_id):
        return {"error": "凭据 ID 必须为整数"}, 400
    parsed_items, shape_problem = _parse_items(raw_items)
    if shape_problem is not None:
        return shape_problem, 400

    conn.execute("BEGIN IMMEDIATE")
    try:
        old_review = placements._review_credential(conn, old_credential_id)
        if old_review is None:
            conn.rollback()
            return {"error": "旧凭据不存在"}, 404
        new_review = placements._review_credential(conn, new_credential_id)
        if new_review is None:
            conn.rollback()
            return {"error": "新凭据不存在"}, 404
        if old_credential_id == new_credential_id:
            conn.rollback()
            return {
                "reason": REASON_SAME,
                "error": "新旧凭据必须不同，不能为同一凭据办理换发",
            }, 400
        for field, label in (("interview_id", "访谈"),
                             ("publication_date", "发布日期"),
                             ("medium", "媒介")):
            if old_review[field] != new_review[field]:
                conn.rollback()
                labels = {"interview_id": "同一访谈",
                          "publication_date": "同一发布日期",
                          "medium": "同一媒介"}
                return {
                    "reason": REASON_MISMATCH,
                    "error": f"新旧凭据必须对应{labels[field]}，"
                             f"但{label}不一致，不能办理换发",
                }, 400

        active = conn.execute(
            "SELECT * FROM channel_placements"
            " WHERE credential_id = ? AND status = 'active' ORDER BY id",
            (old_credential_id,)).fetchall()
        if not active:
            conn.rollback()
            return {
                "old_credential_id": old_credential_id,
                "reissued": False,
                "reason": REASON_NO_ACTIVE,
                "error": "旧凭据没有在投投放记录，无需换发",
            }, 409
        active_ids = {row["id"] for row in active}
        submitted_ids = set(parsed_items)
        missing = sorted(active_ids - submitted_ids)   # 清单缺漏
        extra = sorted(submitted_ids - active_ids)     # 清单多余 / 已撤下 / 不存在
        if missing or extra:
            conn.rollback()
            return {
                "old_credential_id": old_credential_id,
                "new_credential_id": new_credential_id,
                "reissued": False,
                "reason": REASON_MISSING_ENTRY,
                "missing_placement_ids": missing,
                "unexpected_placement_ids": extra,
                "active_placement_ids": sorted(active_ids),
                "error": (
                    "换发清单必须恰好覆盖旧凭据的全部在投记录"
                    + (f"（缺漏：{missing}）" if missing else "")
                    + (f"（不属于在投集合：{extra}）" if extra else "")
                    + "，整批失败，未改动任何记录"),
            }, 409

        problem = _new_credential_problem(new_review)
        if problem is not None:
            conn.rollback()  # 新凭据失效：整批失败，不留任何记录
            return problem, 409

        batch = conn.execute(
            "INSERT INTO placement_reissues (old_credential_id,"
            " new_credential_id) VALUES (?, ?)",
            (old_credential_id, new_credential_id))
        reissue_id = batch.lastrowid

        links = []
        for row in active:
            new_url = parsed_items[row["id"]]
            channel = row["channel"]
            # 旧投放标记已撤下：记录撤下时间与换发原因（凭据不改动）
            conn.execute(
                "UPDATE channel_placements SET status = 'withdrawn',"
                " withdraw_reason = ?, withdrawn_at = datetime('now')"
                " WHERE id = ?",
                (WITHDRAW_REASON, row["id"]))
            # 按原渠道和新链接登记新投放（新凭据已在本事务复核通过）
            cur = conn.execute(
                "INSERT INTO channel_placements (credential_id, channel, url)"
                " VALUES (?, ?, ?)",
                (new_credential_id, channel, new_url))
            new_placement_id = cur.lastrowid
            conn.execute(
                "INSERT INTO placement_reissue_items (reissue_id,"
                " old_placement_id, new_placement_id, channel, old_url, new_url)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (reissue_id, row["id"], new_placement_id,
                 channel, row["url"], new_url))
            links.append({
                "old_placement_id": row["id"],
                "new_placement_id": new_placement_id,
                "channel": channel,
                "old_url": row["url"],
                "new_url": new_url,
            })
        conn.commit()
        created_at = conn.execute(
            "SELECT created_at FROM placement_reissues WHERE id = ?",
            (reissue_id,)).fetchone()["created_at"]
    except Exception:
        conn.rollback()
        raise
    return {
        "id": reissue_id,
        "reissued": True,
        "old_credential_id": old_credential_id,
        "new_credential_id": new_credential_id,
        "interview_id": new_review["interview_id"],
        "interview_title": new_review["interview_title"],
        "medium": new_review["medium"],
        "publication_date": new_review["publication_date"],
        "created_at": created_at,
        "count": len(links),
        "links": links,
    }, 201


# ---------------------------------------------------------------- 查阅

def _batch_dict(conn, row, with_links=True):
    """换发批次视图；with_links 时附带逐条前后关联。"""
    item = {
        "id": row["id"],
        "old_credential_id": row["old_credential_id"],
        "new_credential_id": row["new_credential_id"],
        "created_at": row["created_at"],
    }
    if with_links:
        items = conn.execute(
            "SELECT * FROM placement_reissue_items WHERE reissue_id = ?"
            " ORDER BY id", (row["id"],)).fetchall()
        item["count"] = len(items)
        item["links"] = [{
            "old_placement_id": it["old_placement_id"],
            "new_placement_id": it["new_placement_id"],
            "channel": it["channel"],
            "old_url": it["old_url"],
            "new_url": it["new_url"],
        } for it in items]
    return item


def list_reissues(conn):
    """列出全部换发批次（含逐条前后关联），按批次编号排序。"""
    conn.execute("BEGIN")
    try:
        rows = conn.execute(
            "SELECT * FROM placement_reissues ORDER BY id").fetchall()
        items = [_batch_dict(conn, row) for row in rows]
        conn.rollback()  # 只读事务：释放快照
    except Exception:
        conn.rollback()
        raise
    return items


def reissue_detail(conn, reissue_id):
    """查阅单个换发批次（含新旧凭据要点与逐条前后关联）。

    返回批次详情；批次不存在时返回 None。
    """
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT * FROM placement_reissues WHERE id = ?",
            (reissue_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        detail = _batch_dict(conn, row)
        old_cred = placements._get_credential(conn, row["old_credential_id"])
        new_cred = placements._get_credential(conn, row["new_credential_id"])
        detail["old_credential"] = None if old_cred is None else {
            "id": old_cred["id"],
            "interview_id": old_cred["interview_id"],
            "interview_title": old_cred["interview_title"],
            "medium": old_cred["medium"],
            "publication_date": old_cred["publication_date"],
        }
        detail["new_credential"] = None if new_cred is None else {
            "id": new_cred["id"],
            "interview_id": new_cred["interview_id"],
            "interview_title": new_cred["interview_title"],
            "medium": new_cred["medium"],
            "publication_date": new_cred["publication_date"],
        }
        conn.rollback()  # 只读事务：释放快照
    except Exception:
        conn.rollback()
        raise
    return detail


# ---------------------------------------------------------------- 投放关联补充

def _link_index(conn, placement_ids):
    """在调用方事务内查询这些投放参与的换发关联。

    返回 {placement_id: {"reissue_id", "role": old|new,
                         "other_placement_id", "other_url", "channel"}}。
    每条投放至多参与一次换发（UNIQUE 约束保证）。
    """
    if not placement_ids:
        return {}
    index = {}
    rows = conn.execute(
        "SELECT i.reissue_id, i.old_placement_id, i.new_placement_id,"
        " i.channel, i.old_url, i.new_url FROM placement_reissue_items i"
        " JOIN placement_reissues r ON r.id = i.reissue_id"
        " WHERE i.old_placement_id IN (%s)"
        " OR i.new_placement_id IN (%s) ORDER BY r.id"
        % (",".join("?" * len(placement_ids)),
           ",".join("?" * len(placement_ids))),
        tuple(placement_ids) * 2).fetchall()
    for row in rows:
        index[row["old_placement_id"]] = {
            "reissue_id": row["reissue_id"], "role": "old",
            "other_placement_id": row["new_placement_id"],
            "other_url": row["new_url"], "channel": row["channel"],
        }
        index[row["new_placement_id"]] = {
            "reissue_id": row["reissue_id"], "role": "new",
            "other_placement_id": row["old_placement_id"],
            "other_url": row["old_url"], "channel": row["channel"],
        }
    return index


def attach_links(conn, views):
    """为投放列表 / 详情视图（同一读事务内）补充换发关联。

    未参与换发的投放补充 reissue=None；参与过则给出换发批次、
    自身角色（old=换发时撤下的旧投放 / new=换发登记的新投放）
    与对端投放 ID、链接及渠道。
    """
    index = _link_index(conn, [v["id"] for v in views])
    for view in views:
        view["reissue"] = index.get(view["id"])
    return views
