# -*- coding: utf-8 -*-
"""计费接线：把 billing_service 的账本挂到 HTTP 与封面解锁上。

边界：
  - 不改 billing_service 的业务逻辑，只做参数搬运与错误翻译；
  - 封面扣费走 purchase_entitlement（hold -> settle -> 授权，同一事务、幂等键防重复扣）；
  - 赠送积分与购买积分分开返回，购买积分永不过期（由 billing_service 保证）。
"""
import time
import uuid
from datetime import datetime

import billing_db
import billing_payee
import billing_quota
import billing_service
import billing_settings
import cloud_gateway
import license
import redeem_service

LOCAL_USER_ID = billing_service.LOCAL_USER_ID
# 云端钱包冻结号前缀（2026-11-18 积分真源）：finish_brain_ask 据此分流到云端关单，
# 进程重启后也不依赖内存登记簿。本地冻结号仍为 brainask_ 前缀，互不混淆。
CLOUD_JOB_PREFIX = 'brainaskc_'


def _is_cloud_user():
    return billing_service.request_user_id().startswith('cloud-')

_SERVICE = {'svc': None}


def service():
    """惰性单例：第一次真正用到计费时才建库，import app 本身不写盘。"""
    if _SERVICE['svc'] is None:
        _SERVICE['svc'] = billing_service.BillingService()
    return _SERVICE['svc']


def _seed_once():
    """按账号补一次首启赠送（幂等键带账号，两个账号各发各的）。

    登录上线后 startup() 那条只对 local-owner 生效；新账号第一次进计费页时
    在这里补发，避免"注册完余额 0、什么都试不了"。
    """
    svc = service()
    uid = billing_service.request_user_id()
    try:
        if svc.ledger(limit=1)['total'] > 0:
            return None
        settings = billing_settings.load()
        credits = int(settings.get('trial_grant_credits') or 0)
        if credits <= 0:
            return None
        try:
            state = license.state()
        except Exception:  # noqa: BLE001
            state = {}
        expires_at = _iso_epoch(state.get('expires_at'))
        if expires_at is None:
            expires_at = int(time.time()) + 365 * 86400
        return svc.grant(
            credits * billing_service.CREDIT_MILLI,
            idempotency_key='trial-free-credits:' + uid,
            kind='grant', action='free_monthly', expires_at=expires_at,
            note=str(settings.get('trial_grant_note') or '试用首启赠送') + '（%d 积分）' % credits)
    except billing_service.BillingError:
        return None


def _quota_conn():
    return billing_db.connect(service().db_path)


def _quota_overrides():
    """云端权益 → 配额覆盖（2026-11-14）：额度由云端逐人下发，本地只执行。"""
    ent = billing_service.request_entitlements()
    if not ent:
        return None
    if ent.get('whitelist'):
        return {'export': {'month': None, 'day': None}}
    if 'exports_per_month' in ent:
        return {'export': {'month': ent.get('exports_per_month'), 'day': None}}
    return None


def usage(now=None):
    """当前账号的免费额度用量（G4）：导出/问答各自的周期与剩余。"""
    svc = service()
    member = svc.membership()
    pricing = svc.pricing()
    overrides = _quota_overrides()
    with _quota_conn() as con:
        items = [billing_quota.status(con, billing_service.request_user_id(), feature,
                                      pricing, member['plan'], now, overrides)
                 for feature in billing_quota.FEATURES]
    return {'plan': member['plan'], 'features': member.get('features') or {},
            'items': items}


EXPORT_CHANNELS = {
    'clip_pack': '剪映素材包',
    'delivery_zip': '四件套打包',
    'artifact_download': '成片下载',
}
# 到期后仍允许的"取走已有产物"通道：不扣次数（到期额度本就是 0）
DOWNLOAD_CHANNELS = ('delivery_zip', 'artifact_download')


def export_gate(project_id='', channel='clip_pack'):
    """成片导出前的额度闸门（G4）。

    返回 None 表示放行；返回 dict 表示拦截（带 403 与一句话说明）。
    放行时已把这次额度记上（幂等键 = 通道 + 项目 id：同一动作重试不重复扣，
    换一条导出路径要各算一次），打包失败要调 export_cancel 退回。

    到期（read_only）拆成两个判据（协议第六条第 1 款）：
    - 下载类（成片下载、四件套打包）放行且不计次，让用户取走已有产物；
    - 新建导出包（剪映素材包）拒绝，并给可读原因。
    """
    try:
        state = license.state()
    except Exception:  # noqa: BLE001
        state = {}
    if state.get('read_only'):
        if channel in DOWNLOAD_CHANNELS:
            return None
        return {'ok': False, 'code': 'trial_ended',
                'message': '试用已到期：不能新建导出包或重新打包。'
                           '已生成的成片仍可下载，剪映草稿与项目备份仍可导出。'}
    _seed_once()
    svc = service()
    member = svc.membership()
    uid = billing_service.request_user_id()
    key = 'export:%s:%s' % (channel, str(project_id or '-'))
    overrides = _quota_overrides()
    with _quota_conn() as con:
        with billing_db.transaction(con):
            ok, st = billing_quota.consume(con, uid, 'export', svc.pricing(), member['plan'],
                                           key, action=channel, overrides=overrides)
    if ok:
        return None
    return {'ok': False, 'code': 'quota_exceeded',
            'message': st.get('message') or '免费额度已用完，升级会员可不限次数。',
            'usage': st}


def export_cancel(project_id='', channel='clip_pack'):
    """导出失败时退回刚占用的额度。"""
    svc = service()
    with _quota_conn() as con:
        with billing_db.transaction(con):
            return billing_quota.refund(con, billing_service.request_user_id(), 'export',
                                        'export:%s:%s' % (channel, str(project_id or '-')))


def _plan_label(plan):
    plans = service().pricing().get('plans') or {}
    return str((plans.get(plan) or {}).get('label') or plan)


def _iso_epoch(value):
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def startup():
    """启动时补一次首启赠送积分（幂等）。

    额度来自 src/billing_config.json 的 trial_grant_credits（默认 150，够解锁 3 套），
    不在业务代码里写死；trial / active 两种模式都发（去除试用期后新装即为 active）。
    赠送积分的名义到期日：试用态按试用到期日，无试用期按首启 +365 天；
    宽限期由 billing_service 统一加 30 天。
    """
    # 2026-11-18 积分真源：上次进程没关成的云端冻结/结算，按 jobRef 幂等重放一次。
    try:
        cloud_gateway.wallet_replay_pending()
    except Exception:  # noqa: BLE001
        pass
    try:
        state = license.state()
    except Exception as exc:  # noqa: BLE001
        return {'seeded': False, 'reason': '试用状态读取失败：%s' % str(exc)[:120]}
    if state.get('mode') not in ('trial', 'active'):
        return {'seeded': False, 'reason': '当前不可用：%s' % state.get('mode')}
    settings = billing_settings.load()
    credits = int(settings.get('trial_grant_credits') or 0)
    if credits <= 0:
        return {'seeded': False, 'reason': 'billing_config.json 未配置 trial_grant_credits'}
    first_start = str(state.get('first_start') or '')[:10] or 'unknown'
    expires_at = _iso_epoch(state.get('expires_at'))
    if expires_at is None:
        # 无试用期：赠送积分的名义到期日不再跟 7 天试用挂钩。
        expires_at = int(time.time()) + 365 * 86400
    try:
        result = service().grant(
            credits * billing_service.CREDIT_MILLI,
            idempotency_key='trial-free-credits:' + first_start,
            kind='grant', action='free_monthly', expires_at=expires_at,
            note=str(settings.get('trial_grant_note') or '试用首启赠送') + '（%d 积分）' % credits)
    except billing_service.BillingError as exc:
        return {'seeded': False, 'reason': exc.message}
    return {'seeded': True, 'duplicate': bool(result.get('duplicate')),
            'credits': result.get('delta_credits'),
            'ledger_id': result.get('ledger_id'),
            'expires_at': expires_at}


def brief():
    """给前端/封面工坊用的小结：余额分桶 + 当前档位 + 封面解锁单价。"""
    _seed_once()
    snap = service().balance()
    member = service().membership()
    try:
        cover_price = service().quote('cover_template_unlock')['amount_credits']
    except billing_service.BillingError:
        cover_price = ''
    return {'balance_credits': snap['balance_credits'],
            'granted_credits': snap['granted_credits'],
            'purchased_credits': snap['purchased_credits'],
            'frozen_credits': snap['frozen_credits'],
            'plan': member['plan'],
            'plan_label': _plan_label(member['plan']),
            'membership_active': member['active'],
            'membership_days_left': member['days_left'],
            'cover_price_credits': cover_price,
            **member_view()}


def summary(force=False):
    """计费小结。登录账号（cloud-*）另附 cloud_wallet 云端积分快照（真源）；
    balance 字段仍是本地账本（封面/形象等本地商品专用），两者不混用。
    force=True 跳过 ≤15s 内存缓存直读云端（问答结束后的强制刷新口径）。
    """
    _seed_once()
    data = {'balance': service().balance(),
            'membership': service().membership(),
            'usage': usage(),
            'trial': license.state(),
            'plans': service().pricing().get('plans') or {},
            'actions': service().price_book(),
            **member_view()}
    if _is_cloud_user():
        try:
            # 短超时 4s：云端不可达回 stale 缓存，不阻塞 bootstrap/计费页。
            data['cloud_wallet'] = cloud_gateway.credit_balance(force=force, timeout=4)
        except Exception:  # noqa: BLE001
            data['cloud_wallet'] = {'balance': None, 'held': None, 'available': None,
                                    'stale': True, 'fetched_at': None}
    return data


def charge_cover_template(template_id):
    """封面模板解锁的真实扣费挂点；失败只返回人话，不抛异常。

    已授权（entitlement 存在）直接放行、不再扣分；未授权则买断一次，
    同一模板用固定幂等键，重复提交不会重复扣。
    """
    tid = str(template_id or '').strip()
    if not tid:
        return {'ok': False, 'code': 'validation_error', 'message': '没有指定封面模板，未解锁。'}
    svc = service()
    if membership_free():
        return {'ok': True, 'free': True, 'ledger_id': None,
                'message': '会员期内免费解锁这套模板，不扣积分。'}
    try:
        if svc.has_entitlement(kind='template', ref_id=tid):
            return {'ok': True, 'already': True, 'ledger_id': None,
                    'message': '这套模板已解锁过，直接生成即可。'}
        result = svc.purchase_entitlement(
            kind='template', ref_id=tid,
            idempotency_key='cover-unlock:' + billing_service.request_user_id() + ':' + tid,
            note='封面模板解锁 ' + tid)
    except billing_service.InsufficientCredits:
        try:
            needed = svc.quote('cover_template_unlock', item_id=tid)['amount_credits']
        except billing_service.BillingError:
            needed = '?'
        snap = svc.balance()
        message = ('额度不足：解锁这套封面模板需要 %s 额度，当前可用 %s 额度'
                   '（赠送 %s / 购买 %s）。可以先改用免费模板；'
                   '需要开通或充值，请联系管理员获取兑换码。'
                   % (needed, snap['balance_credits'],
                      snap['granted_credits'], snap['purchased_credits']))
        return {'ok': False, 'code': 'insufficient_credits', 'message': message,
                'required_credits': needed, 'balance': snap}
    except billing_service.BillingError as exc:
        return {'ok': False, 'code': exc.code, 'message': exc.message, 'details': exc.details}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'code': 'billing_error',
                'message': '计费暂时不可用，未解锁：%s' % str(exc)[:120]}
    snap = svc.balance()
    if result.get('already'):
        return {'ok': True, 'already': True, 'ledger_id': None,
                'balance_credits': snap['balance_credits'],
                'granted_credits': snap['granted_credits'],
                'purchased_credits': snap['purchased_credits'],
                'message': '这套模板已解锁过，直接生成即可。'}
    return {'ok': True, 'already': False, 'ledger_id': result.get('ledger_id'),
            'job_id': result.get('job_id'),
            'charged_credits': result.get('charged_credits'),
            'balance_credits': snap['balance_credits'],
            'granted_credits': snap['granted_credits'],
            'purchased_credits': snap['purchased_credits'],
            'message': '已解锁，消耗 %s 额度；剩余 %s 额度（赠送 %s / 购买 %s）。'
                       % (result.get('charged_credits'), snap['balance_credits'],
                          snap['granted_credits'], snap['purchased_credits'])}


def _cloud_charge_error(exc):
    """云端钱包 hold 失败 → 与本地 insufficient_credits 同形状的用户文案。
    余额数字来自云端错误体/快照，不读本地账本（设计稿 §3.2-2）。"""
    code = str(getattr(exc, 'code', '') or '')
    details = getattr(exc, 'details', {}) or {}
    if code == 'INSUFFICIENT_POINTS':
        required = details.get('required')
        available = details.get('available')
        if available is None:
            snap = cloud_gateway.credit_balance(force=True)
            available = snap.get('available')
        return {'ok': False, 'code': 'insufficient_credits',
                'required_points': required, 'available_points': available,
                'message': ('积分不足：问一次知识库需要 %s 积分，当前可用 %s 积分。'
                            '需要开通或充值，请联系管理员在后台加分'
                            '（本地兑换码只作用于封面/形象等本地商品）。'
                            % (required if required is not None else 1,
                               available if available is not None else 0))}
    if code in ('UNAUTHORIZED', 'NOT_LOGGED_IN'):
        return {'ok': False, 'code': 'cloud_unauthorized',
                'message': '云端登录已失效，请重新登录后再问知识库；'
                           '问自己的记录（本地直答）不受影响。'}
    if code == 'UNREACHABLE':
        cloud_gateway.mark_stale()
        return {'ok': False, 'code': 'cloud_unreachable',
                'message': '云端暂时连不上，本次模型问答没有发起（不扣积分、不赊账）。'
                           '可以先问自己的记录，本地直答免费；网络恢复后再试。'}
    if code == 'ACCOUNT_BANNED':
        return {'ok': False, 'code': 'account_banned',
                'message': str(exc) or '账号已被封禁，无法使用云端问答。'}
    if code == 'RATE_LIMITED':
        return {'ok': False, 'code': 'rate_limited',
                'message': '请求过于频繁，请稍后再试。'}
    return {'ok': False, 'code': 'billing_error',
            'message': '云端计费暂时不可用，本次没有扣分：%s' % str(exc)[:120]}


def _start_cloud_brain_ask(note):
    """登录账号（cloud-*）：冻结走云端钱包，不消耗本地 quota、不扣本地账本。
    云端账号的放行依据只有云端 available；每日 token 预算闸在 brain_ask 侧
    先行判定（先预算后积分，设计稿 §1.2），此处不重复。"""
    job_id = CLOUD_JOB_PREFIX + uuid.uuid4().hex[:16]
    try:
        cloud_gateway.credit_hold(job_id)
    except cloud_gateway.ProxyError as exc:
        return None, _cloud_charge_error(exc)
    except Exception as exc:  # noqa: BLE001
        return None, {'ok': False, 'code': 'billing_error',
                      'message': '计费暂时不可用，本次没有扣分：%s' % str(exc)[:120]}
    # 登记本线程冻结号：providers.llm_json → cloud_gateway.llm_chat 自动带 jobRef，
    # 服务端按 jobRef 记用量，超时清扫据此分流 settle/release。
    cloud_gateway.set_ask_job(job_id)
    return job_id, None


# 2026-11-19 t152：会员期内免费＋云端免费名额（free_seats）变量。
_FREE_SEATS = {'at': 0, 'value': None}


def membership_free():
    """当前账号会员是否有效（有效期内 → 所有付费功能免费、不扣积分）。"""
    try:
        return bool(service().membership().get('active'))
    except Exception:
        return False


def free_seats():
    """免费名额：优先云端 /api/config 的 flags.free_seats（admin 可改），失败回落本地配置；默认 50。"""
    import time as _time
    now = _time.time()
    if _FREE_SEATS['value'] is not None and now - _FREE_SEATS['at'] < 60:
        return _FREE_SEATS['value']
    value = None
    try:
        import auth_service
        data, _token = auth_service._cloud('GET', '/api/config?ts=%d' % int(now))
        cfg = (data or {}).get('config') or {}
        raw = cfg.get('free_seats')
        if raw is None or raw == '':
            flag = (cfg.get('flags') or {}).get('free_seats') or {}
            raw = flag.get('scope')
            if raw is None or raw == '':
                raw = flag.get('enabled')
        try:
            value = max(0, min(1000000, int(raw)))
        except (TypeError, ValueError):
            value = None
    except Exception:
        value = None
    if value is None:
        try:
            import billing_settings
            value = int(billing_settings.load().get('free_seats') or 0)
        except Exception:
            value = 50
    _FREE_SEATS.update(at=now, value=value)
    return value


def member_view():
    """给会员中心/计费页的会员与免费名额说明（真实数据，不编）。"""
    try:
        member = service().membership()
    except Exception:
        member = {'plan': 'free', 'active': False, 'expire_at': None, 'days_left': 0}
    seats = free_seats()
    expiry = member.get('expire_at')
    expire_text = ''
    if expiry:
        try:
            import datetime
            expire_text = datetime.datetime.fromtimestamp(int(expiry)).strftime('%Y-%m-%d')
        except Exception:
            expire_text = ''
    return {'member_free': bool(member.get('active')),
            'member_plan': member.get('plan') or 'free',
            'member_expire_at': expiry,
            'member_expire_text': expire_text,
            'free_seats': seats,
            'free_seats_open': (not member.get('active')) and seats > 0,
            'free_seats_note': ('免费名额开放中：前 %d 名注册/激活可免费使用' % seats) if seats > 0 else '免费名额已满：开通会员后继续使用'}


def start_brain_ask(note='第二大脑问答'):
    """问第二大脑的真实扣费：先冻结一次询价，生成成功再结算、失败则释放。

    返回 (job_id, error)；error 不为空表示没扣成，调用方应直接把 error 回给用户，
    不超过一次提问的额度只冻结一次，不会出现"回答失败还扣分"。

    2026-11-18 积分真源：cloud-* 走云端钱包（_start_cloud_brain_ask）；
    local-owner 仍走本地 quota + 本地账本，原逻辑不变。
    """
    svc = service()
    _seed_once()
    # 2026-11-19 t152：会员期内所有付费功能免费——不冻结、不扣积分、不占免费额度。
    if membership_free():
        return 'brainask_free_' + uuid.uuid4().hex[:16], None
    if _is_cloud_user():
        return _start_cloud_brain_ask(note)
    job_id = 'brainask_' + uuid.uuid4().hex[:16]
    member = svc.membership()
    with _quota_conn() as con:
        with billing_db.transaction(con):
            accepted, qstate = billing_quota.consume(
                con, billing_service.request_user_id(), 'brain_ask', svc.pricing(),
                member['plan'], 'brain_ask:' + job_id, action='brain_ask')
    if not accepted:
        return None, {'ok': False, 'code': 'quota_exceeded',
                      'message': qstate.get('message') or '今天的免费问答次数已用完。',
                      'usage': qstate}
    try:
        result = svc.hold(action='brain_ask', idempotency_key='brain-ask:' + job_id,
                          job_id=job_id, note=note)
    except billing_service.InsufficientCredits:
        _refund_quota('brain_ask', 'brain_ask:' + job_id)
        snap = svc.balance()
        try:
            needed = svc.quote('brain_ask')['amount_credits']
        except billing_service.BillingError:
            needed = 1
        return None, {'ok': False, 'code': 'insufficient_credits',
                      'required_credits': needed, 'balance': snap,
                      'message': ('额度不足：问一次知识库需要 %s 额度，当前可用 %s 额度'
                                  '（赠送 %s / 购买 %s）。需要开通或充值，请联系管理员获取兑换码。'
                                  % (needed, snap['balance_credits'],
                                     snap['granted_credits'], snap['purchased_credits']))}
    except billing_service.BillingError as exc:
        _refund_quota('brain_ask', 'brain_ask:' + job_id)
        return None, {'ok': False, 'code': exc.code,
                      'message': '计费暂时不可用，本次没有扣分：%s' % exc.message}
    except Exception as exc:  # noqa: BLE001
        _refund_quota('brain_ask', 'brain_ask:' + job_id)
        return None, {'ok': False, 'code': 'billing_error',
                      'message': '计费暂时不可用，本次没有扣分：%s' % str(exc)[:120]}
    return job_id, None


def _refund_quota(feature, idem_key):
    try:
        with _quota_conn() as con:
            with billing_db.transaction(con):
                billing_quota.refund(con, billing_service.request_user_id(), feature,
                                     idem_key)
    except Exception:  # noqa: BLE001
        pass


def finish_brain_ask(job_id, ok=True):
    """结算或释放；任何异常都不往外抛，避免回答已生成却因计费报错丢掉结果。

    云端冻结号（brainaskc_ 前缀）走云端关单：重试 3 次 + 待对账日志 + 服务端
    清扫兜底（cloud_gateway.wallet_close）；本地冻结号原逻辑不变。
    """
    if not job_id:
        return None
    if str(job_id).startswith('brainask_free_'):
        return {'ok': True, 'free': True, 'charged': 0, 'note': '会员期内免费，不扣积分'}
    cloud_gateway.clear_ask_job()
    if str(job_id).startswith(CLOUD_JOB_PREFIX):
        try:
            return cloud_gateway.wallet_close(job_id, ok=ok)
        except Exception:  # noqa: BLE001
            return None
    svc = service()
    try:
        if ok:
            return svc.settle(job_id, note='第二大脑问答完成')
        _refund_quota('brain_ask', 'brain_ask:' + job_id)
        return svc.release(job_id, reason='生成失败')
    except Exception:  # noqa: BLE001
        return None


# ---- 静态收款码（2026-11-15）--------------------------------------------
# 只做「展示 + 本机配置」：不接支付 SDK、不生成订单、不判定到账、不发回调。
# 图片与说明只落数据目录（app/.runtime/payee/），不进安装包、不进日志、不上云。
# 写鉴权边界：本地便携包形态下只有运行工作台的这台电脑（loopback）能改配置，
# 局域网/手机上的客户一律只读，避免持访问码者把收款码换成自己的。
# 云端化时必须把 _payee_can_edit 换成真实的管理员会话鉴权——此处不擅自
# 发明账号或角色体系，只在注释里标明替换点。
_PAYEE_BODY_LIMIT = 8 * 1024 * 1024  # 4MB 原图 → base64 约 5.4MB，留余量


def _payee_can_edit(handler):
    probe = getattr(handler, '_client_is_loopback', None)
    return bool(probe and probe())


def _payee(handler, method, bits, query=None):
    """挂载 /api/billing/payee*；命中且已响应返回 True，未命中返回 False。

    GET  /api/billing/payee          配置状态（云端优先，附 can_edit / source）
    GET  /api/billing/payee/image    收款码图片二进制（?v= 定位云端缓存或本机旧图）
    POST /api/billing/payee/info     保存标题/联系方式/说明（仅本机、仅云端不可达时用）
    POST /api/billing/payee/image    上传或替换收款码图片（同上）
    POST /api/billing/payee/image/clear  清空图片（活树没有 do_DELETE，用 POST 子动作）
    """
    rest = bits[3:]
    try:
        if method == 'GET' and not rest:
            payload = billing_payee.get()
            # 云端统一配置后，客户端不再提供本机改配置入口；只有云端不可达回落本机时才允许。
            payload['can_edit'] = (_payee_can_edit(handler)
                                   and payload.get('source') != 'cloud')
            handler.json({'ok': True, **payload})
            return True
        if method == 'GET' and rest == ['image']:
            values = (query or {}).get('v') or ['']
            path = billing_payee.image_path(values[0] if values else '')
            if path is None:
                handler.json({'error': '还没有配置收款码。',
                              'code': 'payee_not_configured'}, 404)
                return True
            # 复用 Handler.send_file：同源 CSP、nosniff、Range 语义与全站一致。
            # image_url 带 ?v=<云端 hash 或 local-<mtime>>，换图后 URL 变化，不会命中旧缓存。
            handler.send_file(path)
            return True
        if method != 'POST':
            return False
        if not _payee_can_edit(handler):
            handler.json({'error': '收款方式只能在运行工作台的这台电脑上配置。',
                          'code': 'forbidden'}, 403)
            return True
        if rest == ['info']:
            body = handler.body()
            if not isinstance(body, dict):
                raise ValueError('请求体必须是 JSON 对象。')
            handler.json({'ok': True, **billing_payee.save_info(
                title=body.get('title'), contact=body.get('contact'),
                note=body.get('note'))})
            return True
        if rest == ['image']:
            body = handler.body(_PAYEE_BODY_LIMIT)
            if not isinstance(body, dict):
                raise ValueError('请求体必须是 JSON 对象。')
            handler.json({'ok': True, **billing_payee.save_image(body.get('image'))})
            return True
        if rest == ['image', 'clear']:
            handler.json({'ok': True, **billing_payee.clear_image()})
            return True
    except (ValueError, TypeError) as exc:
        handler.json({'error': str(exc), 'code': 'invalid_input'}, 400)
        return True
    except OSError:
        # 不把路径、图片字节或异常原文回给客户端。
        handler.json({'error': '收款方式保存失败，请检查数据目录是否可写。',
                      'code': 'payee_storage_error'}, 500)
        return True
    return False


def route(handler, method, bits, query=None):
    """挂载 /api/billing/*；命中返回 True，未命中返回 False。"""
    if len(bits) < 3 or bits[0] != 'api' or bits[1] != 'billing':
        return False
    query = query or {}
    if bits[2] == 'payee':
        return _payee(handler, method, bits, query)
    try:
        if method == 'GET' and bits[2] == 'summary':
            # ?refresh=1：跳过云端余额 ≤15s 缓存直读真源（问答结束后前端强制刷新）。
            refresh = str((query.get('refresh') or [''])[0]).lower() in ('1', 'true', 'yes')
            handler.json({'ok': True, **summary(force=refresh)})
            return True
        if method == 'GET' and bits[2] == 'usage':
            handler.json({'ok': True, **usage()})
            return True
        if method == 'GET' and bits[2] == 'membership':
            handler.json({'ok': True, 'membership': service().membership(),
                          'usage': usage()})
            return True
        if method == 'GET' and bits[2] == 'holds':
            handler.json({'ok': True, **service().open_holds()})
            return True
        if method == 'POST' and bits[2] == 'holds' and len(bits) > 3 and bits[3] == 'sweep':
            handler.json({'ok': True, **service().close_expired_holds()})
            return True
        if method == 'GET' and bits[2] == 'ledger':
            def _int(name, default):
                try:
                    return int((query.get(name) or [default])[0])
                except (TypeError, ValueError):
                    return default
            handler.json({'ok': True, **service().ledger(
                limit=_int('limit', 50), offset=_int('offset', 0))})
            return True
        if method == 'GET' and bits[2] == 'prices':
            data = service().pricing()
            handler.json({'ok': True, 'currency': data.get('currency'),
                          'credit_milli': data.get('credit_milli'),
                          'plans': data.get('plans') or {},
                          'catalog': service().catalog(),
                          'actions': service().price_book(),
                          'credit_validity': data.get('credit_validity') or {}})
            return True
        if method == 'GET' and bits[2] == 'entitlements':
            handler.json({'ok': True, 'items': service().entitlements()})
            return True
        if method == 'GET' and bits[2] == 'redeem':
            def _limit(name, default):
                try:
                    return int((query.get(name) or [default])[0])
                except (TypeError, ValueError):
                    return default
            handler.json({'ok': True,
                          'items': redeem_service.history(
                              user_id=billing_service.request_user_id(),
                              limit=_limit('limit', 20)),
                          'balance': service().balance()})
            return True
        if method == 'POST' and bits[2] == 'quote':
            data = handler.body()
            if not isinstance(data, dict):
                raise ValueError('请求体必须是 JSON 对象。')
            quote = service().quote(str(data.get('action') or ''),
                                    qty=data.get('qty', 1),
                                    item_id=data.get('item_id') or None)
            handler.json({'ok': True, 'quote': quote})
            return True
    except billing_service.BillingError as exc:
        handler.json(exc.as_dict(), 400)
        return True
    except (ValueError, TypeError) as exc:
        handler.json({'error': str(exc), 'code': 'invalid_input'}, 400)
        return True
    return False
