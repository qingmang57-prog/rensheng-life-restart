#!/usr/bin/env python3
"""人生重启系统 local development edition. Standard-library only, loopback binding."""
import argparse
import base64
import gzip
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from db import Store, Conflict, uid, now
import branding
import media
import rebuild
import providers
import local_asr
import long_text
import asr_volc
import video_intake
import intake_history
import fault_report
import install_id

# t207/ccE（队长）：业务捕获点接线。只调 record（脱敏+本地队列），flush 由守护线程统一驱动，
# 绝不阻塞主流程；同 (kind,summary) 60 秒去重防刷屏；record 自身异常只进日志不上抛。
_CCE_SEEN={}
_CCE_JOB_SEEN=set()
def _cce_record(kind,summary,**kw):
    """捕获→入队；返回 fault_id（未记录/失败=None）。
    去重键=(kind,summary,trace)：同请求 60 秒重试只记一次；不同 trace 的客户故障互不吞并。"""
    try:
        import time as _t
        trace=str(kw.get('trace_id') or fault_report.current_trace() or '')
        key=(kind,str(summary)[:120],trace)
        now=_t.time()
        if _CCE_SEEN.get(key,0)>now-60:
            return None
        _CCE_SEEN[key]=now
        if len(_CCE_SEEN)>1024:  # 该表只服务 60 秒去重窗口，长时运行防无界增长
            _CCE_SEEN.clear()
        return fault_report.record(kind=kind,summary=str(summary)[:200],
            error_code=kw.pop('error_code','CAPTURED'),stage=kw.pop('stage','http'),
            capability=kw.pop('capability','local_http'),
            actionable=kw.pop('actionable','请重试；持续失败请导出诊断包。'),**kw)
    except Exception as _e:
        try:
            applog.exception('cce.record_failed',_e,'kind={0}'.format(kind))
        except Exception:
            pass
        return None
# video_link 转写子结果分类表：status→(fault kind, stage, capability, error_code, 默认摘要)。
# needs_transcript=正常交接（等用户补音频），不算故障，不记。
_CCE_TS_KINDS={
    'download_failed':('download_failed','video_download','download','DOWNLOAD_FAILED','下载失败'),
    'audio_failed':('audio_extract_failed','video_audio','media','AUDIO_EXTRACT_FAILED','抽音频失败'),
    'asr_missing':('asr_missing','video_asr','asr','ASR_UNAVAILABLE','转写不可用'),
    'asr_failed':('asr_failed','video_asr','asr','ASR_FAILED','转写失败'),
    'empty':('transcript_empty','video_asr','asr','TRANSCRIPT_EMPTY','转写结果为空'),
}
def _cce_cloud_like(exc):
    """W2（t207）：判断一个已被 long_text 包成 ValueError 的异常是否属于「云端/网络」故障。

    long_text.run 的同次重试会把调用异常统一包成
    `ValueError('%s（同一次内容重试一次仍失败：%s）')`，原始类型丢失；
    而契约要求 llm_cloud_error 只收「云端 ≥500 / 超时」这类，不收回包成 ValueError 的输入校验。
    所以这里只按云端故障的固定文案识别（cloud_gateway.ProxyError / 超时），宁缺不滥记。
    """
    text=str(exc or '')
    return any(tok in text for tok in ('连不上云端服务','云端服务','连接超时','超时未响应','ProxyError'))
def _cce_job_fail_spec(r):
    """t207：job 终态失败按**失败发生在哪一步**归类（不再一律 download_failed）。
    步骤→kind 依《接口契约_ccE捕获点_v1》§2 枚举：
      link/collect（解析链接、打开视频页抓资料）→ browser_parse_failed
      transcript（下载/抽音/转写）→ 读 result.transcript.status，缺则按 error 文案兜底
      analysis（模型拆结构）→ llm_cloud_error
    只改分类口径，不改业务行为；未识别步骤沿用 download_failed（stage=video_job）。
    """
    err=str(r.get('error') or '任务失败')[:200]
    steps=[s for s in (r.get('steps') or []) if isinstance(s,dict) and s.get('status')=='failed']
    key=str((steps[0].get('key') if steps else '') or '')
    res=r.get('result') if isinstance(r.get('result'),dict) else {}
    tr=res.get('transcript') if isinstance(res.get('transcript'),dict) else {}
    if key in ('link','collect'):
        return {'kind':'browser_parse_failed','summary':err,'stage':'video_job',
                'error_code':'BROWSER_PARSE_FAILED','capability':'browser_fetch',
                'actionable':'视频页没打开或没解析出来：换链接或直接粘贴口播稿；持续失败请导出诊断包。'}
    if key=='analysis':
        return {'kind':'llm_cloud_error','summary':err,'stage':'video_job',
                'error_code':'VIDEO_ANALYSIS_FAILED','capability':'llm_call',
                'actionable':'模型这一步没跑通：稍后重试；持续失败请导出诊断包。'}
    if key=='transcript':
        st=str(tr.get('status') or '')
        if st=='download_failed':
            return {'kind':'download_failed','summary':str(tr.get('reason') or err)[:200],
                    'stage':'video_job','error_code':'DOWNLOAD_FAILED','capability':'video_download',
                    'actionable':'视频没下载下来：可重试或换链接；持续失败请导出诊断包。'}
        if st=='asr_failed':
            return {'kind':'asr_failed','summary':str(tr.get('reason') or err)[:200],
                    'stage':'video_job','error_code':'ASR_FAILED','capability':'asr',
                    'actionable':'口播没转成文字：可粘贴原文兜底；持续失败请导出诊断包。'}
        if st=='asr_missing':
            return {'kind':'asr_failed','summary':str(tr.get('reason') or err)[:200],
                    'stage':'video_job','error_code':'ASR_UNAVAILABLE','capability':'asr',
                    'actionable':'本机转写暂时不可用：等上一条转写结束后重试；持续失败请导出诊断包。'}
    return {'kind':'download_failed','summary':err,'stage':'video_job',
            'error_code':'JOB_FAILED','capability':'video_download',
            'actionable':'任务失败：可重试或换链接；持续失败请导出诊断包。'}

def _cce_job_fail(r):
    """视频/转写异步 job 终态→按真实阶段分类入队，返回本次 fault_id（无则 None）。
    job 整体 failed→download_failed；done+transcript 子结果失败→按 _CCE_TS_KINDS 分类。
    一个 job 一个阶段只记一次；同一 job 不重复记同阶段。"""
    try:
        if not isinstance(r,dict):
            return None
        jid=str(r.get('id') or r.get('job') or '')
        trace=str(r.get('trace_id') or '')
        if r.get('status')=='failed':
            if jid and ('job:'+jid) not in _CCE_JOB_SEEN:
                _CCE_JOB_SEEN.add('job:'+jid)
                _spec=_cce_job_fail_spec(r)
                return _cce_record(_spec['kind'],_spec['summary'],trace_id=trace,
                    stage=_spec['stage'],error_code=_spec['error_code'],capability=_spec['capability'],
                    job_id=jid or None,actionable=_spec['actionable'])
        elif r.get('status')=='done':
            res=r.get('result') if isinstance(r.get('result'),dict) else {}
            tr=res.get('transcript') if isinstance(res.get('transcript'),dict) else {}
            st=str(tr.get('status') or '')
            spec=_CCE_TS_KINDS.get(st)
            if spec and jid and ('ts:'+jid+':'+st) not in _CCE_JOB_SEEN:
                _CCE_JOB_SEEN.add('ts:'+jid+':'+st)
                kind,stage,cap,code,title=spec
                return _cce_record(kind,str(tr.get('reason') or title)[:200],
                    trace_id=trace,stage=stage,error_code=code,capability=cap,
                    job_id=jid or None,
                    actionable='可在「上传/粘贴」入口手动补充；持续失败请导出诊断包。')
        return None
    except Exception:
        return None
import anchor_job
import director_pass
import packaging_job
import smart_packaging
import creation_service
import flow_service
import graph_view
import wash_service
import resolver_status
import video_link
import bench_service
import content_capture
import douyin_session
import platform_session
import matrix_service
import release_service
import publish_guide
import clip_export
import compliance_route
import feedback_service
import feedback_import
import rate_limit
import collect_schedule
import clip_import
import cover_packaging
import avatar_service
import caption_style
import style_lint
import auto_wash
import read_script
import applog
import backup_restore
import diagnostics
import compliance
import style_profile
import trial_gate
import license
import clone_guard
import billing_service
import billing_route
import mp_bridge
import checkin
import checkin_cloud
import growth_service
import auth_service


def _cloud_retry_note(note):
    # 差网络修复（2026-11-21）：云调用自动重试时在控制台可见，不静默。
    print('[云端]' + note, flush=True)


auth_service.set_retry_listener(_cloud_retry_note)
import member_pay
import cloud_gateway
import secret_store
import content_index
import insight_card
import share_service
import brain_ask
import interview
import prompt_store
import asset_store

ROOT=Path(__file__).resolve().parent
BASE=ROOT.parent


def static_path_allowed(rel):
    """静态目录只服务交付面文件：隐藏目录与 *.bak-* 历史备份一律拒绝。

    2026-11-07：这些备份不进交付包（scan_forbidden.ship_included 已排除），
    但源码树直启时旧路由只校验目录穿越，URL 可直访 /.bak-*/index.html
    看到历史「本机开发版」文案。path 在 L1572 已 unquote，不存在 %2e 绕过。
    与打包规则对齐，且是纯函数，可被负例自测用 AST 提取后独立执行。
    """
    return not any(seg.startswith('.') or '.bak-' in seg or seg.endswith('.bak')
                   for seg in rel.split('/'))


def knowledge_in_category(item, wanted):
    """分类筛选兼容旧词：按这条记录自己的旧分类/别名命中，不把整类旧词都算进来。"""
    category, legacy = content_index.normalize_category(item.get('category'), item.get('category_legacy'))
    names = {category, legacy, *(item.get('category_aliases') or ())}
    return wanted in names


def knowledge_active(item):
    """2026-11-16（归档口径）：archived / superseded 都算「不在用」，缺字段按 active。

    认知列表默认只显示在用的卡，与认知图谱（content_index 只取 active）保持同一口径；
    需要看归档时显式带 include_archived=1。
    """
    return (item.get('lifecycle') or 'active') == 'active'


# 配置来源（低 → 高）：包根 .env.local（空模板）→ app/.env.local（开发机）→ 数据目录 .env.local（用户填写）→ 进程环境变量。
# 用户填的 Key 只写数据目录（默认 app\.runtime\.env.local）：不进包、不随包分发，换版本/重装不丢。
# 包根 .env.local 只是空模板，程序永不写入它；环境变量优先级最高（真实 env 覆盖文件）。
CONFIG_FILES=[BASE/'.env.local',ROOT/'.env.local']


def _read_config_file(path):
    items=[]
    if not path.is_file() or os.getenv('IP_SKIP_LOCAL_CONFIG')=='1':
        return items
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        name,sep,value=line.partition('=')
        if sep and name.strip().startswith('IP_') and value.strip():
            items.append((name.strip(),value.strip().strip('\"\'')))
    return items


_FILE_CONFIG=[]
for _cfg in CONFIG_FILES:
    _FILE_CONFIG.extend(_read_config_file(_cfg))
# 先定位数据目录，才能找到放在数据目录里的用户配置；其余项留到下面统一按优先级应用。
for _name,_value in _FILE_CONFIG:
    if _name=='IP_DATA_DIR':
        os.environ.setdefault(_name,_value)
        break

def _resolve_config_path(name,default,must_exist=True):
    raw=(os.getenv(name,'') or '').strip()
    if raw:
        cand=[Path(raw)]
        if not Path(raw).is_absolute():
            cand.insert(0,BASE/raw)
        for p in cand:
            if p.exists():
                os.environ[name]=str(p)
                return str(p)
        if must_exist:
            print('[配置] %s 路径不存在，回落到包内：%s' % (name,raw),flush=True)
    d=Path(default)
    os.environ[name]=str(d)
    return str(d)

# 数据目录必须先定：它同时是「用户自己填的配置」的存放位置。
_resolve_config_path('IP_DATA_DIR',ROOT/'.runtime',False)
USER_CONFIG_FILE=Path(os.getenv('IP_DATA_DIR'))/'.env.local'
_USER_CONFIG=_read_config_file(USER_CONFIG_FILE)
# setdefault 语义：先写低优先级、后写高优先级；已存在的进程环境变量始终最高。
for _name,_value in _FILE_CONFIG:
    os.environ.setdefault(_name,_value)
for _name,_value in _USER_CONFIG:
    os.environ.setdefault(_name,_value)

_PATH_DEFAULTS={
    'IP_LOCAL_ASR_ROOT':(ROOT/'.runtime'/'asr-workshop',False),
    'IP_FFMPEG':(BASE/'tools'/'ffmpeg'/'bin'/'ffmpeg.exe',True),
    'IP_FFPROBE':(BASE/'tools'/'ffmpeg'/'bin'/'ffprobe.exe',True),
}
for _k,(_default,_must) in _PATH_DEFAULTS.items():
    _resolve_config_path(_k,_default,_must)
print('[配置] 实际路径 DATA=%s FFMPEG=%s ASR_ROOT=%s' % (
    os.getenv('IP_DATA_DIR'),os.getenv('IP_FFMPEG'),os.getenv('IP_LOCAL_ASR_ROOT')),flush=True)
print('[配置] 用户配置 %s（存在=%s，不进包）' % (USER_CONFIG_FILE,USER_CONFIG_FILE.is_file()),flush=True)
# 密钥来源只报「环境变量 / 文件 / 本机配置」和是否为空，绝不打印内容。
# 2026-11-16：默认代理模式（IP_KEY_SOURCE=proxy）下不读本机 Key 文件；
# 只有回滚模式 IP_KEY_SOURCE=local 才走旧的「读 .secrets 文件」路径。
try:
    if (os.getenv('IP_KEY_SOURCE') or 'proxy').strip().lower() in ('local','direct','file','byok'):
        _llm_key,_llm_src=secret_store.read(('IP_DEEPSEEK_KEY','IP_LLM_KEY'),'deepseek.key')
        _volc_key,_volc_src=secret_store.read(('IP_VOLC_ASR_KEY',),'volc_asr.key')
        print('[密钥] %s；%s' % (secret_store.describe('大模型',_llm_key,_llm_src),
                                 secret_store.describe('火山ASR',_volc_key,_volc_src)),flush=True)
        del _llm_key,_volc_key
    else:
        print('[密钥] 代理模式：本机不读取、不保存上游 Key（由云端中控统一下发与轮换）',flush=True)
except Exception:
    pass
DATA=Path(os.getenv('IP_DATA_DIR',str(ROOT/'.runtime')))
STORE=Store(DATA)
# 列表 GET 暖缓存：HDD 冷全表扫实测 bench/accounts 5.12s、projects 3.22s；TTL 内且相关 kind 无写入才命中。
GET_CACHE={}
GET_CACHE_LOCK=threading.Lock()
GET_CACHE_TTL=45.0
def cached_get(key,kinds,build):
    ver=STORE.versions(kinds)
    now=time.monotonic()
    with GET_CACHE_LOCK:
        hit=GET_CACHE.get(key)
        if hit and hit[0]>now and hit[1]==ver:
            return hit[2]
    data=build()
    with GET_CACHE_LOCK:
        GET_CACHE[key]=(time.monotonic()+GET_CACHE_TTL,ver,data)
    return data
prompt_store.start(STORE)
# 2026-11-17（t42）：题库/词库等核心资产同样在启动时拉一次（登录态→DPAPI 密封缓存）
try:
    asset_store.start(STORE)
except Exception as _asset_exc:  # 资产层问题不能拦住应用启动
    logging.getLogger('asset_store').warning('资产初始化失败：%s', _asset_exc)

# ---- 客户端空壳（2026-11-17 t38）：B7 激活配额 + B3 离线 72h ----
# 只拦「核心生成」POST；查看/导出/打卡/档案保存/登录注册一律放行。
SHELL_CORE_PREFIXES = (
    '/api/creation', '/api/wash', '/api/bench', '/api/brain', '/api/interview',
    '/api/feedback', '/api/director', '/api/packaging', '/api/eva', '/api/style',
    '/api/video', '/api/subtitle', '/api/compliance', '/api/flow', '/api/anchor',
    '/api/avatar', '/api/llm', '/api/asr', '/api/rewrite', '/api/reference',
    '/api/knowledge', '/api/mentor', '/api/prepare', '/api/jobs',
)
# t196：只读抓取/采集类端点（读别人的公开正文与公开数据），与「核心生成」分开。
# 清单里只有「读」这一步；改写/生成（/api/wash/stage1、/api/wash/stage2）不在其中。
SHELL_EXTRACT_PATHS = (
    '/api/wash/link', '/api/wash/classify',
    '/api/wash/video/start', '/api/wash/video/status',
    '/api/bench/accounts', '/api/feedback/register-work',
)
SHELL_EXTRACT_PREFIXES = ('/api/bench/accounts/',)  # 采集：/api/bench/accounts/<id>/collect


def shell_gate(method, path):
    """核心生成前的空壳闸：超配额 / 离线超 72h / 未登录且无云端模板 → 403 可读文案。"""
    if method not in ('POST', 'PATCH', 'PUT'):
        return None
    clean = path.split('?', 1)[0].rstrip('/')
    # t176：导出/备份不属于「核心生成」——与闸门自己的口径（已有内容仍可查看和导出）保持一致，
    # 不能因为离线/未登录把导出也拦掉，否则作业化后前端只会拿到 403。
    if clean.startswith('/api/brain/export') or clean.startswith('/api/backup/export'):
        return None
    # 2026-11-25（G.2.1）：定位已导出的文件不是「生成」——导出本身不要求登录，打开它所在的文件夹也不该要求。
    if clean.startswith('/api/jobs/') and clean.endswith('/reveal'):
        return None
    # 2026-11-25 资料外置：打开自己的资料文件夹不是「生成」，不要求登录。
    if clean=='/api/data-home/reveal':
        return None
    # 2026-11-25（需求单 H）：知识卡的卡坯 / 设置 / 存图 / 定位都是本地计算与文件操作，不是「核心生成」；
    # 调模型的精修（/api/knowledge-card/refine）和存进知识库（/api/knowledge）照旧要求登录。
    if clean in ('/api/knowledge-card/draft','/api/knowledge-card/pref',
                 '/api/knowledge-card/image','/api/knowledge-card/reveal'):
        return None
    # t196（2026-11-24）：读原文/公开数据采集不是「生成」，不占模板额度；模板没到位时也不能
    # 挡在读取前面，否则用户看到的是「需登录模板」却以为「这个链接读不出来」。
    # 有效权限检查一处没少：会员/试用门禁（trial_gate）在前面照旧，生成步骤自己的模板门禁
    # （/api/wash/stage1|stage2 的 wash.stage1/wash.rules 等）也照旧要求登录后云端下发的模板。
    if clean in SHELL_EXTRACT_PATHS or clean.startswith(SHELL_EXTRACT_PREFIXES):
        return None
    if not any(clean.startswith(prefix) for prefix in SHELL_CORE_PREFIXES):
        return None
    try:
        status = auth_service.shell_status(STORE)
    except Exception:
        return None
    activation = status.get('activation') or {}
    if activation.get('state') == 'blocked':
        return {'error': activation.get('message') or
                '本账号激活设备已达上限，核心生成已停用；已有内容仍可查看和导出。',
                'code': 'activation_blocked', 'shell': status}
    offline = status.get('offline') or {}
    if offline.get('stale'):
        return {'error': offline.get('message'), 'code': 'offline_stale', 'shell': status}
    try:
        logged_in = auth_service.has_session(STORE)
    except Exception:
        logged_in = False
    if not logged_in:
        # 空壳口径：未登录没有任何核心生成能力（模板只在登录后由云端下发，缓存也不放行），
        # 查看/导出/打卡/档案保存照旧。
        return {'error': '需登录云端获取功能模板后再使用；已保存的内容仍可查看和导出。',
                'code': 'prompt_login_required', 'shell': status}
    return None
# 统一内容索引：启动只建表并判断是否过期，重建放后台线程，不占启动时间；
# 重建完成前检索走 LIKE 兜底，不会出现「刚装上搜不到」。
_INDEX_BOOT=content_index.bootstrap(STORE)
print('[索引] 启动检查 %d ms；索引%s；后台重建=%s' % (
    _INDEX_BOOT['schema_ms'],'已是最新' if _INDEX_BOOT['up_to_date'] else '需要重建',
    '已启动' if _INDEX_BOOT['rebuild_started'] else '未启动'),flush=True)
# 打卡云端的失败标记在后台补报，不占启动主路径；没登录时不发请求。
checkin_cloud.warmup(STORE)
for folder in ('uploads','artifacts','packages'):
    (DATA/folder).mkdir(exist_ok=True)
# 与 tools/boot.py 的首启行为保持一致：正常启动时 boot 已写过 trial.json，
# 这里再跑一次是幂等的；直接 import app 的调用方（回归测试、诊断脚本）也能拿到试用档，
# 否则试用闸门会把所有写请求判成“记录缺失”只读。
try:
    license.initialize()
except Exception:
    pass
# 授权状态以云端账号为准：会员/试用/试用到期三态由 /api/auth/me 下发后缓存，
# 未登录或老账号（无试用字段）仍按本地兜底，避免存量用户被一次性锁成只读。
license.set_membership_provider(auth_service.current_membership)
POOL=ThreadPoolExecutor(max_workers=2,thread_name_prefix='ip-job')
UPLOAD_LOCK=threading.Lock()
INTAKE_LOCK=threading.Lock()
CONTENT_LOCK=threading.Lock()
JOB_LOCK=threading.Lock()
# 恢复会替换整库并移动媒体目录，同一时间只允许一个；并发进来直接 409。
RESTORE_LOCK=threading.Lock()
PART_LABELS={'hook':'开头','content':'主体','anchor':'让人记住的那一句','quote':'值得被转发的一句话','ending':'结尾'}
CORE_ROLES=('hook','content','ending')
ROLES=[(r,PART_LABELS[r]) for r in ('hook','content','anchor','quote','ending')]
SEMANTIC_TYPES={'repeat':'原样重说','restart':'半句改口','filler':'有声口误'}
SEMANTIC_WINDOW_WORDS=500
SEMANTIC_WINDOW_OVERLAP=100
CONTEXT_FILLER={'啊','那个','就是说','然后','其实','所以','那么','就是'}
SEMANTIC_OBSERVE_TIER_B=(os.getenv('SEMANTIC_OBSERVE_TIER_B','1')!='0')
SEMANTIC_TIER_B_ENFORCE=(os.getenv('SEMANTIC_TIER_B_ENFORCE','1')!='0')
SEMANTIC_MAX_DELETIONS=40
SEMANTIC_MAX_RATIO=0.35
# 一键净剪只自动删这批“纯语气词”；“啊/哦/那个/就是说”等可能承载语义，只做建议不自动删。
# “额”不进自动词表：ASR 会把“巨额”拆成“巨/额/的”，自动删“额”会剪出“巨的亏损”。
AUTO_FILLER_LEXICON={'嗯','呃','唔','唉','哎呀','嗯嗯','呃呃'}
FILLER_ONLY_A={'呃','嗯','啊','唉','哦','呀','哎','诶','嗷','唔',
              '那个','这个','就是','然后','所以','那么','其实','好吧','对对对','是是是'}
FILLER_PRONOUN={'那个','这个'}


def semantic_filler_tierA_ok(word_ids,lookup):
    ids=[k for k in (word_ids or []) if k]
    if not ids:return False
    texts=[semantic_word_plain(lookup.get(k,{}).get('text','')) for k in ids]
    if any(not t or t not in FILLER_ONLY_A for t in texts):return False
    if any(t in FILLER_PRONOUN for t in texts):return False
    return sum(len(t) for t in texts)<=4


# LAN mode: when the server binds beyond loopback, every /api request from a non-loopback
# client must present the access token. Set in main() before serve_forever, then read-only.
LAN_MODE=False
LAN_TOKEN=''
# 手机访问开关：默认开启；关闭后非回环客户端一律得到 lan_disabled，电脑本机不受影响。
LAN_ENABLED=True
LAN_SETTING_FILE=DATA/'lan-enabled.json'
# 直启 app.py 时的安装绑定状态；main() 校验后写入，route() 只读闸门复用。
CLONE_STATE={}
# 封面解锁的真实扣费挂点（billing_service 账本）；必须在任何 unlock 之前接上。
cover_packaging.LEDGER_HOOK=billing_route.charge_cover_template


def _load_lan_enabled():
    try:
        return bool(json.loads(LAN_SETTING_FILE.read_text(encoding='utf-8')).get('enabled',True))
    except Exception:
        return True


def _save_lan_enabled(enabled):
    LAN_SETTING_FILE.parent.mkdir(parents=True,exist_ok=True)
    LAN_SETTING_FILE.write_text(json.dumps({'enabled':bool(enabled)},ensure_ascii=False),encoding='utf-8')

# 扫码登录窗口只拉起一次；进程句柄只留内存，cookie 值不进日志、不进响应。
DOUYIN_LOGIN_LOCK=threading.Lock()
DOUYIN_LOGIN_PROC={'proc':None,'started_at':None}
VOICE_MIME_EXT={'audio/webm':'webm','audio/ogg':'ogg','audio/mp4':'m4a','audio/mpeg':'mp3',
                'audio/wav':'wav','audio/x-wav':'wav','audio/aac':'aac'}


def flow_transcribe(payload):
    """POST /api/flow/transcribe：长语音走本地 ASR（content_capture），失败给人话。"""
    data=str(payload.get('audio') or '').strip()
    if not data:
        return {'ok':False,'reason':'没有收到录音数据，请重录；也可以直接打字。'}
    if data.startswith('data:'):
        data=data.split(',',1)[-1]
    mime=str(payload.get('mime') or 'audio/webm').split(';')[0].strip().lower()
    ext=VOICE_MIME_EXT.get(mime,'webm')
    try:
        raw=base64.b64decode(data)
    except Exception:
        return {'ok':False,'reason':'录音数据不完整，请重录一次。'}
    if len(raw)>8*1024*1024:
        return {'ok':False,'reason':'这段录音太长了，建议分两段录。'}
    work=DATA/'tmp';work.mkdir(parents=True,exist_ok=True)
    path=work/(uid('flowvoice')+'.'+ext)
    path.write_bytes(raw)
    try:
        result=content_capture.capture(path,mode='asr')
    except Exception as exc:
        return {'ok':False,'reason':'本地转写失败（%s）。可以直接打字。'%str(exc)[:120]}
    text=str(result.get('text') or '').strip()
    if not text:
        message=str(result.get('message') or result.get('error') or '没识别出文字，请再试一次。')
        return {'ok':False,'reason':message[:200]}
    return {'ok':True,'text':text[:6000]}


def douyin_login_state():
    """GET /api/douyin/login 的固定契约：configured / last_ok_at / hint。

    cookie 只从 app/.runtime/douyin_cookie.json 读，这里不返回任何 cookie 值。
    """
    _cookies,state=douyin_session.load()
    path=douyin_session.COOKIE_FILE
    last_ok=None
    if path.is_file():
        try:
            last_ok=datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec='seconds')
        except OSError:
            last_ok=None
    return {'configured':state.get('state')=='ok','last_ok_at':last_ok,
            'hint':'扫码登录一次，cookie 只存本机',
            'state':state.get('state'),'reason':(state.get('reason') or '')[:200]}


def open_douyin_login():
    """POST /api/douyin/login/open：拉起 tools/douyin_login.py 的扫码窗口。"""
    python=video_intake.browser_python()
    script=ROOT.parent/'tools'/'douyin_login.py'
    if not python.is_file():
        return {'ok':False,'message':'扫码登录组件不完整（浏览器环境未安装）。下一步：重新解压便携包，或改用「上传视频/录音」拿文案。'}
    if not script.is_file():
        return {'ok':False,'message':'缺少登录脚本 tools\\douyin_login.py，请重新解压便携包。'}
    with DOUYIN_LOGIN_LOCK:
        proc=DOUYIN_LOGIN_PROC.get('proc')
        if proc is not None and proc.poll() is None:
            return {'ok':True,'opened':True,'already':True,'message':'登录窗口已经开着，请在窗口里扫码。'}
        env=os.environ.copy()
        env['IP_DATA_DIR']=str(DATA)
        scratch=DATA/'tmp';scratch.mkdir(parents=True,exist_ok=True)
        env['TEMP']=env['TMP']=str(scratch)
        try:
            proc=subprocess.Popen([str(python),str(script)],cwd=str(ROOT.parent),env=env,
                                  stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError as exc:
            return {'ok':False,'message':'登录窗口没能打开（%s）。下一步：重试一次，或改用「上传视频/录音」拿文案。'
                    % str(exc).strip()[:120]}
        DOUYIN_LOGIN_PROC.update(proc=proc,started_at=now())
    return {'ok':True,'opened':True}


_CCE_IMAGE_EXT=('.jpg','.jpeg','.png','.webp','.bmp','.gif','.tif','.tiff','.heic')
def _cce_capture_spec(job):
    """t207：capture_file 终态失败→按 mode/扩展名区分 ocr_failed 与 asr_failed（契约 §2）。
    mode=ocr 或图片扩展名=走画面字幕（OCR）；其余音视频=语音转写（ASR）。"""
    inputs=job.get('inputs') if isinstance(job.get('inputs'),dict) else {}
    mode=str(inputs.get('mode') or '').lower()
    name=str(inputs.get('name') or inputs.get('path') or '').lower()
    is_image=name.endswith(_CCE_IMAGE_EXT)
    if mode=='ocr' or (mode!='asr' and is_image):
        return ('ocr_failed','capture_file','ocr','OCR_FAILED',
                '画面字幕识别失败：可换清晰截图重试，或直接粘贴原文；持续失败请导出诊断包。')
    return ('asr_failed','capture_file','asr','ASR_FAILED',
            '语音转写失败：请重试或换音频文件；持续失败请导出诊断包。')

def capture_file_job(data,loopback):
    """POST /api/capture/file：吃本地音/视频 → 文案（异步 job，结果含 text/source/segments/warnings）。"""
    if not isinstance(data,dict):
        raise ValueError('请求体必须是 JSON 对象。')
    mode=str(data.get('mode') or 'auto').lower()
    if mode not in ('auto','asr','ocr'):
        raise ValueError('识别方式只能是 auto（先语音转写，空了再看画面字幕）、asr（只转语音）或 ocr（只识别画面字幕）。')
    if data.get('asset_id'):
        asset=require('asset',str(data['asset_id']))
        src=Path(str(asset.get('path') or ''))
    elif data.get('path'):
        if not loopback:
            raise PermissionError('局域网模式下只能上传文件，不能直接读取本机路径。')
        src=Path(str(data['path'])).expanduser()
    else:
        raise ValueError('请给出要转写的文件：本机路径 path，或已上传文件的 asset_id。')
    if not src.is_file():
        raise ValueError('找不到这个文件：%s。请确认路径正确，或重新选择文件。' % src)
    ext=src.suffix.lower()
    if ext not in content_capture.SUPPORTED:
        raise ValueError('这个格式（%s）暂时不支持。音频可用 mp3/m4a/wav/amr/aac/flac/ogg；'
                         '视频可用 mp4/mov/mkv/webm/avi 等；图片可用 jpg/png/webp/bmp 等。'
                         % (ext or '无扩展名'))
    inputs={'path':str(src),'mode':mode,'name':src.name,'size':src.stat().st_size}
    def work(job_id):
        STORE.patch('job',job_id,{'stage':'读取文件信息'})
        try:
            result=content_capture.capture(str(src),mode=mode,
                                           stage=lambda s:STORE.patch('job',job_id,{'stage':s}))
        except content_capture.CaptureError as exc:
            raise ValueError(str(exc)) from None
        record,created=content_capture.save_to_store(STORE,result)
        warnings=[]
        if (result.get('asr') or {}).get('error'):
            warnings.append(result['asr']['error'])
        if (result.get('ocr') or {}).get('error'):
            warnings.append(result['ocr']['error'])
        route={'asr':'语音转写','ocr':'画面字幕','mixed':'语音+画面字幕','image_ocr':'图片文字'}
        return {'stage':'已转成文案（%s）' % route.get(result['source'],result['source']),
                'text':result['text'],'source':result['source'],'segments':result['segments'],
                'warnings':warnings,'confidence_note':result['confidence_note'],
                'file':result['file'],'asr':result.get('asr'),'ocr':result.get('ocr'),
                'work_id':record['id'],'work_title':record.get('title'),'work_created':created}
    return new_job(None,'capture_file',inputs,work)


def capture_wait(job_id,timeout=1800):
    """转写是分钟级任务：默认同步等结果；前端想显示进度可传 wait:false 走轮询。"""
    import time as _time
    deadline=_time.monotonic()+timeout
    while True:
        job=STORE.get('job',job_id)
        if job and job.get('status') not in ('queued','running'):
            return job
        if _time.monotonic()>deadline:
            return job
        _time.sleep(1.0)



class TokenRequired(PermissionError):
    pass


class LanDisabled(PermissionError):
    pass


def is_private_host(host):
    if host in ('127.0.0.1', 'localhost', '::1', '[::1]'):
        return True
    if host.startswith('192.168.') or host.startswith('10.'):
        return True
    if host.startswith('172.'):
        try:
            return 16 <= int(host.split('.')[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def require(kind,key):
    item=STORE.get(kind,key)
    if item is None:
        raise KeyError('没有找到该记录。')
    return item


def _lan_url(port):
    """本机局域网地址，供手机上直接打开。取不到时不报错，返回占位提示。"""
    ip=''
    try:
        s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8',80))
            ip=s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        ip=''
    return 'http://%s:%s'%((ip or '<本机局域网IP>'),port)


def _cap(fn,default):
    """单项能力探测失败只降级该项，不让 /api/bootstrap 整体失败。

    典型场景：从映射网络盘（如 V:）启动时 Path.resolve() 得到 UNC 路径，
    Node 读不到父级 package.json，封面模板库探测抛错；旧逻辑会让整页打不开。
    """
    try:
        return fn()
    except Exception:
        return default


# 2026-11-16（性能 D）：能力探测带 60 秒记忆。bootstrap/首屏与多个接口会反复调用，
# 每次都递归扫盘 + 起子进程探测（打包能力单项 10~66ms），是首屏慢的主要后端开销之一。
# 探测结果 60 秒内基本不变，允许短时陈旧；需要即时状态可传 force=True。
CAP_TTL=60
CAP_CACHE={'at':0.0,'data':None}
CAP_LOCK=threading.Lock()
CAP_BUSY={'on':False}


def _cap_refresh():
    """后台重探一次能力，不阻塞当前请求；失败就保留旧值。

    返回本次重探的完成事件：设置页用它最多等 2 秒拿真实结果（见 capabilities_fresh）。
    """
    with CAP_LOCK:
        if CAP_BUSY['on']:
            return CAP_BUSY.get('done')
        done=threading.Event()
        CAP_BUSY['on']=True
        CAP_BUSY['done']=done
    def run():
        try:
            data=_probe_capabilities()
            with CAP_LOCK:
                old=CAP_CACHE['data']
                CAP_CACHE['data']=data
                CAP_CACHE['at']=time.monotonic()
            # 2026-11-16（性能 E-2）：重探结果与旧值不同就留一条日志（只记键名，不打印密钥）。
            if old:
                changed=sorted(k for k in set(old)|set(data) if old.get(k)!=data.get(k))
                if changed:
                    print('[能力] 后台重探结果变化：%s' % ', '.join(changed),flush=True)
        except Exception:
            traceback.print_exc()
        finally:
            with CAP_LOCK:
                CAP_BUSY['on']=False
            done.set()
    threading.Thread(target=run,daemon=True).start()
    return done


def capabilities(force=False):
    hit=CAP_CACHE['data']
    if hit is not None and not force:
        if time.monotonic()-CAP_CACHE['at']<CAP_TTL:
            return hit
        # 2026-11-16（性能 E-2）：过期不阻塞。先把上次结果交出去，后台再探，
        # 否则冷启动直达深链的第一次请求要替探测白等 70ms（实测 96ms vs 25ms）。
        _cap_refresh()
        return hit
    with CAP_LOCK:
        hit=CAP_CACHE['data']
        if not force and hit is not None and time.monotonic()-CAP_CACHE['at']<CAP_TTL:
            return hit
        data=_probe_capabilities()
        CAP_CACHE['data']=data
        CAP_CACHE['at']=time.monotonic()
        return data


def capabilities_fresh(wait=2.0):
    """设置页专用：缓存新鲜就直接给；过期则等本次重探最多 2 秒，尽量给真实结果。

    返回 (能力字典, 是否仍为旧值)。超过 wait 仍未探完才回退旧值，并留日志；
    正常探测约 70ms，不会出现「显示已配置但其实抓不到」的长窗口。
    """
    hit=CAP_CACHE['data']
    if hit is not None and time.monotonic()-CAP_CACHE['at']<CAP_TTL:
        return hit,False
    done=_cap_refresh()
    if done is not None and done.wait(wait):
        data=CAP_CACHE['data']
        if data is not None:
            return data,False
    if hit is None:
        return capabilities(force=True),False
    print('[能力] 重探超过 %.0f 秒仍未完成，本次先返回上次结果' % wait,flush=True)
    return hit,True


def _cap_invalidate():
    """配置刚变（例如保存 Key）：让下一次能力查询立即重探，而不是等满 60 秒 TTL。"""
    CAP_CACHE['at']=0.0
    _cap_refresh()


def news_refresh_capability():
    """2026-11-17 t132：新闻刷新的能力预检。返回 (capability, degraded_payload)。

    缺少链接解析能力时不抛 500，返回友好降级体供路由直接回给前端。
    """
    cap = _cap(resolver_status.snapshot,
               {'ok': False, 'note': '还没做过实测抓取，稍后自动重试。'})
    if cap.get('ok'):
        return cap, None
    return cap, {'ok': False, 'degraded': True, 'code': 'capability_missing',
                 'error': '新闻刷新需要本机链接解析能力，当前还没就绪；先不自动刷新。',
                 'hint': cap.get('note') or '稍后会自动重试，也可以直接粘贴链接试一次。'}


def _probe_capabilities():
    motion_python=Path(os.getenv('IP_MOTION_PYTHON',str(BASE/'tools'/'motion-python'/'Scripts'/'python.exe')))
    adapter=Path(os.getenv('IP_MOTION_ADAPTER',str(BASE/'tools'/'motion-adapter'/'pack.py')))
    publish_ready=video_intake.browser_python().is_file() and (ROOT/'publish_worker.py').is_file()
    cover=_cap(cover_packaging.available,False)
    # 链接解析：只认真实抓取成功的记录（wash_service 实测 / 用户真抓过一篇），不按环境变量写死。
    resolver=_cap(resolver_status.snapshot,
                  {'ok':False,'note':'还没做过实测抓取，稍后自动重试；也可以直接粘贴一个链接试一次。'})
    return {**_cap(providers.available,{}),'ffmpeg':bool(_cap(lambda:media.binary('ffmpeg'),None)),
            'ffprobe':bool(_cap(lambda:media.binary('ffprobe'),None)),
            'motion_installed':adapter.is_file() and motion_python.is_file(),
            'motion_format':'16:9 · 本地包 0.2.7，竖屏尚未验证',
            'cover_packaging':cover,'cover_missing':_cap(cover_packaging.missing,[] if cover else ['封面模板库']),
            'caption_fonts':_cap(caption_style.choices,[]),
            'caption_presets':_cap(caption_style.preset_choices,[]),
            'publishing':publish_ready,'public_resolver':bool(resolver.get('ok')),
            'resolver':bool(resolver.get('ok')),'resolver_note':resolver.get('note') or '',
            'edition':'local_single_owner',
            'browser':_cap(lambda:__import__('browser_fetch').browser_status(),
                           {'available':False,'executable':'','message':'浏览器状态探测失败'}),
            'local_asr_ready':_cap(lambda:local_asr.preflight()['ok'],False),
            **_cap(smart_packaging.capabilities,{})}


ASR_WARM={'status':'idle','stage':'还没预热','message':'','seconds':None,'updated_at':None}
ASR_WARM_LOCK=threading.Lock()


def asr_status():
    caps=providers.available()
    local=local_asr.preflight();help_text=asr_volc.setup_help()
    local_ok=local_asr.available();allowed=providers.cloud_allowed()
    cloud_cfg=providers.cloud_config();cloud=providers.cloud_provider();mode=providers.asr_mode()
    cloud_name='火山引擎' if cloud_cfg=='volc' else ('外部适配器' if cloud_cfg=='external' else '云端')
    reasons='；'.join(str(x) for x in (local.get('problems') or []))
    if not reasons and not local_ok:
        reasons='本地语音识别已被关闭（IP_DISABLE_LOCAL_ASR=1）'
    reasons=reasons or '原因未记录'
    # hint 按真实状态生成：本地可用就说本地，云端有 Key 且账号放行才说云端，
    # 账号未开通（allow_cloud_asr=false）时明确说是权限，不报“已就绪”，也不说成“没配 Key”。
    if mode=='local':
        hint='当前只用本地：本地语音识别可用（模型在包内）。' if local_ok else \
             '当前只用本地，但本地不可用：%s。可改成 auto 或 cloud 并填火山引擎 Key。'%reasons
    elif mode=='cloud':
        if not allowed:
            hint='当前只用云端，但%s'%providers.CLOUD_ASR_BLOCKED
        elif cloud_cfg:
            hint='当前只用云端：%s已就绪。'%cloud_name
        else:
            hint='当前只用云端，但还没有 Key；本地%s。'%('可用' if local_ok else '也不可用（%s）'%reasons)
    elif local_ok and cloud:
        hint='本地语音识别可用，云端%s已就绪；auto 模式先走本地，失败自动回落云端。'%cloud_name
    elif local_ok and not allowed:
        hint='本地语音识别可用（模型在包内，离线也能转写）。云端通道已按账号权益关闭：%s'%providers.CLOUD_ASR_BLOCKED
    elif local_ok:
        hint='本地语音识别可用（模型在包内，离线也能转写）；云端未配置，不影响使用。'
    elif cloud:
        hint='云端语音识别已就绪（%s）；本地不可用：%s。'%(cloud_name,reasons)
    elif not allowed:
        hint='语音识别暂不可用：本机不可用（%s）；%s'%(reasons,providers.CLOUD_ASR_BLOCKED)
    else:
        if local.get('memory_low'):
            hint='语音识别暂不可用：内存不足（可用 %s MB，本地转写至少需要 %s MB）；云端也没有 Key。关闭占用内存的程序后重试，或按下面的指引填火山引擎 Key。'%(
                local.get('free_mb','未知'),local.get('need_mb','未知'))
        else:
            hint='语音识别未配置：本地不可用（%s），云端也没有 Key。可按下面的指引填火山引擎 Key，或换用带完整语音模型的安装包。'%reasons
    return {'available':caps['asr'],'mode':mode,'policy':providers.asr_policy(),'modes':list(providers.ASR_MODES),
            'local':{**local,'ready':local_ok},
            # provider 只报“实际可用的云端通道”（账号未开通时为 none）；configured/allowed 分开报原因。
            'cloud':{'volc':providers.volc_asr(),'external':providers.external_asr(),
                     'provider':cloud or 'none','configured':bool(cloud_cfg),'allowed':allowed},
            'routes':{'local':caps['asr_ready_local'],'cloud':caps['asr_ready_cloud']},
            'warmup':dict(ASR_WARM),
            'device':_cap(local_asr.device_status,{}),
            'resident':_cap(local_asr.resident_status,{'running':False}),
            'diarize':_cap(local_asr.diarize_enabled,False),
            'prewarm':_cap(local_asr.prewarm_decision,{}),
            'cache':_cap(lambda:__import__('asr_cache').stats(),{}),
            'fallback_policy':_cap(providers.fallback_policy,{}),
            'help':{**help_text,'hint':hint},
            'hint':hint}


def _upsert_env_local(pairs):
    """把键值写进数据目录 .env.local（只写用户本机配置，包根模板永不写）。"""
    path=DATA/'.env.local'
    path.parent.mkdir(parents=True,exist_ok=True)
    lines=path.read_text(encoding='utf-8-sig').splitlines() if path.is_file() else []
    out=[];seen=set()
    for line in lines:
        name=line.partition('=')[0].strip()
        if name in pairs and '=' in line and not line.lstrip().startswith('#'):
            out.append(name+'='+pairs[name]);seen.add(name)
        else:
            out.append(line)
    for name,value in pairs.items():
        if name not in seen:
            out.append(name+'='+value)
    path.write_text('\n'.join(out)+'\n',encoding='utf-8')
    return path


def set_asr_mode(mode):
    """运行中可切换 auto/local/cloud，并写回数据目录 .env.local 供下次启动沿用。

    2026-11-14：与用户 Key 同处一个文件（DATA/.env.local），不写包根模板，不随包分发。
    """
    mode=str(mode or '').strip().lower()
    if mode not in providers.ASR_MODES:
        raise ValueError('语音识别通道只能是 auto（本地优先→云端）、local（只用本地）或 cloud（只用云端）。')
    os.environ['IP_ASR_MODE']=mode
    try:
        _upsert_env_local({'IP_ASR_MODE':mode})
    except OSError as exc:
        applog.exception('asr.mode.save',exc)
    applog.info('asr.mode','语音识别通道已切换',mode=mode)
    return asr_status()





def set_asr_diarize(enabled):
    """说话人分离按需开关，默认关闭（口播场景用不上，关掉能省近一半时间）。

    2026-11-16：切换会换模型组件，所以顺带释放常驻进程，下次转写按新设置重载。
    """
    enabled=bool(enabled)
    result=local_asr.set_diarize(enabled)
    try:
        _upsert_env_local({'IP_ASR_DIARIZE':'1' if enabled else '0'})
    except OSError as exc:
        applog.exception('asr.diarize.save',exc)
    applog.info('asr.diarize','说话人分离开关已切换',enabled=enabled,released=result.get('restarting'))
    if result.get('restarting'):
        ASR_WARM.update(status='idle',stage='设置已切换，下次转写会重新加载模型',
                        message='',seconds=None,updated_at=now())
    return {'ok':True,'diarize':enabled,'restarted':result.get('restarting'),'status':asr_status()}


def start_asr_warmup():
    with ASR_WARM_LOCK:
        if ASR_WARM['status']=='running':return dict(ASR_WARM)
        ASR_WARM.update(status='running',stage='正在启动预热',message='',seconds=None,updated_at=now())
    def work():
        def stage(text):ASR_WARM.update(stage=text,updated_at=now())
        result=local_asr.warmup(stage=stage)
        ASR_WARM.update(status='ok' if result['ok'] else 'failed',
                        stage='模型已就绪，首次转写会更快' if result['ok'] else '预热失败',
                        message=result.get('error',''),seconds=result.get('seconds'),updated_at=now())
        applog.info('asr.warmup','语音模型预热结束',ok=result['ok'],seconds=result.get('seconds'),
                    error=(result.get('error') or '')[:300])
    threading.Thread(target=work,daemon=True,name='ip-asr-warmup').start()
    return dict(ASR_WARM)


def parts_valid(parts):
    """三段必填（开头/主体/结尾），让人记住的那一句与值得被转发的一句话可选；旧五段数据也能通过。"""
    if not isinstance(parts,list) or not 3<=len(parts)<=5:
        raise ValueError('稿件至少需要开头、主体、结尾三段。')
    result=[];seen=set()
    for part in parts:
        if not isinstance(part,dict):
            raise ValueError('稿件段落格式无效。')
        role=part.get('role')
        if role not in PART_LABELS or role in seen:
            raise ValueError('稿件段落角色无效或重复。')
        if not isinstance(part.get('text'),str):
            raise ValueError('稿件正文格式无效。')
        if len(part['text'])>12000:
            raise ValueError('单段正文过长。')
        seen.add(role)
        result.append({'id':role+'_01','role':role,'label':PART_LABELS[role],'text':part['text']})
    missing=[PART_LABELS[r] for r in CORE_ROLES if r not in seen]
    if missing:
        raise ValueError('还缺'+'、'.join(missing)+'；这三段写满才能确认。')
    order=list(PART_LABELS)
    return sorted(result,key=lambda p:order.index(p['role']))


def draft_parts(parts):
    """读取旧稿时按角色归一：标签换成三段术语，正文原样保留。"""
    items=[{**p,'label':PART_LABELS[p['role']]} for p in (parts or [])
           if isinstance(p,dict) and p.get('role') in PART_LABELS]
    order=list(PART_LABELS)
    return sorted(items,key=lambda p:order.index(p['role']))


def missing_core(parts):
    texts={p.get('role'):str(p.get('text','')) for p in (parts or []) if isinstance(p,dict)}
    return [PART_LABELS[r] for r in CORE_ROLES if not texts.get(r,'').strip()]


def draft_missing(parts):
    """整篇是否为空（2026-11-25：讲稿页只有一个整篇框；客户确认稿件只看整篇有没有字，不再要求开头/结尾分开有字）。"""
    return [] if ''.join(str(p.get('text','')) for p in (parts or []) if isinstance(p,dict)).strip() else ['稿件内容']


def publication_pack(project,version):
    texts={p['role']:p.get('text','') for p in version['parts']}
    candidates=[project['title'],texts.get('hook','')[:24],texts.get('quote','')[:24],
                '关于'+project['title'][:22],project['title'][:20]+'：从一个问题开始']
    titles=[]
    for title in candidates:
        title=title.strip('，。？！ \n') or project['title']
        if title not in titles:
            titles.append(title)
    pack={'script_version_id':version['id'],'titles':titles,'selected_title':titles[0],
            'tags':project.get('tags',[]),'caption':texts.get('hook','')+'\n\n'+texts.get('ending',''),
            'method':'根据定稿直接整理，未调用生成模型；可编辑', 'ready':True}
    pack['copy_check']=read_script.check_copy(pack)
    pack['caption_alignment']=read_script.alignment_note(read_script.build_captions(version))
    return pack


PROCESS_STARTED_AT = now()   # t176：用于识别「服务重启后残留 running 的作业」，查询时不崩


def data_home_status(data_dir=None):
    """2026-11-25 资料外置：资料文件夹在哪、是不是还在软件文件夹里、没搬成的原因（给人看）。"""
    root=Path(data_dir or os.getenv('IP_DATA_DIR') or (ROOT/'.runtime')).resolve()
    pkg=(Path(__file__).resolve().parent/'.runtime').resolve()
    in_package=root==pkg
    note=''
    if in_package:
        try:note=(pkg/'资料未搬迁-原因.txt').read_text(encoding='utf-8').strip()
        except OSError:note=''
    return {'path':str(root),'in_package':in_package,'note':note,'name':os.path.basename(str(root))}


def _reveal_in_folder(path):
    """在资源管理器里定位一个文件（需求单 G.2.1）。返回 (是否成功, 原因)。

    先走系统 Shell 接口 SHOpenFolderAndSelectItems（Electron shell.showItemInFolder 的底层做法）；
    `explorer /select,` 在 UAC、同名窗口已打开等场景时灵时不灵，只作兜底。
    只在 Windows 本机回环请求里调用（调用方已校验）。
    """
    target=os.path.normpath(str(path))
    try:
        import ctypes
        ole32=ctypes.oledll.ole32;shell32=ctypes.oledll.shell32
        ole32.CoInitialize(None)
        try:
            pidl=ctypes.c_void_p()
            shell32.SHParseDisplayName(target,None,ctypes.byref(pidl),0,None)
            if not pidl:raise OSError('SHParseDisplayName 返回空')
            try:
                shell32.SHOpenFolderAndSelectItems(pidl,0,None,0)
            finally:
                ole32.CoTaskMemFree(pidl)
        finally:
            ole32.CoUninitialize()
        return True,'shell'
    except Exception as exc:
        try:
            subprocess.Popen('explorer /select,"%s"'%target)
            return True,'fallback:%s'%str(exc)[:80]
        except Exception as exc2:
            return False,'%s / %s'%(str(exc)[:80],str(exc2)[:80])

CUSTOMER_ERRORS=(
 ('unsupported',(),r'不支持|unsupported|格式可能不受支持|当前支持 mp4|未知的识别方式',
  '这个链接或文件类型目前还处理不了。','直接粘贴文字','rechoose',False),
 ('ffmpeg_missing',(),r'ffmpeg|ffprobe',
  '处理这个音视频要用的组件不在软件包里，这一步做不了。','查看怎么修','detail',False),
 ('cloud_login',('PromptUnavailable',),r'需登录云端获取|功能模板',
  '这项功能要先登录账号才能用。已经保存的内容不受影响。','去登录','login',False),
 ('memory',('MemoryError',),r'内存不足|out of memory|可用 ?\d+ MB',
  '电脑内存不够，这一步没做完。关掉一些占内存的程序再试，通常就好了。','再试一次','retry',True),
 ('model_missing',('ProviderMissing',),r'生成模型|写作模型|模型.{0,4}(未配置|待连接|未连接)|未配置模型服务|IP_LLM_',
  '自动写作还没连上，这次没法帮你生成。你写的内容都还在。','去设置页查看','settings',False),
 ('asr_failed',(),r'转写失败|语音识别失败|识别失败',
  '把声音转成文字这一步没成功。','再试一次','retry',True),
 # 2026-11-26 H8：timeout 前移到 asr_missing 之前——「转写超时」先命中 timeout（可重试），
 # 不再被 asr_missing 的「转写」正则误判成功能不可用。
 ('timeout',('TimeoutError',),r'超时|timed out|timeout|等太久|未响应|没有响应',
  '这次等得太久，先停下来了。你发的内容还在，可以再试一次。','再试一次','retry',True),
 ('asr_missing',('ProviderMissing',),r'语音识别|转写|ASR|火山|allow_cloud_asr|IP_ASR_MODE',
  '这台电脑上「听声音转文字」的功能暂时用不了，录音和视频转不了。','去设置页查看','settings',False),
 ('bad_draft',(),r'缺少开头|开头、主体|开头，主体',
  '这次自动生成的稿子不完整，没有采用。你原来的稿子没动。','再试一次','retry',True),
 ('file_missing',('FileNotFoundError',),r'找不到这个文件|No such file|已经不在原来的位置|资料文件夹不在原来的位置|被移动、改名或删除',
  '要用到的文件找不到了，可能被移动、改名或删除了。','重新选择文件','rechoose',True),
 ('module_missing',('ModuleNotFoundError','ImportError'),r'No module named|cannot import name|DLL load failed',
  '缺少程序文件：可能被杀毒软件隔离，或安装包不完整。到杀毒软件隔离区恢复该文件并加信任，或重新解压完整包。','导出诊断包','diagnostics',False),
 ('too_large',(),r'(超过|上限).{0,10}(GB|分钟|字)|too_large|too_long|单文件上限|单段正文过长',
  '这个文件或内容太大了，一次处理不了。','换小一点的','rechoose',False),
 ('download_failed',(),r'下载失败|获取视频失败|抽音频失败',
  '视频没取下来，可能链接失效、需要登录或网络不稳。','再试一次','retry',True),
 # 2026-11-26 H8：拆 network 行——云端不可达与模型服务连不上各自成类（正则保持宽松，顺序在 network 之前）。
 ('cloud_unreachable',('CloudUnreachable','ProxyError'),r'连不上云端服务|连不上账号服务器|连不上服务器|连不上云端',
  '连不上账号服务器。请检查是否断网或开了加速/代理；本地记录照常可用，登录和云端生成稍后再试。','再试一次','retry',True),
 ('provider_unreachable',(),r'无法连接已配置的服务|服务返回 HTTP',
  '你配置的模型/识别服务连不上或拒绝了请求（原因见详情）。到「设置 → 服务连接」检查地址和密钥。','去设置页查看','settings',False),
 ('network',('ConnectionError',),r'连不上|网络|Connection (refused|reset|aborted)|getaddrinfo|Name or service not known|WinError 100(54|60|61)|ProxyError|RemoteDisconnected|Remote end closed|URLError|服务未响应',
  '连不上网络或服务，这一步没做完。你发的内容还在。','再试一次','retry',True),
 ('record_missing',('KeyError',),r'记录不存在或不属于当前项目|已经不在了',
  '这条记录已经不在了（可能软件重启过，或已被清理）。','重新来一次','rechoose',True),
)
CUSTOMER_GENERIC=('generic',(),r'','这一步没做完，是我们这边出了问题。你的内容还在。','再试一次','retry',True)
# 不走异常、按业务码认的两类：试用到期（trial_gate.py）、抖音登录（new_job 的 needs_action）。
CUSTOMER_BY_CODE={'trial_readonly':('免费体验已经结束，新增内容暂时做不了。已保存的内容还能查看和导出。','去开通','upgrade',False),
                  'login_required':('这条内容要先登录抖音才能读取。','去登录抖音','douyin_login',False)}
_CJK_RE=re.compile(r'[\u4e00-\u9fff]')

def _customer_view(title,detail,label,action,retry,code,kind='unknown'):
    # 2026-11-26 H8：额外回传 error_kind＝登记表匹配到的类别 key（trial_readonly 等业务码类别沿用该码），
    # 前端据此显示定位码（E-XXX）；error_code 业务语义原样不动。
    return {'title':title,'detail':str(detail or '')[:1500],'next':{'label':label,'action':action},
            'retry':bool(retry),'error_code':str(code or ''),'error_kind':str(kind or 'unknown')}

def customer_error_text(text,code='',type_names=()):
    """原话 → 人话。先按正文匹配，再按异常类型兜底；都没中：有中文就原样给（本仓库的手写提示），英文给通用句。"""
    raw=str(text or '').strip();kind=str(code or '')
    if kind in CUSTOMER_BY_CODE:
        t=CUSTOMER_BY_CODE[kind];return _customer_view(t[0],raw,t[1],t[2],t[3],kind,kind)
    hit=None
    for row in CUSTOMER_ERRORS:
        if row[2] and re.search(row[2],raw,re.I):hit=row;break
    if hit is None:
        for row in CUSTOMER_ERRORS:
            if row[1] and set(row[1])&set(type_names or ()):hit=row;break
    if hit is not None:return _customer_view(hit[3],raw,hit[4],hit[5],hit[6],kind,hit[0])
    if _CJK_RE.search(raw):
        # 400/404/409 多半是输入或状态问题，重发同一个请求没用，给「返回修改」。
        ok=kind in ('','operation_failed')
        return _customer_view(raw[:200],raw,'再试一次' if ok else '返回修改','retry' if ok else 'back',ok,kind,'unknown')
    return _customer_view(CUSTOMER_GENERIC[3],raw,CUSTOMER_GENERIC[4],CUSTOMER_GENERIC[5],CUSTOMER_GENERIC[6],kind,'unknown')

def customer_error(exc,code=''):
    """异常 → {title, detail, next:{label,action}, retry}。KeyError 只取 args[0]（str(exc) 会带一对引号）。"""
    name=type(exc).__name__
    text=str(exc.args[0]) if isinstance(exc,KeyError) and exc.args and isinstance(exc.args[0],str) else str(exc).strip()
    view=customer_error_text(text,code,{c.__name__ for c in type(exc).__mro__})
    if not isinstance(exc,KeyError):view['detail']=('%s: %s'%(name,text))[:1500] if text else name
    return view

CUSTOMER_STAGES=((r'服务重启','软件刚重启过，这条需要重新来一次'),(r'排队','排队中…'),
                 (r'读取文件|识别图片|转写|抽帧|提取音轨|ASR','正在把声音和画面转成文字…'),
                 (r'兜底|云端识别|云端额度|计费|单价','正在转成文字…（本次用了云端识别，会消耗云端额度）'),
                 (r'合并|写第','正在写稿…'),(r'合规|检查','正在检查稿子…'))
def customer_stage(stage):
    """阶段原话 → 客户话术。计费提示必须留着（花了云端额度要让客户知道），只去内部术语。"""
    s=str(stage or '')
    for pattern,text in CUSTOMER_STAGES:
        if re.search(pattern,s,re.I):return text
    return s

def _job_error_view(job):
    """任务对外时补的人话字段（2026-11-25）：失败原话换成人话放 error，原话放 error_detail；
    stage_text 是阶段人话，error_view 是完整视图（title/detail/next/retry）。没失败或没留原因时原样不动。"""
    view=customer_error_text(job.get('error')) if job.get('status')=='failed' and job.get('error') else {}
    return {'stage_text':customer_stage(job.get('stage')),'error':view.get('title') or job.get('error'),
            'error_detail':view.get('detail',''),'error_view':view}

def job_public(job):
    """作业对外字段（t176 契约）：{status, stage, done, total, message, result_path}。

    status 取值：queued | running | succeeded | failed | needs_action。
    服务重启后，DB 里残留的 queued/running 视为已中断（failed），不让前端无限转圈。
    2026-11-25：失败时 message / error 给人话（customer_error_text），原话在 error_detail，
    另带 error_next / error_retry 给界面画「下一步」按钮；stage_text 是阶段人话。
    """
    status = str(job.get('status') or 'unknown')
    message = str(job.get('error') or '')
    result = job.get('result') if isinstance(job.get('result'), dict) else {}
    result_path = str((result or {}).get('result_path') or (result or {}).get('path') or '')
    started = str(job.get('started_at') or '')
    if status in ('queued', 'running') and started and started < str(PROCESS_STARTED_AT):
        status = 'failed'
        message = message or '软件刚重启过，这次处理中断了，请重新发起。'
    if status == 'failed' and not message:
        message = '这次处理没有完成，也没有留下原因。'
    view = customer_error_text(message) if status == 'failed' else {}
    return {'job_id': job.get('id'), 'status': status, 'stage': str(job.get('stage') or ''),
            'stage_text': customer_stage(job.get('stage')),
            'done': job.get('done'), 'total': job.get('total'), 'message': view.get('title') or message,
            'result_path': result_path, 'error': view.get('title') or job.get('error'),
            'error_detail': view.get('detail', ''), 'error_next': view.get('next'),
            'error_retry': view.get('retry', False),
            'created_at': job.get('created_at'), 'started_at': job.get('started_at'),
            'finished_at': job.get('finished_at'), 'poll': '/api/jobs/' + str(job.get('id'))}


def _job_tick(job_id):
    def tick(done, total):
        try:
            STORE.patch('job', job_id, {'done': int(done or 0), 'total': int(total or 0)})
        except Exception:
            pass
    return tick


def new_job(project_id,kind,inputs,work):
    with JOB_LOCK:
        for existing in STORE.list('job',1000):
            if existing.get('project_id')==project_id and existing.get('kind')==kind and existing.get('inputs')==inputs and existing.get('status') in ('queued','running'):return existing
        job=STORE.put('job',{'project_id':project_id,'kind':kind,'status':'queued','stage':'排队',
                           'inputs':inputs,'error':None,'result':None})
    # 2026-11-14：线程池不继承 threading.local；建作业时快照登录账号与权益，runner 里恢复，
    # 否则后台转写读不到 allow_cloud_asr 等逐人权益，会误判云端可用。
    context=billing_service.capture_context()
    def runner():
        billing_service.apply_context(context)
        try:
            STORE.patch('job',job['id'],{'status':'running','stage':'处理中','started_at':now()})
            result=work(job['id'])
            if isinstance(result,dict) and result.get('requires_action') and result.get('classification') in ('auth_required','risk_control'):
                detail=result.get('message')
                result={**result,'need_login':True,'reason':'login_required',
                        'message':'这条要登录抖音才能抓。扫码登录一次，登录信息只存在你自己电脑上。'}
                if detail:result['detail']=detail
            attention=bool(isinstance(result,dict) and result.get('requires_action'))
            STORE.patch('job',job['id'],{'status':'needs_action' if attention else 'succeeded','stage':result.get('stage','需要处理' if attention else '已完成') if isinstance(result,dict) else '已完成','result':result,'finished_at':now()})
        except Exception as exc:
            # Keys/headers are never logged. Local job failures keep prior results intact.
            STORE.patch('job',job['id'],{'status':'failed','stage':'需要处理','error':str(exc)[:1800],'finished_at':now()})
    POOL.submit(runner)
    return job


bench_service.init(STORE,new_job,ROLES)
creation_service.init(STORE,new_job,parts_valid)
feedback_service.init(STORE,new_job)


def intake_job(project):
    with INTAKE_LOCK:
        return _intake_job(project)


def _intake_job(project):
    source=require('source',project['source_id'])
    # Identical active requests reuse the task; do not charge/fetch twice on double clicks.
    for job in STORE.list('job',300):
        if job.get('project_id')==project['id'] and job['kind']=='resolve' and job['status'] in ('queued','running'):
            return job
    def work(job_id):
        STORE.patch('job',job_id,{'stage':'读取视频资料与可用字幕'})
        result=providers.resolve_source(source.get('url') or source.get('share_text',''))
        media_asset=None
        media_url=result.get('media_url')
        duration_ok=isinstance(result['metadata'].get('duration_ms'),(int,float)) and result['metadata']['duration_ms']<=10*60*1000
        if media_url and not result['transcript'].strip() and providers.available()['asr'] and duration_ok:
            try:
                STORE.patch('job',job_id,{'stage':'下载参考视频，稍后本地转写'})
                target=DATA/'artifacts'/(job_id+'.mp4')
                video_intake.download_media(media_url,target)
                info=media.probe(target)
                base=(result['metadata']['title'] or result['metadata']['video_id'])[:80].strip().replace('/','_').replace('\\','_')
                media_asset=STORE.put('asset',{'project_id':project['id'],'script_version_id':None,'purpose':'reference',
                    'filename':base+'.mp4','path':str(target),'info':info,'origin':'platform_link',
                    'source_video_id':str(result['metadata']['video_id'])})
                result['media_asset_id']=media_asset['id'];result['media_downloaded']=True
                result['message']=(result.get('message','') or '')+' 参考视频已下载，继续本地转写与内容拆解。'
            except Exception as exc:
                result['media_download_error']=str(exc)[:300]
                result['message']=(result.get('message','') or '')+f' 视频下载未成功（{str(exc)[:120]}）；转写需要自行上传视频。'
        elif media_url and not duration_ok:
            result['message']=(result.get('message','') or '')+' 视频超过 10 分钟，未自动下载；请截取片段后上传。'
        result['analysis']={'status':'pending' if result['transcript'] else 'needs_transcript','summary':'','note':'已取得字幕后继续模型拆解。'}
        # 2026-11-17 t109：互动数据随解析结果落 source（缺失「—」原样保留，不估算）。
        changes={'resolution':result,'status':result['status'],
                 'interactions':result.get('interactions') or None,
                 'interactions_source':result.get('interactions_source') or '',
                 'need_login':bool(result.get('need_login'))}
        if result['transcript'].strip():changes['text']=result['transcript']
        STORE.patch('source',source['id'],changes,source['revision'])
        title=result['metadata']['title']
        if title and project.get('title_origin')=='automatic':
            fresh=require('project',project['id'])
            STORE.patch('project',project['id'],{'title':title[:160]},fresh['revision'])
        if media_asset:
            follow=content_job(require('project',project['id']),media_asset)
            return {'requires_action':True,'stage':'参考视频已下载，本地转写与内容拆解进行中',
                    'resolution':result,'media_asset_id':media_asset['id'],'follow_job_id':follow['id']}
        if result['transcript'].strip() and providers.available()['llm']:
            return prepare_content(project,require('source',source['id']),job_id)
        return {'requires_action':result['status']!='ready' or result['analysis']['status']!='ready',
            'stage':'内容已取得，分析待配置' if result['status']=='ready' and result['analysis']['status']!='ready' else '资料与内容拆解已完成' if result['status']=='ready' else result['message'],
            'resolution':result,'source_id':source['id']}
    return new_job(project['id'],'resolve',{'source_id':source['id'],'source_revision':source['revision']},work)


def analyze_write_system(length_rule):
    """取写稿 system 模板，并把长度规则挪到模板末尾。

    模板正文由云端中控下发（本地缓存 60 秒会被刷新覆盖），所以顺序在渲染期兜住：
    长度规则留在第 192 字会让后面几万字字幕之前的稳定前缀只剩一百多 token；
    挪到末尾后，模板整段成为可复用前缀。语义不变，只是位置。
    """
    text=prompt_store.get('video.analyze_write',length_rule=length_rule)
    seg='稿件适合约%s，'%length_rule
    if seg in text:
        text=text.replace(seg,'',1).rstrip()+('稿件适合约%s。'%length_rule)
    return text


def merge_analyses(analyses):
    """把同一视频多段分析合并成一份；只归纳去重，不新增判断。"""
    result=long_text.merge_json(
        '输出与输入相同的 JSON 结构：{"parts":[...],"analysis":{...},"items":[...]}。'
        'parts 只保留一份完整讲稿（开头、主体、结尾齐全），items 最多 3 条，重复的合并。'
        'analysis.structure 的每一项必须是对象（label/quote/note 三个字段），不要写成字符串。',
        analyses,'analyze_write')
    # 合并结果仍按段分析的字段契约收口：模型偶尔把 structure 写成字符串数组，
    # 那样在 validate_analysis 里会整次失败。这里只筛掉不合规的项，不改写内容。
    if isinstance(result,dict) and isinstance(result.get('analysis'),dict):
        analysis=result['analysis']
        if isinstance(analysis.get('structure'),list):
            analysis['structure']=[x for x in analysis['structure'] if isinstance(x,dict)]
        if analysis.get('hook') is not None and not isinstance(analysis.get('hook'),dict):
            analysis['hook']={}
    return result


def prepare_content(project,source,job_id):
    """One grounded model pass connects analysis, knowledge candidates and a reviewable draft."""
    if not source.get('text','').strip():raise ValueError('需要真实字幕或正文，不能凭链接猜内容。')
    if not providers.available()['llm']:raise providers.ProviderMissing('写稿模型未配置；字幕已保留，可稍后继续。')
    STORE.patch('job',job_id,{'stage':'DeepSeek 正在拆解、提炼认知并起草三段讲稿'})
    resolution=source.get('resolution') or video_intake.normalize({'transcript':source['text']},
        {'platform':'manual','video_id':'','canonical_url':source.get('url','')},'manual_text')
    profile=STORE.get('profile','owner') or {'style':'','audience':'','redlines':'不虚构本人经历'}
    profile_compact=style_profile.profile_context(profile)
    profile_block=style_profile.profile_block(profile)
    length_rule=str(profile.get('video_length') or '60至90秒')[:60]
    refs=[require('knowledge',key) for key in project.get('knowledge_ids',[])]
    refs=[{'title':x['title'],'claim':x['claim'],'boundary':x.get('boundary',''),'endorsement':x.get('endorsement')}
        for x in refs if x.get('content_use')!='review' and x.get('endorsement')!='rejected']
    use_default=creation_service.flag((project.get('creation_brief') or {}).get('use_default_principles'), True)
    default_principles=creation_service.default_principles_snapshot() if (use_default and not project.get('knowledge_ids')) else []
    principles_block=creation_service.principles_block(default_principles)
    # 2026-11-16：动态内容（逐条不同的 cues、几万字新字幕）一律放到最后，
    # 让模型侧可复用的前缀是「模板 + 档案 + 三观」这段稳定结构，而不是几十 token。
    system=analyze_write_system(length_rule)
    base={'profile':profile_compact,'profile_block':profile_block,
          'personal_knowledge':refs,'personal_principles':default_principles,
          'principles_block':principles_block,'opening':resolution.get('opening')}
    def _payload(seg):
        seg_resolution=resolution if seg==source['text'] else video_intake.normalize({'transcript':seg},
            {'platform':'manual','video_id':'','canonical_url':source.get('url','')},'manual_text')
        return {**base,'cues':seg_resolution.get('cues',[])[:700],'source_text':seg}
    def _call(payload,tag):
        return providers.llm_json(system,payload,tag=tag)
    def _progress(index,total,merging):
        STORE.patch('job',job_id,{'stage':'正在合并 %d 段结论'%total if merging
            else '正在分析第 %d/%d 段'%(index,total)})
    def _merge(answers):
        answer=merge_analyses(answers)
        def _valid(ans):
            try:return parts_valid(ans.get('parts'))
            except ValueError:return []
        if missing_core(_valid(answer)):
            # 合并阶段偶尔会丢开头/结尾或段落格式不合规；回退到某一段结构完整的答案，不让整次分析作废。
            fallback=next((a for a in answers if not missing_core(_valid(a))),None)
            if fallback:answer={**answer,'parts':fallback['parts']}
        return answer
    answer=long_text.run(source['text'],_payload,_call,'analyze_write',
        shrink=long_text.fit_cues,progress=_progress,merge=_merge)
    parts=parts_valid(answer.get('parts'))
    if missing_core(parts):raise ValueError('模型返回缺少开头、主体或结尾；没有替换稿件，请重新生成。')
    analysis=video_intake.validate_analysis(answer.get('analysis',{}),resolution)
    items=answer.get('items',[])
    if not isinstance(items,list) or len(items)>3:raise ValueError('认知条目格式无效；未自动入库。')
    candidates=[]
    for item in items:
        if not isinstance(item,dict):continue
        quote=str(item.get('quote','')).strip();claim=str(item.get('claim','')).strip()
        if not quote or quote not in source['text'] or not claim or not item.get('title'):continue
        digest=hashlib.sha256((source['id']+'\0'+source['text']+'\0'+claim).encode()).hexdigest()
        candidates.append({'legacy_id':'视频提炼','title':str(item['title'])[:160],'claim':claim[:12000],
            'body':claim[:12000],'boundary':str(item.get('boundary') or '适用边界待补')[:12000],
            'category':'随手记','category_legacy':'视频积累','category_aliases':['随手记','视频积累'],'source_id':source['id'],'source_snapshot':source['text'],
            'source_quote':quote,'source_locator':{},'links':[],'markers':['模型提炼待核对'],
            'endorsement':'reference_only','validation':'unverified','lifecycle':'active',
            'content_use':'available','extraction_key':digest})
    # Reject stale input before committing any model-generated information.
    fresh_source=STORE.patch('source',source['id'],{'resolution':{**resolution,'analysis':analysis}},source['revision'])
    saved=[]
    with CONTENT_LOCK:
        existing={x.get('extraction_key'):x for x in STORE.list('knowledge',10000)}
        for item in candidates:
            saved.append(existing.get(item['extraction_key']) or STORE.put('knowledge',{**item,'source_revision':fresh_source['revision']}))
    suggestion=STORE.put('suggestion',{'project_id':project['id'],'base_draft_revision':project['draft_revision'],
        'parts':parts,'source_id':source['id'],'source_revision':fresh_source['revision']})
    current=require('project',project['id']);filled=False
    if current['draft_revision']==project['draft_revision'] and not any(p['text'].strip() for p in current['draft']):
        try:
            STORE.patch('project',project['id'],{'draft':parts,'draft_revision':current['draft_revision']+1,
                'draft_changed':True,'stage':'draft','knowledge_candidate_ids':[x['id'] for x in saved]},current['revision']);filled=True
        except Conflict:pass
    return {'suggestion':suggestion,'draft_filled':filled,'knowledge':saved,
        'stage':'字幕、内容拆解与初稿已就绪，请读稿确认',
        'note':'认知仅作待核对参考；已有稿件不会被自动覆盖。'}


def content_job(project,asset=None):
    with INTAKE_LOCK:
        for job in STORE.list('job',500):
            if job.get('project_id')==project['id'] and job['kind'] in ('prepare_reference','prepare_content','resolve') and job['status'] in ('queued','running'):
                if not asset or job.get('inputs',{}).get('asset_id')==asset['id']:return job
        source=require('source',project['source_id'])
        def work(job_id):
            current_source=source
            if asset:
                STORE.patch('job',job_id,{'stage':providers.asr_route_label()})
                record=next((x for x in STORE.list('transcript',10000) if x.get('asset_id')==asset['id']),None)
                if record is None:
                    audio=DATA/'artifacts'/(job_id+'.mp3');media.extract_audio(asset['path'],audio)
                    record=STORE.put('transcript',{**providers.asr(audio,stage=lambda s:STORE.patch('job',job_id,{'stage':s})),
                        'asset_id':asset['id'],'project_id':project['id']})
                transcript=record
                info={'platform':'uploaded_file','video_id':'','canonical_url':source.get('url','')}
                result=video_intake.normalize({'transcript':transcript['text'],'cues':transcript.get('cues',[]),
                    'words':transcript.get('words',[]),'metadata':{'title':asset['filename'],'duration_ms':asset['info']['duration_ms']},
                    'message':'用户上传视频的本地转写；未取得平台互动数据，也未核验文件与分享链接的一致性。'},info,'workshop_funasr_local')
                current_source=require('source',source['id'])
                if current_source['text']!=source['text'] or current_source.get('reference_asset_id')!=source.get('reference_asset_id'):
                    raise Conflict('转写期间参考正文已发生变化。视频与转写结果已保存，请核对后重新继续；没有覆盖新资料。')
                current_source=STORE.patch('source',source['id'],{'text':transcript['text'],'resolution':result,
                    'status':'ready','reference_asset_id':asset['id'],'transcript_id':record['id']},current_source['revision'])
            return prepare_content(project,current_source,job_id)
        return new_job(project['id'],'prepare_reference' if asset else 'prepare_content',
            {'source_id':source['id'],'source_revision':source['revision'],'asset_id':asset['id'] if asset else None},work)


def next_action(project):
    if project.get('mode')=='direct':
        if project.get('stage')=='ready_to_export':return {'step':'edit','label':'下载这版视频','hint':'成片已确认'}
        if project.get('current_asset_id'):return {'step':'edit','label':'继续剪辑','hint':'转写、净剪与字幕都在剪辑页完成'}
        return {'step':'edit','label':'导入已有视频','hint':'上传视频后自动转写、净剪与字幕'}
    if not project.get('confirmed_version_id'):
        # 2026-11-16：历史记录里可能缺 source_id，或来源已被清理。一条坏记录不能把整个作品列表打崩。
        source={}
        if project.get('source_id'):
            try:
                source=require('source',project['source_id'])
            except KeyError:
                source={}
        if not draft_missing(project.get('draft')):
            return {'step':'script','label':'确认稿件','hint':'内容写好了，确认后进入拍摄'}
        if not source.get('text','').strip() and source.get('url') and not project.get('topic_id'):
            return {'step':'source','label':'补充视频或查看进度','hint':'还没拿到可用字幕，也可直接写稿'}
        return {'step':'script','label':'开始写讲稿','hint':'没有视频也能先写'}
    if project.get('draft_changed'):return {'step':'script','label':'确认修改后的稿件','hint':'新稿还没有确认'}
    if project.get('stage')=='ready_to_export':return {'step':'publish','label':'下载交付资料','hint':'成片已确认'}
    if project.get('stage')=='review':return {'step':'edit','label':'审阅这版视频','hint':'核对声音与剪切'}
    assets=[a for a in STORE.list('asset') if a.get('project_id')==project['id'] and a.get('script_version_id')==project.get('confirmed_version_id')]
    return {'step':'edit','label':'开始剪辑','hint':'拍摄素材已就绪'} if assets else {'step':'shoot','label':'拍摄并上传','hint':'按确认稿拍摄即可'}


def attach_if_current(project_id,script_id,changes):
    project=require('project',project_id)
    if project.get('mode')=='direct':
        # 直剪模式没有定稿概念；素材版本是否仍当前由 attach_direct 在任务侧把关，
        # 这里只兜底"模式不匹配不挂"。
        if script_id is not None:return
        try:
            STORE.patch('project',project_id,changes,expected=project['revision'])
        except Conflict:
            pass
        return
    if project.get('confirmed_version_id')==script_id and not project.get('draft_changed'):
        try:
            STORE.patch('project',project_id,changes,expected=project['revision'])
        except Conflict:
            pass


def attach_direct(project_id,asset,changes):
    """直剪模式回写：素材版本锚（素材ID+素材版本号）对不上就不把旧任务结果挂到当前。"""
    project=STORE.get('project',project_id)
    if not project or project.get('mode')!='direct':return False
    if project.get('current_asset_id')!=asset.get('id') or project.get('material_version')!=asset.get('material_version'):
        return False
    try:
        STORE.patch('project',project_id,changes,expected=project['revision'])
        return True
    except Conflict:
        return False


def material_gate(project,asset):
    """版本归属闸：写稿流程按定稿校验，直剪模式按当前素材版本校验。"""
    if project.get('mode')=='direct':
        if asset.get('purpose')=='direct' and asset.get('id')==project.get('current_asset_id') \
                and asset.get('material_version')==project.get('material_version'):
            return
        raise Conflict('这条素材已不是当前直剪素材；旧素材的处理结果保留，但请在最新导入的视频上操作。')
    if project.get('draft_changed') or not project.get('confirmed_version_id') \
            or asset.get('purpose','shooting')!='shooting' or asset.get('script_version_id')!=project['confirmed_version_id']:
        raise Conflict('素材与当前定稿不一致，请先核对拍摄版本。')


PUBLISH_DIR=DATA/'publish'
PUBLISH_SESSION=PUBLISH_DIR/'douyin-session.json'
PUBLISH_PROGRESS=PUBLISH_DIR/'login-progress.json'


def run_publish_worker(command,timeout=900):
    python=video_intake.browser_python();script=ROOT/'publish_worker.py'
    if not python.is_file() or not script.is_file():raise ValueError('发布执行器未安装：需要 .runtime/browser-env 与 patchright。')
    proc=subprocess.run([str(python),str(script),*command],capture_output=True,timeout=timeout,cwd=str(ROOT),
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if proc.returncode!=0:
        detail=(proc.stdout or proc.stderr).decode('utf-8','replace').strip()[:600]
        raise ValueError('发布执行器返回错误：'+detail)
    try:return json.loads(proc.stdout.decode('utf-8','replace'))
    except ValueError:raise ValueError('发布执行器返回格式无效。') from None


def publish_status():
    if not PUBLISH_SESSION.is_file():return {'logged_in':False,'reason':'no_session','message':'还没有抖音账号会话。'}
    try:
        data=json.loads(PUBLISH_SESSION.read_text(encoding='utf-8'))
        if not any(c.get('name')=='sessionid' and c.get('value') for c in data.get('cookies',[])):
            return {'logged_in':False,'reason':'no_session','message':'还没有抖音账号会话。'}
    except ValueError:
        return {'logged_in':False,'reason':'no_session','message':'还没有抖音账号会话。'}
    return run_publish_worker(['status','--session',str(PUBLISH_SESSION)],timeout=180)


def publish_login_job():
    for job in STORE.list('job',200):
        if job.get('kind')=='publish_login' and job.get('status') in ('queued','running'):return job
    def work(job_id):
        STORE.patch('job',job_id,{'stage':'等待扫码登录抖音创作者中心'})
        result=run_publish_worker(['login','--session',str(PUBLISH_SESSION),
            '--progress',str(PUBLISH_PROGRESS),'--timeout','280'],timeout=320)
        return {'requires_action':not result.get('logged_in'),'stage':'已登录，可以发布演练' if result.get('logged_in') else '需要本人扫码登录',
                'result':result}
    return new_job('system','publish_login',{},work)


def publish_plan_target(data):
    project=require('project',data.get('project_id',''))
    artifact=None
    if data.get('artifact_id'):
        artifact=require('artifact',data['artifact_id'])
    else:
        candidates=[a for a in STORE.list('artifact',2000) if a.get('project_id')==project['id']
                    and a.get('kind')=='packaged_video' and a.get('script_version_id')==project.get('confirmed_version_id')]
        if candidates:artifact=max(candidates,key=lambda a:a.get('created_at',''))
    if artifact is None or artifact.get('project_id')!=project['id'] or artifact.get('kind')!='packaged_video':
        raise ValueError('请先选择本作品已包装的字幕成片。')
    pack=project.get('publication_pack') or {}
    title=str(pack.get('selected_title') or project.get('title') or '')[:30]
    tags=[str(t)[:20] for t in (pack.get('tags') or [])[:10]]
    caption=str(pack.get('caption') or '')[:1000]
    if not title:raise ValueError('发布资料还没有封面标题，请先回到“发布文案”确认。')
    return project,artifact,title,tags,caption


def project_compliance(project):
    version=STORE.get('script_version',project.get('confirmed_version_id') or '') if project else None
    if not version:
        return compliance.check(compliance.draft_text((project or {}).get('draft')))
    report=compliance.check(compliance.draft_text(version.get('parts')))
    # 试用反馈 1C：定稿时本人逐项确认放行的必删命中，随版本复用，导出/发布演练沿用同一记录。
    return compliance.apply_waivers(report,version.get('compliance_waivers'))


def require_compliant(project,label):
    report=project_compliance(project)
    if not report.get('ok'):
        raise ValueError(label+'被拦截：'+compliance.block_message(report))
    return report


def publish_plan_job(data):
    project,artifact,title,tags,caption=publish_plan_target(data)
    require_compliant(project,'发布演练')
    def work(job_id):
        STORE.patch('job',job_id,{'stage':'发布演练：上传草稿并核对表单（不发布）'})
        screenshot=DATA/'artifacts'/('publish-plan-'+job_id+'.png')
        result=run_publish_worker(['plan','--session',str(PUBLISH_SESSION),'--video',artifact['path'],
            '--title',title,'--desc',caption,'--tags-json',json.dumps(tags,ensure_ascii=False),
            '--screenshot',str(screenshot),'--progress',str(PUBLISH_PROGRESS)],timeout=900)
        return {'requires_action':True,'stage':'发布演练完成，未点击发布',
                'result':result,'artifact_id':artifact['id'],'title':title}
    return new_job(project['id'],'publish_plan',{'artifact_id':artifact['id']},work)


def publish_submit_job(data):
    if data.get('confirm') is not True:raise ValueError('准备发布资料需要勾选本人确认（工作台不会代发）。')
    project=require('project',data.get('project_id',''))
    require_compliant(project,'发布')
    if project.get('stage')!='ready_to_export' or not project.get('reviewed_artifact_id'):
        raise Conflict('请先审片并确认成片，进入交付阶段后再准备发布资料。')
    artifact=require('artifact',project['reviewed_artifact_id'])
    if artifact.get('project_id')!=project['id']:raise ValueError('审片成片不属于本作品。')
    pack=project.get('publication_pack') or {}
    title=str(pack.get('selected_title') or project.get('title') or '')[:30]
    tags=[str(t)[:20] for t in (pack.get('tags') or [])[:10]]
    caption=str(pack.get('caption') or '')[:1000]
    def work(job_id):
        STORE.patch('job',job_id,{'stage':'正在准备发布资料：上传草稿并填表（不会点击发布）'})
        result=run_publish_worker(['submit','--session',str(PUBLISH_SESSION),'--video',artifact['path'],
            '--title',title,'--desc',caption,'--tags-json',json.dumps(tags,ensure_ascii=False),
            '--progress',str(PUBLISH_PROGRESS)],timeout=900)
        prepared=bool(result.get('prepared'))
        return {'requires_action':bool(result.get('needs_action')),
                'stage':('发布资料已就绪，未代发；请本人到创作者中心确认并发布，发布后回填作品链接。'
                         if prepared else '发布资料准备未完成，请查看失败原因'),
                'result':result,'artifact_id':artifact['id'],'title':title}
    return new_job(project['id'],'publish_submit',{'artifact_id':artifact['id']},work)


matrix_service.init(STORE,new_job,{
    'run_worker':run_publish_worker,
    'session_dir':PUBLISH_DIR,
    'default_session':PUBLISH_SESSION,
    'progress':PUBLISH_PROGRESS,
    'artifacts_dir':DATA/'artifacts',
    'publishing_ready':video_intake.browser_python().is_file() and (ROOT/'publish_worker.py').is_file(),
})


def do_cut(project,asset,mode,manual=None,extra=None,skip_validate=False):
    script_id=project.get('confirmed_version_id')
    inputs={'script_version_id':script_id,'asset_id':asset['id'],'mode':mode,'segments':manual}
    def work(job_id):
        plan={'segments':([{'start_ms':int(s['start_ms']),'end_ms':int(s['end_ms'])} for s in manual]
                          if skip_validate else media.validate_segments(manual,asset['info']['duration_ms'])),
              'removed':[],'method':'manual_source_intervals'} if manual else media.silence_plan_compressed(asset['path'],asset['info'],mode)
        if extra:plan.update(extra)
        plan.update(asset_id=asset['id'],script_version_id=script_id)
        saved=STORE.put('cleanup_plan',plan)
        STORE.patch('job',job_id,{'stage':'按源时间线剪切并编码'})
        output=DATA/'artifacts'/(job_id+'.mp4')
        mapping=media.render_cut(asset['path'],asset['info'],plan['segments'],output)
        info=media.probe(output)
        artifact=STORE.put('artifact',{'project_id':project['id'],'script_version_id':script_id,
            'asset_id':asset['id'],'kind':'clean_video','path':str(output),'filename':'清理-'+project['title'][:30]+'.mp4',
            'info':info,'plan_id':saved['id'],'mapping':mapping})
        warning=plan.get('warning','手动区间剪辑，请核对原意和声音。')
        if project.get('mode')=='direct':
            if not attach_direct(project['id'],asset,{'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None}):
                warning='注意：处理期间导入了新素材，这版结果保留在记录里，但没有设为当前成片。'+warning
        else:
            attach_if_current(project['id'],script_id,{'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None})
        return {'plan':saved,'artifact':artifact,'warning':warning}
    return new_job(project['id'],'cut',inputs,work)


def direct_create(data):
    """直剪模式立项：不伪造定稿，source 只是占位关联，confirmed_version_id 保持 None。"""
    title=str(data.get('title','')).strip()[:160] or '直剪视频'
    source=STORE.put('source',{'title':title,'url':'','share_text':'','text':'','kind':'direct_video','status':'ready'})
    parts=[{'id':r+'_01','role':r,'label':l,'text':''} for r,l in ROLES]
    return STORE.put('project',{'title':title,'source_id':source['id'],'draft':parts,'draft_revision':1,
        'is_test':data.get('is_test') is True,
        'draft_changed':False,'confirmed_version_id':None,'stage':'edit','mode':'direct',
        'material_version':0,'current_asset_id':None,'knowledge_ids':[],'tags':[],'director':[],
        'title_origin':'manual'})


def direct_pipeline_job(project,asset,silence_mode='conservative'):
    """直剪自动链：转写（已有则复用）→ 静音净剪 → 有转写再烧字幕。

    任务完成时按素材版本锚回写；素材已更换则结果保留在记录里，不冒充当前结果。
    语音识别不可用时仍产出静音净剪版并如实说明，原始素材保留，可重试。
    """
    if project.get('mode')!='direct':raise Conflict('这条作品不是直剪模式。')
    if asset.get('project_id')!=project['id'] or asset.get('purpose')!='direct':raise Conflict('这条素材不是本作品的直剪素材。')
    if silence_mode not in ('conservative','natural','tight'):
        raise ValueError('停顿清理档位无效，仅支持 conservative/natural/tight。')
    def work(job_id):
        stage=lambda s:STORE.patch('job',job_id,{'stage':s})
        out=DATA/'packages'/job_id;out.mkdir(exist_ok=True)
        transcript=next((x for x in STORE.list('transcript',10000) if x.get('asset_id')==asset['id']),None)
        asr_note=''
        if transcript is None:
            if providers.available()['asr']:
                stage('语音识别转写，保留原始字幕与时间戳')
                audio=out/'speech.mp3';media.extract_audio(asset['path'],audio)
                transcript=STORE.put('transcript',{**providers.asr(audio,stage=stage),
                    'asset_id':asset['id'],'project_id':project['id']})
            else:
                asr_note=('语音识别当前不可用，本次只做了静音净剪、没有字幕。'
                          '配置语音识别后到设置页检查通道，再点「重新自动处理」即可补上字幕；原始素材保留。')
        stage('保守静音净剪，保留原始素材')
        plan=media.silence_plan_compressed(asset['path'],asset['info'],silence_mode)
        clean_path=out/'clean.mp4'
        mapping=media.render_cut(asset['path'],asset['info'],plan['segments'],clean_path)
        info=media.probe(clean_path)
        expected=sum(x['end_ms']-x['start_ms'] for x in plan['segments'])
        diff=abs(info['duration_ms']-expected)
        # 2026-11-10（试用反馈 P0-4）：200ms 级差异不等于坏片；可播放且差异有限时
        # 保留成片、标「需人工复核」，差异大到像丢段才判失败。与 anchor/packaging 同型。
        if diff>max(2000,expected*0.05):
            raise ValueError(f'成片时长 {info["duration_ms"]}ms 与保留计划 {expected}ms 相差 {diff}ms，超出可播放容差；中间文件保留：{clean_path}')
        needs_review=diff>200
        saved=STORE.put('cleanup_plan',{**plan,'mapping':mapping,'method':'direct_silence',
            'silence_mode':silence_mode,'asset_id':asset['id'],'script_version_id':None,
            'material_version':asset.get('material_version')})
        clean=STORE.put('artifact',{'project_id':project['id'],'script_version_id':None,'asset_id':asset['id'],
            'material_version':asset.get('material_version'),
            'kind':'clean_video','path':str(clean_path),'filename':'净剪-'+project['title'][:30]+'.mp4',
            'info':info,'plan_id':saved['id'],'mapping':mapping,
            'needs_review':needs_review,'duration_diff_ms':diff})
        artifact=clean
        caption_note=''
        if transcript and (transcript.get('words') or transcript.get('cues')):
            stage('烧录字幕，保留原声与剪切时间轴')
            # 2026-11-10（试用反馈 P0-3）：与字幕包装共用词级回退，词级缺项的句子
            # 按句级时间保留并报数，不再静默丢句。
            cues,cap_stats=packaging_job.map_cues(transcript.get('cues') or [],mapping,transcript.get('words'))
            if not cues:raise ValueError('没有可映射到剪后时间轴的字幕；请先核对转写。')
            if cap_stats['fallback_cues']:
                caption_note='其中 %d 句词级结果缺失，已按句级时间保留；请对照原声核对字幕。'%cap_stats['fallback_cues']
            artifact=render_caption_artifact(project,clean,cues,[],'直剪：仅使用真实字幕，未添加额外强调。',
                job_id,project.get('caption_style'),attach=False)
        attached=attach_direct(project['id'],asset,
            {'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None})
        note=asr_note or '转写、静音净剪与字幕已完成，请播放审阅。'
        if caption_note:note+=' '+caption_note
        if needs_review:note+=f'（时长与保留计划相差 {diff}ms，需人工核对音画）'
        if not attached:
            note+='注意：处理期间导入了新素材，这版结果保留在记录里，但没有设为当前成片。'
        return {'artifact':artifact,'clean_artifact':clean,'transcript':transcript,'attached':attached,
            'stage':'直剪处理完成，请播放审阅' if attached else '直剪处理完成（结果未挂到当前素材）','note':note}
    return new_job(project['id'],'direct_pipeline',
        {'asset_id':asset['id'],'material_version':asset.get('material_version'),'silence_mode':silence_mode},work)


def motion_job(project,artifact,brief,render=False):
    cap=capabilities()
    if not cap['motion_installed']:
        raise ValueError('本地 MotionGit 适配器或其 Python 环境未找到。')
    if project.get('draft_changed') or project.get('confirmed_version_id')!=artifact.get('script_version_id'):
        raise Conflict('当前稿件与清理版本不一致，请先确认要处理的版本。')
    if not isinstance(brief,dict) or not brief.get('groups') or not brief.get('basic'):
        raise ValueError('请先填写并检查模板 brief。不得把整篇原稿直接塞进模板槽位。')
    face=brief.get('presenterFace',{})
    if any(not isinstance(face.get(k),(int,float)) or not 0<face[k]<=1 for k in ('cx','cy','h')):
        raise ValueError('本地模板需要实测人脸几何：请在 brief.presenterFace 中填写 cx/cy/h（0–1），不能把默认位置当作检测结果。')
    adapter=Path(os.getenv('IP_MOTION_ADAPTER',str(BASE/'tools'/'motion-adapter'/'pack.py')))
    interpreter=os.getenv('IP_MOTION_PYTHON',str(BASE/'tools'/'motion-python'/'Scripts'/'python.exe'))
    def work(job_id):
        job_root=DATA/'packages'/job_id;job_root.mkdir()
        # pack.py requires a new output directory. Keep the brief beside it.
        out=job_root/'delivery'
        data={**brief}
        # All file bindings come from our DB, never arbitrary client filesystem paths.
        data['media']={'presenter':{'mode':'video','file':artifact['path'],'face':face},
                       'voice':{'mode':'file','file':artifact['path']}}
        captions=project.get('caption_cues')
        if captions:
            data['media']['captions']=captions
        data['soundEffects']='none'
        bp=job_root/'brief.json';bp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        args=[interpreter,str(adapter),'--brief',str(bp),'--out',str(out)]
        if render:
            args.append('--render')
        STORE.patch('job',job_id,{'stage':'MotionGit 真实模板处理中；当前仅横屏'})
        audio_recovery=None
        try:
            result=media.run(args,timeout=3600)
            log=result.stdout+'\n'+result.stderr
        except media.MediaProcessError as exc:
            if not render or 'Audio track is shorter than the output clock' not in exc.stderr or not (out/'silent.mp4').is_file():raise
            audio_recovery=media.finalize_render_audio(out/'silent.mp4',artifact['path'],out/'silent-with-audio.mp4')
            log=exc.stdout+'\n'+exc.stderr+'\nShort audio tail repaired; see job result.'
        (job_root/'adapter.log').write_text(log,encoding='utf-8')
        filename='silent-with-audio.mp4' if render else 'preview.png'
        output=out/filename
        if not output.is_file():
            raise RuntimeError('适配器未返回预期产物，不能标记出片成功。')
        item=STORE.put('artifact',{'project_id':project['id'],'script_version_id':project['confirmed_version_id'],
             'kind':'packaged_video' if render else 'template_frame','path':str(output),
             'filename':filename,'info':media.probe(output) if render else {'width':1920,'height':1080}})
        if render:
            attach_if_current(project['id'],project['confirmed_version_id'],{'latest_artifact_id':item['id'],'reviewed_artifact_id':None,'stage':'review'})
        return {'artifact':item,'audio_recovery':audio_recovery,'note':'真实模板产物；本地包仅横屏，预览帧不代表完整视频。'}
    return new_job(project['id'],'motion_render' if render else 'motion_preview',{'artifact_id':artifact['id'],'brief':brief},work)


def cover_generate_job(project,data):
    """封面生成 job（2026-11-17 t130 改）：默认可靠封面（ffmpeg 抽帧+标题）为主路径，模板封面为高级。

    - 直剪（mode=direct）不要求确认稿，只校验底片/成片属于本作品；
    - 默认封面只需 ffmpeg，1080×1440 PNG，失败如实报错；
    - 模板封面要求模板环境 available + 已解锁；缺依赖/未解锁/渲染失败自动降级默认封面并说明；
    - 封面回写目标成片 cover_id（目标失效时保留封面、不挂接）。
    """
    direct = project.get('mode') == 'direct'
    artifacts = [a for a in STORE.list('artifact', 500) if a.get('project_id') == project['id']
                 and a.get('kind') in ('clean_video', 'packaged_video')]
    aid = str(data.get('artifact_id') or project.get('latest_artifact_id') or '')
    target = None
    if aid:
        target = STORE.get('artifact', aid)
        if (not target or target.get('project_id') != project['id']
                or target.get('kind') not in ('clean_video', 'packaged_video')):
            raise Conflict('封面只能挂到本作品的成片上。')
        if not direct and (target.get('script_version_id') != project.get('confirmed_version_id')
                           or project.get('draft_changed')):
            raise Conflict('封面只能挂到当前定稿的本作品成片上。')
    if target is None and artifacts:
        artifacts.sort(key=lambda a: str(a.get('updated_at') or a.get('created_at') or ''), reverse=True)
        target = artifacts[0]
    want_default = str(data.get('cover_kind') or '') == 'default' or not str(data.get('template_id') or '')
    template_note = ''
    template_id = ''
    fields = {}
    person = {'on': False}
    if not want_default:
        template_id, fields, person = cover_packaging.sanitize(data)
        fields['account'] = fields.get('account') or branding.account(STORE)
        if cover_packaging.requires_person(template_id):
            person['on'] = True
            if not avatar_service.path(STORE):
                template_note = '这套模板需要先上传「我的形象」；已改用默认封面。'
                want_default = True
        if not want_default and not cover_packaging.available():
            template_note = '封面模板环境缺少：' + '、'.join(cover_packaging.missing()) + '；已改用默认封面。'
            want_default = True
        if not want_default and not cover_packaging.unlocked(STORE, template_id):
            template_note = '这套模板还未解锁；已改用默认封面（可先解锁再试模板）。'
            want_default = True
    if want_default and not cover_packaging.default_available():
        raise ValueError('默认封面需要 ffmpeg；当前环境没有找到 ffmpeg。请先安装 ffmpeg 或使用模板封面。')
    title = str(data.get('title') or (project.get('publication_pack') or {}).get('selected_title')
                or project.get('title') or '')[:40]

    def _video_path():
        if target and Path(str(target.get('path') or '')).is_file():
            return str(target['path'])
        cand = [a for a in artifacts if Path(str(a.get('path') or '')).is_file()]
        if cand:
            cand.sort(key=lambda a: str(a.get('updated_at') or a.get('created_at') or ''), reverse=True)
            return str(cand[0]['path'])
        asset_ids = [project.get('current_asset_id')] + [a.get('id') for a in STORE.list('asset', 500)
                                                          if a.get('project_id') == project['id']]
        for asset_id in asset_ids:
            asset = STORE.get('asset', str(asset_id or '')) if asset_id else None
            if asset and Path(str(asset.get('path') or '')).is_file():
                return str(asset['path'])
        raise Conflict('还没有可抽帧的底片：先完成直剪/净剪出片或上传素材，再生成封面。')

    def _attach(cover, note=''):
        fresh = STORE.get('artifact', target['id']) if target else None
        ok = bool(fresh and fresh.get('project_id') == project['id']
                  and fresh.get('kind') in ('clean_video', 'packaged_video'))
        if ok and not direct:
            ok = fresh.get('script_version_id') == project.get('confirmed_version_id') and not project.get('draft_changed')
        if ok:
            STORE.patch('artifact', target['id'], {'cover_id': cover['id']})
        elif note:
            STORE.patch('artifact', cover['id'], {'note': note})
        return ok

    def work(job_id):
        out = DATA / 'packages' / job_id
        out.mkdir(parents=True, exist_ok=True)
        if want_default:
            STORE.patch('job', job_id, {'stage': '抽帧并写标题 · 默认封面'})
            try:
                png = cover_packaging.default_cover(out, _video_path(), title)
            except Exception as exc:
                raise ValueError(str(exc)[:400]) from None
            note = '默认可靠封面 1080×1440（ffmpeg 抽帧+标题）。' + ((' ' + template_note) if template_note else '')
            cover = STORE.put('artifact', {'project_id': project['id'],
                'script_version_id': project.get('confirmed_version_id'),
                'kind': 'cover', 'path': str(png), 'filename': '封面-默认.png',
                'info': {'width': cover_packaging.WIDTH, 'height': cover_packaging.HEIGHT},
                'cover_kind': 'default', 'cover_title': title, 'note': note})
            attached = _attach(cover, '' if target else '还没有可挂接的成片；封面已生成，出片后可再挂。')
            return {'artifact': require('artifact', cover['id']), 'cover_kind': 'default',
                    'attached': attached, 'fallback_reason': template_note,
                    'stage': '默认封面已生成' + ('并挂到成片' if attached else '（未挂接）'),
                    'cover_url': '/api/artifacts/' + cover['id']}
        # 模板路径：失败自动降级默认封面并说明
        try:
            STORE.patch('job', job_id, {'stage': '按模板渲染封面 · ' + template_id})
            png = cover_packaging.render(out, template_id, fields, person, avatar_service.path(STORE))
            width, height = cover_packaging._png_size(png)
            name = next((s['name'] for s in cover_packaging.styles() if s['id'] == template_id), template_id)
            cover = STORE.put('artifact', {'project_id': project['id'],
                'script_version_id': project.get('confirmed_version_id'),
                'kind': 'cover', 'path': str(png), 'filename': '封面-' + name + '.png',
                'info': {'width': width, 'height': height},
                'cover_template_id': template_id, 'cover_fields': fields, 'cover_person': person,
                'cover_kind': 'template', 'note': '模板封面 %d×%d。' % (width, height)})
            attached = _attach(cover, '原目标成片已变化，封面保留但未挂接；请在最新成片上重新生成。')
            return {'artifact': require('artifact', cover['id']), 'cover_kind': 'template',
                    'attached': attached, 'fallback_reason': '',
                    'stage': '模板封面已生成' + ('并挂到成片' if attached else '（未挂接）'),
                    'cover_url': '/api/artifacts/' + cover['id']}
        except Exception as exc:
            if not cover_packaging.default_available():
                raise ValueError('模板封面失败（%s），且默认封面不可用（缺 ffmpeg）。' % str(exc)[:200]) from None
            STORE.patch('job', job_id, {'stage': '模板失败，改用默认封面'})
            png = cover_packaging.default_cover(out, _video_path(), title)
            reason = '模板封面失败：%s；已自动改用默认封面。' % str(exc)[:160]
            cover = STORE.put('artifact', {'project_id': project['id'],
                'script_version_id': project.get('confirmed_version_id'),
                'kind': 'cover', 'path': str(png), 'filename': '封面-默认.png',
                'info': {'width': cover_packaging.WIDTH, 'height': cover_packaging.HEIGHT},
                'cover_kind': 'default', 'cover_title': title, 'note': reason})
            attached = _attach(cover, reason)
            return {'artifact': require('artifact', cover['id']), 'cover_kind': 'default',
                    'attached': attached, 'fallback_reason': reason,
                    'stage': '已用默认封面兜底', 'cover_url': '/api/artifacts/' + cover['id']}

    inputs = {'artifact_id': (target or {}).get('id', ''),
              'cover_kind': 'default' if want_default else 'template',
              'template_id': template_id, 'title': title, 'fields': fields, 'person': person}
    return new_job(project['id'], 'cover_render', inputs, work)


def render_caption_artifact(project,clean,cues,overlays,director_note,job_id,style=None,attach=True):
    out=DATA/'packages'/job_id;out.mkdir(exist_ok=True)
    script_id=project['confirmed_version_id'];asset={'id':clean['asset_id']}
    mapping=clean['mapping'];clean_path=Path(clean['path'])
    output=out/'packaged.mp4';pack=project.get('publication_pack') or {}
    details=media.render_packaged(clean_path,output,cues,pack.get('selected_title',project['title']),overlays,style)
    subtitles=out/'captions.srt'
    def srt_clock(ms):
        ms=round(ms);return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    subtitles.write_text('\n\n'.join(f'{i+1}\n{srt_clock(c["start_ms"])} --> {srt_clock(c["end_ms"])}\n{c["text"]}' for i,c in enumerate(cues)),encoding='utf-8-sig')
    cover_path=out/'cover.jpg';media.run([media.binary('ffmpeg'),'-y','-ss','0.5','-i',output,'-frames:v','1','-q:v','2',cover_path],60)
    cover=STORE.put('artifact',{'project_id':project['id'],'script_version_id':script_id,'kind':'cover',
        'path':str(cover_path),'filename':'封面.jpg','info':{'width':details['info']['width'],'height':details['info']['height']}})
    artifact=STORE.put('artifact',{'project_id':project['id'],'script_version_id':script_id,'asset_id':asset['id'],
        'kind':'packaged_video','path':str(output),'filename':'字幕包装-'+project['title'][:30]+'.mp4',
        **details,'mapping':mapping,'caption_cues':cues,'subtitle_path':str(subtitles),'cover_id':cover['id'],
        'overlays':overlays,'director_note':director_note,'clean_artifact_id':clean['id'],'caption_style':style})
    if attach:
        attach_if_current(project['id'],script_id,{'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None})
    return artifact


def prepare_video_job(project,asset,base_artifact=None):
    material_gate(project,asset)
    if not providers.available()['asr']:raise providers.ProviderMissing('本地字幕转写尚未就绪。')
    if base_artifact and (base_artifact.get('project_id')!=project['id'] or base_artifact.get('script_version_id')!=project['confirmed_version_id'] or base_artifact.get('kind')!='clean_video' or base_artifact.get('asset_id')!=asset['id'] or not base_artifact.get('mapping')):
        raise Conflict('这版清理与所选素材或稿件不一致，请核对后再包装。')
    with CONTENT_LOCK:
        for task in STORE.list('job',500):
            if task.get('project_id')==project['id'] and task['kind']=='prepare_video' and task['status'] in ('queued','running'):return task
        script_id=project['confirmed_version_id']
        def work(job_id):
            out=DATA/'packages'/job_id;out.mkdir()
            transcript=next((x for x in STORE.list('transcript',10000) if x.get('asset_id')==asset['id']),None)
            if transcript is None:
                STORE.patch('job',job_id,{'stage':'语音识别转写，保留原始字幕与时间戳'})
                audio=out/'speech.mp3';media.extract_audio(asset['path'],audio)
                transcript=STORE.put('transcript',{**providers.asr(audio,stage=lambda s:STORE.patch('job',job_id,{'stage':s})),
                    'asset_id':asset['id'],'project_id':project['id']})
            if base_artifact:
                clean=base_artifact;clean_path=Path(clean['path']);mapping=clean['mapping']
            else:
                STORE.patch('job',job_id,{'stage':'保守缩短长停顿，保留原始素材'})
                plan=media.silence_plan_compressed(asset['path'],asset['info'],'conservative')
                clean_path=out/'clean.mp4';mapping=media.render_cut(asset['path'],asset['info'],plan['segments'],clean_path)
                saved_plan=STORE.put('cut_plan',{**plan,'mapping':mapping,'project_id':project['id'],'asset_id':asset['id'],'script_version_id':script_id})
                clean=STORE.put('artifact',{'project_id':project['id'],'script_version_id':script_id,'asset_id':asset['id'],
                    'kind':'clean_video','path':str(clean_path),'filename':'清理.mp4','info':media.probe(clean_path),
                    'mapping':mapping,'plan_id':saved_plan['id']})
            # 2026-11-10（试用反馈 P0-3）：直剪/字幕包装与学习包装共用同一套词级回退，
            # 词级结果缺项的句子按句级时间保留并报数，不再静默丢句。
            cues,cap_stats=packaging_job.map_cues(transcript.get('cues') or [],mapping,transcript.get('words'))
            if not cues:raise ValueError('没有可映射到剪后时间轴的字幕；请先核对转写。')
            caption_note=('其中 %d 句词级结果缺失，已按句级时间保留；请对照原声核对字幕。'%cap_stats['fallback_cues']) if cap_stats['fallback_cues'] else ''
            overlays=[];director_note='仅使用真实字幕，未添加额外强调。'
            if providers.available()['llm']:
                STORE.patch('job',job_id,{'stage':'按实际讲稿内容挑选关键词强调'})
                try:
                    creative=providers.llm_json(prompt_store.get('subtitle.keywords'),{'cues':cues[:600]})
                    for item in creative.get('overlays',[])[:4]:
                        c=next((c for c in cues if c['id']==item.get('cue_id')),None);quote=str(item.get('quote','')).strip()
                        if c and quote and quote in c['text'] and len(quote)<=12:overlays.append({'cue_id':c['id'],'quote':quote,'reason':str(item.get('reason',''))[:300]})
                    director_note='DeepSeek 根据实际讲稿建议，强调引文已逐字回源。'
                except Exception:director_note='内容包装建议暂不可用，字幕出片继续执行。'
            STORE.patch('job',job_id,{'stage':'烧录字幕、封面标题和关键词强调'})
            artifact=render_caption_artifact(project,clean,cues,overlays,director_note,job_id,project.get('caption_style'))
            route={'local':'本机模型（未联网）','cloud_volc':'云端 · 火山豆包语音','cloud_external':'云端 · 自建适配器'}.get(
                transcript.get('asr_route'),'未标注')
            return {'artifact':artifact,'transcript':transcript,'stage':'字幕包装已完成，请播放审阅',
                'note':'本次转写走「%s」。默认仅保守清理静音；有声嗯啊仍需本人核对后按词删除。'%route
                       + ((' '+caption_note) if caption_note else '')}
        return new_job(project['id'],'prepare_video',{'asset_id':asset['id'],'script_version_id':script_id,'base_artifact_id':base_artifact['id'] if base_artifact else None},work)


def semantic_plan_valid(words,answer,duration_ms,max_deletions=SEMANTIC_MAX_DELETIONS):
    """Ground model deletions in existing word IDs; the backend derives every interval from source timestamps.
    Borrowed from disfluency research (reparandum/interregnum/repair): a repeat or restart may only be
    deleted when the more complete repair it points to actually exists and survives every deletion.
    Long transcripts occasionally yield a malformed suggestion; drop and record it per entry instead of
    failing the whole analysis. The global runaway guards (count cap, ratio cap) still hard-fail."""
    lookup={w['id']:w for w in words}
    raw=answer.get('deletions')
    if not isinstance(raw,list) or len(raw)>max_deletions:
        raise ValueError(f'删除建议格式无效或超过{max_deletions}条；未采纳，原始转写保留。')
    deletions=[];skipped=[];seen=set()
    def skip(item,reason):
        ids=item.get('word_ids') if isinstance(item,dict) else item
        text=''.join(lookup[k]['text'] for k in ids if isinstance(k,str) and k in lookup) if isinstance(ids,list) else str(ids)
        skipped.append({'text':text[:120],'reason':reason})
    for item in raw:
        if not isinstance(item,dict):skip(item,'建议不是对象');continue
        ids=item.get('word_ids')
        if not isinstance(ids,list) or not ids or not all(isinstance(k,str) for k in ids):
            skip(item,'缺少词ID列表');continue
        if any(k not in lookup for k in ids):
            skip(item,'引用了不存在的词ID');continue
        kind=item.get('type')
        if kind not in SEMANTIC_TYPES:skip(item,'删除类型无效');continue
        rep=item.get('repair_word_ids') or []
        if kind in ('repeat','restart'):
            if not isinstance(rep,list) or not rep or not all(isinstance(k,str) for k in rep):
                skip(item,'未指出保留下来的重说版本，按不是改口处理');continue
            if any(k not in lookup for k in rep):
                skip(item,'repair_word_ids 引用了不存在的词ID');continue
            rep=[k for k in dict.fromkeys(rep)]
            if set(rep)&set(ids):
                skip(item,'把它声称保留的重说版本也删了');continue
        else:rep=[]
        ids=[k for k in dict.fromkeys(ids) if k not in seen];seen.update(ids)
        if not ids:skip(item,'词ID已被其他建议占用');continue
        intervals=[[lookup[k]['start_ms'],lookup[k]['end_ms']] for k in ids]
        deletions.append({'id':f'del_{len(deletions)+1}','word_ids':ids,'type':kind,'type_label':SEMANTIC_TYPES[kind],
            'text':''.join(lookup[k]['text'] for k in ids),'intervals':intervals,
            'start_ms':min(a for a,_ in intervals),'end_ms':max(b for _,b in intervals),
            'repair_word_ids':rep,'repair_text':''.join(lookup[k]['text'] for k in rep),
            'kept_text':str(item.get('kept_text',''))[:300],'reason':str(item.get('reason',''))[:300],
            'needs_review':bool(item.get('needs_review'))})
    # R2 段级保护：重启密集区（≥2 处重启信号/删除点落在 20s 窗口）视为整段废 take，
    # 段内"完整句"不再触发 repair 保护——句级保护让位于段级裁决（本人手剪画像 R2）。
    changed=True
    purge_regions=anchor_job.dense_regions(list(words),[d['start_ms'] for d in deletions]) if words else []
    while changed:
        changed=False
        alive={k for d in deletions for k in d['word_ids']}
        for d in list(deletions):
            if d['repair_word_ids'] and set(d['repair_word_ids'])&(alive-set(d['word_ids'])):
                rep_ms=[(lookup[k]['start_ms'],lookup[k]['end_ms']) for k in d['repair_word_ids'] if k in lookup]
                if any(a>=rs and b<=re for a,b in rep_ms for rs,re in purge_regions):
                    d['region_purge']=True
                    continue
                deletions.remove(d);skipped.append({'text':d['text'][:120],'reason':'声称保留的重说版本被其他删除建议删掉了'})
                changed=True
    # 改口校验不过的不丢弃建议，但降级为需人工核对：删除侧与保留侧凑不成同一次重说
    # 或 kept_text 对不上保留词时，一键净剪不得自动采纳（实测会剪出「力呀」这种半句）。
    for d in deletions:
        if d['type'] in ('repeat','restart') and not d.get('region_purge') and not semantic_repair_ok(d,lookup):
            d['needs_review']=True
            note='保留侧与删除侧不成同一次重说或 kept_text 对不上保留词，需人工核对'
            d['reason']=(d['reason']+'；' if d['reason'] else '')+note
    # 2026-11-17（t103 治根）：t72 档 B 观察/enforce（原只在死块，现移植到活块）
    for d in deletions:
        if d.get('type')=='filler' and SEMANTIC_OBSERVE_TIER_B:
            ok,why=semantic_tier_b_observe(d,lookup)
            if ok:
                if SEMANTIC_TIER_B_ENFORCE and why!='pronoun-observe':
                    d['tier_b_auto']=True
                    d['observe']=False
                    d['needs_review']=False
                    d['reason']=(d['reason']+'；' if d['reason'] else '')+'档B enforce：纯语气词自动删（语义判定通过）'
                else:
                    d['observe']=True
                    d['needs_review']=True
                    d['reason']=(d['reason']+'；' if d['reason'] else '')+'档B上下文语气词先 observe（只建议不自动）'+(why=='pronoun-observe' and '：指代词永不自动' or '')
    # 2026-11-17（t103 治根）：t70 H1 纯子序列护栏（原只在死块，现移植到活块；只保留一份）
    deletions, h1_skipped = semantic_pure_subsequence_guard(words, deletions)
    skipped.extend(h1_skipped)
    total=sum(b-a for d in deletions for a,b in d['intervals'])
    if deletions and total>duration_ms*SEMANTIC_MAX_RATIO:
        raise ValueError(f'模型建议累计删除 {total/1000:.1f}s，超过素材时长的{SEMANTIC_MAX_RATIO:.0%}；未采纳任何内容，请复核后重新分析。')
    return deletions,skipped


def semantic_word_plain(text):
    """只保留汉字/字母/数字，用来判断一个词是不是固定词表里的纯语气词。"""
    return re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]','',text or '')


def semantic_repair_ok(d,lookup,order=None):
    """改口的保留侧必须和删除侧相邻、同属一次重说，kept_text 也要能对上保留词。

    只校验"repair 词存在且没被同删"不够：实测自动采纳过把「压力呀」剪成「力呀」的方案，
    那是删除侧和保留侧拼了两个不完整尝试。不能确认为同一次改口时一律不许自动执行。
    """
    ids=d.get('word_ids') or []
    rep=d.get('repair_word_ids') or []
    if not ids or not rep:return False
    order=order or {k:i for i,(k,_) in enumerate(lookup.items())}
    if any(k not in lookup or k not in order for k in list(ids)+list(rep)):return False
    del_idx=sorted(order[k] for k in ids);rep_idx=sorted(order[k] for k in rep)
    contiguous=lambda seq:all(b-a==1 for a,b in zip(seq,seq[1:]))
    if not contiguous(del_idx) or not contiguous(rep_idx):return False
    if del_idx[-1]<rep_idx[0]:lo,hi=del_idx[-1],rep_idx[0]
    elif rep_idx[-1]<del_idx[0]:lo,hi=rep_idx[-1],del_idx[0]
    else:return False
    between=[k for k,i in sorted(order.items(),key=lambda kv:kv[1]) if lo<i<hi]
    if len(between)>2:return False
    if any(semantic_word_plain(lookup[k].get('text','')) not in AUTO_FILLER_LEXICON for k in between):
        return False
    kept=semantic_word_plain(d.get('kept_text') or '')
    repair=semantic_word_plain(''.join(lookup[k].get('text','') for k in sorted(rep,key=order.get)))
    return bool(kept) and kept in repair


def semantic_norm_text(text):
    """H1 规范化：只留汉字/字母/数字（去标点空白），用于纯子序列断言。"""
    return re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', text or '')


def semantic_is_subsequence(keep, original):
    it = iter(original or '')
    return all(ch in it for ch in (keep or ''))


def semantic_pure_subsequence_guard(words, deletions):
    """H1：自动删除落地前断言「保留全文」是「原全文」的纯子序列（只删不增不改）。

    违例时**逐条撤销**删除建议（从后往前），留 skipped 痕迹，不整单失败。
    蓝本：ally_compose.py:286-297。返回 (deletions, skipped)。
    """
    if not deletions:
        return deletions, []
    lookup = {w['id']: w for w in words or []}
    orig = semantic_norm_text(''.join(str(w.get('text') or '') for w in (words or [])))
    dropped = []
    alive = list(deletions)
    while alive:
        dead = {k for d in alive for k in d.get('word_ids', [])}
        keep = semantic_norm_text(''.join(str(w.get('text') or '') for w in (words or [])
                                           if w.get('id') not in dead))
        if semantic_is_subsequence(keep, orig):
            break
        victim = alive.pop()
        dropped.append({'text': str(victim.get('text') or '')[:120],
                        'reason': 'H1 纯子序列断言不过（保留文本不是原转写的纯子序列），已撤销该条自动删除'})
    return alive, dropped


def semantic_auto_eligible(d,lookup):
    """一键净剪的自动选择规则：固定词表语气词 + 有保留重说的改口；其余一律留作建议。"""
    if d.get('needs_review') and not d.get('tier_b_auto'):return False
    if d.get('observe'):return False
    if d.get('tier_b_auto'):
        ids=d.get('word_ids') or []
        texts=[semantic_word_plain((lookup.get(k) or {}).get('text','')) for k in ids]
        if not ids or any(t not in FILLER_ONLY_A or t in FILLER_PRONOUN for t in texts):return False
        return sum(len(t) for t in texts)<=4
    if d.get('mechanical') in ('exact_repeat','semantic_repeat'):
        # P1-a 机械验证档：删除侧与保留侧逐字相同＋±2 词上下文一致，才允许自动（H1 再兜底）
        ids=d.get('word_ids') or [];rep=d.get('repair_word_ids') or []
        if not ids or not rep:return False
        dl=''.join(semantic_word_plain((lookup.get(k) or {}).get('text','')) for k in ids)
        kp=''.join(semantic_word_plain((lookup.get(k) or {}).get('text','')) for k in rep)
        if not dl or dl!=kp:return False
        words_list=list(lookup.values())
        # 2026-11-17（t86）：语义验证通过才自动；未过语义（exact_repeat 旧档）一律不自动。
        if d.get('mechanical')=='semantic_repeat' and not d.get('semantic_ok'):return False
        return semantic_repeat_context_ok(words_list,ids,rep)
    if d.get('type')=='filler':
        if not all(semantic_word_plain(lookup.get(k,{}).get('text','')) in AUTO_FILLER_LEXICON for k in d.get('word_ids',[])):
            return semantic_filler_tierA_ok(d.get('word_ids'),lookup)
        # 2026-11-17（t70 P0-b）：旧词表命中也要过档 A 纪律（≤4字、非指代；整行纯语气由 LLM 建议保证）
        return semantic_filler_tierA_ok(d.get('word_ids'),lookup)
    if d.get('type') in ('repeat','restart'):
        return semantic_repair_ok(d,lookup)
    return False


def semantic_filler_plan(words,duration_ms):
    """无生成模型时的兜底：只按固定词表删纯语气词，切点仍由 semantic_plan_valid 回源。"""
    items=[];i=0
    while i<len(words):
        w=words[i]
        if semantic_word_plain(w.get('text','')) not in AUTO_FILLER_LEXICON:
            i+=1;continue
        j=i
        while j+1<len(words) and semantic_word_plain(words[j+1].get('text','')) in AUTO_FILLER_LEXICON:j+=1
        prev_end=words[i-1]['end_ms'] if i else None
        next_start=words[j+1]['start_ms'] if j+1<len(words) else None
        # 前后都紧贴正文才算句中语气词；独立成句的“嗯”可能是在回答，不自动删。
        if next_start is not None and next_start-words[j]['end_ms']<=600 and (prev_end is None or w['start_ms']-prev_end<=600):
            items.append({'word_ids':[x['id'] for x in words[i:j+1]],'type':'filler','repair_word_ids':[],
                          'kept_text':'','reason':'固定词表纯语气词，前后紧贴正文','needs_review':False})
        i=j+1
    cap=max(SEMANTIC_MAX_DELETIONS,len(words)//20)
    return semantic_plan_valid(words,{'deletions':items},duration_ms,max_deletions=cap)


def semantic_trim_silence(removed,words,pad_ms=30,min_keep_ms=80):
    """静音检测按能量判停顿，ASR词尾常比实际发声多占几十毫秒。
    真实样本里出现过静音段压住“么”最后51ms；这里把静音删除段从所有保留词的区间里让开，
    宁可少删一点停顿，也不切进字头字尾。语义删除的整词由调用方排除，不在此处保护。"""
    spans=[(w['start_ms']-pad_ms,w['end_ms']+pad_ms) for w in words]
    out=[]
    for seg in removed:
        keep=[(int(seg['start_ms']),int(seg['end_ms']))]
        for a,b in spans:
            nxt=[]
            for x,y in keep:
                if b<=x or a>=y:nxt.append((x,y));continue
                if x<a:nxt.append((x,min(a,y)))
                if b<y:nxt.append((max(b,x),y))
            keep=nxt
        for x,y in keep:
            if y-x>=min_keep_ms:out.append({**seg,'start_ms':x,'end_ms':y})
    return out


SEMANTIC_REPEAT_SYSTEM = (
    "你是口播剪辑的重复判定员。给若干「候选重复对」：每对有删除侧片段D、保留侧片段K，"
    "以及两侧前后各 3 个词的上下文。只判一件事：D 与 K 是否**同义重复**（说了两遍同一件事）。\n"
    "判重复：原样重说、同义改写、车轱辘话（换词不换意）。\n"
    "判不重复：数字/时间/条件/对象不同（如「八十多岁」vs「五十多岁」）、语义不同、举例不同、"
    "前后上下文指向不同事实。\n"
    "拿不准按不重复（宁少删）。输出 JSON 对象：{\"decisions\":[{\"i\":序号,\"same\":true/false,\"reason\":\"一句话\"}]}。"
)


def semantic_repeat_scan(cands, words, llm=None, max_cands=20):
    """LLM 语义扫描：判断规则候选是否同义重复。返回 {i: (same, reason)}；失败返回 None。"""
    cands = list(cands or [])[:max_cands]
    if not cands:
        return {}
    order = {w['id']: i for i, w in enumerate(words or [])}
    norm = [semantic_word_plain(w.get('text', '')) for w in words]

    def ctx(ids, before=3, after=3):
        idx = [order[k] for k in ids if k in order]
        if not idx:
            return '', ''
        i0, i1 = idx[0], idx[-1]
        return ''.join(norm[max(0, i0 - before):i0]), ''.join(norm[i1 + 1:i1 + 1 + after])

    payload = []
    for i, c in enumerate(cands):
        db, da = ctx(c['del_word_ids'])
        kb, ka = ctx(c['keep_word_ids'])
        payload.append({'i': i, 'D': c['text'], 'K': c['text'],
                        'D_before': db, 'D_after': da, 'K_before': kb, 'K_after': ka})
    try:
        answer = (llm or __import__('providers').llm_json)(SEMANTIC_REPEAT_SYSTEM, {'candidates': payload})
    except Exception:
        return None
    rows = answer.get('decisions') if isinstance(answer, dict) else None
    if not isinstance(rows, list):
        return None
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get('i'))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(cands):
            out[i] = (bool(r.get('same')), str(r.get('reason') or '')[:120])
    return out or None


def semantic_repeat_context_ok(words, del_ids, keep_ids, span=2):
    """机械原样重复的上下文闸：删除侧与保留侧需前后 ±span 个词逐字一致。

    例：「平均寿命**八十多岁**」vs「平均寿命**五十多岁**」——片段同为「平均寿命」，
    但后接词 八/五 不同 → 不判重复（防误删）；真重复（后接上下文也一致）→ 判重复。
    """
    del_ids = list(del_ids or [])
    keep_ids = list(keep_ids or [])
    if not del_ids or not keep_ids:
        return False
    order = {w['id']: i for i, w in enumerate(words or [])}
    if any(k not in order for k in del_ids + keep_ids):
        return False
    norm = [semantic_word_plain(w.get('text', '')) for w in words]
    for side in (del_ids, keep_ids):
        idx = sorted(order[k] for k in side)
        if any(b - a != 1 for a, b in zip(idx, idx[1:])):
            return False  # 片段不连续，交给别的校验
    di = order[del_ids[0]]
    ki = order[keep_ids[0]]
    n = len(del_ids)
    # 前后各 span 个词必须一致（越界视为不一致，宁少删）
    for off in range(1, span + 1):
        d_before = norm[di - off] if di - off >= 0 else None
        k_before = norm[ki - off] if ki - off >= 0 else None
        d_after = norm[di + n - 1 + off] if di + n - 1 + off < len(norm) else None
        k_after = norm[ki + n - 1 + off] if ki + n - 1 + off < len(norm) else None
        # 一侧到边界（无上下文）时跳过该侧比较；两侧都有上下文才必须逐字一致。
        if d_before is not None and k_before is not None and d_before != k_before:
            return False
        if d_after is not None and k_after is not None and d_after != k_after:
            return False
    return True


def semantic_windows(total, size=None, overlap=None):
    """P1-a 分窗：500 词一窗、100 词重叠（去尾重复窗）。返回 [(start,end)]。"""
    size = size or SEMANTIC_WINDOW_WORDS
    overlap = SEMANTIC_WINDOW_OVERLAP if overlap is None else overlap
    stride = max(1, size - overlap)
    if total <= 0:
        return []
    windows = [(i, min(i + size, total)) for i in range(0, total, stride)]
    if len(windows) > 1 and windows[-1][0] < windows[-2][1] and windows[-1][1] == total:
        windows = windows[:-1]
    return windows


def semantic_tier_b_observe(d, lookup):
    """P1-b 档 B：上下文语气词（啊/那个/就是说…）过三关后只 observe，不自动。

    三关：①reason 非空；②词形机械闸（全为语气词表成员；指代词「那个」单列）；
    ③音频可行性（词时间戳完整且 end>start）。指代词永不自动。
    """
    ids = d.get('word_ids') or []
    if not ids:
        return False, 'no-words'
    if not str(d.get('reason') or '').strip():
        return False, 'no-reason'
    texts = [semantic_word_plain(lookup.get(k, {}).get('text', '')) for k in ids]
    if any(t not in CONTEXT_FILLER and t not in AUTO_FILLER_LEXICON for t in texts):
        return False, 'not-filler'
    if any(t in FILLER_PRONOUN for t in texts):
        return True, 'pronoun-observe'   # 指代词：可建议，但永远 observe/需人工
    for k in ids:
        w = lookup.get(k) or {}
        try:
            if int(w.get('end_ms')) <= int(w.get('start_ms')):
                return False, 'audio-infeasible'
        except (TypeError, ValueError):
            return False, 'audio-infeasible'
    return True, 'observe'


def semantic_llm_deletions(job_id,words):
    prompt=f'你是口播视频剪掉停顿和口误编辑。输入是本地转写的逐词列表（id、text、开始秒）。口语不流畅的标准结构是三段：废稿（先说出口的不完整或错误版本）+ 语气词（嗯、呃）+ 重说（完整正确的版本）。删除只针对废稿和语气词，重说永远保留。找出应当从成片中实际剪掉的口语残留，只输出删除建议。规则：1) 只能引用输入中已存在的词id，不得编造id或时间；2) type为repeat或restart时必须给出repair_word_ids，指出保留下来的更完整重说版本的词id；repair_word_ids必须指向最终保留在成片里的词，不得指向其他删除建议要删的词；若通篇找不到更完整的重说，该内容不是改口，不得删除；3) 同一内容说了多次时，保留表达最完整、事实条件最齐全的那一次，删其余各次，repair_word_ids指向保留版；4) 不得建议删除否定词、数字或适用条件；改口前后语义相反时保留改口后的结果；5) 用于强调的有意重复必须保留；6) filler只能是纯语气口误（嗯、呃、啊、那个、就是说），不得包含实义词；7) 输出前自检：把所有保留的词按顺序连成全文通读一遍，必须通顺成话；发现悬空半句、条件缺失或逻辑断裂就撤销该条删除，拿不准的设 needs_review=true 并在 reason 里说明要试听什么；8) 每条给出保留下来的替代说法 kept_text 和一句中文理由。没有可删内容时返回空 deletions 数组，不要硬凑。仅返回JSON对象：{{deletions:[{{word_ids:[],repair_word_ids:[],type:"repeat"|"restart"|"filler",kept_text,reason,needs_review}}]}}。filler 的 repair_word_ids 为空数组。deletions最多{SEMANTIC_MAX_DELETIONS}条。'
    # 2026-11-17（t72 P1-a）：500 词分窗 + 100 词重叠，跨窗重复不再被边界切断。
    windows=semantic_windows(len(words))
    raw=[];seen=set()
    rule_hits=[c for c in anchor_job.repeat_candidates(words) if semantic_repeat_context_ok(words,c['del_word_ids'],c['keep_word_ids'])]
    # 2026-11-17（t86）：规则候选交 LLM 语义扫描——数字/语义不同（八十 vs 五十）不判重复。
    _scan=semantic_repeat_scan(rule_hits,words)
    semantic_ok_hits=[]
    for _i,_c in enumerate(rule_hits):
        _same,_reason=(_scan or {}).get(_i,(False,'语义扫描不可用，按不重复处理'))
        if _same:
            _c=dict(_c); _c['semantic_reason']=_reason; semantic_ok_hits.append(_c)
    rule_hits=semantic_ok_hits
    for ci,(a,b) in enumerate(windows,1):
        chunk=words[a:b]
        hint=''
        local=[[w['id'] for w in chunk].index(k) for k in []]
        ids=[w['id'] for w in chunk]
        inwin=[]
        for cand in rule_hits:
            if cand['del_word_ids'][0] in ids and cand['keep_word_ids'][0] in ids:
                inwin.append('删除%s↔保留%s(%s)' % (''.join(cand['del_word_ids'][:6]),''.join(cand['keep_word_ids'][:6]),cand['text'][:8]))
        if inwin:
            hint='\n规则检测到本段内的原样重复（仅作提示，仍需按规则2/5判断）：'+'；'.join(inwin[:6])
        STORE.patch('job',job_id,{'stage':f'正在分析重复表达、改口与有声口误（第{ci}/{len(windows)}段）'})
        answer=providers.llm_json(prompt+hint,{'words':[{'id':w['id'],'text':w['text'],'start_s':round(w['start_ms']/1000,2)} for w in chunk]})
        part=answer.get('deletions')
        if not isinstance(part,list):
            raise ValueError('模型没有按约定返回删除建议列表；未采纳任何内容。')
        for item in part:
            key=tuple(sorted(str(k) for k in (item.get('word_ids') or [])))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            raw.append(item)
    # 2026-11-17（t72 P1-a）：非紧邻「原样重复」机械验证档（删除侧⊆保留侧逐字相同），
    # 直接补进建议（reason/kept_text 齐全，过 H1 后可按机械档自动；灰区仍 observe）。
    for cand in rule_hits:
        raw.append({'word_ids':cand['del_word_ids'],'repair_word_ids':cand['keep_word_ids'],
                    'type':'repeat','kept_text':cand['text'],
                    'reason':'语义扫描：与前文同义重复（%s）' % (cand.get('semantic_reason') or ''),
                    'needs_review':False,'mechanical':'semantic_repeat','semantic_ok':True})
    return raw


def semantic_cut_job(project,asset):
    if not providers.available()['llm']:raise providers.ProviderMissing('剪掉停顿和口误分析需要生成模型；可先用按词手动删除。')
    if not providers.available()['asr']:raise providers.ProviderMissing('本地字幕转写尚未就绪。')
    material_gate(project,asset)
    script_id=project['confirmed_version_id']
    def work(job_id):
        out=DATA/'packages'/job_id;out.mkdir(exist_ok=True)
        transcript=next((x for x in STORE.list('transcript',10000) if x.get('asset_id')==asset['id']),None)
        if transcript is None:
            STORE.patch('job',job_id,{'stage':'语音识别转写，保留原始字幕与时间戳'})
            audio=out/'speech.mp3';media.extract_audio(asset['path'],audio)
            transcript=STORE.put('transcript',{**providers.asr(audio,stage=lambda s:STORE.patch('job',job_id,{'stage':s})),
                'asset_id':asset['id'],'project_id':project['id']})
        words=transcript.get('words',[])
        if not words:raise ValueError('这份转写没有词级时间戳，无法建立可回源的删除计划。')
        raw=semantic_llm_deletions(job_id,words)
        cap=max(SEMANTIC_MAX_DELETIONS,len(words)//20)
        deletions,skipped=semantic_plan_valid(words,{'deletions':raw},asset['info']['duration_ms'],max_deletions=cap)
        by_word={k:d['id'] for d in deletions for k in d['word_ids']}
        preview=[[w['text'],by_word.get(w['id'])] for w in words[:3000]]
        lookup={w['id']:w for w in words}
        plan=STORE.put('semantic_plan',{'project_id':project['id'],'asset_id':asset['id'],'transcript_id':transcript['id'],
            'script_version_id':script_id,'deletions':deletions,'skipped':skipped,'preview':preview,'word_count':len(words),
            'auto_eligible_ids':[d['id'] for d in deletions if semantic_auto_eligible(d,lookup)],
            'duration_ms':asset['info']['duration_ms'],'method':'llm_word_id_grounded',
            'note':'模型只引用已存在的词ID，毫秒区间由后端从源时间戳回源生成；尚未剪切任何音视频。'})
        stage=f'已生成 {len(deletions)} 条删除建议'
        if skipped:stage+=f'（另有 {len(skipped)} 条未通过校验，已丢弃并保留原文）'
        return {'plan':plan,'transcript':transcript,'requires_action':bool(deletions),
            'stage':stage+'，请核对勾选后执行剪切' if deletions else '未发现明确的重复或改口；可试听原片确认，或稍后重新分析'}
    return new_job(project['id'],'semantic_propose',{'asset_id':asset['id'],'script_version_id':script_id},work)


def render_semantic_clean(project,asset,plan,chosen,silence_mode,script_id,job_id,auto=False):
    """把选中的删除项 + 静音清理真正落成一条 clean_video；手动与一键净剪共用这一条链路。"""
    transcript=require('transcript',plan['transcript_id'])
    lookup={w['id']:w for w in transcript.get('words',[])}
    removed=[]
    for d in chosen:
        for k in d['word_ids']:
            w=lookup.get(k)
            if not w:raise ValueError('建议对应的原始词时间戳已变化，请重新分析；未剪切任何内容。')
            removed.append({'start_ms':w['start_ms'],'end_ms':w['end_ms'],'reason':'semantic:'+d['id']})
    # 2026-11-17（t70 P0-a）：统一边界函数（cue 吸附/受限 pad/夹回原剪点）。
    cues=transcript.get('cues') or []
    boundary_meta={}
    if cues and removed:
        _r,_k,boundary_meta=media.boundary_plan(transcript.get('words',[]),removed,
                                                asset['info']['duration_ms'],cues)
        removed=[{'start_ms':x['start_ms'],'end_ms':x['end_ms'],'reason':'semantic'} for x in _r]
    STORE.patch('job',job_id,{'stage':'切点吸附到邻近静音，避免切进字头字尾'})
    silences=media.silence_intervals(asset['path']) if asset['info']['audio'] else []
    removed=media.snap_edges_to_silence(silences,removed)
    STORE.patch('job',job_id,{'stage':'与保守静音清理合并，形成源时间保留计划'})
    silence=media.silence_plan_compressed(asset['path'],asset['info'],silence_mode)
    protected={k for d in chosen for k in d['word_ids']}
    kept_words=[w for w in transcript.get('words',[]) if w.get('id') not in protected]
    removed+=semantic_trim_silence([{**x,'reason':'silence'} for x in silence['removed']],kept_words)
    segments,merged=media.combine_removed(asset['info']['duration_ms'],removed)
    STORE.patch('job',job_id,{'stage':'按源时间线剪切并编码'})
    output=DATA/'artifacts'/(job_id+'.mp4')
    mapping=media.render_cut(asset['path'],asset['info'],segments,output)
    info=media.probe(output)
    expected=sum(x['end_ms']-x['start_ms'] for x in segments)
    diff=abs(info['duration_ms']-expected)
    # 2026-11-10（试用反馈 P0-4）：与 anchor/direct 同型，毫秒级差异标需复核而非直报失败。
    if diff>max(2000,expected*0.05):
        raise ValueError(f'成片时长 {info["duration_ms"]}ms 与保留计划 {expected}ms 相差 {diff}ms，超出可播放容差；中间文件保留：{output}')
    needs_review=diff>200
    previous=STORE.get('project',project['id']) or project
    previous_artifact_id=previous.get('latest_artifact_id') or ''
    saved=STORE.put('cleanup_plan',{'segments':segments,'removed':merged,'method':'semantic_plus_silence',
        'silence_mode':silence_mode,
        'edge_snap':{'window_ms':250,'extend_ms':200,'source':'ffmpeg_silencedetect'},
        'semantic_plan_id':plan['id'],'applied_deletions':[d['id'] for d in chosen],
        'asset_id':asset['id'],'script_version_id':script_id,'mapping':mapping})
    artifact=STORE.put('artifact',{'project_id':project['id'],'script_version_id':script_id,'asset_id':asset['id'],
        'kind':'clean_video','path':str(output),'filename':('一键净剪-' if auto else '剪掉停顿和口误-')+project['title'][:30]+'.mp4',
        'info':info,'plan_id':saved['id'],'semantic_plan_id':plan['id'],'mapping':mapping,
        'previous_artifact_id':previous_artifact_id,'auto_applied':bool(auto),
        'needs_review':needs_review,'duration_diff_ms':diff})
    stale_note=''
    if project.get('mode')=='direct':
        if not attach_direct(project['id'],asset,{'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None}):
            stale_note='注意：处理期间导入了新素材，这版结果保留在记录里，但没有设为当前成片。'
    else:
        attach_if_current(project['id'],script_id,{'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None})
    covered={d['id'] for d in chosen}
    skipped=[d for d in plan.get('deletions',[]) if d['id'] not in covered]
    summary=[{'id':d['id'],'text':d['text'],'type_label':d['type_label'],'reason':d['reason'],
              'start_ms':d['start_ms'],'end_ms':d['end_ms']} for d in chosen]
    if auto:
        note=(f'已自动剪除 {len(chosen)} 处高置信度内容并清理静音；原始素材、转写与旧版本保留。'
              +f'另外 {len(skipped)} 条模糊建议没有自动删，仍在列表里可逐条勾选。' if skipped else
              f'已自动剪除 {len(chosen)} 处高置信度内容并清理静音；原始素材、转写与旧版本保留。')
    else:
        note=(f'已实际剪除 {len(chosen)} 处勾选区间的音视频；原始素材、转写与旧版本保留。'
              +(f'未勾选的 {len(skipped)} 条建议没有删除。' if skipped else ''))
    return {'artifact':artifact,'plan':saved,'summary':summary,'skipped_count':len(skipped),
            'stage':'剪掉停顿和口误完成，请试听审阅'+(f'（时长与保留计划相差 {diff}ms，需人工核对音画）' if needs_review else ''),'note':stale_note+note+'字幕包装请点「保留这版剪切，添加字幕包装」，字幕会按新时间轴重算。'}


def semantic_apply_job(project,asset,plan,deletion_ids,silence_mode='conservative',auto_apply=False):
    if silence_mode not in ('conservative','natural','tight'):
        raise ValueError('停顿清理档位无效，仅支持 conservative/natural/tight。')
    if project.get('mode')=='direct':
        if plan.get('asset_id')!=asset['id'] or asset.get('id')!=project.get('current_asset_id') or asset.get('material_version')!=project.get('material_version'):
            raise Conflict('这份清理建议对应的素材已不是当前素材，请在最新素材上重新分析。')
    elif project.get('draft_changed') or project.get('confirmed_version_id')!=plan.get('script_version_id') or plan.get('asset_id')!=asset['id'] or asset.get('script_version_id')!=project['confirmed_version_id']:
        raise Conflict('这份清理建议与当前定稿或素材不一致，请重新分析。')
    wanted=set()
    if not auto_apply:
        if not isinstance(deletion_ids,list) or not deletion_ids or not all(isinstance(k,str) for k in deletion_ids):
            raise ValueError('请先勾选要执行删除的条目。')
        wanted=set(deletion_ids)
        chosen=[d for d in plan.get('deletions',[]) if d['id'] in wanted]
        if len(chosen)!=len(wanted):raise ValueError('勾选的删除项不属于这份建议。')
    script_id=project['confirmed_version_id']
    def work(job_id):
        transcript=require('transcript',plan['transcript_id'])
        lookup={w['id']:w for w in transcript.get('words',[])}
        chosen=[d for d in plan.get('deletions',[]) if d['id'] in wanted]
        if auto_apply:
            chosen=[d for d in plan.get('deletions',[]) if semantic_auto_eligible(d,lookup)]
        result=render_semantic_clean(project,asset,plan,chosen,silence_mode,script_id,job_id,auto=auto_apply)
        if auto_apply:
            result['stage']=f'一键净剪完成：自动采纳 {len(chosen)} 条，请试听审阅'
            result['auto_selected']=[d['id'] for d in chosen]
        return result
    payload={'plan_id':plan['id'],'deletion_ids':sorted(wanted),'auto_apply':bool(auto_apply)}
    return new_job(project['id'],'semantic_cut',payload,work)


def semantic_auto_job(project,asset,silence_mode='conservative'):
    """一键净剪：有模型时分析后自动采纳高置信度；无模型时只删静音+固定词表语气词。"""
    if silence_mode not in ('conservative','natural','tight'):
        raise ValueError('停顿清理档位无效，仅支持 conservative/natural/tight。')
    material_gate(project,asset)
    existing=next((x for x in STORE.list('transcript',10000) if x.get('asset_id')==asset['id']),None)
    if existing is None and not providers.available()['asr']:
        raise providers.ProviderMissing('一键净剪需要先有词级转写；本地转写未就绪时，可先在有网络的环境转写一次，或手动按词删除。')
    script_id=project['confirmed_version_id']
    def work(job_id):
        out=DATA/'packages'/job_id;out.mkdir(exist_ok=True)
        transcript=next((x for x in STORE.list('transcript',10000) if x.get('asset_id')==asset['id']),None)
        if transcript is None:
            STORE.patch('job',job_id,{'stage':'语音识别转写，保留原始字幕与时间戳'})
            audio=out/'speech.mp3';media.extract_audio(asset['path'],audio)
            transcript=STORE.put('transcript',{**providers.asr(audio,stage=lambda s:STORE.patch('job',job_id,{'stage':s})),
                'asset_id':asset['id'],'project_id':project['id']})
        words=transcript.get('words',[])
        if not words:raise ValueError('这份转写没有词级时间戳，无法建立可回源的删除计划。')
        if providers.available()['llm']:
            raw=semantic_llm_deletions(job_id,words)
            cap=max(SEMANTIC_MAX_DELETIONS,len(words)//20)
            deletions,skipped=semantic_plan_valid(words,{'deletions':raw},asset['info']['duration_ms'],max_deletions=cap)
            method='llm_word_id_grounded'
        else:
            STORE.patch('job',job_id,{'stage':'无生成模型：按固定词表清纯语气词，静音清理照常执行'})
            deletions,skipped=semantic_filler_plan(words,asset['info']['duration_ms'])
            method='deterministic_filler'
        lookup={w['id']:w for w in words}
        eligible=[d for d in deletions if semantic_auto_eligible(d,lookup)]
        if not (transcript.get('cues') or []):
            eligible=[]
            skipped=list(skipped)+[{'text':'','reason':'没有可吸附的字幕行界，对齐不确定；本次不自动删，建议留在列表里人工核对'}]
            STORE.patch('job',job_id,{'stage':'对齐不确定：不自动删，只保留建议'})
        by_word={k:d['id'] for d in deletions for k in d['word_ids']}
        preview=[[w['text'],by_word.get(w['id'])] for w in words[:3000]]
        plan=STORE.put('semantic_plan',{'project_id':project['id'],'asset_id':asset['id'],'transcript_id':transcript['id'],
            'script_version_id':script_id,'deletions':deletions,'skipped':skipped,'preview':preview,'word_count':len(words),
            'duration_ms':asset['info']['duration_ms'],'method':method,'auto_mode':True,
            'auto_eligible_ids':[d['id'] for d in eligible],
            'note':'一键净剪只自动采纳固定词表纯语气词，以及有保留重说的改口；其余模糊建议不自动删。'})
        result=render_semantic_clean(project,asset,plan,eligible,silence_mode,script_id,job_id,auto=True)
        artifact=result['artifact'];original_s=round(asset['info']['duration_ms']/1000,1)
        new_s=round(artifact['info']['duration_ms']/1000,1)
        pct=round((1-artifact['info']['duration_ms']/asset['info']['duration_ms'])*100,1) if asset['info']['duration_ms'] else 0
        updated=STORE.patch('semantic_plan',plan['id'],{'auto_applied':True,'auto_artifact_id':artifact['id'],
            'auto_selected_ids':[d['id'] for d in eligible],'auto_original_seconds':original_s,
            'auto_new_seconds':new_s,'auto_reduction_pct':pct})
        result.update({'plan':updated,'auto':True,'auto_selected':[d['id'] for d in eligible],
            'auto_original_seconds':original_s,'auto_new_seconds':new_s,'auto_reduction_pct':pct,
            'remaining_suggestions':len(deletions)-len(eligible),'requires_action':False,
            'stage':f'一键净剪完成：自动采纳 {len(eligible)} 条，{original_s}s → {new_s}s（降 {pct}%）'})
        return result
    return new_job(project['id'],'semantic_auto',{'asset_id':asset['id'],'script_version_id':script_id},work)


def semantic_undo(project,artifact):
    """撤销一条净剪成片：回到上一版；没有上一版就回到原片（latest 置空，素材保留）。"""
    if artifact.get('project_id')!=project['id'] or artifact.get('kind')!='clean_video' or not artifact.get('semantic_plan_id'):
        raise Conflict('这条成片不是可撤销的净剪版本。')
    current=STORE.get('project',project['id']) or project
    if current.get('latest_artifact_id')!=artifact['id']:
        raise Conflict('只有当前正在播放的这一版净剪才能撤销。')
    previous_id=artifact.get('previous_artifact_id') or ''
    previous=STORE.get('artifact',previous_id) if previous_id else None
    if previous_id and (not previous or previous.get('project_id')!=project['id']):
        previous_id=''
    attach_if_current(project['id'],artifact.get('script_version_id'),{'latest_artifact_id':previous_id,
        'stage':'review' if previous_id else 'edit','reviewed_artifact_id':None})
    STORE.patch('artifact',artifact['id'],{'undone_at':now()})
    return {'artifact':previous,'restored_artifact_id':previous_id,
            'original_seconds':round((previous or {}).get('info',{}).get('duration_ms',0)/1000,1) if previous_id else None,
            'note':'已撤销这次净剪，回到上一版。' if previous_id else '已撤销这次净剪，回到原片；原始素材仍保留在素材列表。'}


def latest_clean_plan(asset,script_id):
    """取这份素材当前定稿下最新的保留计划。前端不传区间，一律后端回源。"""
    for plan in STORE.list('cleanup_plan',500):
        if (plan.get('asset_id')==asset['id'] and plan.get('script_version_id')==script_id
                and plan.get('segments') and plan.get('method')!='rebuild_from_clean_plan'):
            return plan
    return None


def rebuild_estimate(asset,script_id,target_s):
    """秒级估算：不碰 ffmpeg，只做段治理 / 切口预算 / 变速可得性。

    静音吸附要解码整条音频，放到渲染任务里做；这里只回答"这条素材能不能到目标时长"。
    """
    clean_plan=latest_clean_plan(asset,script_id)
    if not clean_plan:
        return {'available':False,'message':'这份素材还没有清理结果。先做剪掉停顿和口误，再做剪辑清理/成片重建。'}
    raw=clean_plan.get('segments') or []
    segments=[s for s in raw if s['end_ms']-s['start_ms']>=400]
    if not segments:raise ValueError('这份保留计划没有可用片段，请先做清理。')
    governed=rebuild.govern(segments)
    net_raw=sum(s['end_ms']-s['start_ms'] for s in governed)/1000.0
    params_view={k:rebuild.PARAMS[k] for k in ('avg_seg_s','speed_max','min_seg_s')}
    try:
        net,reachable=rebuild.check_reachable(governed,target_s)
    except rebuild.Unreachable as exc:
        return {'available':True,'reachable':False,'message':str(exc),
                'net_seconds':round(net_raw,1),'target_seconds':target_s,'params':params_view}
    rows,limit,merges=rebuild.budget(governed,target_s)
    if sum(s['end_ms']-s['start_ms'] for s in rows)/1000.0/rebuild.PARAMS['speed_max']>target_s*1.05:
        rows,limit,merges=governed,len(governed),[]
    net_capped=sum(s['end_ms']-s['start_ms'] for s in rows)/1000.0
    base=max(1.0,min(rebuild.PARAMS['speed_max'],net_capped/target_s)) if target_s else 1.0
    return {'available':True,'plan_id':clean_plan['id'],'plan_method':clean_plan.get('method'),
            'net_seconds':round(net,1),'reachable_seconds':round(reachable,1),
            'governed':len(governed),'budget_limit':limit,'segments':len(rows),
            'cuts':max(0,len(rows)-1),'base_speed':round(base,3),
            'estimated_seconds':round(net_capped/base,1) if base else None,
            'target_seconds':target_s,'reachable':True,'params':params_view}


def textcut_state(asset,transcript,script_id):
    """文字剪片一进来要看的真实状态：这份素材现在的清理方案，哪些词已经被剪了。

    微调的前提是面板显示的就是成片真实的样子，而不是"一张没动过的稿子"。
    没有清理方案时就是全保留。
    """
    duration=asset['info']['duration_ms']
    plan=latest_clean_plan(asset,script_id)
    if not plan or not plan.get('segments'):
        return {'has_plan':False,'plan_id':None,'plan_method':None,'deleted_word_ids':[],
                'keep':[{'start_ms':0,'end_ms':duration}],'removed':[],
                'original_seconds':round(duration/1000.0,2),'keep_seconds':round(duration/1000.0,2),
                'removed_seconds':0.0,'segments':None,'cuts':0}
    ids,removed=media.deleted_word_ids(transcript.get('words',[]),plan['segments'],duration)
    kept=sum(s['end_ms']-s['start_ms'] for s in plan['segments'])/1000.0
    return {'has_plan':True,'plan_id':plan['id'],'plan_method':plan.get('method'),
            'deleted_word_ids':sorted(ids),
            'keep':[{'start_ms':s['start_ms'],'end_ms':s['end_ms']} for s in plan['segments']],
            'removed':removed,'ok':True,
            'original_seconds':round(duration/1000.0,2),'keep_seconds':round(kept,2),
            'removed_seconds':round(duration/1000.0-kept,2),
            'segments':len(plan['segments']),'cuts':max(0,len(plan['segments'])-1)}


def textcut_estimate(words,deleted_ids,duration_ms,base_removed=None):
    """文字剪片预估：只算区间，不渲染。

    与 `remove-words` 共用同一个 `media.word_cut_ranges`，所以这里显示的
    「剪后剩多少秒」就是出片后的真实秒数；返回的 keep 区间同时给前端做
    播放预览（播到被剪掉的地方就跳过去），预览与出片是同一份区间。

    base_removed 是当前清理方案已经剪掉的区间：手动微调以它为基底，恢复一个词
    只把那一段捞回来，不会把之前剪好的内容一起还原。
    """
    ids=[k for k in (deleted_ids or []) if isinstance(k,str)]
    if ids and len(ids)>=len(words):
        raise ValueError('全部词都划掉了，至少要留一句。')
    removed,keep_segments=media.word_cut_ranges(words,ids,duration_ms,base_removed,cues=transcript.get('cues') or [])
    kept=sum(s['end_ms']-s['start_ms'] for s in keep_segments)/1000.0
    return {'words':len(ids),'removed_seconds':round(duration_ms/1000.0-kept,2),
            'keep_seconds':round(kept,2),'segments':len(keep_segments),'cuts':max(0,len(keep_segments)-1),
            'original_seconds':round(duration_ms/1000.0,2),'ok':True,
            'keep':[{'start_ms':s['start_ms'],'end_ms':s['end_ms']} for s in keep_segments],
            'removed':[{'start_ms':s['start_ms'],'end_ms':s['end_ms']} for s in removed]}


def textcut_preview(words,deleted_ids,duration_ms,base_removed=None):
    """预估不抛错：删过头（保留不足 300ms）时回一个可读的提示，而不是弹异常。"""
    try:
        return textcut_estimate(words,deleted_ids,duration_ms,base_removed)
    except ValueError as exc:
        return {'ok':False,'words':len(deleted_ids or []),'message':str(exc),
                'original_seconds':round(duration_ms/1000.0,2)}


def rebuild_job(project,asset,target_s,lufs=-9.5):
    """成片重建：清理保留区间 → 段治理 → 切口预算 → 静音吸附 → 变速 → 渲染 → 双闸。

    参数口径见 `剪辑/剪辑逻辑.md`，这里不自创参数。
    """
    script_id=project['confirmed_version_id']
    clean_plan=latest_clean_plan(asset,script_id)
    if not clean_plan:raise ValueError('这份素材还没有清理结果，请先完成剪掉停顿和口误。')
    def work(job_id):
        stage=lambda s:STORE.patch('job',job_id,{'stage':s})
        stage('测量停顿位置：切点只认波形，不认转写时间戳')
        silences=media.silence_intervals(asset['path'],0.05,-35) if asset['info']['audio'] else []
        stage('段治理、切口预算与变速分配')
        try:
            built=rebuild.build(clean_plan['segments'],silences,target_s)
        except rebuild.Unreachable as exc:
            raise ValueError(str(exc)) from None
        summary=built['summary']
        stage('渲染干声片（第 1 遍，不做响度）')
        dry=DATA/'artifacts'/(job_id+'.dry.mp4')
        render=media.render_rebuild(asset['path'],asset['info'],built['plan'],dry)
        dry_stats=media.silence_stats(dry)
        structure=rebuild.judge_structure(summary,dry_stats)
        stage('响度归一（视频流 copy，画质零损失）')
        final=DATA/'artifacts'/(job_id+'.mp4')
        normal=media.normalize_loudness(dry,final,lufs=lufs)
        loudness=rebuild.judge_loudness(normal['final'])
        gated=bool(structure['ok'] and loudness['ok'])
        saved=STORE.put('cleanup_plan',{'segments':[{'start_ms':s['start_ms'],'end_ms':s['end_ms']} for s in built['plan']],
            'method':'rebuild_from_clean_plan','source_plan_id':clean_plan['id'],'speed_plan':built['plan'],
            'target_seconds':target_s,'asset_id':asset['id'],'script_version_id':script_id,'params':built['params']})
        artifact=STORE.put('artifact',{'project_id':project['id'],'script_version_id':script_id,'asset_id':asset['id'],
            'kind':'final_video','path':str(final),'filename':('成片-' if gated else '未过闸-')+project['title'][:30]+'.mp4',
            'info':media.probe(final),'plan_id':saved['id'],'gated':gated,'mapping':render['plan'],
            'rebuild':{'summary':summary,'dry_stats':dry_stats,
                       'render':{k:render[k] for k in ('segments','cuts','output_seconds','size','fps')},
                       'normalize':normal,'structure_gate':structure,'loudness_gate':loudness,
                       'params':{k:built['params'][k] for k in ('avg_seg_s','speed_max','min_seg_s','snap_window_s','ear_s')}}})
        attach_if_current(project['id'],script_id,{'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None})
        return {'artifact':artifact,'plan':saved,'gated':gated,'summary':summary,
                'structure_gate':structure,'loudness_gate':loudness,'dry_stats':dry_stats,
                'stage':'成片已出，请试听审阅' if gated else '已出片但未过闸，请看闸门报告',
                'note':('响度 %.1f LUFS / LRA %.1f，干声态静音 %.1f%%、最长空隙 %.2fs；%d 段 %d 刀。'
                        % (normal['final']['lufs'],normal['final']['lra'],dry_stats['quiet_ratio']*100,
                           dry_stats['longest_gap_s'],summary['segments'],summary['cuts']))
                      +('' if gated else ' 未过闸：'+'；'.join(structure['fails']+loudness['fails']))}
    return new_job(project['id'],'rebuild',{'asset_id':asset['id'],'clean_plan_id':clean_plan['id'],
                                            'target_s':target_s,'lufs':lufs},work)



# t116：第二大脑整体导出（认知卡 / 碎片 / 会话 / 打卡），JSON＋Markdown。
def brain_export_data(store, data_dir):
    knowledge=[x for x in store.list('knowledge',10000)]
    fragments=[x for x in store.list('growth_fragment',10000)]
    sessions=[]
    for kind in ('brain_session','interview_session'):
        try:sessions += store.list(kind,2000)
        except Exception:pass
    profiles=[x for x in store.list('checkin_profile',200)]
    checkins=[]
    cdir=Path(data_dir)/'checkin'
    if cdir.is_dir():
        for path in sorted(cdir.glob('*.jsonl')):
            for line in path.read_text(encoding='utf-8',errors='replace').splitlines():
                line=line.strip()
                if not line:continue
                try:item=json.loads(line)
                except ValueError:continue
                item['_file']=path.name
                checkins.append(item)
    return {
        'schema':'ip-brain-export-v1','schema_version':1,
        'generated_at':datetime.now().astimezone().isoformat(timespec='seconds'),
        'storage':{'where':'本机 SQLite + 本机 checkin JSONL','cloud':'仅本次模型调用发送提示词；工作台不整库上传、不用于训练'},
        'counts':{'knowledge':len(knowledge),'growth_fragment':len(fragments),'sessions':len(sessions),'checkin_profiles':len(profiles),'checkins':len(checkins)},
        'knowledge':knowledge,'growth_fragments':fragments,'sessions':sessions,
        'checkin_profiles':profiles,'checkins':checkins,
    }

def brain_export_markdown(data):
    out=['# 重启系统整体导出','',f"生成时间：{data['generated_at']}",'',
         '记录只存本机；模型调用只发送本次提示词，不整库上传、不用于训练。','',
         '## 数量',f"- 认知卡：{data['counts']['knowledge']}",f"- 碎片：{data['counts']['growth_fragment']}",
         f"- 会话：{data['counts']['sessions']}",f"- 打卡档案：{data['counts']['checkin_profiles']}",f"- 打卡记录：{data['counts']['checkins']}",'',
         '## 认知卡','']
    for x in data['knowledge']:
        out += [f"### {x.get('title') or '（无标题）'}",f"- 类别：{x.get('category') or ''}",f"- 主张：{x.get('claim') or ''}",f"- 边界：{x.get('boundary') or ''}",f"- 生命周期：{x.get('lifecycle') or 'active'}",f"- 更新时间：{x.get('updated_at') or x.get('created_at') or ''}",'']
    out += ['## 碎片','']
    for x in data['growth_fragments']:
        out += [f"### {x.get('title') or x.get('text','')[:40] or '碎片'}",f"- {x.get('text') or x.get('content') or ''}",f"- 时间：{x.get('created_at') or ''}",'']
    out += ['## 会话','']
    for x in data['sessions']:
        out += [f"### {x.get('title') or '会话'}（{x.get('kind') or ''}）",f"- 时间：{x.get('created_at') or ''}",'```json',json.dumps(x,ensure_ascii=False,indent=2),'```','']
    out += ['## 打卡档案','']
    for x in data['checkin_profiles']:
        out += [f"### {x.get('name') or x.get('id')}",'```json',json.dumps(x,ensure_ascii=False,indent=2),'```','']
    out += ['## 打卡记录','']
    for x in data['checkins']:
        out += ['```json',json.dumps(x,ensure_ascii=False,indent=2),'```','']
    return '\n'.join(out)


def brain_import_payload(store, payload, replace=False, tick=None):
    """合并导入第二大脑导出；按 id／标题去重，默认不覆盖。返回计数。"""
    if not isinstance(payload, dict):
        raise ValueError('导入文件不是有效的 JSON 对象。')
    schema=str(payload.get('schema') or '')
    if schema and not schema.startswith('ip-brain-export'):
        raise ValueError('不是重启系统导出文件（schema=%s）。' % schema[:60])
    knowledge=payload.get('knowledge')
    if not isinstance(knowledge, list):
        raise ValueError('导出文件缺少 knowledge 数组。')
    imported=skipped=replaced=0
    by_kind={}
    # t176：作业化进度——先算总条数，再逐条上报 done/total（1 秒级可见，不是假进度）。
    _total_items=0
    for _key in ('knowledge','growth_fragments','sessions'):
        _rows=payload.get(_key)
        if isinstance(_rows,list): _total_items+=len(_rows)
    _done_items=0
    if tick: tick(0,_total_items)
    for kind,keyname in (('knowledge','knowledge'),('growth_fragment','growth_fragments'),('brain_session','sessions'),('interview_session','sessions')):
        rows=payload.get(keyname)
        if not isinstance(rows,list): rows=[]
        if kind!='knowledge' and keyname=='sessions':
            # sessions 导出里可能混有 brain_session/interview_session；按 kind 字段分流
            rows=[x for x in rows if isinstance(x,dict) and (x.get('kind') or 'brain_session')==kind]
        existing=store.list(kind,10000)
        ids={x.get('id') for x in existing if x.get('id')}
        titles={str(x.get('title') or '').strip().lower() for x in existing if x.get('title')}
        count=0
        for item in rows:
            _done_items+=1
            if tick and (_done_items % 20 == 0 or _done_items == _total_items):
                tick(_done_items,_total_items)
            if not isinstance(item,dict):
                skipped+=1;continue
            title=str(item.get('title') or '').strip()
            ident=str(item.get('id') or '').strip()
            dup=bool((ident and ident in ids) or (title and title.lower() in titles))
            if dup and not replace:
                skipped+=1;continue
            if dup and replace:
                if ident:
                    store.put(kind,item,ident); replaced+=1;count+=1;ids.add(ident)
                    if title:titles.add(title.lower())
                    continue
                skipped+=1;continue
            if not title and not ident:
                skipped+=1;continue
            saved=store.put(kind,item,ident or None)
            if saved and saved.get('id'):ids.add(saved['id'])
            if title:titles.add(title.lower())
            imported+=1;count+=1
        by_kind[kind]=count
    try:
        content_index.rebuild(store)
    except Exception as exc:
        applog.warning('brain.import.index','索引重建失败：%s',exc)
    return {'ok':True,'imported':imported,'skipped':skipped,'replaced':replaced,'by_kind':by_kind,
            'schema':schema or 'ip-brain-export-v1','replace':bool(replace)}

def build_version_value():
    try:
        data=json.loads((ROOT/'web'/'version.json').read_text(encoding='utf-8'))
        return str(data.get('build') or data.get('version') or '')
    except Exception:
        return ''

def version_status_value(data_dir):
    build=build_version_value(); path=Path(data_dir)/'last_version'
    try: last=path.read_text(encoding='utf-8').strip()
    except OSError: last=''
    return {'build':build,'last_version':last,'first_run':not bool(last),
            'upgraded':bool(last and build and last!=build)}

def mark_version_seen(data_dir, version=''):
    build=build_version_value(); target=str(version or build)
    path=Path(data_dir)/'last_version'; path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(target+'\n',encoding='utf-8')
    return {'ok':True,'build':build,'last_version':target}

class Handler(BaseHTTPRequestHandler):
    server_version='IPWorkbench/0.4'

    def log_message(self,fmt,*args):
        # Keep logs free of request bodies, query parameters, and provider credentials.
        if args and str(args[0]).startswith('GET /api/jobs'):
            return
        super().log_message(fmt,*args)

    def json(self,data,status=200,headers=(),cache='no-store'):
        raw=json.dumps(data,ensure_ascii=False).encode()
        probe=data
        if isinstance(data,dict):
            def _volatile_strip(obj):
                if isinstance(obj,dict):return {k:_volatile_strip(v) for k,v in obj.items() if k!='checked_at'}
                if isinstance(obj,list):return [_volatile_strip(v) for v in obj]
                return obj
            probe=_volatile_strip(data)
        etag='"'+hashlib.sha1(json.dumps(probe,ensure_ascii=False).encode()).hexdigest()[:16]+'"'
        if self.command=='GET' and self.headers.get('If-None-Match')==etag:
            self.send_response(304);self.send_header('ETag',etag)
            self.send_header('Cache-Control',cache);self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers();return
        encoding=''
        if len(raw)>1024 and 'gzip' in (self.headers.get('Accept-Encoding') or ''):
            raw=gzip.compress(raw,6);encoding='gzip'
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        for name,value in headers:self.send_header(name,value)
        self.send_header('ETag',etag)
        trace_id=getattr(self,'trace_id','')
        if trace_id:self.send_header('X-Trace-Id',trace_id)
        if encoding:self.send_header('Content-Encoding',encoding);self.send_header('Vary','Accept-Encoding')
        self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control',cache)
        self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)

    def body(self,limit=None):
        size=int(self.headers.get('Content-Length',0))
        cap=limit or 2*1024*1024
        if not 0<size<=cap:
            raise ValueError('JSON 请求大小无效（上限 %dMB）。' % (cap//(1024*1024)))
        return json.loads(self.rfile.read(size))

    def _client_is_loopback(self):
        return self.client_address[0] in ('127.0.0.1','::1')

    def _cookie_token(self):
        raw=self.headers.get('Cookie','')
        if not raw:return ''
        try:
            cookie=SimpleCookie();cookie.load(raw)
        except Exception:
            return ''
        item=cookie.get('ip_token')
        return item.value if item else ''

    def _query_token(self):
        return (parse_qs(urlsplit(self.path).query).get('token') or [''])[0]

    def guard(self,write=False):
        host=self.headers.get('Host','').split(':')[0].lower()
        if host not in ('127.0.0.1','localhost','[::1]') and not (LAN_MODE and is_private_host(host)):
            raise PermissionError('本地版仅允许本机访问。')
        if LAN_MODE and not self._client_is_loopback():
            if not LAN_ENABLED:
                raise LanDisabled('手机访问已在设置里关闭。要手机访问，请到电脑上打开「手机访问」开关。')
        # /mp/ 是小程序只读桥接，同样属于对外数据面；只放行换取会话的 /mp/login。
        _gp=self.path.split('?')[0]
        if (LAN_MODE and LAN_ENABLED and (_gp.startswith('/api') or _gp.startswith('/mp/'))
                and _gp not in ('/api/access','/mp/login') and not self._client_is_loopback()):
            token=self.headers.get('X-Workbench-Token') or self._cookie_token() or self._query_token()
            if not LAN_TOKEN or token!=LAN_TOKEN:
                raise TokenRequired('手机访问需要访问码，请输入工作台启动时显示的访问码。')
        origin=self.headers.get('Origin')
        if write and origin:
            if origin not in ('http://'+self.headers.get('Host',''),):
                raise PermissionError('不允许跨站写入。')

    def _fail(self,payload,status,exc=None,code=None):
        # 2026-11-25：报错出口说人话。HTTP 状态与 code 原样不动；error 换成客户能懂的一句话，
        # 原话放 detail（日志/诊断包仍记原话），另带 title/next/retry 给界面画「下一步」。见 customer_error。
        view=customer_error(exc,code) if exc is not None else customer_error_text(payload.get('error'),code or payload.get('code'))
        return self.json({**payload,**view,'error':view['title']},status)

    def dispatch(self,method):
        self.trace_id=fault_report.bind_trace(self.headers.get('X-Trace-Id',''))
        try:
            self.guard(write=method in ('POST','PATCH','PUT','DELETE'))
            self.route(method)
        except auth_service.CloudError as exc:
            # 云端账号错误原样透出状态与业务码（如 409 ACCOUNT_EXISTS），别塌成 400 invalid_input。
            _st=getattr(exc,'status',400) or 400
            if _st>=500:
                # ccE：云端 5xx=基础设施故障，自动上报；4xx 账号态留给用户自己处理。
                _cce_record('llm_cloud_error',str(exc)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='http',error_code='CLOUD_{0}'.format(_st),capability='llm_cloud',
                    actionable='云端服务异常，请稍后重试；持续失败请导出诊断包。')
            self.json({'error':str(exc),'code':getattr(exc,'code','cloud_error') or 'cloud_error'},_st)
        except prompt_store.PromptUnavailable as exc:
            # 2026-11-17 空壳：内置模板已掏空，云端模板没到位就明确告诉用户「请先登录」。
            self._fail({'error':str(exc),'code':getattr(exc,'code','prompt_login_required')},403,exc,'prompt_login_required')
        except wash_service.TooLongError as exc:
            # t201：正文＋素材超过模型单次上限——给显式失败（413 + too_long 结构），
            # 前端落到既有的 wash.blocked 分栏，不再塌成 500「单条 content 太长」。
            self.json(exc.as_dict(),413)
        except (ValueError,json.JSONDecodeError) as exc:
            self._fail({'error':str(exc),'code':'invalid_input'},400,exc,'invalid_input')
        except KeyError as exc:
            # 2026-11-25：KeyError 只取 args[0]（str(exc) 会带一对引号）。带的是专门写的话（如「这次拆题记录已经不在了…」）
            # 就留在 detail 里；error 统一给登记表的人话。
            _msg=str(exc.args[0]) if exc.args and isinstance(exc.args[0],str) else ''
            self._fail({'error':_msg or '记录不存在或不属于当前项目。','code':'not_found'},404,exc,'not_found')
        except (Conflict,providers.ProviderMissing) as exc:
            if isinstance(exc,providers.ProviderMissing):
                # ccE：模型服务未配置=能力缺失，上报攒量（不算 bug，按配置类处理）。
                _cce_record('llm_cloud_error',str(exc)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='http',error_code='PROVIDER_MISSING',capability='llm_cloud',
                    actionable='模型服务未配置：请先完成账号登录或线路设置后重试。')
            self._fail({'error':str(exc),'code':'conflict_or_setup_required'},409,exc,'conflict_or_setup_required')
        except PermissionError as exc:
            code='token_required' if isinstance(exc,TokenRequired) else ('lan_disabled' if isinstance(exc,LanDisabled) else 'forbidden')
            self.json({'error':str(exc),'code':code},401 if code=='token_required' else 403)
        except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
            pass
        except Exception as exc:
            path_only=unquote(urlsplit(self.path).path)
            applog.exception('http.exception',exc,'{0} {1}'.format(method,path_only))
            fault_report.record_exception(exc,kind='http_unhandled',summary='接口处理失败',
                trace_id=getattr(self,'trace_id',''),stage='http',method=method,route=path_only,
                error_code='HTTP_500',capability='local_http',
                actionable='查看诊断日志中的 trace 号后重试；持续失败请导出诊断包。')
            self._fail({'error':str(exc)[:1500],'code':'operation_failed','trace_id':getattr(self,'trace_id','')},500,exc,'operation_failed')

    def do_GET(self):self.dispatch('GET')
    def do_POST(self):self.dispatch('POST')
    def do_PATCH(self):self.dispatch('PATCH')
    def do_PUT(self):self.dispatch('PUT')
    def do_DELETE(self):self.dispatch('DELETE')

    def route(self,method):
        global LAN_ENABLED
        parsed=urlsplit(self.path); path=unquote(parsed.path); query=parse_qs(parsed.query)
        bits=[b for b in path.split('/') if b]
        clone_blocked=clone_guard.blocked(method,path,CLONE_STATE)
        if clone_blocked:return self.json(clone_blocked,403)
        # 2026-11-14 G0：账本按登录账号隔离。每个请求先定用户，计费/导出/问答钩子
        # 不传 user_id 时自动落到当前账号，没登录才回落 local-owner。
        # 2026-11-14 云端账号：账号主数据在 api.jidixh.com，账本按云账号 id 记，
        # 前缀用 cloud- 区分老的本地账号（acct-），换电脑时账号本身仍然可用。
        try:
            _acct=auth_service.current_account(STORE,self,verify=False)
        except Exception:
            _acct=None
        billing_service.set_request_user(('cloud-%s'%_acct['id']) if _acct else '')
        # 云端逐人下发的权益（导出次数/清晰度/云端ASR开关），本请求内按它执行；
        # 云端没下发就是不覆盖，沿用本地配置兜底，不在这里写死额度。
        billing_service.set_entitlements((_acct or {}).get('entitlements') or {})
        # 授权门禁放在账号解析之后：会员/试用状态由云端下发（会员放行、试用期内放行、
        # 试用到期只拦新增生成，查看与导出照旧）。未登录/老账号走本地兜底。
        blocked=trial_gate.blocked(method,path)
        if blocked:
            # 2026-11-25：试用到期的 403 也说人话（error 换人话、原话在 detail，code / trial 结构不动）。
            # 只有「真到期」才说「免费体验已经结束，去开通」；记录缺失 / 校验失败 / 时间回拨同样是只读，
            # 但开通解决不了，照 license 写的具体原因说，不误导人去付费。
            _mode=str(((blocked.get('trial') or {}).get('mode')) or '')
            _kind='trial_tampered' if _mode=='tampered' else str(blocked.get('code') or '')
            _view=customer_error_text(blocked.get('error'),_kind)
            return self.json({**blocked,**_view,'error':_view['title']},403)
        shell_blocked=shell_gate(method,path)
        if shell_blocked:return self.json(shell_blocked,403)
        if auth_service.route(self,method,bits,query,STORE,DATA):return
        # 会员在线开通：下单/查单/取二维码转发到云端（金额与权益只在云端决定）
        if member_pay.route(self,method,bits,query,STORE):return
        if content_index.route(self,method,bits,query,STORE):return
        if growth_service.route(self,method,bits,query,STORE,submit_job=new_job):return
        if graph_view.route(self,method,bits,query,STORE):return
        if checkin.route(self,method,bits,query,STORE,DATA) is not False:return
        # 2026-11-14 打卡二选一：统一用 60 天打卡（/api/checkin，界面唯一入口）。
        # 主理人打卡（mentor_checkin）没有界面、也没有任何数据，不再挂路由；模块文件保留可回退。
        if clip_export.route(self,method,bits,query,STORE,DATA):return
        if compliance_route.route(self,method,bits,query):return
        if billing_route.route(self,method,bits,query):return
        # 2026-11-06：小程序只读桥接。只在 /mp/ 前缀读 body —— body() 每次都消耗
        # rfile，不能对其它 POST 提前调用，否则原接口会读到空请求体。
        if path.startswith('/mp/'):
            _mp_body=self.body() if method in ('POST','PATCH','PUT') else {}
            _mp=mp_bridge.handle(method,path,_mp_body,STORE)
            if _mp is not None:return self.json(_mp[1],_mp[0])
        if method=='GET' and path=='/api/bootstrap':

            return self.json({'profile':creation_service.profile(),
                  'brand_name':branding.name(STORE),'brand_default':branding.DEFAULT_NAME,
                  'capabilities':capabilities(),'trial':license.state(),
                  'billing':_cap(billing_route.summary,{}),
                  'flow':_cap(lambda:flow_service.summary(STORE),{'intro_seen':False,'current':1,'stage':'intro',
                      'completed':False,'track':'','skipped':False,'card':None,'knowledge_ids':[],
                      'answers':{},'answered':0,'questions':[{'id':q['id'],'section':q['section'],'text':q['text']}
                      for q in flow_service.questions()],
                      'need_login':not flow_service.assets_ready(),'sections':flow_service.SECTIONS,'tracks':flow_service.TRACKS}),
                  'knowledge_count':len([x for x in STORE.list('knowledge',10000)
                                         if x.get('scope')!='example' and knowledge_active(x)]),
                  'lan_mode':LAN_MODE,'lan_enabled':LAN_ENABLED,
                  'lan_url':_lan_url(self.server.server_address[1]) if (LAN_MODE and LAN_ENABLED) else '',
                  'lan_token':LAN_TOKEN if (LAN_MODE and LAN_ENABLED) else ''},cache='private, no-cache')
        if method=='GET' and path=='/api/capabilities':
            # 2026-11-16（性能 E-2）：设置页进入时取真实能力。缓存过期就等本次重探最多 2 秒，
            # 避免「显示已配置但其实抓不到」留出长窗口；仍拿不到时用 stale 标记旧值。
            data,stale=capabilities_fresh()
            return self.json({'ok':True,'capabilities':data,'stale':stale})
        if path=='/api/asr/status' and method=='GET':
            return self.json(asr_status())
        if path=='/api/asr/warmup' and method=='POST':
            return self.json(start_asr_warmup(),202)
        if path=='/api/asr/mode' and method=='POST':
            return self.json(set_asr_mode(self.body().get('mode','')))
        if path=='/api/asr/diarize' and method=='POST':
            return self.json(set_asr_diarize(bool(self.body().get('enabled'))))
        if path=='/api/douyin/login' and method=='GET':
            return self.json(douyin_login_state())
        if path=='/api/platform/session' and method=='GET':
            # P0-1 统一会话层：只读判定，返回 {status, account_name, last_checked_at, expires_at, reason}。
            account_id=query.get('account_id',[''])[0]
            account_name=''
            if account_id:
                account=STORE.get(contracts.KIND_PLATFORM_ACCOUNT,account_id)
                account_name=(account or {}).get('display_name') or ''
            return self.json(platform_session.state(query.get('platform',['douyin'])[0],account_name))
        if path=='/api/douyin/login/open' and method=='POST':
            return self.json(open_douyin_login())
        if path=='/api/capture/file' and method=='POST':
            data=self.body();job=capture_file_job(data,self._client_is_loopback())
            if data.get('wait',True) is False:
                return self.json({'ok':True,'job_id':job['id'],'status':job['status'],'poll':'/api/jobs/'+job['id'],
                                  'text':None,'source':None,'segments':[],'warnings':[]},202)
            done=capture_wait(job['id'])
            if done.get('status')!='succeeded':
                if done.get('status') in ('queued','running'):
                    return self.json({'ok':True,'job_id':done['id'],'status':done['status'],'poll':'/api/jobs/'+done['id'],
                                      'text':None,'source':None,'segments':[],'warnings':[]},202)
                # ccE：OCR/ASR 文件转写失败自动上报（含提交流 too large 在 body() 里 413 到不了这）。
                # t207：按 job 真实 mode/扩展名分「OCR 失败」与「ASR 失败」两类，不再合并成 asr_ocr。
                _ck,_cst,_ccap,_ccode,_cact=_cce_capture_spec(done)
                _fid=_cce_record(_ck,str(done.get('error') or '文件转写失败')[:200],
                    trace_id=getattr(self,'trace_id',''),stage=_cst,
                    error_code=_ccode,capability=_ccap,actionable=_cact)
                _body={'ok':False,'error':done.get('error') or '转写没有完成，请重试。','code':'invalid_input'}
                if _fid:_body['fault_id']=_fid
                return self.json(_body,400)
            result=done.get('result') or {}
            return self.json({'ok':True,'job_id':done['id'],**{key:result.get(key) for key in
                              ('text','source','segments','warnings','confidence_note','file','asr','ocr',
                               'work_id','work_title','work_created')}})
        if path=='/api/diagnostics/export' and method=='GET':
            package=diagnostics.build_package(DATA,ROOT)
            applog.info('diagnostics.export','已生成诊断包',filename=package['filename'],
                        bytes=package['bytes'],errors=package['error_count'])
            # format=json：只回元数据（落盘路径等），供界面报「文件在哪」；默认仍是 zip 下载。
            if (query.get('format') or [''])[0]=='json':
                return self.json(package)
            return self.send_file(Path(package['path']),download=True,filename=package['filename'])
        # t184（修 t176 R1）：作业查询**唯一实现**。返回「旧全量契约 + 新进度字段」的超集：
        #   · 旧调用方（链接处理/转写）读 j.result / j.kind / j.project_id / j.id → 原样保留；
        #   · result.need_login → ok:false + need_login:true + reason=login_required + message（旧 :4118 语义）；
        #   · 404 语义、重启后残留 running→failed（job_public 负责），一条路径统一。
        # 2026-11-25 资料外置：设置页「你的资料保存在」。「打开文件夹」只认服务端自己的资料目录，
        # 不接受前端传路径；仅限本机回环。
        _home=path.split('?',1)[0].rstrip('/')
        if _home=='/api/data-home' and method=='GET':
            return self.json(data_home_status())
        if _home=='/api/data-home/reveal' and method=='POST':
            if not self._client_is_loopback():
                return self.json({'ok':False,'error':'只能在电脑本机上打开文件夹。'},403)
            target=Path(os.getenv('IP_DATA_DIR') or (ROOT/'.runtime'))
            if not target.is_dir():
                return self.json({'ok':False,'error':'资料文件夹不在原来的位置：%s' % target},404)
            try:
                _ok,_why=_reveal_in_folder(target)
                if not _ok:raise RuntimeError(_why)
            except Exception:
                return self.json({'ok':False,'error':'没能打开文件夹，可以复制完整路径手动打开。'},500)
            return self.json({'ok':True})
        # 2026-11-25（需求单 G.2.1 / G.3）：导出完成弹窗「打开所在文件夹」。
        # 只认作业号；路径取服务端作业记录，不接受前端传路径。仅限本机回环：手机/局域网访问不弹资源管理器。
        if path.startswith('/api/jobs/') and path.endswith('/reveal') and method=='POST':
            if not self._client_is_loopback():
                return self.json({'ok':False,'error':'只能在电脑本机上打开文件夹。'},403)
            job_id=path[len('/api/jobs/'):-len('/reveal')].strip('/')
            job=STORE.get('job',job_id) if job_id else None
            result=job.get('result') if isinstance(job,dict) and isinstance(job.get('result'),dict) else {}
            target=Path(str(result.get('result_path') or result.get('path') or '')) if result else None
            if not job or job.get('status')!='succeeded' or not target or not target.is_file():
                return self.json({'ok':False,'error':'这个文件已经不在原来的位置（可能被移动、改名或删除）。可以复制完整路径，到资源管理器地址栏粘贴后找找。'},404)
            try:
                _ok,_why=_reveal_in_folder(target)
                if not _ok:raise RuntimeError(_why)
            except Exception:
                return self.json({'ok':False,'error':'没能打开文件夹，可以复制完整路径手动打开。'},500)
            return self.json({'ok':True})
        if path.startswith('/api/jobs/') and method=='GET':
            job_id=path.split('/',3)[3].strip('/')
            job=STORE.get('job',job_id) if job_id else None
            if not job:
                return self.json({'ok':False,'job_id':job_id,'status':'failed',
                                  'message':'这条任务记录已经不在了（可能已被清理），请重新发起。',
                                  'error':'这条任务记录已经不在了（可能已被清理），请重新发起。'},404)
            # 增量轮询：since=上次 revision，没变化只回轻量确认，不再每次重算完整视图（轮询全量重算是卡顿根因之一）。
            since=query.get('since',[''])[0]
            if since and since==str(job.get('revision')):
                return self.json({'ok':True,'job_id':job_id,'id':job_id,'unchanged':True,
                                  'status':job.get('status'),'revision':job.get('revision')})
            view=dict(job)                       # 旧契约：完整 job 对象（id/kind/project_id/inputs/status/result/error/时间戳…）
            view.update({k:v for k,v in job_public(job).items() if k!='job_id'})   # 新进度字段
            view['id']=job.get('id')             # 旧字段 id 必须保留（job_public 里叫 job_id）
            view['job_id']=job.get('id')
            result=job.get('result') if isinstance(job.get('result'),dict) else {}
            if result.get('need_login'):
                view.update({'ok':False,'need_login':True,'reason':'login_required',
                             'message':result.get('message') or '这条要登录抖音才能抓。'})
            else:
                view.setdefault('ok', True)
            return self.json(view)
        if path=='/api/backup/status' and method=='GET':
            return self.json(backup_restore.status(DATA))
        if path=='/api/backup/export' and method=='GET':
            result=backup_restore.export_full(DATA,ROOT)
            applog.info('backup.export','已导出全量包',filename=result['filename'],
                        bytes=result['bytes'],files=result['files'],elapsed_s=result['elapsed_s'])
            return self.send_file(Path(result['path']),download=True,filename=result['filename'])
        if path=='/api/backup/export' and method=='POST':
            data=self.body()
            kind=str(data.get('kind') or 'database')
            if kind not in ('full','database'):
                raise ValueError('未知的备份类型：'+kind)
            legacy_sync=str(data.get('sync') or (query.get('sync') or [''])[0]).lower() in ('1','true','yes')
            if not legacy_sync:
                # t176：作业化——秒回 job_id，前端轮询 /api/jobs/{id} 看 done/total。
                def _export_work(job_id, _kind=kind):
                    STORE.patch('job',job_id,{'stage':'准备导出','done':0,'total':0})
                    if _kind=='full':
                        result=backup_restore.export_full(DATA,ROOT,tick=_job_tick(job_id))
                        label='已导出全量包（含素材）'
                    else:
                        result=backup_restore.export_database(DATA,ROOT)
                        label='已导出数据库包'
                    files=int(result.get('files') or 0)
                    STORE.patch('job',job_id,{'done':files,'total':files,'stage':'导出完成'})
                    applog.info('backup.export',label,filename=result['filename'],
                                bytes=result['bytes'],files=result['files'],elapsed_s=result['elapsed_s'])
                    return {'result_path':str(result.get('path') or ''),'filename':result.get('filename'),
                            'bytes':result.get('bytes'),'files':files,'package_type':_kind,
                            'stage':'导出完成'}
                job=new_job(None,'backup_export',{'kind':kind},_export_work)
                return self.json({'ok':True,'job_id':job['id'],'status':job['status'],
                                  'poll':'/api/jobs/'+job['id']},202)
            if kind=='full':
                result=backup_restore.export_full(DATA,ROOT)
                label='已导出全量包（含素材）'
            elif kind=='database':
                result=backup_restore.export_database(DATA,ROOT)
                label='已导出数据库包'
            else:
                raise ValueError('未知的备份类型：'+kind)
            applog.info('backup.export',label,filename=result['filename'],
                        bytes=result['bytes'],files=result['files'],elapsed_s=result['elapsed_s'])
            return self.json(result,201)
        if path=='/api/backup/restore' and method=='POST':
            data=self.body()
            if data.get('confirm') is not True:
                raise ValueError('恢复会写入一个新目录；请传 confirm:true 明确确认。')
            zip_path=Path(str(data.get('zip_path') or ''))
            target=Path(str(data.get('target_dir') or ''))
            if not zip_path.is_file():
                raise ValueError('找不到全量备份包，请确认 zip_path 指向的 .zip 文件存在。')
            result=backup_restore.restore_full(zip_path,target,ROOT,current_data=DATA)
            applog.info('backup.restore','已恢复全量包',target=result['restored_to'],
                        files=result['files'],tables=result['tables']['total'],rewrites=result['path_rewrites'])
            return self.json(result,201)
        if path=='/api/backup/restore-in-place' and method=='POST':
            data=self.body()
            if data.get('confirm') is not True:
                raise ValueError('恢复会覆盖当前数据；请传 confirm:true 明确确认。')
            if not RESTORE_LOCK.acquire(blocking=False):
                return self.json({'error':'已有一个恢复在进行，请等它结束再试。','code':'restore_busy'},409)
            try:
                # 有任务在写库时恢复，结果会落到即将被替换的旧库里，等于白跑。
                running=[j for j in STORE.list('job',10000) if j.get('status') in ('queued','running')]
                if running:
                    return self.json({'error':'还有 %d 个任务在跑（含排队），等它们结束再恢复，避免结果写进旧库。'%len(running),
                                      'code':'jobs_running'},409)
                name=str(data.get('filename') or '')
                if not name:
                    raise ValueError('请选择要恢复的备份文件。')
                exports=(Path(DATA)/'exports').resolve()
                zip_path=(exports/name).resolve()
                if zip_path.parent!=exports:
                    raise ValueError('只能恢复 exports 目录里的备份包。')
                if not zip_path.is_file():
                    raise ValueError('找不到备份文件：'+name)
            finally:
                RESTORE_LOCK.release()
            # t176：校验通过后作业化执行（锁在作业线程里再拿，避免阻塞请求线程、也让前端能看到 done/total）。
            def _restore_in_place_work(job_id, _zip=zip_path, _name=name):
                if not RESTORE_LOCK.acquire(blocking=False):
                    raise ValueError('已有一个恢复在进行，请等它结束再试。')
                try:
                    STORE.patch('job',job_id,{'stage':'校验备份包'})
                    result=backup_restore.restore_in_place(_zip,DATA,ROOT,tick=_job_tick(job_id))
                finally:
                    RESTORE_LOCK.release()
                files=int(result.get('files') or 0)
                STORE.patch('job',job_id,{'done':files,'total':files,'stage':'恢复完成'})
                applog.info('backup.restore_in_place',
                            '已就地恢复'+('全量包（含素材）' if result.get('package_type')=='full' else '数据库包'),
                            source=_name,files=result['files'],tables=result['tables']['total'],
                            pre_restore_backup=result['pre_restore_backup'])
                return {'result_path':str(_zip),'filename':_name,'files':files,
                        'package_type':result.get('package_type'),'restored':True,
                        'pre_restore_backup':result.get('pre_restore_backup'),'stage':'恢复完成'}
            job=new_job(None,'backup_restore_in_place',{'filename':name},_restore_in_place_work)
            return self.json({'ok':True,'job_id':job['id'],'status':job['status'],
                              'poll':'/api/jobs/'+job['id']},202)
        if path=='/api/creation/catalog' and method=='GET':
            return self.json(creation_service.CATALOG)
        if path=='/api/creation/topics' and method=='POST':
            return self.json(creation_service.generate_topics(self.body()),202)
        if path=='/api/creation/pain-points' and method=='GET':
            return self.json({'items':STORE.list('pain_point',50)})
        if len(bits)==5 and bits[:3]==['api','creation','pain-points'] and bits[4]=='adopt' and method=='POST':
            return self.json(creation_service.adopt_pain(bits[3]))
        if path=='/api/creation/history' and method=='GET':
            return self.json(creation_service.history(query))
        if path=='/api/flow' and method=='GET':
            return self.json(flow_service.summary(STORE))
        if path=='/api/flow' and method=='POST':
            return self.json(flow_service.save(STORE,self.body()))
        if path=='/api/flow/finalize' and method=='POST':
            return self.json(flow_service.finalize(STORE))
        if path=='/api/flow/transcribe' and method=='POST':
            # ccE：转写失败自动上报（file too large 由 body() 自己 413，到不了这里）。
            try:
                return self.json(flow_transcribe(self.body(12*1024*1024)))
            except ValueError as _e:
                _fid=_cce_record('asr_failed',str(_e)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='flow_transcribe',error_code='ASR_FAILED',capability='asr',
                    actionable='语音转写失败：请重试或换音频文件；持续失败请导出诊断包。')
                _body={'error':str(_e),'code':'invalid_input'}
                if _fid:_body['fault_id']=_fid
                return self.json(_body,400)
        if path=='/api/intake/history' and method=='POST':
            # t207（队长）：转写/摘录入库。存主库随备份/升级走；成功才 201；空文本 400。
            payload,status=intake_history.create(STORE,self.body(4*1024*1024))
            return self.json(payload,status)
        if path=='/api/intake/history' and method=='GET':
            return self.json(intake_history.list_items(STORE,(query.get('limit') or ['20'])[0],(query.get('offset') or ['0'])[0]))
        if len(bits)==4 and bits[:3]==['api','intake','history'] and method=='GET':
            payload,status=intake_history.get(STORE,bits[3])
            return self.json(payload,status)
        if len(bits)==4 and bits[:3]==['api','intake','history'] and method=='DELETE':
            payload,status=intake_history.delete(STORE,bits[3])
            return self.json(payload,status)
        if path=='/api/faults/report' and method=='POST':
            # t198（ccE 顺手接线）：客户端故障只入本地脱敏队列，不打印、不阻塞主流程。
            result=fault_report.record_from_request(self.body(),trace_id=getattr(self,'trace_id',''))
            return self.json(result,200 if result.get('ok') else 400)
        if path=='/api/wash/link' and method=='POST':
            # ccE：链接抓取失败自动上报。fetch_link 失败不抛异常（wash_service.py:780），
            # 返回 ok=False 信封：_kind∈{blocked,gone,http,timeout,network,size,type,empty}
            # =真实抓取失败（记故障）；kind=video/list_page=正常分流、无 _kind=输入格式问题（不记）。
            _r=None
            _url=''   # body() 只读一次：rfile 每次从 socket 读，失败路径二次调用会阻塞到客户端断开（W1）
            try:
                _url=str(self.body().get('url') or '')
                _r=wash_service.fetch_link(_url)
            except ValueError as _e:
                _fid=_cce_record('url_fetch_failed',str(_e)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='wash_link',error_code='FETCH_FAILED',capability='fetch_link',
                    url=_url[:200],
                    actionable='链接抓取失败：请检查链接后重试；持续失败请导出诊断包。')
                _body={'error':str(_e),'code':'invalid_input'}
                if _fid:_body['fault_id']=_fid
                return self.json(_body,400)
            except Exception as _e:
                _fid=_cce_record('url_fetch_failed','抓取异常：{0}'.format(_e)[:180],trace_id=getattr(self,'trace_id',''),
                    stage='wash_link',error_code='FETCH_ERROR',capability='fetch_link',
                    url=_url[:200],
                    actionable='链接抓取异常：请重试；持续失败请导出诊断包。')
                _body={'error':str(_e)[:1500],'code':'operation_failed','trace_id':getattr(self,'trace_id','')}
                if _fid:_body['fault_id']=_fid
                return self.json(_body,500)
            if isinstance(_r,dict) and _r.get('ok') is False and (_r.get('fetch_kind') or _r.get('_kind')):
                _fk=str(_r.get('fetch_kind') or _r.get('_kind'))
                _fid=_cce_record('url_fetch_failed',str(_r.get('reason') or _fk)[:200],
                    trace_id=getattr(self,'trace_id',''),stage='wash_link',
                    error_code='FETCH_'+_fk.upper(),capability='fetch_link',
                    url=_url[:200],
                    actionable='链接抓取失败：请检查链接后重试，或直接粘贴原文；持续失败请导出诊断包。')
                if _fid:
                    _r=dict(_r);_r['fault_id']=_fid
            return self.json(_r)
        if path=='/api/wash/classify' and method=='POST':
            # t196：短链先解析落地地址再分类（作品/图文/账号主页/直播/文章/网页），
            # 前端据此把链接送到正确的输入框，不凭域名猜。
            # ccE：短链解析失败按契约 §2 记 url_fetch_failed（stage=classify_link, capability=fetch_link）；
            # browser_parse_failed 留给「视频页真打开了但解析不出」的 job 步骤（见 _cce_job_fail_spec）。
            try:
                return self.json(wash_service.classify_link(str(self.body().get('url') or '')))
            except ValueError as _e:
                _fid=_cce_record('url_fetch_failed',str(_e)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='classify_link',error_code='CLASSIFY_FAILED',capability='fetch_link',
                    actionable='链接解析失败：请重试或直接粘贴正文；持续失败请导出诊断包。')
                _body={'error':str(_e),'code':'invalid_input'}
                if _fid:_body['fault_id']=_fid
                return self.json(_body,400)
            except Exception as _e:
                _fid=_cce_record('url_fetch_failed',str(_e)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='classify_link',error_code='CLASSIFY_FAILED',capability='fetch_link',
                    actionable='链接解析失败：请重试或直接粘贴正文；持续失败请导出诊断包。')
                _body={'error':str(_e)[:1500],'code':'operation_failed','trace_id':getattr(self,'trace_id','')}
                if _fid:_body['fault_id']=_fid
                return self.json(_body,500)
        if path=='/api/wash/video/start' and method=='POST':
            url=str(self.body().get('url') or '')
            _job=video_link.start(STORE,DATA,url,with_analysis=False)
            # ccE：把提交请求的 trace 烙进 job（dict 按引用存储，后续 job_status 可带上原始 trace）。
            _job['trace_id']=getattr(self,'trace_id','')
            return self.json(_job,202)
        if path=='/api/wash/video/status' and method=='GET':
            _r=video_link.job_status((query.get('job') or [''])[0])
            _fid=_cce_job_fail(_r)   # 先按原话记故障（诊断包要原话），再换成给客户看的
            if _fid:_r['fault_id']=_fid
            _res=_r.get('result') if isinstance(_r.get('result'),dict) else {}
            _tr=_res.get('transcript') if isinstance(_res.get('transcript'),dict) else {}
            _ev=customer_error_text(_r.get('error') or _tr.get('reason') or '')
            # 2026-11-25：失败原话换人话放 error，原话放 error_detail；error_view 给界面画「下一步」。
            # _r 是内存里那条任务本身，只在返回的副本上换，库里的原话不动。
            _out={**_r,'stage_text':customer_stage(_r.get('stage')),'error_view':_ev}
            if _r.get('error'):_out.update({'error':_ev['title'],'error_detail':_ev['detail']})
            return self.json(_out)
        if path=='/api/video/anatomy' and method=='POST':
            _job=video_link.start(STORE,DATA,str(self.body().get('url') or ''))
            _job['trace_id']=getattr(self,'trace_id','')
            return self.json(_job,202)
        if path=='/api/video/anatomy/status' and method=='GET':
            _r=video_link.job_status((query.get('job') or [''])[0])
            _fid=_cce_job_fail(_r)
            if _fid:_r['fault_id']=_fid
            return self.json(_r)
        if path=='/api/wash/stage1' and method=='POST':
            # t207：模板/模型这类生成失败按契约归 llm_cloud_error（输入类 ValueError 与超长不记）。
            try:
                return self.json(wash_service.stage1(STORE,self.body()),201)
            except wash_service.TooLongError:
                raise
            except ValueError as _ve:
                # W2：long_text 会把云端网络故障包成 ValueError（「…（同一次内容重试一次仍失败：连不上云端服务…）」），
                # 这一类要记账；纯输入校验失败（读不到字数/字段缺项）仍不记，避免噪音。
                if _cce_cloud_like(_ve):
                    _cce_record('llm_cloud_error',str(_ve)[:200],trace_id=getattr(self,'trace_id',''),
                        stage='stage1',error_code='LLM_STAGE1_FAILED',capability='llm_call',
                        actionable='模型这一步没跑通：稍后重试；持续失败请导出诊断包。')
                raise
            except Exception as _e:
                _cce_record('llm_cloud_error',str(_e)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='stage1',error_code='LLM_STAGE1_FAILED',capability='llm_call',
                    actionable='模型这一步没跑通：稍后重试；持续失败请导出诊断包。')
                raise
        if path=='/api/wash/stage2' and method=='POST':
            try:
                return self.json(wash_service.stage2(STORE,self.body()),201)
            except wash_service.TooLongError:
                raise
            except ValueError as _ve:
                if _cce_cloud_like(_ve):
                    _cce_record('llm_cloud_error',str(_ve)[:200],trace_id=getattr(self,'trace_id',''),
                        stage='stage2',error_code='LLM_STAGE2_FAILED',capability='llm_call',
                        actionable='模型这一步没跑通：稍后重试；持续失败请导出诊断包。')
                raise
            except Exception as _e:
                _cce_record('llm_cloud_error',str(_e)[:200],trace_id=getattr(self,'trace_id',''),
                    stage='stage2',error_code='LLM_STAGE2_FAILED',capability='llm_call',
                    actionable='模型这一步没跑通：稍后重试；持续失败请导出诊断包。')
                raise
        if path=='/api/wash/history' and method=='GET':
            return self.json(wash_service.history(STORE))
        if len(bits)==4 and bits[:3]==['api','wash','plan'] and method=='GET':
            return self.json(wash_service.get_plan(STORE,bits[3]))
        if path=='/api/profile' and method=='PATCH':
            return self.json(creation_service.update_profile(self.body()))
        if path=='/api/profile/style-card' and method=='GET':
            card=(STORE.get('profile','owner') or {}).get('style_card')
            return self.json({'ok':True,'card':card if isinstance(card,dict) else None,
                              'prompt_block':style_profile.to_prompt_block(card) if isinstance(card,dict) else '',
                              'works_count':len([x for x in STORE.list('bench_work',500) if (x.get('title') or x.get('text') or '').strip()])})
        if path=='/api/profile/style-card' and method=='POST':
            data=self.body()
            result=style_profile.generate_and_write(STORE,use_llm=bool(data.get('use_llm',True)))
            if result.get('ok'):
                applog.info('style_card.generate','已生成账号风格卡',
                            works=(result.get('style_card',{}).get('source',{}) or {}).get('text_work_count'))
            return self.json(result)
        # 2026-11-17 t90：口癖勾选闭环（候选/已确认/保存）；只允许保存当前候选或已确认过的词，防任意写入。
        if path=='/api/style/catchphrases' and method=='GET':
            candidates=style_profile.catchphrase_candidates(STORE)
            confirmed=style_profile.confirmed_catchphrases(STORE)
            confirmed_set=set(confirmed)
            items=[{'phrase':c['phrase'],'count':c['count'],
                    'confirmed':c['phrase'] in confirmed_set} for c in candidates]
            seen={x['phrase'] for x in items}
            for phrase in confirmed:
                if phrase not in seen:
                    items.append({'phrase':phrase,'count':0,'confirmed':True,'confirmed_only':True})
            profile=STORE.get('profile','owner') or {}
            return self.json({'ok':True,'items':items,'confirmed':confirmed,
                              'confirmed_at':profile.get('voice_catchphrases_confirmed_at') or '',
                              'corpus':style_profile.owner_corpus_stats(STORE)})
        if path=='/api/style/catchphrases' and method=='POST':
            data=self.body(); phrases=data.get('phrases')
            if not isinstance(phrases,list):
                raise ValueError('phrases 必须是数组。')
            allowed={c['phrase'] for c in style_profile.catchphrase_candidates(STORE,top=200)}
            allowed.update(style_profile.confirmed_catchphrases(STORE))
            clean=[]
            for raw in phrases[:20]:
                phrase=str(raw or '').strip()
                if not phrase: continue
                if '\n' in phrase or len(phrase)>12:
                    raise ValueError('单个口癖不超过 12 字。')
                if phrase not in allowed:
                    raise ValueError('这个候选不在当前口癖列表里：'+phrase[:12])
                if phrase not in clean: clean.append(phrase)
            style_profile.save_catchphrases(STORE, clean)
            confirmed=style_profile.confirmed_catchphrases(STORE)
            return self.json({'ok':True,'confirmed':confirmed,
                              'voice_block':style_profile.voice_sample_block(STORE)})
        if path=='/api/branding' and method=='GET':
            return self.json({'name':branding.name(STORE),'default_name':branding.DEFAULT_NAME,
                              'account':branding.account(STORE)})
        if path=='/api/avatar' and method=='GET':
            return self.json(avatar_service.status(STORE))
        if path=='/api/avatar' and method=='POST':
            return self.json(avatar_service.save(STORE,self.body()))
        if path=='/api/avatar/image' and method=='GET':
            avatar=avatar_service.path(STORE)
            if not avatar:
                return self.json({'error':'形象还没上传。'},404)
            return self.send_file(avatar)
        if path=='/api/avatar/frames' and method=='POST':
            data=self.body()
            ref=None
            if data.get('artifact_id'):
                ref=require('artifact',str(data['artifact_id']))
                if ref.get('kind') not in ('clean_video','packaged_video'):
                    raise ValueError('只能从成片或素材视频里取帧。')
            elif data.get('asset_id'):
                ref=require('asset',str(data['asset_id']))
            else:
                raise ValueError('缺少视频记录：先有当前定稿的成片或上传素材。')
            video=Path(str(ref.get('path') or ''))
            if not video.is_file():
                raise ValueError('视频文件不在了，无法取帧。')
            return self.json(avatar_service.frames(str(video)))
        if path=='/api/avatar/frame' and method=='GET':
            # ftoken 是取帧令牌；token 已被手机访问码占用，旧参数名只在回环本机兼容。
            frame_token=(query.get('ftoken') or [''])[0]
            if not frame_token and self._client_is_loopback():
                frame_token=(query.get('token') or [''])[0]
            index=(query.get('n') or ['0'])[0]
            return self.send_file(avatar_service.frame_file(frame_token,index))
        if path=='/api/avatar/generate' and method=='POST':
            return self.json(avatar_service.generate(STORE,self.body()))
        if path=='/api/branding' and method=='PATCH':
            data=self.body()
            if 'name' not in data:
                raise ValueError('缺少名字字段。')
            return self.json({'name':branding.set_name(STORE,data['name']),
                              'default_name':branding.DEFAULT_NAME,'account':branding.account(STORE)})
        if path=='/api/version/status' and method=='GET':
            return self.json(version_status_value(DATA))
        if path=='/api/version/seen' and method=='POST':
            data=self.body() if int(self.headers.get('Content-Length',0) or 0) else {}
            return self.json(mark_version_seen(DATA,data.get('version') or ''))
        if path=='/api/brain/import' and method=='POST':
            size=int(self.headers.get('Content-Length',0) or 0)
            if not 0<size<=50*1024*1024:
                raise ValueError('导入文件大小无效（上限 50MB）。')
            raw=self.rfile.read(size)
            ctype=(self.headers.get('Content-Type') or '').lower()
            if 'zip' in ctype or raw[:2]==b'PK':
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    name=next((n for n in z.namelist() if n.lower().endswith('.json')),None)
                    if not name:raise ValueError('ZIP 里没有 brain-export.json。')
                    raw=z.read(name)
            payload=json.loads(raw.decode('utf-8-sig'))
            replace=(query.get('replace') or ['0'])[0].lower() in ('1','true','yes')
            legacy_sync=(query.get('sync') or [''])[0].lower() in ('1','true','yes')
            if legacy_sync:
                return self.json(brain_import_payload(STORE,payload,replace),201)
            # t184（修 R3）：作业身份＝内容摘要。不同文件 / 同名不同内容 → 不同 inputs → 不会被合并漏导入；
            # 同内容重复提交摘要相同 → 仍按 (project_id,kind,inputs) 合并去重。
            digest=hashlib.sha256(raw).hexdigest()
            # t176：作业化——请求线程只解析校验，写库在后台线程逐条上报 done/total。
            def _import_work(job_id,_payload=payload,_replace=replace):
                STORE.patch('job',job_id,{'stage':'导入知识库'})
                counts=brain_import_payload(STORE,_payload,_replace,tick=_job_tick(job_id))
                total=int(counts.get('total') or (int(counts.get('imported') or 0)+int(counts.get('skipped') or 0)+int(counts.get('replaced') or 0)))
                STORE.patch('job',job_id,{'done':total,'total':total or 0,'stage':'导入完成'})
                counts=dict(counts); counts['stage']='导入完成'; counts['total']=total
                return counts
            job=new_job(None,'brain_import',{'replace':bool(replace),'digest':digest,'bytes':len(raw)},_import_work)
            return self.json({'ok':True,'job_id':job['id'],'status':job['status'],
                              'poll':'/api/jobs/'+job['id']},202)
        if path=='/api/brain/export' and method=='POST':
            # t176：作业化——落盘到 data_dir/exports（大包不进内存、不直接回传），响应带完整路径。
            data=self.body() if int(self.headers.get('Content-Length',0) or 0) else {}
            fmt=str(data.get('format') or (query.get('format') or ['zip'])[0]).lower()
            def _brain_export_work(job_id,_fmt=fmt):
                STORE.patch('job',job_id,{'stage':'收集知识库数据'})
                payload=brain_export_data(STORE,DATA)
                counts=payload.get('counts') or {}
                total=sum(int(v or 0) for v in counts.values())
                STORE.patch('job',job_id,{'done':0,'total':total,'stage':'写导出文件'})
                body=json.dumps(payload,ensure_ascii=False,indent=2)
                exports=Path(DATA)/'exports';exports.mkdir(parents=True,exist_ok=True)
                stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
                if _fmt=='json':
                    target=exports/('brain-export-'+stamp+'.json');target.write_text(body,encoding='utf-8')
                    filename='重启系统-整体导出-'+stamp+'.json'
                elif _fmt in ('md','markdown'):
                    target=exports/('brain-export-'+stamp+'.md');target.write_text(brain_export_markdown(payload),encoding='utf-8')
                    filename='重启系统-整体导出-'+stamp+'.md'
                else:
                    target=exports/('brain-export-'+stamp+'.zip')
                    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
                        z.writestr('brain-export.json',body)
                        z.writestr('brain-export.md',brain_export_markdown(payload))
                    filename='重启系统-整体导出-'+stamp+'.zip'
                STORE.patch('job',job_id,{'done':total,'total':total,'stage':'导出完成'})
                return {'result_path':str(target.resolve()),'filename':filename,'format':_fmt,
                        'counts':counts,'bytes':target.stat().st_size,'stage':'导出完成'}
            job=new_job(None,'brain_export',{'format':fmt},_brain_export_work)
            return self.json({'ok':True,'job_id':job['id'],'status':job['status'],
                              'poll':'/api/jobs/'+job['id']},202)
        if path=='/api/brain/export' and method=='GET':
            fmt=(query.get('format') or ['zip'])[0].lower()
            payload=brain_export_data(STORE,DATA)
            body=json.dumps(payload,ensure_ascii=False,indent=2)
            stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
            tmpdir=DATA/'tmp';tmpdir.mkdir(parents=True,exist_ok=True)
            if fmt=='json':
                target=tmpdir/('brain-export-'+stamp+'.json');target.write_text(body,encoding='utf-8')
                name='重启系统-整体导出-'+stamp+'.json'
            elif fmt in ('md','markdown'):
                target=tmpdir/('brain-export-'+stamp+'.md');target.write_text(brain_export_markdown(payload),encoding='utf-8')
                name='重启系统-整体导出-'+stamp+'.md'
            else:
                target=tmpdir/('brain-export-'+stamp+'.zip')
                with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
                    z.writestr('brain-export.json',body)
                    z.writestr('brain-export.md',brain_export_markdown(payload))
                name='重启系统-整体导出-'+stamp+'.zip'
            try:
                return self.send_file(target,download=True,filename=name)
            finally:
                try:target.unlink()
                except OSError:pass
        if path=='/api/knowledge/import' and method=='POST':
            data=self.body()
            filename=str(data.get('filename','认知导入.md'))[:160]
            text=str(data.get('text') or '')
            legacy_sync=(query.get('sync') or [''])[0].lower() in ('1','true','yes')
            if legacy_sync:
                return self.json(STORE.import_brain(text,filename))
            # t187（B.3 收口，撤销 t184 的不定进度临时口径）：接 db.import_brain 的**事务外**进度回调。
            #   · phase='parse'        → 解析阶段：未知总量时 done/total 先给 None + stage「解析中」，
            #                            识别出条目总数后写真实 done/total（百分比递增）；
            #   · phase='commit_start' → 即将开写库事务（此时仍无事务，可安全写 job）；
            #   · phase='done'         → 事务已提交（或去重命中），写满并给真实条数。
            # 红线：**绝不在 BEGIN 事务内用另一连接写 job** —— 本函数只在上面三个事务外时点写库。
            # t184（修 R3）：inputs 纳入内容摘要（sha256(规范化文本)），同名不同内容不会误合并。
            import hashlib as _hashlib
            digest=_hashlib.sha256(text.encode('utf-8')).hexdigest()
            def _knowledge_import_work(job_id,_text=text,_name=filename):
                state={'stage':'解析中','done':None,'total':None}
                def flush():
                    try:
                        STORE.patch('job',job_id,{'stage':state['stage'],'done':state['done'],'total':state['total']})
                    except Exception:
                        pass      # 进度写失败不打断导入（下次 tick 再写）
                def tick(phase,done=0,total=None):
                    if phase=='parse':
                        state['total']=int(total) if total else None
                        state['done']=int(done)
                        state['stage']=('解析中：已识别 %d / %d 条' % (int(done),int(total))) if total else '解析中'
                    elif phase=='commit_start':
                        n=int(total or done or 0)
                        state.update({'stage':'写入数据库（%d 条）' % n,'done':n,'total':n})
                    elif phase=='done':
                        n=int(total or done or 0)
                        state.update({'stage':'导入完成：%d 条' % n,'done':n,'total':n})
                    flush()
                STORE.patch('job',job_id,{'stage':'解析中','done':None,'total':None})
                result=STORE.import_brain(_text,_name,tick=tick)
                total=result.get('count') or 0
                state.update({'stage':'导入完成：%d 条' % total,'done':total,'total':total})
                flush()
                result=dict(result); result['stage']='导入完成'; result['parsed']=total
                return result
            job=new_job(None,'knowledge_import',{'filename':filename,'digest':digest,'chars':len(text)},_knowledge_import_work)
            return self.json({'ok':True,'job_id':job['id'],'status':job['status'],
                              'poll':'/api/jobs/'+job['id']},202)
        # 2026-11-25（需求单 H）：输入时实时出的知识卡。卡坯零模型；精修才调模型且同一段文字只调一次；
        # 「存下来」走下面的 /api/knowledge（卡片就是一条认知，不另建存储）。
        if path=='/api/knowledge-card/pref' and method in ('GET','POST'):
            if method=='POST':
                STORE.put('pref',{'auto_refine':(self.body() or {}).get('auto_refine') is not False},'knowledge_card')
            saved=STORE.get('pref','knowledge_card')
            return self.json({'auto_refine':(not isinstance(saved,dict)) or saved.get('auto_refine',True) is not False})
        if path=='/api/knowledge-card/draft' and method=='POST':
            return self.json(insight_card.draft(STORE,(self.body() or {}).get('text','')))
        if path=='/api/knowledge-card/refine' and method=='POST':
            text=str((self.body() or {}).get('text') or '')
            return self.json(insight_card.refine(STORE,text,insight_card.draft(STORE,text)))
        if path=='/api/knowledge-card/image' and method=='POST':
            png=insight_card.render_png(self.body() or {},DATA/'artifacts')
            item=STORE.put('artifact',{'kind':'knowledge_card','path':str(png),'filename':png.name,
                'info':{'width':insight_card.CARD_WIDTH}})
            return self.json({'id':item['id'],'url':'/api/artifacts/'+item['id'],'path':str(png)},201)
        if path=='/api/knowledge-card/reveal' and method=='POST':
            # 与作业的「打开所在文件夹」同一规矩：只认素材号，路径取服务端记录，仅限本机回环。
            if not self._client_is_loopback():
                return self.json({'ok':False,'error':'只能在电脑本机上打开文件夹。'},403)
            data=self.body() or {}
            item=STORE.get('artifact',str(data.get('id') or '')) if data.get('id') else None
            target=Path(str(item.get('path') or '')) if isinstance(item,dict) else None
            if not target or not target.is_file():
                return self.json({'ok':False,'error':'这张图片已经不在原来的位置（可能被移动、改名或删除）。可以复制完整路径，到资源管理器地址栏粘贴后找找。'},404)
            try:
                _ok,_why=_reveal_in_folder(target)
                if not _ok:raise RuntimeError(_why)
            except Exception:
                return self.json({'ok':False,'error':'没能打开文件夹，可以复制完整路径手动打开。'},500)
            return self.json({'ok':True})
        # 2026-11-25（分享一期）：口播稿公开分享链接。脱敏闸在客户端、落库与公开读在云端；
        # 未登录/断网/拒发都由下层翻成中文原因，这里原样透传，不伪造成功。
        if path=='/api/share/publish' and method=='POST':
            data=self.body() or {}
            try:
                return self.json(share_service.publish({'kind':'script','title':data.get('title'),
                    'body':data.get('body'),'author':data.get('author'),'ref':data.get('ref')}),201)
            except cloud_gateway.ProxyError as exc:
                return self.json({'ok':False,'reason':str(exc),'code':exc.code},exc.status or 502)
        if path=='/api/share/revoke' and method=='POST':
            try:
                return self.json(share_service.revoke(str((self.body() or {}).get('token') or '')))
            except cloud_gateway.ProxyError as exc:
                return self.json({'ok':False,'reason':str(exc),'code':exc.code},exc.status or 502)
        if path=='/api/share/mine' and method=='GET':
            try:
                return self.json(share_service.mine())
            except cloud_gateway.ProxyError as exc:
                return self.json({'ok':False,'reason':str(exc),'code':exc.code},exc.status or 502)
        if path=='/api/knowledge' and method=='POST':
            data=self.body();title=str(data.get('title','')).strip()[:160];claim=str(data.get('claim','')).strip()[:12000]
            if not title or not claim:raise ValueError('请填写认知标题和核心主张。')
            # 2026-11-16（异常态）：正文不再静默用核心主张顶替。
            # 纯空白直接拒绝；空正文只有请求方明确 body_origin=claim 时才带入，并留下来源标记。
            body=str(data.get('body') or '')[:40000]
            if body and not body.strip():
                raise ValueError('正文只有空白字符，不能保存。请补充正文，或清空正文后改用核心主张填充。')
            body_origin=str(data.get('body_origin') or ('manual' if body.strip() else 'empty'))[:20]
            if body_origin not in ('manual','claim','empty'):body_origin='manual'
            if body_origin=='claim' and not body.strip():body=claim
            if not body.strip():
                body='';body_origin='empty'
            source_id=data.get('source_id')
            if source_id:require('source',source_id)
            # 2026-11-14：只落 5 类之一；旧分类名转成新类并留在 category_legacy 里。
            category,category_legacy=content_index.normalize_category(data.get('category') or '认知底座')
            item=STORE.put('knowledge',{'legacy_id':'新增认知','title':title,'category':category,
                'category_legacy':category_legacy,
                'category_aliases':content_index.row_aliases(category,category_legacy),
                'claim':claim,'boundary':str(data.get('boundary',''))[:12000],'body':body,'body_origin':body_origin,
                'source_id':source_id,'source_locator':{},'links':[],'markers':['待本人确认'],
                'endorsement':'reference_only','validation':'unverified','lifecycle':'active','content_use':'available'})
            return self.json(item,201)
        if path=='/api/knowledge' and method=='GET':
            items=STORE.list('knowledge',10000);q=query.get('q',[''])[0].strip().lower();category=query.get('category',[''])[0]
            # 示例库内容（scope=example，某类账号的做法）不进默认列表：
            # 默认只看本人的认知；带 scope=example 才切到示例库视图。
            scope=query.get('scope',[''])[0]
            if scope=='example':
                pool=[x for x in items if x.get('scope')=='example']
            else:
                pool=[x for x in items if x.get('scope')!='example']
            # 归档/已被替代默认不进列表：与 /api/graph/cards 只算 active 的口径一致，
            # 否则同一时刻两处数字对不上；要看归档时由前端显式带 include_archived=1。
            show_inactive=query.get('include_archived',[''])[0] in ('1','true','yes')
            # 2026-11-17（t64 认知卡删除）：默认不含「已删除」；include_deleted=1 切到回收站视图。
            show_deleted=query.get('include_deleted',[''])[0] in ('1','true','yes')
            deleted=[x for x in pool if (x.get('lifecycle') or 'active')=='deleted']
            hidden=[x for x in pool if not knowledge_active(x)]
            if show_deleted:
                items=list(deleted)
            else:
                items=pool if show_inactive else [x for x in pool if knowledge_active(x)]
            counts={}
            for item in items:
                cat,_legacy=content_index.normalize_category(item.get('category'),item.get('category_legacy'))
                counts[cat]=counts.get(cat,0)+1
            if q:
                items=[x for x in items if q in (x.get('title','')+x.get('claim','')+x.get('body','')
                        +str(x.get('category') or '')+str(x.get('category_legacy') or '')).lower()]
            if category:
                items=[x for x in items if knowledge_in_category(x,category)]
            return self.json({'items':items,'total':len(items),'category_counts':counts,
                              'scope':scope or 'default','archived_count':len(hidden),
                              'deleted_count':len(deleted),'include_deleted':show_deleted,
                              'include_archived':show_inactive})
        if len(bits)==3 and bits[:2]==['api','sources'] and method=='GET':
            source=require('source',bits[2])
            raw=source['text'].encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','text/markdown; charset=utf-8')
            self.send_header('Content-Disposition','attachment; filename="source.md"')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
        if len(bits)==3 and bits[:2]==['api','knowledge']:
            key=bits[2]
            if method=='GET':return self.json(require('knowledge',key))
            if method=='PATCH':
                data=self.body();changes={k:data[k] for k in ('endorsement','validation','lifecycle','content_use') if k in data}
                if data.get('category'):
                    # 2026-11-16：第二大脑「连到支柱」直接写归属，复用同一套分类归一。
                    category,legacy=content_index.normalize_category(data['category'])
                    changes['category']=category;changes['category_legacy']=legacy
                    changes['category_aliases']=list(content_index.CATEGORY_ALIASES.get(category,()))
                if changes.get('endorsement') not in (None,'legacy','accepted','reference_only','rejected'):
                    raise ValueError('认同状态无效。')
                if changes.get('validation') not in (None,'unverified','personal_case','external_evidence','mixed'):
                    raise ValueError('验证状态无效。')
                if changes.get('content_use') not in (None,'review','available'):
                    raise ValueError('用途状态无效。')
                if changes.get('lifecycle') not in (None,'active','archived','superseded','deleted'):
                    raise ValueError('生命周期状态无效。')
                item=STORE.patch('knowledge',key,changes,data.get('revision'))
                # 归档/恢复会影响「认知图谱」和内容检索：让索引在后台按新状态重建，
                # 避免列表已经变了、图谱还按旧状态多算一张（两边数字再次打架）。
                if changes.get('lifecycle'):
                    content_index.ensure_fresh(STORE, wait=True)
                return self.json(item)
            if method=='DELETE':
                # 2026-11-17（t64）：可恢复的软删除（lifecycle=deleted）；彻底删除必须二次确认且仅限已删除的卡。
                item=require('knowledge',key)
                hard=query.get('hard',[''])[0] in ('1','true','yes')
                if hard:
                    if (item.get('lifecycle') or 'active')!='deleted':
                        raise ValueError('彻底删除只对「已删除」的认知卡开放：先删除，再彻底删除。')
                    with STORE.connect() as con:
                        con.execute('DELETE FROM records WHERE kind=? AND id=?',('knowledge',key))
                    STORE.bump('knowledge')  # 直接 SQL 删除绕过 put/patch，手动失效图谱/列表缓存
                    content_index.ensure_fresh(STORE, wait=True)
                    return self.json({'ok':True,'hard':True,'id':key})
                patched=STORE.patch('knowledge',key,{'lifecycle':'deleted'},item.get('revision'))
                content_index.ensure_fresh(STORE, wait=True)
                return self.json(patched)
        if path=='/api/projects' and method=='GET':
            # 2026-11-16（性能 E）：作品列表不再随 bootstrap 下发，改成需要它的页面按需取。
            return self.json(cached_get('projects',('project','source','asset'),
                                        lambda:[{**p,'next_action':next_action(p)} for p in STORE.list('project')]),
                             cache='private, no-cache')
        if path=='/api/projects' and method=='POST':
            data=self.body()
            if data.get('mode')=='direct':
                return self.json(direct_create(data),201)
            title=str(data.get('title','')).strip()[:160] or ('参考视频' if data.get('auto_resolve') else '新的作品')
            source_text=str(data.get('source_text',''))[:120000];share=str(data.get('url',''))[:12000]
            found=re.search(r'https?://[^\s<>]+',share)
            url=found.group(0).rstrip('。），,') if found else ''
            info=video_intake.share_info(share) if data.get('auto_resolve') else None
            if info and not info['url']:raise ValueError('请粘贴完整的抖音或视频号 HTTPS 分享链接。')
            if info:url=info['url']
            knowledge=[]
            for key in data.get('knowledge_ids',[])[:10]:
                item=require('knowledge',key)
                if item.get('content_use')=='review':
                    raise ValueError('这条历史资料有用途限制，请先在认知详情核对后允许用于写稿。')
                knowledge.append(item)
            if knowledge and not source_text:
                source_text='\n\n'.join(x.get('claim') or x['title'] for x in knowledge)
            source=STORE.put('source',{'title':title,'url':url,'share_text':share,'text':source_text,'kind':'link' if url else 'text',
                                     'status':'ready' if source_text.strip() else 'awaiting_transcript'})
            parts=[{'id':r+'_01','role':r,'label':l,'text':source_text if r=='content' and knowledge else ''} for r,l in ROLES]
            project=STORE.put('project',{'title':title,'source_id':source['id'],'draft':parts,'draft_revision':1,
                'is_test':data.get('is_test') is True,
                'draft_changed':True,'confirmed_version_id':None,'stage':'draft','knowledge_ids':[x['id'] for x in knowledge],
                'tags':sorted(set(x.get('category','') for x in knowledge)),'director':[],
                'title_origin':'automatic' if data.get('auto_resolve') and not data.get('title') else 'manual'})
            # An unresolved link is still saved immediately. No made-up extraction.
            if data.get('auto_resolve'):intake_job(project)
            return self.json(project,201)
        if len(bits)>=3 and bits[:2]==['api','projects']:
            key=bits[2];project=require('project',key)
            if len(bits)==3 and method=='GET':
                return self.json({**project,'draft':draft_parts(project.get('draft')),
                    'auto_wash':project.get('auto_wash') or None,
                    'compliance':_cap(lambda:compliance.check(compliance.draft_text(project.get('draft')),use_llm=False),{}),
                    'source':require('source',project['source_id']),
                    'next_action':next_action(project),
                    'assets':[x for x in STORE.list('asset') if x.get('project_id')==key and x.get('purpose','shooting') in ('shooting','direct')],
                    'reference_assets':[x for x in STORE.list('asset') if x.get('project_id')==key and x.get('purpose')=='reference'],
                    'knowledge_candidates':[x for x in STORE.list('knowledge',10000) if x.get('source_id')==project['source_id'] and x.get('endorsement')=='reference_only' and knowledge_active(x)],
                    'artifacts':[x for x in STORE.list('artifact') if x.get('project_id')==key],
                    'clean_plans':[x for x in STORE.list('cleanup_plan',300) if x.get('method')!='rebuild_from_clean_plan'],
                    'transcripts':[{'id':x['id'],'asset_id':x['asset_id'],'words':x.get('words',[])}
                        for x in STORE.list('transcript',200)
                        if x.get('project_id')==key and x.get('asset_id') and x.get('words')],
                    'semantic_plans':[x for x in STORE.list('semantic_plan',500) if x.get('project_id')==key],
                    'anchor_plans':[x for x in STORE.list('anchor_plan',200) if x.get('project_id')==key],
                    'smart_packaging':_cap(lambda:smart_packaging.project_plan(STORE,project),{}),
                    'cover':_cap(lambda:{**cover_packaging.studio(STORE,project),
                        'billing':_cap(billing_route.brief,{})},
                        {'available':False,'missing':['封面模板库'],'styles':[],
                         'recommendation':None,'defaults':{},'avatar':{},'brand':'',
                         'note':'封面模板库读取失败，这一块已降级；稿件与导出不受影响。'}),
                    'director_brief':next((x for x in STORE.list('director_brief',50)
                        if x.get('script_version_id')==project.get('confirmed_version_id')),None),
                    'versions':[{**x,'parts':draft_parts(x.get('parts'))} for x in STORE.list('script_version') if x.get('project_id')==key],
                    'jobs':[{**x,**_job_error_view(x)} for x in STORE.list('job') if x.get('project_id')==key]})
            action=bits[3] if len(bits)>3 else ''
            if action=='lint' and method=='POST':
                data=self.body()
                text=str(data.get('text') or '').strip()
                if not text:
                    text=compliance.draft_text(project.get('draft')).strip()
                if not text:
                    raise ValueError('没有可检查的稿子：先把逐字稿生成或粘贴进来。')
                return self.json(style_lint.check(text))
            if action=='auto-wash' and method=='GET':
                wash_id=(query.get('wash') or [''])[0] or project.get('auto_wash_id') or ''
                record=STORE.get('auto_wash',wash_id) if wash_id else None
                if not record or record.get('project_id')!=key:
                    raise KeyError('没有找到这次的洗稿记录。')
                return self.json(record)
            if action=='auto-wash' and method=='POST':
                data=self.body();act=str(data.get('action') or '')
                if act=='restore':
                    wash_id=str(data.get('auto_wash_id') or project.get('auto_wash_id') or '')
                    record=STORE.get('auto_wash',wash_id) if wash_id else None
                    if not record or record.get('project_id')!=key:
                        raise KeyError('没有找到这次的洗稿记录。')
                    if record.get('outcome')!='applied':
                        raise ValueError('这次没有自动改动，不需要还原。')
                    parts=parts_valid(record.get('parts_before') or [])
                    updated=STORE.patch('project',key,{'draft':parts,'draft_revision':project['draft_revision']+1,
                        'draft_changed':True,'stage':'draft','auto_wash_id':None,'auto_wash':None},project['revision'])
                    STORE.patch('auto_wash',wash_id,{'restored_at':now()})
                    updated['draft']=draft_parts(updated.get('draft'))
                    updated['compliance']=compliance.check(compliance.draft_text(updated.get('draft')),use_llm=False)
                    return self.json(updated)
                if act=='rewash':
                    if not compliance.draft_text(project.get('draft')).strip():
                        raise ValueError('还没有草稿可洗：先生成或粘贴逐字稿。')
                    wash=auto_wash.wash_parts(parts_valid(project.get('draft')))
                    record=STORE.put('auto_wash',{'project_id':key,'job_id':'','mode':'rewash',
                        'outcome':wash['outcome'],'reason':wash['reason'],'changes':wash['changes'],
                        'parts_before':wash['parts_before'],'parts_after':wash['parts_after'],
                        'chars_before':wash['chars_before'],'chars_after':wash['chars_after'],
                        'delta_pct':wash['delta_pct'],'hits_before':wash['hits_before'],
                        'hits_after':wash['hits_after'],'source':'manual'})
                    if wash['outcome']=='applied':
                        parts=parts_valid(wash['parts_after'])
                        updated=STORE.patch('project',key,{'draft':parts,'draft_revision':project['draft_revision']+1,
                            'draft_changed':True,'stage':'draft','auto_wash_id':record['id'],
                            'auto_wash':auto_wash.summary(record)},project['revision'])
                        updated['draft']=draft_parts(updated.get('draft'))
                        updated['compliance']=compliance.check(compliance.draft_text(updated.get('draft')),use_llm=False)
                    else:
                        updated={**project,'draft':draft_parts(project.get('draft'))}
                    return self.json({'auto_wash':auto_wash.summary(record),'project':updated})
                raise ValueError('洗稿操作无效：只支持 restore / rewash。')
            if action=='draft' and method=='PATCH':
                data=self.body();parts=parts_valid(data['parts'])
                if data.get('draft_revision')!=project['draft_revision']:
                    raise Conflict('稿件已被其他页面修改，请刷新后比较再保存。')
                updated=STORE.patch('project',key,{'draft':parts,'draft_revision':project['draft_revision']+1,
                    'draft_changed':True,'stage':'draft','reviewed_artifact_id':None},project['revision'])
                updated['draft']=draft_parts(updated.get('draft'))
                updated['compliance']=compliance.check(compliance.draft_text(updated.get('draft')),use_llm=False)
                return self.json(updated)
            if action=='source' and method=='PATCH':
                data=self.body();source=require('source',project['source_id']);text=str(data.get('text',''))[:120000]
                changes={'text':text,'status':'ready' if text.strip() else 'awaiting_transcript'}
                if source.get('resolution') and text!=source.get('text'):
                    prior=source['resolution']
                    changes['resolution']={**prior,'transcript':text,'cues':[],'words':[],'opening':video_intake.opening_window([],[]),
                        'analysis':{'status':'stale','summary':'','note':'原文已手动修改，旧分析与时间轴已停用。'},'status':'ready' if text.strip() else 'partial'}
                return self.json(STORE.patch('source',source['id'],changes,data.get('revision')))
            if action=='resolve' and method=='POST':
                return self.json(intake_job(project),202)
            if action=='prepare-content' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id']) if data.get('asset_id') else None
                if asset and (asset['project_id']!=key or asset.get('purpose')!='reference'):raise ValueError('需要本作品的参考视频。')
                return self.json(content_job(project,asset),202)
            if action=='prepare-video' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                base=require('artifact',data['artifact_id']) if data.get('artifact_id') else None
                # 字幕样式可选：随请求更新并持久化到作品；不传则沿用该作品已存样式（默认行为不变）
                style=caption_style.sanitize(data.get('caption_style'))
                if style:
                    STORE.patch('project',key,{'caption_style':style})
                    # project 是 patch 前取的旧对象；同步内存副本，确保本次任务立即按新样式渲染
                    project={**project,'caption_style':style}
                return self.json(prepare_video_job(project,asset,base),202)
            if action=='revise-captions' and method=='POST':
                data=self.body();artifact=require('artifact',data['artifact_id'])
                if artifact.get('project_id')!=key or artifact.get('script_version_id')!=project.get('confirmed_version_id') or project.get('draft_changed'):
                    raise Conflict('字幕对应的成片与当前稿件不一致。')
                if artifact.get('kind')!='packaged_video' or not artifact.get('clean_artifact_id') or not artifact.get('caption_cues'):
                    raise ValueError('这版没有可校对的字幕；请先制作字幕包装。')
                edits=data.get('edits')
                if not isinstance(edits,list) or not 1<=len(edits)<=2000:raise ValueError('字幕修改格式无效。')
                lookup={c['id']:c for c in artifact['caption_cues']};changes={}
                for item in edits:
                    if not isinstance(item,dict) or item.get('id') not in lookup or item['id'] in changes:raise ValueError('字幕编号无效或重复。')
                    text=item.get('text')
                    if not isinstance(text,str) or not 0<len(text.strip())<=100:raise ValueError('每段校对字幕需为1至100个字。')
                    changes[item['id']]=text.strip()
                cues=[{**c,'text':changes.get(c['id'],c['text'])} for c in artifact['caption_cues']]
                if cues==artifact['caption_cues']:raise ValueError('字幕未发生变化，无需重新渲染。')
                clean=require('artifact',artifact['clean_artifact_id'])
                if clean.get('project_id')!=key or clean.get('script_version_id')!=project.get('confirmed_version_id'):raise Conflict('清理底片版本不一致。')
                if artifact.get('packaging_method') in (smart_packaging.VERSION,'smart-packaging/2'):
                    previous_plan=require('packaging_plan',artifact['packaging_plan_id'])
                    smart_packaging.validate_plan(STORE,project,previous_plan)
                    transcript=next((t for t in STORE.list('transcript',10000) if t.get('asset_id')==clean['asset_id']),None)
                    if not transcript:raise ValueError('原始转写已缺失，请先恢复转写。')
                    base_cues=smart_packaging.cut_cues(transcript,clean['mapping'],clean['info']['duration_ms'])
                    STORE.put('packaging_captions',{'project_id':key,'clean_artifact_id':clean['id'],
                        'base_cue_digest':smart_packaging.digest(base_cues),'cues':cues,'caption_parent_id':artifact['id']},key=clean['id'])
                    revised_plan=smart_packaging.ensure_plan(STORE,project,clean,previous_plan['selected']['id'])
                    def corrected_work(job_id):
                        stage=lambda value:STORE.patch('job',job_id,{'stage':value})
                        result=smart_packaging.render(STORE,require('project',key),revised_plan,job_id,DATA,False,stage)
                        item=result['artifact']
                        STORE.patch('artifact',item['id'],{'caption_parent_id':artifact['id'],'caption_origin':'user_corrected'})
                        if not result.get('stale'):
                            attach_if_current(key,revised_plan['script_version_id'],{'latest_artifact_id':item['id'],'reviewed_artifact_id':None,'stage':'review'})
                        result['artifact']=require('artifact',item['id'])
                        return result
                    return self.json(new_job(key,'caption_revision',{'artifact_id':artifact['id'],'edits':edits},corrected_work),202)
                def work(job_id):
                    STORE.patch('job',job_id,{'stage':'按校对字幕重出视频，保留原声和剪切时间轴'})
                    overlays=[o for o in artifact.get('overlays',[]) if any(c['id']==o.get('cue_id') and o.get('quote','') in c['text'] for c in cues)]
                    # 校对重出沿用该成片当时的字幕样式，不因校对丢字体/增强设置
                    result=render_caption_artifact(project,clean,cues,overlays,'本人校对字幕；原始机器转写和上一版视频保留。',job_id,artifact.get('caption_style'))
                    STORE.patch('artifact',result['id'],{'caption_parent_id':artifact['id'],'caption_origin':'user_corrected'})
                    return {'artifact':require('artifact',result['id']),'stage':'校对版已生成，请重新播放审阅'}
                return self.json(new_job(key,'caption_revision',{'artifact_id':artifact['id'],'edits':edits},work),202)
            if action=='extract-knowledge' and method=='POST':
                if not providers.available()['llm']:raise providers.ProviderMissing('模型尚未配置，可先手动记录认知。')
                source=require('source',project['source_id'])
                if not source['text'].strip():raise ValueError('需要先取得正文，不能凭链接猜认知。')
                def work(job_id):
                    def _payload(seg):
                        return {'source_text':seg}
                    def _call(payload,tag):
                        return providers.llm_json(prompt_store.get('reference.knowledge'),payload,tag=tag)
                    def _progress(index,total,merging):
                        STORE.patch('job',job_id,{'stage':'正在合并 %d 段提炼结果'%total if merging
                            else '参考正文较长，正在提炼第 %d/%d 段'%(index,total)})
                    def _valid(value):
                        rows=value.get('items') if isinstance(value,dict) else None
                        return isinstance(rows,list) and 1<=len(rows)<=5
                    def _merge(results):
                        try:
                            merged=long_text.merge_json(
                                '仅返回 {"items":[{"title","claim","boundary","quote"}]}，items 最多 5 条；'
                                'quote 必须逐字来自对应分段原文，重复的合并。',results,'reference_knowledge')
                            if _valid(merged):return merged
                        except Exception:
                            pass
                        picked=[];seen=set()
                        for part in results:
                            for item in (part.get('items') or []) if isinstance(part,dict) else []:
                                if not isinstance(item,dict):continue
                                key=(str(item.get('title') or '').strip(),str(item.get('claim') or '').strip())
                                if not key[0] or not key[1] or key in seen:continue
                                seen.add(key);picked.append(item)
                                if len(picked)>=5:break
                            if len(picked)>=5:break
                        return {'items':picked}
                    result=long_text.run(source['text'],_payload,_call,'reference_knowledge',
                        progress=_progress,merge=_merge)
                    items=result.get('items')
                    if not isinstance(items,list) or not 1<=len(items)<=5:raise ValueError('模型认知条目格式无效，未入库。')
                    clean=[]
                    for item in items:
                        if not isinstance(item,dict) or not isinstance(item.get('claim'),str) or not item.get('title'):raise ValueError('认知条目缺少标题或主张。')
                        quote=str(item.get('quote',''));matched=bool(quote and quote in source['text'])
                        clean.append({'legacy_id':'链接提炼','title':str(item['title'])[:160],'claim':item['claim'][:12000],
                            'boundary':str(item.get('boundary','适用边界待补'))[:12000],'body':item['claim'][:12000],
                            'category':'随手记','category_legacy':'链接积累','category_aliases':['随手记','链接积累'],'source_id':source['id'],'source_revision':source['revision'],'source_snapshot':source['text'],'source_quote':quote if matched else '',
                            'source_locator':{},'links':[],'markers':['模型提炼待核对']+([] if matched else ['引文待定位']),
                            'endorsement':'reference_only','validation':'unverified','lifecycle':'active','content_use':'available'})
                    saved=[STORE.put('knowledge',x) for x in clean]
                    return {'items':saved,'note':'待核对认知已入库；本人认同与验证状态尚未改变。'}
                return self.json(new_job(key,'extract_knowledge',{'source_id':source['id'],'source_revision':source['revision']},work),202)
            if action=='writing-suggestions' and method=='POST':
                return self.json(creation_service.writing(project,self.body()),202)
            if action=='generate' and method=='POST':
                if not providers.available()['llm']:
                    raise providers.ProviderMissing('未配置模型服务，可直接在讲稿页手动写；不会用规则稿假装 AI 私人定制。')
                source=require('source',project['source_id'])
                if not source.get('text','').strip():
                    raise ValueError('还没有参考文字；可先在讲稿页手动写，或补充字幕后再让 AI 改写。')
                profile=STORE.get('profile','owner') or {}
                profile_compact=style_profile.profile_context(profile)
                profile_block=style_profile.profile_block(profile)
                style_block=style_profile.profile_style_block(STORE)
                use_default=creation_service.flag((project.get('creation_brief') or {}).get('use_default_principles'),True)
                default_principles=creation_service.default_principles_snapshot() if (use_default and not project.get('knowledge_ids')) else []
                principles_block=creation_service.principles_block(default_principles)
                def work(job_id):
                    def _payload(seg):
                        return {'profile':profile_compact,'profile_block':profile_block,'style_card':style_block,
                            'personal_principles':default_principles,'principles_block':principles_block,
                            'reference_text':seg,'current_draft':project['draft']}
                    def _call(payload,tag):
                        last=None
                        for attempt in (1,2):
                            result=providers.llm_json(prompt_store.get('reference.rewrite'),payload,
                                tag=tag if attempt==1 else tag+'#retry')
                            try:
                                if not isinstance(result,dict):raise ValueError('模型没有返回JSON对象')
                                parts_valid(result.get('parts'))
                                return result
                            except ValueError as exc:
                                last=(exc,result)
                        raise ValueError(str(last[0])+'；模型原始返回：'+json.dumps(last[1],ensure_ascii=False)[:400])
                    def _progress(index,total,merging):
                        STORE.patch('job',job_id,{'stage':'正在合并 %d 段改写'%total if merging
                            else '参考正文较长，正在改写第 %d/%d 段'%(index,total)})
                    def _merge(results):
                        try:
                            merged=long_text.merge_json(
                                '仅返回 {"parts":[{"role","text"}]}，按 hook/content/ending 顺序给出；'
                                '把多段改写合成一份完整讲稿，重复的合并，不新增分段里没有的观点。',
                                results,'reference_rewrite')
                            if isinstance(merged,dict):
                                parts_valid(merged.get('parts'))
                                return merged
                        except Exception:
                            pass
                        for part in results:
                            try:
                                parts_valid(part.get('parts'))
                                return part
                            except (AttributeError,ValueError):
                                continue
                        raise ValueError('合并后的讲稿缺少开头、主体或结尾，请重新生成。')
                    result=long_text.run(source['text'],_payload,_call,'reference_rewrite',
                        progress=_progress,merge=_merge)
                    parts=parts_valid(result.get('parts'))
                    suggestion=STORE.put('suggestion',{'project_id':key,
                        'base_draft_revision':project['draft_revision'],'parts':parts,'source_id':source['id']})
                    return {'suggestion':suggestion,'note':'建议已生成，接受后才会替换当前草稿。'}
                return self.json(new_job(key,'rewrite',{'draft_revision':project['draft_revision'],'source_id':source['id']},work),202)
            if action=='accept-suggestion' and method=='POST':
                data=self.body();suggestion=require('suggestion',data['suggestion_id'])
                if suggestion['project_id']!=key:
                    raise Conflict('建议不属于这个项目，请重新生成。')
                if suggestion['base_draft_revision']!=project['draft_revision']:
                    # 生成时自动填入空草稿会让 revision+1；只要草稿仍与建议逐段一致，就允许采纳并定稿。
                    same=lambda rows:[{'role':str(x.get('role')),'text':str(x.get('text') or '')} for x in (rows or [])]
                    if same(project.get('draft'))!=same(suggestion.get('parts')):
                        raise Conflict('建议对应旧稿，请保留当前修改并重新生成建议。')
                snapshot=suggestion.get('input_snapshot')
                if snapshot:
                    source=require('source',project['source_id'])
                    if source['revision']!=snapshot['source']['revision']:
                        raise Conflict('来源资料已变化，请结合最新资料重新生成建议。')
                    creation_service.knowledge_snapshot(project.get('knowledge_ids',[]))
                parts=parts_valid(suggestion['parts'])
                missing=missing_core(parts)
                if missing:
                    raise ValueError('建议还差'+'、'.join(missing)+'，没有定稿；请重新生成或手动补齐后再确认。')
                report=compliance.apply_waivers(compliance.check(compliance.draft_text(parts),use_llm=False),
                    data.get('compliance_waivers'))
                if not report.get('ok'):
                    return self.json({'error':'建议里还有必删表达，没有定稿；先按清单改稿再确认。'
                        +compliance.block_message(report),'code':'compliance_blocked','compliance':report},409)
                source=require('source',project['source_id'])
                next_rev=project['draft_revision']+1
                version=STORE.put('script_version',{'project_id':key,'parts':parts,'draft_revision':next_rev,'confirmed_at':now(),'source_id':project['source_id'],'source_revision':source['revision'],'source_snapshot':source['text'],
                    'compliance':{'ok':True,'checked_at':now(),'counts':report.get('counts'),'waived_count':report.get('waived_count',0)},
                    'compliance_waivers':report.get('waivers',[]),
                    'confirmation_scope':'integration_test' if project.get('is_test') else 'personal_workflow'})
                updated=STORE.patch('project',key,{'draft':parts,'draft_revision':next_rev,'draft_changed':False,'stage':'shoot',
                    'confirmed_version_id':version['id'],'reviewed_artifact_id':None,'auto_wash_id':suggestion.get('auto_wash_id'),'auto_wash':suggestion.get('auto_wash'),
                    'publication_pack':publication_pack(project,version),'director':media.director(version['parts'])},project['revision'])
                updated['draft']=draft_parts(updated.get('draft'))
                updated['compliance']=report
                updated['next_action']=next_action(updated)
                return self.json(updated)
            if action=='confirm' and method=='POST':
                data=self.body()
                if data.get('draft_revision')!=project['draft_revision']:
                    raise Conflict('稿件版本已变化，请先保存并重新确认。')
                if draft_missing(project['draft']):
                    raise ValueError('稿件还是空的，写几句再确认。')
                report=compliance.apply_waivers(compliance.check(compliance.draft_text(project['draft']),use_llm=False),
                    data.get('compliance_waivers'))
                if not report.get('ok'):
                    return self.json({'error':'稿子里还有必删表达，先按清单改稿再确认。'
                        +compliance.block_message(report),'code':'compliance_blocked','compliance':report},409)
                source=require('source',project['source_id'])
                version=STORE.put('script_version',{'project_id':key,'parts':project['draft'],'draft_revision':project['draft_revision'],'confirmed_at':now(),'source_id':project['source_id'],'source_revision':source['revision'],'source_snapshot':source['text'],
                    'compliance':{'ok':True,'checked_at':now(),'counts':report.get('counts'),'waived_count':report.get('waived_count',0)},
                    'compliance_waivers':report.get('waivers',[]),
                    'confirmation_scope':'integration_test' if project.get('is_test') else 'personal_workflow'})
                return self.json(STORE.patch('project',key,{'confirmed_version_id':version['id'],'draft_changed':False,
                    'stage':'shoot','publication_pack':publication_pack(project,version),'director':media.director(version['parts'])},project['revision']))
            if action=='metadata' and method=='PATCH':
                data=self.body();pack=project.get('publication_pack')
                if not pack or project.get('draft_changed'):
                    raise Conflict('需要先确认当前稿件。')
                pack={**pack,'selected_title':str(data.get('selected_title',pack['selected_title']))[:120],
                     'caption':str(data.get('caption',pack['caption']))[:12000],
                     'tags':[str(t)[:40] for t in data.get('tags',pack['tags'])[:20]]}
                pack['copy_check']=read_script.check_copy(pack)
                return self.json(STORE.patch('project',key,{'publication_pack':pack},project['revision']))
            if action=='cut' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                if project.get('mode')=='direct':
                    material_gate(project,asset)
                else:
                    if not project.get('confirmed_version_id') or project.get('draft_changed'):
                        raise Conflict('请先确认当前拍摄稿。')
                    if asset.get('purpose','shooting')!='shooting' or asset['script_version_id']!=project['confirmed_version_id']:
                        raise Conflict('素材关联旧稿，不能静默切换到新稿。请新建拍摄批次。')
                return self.json(do_cut(project,asset,data.get('mode','natural'),data.get('segments')),202)
            if action=='direct-run' and method=='POST':
                data=self.body();asset=require('asset',str(data.get('asset_id') or project.get('current_asset_id') or ''))
                if asset['project_id']!=key:raise KeyError('asset')
                material_gate(project,asset)
                return self.json(direct_pipeline_job(project,asset,data.get('silence_mode','conservative')),202)
            if action=='transcribe' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                if asset.get('purpose','shooting')=='direct':
                    if project.get('mode')!='direct':raise Conflict('这条素材属于直剪作品，请在对应作品里处理。')
                elif asset.get('purpose','shooting')!='shooting':raise ValueError('参考素材请在资料页执行本地转写。')
                if not providers.available()['asr']:raise providers.ProviderMissing(providers.asr_unavailable_reason()+' 可以先用真实静音清理。')
                def work(job_id):
                    audio=DATA/'artifacts'/(job_id+'.mp3');media.extract_audio(asset['path'],audio)
                    transcript=providers.asr(audio,stage=lambda s:STORE.patch('job',job_id,{'stage':s}))
                    record=STORE.put('transcript',{**transcript,'asset_id':asset['id'],'project_id':key})
                    return {'transcript':record,'note':'原始转写保留。口误位置需核对后再提交保留片段，不能凭净文本猜时间。'}
                return self.json(new_job(key,'transcribe',{'asset_id':asset['id']},work),202)
            if action=='textcut-state' and method=='POST':
                data=self.body();transcript=require('transcript',data['transcript_id'])
                if transcript.get('project_id')!=key:raise KeyError('transcript')
                asset=require('asset',transcript['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                return self.json(textcut_state(asset,transcript,asset.get('script_version_id')))
            if action=='textcut-estimate' and method=='POST':
                data=self.body();transcript=require('transcript',data['transcript_id'])
                if transcript.get('project_id')!=key:raise KeyError('transcript')
                asset=require('asset',transcript['asset_id'])
                ids=data.get('word_ids') or []
                if not isinstance(ids,list):raise ValueError('word_ids 需为列表。')
                plan=latest_clean_plan(asset,asset.get('script_version_id'))
                base=media.complement_segments(plan['segments'],asset['info']['duration_ms']) if plan and plan.get('segments') else None
                return self.json(textcut_preview(transcript['words'],ids,asset['info']['duration_ms'],base))
            if action=='remove-words' and method=='POST':
                data=self.body();transcript=require('transcript',data['transcript_id']);asset=require('asset',transcript['asset_id'])
                if transcript['project_id']!=key or asset['project_id']!=key:raise KeyError('transcript')
                if project.get('mode')=='direct':
                    material_gate(project,asset)
                elif project.get('draft_changed') or asset['script_version_id']!=project.get('confirmed_version_id'):
                    raise Conflict('转写素材与当前定稿不一致，请核对版本。')
                words=transcript['words'];duration=asset['info']['duration_ms']
                plan=latest_clean_plan(asset,project.get('confirmed_version_id'))
                base=media.complement_segments(plan['segments'],duration) if plan and plan.get('segments') else None
                ids=[k for k in (data.get('word_ids') or []) if isinstance(k,str)]
                removed,segments=media.word_cut_ranges(words,ids,duration,base,cues=transcript.get('cues') or [])
                return self.json(do_cut(project,asset,'asr_words_reviewed',segments,
                    {'removed':removed,'method':'words_over_clean_plan','base_plan_id':(plan or {}).get('id'),
                     'deleted_word_ids':sorted(set(ids))},skip_validate=True),202)
            if action=='semantic-cut' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                return self.json(semantic_cut_job(project,asset),202)
            if action=='semantic-auto-apply' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                return self.json(semantic_auto_job(project,asset,data.get('silence_mode','conservative')),202)
            if action=='apply-semantic-cut' and method=='POST':
                data=self.body()
                if data.get('undo'):
                    artifact=require('artifact',data['artifact_id'])
                    return self.json(semantic_undo(project,artifact))
                asset=require('asset',data['asset_id']);plan=require('semantic_plan',data['plan_id'])
                if asset['project_id']!=key or plan.get('project_id')!=key:raise KeyError('plan')
                return self.json(semantic_apply_job(project,asset,plan,data.get('deletion_ids'),
                    data.get('silence_mode','conservative'),bool(data.get('auto_apply'))),202)
            if action=='rebuild-estimate' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                if project.get('draft_changed') or asset.get('script_version_id')!=project.get('confirmed_version_id'):
                    raise Conflict('素材关联旧稿，不能静默切换到新稿。')
                target=float(data.get('target_s') or 196)
                return self.json(rebuild_estimate(asset,project['confirmed_version_id'],target))
            if action=='rebuild-render' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                if project.get('draft_changed') or asset.get('script_version_id')!=project.get('confirmed_version_id'):
                    raise Conflict('素材关联旧稿，不能静默切换到新稿。')
                target=float(data.get('target_s') or 196)
                if not 30<=target<=1800:raise ValueError('目标时长需在 30–1800 秒之间。')
                lufs=float(data.get('lufs') or -9.5)
                if not -16<=lufs<=-6:raise ValueError('响度目标需在 -16 ~ -6 LUFS 之间。')
                return self.json(rebuild_job(project,asset,target,lufs),202)
            if action=='anchor-align' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id'])
                if asset['project_id']!=key:raise KeyError('asset')
                def work(job_id):
                    stage=lambda s:STORE.patch('job',job_id,{'stage':s})
                    return anchor_job.build_plan(STORE,require('project',key),asset,job_id,DATA,stage)
                return self.json(new_job(key,'anchor_align',{'asset_id':asset['id'],'script_version_id':project['confirmed_version_id']},work),202)
            if action=='anchor-apply' and method=='POST':
                data=self.body();asset=require('asset',data['asset_id']);plan=require('anchor_plan',data['plan_id'])
                if asset['project_id']!=key or plan.get('project_id')!=key:raise KeyError('plan')
                sel=data.get('selection') or {}
                bad=[p['part_id'] for p in plan.get('parts',[]) if p.get('takes') and not
                     any(isinstance(sel.get(p['part_id']),int) and t['take_index']==sel[p['part_id']] for t in p['takes'])]
                if bad:raise ValueError('请先为每段稿本选择一条 take。')
                def work(job_id):
                    stage=lambda s:STORE.patch('job',job_id,{'stage':s})
                    # 2026-11-10（试用反馈 P0-2）：未传 keep_improvised 时默认保留全部稿外口播；
                    # 传了（哪怕是空列表）就按传的来，方便逐条取消。
                    keep_improv=data.get('keep_improvised')
                    if keep_improv is None:
                        keep_improv=[x['cue_id'] for x in plan.get('improvised',[])]
                    result=anchor_job.apply_plan(STORE,require('project',key),asset,plan,
                        data.get('selection') or {},keep_improv,
                        data.get('silence_mode','conservative'),job_id,DATA,stage)
                    artifact=result.get('artifact')
                    if artifact:
                        attach_if_current(key,project['confirmed_version_id'],
                            {'latest_artifact_id':artifact['id'],'stage':'review','reviewed_artifact_id':None})
                    return result
                return self.json(new_job(key,'anchor_cut',{'plan_id':plan['id']},work),202)
            if action=='packaging-plan' and method=='POST':
                data=self.body();artifact=require('artifact',data.get('artifact_id',''))
                if artifact.get('project_id')!=key:raise KeyError('artifact')
                try:
                    plan=smart_packaging.ensure_plan(STORE,project,artifact,data.get('style_id'),data.get('content'),data.get('expected_plan_id'))
                except smart_packaging.NoTranscript as exc:
                    raise ValueError(str(exc)+' 也可以先到拍摄上传页对素材点「识别原始语音」，'
                        '再回来排卡片；转写失败时在设置里把语音识别通道切到 auto 或 cloud。') from None
                smart_packaging.validate_plan(STORE,project,plan)
                return self.json(plan)
            if action in ('packaging-preview','packaging-render') and method=='POST':
                data=self.body();plan=require('packaging_plan',data.get('plan_id',''))
                clean=smart_packaging.validate_plan(STORE,project,plan)
                preview=action=='packaging-preview'
                if plan['selected'].get('spatial_template'):
                    gaps=smart_packaging.spatial_packaging.capabilities()['missing'] + smart_packaging.runtime()['missing']
                elif plan['selected'].get('eva_template'):
                    gaps=smart_packaging.eva_packaging.missing()
                else:
                    gaps=smart_packaging.runtime()['missing']
                if gaps:
                    raise ValueError('包装环境缺少：'+'、'.join(gaps))
                if not preview and not any(a.get('kind')=='packaging_preview' and a.get('packaging_plan_id')==plan['id']
                        and a.get('packaging_fingerprint')==plan['fingerprint'] and a.get('project_id')==key
                        and Path(a.get('path','')).is_file() for a in STORE.list('artifact',10000)):
                    raise Conflict('请先生成并查看当前风格的片段预览。')
                bgm=bool(data.get('bgm'))
                def work(job_id):
                    stage=lambda value:STORE.patch('job',job_id,{'stage':value})
                    result=smart_packaging.render(STORE,require('project',key),plan,job_id,DATA,preview,stage,bgm)
                    if not preview and not result.get('stale'):
                        smart_packaging.validate_plan(STORE,require('project',key),plan)
                        attach_if_current(key,plan['script_version_id'],{'latest_artifact_id':result['artifact']['id'],
                            'stage':'review','reviewed_artifact_id':None})
                    return result
                return self.json(new_job(key,'packaging_preview' if preview else 'packaging_render',
                    {'plan_id':plan['id'],'artifact_id':clean['id'],'bgm':bgm},work),202)
            if action=='package-lite' and method=='POST':
                data=self.body();artifact=require('artifact',data['artifact_id'])
                if artifact['project_id']!=key:raise KeyError('artifact')
                if artifact.get('kind')!='clean_video':
                    raise ValueError('学习包装（轻）需要清理底片；已包装成片请用字幕校对或 MotionTemplates。')
                def work(job_id):
                    stage=lambda s:STORE.patch('job',job_id,{'stage':s})
                    result=packaging_job.package_job(STORE,require('project',key),artifact,job_id,DATA,stage)
                    item=result.get('artifact')
                    if item:
                        attach_if_current(key,project['confirmed_version_id'],
                            {'latest_artifact_id':item['id'],'stage':'review','reviewed_artifact_id':None})
                    return result
                return self.json(new_job(key,'package_lite',{'artifact_id':artifact['id']},work),202)
            if action=='director-brief' and method=='POST':
                data=self.body()
                if not project.get('confirmed_version_id') or project.get('draft_changed'):
                    raise Conflict('请先确认当前稿件。')
                version=require('script_version',project['confirmed_version_id'])
                brief=director_pass.ensure_brief(STORE,version)
                item=data.get('item')
                if item is not None and item not in director_pass.ITEMS:
                    raise ValueError('导演三件套条目无效。')
                if item is not None and not providers.available()['llm']:
                    raise providers.ProviderMissing('生成模型未配置；当前显示规则兜底版三件套。')
                def work(job_id):
                    STORE.patch('job',job_id,{'stage':'导演正在生成'+{'mood_arc':'情绪弧线','pacing':'节奏设计','visual_plan':'视觉预埋'}.get(item,'三件套')})
                    fresh=STORE.get('script_version',version['id'])
                    changes={}
                    if item:
                        changes[item]=director_pass.generate_item(item,fresh['parts'])
                    else:
                        for name in director_pass.ITEMS:
                            changes[name]=director_pass.generate_item(name,fresh['parts'])
                    previous={k:brief.get(k) for k in director_pass.ITEMS}
                    merged={**{k:v for k,v in brief.items() if k not in changes},**changes,
                            'previous':previous,'previous_replaced_at':now()}
                    saved=STORE.put('director_brief',merged,key=brief['id'])
                    return {'brief':saved,'stage':'导演三件套已生成，可在发布文案页查看'}
                return self.json(new_job(key,'director_brief',{'item':item},work),202)
            if action=='motion' and method=='POST':
                data=self.body();artifact=require('artifact',data['artifact_id'])
                if artifact['project_id']!=key:raise KeyError('artifact')
                return self.json(motion_job(project,artifact,data.get('brief'),bool(data.get('render'))),202)
            if action=='cover-unlock' and method=='POST':
                data=self.body()
                return self.json(cover_packaging.unlock(STORE,str(data.get('template_id') or '')))
            if action=='cover-generate' and method=='POST':
                return self.json(cover_generate_job(project,self.body()),202)
            if action=='review' and method=='POST':
                data=self.body()
                # 一键审阅：不传 artifact_id 时取本作品最近一次成片，前端不必自己猜 id。
                aid=str(data.get('artifact_id') or project.get('latest_artifact_id') or '')
                if not aid:raise ValueError('这条作品还没有可审阅的成片，先完成净剪或包装出片。')
                artifact=require('artifact',aid)
                if artifact['project_id']!=key or artifact['kind'] not in ('clean_video','packaged_video'):
                    raise ValueError('只能审核本项目可播放的成片。')
                if project.get('mode')=='direct':
                    if artifact.get('asset_id')!=project.get('current_asset_id'):
                        raise Conflict('这版成片对应的素材已更换，请审阅当前素材的最新成片。')
                elif artifact.get('script_version_id')!=project.get('confirmed_version_id') or project.get('draft_changed'):
                    raise Conflict('当前稿件与成片版本不一致。')
                STORE.put('review',{'project_id':key,'artifact_id':artifact['id'],'decision':'accepted',
                    'scope':'integration_test' if project.get('is_test') else 'personal_workflow'})
                return self.json(STORE.patch('project',key,{'reviewed_artifact_id':artifact['id'],'stage':'ready_to_export'},project['revision']))
            if action=='export' and method=='GET':
                pack=project.get('publication_pack');version=require('script_version',project.get('confirmed_version_id',''))
                if project.get('draft_changed'):raise Conflict('请先确认最新稿件。')
                report=compliance.apply_waivers(compliance.check(compliance.draft_text(version.get('parts')),use_llm=False),
                    version.get('compliance_waivers'))
                if not report.get('ok'):
                    return self.json({'error':'导出被拦截：定稿里还有必删表达，请改稿并重新确认后再导出。'
                        +compliance.block_message(report),'code':'compliance_blocked','compliance':report},409)
                reviewed=None
                if query.get('include_video')==['1']:
                    if not project.get('reviewed_artifact_id'):
                        raise Conflict('成片还没审阅，先点「确认审阅通过」，再导出含视频的四件套。')
                    reviewed=require('artifact',project.get('reviewed_artifact_id'))
                    if reviewed.get('project_id')!=key or reviewed.get('script_version_id')!=project['confirmed_version_id']:raise Conflict('请先审阅当前稿件对应的成片。')
                target=DATA/'packages'/('delivery-'+key+'-r'+str(project['revision'])+('-full-aigc.zip' if reviewed else '-text.zip'))
                delivery_name=branding.file_stem(STORE,project.get('title'),'四件套' if reviewed else '发布资料')+'.zip'
                # 四件套（含成片）并入统一导出计次；到期放行已有成片，超次拒绝（协议第五条第 2 款、第六条第 1 款）
                video_src=None
                if reviewed:
                    blocked=clip_export._export_gate(key,'delivery_zip')
                    if blocked:return self.json(blocked,403)
                    try:
                        video_src,_=clip_export.prepare_download(Path(reviewed['path']),DATA,clip_export._export_max_height(),key)
                    except Exception:
                        clip_export._export_cancel(key,'delivery_zip')
                        raise
                if target.is_file():return self.send_file(target,download=True,filename=delivery_name)
                temporary=DATA/'packages'/(uid('delivery')+'.tmp')
                read_md,_=read_script.build_read(version)
                cues=(reviewed.get('caption_cues') if reviewed and reviewed.get('caption_cues')
                      else read_script.build_captions(version))
                publication={**pack,'copy_check':read_script.check_copy(pack),
                    'caption_alignment':read_script.alignment_note(cues),
                    'read_script':'script-read.md 是念稿版：｜短停（约0.3秒） ‖换气（约0.8秒） ▲重音 ■收句；不做 TTS。'}
                try:
                    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
                        z.writestr('script.md','# '+project['title']+'\n\n'+'\n\n'.join('## '+p['label']+'\n\n'+p['text'] for p in version['parts']))
                        z.writestr('script-read.md',read_md)
                        z.writestr('captions.srt',read_script.srt(cues))
                        z.writestr('publication.json',json.dumps(publication,ensure_ascii=False,indent=2))
                        z.writestr('creative-plan.json',json.dumps(project.get('director',[]),ensure_ascii=False,indent=2))
                        if reviewed:
                            z.write(str(video_src),'video.mp4')
                            if reviewed.get('subtitle_path') and Path(reviewed['subtitle_path']).is_file():z.write(reviewed['subtitle_path'],'captions.srt')
                            if reviewed.get('recipe_path') and Path(reviewed['recipe_path']).is_file():z.write(reviewed['recipe_path'],'packaging-recipe.json')
                            if reviewed.get('cover_id'):
                                cover=require('artifact',reviewed['cover_id'])
                                z.write(cover['path'],'cover'+(Path(cover['path']).suffix.lower() or '.png'))
                        z.writestr('README.txt','这是定稿交付包，尚未发送到任何社交平台。'+('包含本人已审阅的成片。' if reviewed else '视频请在网页中单独下载。')+'script-read.md 是念稿版（自己念，不做 TTS）；captions.srt 时间轴是按 4.2 字/秒估算的，录完真声后按实际口播微调，行序不用改。封面标题的套路词检查在 publication.json 的 copy_check 里。')
                    if require('project',key)['revision']!=project['revision']:raise Conflict('资料刚刚发生变化，请重新下载最新交付包。')
                    temporary.replace(target)
                except Exception:
                    if reviewed:clip_export._export_cancel(key,'delivery_zip')
                    raise
                return self.send_file(target,download=True,filename=delivery_name)
        if path=='/api/uploads' and method=='POST':
            data=self.body();purpose=data.get('purpose','shooting')
            if purpose not in ('reference','shooting','capture','direct'):raise ValueError('上传用途无效。')
            if purpose=='capture':
                # 吃文件出文案：不限项目，音/视频/图片都收；真正处理在 /api/capture/file。
                try:
                    size=int(data.get('size') or 0)
                except (TypeError,ValueError):
                    raise ValueError('文件大小无效。') from None
                if not 0<size<=4*1024*1024*1024:raise ValueError('本地单文件上限 4GB。')
                filename=Path(str(data.get('filename') or '')).name
                if not filename or Path(filename).suffix.lower() not in content_capture.SUPPORTED:
                    raise ValueError('当前支持音频 mp3/m4a/wav/amr/aac/flac/ogg，视频 mp4/mov/mkv/webm/avi 等，图片 jpg/png/webp/bmp 等。')
                session=STORE.put('upload',{'project_id':None,'script_version_id':None,'purpose':'capture',
                    'source_revision':None,'auto_prepare':False,'filename':filename,'size':size,
                    'offset':0,'status':'uploading'})
                return self.json(session,201)
            project=require('project',data['project_id'])
            if purpose=='shooting' and (not project.get('confirmed_version_id') or project.get('draft_changed')):raise Conflict('上传前请确认拍摄稿。')
            if purpose=='direct':
                if project.get('mode')!='direct':raise Conflict('这条作品不是直剪模式；请到首页用「导入已有视频，直接剪辑」新建。')
                running=next((j for j in STORE.list('job') if j.get('project_id')==project['id']
                    and j.get('kind')=='direct_pipeline' and j.get('status') in ('queued','running')),None)
                if running:raise Conflict('当前素材正在处理中，请先等它完成或失败后再导入新视频。')
            size=int(data['size'])
            if not 0<size<=4*1024*1024*1024:raise ValueError('本地单文件上限 4GB。')
            filename=Path(str(data['filename'])).name
            if Path(filename).suffix.lower() not in ('.mp4','.mov','.webm','.mkv','.m4v'):
                raise ValueError('当前支持 mp4/mov/webm/mkv/m4v，实际类型还会经 ffprobe 校验。')
            source=require('source',project['source_id'])
            session=STORE.put('upload',{'project_id':project['id'],'script_version_id':project['confirmed_version_id'] if purpose=='shooting' else None,
                                     'purpose':purpose,'source_revision':source['revision'],'auto_prepare':data.get('auto_prepare',True) is True,
                                     'filename':filename,'size':size,'offset':0,'status':'uploading'})
            return self.json(session,201)
        if len(bits)>=3 and bits[:2]==['api','uploads']:
            session=require('upload',bits[2]);part=DATA/'uploads'/(session['id']+'.part')
            if method=='GET' and len(bits)==3:
                return self.json({**session,'offset':part.stat().st_size if part.exists() else session['offset']})
            if method=='PUT' and len(bits)==3:
                size=int(self.headers.get('Content-Length',0));offset=int(query.get('offset',['0'])[0])
                if not 0<size<=8*1024*1024:raise ValueError('每片上限 8MB。')
                with UPLOAD_LOCK:
                    session=require('upload',session['id'])
                    if session['status']!='uploading':raise Conflict('会话已完成，不能继续追加。')
                    actual=part.stat().st_size if part.exists() else 0
                    if offset!=actual or actual+size>session['size']:raise Conflict('上传位置已变化，请恢复会话后重试。')
                    raw=self.rfile.read(size)
                    if len(raw)!=size:raise ValueError('分片未完整收到，请重试当前片段。')
                    with part.open('ab') as out:out.write(raw)
                    session=STORE.patch('upload',session['id'],{'offset':actual+size})
                return self.json(session)
            if method=='POST' and len(bits)==4 and bits[3]=='complete':
                with UPLOAD_LOCK:
                    session=require('upload',session['id'])
                    if session['status']=='ready':return self.json(require('asset',session['asset_id']))
                    if not part.exists() or part.stat().st_size!=session['size']:raise ValueError('文件尚未完整上传。')
                    # capture 允许纯音频（mp3/wav 等），media.probe 只认视频，改到 content_capture 里探测。
                    info={} if session.get('purpose')=='capture' else media.probe(part)
                    target=part.with_suffix(Path(session['filename']).suffix.lower());part.replace(target)
                    asset=STORE.put('asset',{'project_id':session['project_id'],'script_version_id':session['script_version_id'],
                         'purpose':session.get('purpose','shooting'),'filename':session['filename'],'path':str(target),'info':info})
                    if asset.get('purpose')=='direct':
                        # 素材版本锚：每次导入递增；旧素材任务完成后不得把结果挂到新素材上。
                        current=require('project',session['project_id'])
                        mver=int(current.get('material_version') or 0)+1
                        asset=STORE.patch('asset',asset['id'],{'material_version':mver})
                        STORE.patch('project',current['id'],{'material_version':mver,'current_asset_id':asset['id'],
                            'stage':'edit','latest_artifact_id':None,'reviewed_artifact_id':None},current['revision'])
                    STORE.patch('upload',session['id'],{'status':'ready','asset_id':asset['id']})
                if asset.get('purpose')=='reference' and session.get('auto_prepare'):
                    content_job(require('project',session['project_id']),asset)
                if asset.get('purpose')=='direct' and session.get('auto_prepare'):
                    direct_pipeline_job(require('project',session['project_id']),asset)
                return self.json(asset)
        if len(bits)>=3 and bits[:2]==['api','bench']:
            sub=bits[2]
            if sub=='accounts':
                if len(bits)==3 and method=='POST':
                    account=bench_service.save_account(self.body())
                    return self.json(account,200 if account.get('reused') else 201)
                if len(bits)==3 and method=='GET':
                    return self.json({'items':cached_get('bench_accounts',('bench_account','job'),
                                                         bench_service.list_accounts)})
                if len(bits)==5 and bits[4]=='collect' and method=='POST':
                    data=self.body() if int(self.headers.get('Content-Length',0) or 0) else {}
                    return self.json(bench_service.collect_account(bits[3],data),202)
            if sub=='works' and len(bits)==3 and method=='GET':
                return self.json(bench_service.list_works(query))
            if sub=='video' and len(bits)==3 and method=='POST':
                result=bench_service.collect_video_link(self.body())
                return self.json(result,200 if result.get('reused') else 202)
            if sub=='angles' and len(bits)==3 and method=='POST':
                return self.json(bench_service.angles_from_work(self.body()),201)
            if sub=='comments' and len(bits)==3 and method=='GET':
                return self.json(bench_service.list_comments(query))
            if sub=='topics':
                if len(bits)==3 and method=='GET':
                    return self.json(bench_service.list_topics(query))
                if len(bits)==4 and bits[3]=='from-evidence' and method=='POST':
                    return self.json({'items':bench_service.topics_from_evidence(self.body())},201)
                if len(bits)==4 and bits[3]=='from-text' and method=='POST':
                    return self.json({'items':bench_service.topics_from_text(self.body())},201)
                if len(bits)==5 and bits[4]=='start-project' and method=='POST':
                    return self.json(bench_service.start_project(bits[3]),200)
                if len(bits)==3 and method=='POST':
                    raise ValueError('请用 /api/bench/topics/from-evidence 或 /api/bench/topics/from-text。')
            if sub=='topic-from-text' and len(bits)==3 and method=='POST':
                return self.json({'items':bench_service.topics_from_text(self.body())},201)
        if len(bits)>=3 and bits[:2]==['api','matrix']:
            sub=bits[2]
            if sub=='capabilities' and len(bits)==3 and method=='GET':
                return self.json(matrix_service.platform_capabilities())
            if sub=='accounts':
                if len(bits)==3 and method=='GET':
                    return self.json(matrix_service.list_accounts())
                if len(bits)==3 and method=='POST':
                    return self.json(matrix_service.create_account(self.body()),201)
                if len(bits)==4 and method=='GET':
                    return self.json(matrix_service.get_account(bits[3]))
                if len(bits)==5 and bits[4]=='login' and method=='POST':
                    return self.json(matrix_service.start_login(bits[3]),202)
            if sub=='tasks':
                if len(bits)==3 and method=='GET':
                    return self.json(matrix_service.list_tasks(project_id=query.get('project_id',[''])[0] or None))
                if len(bits)==3 and method=='POST':
                    return self.json(matrix_service.create_tasks(self.body()),201)
                if len(bits)==4 and method=='GET':
                    return self.json(matrix_service.get_task(bits[3]))
                if len(bits)==5 and bits[4]=='plan' and method=='POST':
                    return self.json(matrix_service.plan_task(bits[3]),202)
                if len(bits)==5 and bits[4]=='submit' and method=='POST':
                    return self.json(matrix_service.submit_task(bits[3],self.body()),202)
                if len(bits)==5 and bits[4]=='publish-result' and method=='POST':
                    return self.json(matrix_service.register_publish_result(bits[3],self.body()),201)
        if path=='/api/releases/prepare' and method=='GET':
            return self.json(release_service.prepare(STORE,query.get('project_id',[''])[0]))
        # 2026-11-17（t47 多平台发布工作台）：平台能力位 / 人工引导 / 四件套文本
        if path=='/api/publish/platforms' and method=='GET':
            return self.json(publish_guide.platforms())
        if path=='/api/publish/guide' and method=='GET':
            return self.json(publish_guide.guide(query.get('platform',[''])[0]))
        if path=='/api/publish/open-page' and method=='GET':
            return self.json(publish_guide.open_page(query.get('platform',[''])[0]))
        if path=='/api/publish/kit' and method=='POST':
            body=self.body()
            prepared=release_service.prepare(STORE,body.get('project_id',''))
            return self.json(publish_guide.kit(prepared,body.get('platform','')))
        if path=='/api/releases/kit' and method=='GET':
            return self.json(release_service.kit(STORE,query.get('project_id',[''])[0]))
        if path=='/api/releases/register' and method=='POST':
            result=release_service.register(STORE,self.body())
            work=result.get('platform_work') or {}
            if work.get('id'):
                # P0-6：发布回填后登记首次采集提醒（只提醒，不代发）。
                collect_schedule.upsert_after_publish(STORE,work)
            return self.json(result,201)
        if len(bits)>=3 and bits[:2]==['api','collect']:
            sub=bits[2]
            if sub=='schedules' and len(bits)==3 and method=='GET':
                # P0-6：只提醒，不代发、不后台自动采集；到期项由本人点“现在采”。
                data=collect_schedule.list_schedules(STORE)
                data.update(collect_schedule.summary(STORE))
                return self.json(data)
            if sub=='schedules' and len(bits)==5 and bits[4]=='run' and method=='POST':
                return self.json(collect_schedule.run(STORE,bits[3]),200)
            if sub=='run-due' and len(bits)==3 and method=='POST':
                # 手动批量处理到期项；没有后台线程，仍由本人触发。
                results=[]
                for item in collect_schedule.due_items(STORE):
                    try:
                        results.append(collect_schedule.run(STORE,item['id']))
                    except Exception as exc:
                        results.append({'schedule':item,'ok':False,'message':str(exc)[:300]})
                return self.json({'items':results,'total':len(results)},200)
        if len(bits)>=3 and bits[:2]==['api','feedback']:
            sub=bits[2]
            if sub=='overview' and len(bits)==3 and method=='GET':
                return self.json(feedback_service.overview(query.get('project_id',[''])[0]))
            if sub=='works' and len(bits)==3 and method=='GET':
                return self.json(feedback_service.list_works(query.get('project_id',[''])[0]))
            if sub=='collect' and len(bits)==3 and method=='POST':
                data=self.body() if int(self.headers.get('Content-Length',0) or 0) else {}
                return self.json(feedback_service.collect(data),202)
            if sub=='register-work' and len(bits)==3 and method=='POST':
                data=self.body() if int(self.headers.get('Content-Length',0) or 0) else {}
                return self.json(feedback_service.register_work(data),201)
            if sub=='import-export' and len(bits)==3 and method=='POST':
                data=self.body()
                return self.json(feedback_import.import_export(
                    STORE,data.get('project_id',''),data.get('path',''),
                    data.get('collected_at') or None),201)
            if sub=='rate-limit' and len(bits)==3 and method=='GET':
                # P0-5 只读额度：默认 observe，只展示不拦；enforce 见 rate_limit.mode()。
                return self.json(rate_limit.precheck(
                    STORE, query.get('action',['collect'])[0],
                    query.get('account_id',[''])[0], query.get('work_id',[''])[0]))
            if sub=='snapshots' and len(bits)==3 and method=='GET':
                return self.json(feedback_service.list_snapshots(query.get('platform_work_id',[''])[0]))
            if sub=='comments' and len(bits)==3 and method=='GET':
                return self.json(feedback_service.list_comments(query))
            if sub=='classify-comments' and len(bits)==3 and method=='POST':
                data=self.body()
                return self.json(feedback_service.classify_comments(data.get('platform_work_id')),201)
            if sub=='insight' and len(bits)==3 and method=='POST':
                data=self.body()
                return self.json(feedback_service.create_insight(data.get('project_id')),201)
            if sub=='insights' and len(bits)==3 and method=='GET':
                return self.json(feedback_service.list_insights(query.get('project_id',[''])[0]))
            if sub=='insights' and len(bits)==5 and bits[4]=='start-topic' and method=='POST':
                return self.json(feedback_service.start_topic(bits[3]),201)
        # t184：作业查询已收敛到上方唯一分支（旧分支删除，避免两处语义漂移）。
        if path=='/api/publish/status' and method=='GET':return self.json(publish_status())
        if path=='/api/lan' and method=='GET':
            return self.json({'mode':LAN_MODE,'enabled':LAN_ENABLED,'token_configured':bool(LAN_TOKEN)})
        if path=='/api/lan' and method=='POST':
            if not self._client_is_loopback():
                raise PermissionError('手机访问开关只能在运行工作台的那台电脑上改。')
            data=self.body()
            if not isinstance(data.get('enabled'),bool):
                raise ValueError('缺少 enabled 布尔值。')
            LAN_ENABLED=data['enabled']
            _save_lan_enabled(LAN_ENABLED)
            return self.json({'ok':True,'enabled':LAN_ENABLED,
                              'message':'手机访问已开启，手机用局域网地址访问时需要访问码。' if LAN_ENABLED
                              else '手机访问已关闭，手机立即无法访问；电脑本机不受影响。'})
        if path=='/api/packaging/music' and method=='GET':
            return self.json(smart_packaging.music_status(DATA))
        if path=='/api/packaging/music' and method=='POST':
            if not self._client_is_loopback():
                raise PermissionError('配乐文件夹只能在运行工作台的那台电脑上选择。')
            return self.json(smart_packaging.music_set(self.body().get('folder',''),DATA))
        if path=='/api/packaging/music/pick' and method=='POST':
            if not self._client_is_loopback():
                raise PermissionError('配乐文件夹只能在运行工作台的那台电脑上选择。')
            return self.json(smart_packaging.music_pick_dialog(DATA))
        if path=='/api/access' and method=='POST':
            if not LAN_MODE or not LAN_ENABLED:
                raise PermissionError('当前未开启手机访问，不需要访问码。')
            data=self.body()
            if str(data.get('token',''))!=LAN_TOKEN:
                raise PermissionError('访问码不正确。')
            cookie=SimpleCookie();cookie['ip_token']=LAN_TOKEN
            cookie['ip_token']['path']='/';cookie['ip_token']['max-age']=31536000
            cookie['ip_token']['samesite']='Lax';cookie['ip_token']['httponly']=True
            return self.json({'ok':True,'message':'访问码正确，本机浏览器已记住。'},headers=(('Set-Cookie',cookie['ip_token'].OutputString()),))
        if path=='/api/publish/login' and method=='POST':return self.json(publish_login_job(),202)
        if path=='/api/publish/login-progress' and method=='GET':
            if PUBLISH_PROGRESS.is_file():
                try:return self.json(json.loads(PUBLISH_PROGRESS.read_text(encoding='utf-8')))
                except ValueError:pass
            return self.json({'phase':'idle','message':'登录任务尚未开始。'})
        if path=='/api/publish/plan' and method=='POST':return self.json(publish_plan_job(self.body()),202)
        if path=='/api/publish/submit' and method=='POST':return self.json(publish_submit_job(self.body()),202)
        if clip_import.route(self,method,bits,query,STORE,DATA):return
        if len(bits)==3 and bits[:2] in (['api','artifacts'],['api','assets']) and method=='GET':
            kind='artifact' if bits[1]=='artifacts' else 'asset';item=require(kind,bits[2])
            download='download' in query;filename=''
            target=Path(item['path'])
            if download:
                project=STORE.get('project',item.get('project_id') or '') or {}
                label={'clean_video':'成片','packaged_video':'成片','final_video':'成片',
                       'cover':'封面','template_frame':'预览帧'}.get(item.get('kind'),'素材')
                filename=branding.file_stem(STORE,project.get('title'),label)+target.suffix.lower()
            # 成片下载并入统一导出计次；到期放行已有成片，超次则拒绝（协议第五条第 2 款、第六条第 1 款）
            if kind=='artifact' and item.get('kind') in clip_export.VIDEO_KINDS and item.get('project_id'):
                blocked=clip_export._export_gate(item.get('project_id'),'artifact_download')
                if blocked:return self.json(blocked,403)
                try:
                    target,_=clip_export.prepare_download(target,DATA,clip_export._export_max_height(),item.get('project_id'))
                except Exception:
                    clip_export._export_cancel(item.get('project_id'),'artifact_download')
                    raise
            return self.send_file(target,download=download,filename=filename)
        if path=='/api/brain/ask' and method=='POST':
            data=self.body()
            return self.json(brain_ask.ask(STORE,data.get('question'),data.get('history')))
        # 深挖访谈：模型只决定下一问，服务端 6 条硬校验；不扣积分。
        # ===== t178（需求单 A）：240 问分阶段诊断（零 LLM；题库缺失即报错，不编题）=====
        if path.startswith('/api/diagnosis') and method=='GET':
            import diagnosis as _diag
            if path=='/api/diagnosis/questions':
                sid=(query.get('session_id') or [''])[0]
                stage=(query.get('stage') or [''])[0]
                try:
                    return self.json({'ok':True,**_diag.next_batch(STORE,sid,stage or None)})
                except _diag.StageLocked as exc:
                    return self.json({'ok':False,'code':'stage_locked','error':str(exc)},409)
            if path=='/api/diagnosis/profile':
                sid=(query.get('session_id') or [''])[0]
                return self.json({'ok':True,**_diag.profile(STORE,sid or None)})
            if path=='/api/diagnosis/state':
                sid=(query.get('session_id') or [''])[0]
                return self.json({'ok':True,**_diag.state(STORE,sid or None)})
            if path=='/api/diagnosis/bank':
                bank=_diag.load_bank()
                return self.json({'ok':True,'total':bank['total'],
                                  'families':[{'no':f['no'],'name':f['name'],'count':len(f['questions'])}
                                              for f in bank['families']]})
            raise ValueError('未知的诊断接口：'+path)
        if path=='/api/diagnosis/start' and method=='POST':
            import diagnosis as _diag
            try:
                return self.json({'ok':True,**_diag.start(STORE)},201)
            except _diag.BankError as exc:
                return self.json({'ok':False,'code':'bank_missing','error':str(exc)},503)
        if path=='/api/diagnosis/answer' and method=='POST':
            import diagnosis as _diag
            data=self.body()
            try:
                # t185：gate 题（是否有负债 / 是否有孩子…）走 answer_gate，答否自动把该族标 na。
                gate_id=str(data.get('gate_id') or '')
                if gate_id:
                    return self.json(_diag.answer_gate(STORE,str(data.get('session_id') or ''),
                                                       gate_id,data.get('text') or '',
                                                       bool(data.get('no'))))
                return self.json(_diag.answer(STORE,str(data.get('session_id') or ''),
                                              data.get('question_no'),data.get('text') or '',
                                              bool(data.get('skip')),bool(data.get('na'))))
            except _diag.StageLocked as exc:
                return self.json({'ok':False,'code':'stage_locked','error':str(exc)},409)
        # t185：卡片确认 / 修正状态位（不阻塞阶段推进）
        if path=='/api/diagnosis/confirm-card' and method=='POST':
            import diagnosis as _diag
            data=self.body()
            return self.json(_diag.confirm_card(STORE,str(data.get('session_id') or ''),
                                               data.get('stage'),str(data.get('action') or 'confirm'),
                                               data.get('corrections') or []))
        if path=='/api/diagnosis/ack-card' and method=='POST':
            import diagnosis as _diag
            data=self.body()
            return self.json(_diag.ack_card(STORE,str(data.get('session_id') or ''),data.get('stage')))
        if path=='/api/interview/state' and method=='GET':
            query=parse_qs(urlsplit(self.path).query)
            sid=(query.get('session_id') or [''])[0]
            return self.json(interview.state(STORE,sid or None))
        if path=='/api/interview/profile' and method=='GET':
            return self.json(interview.profile_summary(STORE))
        if path=='/api/interview/start' and method=='POST':
            data=self.body()
            return self.json(interview.start(STORE,data.get('theme') or '',data.get('cloud')),201)
        if path=='/api/interview/answer' and method=='POST':
            data=self.body()
            return self.json(interview.answer(STORE,str(data.get('session_id') or ''),data.get('text') or ''))
        if path=='/api/interview/close' and method=='POST':
            data=self.body()
            return self.json(interview.close(STORE,str(data.get('session_id') or '')))
        if path=='/api/interview/apply' and method=='POST':
            data=self.body()
            return self.json(interview.apply_items(STORE,data.get('pids') or []))
        if path=='/api/interview/undo' and method=='POST':
            data=self.body()
            return self.json(interview.undo_items(STORE,data.get('aids') or []))
        if path=='/api/interview/settings' and method=='POST':
            data=self.body()
            return self.json(interview.save_settings(STORE,bool(data.get('cloud',True))))
        if path=='/api/brain/news/refresh' and method=='POST':
            # 2026-11-17 t132：能力缺失降级——不再 500 抛错；前端也不在缺能力时自动触发。
            _resolver, _degraded = news_refresh_capability()
            if _degraded:
                return self.json(_degraded)
            try:
                import news_service
                body = self.body()
                custom = body.get('keywords')
                result = news_service.refresh_news(days=7, max_keywords=10, max_items=10, custom_keywords=custom)
                STORE.bump('news')  # news 走独立连接直写 records，绕过 put/patch，这里手动失效列表缓存
                return self.json(result)
            except Exception as exc:
                applog.exception('brain.news.refresh.failed', exc)
                return self.json({'ok': False, 'degraded': True,
                                  'error': '刷新失败：' + str(exc)[:120]}, 200)
        if path=='/api/brain/news' and method=='GET':
            try:
                import news_service
                items = cached_get('brain_news',('news',),lambda:news_service.list_news(limit=20))
                return self.json({'ok': True, 'items': items})
            except Exception as exc:
                applog.exception('brain.news.list.failed', exc)
                return self.json({'ok': False, 'error': '读取失败：' + str(exc)[:120]}, 500)
        # 官网/会员中心的价格唯一来源：云端 GET /api/config。
        # 页面同源只能打本机 127.0.0.1，云端 CORS 不放行 127.0.0.1，必须由本机后端代取。
        if method=='GET' and path=='/api/config':
            try:
                data,_token=auth_service._cloud('GET','/api/config')
            except Exception as exc:
                applog.exception('config.proxy.failed',exc)
                return self.json({'ok':False,'error':'云端配置暂时不可用。'},502)
            return self.json(data)
        if method=='GET' and not path.startswith('/api/'):
            rel=path.lstrip('/') or 'index.html'
            if not static_path_allowed(rel):
                return self.json({'error':'资源不可用。'},404)
            target=(ROOT/'web'/rel).resolve()
            if not target.is_relative_to((ROOT/'web').resolve()):raise PermissionError('无效文件路径。')
            return self.send_file(target)
        self.json({'error':'接口不存在。'},404)

    def send_file(self,path,download=False,filename=''):
        if not path.is_file():return self.json({'error':'产物不存在或已清理。'},404)
        st=path.stat();size=st.st_size;start=0;end=size-1;status=200
        static=not download and not self.path.startswith('/api/')
        lm=self.date_time_string(int(st.st_mtime))
        etag='"'+hashlib.sha1((str(size)+'-'+str(st.st_mtime_ns)).encode()).hexdigest()[:16]+'"'
        ver=self.path.split('?',1)[1] if '?' in self.path else ''
        suffix=path.suffix.lower()
        if static and ver and suffix in ('.js','.css'):
            # 2026-11-16：原来是 max-age=31536000, immutable。配合手写的 ?v= 版本号，
            # 只要有一次忘记 bump，浏览器就永不复核、客户一直看到旧界面，而服务端毫无异常
            # （会员中心的微信扫码入口就这样被藏了一整天）。本机回环下 304 校验成本可忽略，
            # 改成每次回源校验：内容没变回 304，变了立刻生效，这类事故不可能再发生。
            cache='no-cache'
        elif static and suffix=='.html':
            cache='no-cache'
        elif static:
            cache='public, max-age=86400'
        else:
            cache='no-store'
        not_modified=False
        if static and self.command=='GET' and not self.headers.get('Range'):
            inm=(self.headers.get('If-None-Match') or '').strip()
            ims=(self.headers.get('If-Modified-Since') or '').strip()
            not_modified=(inm==etag) or (not inm and ims==lm)
        header=self.headers.get('Range','')
        if header:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
            if not match:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
            a,b=match.groups()
            if not a and b:start=max(0,size-int(b))
            elif a:start=int(a);end=min(end,int(b)) if b else end
            if start>end or start>=size:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
            status=206
        if not_modified:
            self.send_response(304);self.send_header('ETag',etag);self.send_header('Last-Modified',lm)
            self.send_header('Cache-Control',cache);self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers();return
        self.send_response(status);self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('ETag',etag);self.send_header('Last-Modified',lm)
        self.send_header('Cache-Control',cache)
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        if download:
            name=filename or ('video.mp4' if path.suffix=='.mp4' else 'artifact'+path.suffix)
            display=re.sub(r'[^A-Za-z0-9._-]+','_',name).strip('._-') or ('download'+path.suffix.lower())
            if not display.lower().endswith(path.suffix.lower()):display+=path.suffix.lower()
            self.send_header('Content-Disposition','attachment; filename="'+display+'"; filename*=UTF-8\'\''+quote(name,safe=''))
        self.end_headers()
        with path.open('rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining:
                chunk=f.read(min(128*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)


def _port_available(host,port):
    """独占探测端口：被占返回 False。Windows 默认的 SO_REUSEADDR 会允许重复绑定，
    必须用 SO_EXCLUSIVEADDRUSE 才能问出真实占用情况。"""
    probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
            probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        probe.bind((host,port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


_SINGLE_INSTANCE_LOCK=None  # 持有数据目录锁句柄直到进程退出（OS 保证退出/崩溃即释放）


def _acquire_data_lock():
    """2026-11-17（t50 根因修复②）：数据目录单实例锁——同一 DATA(.runtime) 只许一个后端。

    08:38 事故根因：watchdog 重负载误判假死→port_fallback 起第二后端共用同一生产 DATA→
    第二进程启动恢复把活 job 全标 failed。端口独占探测只防同端口，防不了换端口双实例。
    这里用 OS 文件锁（Windows msvcrt / POSIX fcntl）：进程退出（含崩溃）锁自动释放，
    无陈旧锁误判。拿不到锁＝旧持有者还活着→立即退出、不写任何库、不动任何 job，
    活 job 在旧进程里继续跑（自动续跑）；拿到锁＝OS 确认旧持有者已死，才允许启动恢复。
    """
    global _SINGLE_INSTANCE_LOCK
    path=DATA/'.backend.lock'
    try:
        fh=open(path,'a+b')
    except OSError as exc:
        print('[lock] 数据目录锁文件打不开：%s'%exc,flush=True)
        raise SystemExit(1)
    try:
        if os.name=='nt':
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        fh.close()
        print('[lock] 该数据目录已有另一个后端在运行（同一 .runtime 只允许一个后端）；'
              '本进程退出，不修改任何任务。',flush=True)
        try:applog.info('server.single_instance.blocked',lock=str(path))
        except Exception:pass
        raise SystemExit(1)
    try:
        fh.seek(0);fh.truncate();fh.write(str(os.getpid()).encode('utf-8'));fh.flush()
    except OSError:pass
    _SINGLE_INSTANCE_LOCK=fh


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--host',default='127.0.0.1',help='绑定地址；设为 0.0.0.0 允许同一局域网内的手机访问（/api 需要访问码）')
    args=parser.parse_args()
    # 2026-11-17（t50 根因修复②）：数据目录单实例锁必须在任何写库动作之前——
    # 共用同一 DATA 的第二后端直接拒起，杜绝「双实例启动恢复误杀活 job」（08:38 视频丢失根因）。
    _acquire_data_lock()
    # 端口独占探测必须在任何写库动作之前：第二个实例绑不上端口就该直接退出，
    # 不能先把正在运行实例的 queued/running 任务标成“服务重启失败”。
    if not _port_available(args.host,args.port):
        print(f'[port] {args.port} 已被其他进程占用，退出（启动器会改用下一个端口）',flush=True)
        raise SystemExit(1)
    global LAN_MODE,LAN_TOKEN,LAN_ENABLED
    LAN_MODE=args.host not in ('127.0.0.1','localhost','::1')
    if LAN_MODE:
        LAN_ENABLED=_load_lan_enabled()
        LAN_TOKEN=os.getenv('IP_WORKBENCH_TOKEN','').strip()
        token_file=DATA/'lan-token.txt'
        if not LAN_TOKEN and token_file.is_file():
            LAN_TOKEN=token_file.read_text(encoding='utf-8').strip()
        if not LAN_TOKEN or len(LAN_TOKEN)<8:
            # 空码或过短的一律重新生成，避免手机端被弱码猜中
            LAN_TOKEN=secrets.token_urlsafe(9)
            token_file.write_text(LAN_TOKEN,encoding='utf-8')
        print(f'LAN mode enabled. Access code: {LAN_TOKEN}  (saved to .runtime/lan-token.txt)',flush=True)
        print('手机打开：%s (手机与电脑连同一 WiFi，输入上面的访问码)'%_lan_url(args.port),flush=True)
        print('手机访问开关：%s（可在 设置 → 个人档案 里关闭）'%('开启' if LAN_ENABLED else '关闭'),flush=True)
    try:
        trial_state=license.initialize()
        print(license.status_text(trial_state),flush=True)
    except Exception as exc:
        applog.exception('trial.init.failed',exc)
    # 计费接线：试用用户补一次赠送积分（幂等）；失败不影响启动与已有作品。
    try:
        seeded=billing_route.startup()
        if seeded.get('seeded'):
            print('[计费] 试用赠送积分 %s（流水 #%s）'%(seeded.get('credits'),seeded.get('ledger_id')),flush=True)
        elif seeded.get('reason'):
            print('[计费] 未发放试用积分：%s'%seeded['reason'],flush=True)
    except Exception as exc:
        applog.exception('billing.seed.failed',exc)
        print('[计费] 试用积分初始化失败，按 0 积分继续：%s'%str(exc)[:120],flush=True)
    # 直启 app.py 也做安装绑定校验：read_only（超出迁移上限）拒绝启动，
    # 指纹异常只提示“请联系客服核对”，不阻断已有作品。
    try:
        clone_state=clone_guard.check()
    except Exception as exc:
        clone_state={}
        applog.exception('clone.check.failed',exc)
        print('[安装] 绑定检查失败，按正常可用处理；详情见日志。',flush=True)
    CLONE_STATE.update(clone_state)
    if clone_state:
        print('[安装] '+str(clone_state.get('message') or ''),flush=True)
    # 2026-11-17（B7/B3）：后台复核登录态（落 72h 宽限）并换激活凭据，不阻塞启动。
    try:
        import threading as _threading
        _threading.Thread(target=lambda: auth_service.refresh_shell(STORE), daemon=True).start()
    except Exception as _exc:
        applog.exception('shell.activation.start_failed', _exc)
    # 会话复核改后台定时（启动即预热）：/api/auth/me 请求路径不再同步撞 30s×3 重试的云端判定。
    try:
        auth_service.start_session_refresher(STORE)
    except Exception as _exc:
        applog.exception('session.refresher.start_failed', _exc)
    if clone_state.get('read_only'):
        raise SystemExit(3)
    try:
        bind=license.binding()
        print('[绑定] '+str(bind.get('message') or ''),flush=True)
    except Exception as exc:
        applog.exception('license.binding.failed',exc)
        print('[绑定] 绑定校验失败，按正常可用处理；详情见日志。',flush=True)
    try:
        filled=checkin.reconcile_all(STORE,DATA)
        if filled:
            applog.info('checkin.reconcile','已补算历史漏卡',
                        profiles=[x for x in filled if x.get('created')])
    except Exception as exc:
        applog.exception('checkin.reconcile.failed',exc)
    try:
        auto=backup_restore.maybe_auto_backup(DATA)
        if auto.get('created'):
            applog.info('backup.auto','已自动备份数据库',filename=auto['filename'],
                        bytes=auto['bytes'],removed=auto['removed'])
    except Exception as exc:
        applog.exception('backup.auto.failed',exc)
    applog.info('server.start','工作台启动',port=args.port)
    # 2026-11-17（t50 根因修复③）：启动恢复只在数据目录锁已取得（＝OS 确认旧持有者已退出）
    # 之后才把中断任务标 failed；旧持有者还活着时本进程早已在锁处退出，活 job 在旧进程
    # 继续跑，不会被误杀。不再「一启动就把活 job 杀光」。
    _interrupted=0
    for job in STORE.list('job',10000):
        if job['status'] in ('queued','running'):
            _interrupted+=1
            STORE.patch('job',job['id'],{'status':'failed','stage':'服务重启，需要重新提交','error':'旧进程已确认退出（数据目录锁已取得）；原文件与历史结果保留，可重新提交。'})
    if _interrupted:
        applog.info('server.recovery.interrupted',count=_interrupted)
    try:
        st=asr_status()
        local_ok='可用' if st['local']['ready'] else '不可用'
        print('语音识别：%s（实际顺序：%s）；本地模型%s；云端：%s'%(st['mode'],
            '→'.join(st['policy']['order']),local_ok,
            '火山' if st['cloud']['volc'] else '适配器' if st['cloud']['external'] else '未配置'),flush=True)
        if not st['local']['ready'] and st['local']['problems']:
            print('  本地转写原因：'+'；'.join(st['local']['problems'][:3]),flush=True)
        prewarm=local_asr.prewarm_decision()
        if st['mode']!='cloud' and st.get('local',{}).get('ready') and prewarm['enabled']:
            print('正在后台预热语音模型：%s'%prewarm['reason'],flush=True)
            start_asr_warmup()
        elif st['mode']!='cloud' and st.get('local',{}).get('ready'):
            print('跳过启动预热：%s'%prewarm['reason'],flush=True)
    except Exception as exc:
        applog.exception('asr.status.failed',exc)
    try:
        import browser_fetch
        bs=browser_fetch.browser_status()
        print('视频链接浏览器：%s'%(bs['executable'] or '未找到'),flush=True)
        if not bs['available']:
            print('  '+bs['message'],flush=True)
    except Exception as exc:
        applog.exception('browser.status.failed',exc)
    # 链接解析：启动后真抓一篇公开文章自测，设置页状态按实测结果报（不写死「已配置」）。
    try:
        threading.Thread(target=wash_service.probe_resolver,daemon=True,
                         name='ip-resolver-probe').start()
    except Exception as exc:
        applog.exception('resolver.probe.failed',exc)
    # 同一端口只允许一个后端。Windows 上 SO_REUSEADDR 会让第二个进程也绑定成功，
    # 两个服务器轮流应答同一端口，健康检查随机拿到 500，启动器会误判“服务重启失败”。
    # 先用独占绑定探测：端口已被占就直接退出，交回启动器换下一个端口。
    if not _port_available(args.host,args.port):
        print(f'[port] {args.port} 已被其他进程占用，退出（启动器会改用下一个端口）',flush=True)
        raise SystemExit(1)
    server=ThreadingHTTPServer((args.host,args.port),Handler)
    print(f'人生重启系统 ready: http://127.0.0.1:{args.port}  (bind {args.host})',flush=True)
    print('Local single-owner edition; data stays in .runtime unless a provider is explicitly configured.',flush=True)
    # 2026-11-16（性能 E-2）：启动后先把能力探测预热掉，让第一个页面不用替它等 70ms。
    _cap_refresh()
    # t207/ccE（队长）：故障上报守护线程——本地队列统一异步上报中控。
    # env IP_ZHONGKONG_URL 空=禁用（只留本地队列，零外发）；每 60s 一轮，逐条 POST
    # /api/defects/ingest，2xx 才标 sent；任何异常只记日志，绝不影响主服务。
    _zk_url=os.environ.get('IP_ZHONGKONG_URL','').strip()
    if _zk_url:
        def _cce_flush_loop():
            import time as _t
            _tok=os.environ.get('IP_DEFECT_INGEST_TOKEN','').strip()
            while True:
                _t.sleep(60)
                try:
                    _res=fault_report.flush(_zk_url,_tok if _tok else None,timeout=5,
                        install_id=install_id.install_id(DATA))
                    # 有送达才打一行（审计用）；kept>0 说明中控暂时不通，队列保留待下轮。
                    if (_res or {}).get('sent'):
                        print('[cce] 已上报 {0} 条故障到中控（待发 {1} 条）'.format(
                            _res.get('sent'),_res.get('kept')),flush=True)
                except Exception as _e:
                    try:applog.exception('cce.flush_failed',_e)
                    except Exception:pass
        import threading as _th
        _th.Thread(target=_cce_flush_loop,daemon=True,name='cce-flush').start()
        print('[cce] 故障自动上报已启用（中控：{0}）'.format(_zk_url),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close();POOL.shutdown(wait=False,cancel_futures=True)


if __name__=='__main__':
    # 2026-11-06：直启（绕过 start.bat / boot.py）也要自愈 venv。否则 pyvenv.cfg 里
    # 仍是打包机的绝对路径，本地转写与浏览器环境换机后必挂（见踩坑与经验第 18 条）。
    try:
        import sys as _sys
        _tools=str(ROOT.parent/'tools')
        if _tools not in _sys.path:_sys.path.insert(0,_tools)
        import fix_venv as _fix_venv
        for _rel,_ok,_msg in _fix_venv.ensure_venvs():
            if _ok is False:print('[WARN] venv 自愈失败 %s：%s'%(_rel,_msg),flush=True)
    except Exception as _exc:
        print('[WARN] venv 自愈未执行：%s'%_exc,flush=True)
    main()
