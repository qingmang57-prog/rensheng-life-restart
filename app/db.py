"""Local, single-owner storage. No external dependency or cloud synchronization."""
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return prefix + '_' + uuid.uuid4().hex[:16]


class Conflict(Exception):
    pass


class Store:
    # 连接池上限：并发请求各借一条，超出才新建/关闭（实测每请求 connect/close 触发 WAL checkpoint 是暖请求卡顿根因之一）。
    _POOL_MAX = 8

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'workbench.sqlite3'
        self._pool = []
        self._pool_lock = threading.Lock()
        self._versions = {}
        self._ver_lock = threading.Lock()
        with self.connect() as con:
            con.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL, id TEXT NOT NULL,
                    data TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(kind,id));
                CREATE INDEX IF NOT EXISTS by_kind ON records(kind,updated_at);
                CREATE INDEX IF NOT EXISTS by_kind_created ON records(kind,created_at);
                CREATE TABLE IF NOT EXISTS imports (
                    sha256 TEXT PRIMARY KEY, filename TEXT NOT NULL,
                    source_id TEXT NOT NULL, imported_at TEXT NOT NULL, count INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        # 复用连接而不是每请求 connect/close：最后一个连接关闭会触发 WAL checkpoint（HDD 上实测暖请求被拖到秒级）。
        with self._pool_lock:
            con = self._pool.pop() if self._pool else None
        if con is None:
            con = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
            con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            with self._pool_lock:
                if len(self._pool) < self._POOL_MAX:
                    self._pool.append(con)
                else:
                    con.close()

    def bump(self, kind):
        """直接 SQL 写 records 的地方（绕过 put/patch）手动 bump，让 TTL 缓存立即失效。"""
        with self._ver_lock:
            self._versions[kind] = self._versions.get(kind, 0) + 1

    def versions(self, kinds):
        """缓存键用的写版本号快照；任何 put/patch/bump 都会推进对应 kind。"""
        with self._ver_lock:
            return tuple(self._versions.get(k, 0) for k in kinds)

    @staticmethod
    def unpack(row):
        if not row:
            return None
        data = json.loads(row['data'])
        data.update(id=row['id'], revision=row['revision'], created_at=row['created_at'], updated_at=row['updated_at'])
        return data

    def get(self, kind, key):
        with self.connect() as con:
            return self.unpack(con.execute('SELECT * FROM records WHERE kind=? AND id=?', (kind, key)).fetchone())

    def list(self, kind, limit=300):
        with self.connect() as con:
            return [self.unpack(r) for r in con.execute('SELECT * FROM records WHERE kind=? ORDER BY updated_at DESC LIMIT ?', (kind, limit))]

    def put(self, kind, data, key=None, expected=None):
        key = key or data.get('id') or uid(kind)
        clean = {k:v for k,v in data.items() if k not in ('id','revision','created_at','updated_at')}
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM records WHERE kind=? AND id=?', (kind,key)).fetchone()
            rev = row['revision'] if row else 0
            if expected is not None and expected != rev:
                raise Conflict('内容已在另一页面更新，请刷新后比较再保存。')
            ts = now()
            con.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?,?,?)',
                        (kind,key,json.dumps(clean,ensure_ascii=False),rev+1,row['created_at'] if row else ts,ts))
        self.bump(kind)
        return self.get(kind,key)

    def patch(self, kind, key, changes, expected=None):
        old = self.get(kind,key)
        if old is None:
            raise KeyError(key)
        check = old['revision'] if expected is None else expected
        return self.put(kind,{**old,**changes},key,check)

    def import_brain(self, text, filename, tick=None):
        """导入 .md 认知文档（t187：新增可选进度回调，向后兼容）。

        tick(phase, done, total) 只在**事务外**调用，避免「BEGIN 事务内另开连接写 job」把库锁死：
          phase='parse'        → 解析阶段（此时还没开事务）；done/total 均为条目数
          phase='commit_start' → 即将开始写库事务（仍无事务在跑）
          phase='done'         → 事务已提交/去重命中，可安全收尾
        旧调用（不传 tick）行为完全不变。
        """
        original_text=text
        # File.text() and Python text reads differ on Windows line endings.
        # Dedup by normalized content while retaining the supplied full text.
        text=text.lstrip('\ufeff').replace('\r\n','\n').replace('\r','\n')
        sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with self.connect() as con:
            prior = con.execute('SELECT * FROM imports WHERE sha256=?',(sha,)).fetchone()
            if prior:
                if tick: tick('done', prior['count'], prior['count'])
                return {'duplicate':True,'count':prior['count'],'parsed':prior['count'],'source_id':prior['source_id'],'sha256':sha}
        pattern = re.compile(r'^###\s+((?:三观|账号观|驾驭金钱|事业)-\d+)\s*[·・]\s*(.+?)\s*$',re.M)
        matches = list(pattern.finditer(text))
        if not matches:
            raise ValueError('没有找到认知条目标题。支持“### 三观-01 · 标题”等格式。')
        source_id = 'brain_source_' + sha[:16]
        rows = []
        total_items = len(matches)
        if tick: tick('parse', 0, total_items)
        for i,m in enumerate(matches):
            legacy,title = m.groups()
            end = matches[i+1].start() if i+1<len(matches) else len(text)
            raw_section = text[m.end():end].strip()
            body = re.split(r'^##\s+',raw_section,flags=re.M)[0].strip()
            # Revision notes can qualify the preceding item outside its body.
            # Preserve that context as source data, never as agent instructions.
            restricted='口径锁死' in raw_section or '不派去写稿' in raw_section
            context_notes=raw_section[len(body):].strip() if restricted else ''
            def field(label):
                match = re.search(r'^-\s*\*\*'+label+r'(?:[^*]*)\*\*\s*[：:]?\s*(.*?)(?=\n-\s*\*\*|\Z)',body,re.M|re.S)
                return match.group(1).strip() if match else ''
            claim = field('一句话主张').lstrip('：:').strip()
            boundary = field('边界').lstrip('：:').strip()
            links = sorted(set(re.findall(r'(?:三观|账号观|驾驭金钱|事业)-\d+',body))-{legacy})
            markers = sorted(set(re.findall(r'待他认|待他验|待拍板|口径锁死|候选|用词更正',body+context_notes)))
            import content_index
            old_category = legacy.rsplit('-',1)[0]
            category, category_legacy = content_index.normalize_category(old_category)
            rows.append((legacy, {'legacy_id':legacy,'title':title,'category':category,
                'category_legacy':category_legacy,
                'category_aliases':content_index.row_aliases(category, category_legacy),
                'claim':claim,'boundary':boundary,'body':body,'source_id':source_id,'links':links,
                'endorsement':'legacy','validation':'unverified','lifecycle':'active','markers':markers,
                'content_use':'review' if restricted else 'available','context_notes':context_notes,
                'source_locator':{'line':text[:m.start()].count('\n')+1},'import_sha256':sha}))
            if tick and (len(rows) % 5 == 0 or len(rows) == total_items):
                tick('parse', len(rows), total_items)
        ts = now()
        # Source and all items are committed together; no half-import on crash.
        if tick: tick('commit_start', len(rows), len(rows))
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT 1 FROM imports WHERE sha256=?',(sha,)).fetchone():
                return {'duplicate':True,'count':len(rows),'source_id':source_id,'sha256':sha}
            source = {'title':filename,'text':original_text,'kind':'legacy_brain','sha256':sha,'hash_basis':'utf8_bom_removed_lf'}
            con.execute('INSERT INTO records VALUES (?,?,?,?,?,?)',('source',source_id,json.dumps(source,ensure_ascii=False),1,ts,ts))
            for legacy,data in rows:
                key = 'knowledge_' + sha[:8] + '_' + legacy
                con.execute('INSERT INTO records VALUES (?,?,?,?,?,?)',('knowledge',key,json.dumps(data,ensure_ascii=False),1,ts,ts))
            con.execute('INSERT INTO imports VALUES (?,?,?,?,?)',(sha,filename,source_id,ts,len(rows)))
        self.bump('source')
        self.bump('knowledge')
        if tick: tick('done', len(rows), len(rows))
        return {'duplicate':False,'count':len(rows),'parsed':len(rows),'source_id':source_id,'sha256':sha,
                'unresolved_links':sorted({x for _,r in rows for x in r['links']}-{x for x,_ in rows})}

