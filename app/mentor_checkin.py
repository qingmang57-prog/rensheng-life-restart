# -*- coding: utf-8 -*-
"""主理人公测打卡 + 履约保证金台账。

口径（2026-11-03 已由用户确认）
- 首发 10 个席位，硬封顶 10 人；active 席位满 10 后只能等有人退出再补位。
- 每位主理人交 599 元「可退履约保证金」（代码/接口文案统一用“履约保证金”）。
- 公测期 60 天，每天 1 条作品；满 60 条全额退还 599 元。
- 漏发 1 条扣 50 元；漏发 >= 7 条（扣 >= 350 元）不予退还，应退 0 元。
- 工具故障 / 平台故障导致的漏发可人工豁免，必须写原因并留痕。
- 判定口径以工作台内的作品记录为准（platform_work / register_work 链路），
  手动补录只用于工具没抓到的场景。

数据
- 四张表直接建在现有 sqlite（workbench.sqlite3）里：
  mentor_seat / mentor_deposit / checkin_record / settlement。
- 金额一律用“分”存整数，展示时再转成两位小数字符串，不做浮点运算。
- 日期按 Asia/Shanghai（固定 UTC+8）的自然日，存 YYYY-MM-DD。
"""
import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

SEAT_LIMIT = 10
PERIOD_DAYS = 60
DEPOSIT_CENTS = 59900            # 599.00 元
PENALTY_CENTS = 5000             # 50.00 元 / 条
MISS_FORFEIT_COUNT = 7           # 漏发 >= 7 条即不予退还
TZ = timezone(timedelta(hours=8))
SEAT_STATUSES = ('active', 'out', 'eliminated')
DEPOSIT_STATUSES = ('held', 'refunded', 'forfeited')

_LOCK = threading.Lock()


# ------------------------------------------------------------------ 基础工具
def today_local():
    return datetime.now(TZ).date()


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError('日期格式应为 YYYY-MM-DD。') from None


def yuan(cents):
    """整型分 -> 两位小数字符串，避免浮点误差。"""
    cents = int(cents)
    sign = '-' if cents < 0 else ''
    cents = abs(cents)
    return '%s%d.%02d' % (sign, cents // 100, cents % 100)


def _now_text():
    return datetime.now(TZ).isoformat(timespec='seconds')


class MentorLedger:
    """一个实例对应一个 sqlite 文件；建表幂等，可在每个请求里临时创建。"""

    def __init__(self, store):
        self.path = str(getattr(store, 'path', store))
        self.store = store if hasattr(store, 'get') else None
        with self._connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS mentor_seat (
                    id TEXT PRIMARY KEY,
                    seat_no INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    joined_on TEXT NOT NULL,
                    left_on TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS mentor_seat_active_no
                    ON mentor_seat(seat_no) WHERE status='active';
                CREATE INDEX IF NOT EXISTS mentor_seat_status ON mentor_seat(status);

                CREATE TABLE IF NOT EXISTS mentor_deposit (
                    id TEXT PRIMARY KEY,
                    seat_id TEXT NOT NULL UNIQUE,
                    amount_cents INTEGER NOT NULL,
                    collected_on TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'held',
                    refunded_on TEXT,
                    refundable_cents INTEGER NOT NULL,
                    deduction_cents INTEGER NOT NULL DEFAULT 0,
                    deduction_detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);

                CREATE TABLE IF NOT EXISTS checkin_record (
                    id TEXT PRIMARY KEY,
                    seat_id TEXT NOT NULL,
                    date TEXT NOT NULL,
                    work_id TEXT,
                    platform TEXT NOT NULL DEFAULT '',
                    waived INTEGER NOT NULL DEFAULT 0,
                    waive_reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    recorded_at TEXT NOT NULL,
                    UNIQUE(seat_id, date));
                CREATE INDEX IF NOT EXISTS checkin_record_seat ON checkin_record(seat_id, date);
                CREATE INDEX IF NOT EXISTS checkin_record_work ON checkin_record(seat_id, work_id);

                CREATE TABLE IF NOT EXISTS settlement (
                    id TEXT PRIMARY KEY,
                    seat_id TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    checked INTEGER NOT NULL,
                    missed INTEGER NOT NULL,
                    deduction_cents INTEGER NOT NULL,
                    refundable_cents INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    snapshot TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL);
            ''')

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _row(row):
        return dict(row) if row else None

    # -------------------------------------------------------------- 席位
    def get_seat(self, seat_id):
        with self._connect() as con:
            return self._row(con.execute('SELECT * FROM mentor_seat WHERE id=?',
                                         (str(seat_id or ''),)).fetchone())

    def _require_seat(self, seat_id):
        seat = self.get_seat(seat_id)
        if not seat:
            raise KeyError(seat_id)
        return seat

    def list_seats(self, include_inactive=True):
        sql = 'SELECT * FROM mentor_seat '
        if not include_inactive:
            sql += "WHERE status='active' "
        sql += 'ORDER BY seat_no ASC, created_at ASC'
        with self._connect() as con:
            return [dict(r) for r in con.execute(sql)]

    def _active_count(self):
        with self._connect() as con:
            return int(con.execute("SELECT COUNT(*) FROM mentor_seat WHERE status='active'")
                       .fetchone()[0])

    def create_seat(self, seat_no, name, joined_on=None, note='', deposit_collected=True):
        """开一个席位并登记履约保证金。超过 10 人或同一席位已有人在约时拒绝。"""
        name = str(name or '').strip()
        if not 1 <= len(name) <= 40:
            raise ValueError('主理人姓名应为 1-40 个字。')
        try:
            seat_no = int(seat_no)
        except (TypeError, ValueError):
            raise ValueError('席位编号必须是 1-10 的整数。') from None
        if not 1 <= seat_no <= SEAT_LIMIT:
            raise ValueError('席位编号只能是 1-%d。' % SEAT_LIMIT)
        joined = parse_date(joined_on or today_local())
        note = str(note or '')[:200]
        with _LOCK, self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            active = int(con.execute("SELECT COUNT(*) FROM mentor_seat WHERE status='active'")
                         .fetchone()[0])
            if active >= SEAT_LIMIT:
                raise ValueError('%d 个席位已满，只能等有人退出后补位。' % SEAT_LIMIT)
            taken = con.execute("SELECT 1 FROM mentor_seat WHERE seat_no=? AND status='active'",
                                (seat_no,)).fetchone()
            if taken:
                raise ValueError('第 %d 号席位已有人在约，先登记退出再补位。' % seat_no)
            ts = _now_text()
            seat_id = 'seat_%d_%s' % (seat_no, datetime.now(TZ).strftime('%Y%m%d%H%M%S%f'))
            con.execute('INSERT INTO mentor_seat VALUES (?,?,?,?,?,?,?,?,?)',
                        (seat_id, seat_no, name, 'active', joined.isoformat(), None, note, ts, ts))
            dep_id = 'dep_' + seat_id[5:]
            con.execute('INSERT INTO mentor_deposit VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (dep_id, seat_id, DEPOSIT_CENTS, joined.isoformat(), 'held', None,
                         DEPOSIT_CENTS, 0, '{}', ts, ts))
        return {'seat': self.get_seat(seat_id), 'deposit': self.get_deposit_row(seat_id)}

    # -------------------------------------------------------------- 保证金行
    def get_deposit_row(self, seat_id):
        with self._connect() as con:
            return self._row(con.execute('SELECT * FROM mentor_deposit WHERE seat_id=?',
                                         (str(seat_id or ''),)).fetchone())

    def _deposit_cents(self, seat_id):
        row = self.get_deposit_row(seat_id)
        return int(row['amount_cents']) if row else DEPOSIT_CENTS

    # -------------------------------------------------------------- 打卡
    def _records(self, seat_id):
        with self._connect() as con:
            return [dict(r) for r in con.execute(
                'SELECT * FROM checkin_record WHERE seat_id=? ORDER BY date ASC', (seat_id,))]

    def record_checkin(self, seat_id, work_id, date_value, platform, source='platform'):
        """登记一条作品打卡。同一天只算 1 条；同一作品不重复计数。"""
        seat = self._require_seat(seat_id)
        if seat['status'] != 'active':
            raise ValueError('该席位已退出，不能再登记打卡。')
        day = parse_date(date_value or today_local())
        start = parse_date(seat['joined_on'])
        end = start + timedelta(days=PERIOD_DAYS - 1)
        if day < start:
            raise ValueError('打卡周期还没开始（起始日 %s）。' % start.isoformat())
        if day > end:
            raise ValueError('60 天周期已于 %s 结束，不能再补录。' % end.isoformat())
        if day > today_local():
            raise ValueError('不能给未来日期打卡（今天 %s）。' % today_local().isoformat())
        work_id = str(work_id or '').strip()
        platform = str(platform or '').strip()[:32]
        if not work_id and not platform:
            raise ValueError('请至少提供作品编号或平台名。')
        recorded_at = _now_text()
        with _LOCK, self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            same_day = con.execute('SELECT * FROM checkin_record WHERE seat_id=? AND date=?',
                                   (seat_id, day.isoformat())).fetchone()
            if same_day:
                return {'record': dict(same_day), 'dedup': True,
                        'message': '这一天已有记录，一天多条只算 1 条。'}
            if work_id:
                dup = con.execute(
                    'SELECT * FROM checkin_record WHERE seat_id=? AND work_id=? AND waived=0',
                    (seat_id, work_id)).fetchone()
                if dup:
                    return {'record': dict(dup), 'dedup': True,
                            'message': '这条作品已经记过卡（%s），不重复计数。' % dup['date']}
            # 以工作台内作品记录为准：命中 platform_work 时补全平台名。
            if work_id and self.store is not None:
                work = self.store.get('platform_work', work_id)
                if work:
                    platform = str(work.get('platform') or platform)[:32]
            rec_id = 'chk_%s_%s' % (seat_id[5:], day.strftime('%Y%m%d'))
            con.execute('INSERT INTO checkin_record VALUES (?,?,?,?,?,?,?,?,?)',
                        (rec_id, seat_id, day.isoformat(), work_id or None, platform,
                         0, '', source or 'platform', recorded_at))
            row = con.execute('SELECT * FROM checkin_record WHERE id=?', (rec_id,)).fetchone()
        return {'record': dict(row), 'dedup': False, 'message': '已记录一条作品打卡。',
                'progress': self.compute_deposit(seat_id)}

    def grant_waiver(self, seat_id, date_value, reason):
        """人工豁免某一天（工具故障 / 平台故障），必须写原因并留痕。"""
        seat = self._require_seat(seat_id)
        if seat['status'] != 'active':
            raise ValueError('该席位已退出，不能再登记豁免。')
        day = parse_date(date_value or today_local())
        reason = str(reason or '').strip()
        if not reason:
            raise ValueError('豁免必须写清原因（工具故障或平台故障）。')
        if len(reason) > 200:
            raise ValueError('豁免原因最多 200 字。')
        start = parse_date(seat['joined_on'])
        end = start + timedelta(days=PERIOD_DAYS - 1)
        if not start <= day <= end:
            raise ValueError('豁免日期应在周期 %s 至 %s 之内。' % (start.isoformat(), end.isoformat()))
        if day > today_local():
            raise ValueError('不能豁免未来日期。')
        with _LOCK, self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT * FROM checkin_record WHERE seat_id=? AND date=?',
                                   (seat_id, day.isoformat())).fetchone()
            if existing and existing['waived']:
                con.execute('UPDATE checkin_record SET waive_reason=? WHERE id=?',
                            (reason, existing['id']))
                row = con.execute('SELECT * FROM checkin_record WHERE id=?',
                                  (existing['id'],)).fetchone()
                return {'waiver': dict(row), 'dedup': True, 'message': '该日已豁免，原因已更新。',
                        'progress': self.compute_deposit(seat_id)}
            if existing:
                raise ValueError('该日已经打卡，无需豁免。')
            rec_id = 'wav_%s_%s' % (seat_id[5:], day.strftime('%Y%m%d'))
            con.execute('INSERT INTO checkin_record VALUES (?,?,?,?,?,?,?,?,?)',
                        (rec_id, seat_id, day.isoformat(), None, '', 1, reason, 'manual',
                         _now_text()))
            row = con.execute('SELECT * FROM checkin_record WHERE id=?', (rec_id,)).fetchone()
        return {'waiver': dict(row), 'dedup': False, 'message': '已登记豁免，漏发不计入扣减。',
                'progress': self.compute_deposit(seat_id)}

    # -------------------------------------------------------------- 结算
    def compute_deposit(self, seat_id, as_of=None):
        """按 60 天口径算应退金额与明细；as_of 仅用于测试/回放。"""
        seat = self._require_seat(seat_id)
        start = parse_date(seat['joined_on'])
        end = start + timedelta(days=PERIOD_DAYS - 1)
        as_of_date = parse_date(as_of) if as_of else today_local()
        if as_of_date < start:
            days_started = 0
            settled_end = start - timedelta(days=1)
        else:
            days_started = min(PERIOD_DAYS, (as_of_date - start).days + 1)
            settled_end = min(end, as_of_date - timedelta(days=1))
        settled_days = (0 if settled_end < start
                        else min(PERIOD_DAYS, (settled_end - start).days + 1))
        records = {r['date']: r for r in self._records(seat_id)}
        missed_dates, waived_dates = [], []
        for offset in range(settled_days):
            day = start + timedelta(days=offset)
            rec = records.get(day.isoformat())
            if rec is None:
                missed_dates.append(day.isoformat())
            elif rec.get('waived'):
                waived_dates.append(day.isoformat())
        checked = len([r for r in records.values()
                       if not r.get('waived') and start.isoformat() <= r['date'] <= end.isoformat()
                       and parse_date(r['date']) <= as_of_date])
        waived = len([r for r in records.values()
                      if r.get('waived') and start.isoformat() <= r['date'] <= end.isoformat()
                      and parse_date(r['date']) <= as_of_date])
        deposit_cents = self._deposit_cents(seat_id)
        missed = len(missed_dates)
        penalty_cents = missed * PENALTY_CENTS
        forfeited = missed >= MISS_FORFEIT_COUNT
        if forfeited:
            deduction_cents = deposit_cents
            refundable_cents = 0
        else:
            deduction_cents = min(deposit_cents, penalty_cents)
            refundable_cents = deposit_cents - deduction_cents
        period_done = as_of_date > end
        return {
            'seat_id': seat_id,
            'seat_no': seat['seat_no'],
            'name': seat['name'],
            'status': seat['status'],
            'period_start': start.isoformat(),
            'period_end': end.isoformat(),
            'as_of': as_of_date.isoformat(),
            'days_elapsed': days_started,
            'days_settled': settled_days,
            'checked': checked,
            'waived': waived,
            'missed': missed,
            'missed_dates': missed_dates,
            'waived_dates': waived_dates,
            'deposit_cents': deposit_cents,
            'deposit_yuan': yuan(deposit_cents),
            'penalty_per_miss_cents': PENALTY_CENTS,
            'penalty_per_miss_yuan': yuan(PENALTY_CENTS),
            'penalty_cents': penalty_cents,
            'penalty_yuan': yuan(penalty_cents),
            'deduction_cents': deduction_cents,
            'deduction_yuan': yuan(deduction_cents),
            'refundable_cents': refundable_cents,
            'refundable_yuan': yuan(refundable_cents),
            'forfeited': forfeited,
            'period_done': period_done,
            'today_checked': (today_local().isoformat() in records
                              and not records[today_local().isoformat()].get('waived')),
            'remaining_days': max(0, PERIOD_DAYS - days_started),
        }

    def seat_license(self, seat_id, as_of=None):
        """是否仍有分销资格：在约且未触发“漏发 >= 7 条”才有效。"""
        seat = self._require_seat(seat_id)
        if seat['status'] != 'active':
            return {'seat_id': seat_id, 'licensed': False, 'checked_at': _now_text(),
                    'reason': '席位状态为 %s，分销资格已停用。' % seat['status']}
        progress = self.compute_deposit(seat_id, as_of=as_of)
        if progress['forfeited']:
            return {'seat_id': seat_id, 'licensed': False, 'checked_at': _now_text(),
                    'reason': '漏发 %d 条已达不予退还条件，分销资格停用。' % progress['missed']}
        return {'seat_id': seat_id, 'licensed': True, 'checked_at': _now_text(),
                'reason': '在约期内，分销资格有效。'}

    def seat_overview(self, as_of=None):
        items = []
        for seat in self.list_seats():
            progress = self.compute_deposit(seat['id'], as_of=as_of)
            dep = self.get_deposit_row(seat['id'])
            items.append({
                'id': seat['id'], 'seat_no': seat['seat_no'], 'name': seat['name'],
                'status': seat['status'], 'joined_on': seat['joined_on'],
                'left_on': seat['left_on'], 'note': seat['note'],
                'progress': progress,
                'deposit': dep,
                'license': self.seat_license(seat['id'], as_of=as_of),
            })
        return items

    def deposit_detail(self, seat_id, as_of=None):
        seat = self._require_seat(seat_id)
        progress = self.compute_deposit(seat_id, as_of=as_of)
        return {
            'seat': seat,
            'deposit': self.get_deposit_row(seat_id),
            'progress': progress,
            'missed_days': [
                {'date': d, 'reason': '未登记作品'}
                for d in progress['missed_dates']
            ],
            'waived_days': [
                {'date': r['date'], 'reason': r.get('waive_reason') or ''}
                for r in self._records(seat_id) if r.get('waived')
            ],
            'license': self.seat_license(seat_id, as_of=as_of),
        }

    def save_settlement(self, seat_id, as_of=None):
        """把当前结算快照写入 settlement（接口未强开，留给后续对账）。"""
        progress = self.compute_deposit(seat_id, as_of=as_of)
        ts = _now_text()
        sid = 'set_%s_%s' % (seat_id[5:], datetime.now(TZ).strftime('%Y%m%d%H%M%S%f'))
        with _LOCK, self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute('INSERT INTO settlement VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (sid, seat_id, progress['period_start'], progress['period_end'],
                         progress['checked'], progress['missed'], progress['deduction_cents'],
                         progress['refundable_cents'],
                         'final' if progress['period_done'] else 'draft',
                         json.dumps(progress, ensure_ascii=False), ts))
        return {'id': sid, 'progress': progress}


# ------------------------------------------------------------------ HTTP 路由
def route(handler, method, bits, query, store, data_dir=None):
    """挂在 app.py 的 route 里；只处理 /api/mentor/*，命中返回 True。"""
    if not bits or bits[:2] != ['api', 'mentor']:
        return False
    ledger = MentorLedger(store)

    def q(name, default=''):
        return (query.get(name) or [default])[0]

    def reply(payload, status=200):
        # handler.json 已经写出响应；必须返回 True 让 app.py 的 route 立即结束，
        # 否则外层 if 会把 None 当假值继续往下走，再补写一个 404。
        handler.json(payload, status)
        return True

    path = '/' + '/'.join(bits)
    if path == '/api/mentor/seats' and method == 'GET':
        return reply({'ok': True, 'seat_limit': SEAT_LIMIT,
                      'deposit_yuan': yuan(DEPOSIT_CENTS),
                      'penalty_per_miss_yuan': yuan(PENALTY_CENTS),
                      'miss_forfeit_count': MISS_FORFEIT_COUNT,
                      'as_of': q('as_of') or None,
                      'seats': ledger.seat_overview(as_of=q('as_of') or None)})
    if path == '/api/mentor/seats' and method == 'POST':
        data = handler.body()
        created = ledger.create_seat(data.get('seat_no'), data.get('name'),
                                     data.get('joined_on'), data.get('note'),
                                     data.get('deposit_collected', True))
        seat_id = created['seat']['id']
        return reply({'ok': True, **created,
                      'progress': ledger.compute_deposit(seat_id)}, 201)
    if path == '/api/mentor/checkin' and method == 'POST':
        data = handler.body()
        result = ledger.record_checkin(data.get('seat_id'), data.get('work_id'),
                                       data.get('date'), data.get('platform'),
                                       data.get('source') or 'manual')
        return reply({'ok': True, **result}, 200 if result.get('dedup') else 201)
    if path == '/api/mentor/waiver' and method == 'POST':
        data = handler.body()
        result = ledger.grant_waiver(data.get('seat_id'), data.get('date'), data.get('reason'))
        return reply({'ok': True, **result}, 200 if result.get('dedup') else 201)
    if len(bits) == 4 and bits[:3] == ['api', 'mentor', 'deposit'] and method == 'GET':
        return reply({'ok': True, **ledger.deposit_detail(
            bits[3], as_of=q('as_of') or None)})
    raise ValueError('接口不存在。')
