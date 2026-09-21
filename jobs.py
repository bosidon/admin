# -*- coding: utf-8 -*-
"""任务队列 + 调度器（SQLite 持久化，重启不丢）

设计要点：
- 状态全在库里（jobs 表）：任务列表/日志/结果/排队位次都能查，进程重启不丢
- 出图任务全局唯一：部分唯一索引 uniq_running_images（一块 GPU 只能跑一个）
  注意：**status 始终保持 'running'**，细分阶段放 stage 列 —— 否则并发计数会漏数
- 调度器线程按 kind 起并发：images 固定 1；plan/rewrite/text 用 settings.job_llm_parallel（默认 2）
- 幂等：同 kind + 同文章 + 同参数已有排队/运行中的任务时，复用该任务（防重复点击/多标签）
- 启动自愈：recover_orphans() 把上次残留的 running/queued 标成 interrupted
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = str(BASE_DIR / "data" / "database.db")

STATUS_ACTIVE = ("queued", "running")
STATUS_DONE = ("done", "failed", "canceled", "interrupted")
LOG_MAX = 200
JSON_COLS = ("images", "plan_images", "quotes", "scenes", "result")

_KINDS_DEFAULT_LIMIT = {"images": 1, "plan": 2, "rewrite": 2, "text": 2}

# kind → 资源域兜底表。⚠️ 必须有：register() 只在 app 进程里跑过，
# 若从别的进程（脚本/定时任务）入队，self.domains 是空的 → 会错判成 llm 域，
# 导致「一块 GPU 串行」失效（实测出过：抠图和拼版同时在 GPU 上跑）。
# 注意 dict 在模块级、位于 DISPATCHER 定义之前，所以用函数内合并而不是直接引用。
_KIND_DOMAIN = {"images": "gpu", "cutout": "gpu", "edit": "gpu", "stitch": "gpu",
                "txt2img": "gpu",
                "plan": "llm", "rewrite": "llm", "text": "llm"}

KIND_LABEL = {"images": "生成配图", "plan": "生成方案", "rewrite": "重写条目", "text": "生成文案",
              "cutout": "素材抠图", "edit": "素材编辑", "stitch": "素材拼版", "txt2img": "文生图"}
STATUS_LABEL = {"queued": "排队中", "running": "进行中", "done": "已完成",
                "failed": "失败", "canceled": "已取消", "interrupted": "被中断"}
STAGE_LABEL = {"planning": "分析文案", "booting": "开机中", "ready": "等 ComfyUI",
               "waiting": "等空闲实例",
               "generating": "出图中", "": ""}


_LOCK = threading.Lock()          # 原子领取任务用


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA busy_timeout=8000")
    except Exception:
        pass
    return c


def init():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        article_id INTEGER,
        status TEXT NOT NULL,
        stage TEXT DEFAULT '',
        payload TEXT DEFAULT '{}',
        total INTEGER DEFAULT 0,
        done INTEGER DEFAULT 0,
        images TEXT DEFAULT '[]',
        plan_images TEXT DEFAULT '[]',
        quotes TEXT DEFAULT '[]',
        scenes TEXT DEFAULT '[]',
        style TEXT DEFAULT '',
        log TEXT DEFAULT '',
        result TEXT DEFAULT '',
        error TEXT DEFAULT '',
        owner TEXT DEFAULT '',
        want_cards INTEGER DEFAULT 0,
        want_scenes INTEGER DEFAULT 0,
        created_at TEXT, started_at TEXT, finished_at TEXT,
        domain TEXT DEFAULT 'llm',
        priority INTEGER DEFAULT 0
    );
    CREATE UNIQUE INDEX IF NOT EXISTS uniq_running_images
        ON jobs(kind) WHERE status = 'running' AND kind = 'images';
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_article ON jobs(article_id, created_at);
    """)
    # 轻量列迁移：CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，新增字段要显式 ALTER
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    for col, ddl in (("stage", "TEXT DEFAULT ''"), ("images", "TEXT DEFAULT '[]'"),
                     ("plan_images", "TEXT DEFAULT '[]'"), ("quotes", "TEXT DEFAULT '[]'"),
                     ("scenes", "TEXT DEFAULT '[]'"), ("style", "TEXT DEFAULT ''"),
                     ("result", "TEXT DEFAULT ''"), ("owner", "TEXT DEFAULT ''"),
                     ("want_cards", "INTEGER DEFAULT 0"), ("want_scenes", "INTEGER DEFAULT 0"),
                     ("started_at", "TEXT"), ("domain", "TEXT DEFAULT 'llm'"),
                     ("priority", "INTEGER DEFAULT 0"),
                     ("host", "TEXT DEFAULT ''"), ("owner_id", "TEXT DEFAULT ''"),
                     ("ready_at", "TEXT")):          # 实例/ComfyUI 就绪时刻（精确用时起点）
        if col not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (col, ddl))
    c.commit()
    c.close()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def set_ready(job_id):
    """实例/ComfyUI 就绪、真正开始干活的时间 → 精确用时起点（开机等待不计入）"""
    c = _conn()
    try:
        with _LOCK:
            c.execute("UPDATE jobs SET ready_at=? WHERE id=? AND IFNULL(ready_at,'')=''",
                      (_now(), job_id))
            c.commit()
    finally:
        c.close()


def gpu_active_count():
    """GPU 域「排队 + 运行中」任务数（空闲关机判定用）"""
    c = _conn()
    try:
        r = c.execute("SELECT COUNT(*) n FROM jobs WHERE domain='gpu' "
                      "AND status IN ('queued','running')").fetchone()
        return int((r["n"] if r else 0) or 0)
    finally:
        c.close()


def last_gpu_activity():
    """最后一次 GPU 任务结束时间（epoch 秒）；无记录返回 None"""
    c = _conn()
    try:
        r = c.execute("SELECT MAX(COALESCE(finished_at, started_at, created_at)) t "
                      "FROM jobs WHERE domain='gpu'").fetchone()
        t = (r["t"] if r else "") or ""
        return time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None
    finally:
        c.close()


def _row(r):
    if not r:
        return None
    d = dict(r)
    for k in ("payload",) + JSON_COLS:
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v) if v else ([] if k in JSON_COLS else {})
            except Exception:
                d[k] = [] if k in JSON_COLS else {}
    d["log_lines"] = (d.get("log") or "").split("\n") if d.get("log") else []
    d["kind_label"] = KIND_LABEL.get(d.get("kind"), d.get("kind") or "")
    d["status_label"] = STATUS_LABEL.get(d.get("status"), d.get("status") or "")
    d["stage_label"] = STAGE_LABEL.get(d.get("stage") or "", "")
    return d


# ---------------- 基础 CRUD ----------------
def create(kind, article_id=None, payload=None, total=0, owner="", domain="llm", priority=0,
           owner_id=""):
    jid = uuid.uuid4().hex[:12]
    c = _conn()
    c.execute("INSERT INTO jobs (id,kind,article_id,status,stage,payload,total,done,images,plan_images,"
              "quotes,scenes,style,log,result,error,owner,want_cards,want_scenes,created_at,"
              "started_at,finished_at,domain,priority,owner_id) "
              "VALUES (?,?,?,'queued','',?,?,0,'[]','[]','[]','[]','','','','',?,0,0,?,NULL,NULL,?,?,?)",
              (jid, kind, int(article_id) if article_id else None,
               json.dumps(payload or {}, ensure_ascii=False), int(total or 0), owner or "", _now(),
               domain or "llm", int(priority or 0), owner_id or ""))
    c.commit()
    c.close()
    return jid


def get(job_id):
    c = _conn()
    r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    c.close()
    return _row(r)


def find_active(kind, article_id, payload=None):
    c = _conn()
    rows = c.execute("SELECT * FROM jobs WHERE kind=? AND status IN ('queued','running')"
                     " AND article_id IS ? ORDER BY created_at", (kind, article_id)).fetchall()
    c.close()
    want = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    for r in rows:
        d = _row(r)
        if json.dumps(d.get("payload") or {}, ensure_ascii=False, sort_keys=True) == want:
            return d
    return None


def list_jobs(active=False, article_id=None, limit=20, status=None,
              state=None, owner_id=None, me_id=None):
    """state='active'（排队+进行中）/ 'done'（终态）；
       owner_id：只看该归属人；me_id：非管理员视角（活跃任务全员可见 + 终态只看自己）"""
    c = _conn()
    sql = "SELECT * FROM jobs WHERE 1=1"
    args = []
    if active or state == 'active':
        sql += " AND status IN ('queued','running')"
    elif state == 'done':
        sql += " AND status IN (%s)" % ",".join("'%s'" % x for x in STATUS_DONE)
    if article_id:
        sql += " AND article_id=?"
        args.append(int(article_id))
    if status:
        sql += " AND status=?"
        args.append(status)
    if owner_id:
        sql += " AND owner_id=?"
        args.append(str(owner_id))
    if me_id:
        sql += " AND (status IN ('queued','running') OR owner_id=?)"
        args.append(str(me_id))
    sql += (" ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,"
            " created_at DESC LIMIT ?")
    args.append(int(limit))
    rows = c.execute(sql, args).fetchall()
    c.close()
    out = []
    for r in rows:
        d = _row(r)
        if d["status"] == "queued":
            d["queue_pos"] = queue_pos(d["id"])
        out.append(d)
    return out


def update(job_id, **kw):
    if not kw:
        return
    sets, args = [], []
    for k, v in kw.items():
        sets.append("%s=?" % k)
        args.append(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
    args.append(job_id)
    c = _conn()
    c.execute("UPDATE jobs SET %s WHERE id=?" % ", ".join(sets), args)
    c.commit()
    c.close()


def log(job_id, msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    c = _conn()
    r = c.execute("SELECT log FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r:
        c.close()
        return
    lines = (r["log"] or "").split("\n") if r["log"] else []
    lines.append(line)
    if len(lines) > LOG_MAX:
        lines = lines[-LOG_MAX:]
    c.execute("UPDATE jobs SET log=? WHERE id=?", ("\n".join(lines), job_id))
    c.commit()
    c.close()


# ---------------- 队列操作 ----------------
def running_count(kind):
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM jobs WHERE kind=? AND status='running'", (kind,)).fetchone()[0]
    c.close()
    return int(n)


def queued_count(kind):
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM jobs WHERE kind=? AND status='queued'", (kind,)).fetchone()[0]
    c.close()
    return int(n)


def domain_limit(domain):
    """域的并发上限：llm → settings.llm_parallel（默认 2）；gpu → gpu_parallel（默认 1）；none → 不限"""
    if domain == "none":
        return 999
    key = {"llm": "llm_parallel", "gpu": "gpu_parallel"}.get(domain, "")
    dft = 2 if domain == "llm" else 1
    if not key:
        return 1
    try:
        c = _conn()
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        c.close()
        raw = str((r["value"] if r else "") or "").strip()
        if domain == "llm" and raw in ("0", "-1", "unlimited", "不限"):
            return 32                       # 填 0/不限 = 放开（32 并发已远超实际需要）
        return max(1, min(32, int(raw or dft)))
    except Exception:
        return dft


def running_domain(domain):
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM jobs WHERE status='running' AND domain=?", (domain,)).fetchone()[0]
    c.close()
    return n


def next_in_domain(domain):
    """同域内挑下一个该跑的：优先级高的先跑，同级按入队时间（交互式可插队）"""
    c = _conn()
    r = c.execute("SELECT * FROM jobs WHERE status='queued' AND domain=? "
                  "ORDER BY priority DESC, created_at ASC LIMIT 1", (domain,)).fetchone()
    c.close()
    return _row(r) if r else None


def claim_job(job_id):
    """原子领取指定任务（queued → running）"""
    c = _conn()
    with _LOCK:
        cur = c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                        (_now(), job_id))
        c.commit()
        n = cur.rowcount
    c.close()
    return bool(n)


class Gate:
    """同步 LLM 调用也要过的闸门（与队列共用同一并发上限）"""

    def __init__(self, limit_fn, name="llm", wait_max=180):
        self.limit_fn = limit_fn
        self.name = name
        self.wait_max = wait_max
        self._n = 0
        self._cv = threading.Condition()

    def __enter__(self):
        t0 = time.time()
        with self._cv:
            while self._n >= self.limit_fn():
                if time.time() - t0 > self.wait_max:      # 超时放行，避免卡死
                    break
                self._cv.wait(1)
            self._n += 1
        return self

    def __exit__(self, *a):
        with self._cv:
            self._n = max(0, self._n - 1)
            self._cv.notify()
        return False

    def busy(self):
        return self._n >= self.limit_fn()


LLM_GATE = Gate(lambda: domain_limit("llm"), "llm")


def queue_pos(job_id):
    """排在它前面还有几个同 kind 的排队任务（1 = 下一个就跑）"""
    c = _conn()
    r = c.execute("SELECT kind, status, created_at FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not r or r["status"] != "queued":
        c.close()
        return 0
    n = c.execute("SELECT COUNT(*) FROM jobs WHERE kind=? AND status='queued' AND created_at<?",
                  (r["kind"], r["created_at"])).fetchone()[0]
    c.close()
    return int(n) + 1


def cancel(job_id):
    """取消：排队中直接撤销；运行中打标记（执行体轮询后中断）"""
    j = get(job_id)
    if not j or j["status"] in STATUS_DONE:
        return False
    if j["status"] == "queued":
        update(job_id, status="canceled", finished_at=_now(), error="已取消（尚未开始）")
        log(job_id, "已取消（尚未开始）")
        return True
    update(job_id, error="取消中…")
    log(job_id, "收到取消请求，正在中断…")
    return True


def canceled(job_id):
    j = get(job_id)
    return bool(j) and (j["status"] == "canceled" or "取消中" in (j.get("error") or ""))


def recover_orphans():
    """启动自愈：上次进程残留的 running/queued → interrupted"""
    c = _conn()
    n = c.execute("UPDATE jobs SET status='interrupted', finished_at=?,"
                  " error=CASE WHEN error IS NULL OR error='' THEN '服务重启中断' ELSE error END,"
                  " log=CASE WHEN log IS NULL THEN '' ELSE log END || ?"
                  " WHERE status IN ('queued','running')",
                  (_now(), "\n[启动自检] 服务重启，任务被中断")).rowcount
    c.commit()
    c.close()
    return int(n)


# ---------------- 调度器 ----------------
class Dispatcher:
    """按 kind 起并发的调度线程；并发上限可动态读（改设置立即生效）"""

    def __init__(self):
        self.runners = {}          # kind → 执行体
        self.domains = {}          # kind → 资源域（llm / gpu / none）
        self._thread = None
        self._stop = False

    def register(self, kind, fn, domain="llm"):
        """注册任务类型：kind 只是标签，限流按 domain 走（域上限用 settings 实时读）"""
        self.runners[kind] = fn
        self.domains[kind] = domain or "llm"

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True, name="job-dispatcher")
        self._thread.start()

    def _loop(self):
        """按域调度：域内并发不超上限；域内挑「优先级高 + 入队早」的任务（交互式可插队）"""
        while not self._stop:
            try:
                for domain in sorted(set(self.domains.values())):
                    while running_domain(domain) < domain_limit(domain):
                        cand = next_in_domain(domain)
                        if not cand:
                            break
                        if not claim_job(cand["id"]):
                            continue
                        fn = self.runners.get(cand["kind"])
                        if not fn:
                            update(cand["id"], status="failed", error="未注册的任务类型 %s" % cand["kind"],
                                   finished_at=_now())
                            continue
                        threading.Thread(target=self._run, args=(cand["kind"], fn, cand),
                                         daemon=True,
                                         name="job-%s-%s" % (cand["kind"], cand["id"][:6])).start()
            except Exception:
                pass
            time.sleep(1.0)

    def _run(self, kind, fn, job):
        jid = job["id"]
        try:
            fn(job)
            cur = get(jid) or {}
            if cur.get("status") == "running":      # 跑完就收尾（取消只作用于排队中/会中断的任务）
                update(jid, status="done", finished_at=_now(), stage="")
        except Exception as e:
            msg = str(e)[:300] or "未知错误"
            log(jid, "❌ 失败：%s" % msg)
            cur = get(jid) or {}
            if cur.get("status") == "running":
                update(jid, status="canceled" if "已取消" in msg else "failed",
                       error=msg, finished_at=_now(), stage="")

    def enqueue(self, kind, article_id=None, payload=None, total=0, owner="", priority=0,
                owner_id=""):
        """入队（幂等）：同 kind + 文章 + 参数已在队列 → 复用。domain 取注册时声明的域"""
        old = find_active(kind, article_id, payload)
        if old:
            return old["id"], True
        return create(kind, article_id, payload, total, owner,
                      domain=self.domains.get(kind) or _KIND_DOMAIN.get(kind, "llm"),
                      priority=priority,
                      owner_id=owner_id), False


DISPATCHER = Dispatcher()


def stats_today(owner_id=None):
    """看板统计：排队 / 进行中 / 今日完成 / 今日失败或取消
       owner_id 给定时「今日完成/今日失败」只统计该用户（终态任务只看自己）"""
    c = _conn()
    day = time.strftime("%Y-%m-%d")
    def n(sql, a=()):
        return c.execute(sql, a).fetchone()[0]
    own = " AND owner_id=?" if owner_id else ""
    a_done = (day + "%", str(owner_id)) if owner_id else (day + "%",)
    out = {"queued": n("SELECT COUNT(*) FROM jobs WHERE status='queued'"),
           "running": n("SELECT COUNT(*) FROM jobs WHERE status='running'"),
           "done_today": n("SELECT COUNT(*) FROM jobs WHERE status='done' AND finished_at LIKE ?" + own, a_done),
           "failed_today": n("SELECT COUNT(*) FROM jobs WHERE status IN "
                             "('failed','canceled','interrupted') AND finished_at LIKE ?" + own, a_done)}
    c.close()
    return out


# 用时统计口径：ready_at → finished_at —— **不含开机等待与等 ComfyUI 就绪的时间**；
# 老数据没有 ready_at 时回退 started_at（口径同旧版）
_SEC = "((julianday(finished_at) - julianday(COALESCE(NULLIF(ready_at,''), started_at, finished_at))) * 86400)"

def _terminal_where(owner_id=None):
    w = ("status IN (%s) AND IFNULL(started_at,'')<>'' AND IFNULL(finished_at,'')<>''"
         % ",".join("'%s'" % x for x in STATUS_DONE))
    a = []
    if owner_id:
        w += " AND owner_id=?"
        a.append(str(owner_id))
    return w, a


def timing_summary(owner_id=None):
    """已完成任务用时统计（不受列表条数限制）。
       owner_id=None → 全部用户；返回 总数/合计秒/平均秒 + 按类型明细"""
    w, a = _terminal_where(owner_id)
    c = _conn()
    r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(%s),0) secs, COALESCE(AVG(%s),0) av"
                  " FROM jobs WHERE %s" % (_SEC, _SEC, w), a).fetchone()
    rows = c.execute("SELECT kind, COUNT(*) n, COALESCE(SUM(%s),0) secs, COALESCE(AVG(%s),0) av"
                     " FROM jobs WHERE %s GROUP BY kind ORDER BY secs DESC" % (_SEC, _SEC, w), a).fetchall()
    c.close()
    kinds = [{"kind": x["kind"], "label": KIND_LABEL.get(x["kind"], x["kind"]),
              "count": int(x["n"]), "seconds": int(round(x["secs"] or 0)),
              "avg": int(round(x["av"] or 0))} for x in rows]
    return {"count": int(r["n"]), "seconds": int(round(r["secs"] or 0)),
            "avg": int(round(r["av"] or 0)), "by_kind": kinds}


def owner_options():
    """已完成任务的归属人列表（管理员筛选用户用）"""
    c = _conn()
    rows = c.execute("SELECT owner_id, MAX(owner) owner, COUNT(*) n FROM jobs"
                     " WHERE IFNULL(owner_id,'')<>'' GROUP BY owner_id ORDER BY n DESC").fetchall()
    c.close()
    return [{"id": str(x["owner_id"]), "name": (x["owner"] or ("#" + str(x["owner_id"]))),
             "count": int(x["n"])} for x in rows]


def active_summary():
    """给前端/日志用：当前各 kind 的排队与运行数"""
    out = {}
    for k in KIND_LABEL:
        out[k] = {"queued": queued_count(k), "running": running_count(k)}
    return out
