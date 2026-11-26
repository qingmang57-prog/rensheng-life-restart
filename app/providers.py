"""Explicit, configurable server-side provider boundaries. Never fake AI success."""
import base64
import hashlib
import json
import math
import os
import ssl
import sys
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import video_intake
import local_asr
import asr_cache
import asr_volc
import secret_store
import cloud_gateway


class ProviderMissing(Exception):
    pass


class ProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        old,new=urllib.parse.urlsplit(req.full_url),urllib.parse.urlsplit(newurl)
        if (old.scheme,old.netloc)!=(new.scheme,new.netloc):
            raise ValueError('服务重定向到其他地址，已停止，未转发密钥。')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


ASR_MODES=('auto','local','cloud')
# auto 的实际顺序：本地优先（不耗积分），本地不可用/失败时才回落云端（按量扣用户自己的 Key 额度）。
ASR_AUTO_ORDER=('local','cloud')


def asr_policy():
    """给状态接口/启动日志用的实际策略，便于确认默认值生效。"""
    mode=asr_mode()
    return {'mode':mode,'order':list(ASR_AUTO_ORDER) if mode=='auto' else [mode]}


def asr_mode():
    """auto（默认，本地优先→失败回落云端）/ local（只用本地）/ cloud（只用云端）。
    兼容旧值：volc/external → cloud；workshop_local → local。"""
    raw=os.getenv('IP_ASR_MODE','').strip().lower()
    return {'':'auto','auto':'auto','local':'local','workshop_local':'local',
            'cloud':'cloud','volc':'cloud','external':'cloud'}.get(raw,'auto')


def external_asr():
    return bool(os.getenv('IP_ASR_URL'))


def volc_asr():
    """火山 SeedASR 直连：BYOK，凭据只从环境变量读，绝不预置。"""
    return asr_volc.key_present()


def cloud_provider():
    """实际可用的云端通道：账号未开通云端 ASR（allow_cloud_asr=false）时一律为空。"""
    if not cloud_allowed():return ''
    return cloud_config()


def cloud_allowed():
    """云端 ASR 是否被账号权益放行：只有云端明确下发 allow_cloud_asr=false 才拦。"""
    try:
        import billing_service
        return bool(billing_service.allow_cloud_asr())
    except Exception:  # noqa: BLE001
        return True


def cloud_config():
    """实际可用的云端通道：代理优先；只有回滚模式才看本机 Key / 自建适配器。"""
    if cloud_gateway.key_source()=='proxy':
        return 'proxy' if cloud_gateway.available() else ''
    if volc_asr():return 'volc'
    if external_asr():return 'external'
    return ''


# 账号权益关闭云端 ASR 时的统一口径：说清是账号未开通，不是没配 Key。
_CLOUD_BLOCKED_REASON='当前账号未开通云端语音识别（云端下发 allow_cloud_asr=false）'
_CLOUD_BLOCKED_DETAIL='这是账号权限设置，不是本机 Key 配置问题，填了 Key 也不会启用'
CLOUD_ASR_BLOCKED=(_CLOUD_BLOCKED_REASON+'，本次不会把音频上传云端。'+_CLOUD_BLOCKED_DETAIL
                   +'；要云端转写请在账号里开通该权限，或改用本地语音识别。')


def _cloud_missing_note():
    """云端不能兜底时的准确原因：账号未开通 / 本机未配置。"""
    if not cloud_allowed():
        return _CLOUD_BLOCKED_REASON+'，音频不会上传云端（'+_CLOUD_BLOCKED_DETAIL+'）'
    return '云端未配置（没有可用的 Key 或转写适配器）'


def cloud_setup_hint():
    h=asr_volc.setup_help()
    return ('云端转写还没配置。请用你自己的火山引擎账号开通「豆包语音 · 录音文件识别（大模型）」后，'
            '到语音技术控制台（%s）→ 应用管理 复制 API Key，填进 %s：\n'
            '    %s\n'
            '保存后重启工作台即可；旧版控制台是 AppID+Access Token 时再填 %s。'
            '不想用云端就把 IP_ASR_MODE 设为 local。'
            %(h['console'],h['env_file'],'；'.join(h['fields'][:2]),h['fields'][2]))


def available():
    local_ready=local_asr.available();cloud=cloud_provider();mode=asr_mode()
    configured=local_ready or bool(cloud) if mode=='auto' else (local_ready if mode=='local' else bool(cloud))
    proxy=cloud_gateway.describe()
    # 代理模式：大模型可用性只看「已登录 + 云端就绪」，不再读本机 Key；
    # 回滚模式（IP_KEY_SOURCE=local）才按旧的直连配置判断。
    llm_direct=bool(os.getenv('IP_LLM_URL') and os.getenv('IP_LLM_MODEL'))
    llm_ready=proxy['ready'] if proxy['mode']=='proxy' else llm_direct
    return {'llm':llm_ready,
            'llm_mode':proxy['mode'],
            'llm_proxy_ready':proxy['ready'],
            'asr':configured,'asr_mode':mode,'asr_ready_local':local_ready,'asr_ready_cloud':bool(cloud),
            # 账号权益是否放行云端 ASR：false 时 asr_ready_cloud 一定为 false，界面不能报“云端已就绪”。
            'asr_cloud_allowed':cloud_allowed(),
            'llm_model':os.getenv('IP_LLM_MODEL','') if proxy['mode']=='local' else 'deepseek-chat',
            # 只报是否已配置，不返回值；代理模式不读取本机 Key 文件。
            'llm_key_set':bool(secret_store.read(('IP_DEEPSEEK_KEY','IP_LLM_KEY'),'deepseek.key')[0])
                          if proxy['mode']=='local' else False,
            'asr_key_set':asr_volc.key_present() if proxy['mode']=='local' else False,
            'resolver':bool(os.getenv('IP_RESOLVER_URL'))}


def post(url, payload, key='', timeout=180):
    if not url.startswith(('https://','http://127.0.0.1:','http://localhost:')):
        raise ValueError('服务地址必须为 HTTPS 或本机地址。')
    headers={'Content-Type':'application/json'}
    if key:
        headers['Authorization']='Bearer '+key
    req=urllib.request.Request(url,json.dumps(payload,ensure_ascii=False).encode(),headers)
    try:
        # Keep TLS verification; bundled CA fixes missing Windows runtime CA roots.
        try:
            import pip._vendor.certifi as certifi
            context=ssl.create_default_context(cafile=certifi.where())
        except ImportError:context=ssl.create_default_context()
        opener=urllib.request.build_opener(ProviderRedirect(),urllib.request.HTTPSHandler(context=context))
        with opener.open(req,timeout=timeout) as res:
            raw=res.read(8*1024*1024)
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        explanation={401:'密钥无效或无权访问',402:'账户余额不足',429:'调用过于频繁，请稍后重试',503:'服务暂时繁忙'}.get(exc.code,'请检查服务地址、权限和模型配置')
        raise RuntimeError(f'服务返回 HTTP {exc.code}：{explanation}。已保存的资料和稿件仍保留。') from None
    except urllib.error.URLError:
        raise RuntimeError('无法连接已配置的服务，请检查网络和服务地址。') from None


def _parse_json_text(text):
    text=(text or '').strip()
    if text.startswith('```'):
        text=text.split('\n',1)[1].rsplit('```',1)[0]
    return json.loads(text)


def _runtime_dir():
    root = os.getenv('IP_DATA_DIR')
    return Path(root) if root else Path(__file__).resolve().parent / '.runtime'


def _caller_tag(depth=2):
    """调用点标识：文件名:行号:函数名，把用量对到具体功能。"""
    try:
        frame = sys._getframe(depth)
        return '%s:%d:%s' % (os.path.basename(frame.f_code.co_filename), frame.f_lineno,
                             frame.f_code.co_name)
    except Exception:  # noqa: BLE001
        return ''


def _log_llm_usage(caller, model, usage, elapsed_ms, mode='proxy', error='',
                   stable_sha='', stable_chars=0):
    """每次大模型调用追加一行到 .runtime/llm_usage.jsonl。

    只追加、单条一行；写入失败一律吞掉，不影响主流程。网关没回传缓存字段时
    如实记 cache=gateway_field_missing 并保留实到字段名，不估算、不补零。
    stable_sha/stable_chars：本次 per-user 稳定段指纹与字符数，用于核验
    不同功能是否共享同一段前缀（同指纹＝跨功能 warming 成立）。
    """
    try:
        usage = usage if isinstance(usage, dict) else {}
        hit = usage.get('prompt_cache_hit_tokens')
        miss = usage.get('prompt_cache_miss_tokens')
        rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'), 'call': caller, 'mode': mode,
               'model': model or '', 'prompt_tokens': usage.get('prompt_tokens'),
               'completion_tokens': usage.get('completion_tokens'),
               'hit': hit, 'miss': miss, 'ms': int(elapsed_ms)}
        if error:
            rec['error'] = error
        if stable_sha:
            rec['stable_sha'] = stable_sha
            rec['stable_chars'] = int(stable_chars or 0)
        if hit is None and miss is None:
            rec['cache'] = 'gateway_field_missing'
            rec['usage_keys'] = sorted(usage.keys())
        else:
            total = (hit or 0) + (miss or 0)
            rec['hit_ratio'] = round((hit or 0) / total, 4) if total else None
        folder = _runtime_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with open(str(folder / 'llm_usage.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:  # noqa: BLE001
        pass


def cache_telemetry(days=14):
    """per-user（本机单用户版）缓存命中遥测：按天聚合 hit/miss/hit_rate，看「越用越高」。

    只读 llm_usage.jsonl；网关未回传缓存字段的记录不参与命中率计算，不估算不补零。
    返回 {'series':[{date,calls,hit,miss,hit_rate}...], 'total':{hit,miss,hit_rate}}。
    """
    path = _runtime_dir() / 'llm_usage.jsonl'
    out = {'series': [], 'total': {'hit': 0, 'miss': 0, 'hit_rate': None}}
    rows = []
    try:
        with open(str(path), encoding='utf-8') as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    except (OSError, ValueError):
        return out
    by_day = {}
    for r in rows:
        hit, miss = r.get('hit'), r.get('miss')
        if not isinstance(hit, int) or not isinstance(miss, int):
            continue
        day = str(r.get('ts') or '')[:10]
        d = by_day.setdefault(day, {'hit': 0, 'miss': 0, 'calls': 0})
        d['hit'] += hit
        d['miss'] += miss
        d['calls'] += 1
    for day in sorted(by_day)[-max(1, int(days)):]:
        d = by_day[day]
        total = d['hit'] + d['miss']
        out['series'].append({'date': day, 'calls': d['calls'], 'hit': d['hit'], 'miss': d['miss'],
                              'hit_rate': round(d['hit'] / total, 4) if total else None})
    hit = sum(d['hit'] for d in by_day.values())
    miss = sum(d['miss'] for d in by_day.values())
    out['total'] = {'hit': hit, 'miss': miss,
                    'hit_rate': round(hit / (hit + miss), 4) if (hit + miss) else None}
    return out


# ---------------------------------------------------------------------------
# 2026-11-17 t34：全局稳定缓存前缀（所有功能共用、字节级恒定）。
# DeepSeek 前缀缓存按 token 前缀完全匹配；把恒定 system 段放 messages[0]，
# 任何功能从第二次请求起都能命中这一段（跨功能共享）。禁止写入时间戳/随机 ID/变量。
# ---------------------------------------------------------------------------
GLOBAL_CACHE_PREFIX = (
    '你是「人生重启系统」的写作与教练引擎。以下为全局铁律，任何功能都不得违反：\n'
    '1. 只输出一个合法的 JSON 对象（不输出 Markdown 代码块、前后缀或解释）。\n'
    '2. 不编造来源、数据、身份、经历、客户故事或评论；未知内容标注待补/待验证。\n'
    '3. 禁用绝对化与夸大字眼（最、第一、100%、根治、永久、保证、必火、全网）。\n'
    '4. 做教练时只谈工作与行动，不做心理诊断、不替本人下人生结论；赚钱是正当目标。\n'
    '5. 尊重输入里的红线与禁忌；不把参考观点当成本人亲身验证。'
)


def stable_json(value):
    """稳定序列化：键排序＋无多余空白，同一内容每次字节完全一致（缓存前缀友好）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))



# 稳定段装配的单一入口：空段用常量占位，消息序固定四段，不出现位次漂移。
STABLE_EMPTY_PLACEHOLDER = '（本人暂无档案、命题与已验证积累）'
_BLOCK_SEP = '\n\n'


def stable_section(stable_prefix=None, stable_blocks=None):
    """稳定段唯一装配点：stable_prefix + 有序 stable_blocks，去空后按序连接。

    同一用户的规范 per-user 段必须逐字一致（跨功能 warming 的前提）：所有功能都经
    creation_service.stable_blocks_for 取段，不各自拼字符串。契约序由
    creation_service.user_stable_prefix 维护：档案→口吻→命题+主线→append-only 积累
    （段尾；新增卡只延长末尾、不掀冷前面字节）；本函数只按调用方给的顺序拼接，不重排。
    只允许放稳定内容；时间戳/随机 ID/本次 query 等变量一律留在最后的 user payload。
    """
    if isinstance(stable_prefix, (list, tuple)):
        raise TypeError('stable_prefix 只接受字符串；有序多段请用 stable_blocks=')
    parts = []
    for block in ([stable_prefix] + list(stable_blocks or [])):
        text = str(block or '').strip()
        if text:
            parts.append(text)
    return _BLOCK_SEP.join(parts)


def stable_text(stable_prefix=None, stable_blocks=None):
    """稳定段实际下发文本：没有稳定内容时用常量占位，保证消息序与字节恒定。"""
    return stable_section(stable_prefix, stable_blocks) or STABLE_EMPTY_PLACEHOLDER


def stable_fingerprint(stable_prefix=None, stable_blocks=None):
    """稳定段指纹（sha256 前 16 位）：跨功能一致＝跨功能 warming 的可核验证据。"""
    text = stable_section(stable_prefix, stable_blocks)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16] if text else ''


def _cache_messages(system, payload, stable_prefix=None, stable_blocks=None, stable=None):
    """按缓存顺序组装 messages：[全局前缀] → [per-user 稳定段] → [功能 system] → [变量 user]。

    t34 flywheel／t1 契约：稳定段提到功能模板之前——同一用户的规范稳定段跨功能字节一致，
    任一功能暖过的前缀其它功能直接命中；积累 append-only 增长时旧前缀字节不变保持命中，
    新增部分延长前缀（越用越高）。变量永远只在最后的 user 段，且只有这一条是变量，
    消息位次固定为 4 段（便于静态核验）。
    """
    if stable is None:
        stable = stable_text(stable_prefix, stable_blocks)
    return [
        {'role': 'system', 'content': GLOBAL_CACHE_PREFIX},
        {'role': 'system', 'content': stable},
        {'role': 'system', 'content': system + '\n仅输出有效 JSON 对象。'},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)},
    ]


def llm_json(system, payload, stable_prefix=None, stable_blocks=None, tag=None):
    """单次 JSON 补全。

    stable_prefix：跨请求完全稳定的长文本（档案/三观/风格卡）。与 stable_blocks
    一起放在全局前缀之后、功能 system 之前（per-user 稳定段跨功能字节一致，
    任一功能暖过的前缀其它功能直接命中）；动态内容仍在最后 user 段。
    stable_blocks：有序的 per-user/功能稳定块（persona、principles、catalog 等），
    按给定顺序拼接进同一稳定 system 段（保持字节恒定）。
    tag：用量日志里的调用点标识；分段分析传 'analyze_write:seg3/7' 这类值，
    便于在 llm_usage.jsonl 里一眼看出是哪一段或合并。
    """
    stable = stable_text(stable_prefix, stable_blocks)
    messages = _cache_messages(system, payload, stable=stable)
    stable_sha = hashlib.sha256(stable.encode('utf-8')).hexdigest()[:16]
    # 代理模式：不读本机 Key；令牌由 cloud_gateway 从本地会话表取，只进内存。
    if cloud_gateway.key_source()=='proxy':
        started=time.perf_counter()
        try:
            data=cloud_gateway.llm_chat(messages,json_mode=True,max_tokens=8000)
        except cloud_gateway.ProxyError as exc:
            _log_llm_usage(tag or _caller_tag(2),'',{},(time.perf_counter()-started)*1000,
                           'proxy',getattr(exc,'code','') or 'proxy_error',
                           stable_sha=stable_sha,stable_chars=len(stable))
            raise RuntimeError(str(exc)) from None
        _log_llm_usage(tag or _caller_tag(2),data.get('model'),data.get('usage'),
                       (time.perf_counter()-started)*1000,'proxy',
                       stable_sha=stable_sha,stable_chars=len(stable))
        try:
            return _parse_json_text(data.get('content'))
        except (TypeError,ValueError):
            raise RuntimeError('模型没有返回可校验的 JSON；原稿未被覆盖。') from None
    if not available()['llm']:
        raise ProviderMissing('生成模型尚未配置。可以先编辑稿件；配置 IP_LLM_URL、IP_LLM_MODEL 后启用真实生成。')
    request={
        'model':os.environ['IP_LLM_MODEL'], 'temperature':0.5,
        'messages':messages,
        'max_tokens':8000,'response_format':{'type':'json_object'}
    }
    if urllib.parse.urlsplit(os.environ['IP_LLM_URL']).hostname=='api.deepseek.com':
        request['thinking']={'type':'disabled'}
    # 密钥来源：进程环境变量 → 包外 .secrets/deepseek.key → 本机配置；绝不打印内容。
    key, _source = secret_store.read(('IP_DEEPSEEK_KEY', 'IP_LLM_KEY'), 'deepseek.key')
    started=time.perf_counter()
    response=post(os.environ['IP_LLM_URL'],request,key,timeout=300)
    _log_llm_usage(tag or _caller_tag(2),os.environ.get('IP_LLM_MODEL',''),response.get('usage'),
                   (time.perf_counter()-started)*1000,'local',
                   stable_sha=stable_sha,stable_chars=len(stable))
    try:
        text=response['choices'][0]['message']['content'].strip()
        if text.startswith('```'):
            text=text.split('\n',1)[1].rsplit('```',1)[0]
        return json.loads(text)
    except (KeyError,IndexError,TypeError,ValueError):
        raise RuntimeError('模型没有返回可校验的 JSON；原稿未被覆盖。') from None


def resolve_source(url):
    if os.environ.get('IP_RESOLVER_URL'):
        result=post(os.environ['IP_RESOLVER_URL'],{'url':url},os.getenv('IP_RESOLVER_KEY',''))
        return video_intake.normalize(result,video_intake.share_info(url),'configured_resolver')
    # 2026-11-17 t67：抖音先走移动 Feed 免签名快路径（~1 秒、去水印、原生字幕），
    # 失败再退回浏览器（stealth/会话复用），最后公开页。
    fast_error=''
    try:
        return video_intake.resolve_share(url)
    except ValueError as exc:
        fast_error=str(exc)[:160]
    try:
        return video_intake.browser_resolve(url)
    except Exception as exc:
        fallback=video_intake.public_resolve(url)
        note=''
        if fast_error:note+='（快路径未成功：'+fast_error+'；'
        fallback['message']=(fallback.get('message','') or '')+note+f'本机浏览器解析未成功：{str(exc)[:180]}）'
        return fallback


def _via_local(audio_path,stage):
    """本地优先路径：先查内容指纹缓存，未命中才真转写，成功后写回缓存。"""
    tag=local_asr.cache_tag()
    cached=asr_cache.get(audio_path,tag)
    if cached:
        cues=video_intake.cues_valid(cached.get('cues',[]))
        words=video_intake.cues_valid(cached.get('words',[]),'word')
        if cues and str(cached.get('text','')).strip():
            if stage:stage('命中本地转写缓存，直接复用上次结果')
            return {**cached,'cues':cues,'words':words,'asr_route':'local_cache',
                    'asr_route_note':'本机缓存命中：这段音频之前转过（%s），本次没有重新识别、未联网'
                                     %(cached.get('cache_created_at') or '时间未知')}
    result=local_asr.transcribe(audio_path,stage=stage)
    cues=video_intake.cues_valid(result.get('cues',[]))
    words=video_intake.cues_valid(result.get('words',[]),'word')
    if not cues or not result.get('text','').strip():raise ValueError('本地转写没有取得可用字幕。')
    note='本机 GPU 模型，未联网' if result.get('device')=='cuda' else '本机模型，未联网'
    if result.get('gpu_fallback'):note+='；'+str(result['gpu_fallback'])
    if result.get('speed_warning'):note+='；'+str(result['speed_warning'])
    out={**result,'cues':cues,'words':words,'asr_route':'local','asr_route_note':note}
    asr_cache.put(audio_path,tag,out)
    return out


def _via_volc(audio_path,stage):
    if stage:stage('云端（火山）转写中')
    try:
        result=asr_volc.transcribe(audio_path)
    except asr_volc.VolcAsrError:
        raise
    except Exception as exc:
        # 云端失败一律转成中文说明，不把原始异常抛到界面。
        raise ValueError('云端转写失败（%s）；本次没有取得字幕。' % (str(exc).strip()[:200] or '未知错误')) from None
    # 2026-11-18（G4/t21）：云端结果与本地链走同一纠错链（LLM 纠错+等长回写 words）。
    # 热词口径与本地一致（local_asr._profile_hotwords 内部自载，失败空表降级）；
    # 纠错失败只记日志、放行未纠错结果，不阻断转写（与 local_asr L840-845 同型兜底）。
    try:
        import asr_postprocess
        result,_pp=asr_postprocess.apply_to_transcript(result,hotwords=local_asr._profile_hotwords())
    except Exception as exc:
        import applog
        applog.exception('asr.postprocess.volc.failed',exc)
    cues=video_intake.cues_valid(result.get('cues',[]))
    words=video_intake.cues_valid(result.get('words',[]),'word')
    if not words or not result.get('text','').strip():raise ValueError('火山转写没有取得可用字幕。')
    return {**result,'cues':cues,'words':words,'asr_route':'cloud_volc',
            'asr_route_note':'云端：火山豆包语音录音文件识别（大模型），使用你自己的 Key'}


def _via_external(audio_path):
    raw=audio_path.read_bytes()
    if len(raw)>8*1024*1024:
        raise ValueError('当前转写适配器音频上限为 8MB，请分段或使用文件URL适配器。')
    result=post(os.environ['IP_ASR_URL'],{'audio_base64':base64.b64encode(raw).decode(),
                'format':'mp3','preserve_fillers':True,'timestamps':'word'},os.getenv('IP_ASR_KEY',''),timeout=300)
    words=result.get('words')
    if not isinstance(words,list):
        raise ValueError('ASR 适配器必须返回 words 数组。')
    clean=[]
    for i,w in enumerate(words):
        start,end=float(w['start_ms']),float(w['end_ms'])
        if not math.isfinite(start) or not math.isfinite(end) or start<0 or end<=start:
            raise ValueError('ASR 返回无效时间区间，未执行剪辑。')
        clean.append({'id':f'word_{i}','text':str(w['text']), 'start_ms':start,'end_ms':end})
    return {'text':str(result.get('text','')),'words':clean,'cues':video_intake.cues_valid(result.get('cues',[])),
            'method':'configured_asr','asr_route':'cloud_external','asr_route_note':'云端：自建转写适配器'}


def _via_proxy(audio_path,stage):
    """代理模式：音频只上传到自己的云端，由云端用服务器 Key 转写；本机不持有上游 Key。"""
    raw=audio_path.read_bytes()
    try:
        result=cloud_gateway.asr_transcribe(raw,fmt=(audio_path.suffix.lstrip('.') or 'mp3').lower(),
                                            duration_sec=_audio_seconds(audio_path) or None)
    except cloud_gateway.ProxyError as exc:
        raise ProviderMissing(str(exc)) from None
    clean=[]
    for i,w in enumerate(result.get('words') or []):
        start,end=float(w.get('start_ms') or 0),float(w.get('end_ms') or 0)
        if not math.isfinite(start) or not math.isfinite(end) or start<0 or end<=start:continue
        clean.append({'id':f'word_{i}','text':str(w.get('text') or ''),'start_ms':start,'end_ms':end})
    if not clean:
        raise ProviderMissing('云端转写没有返回可用的词级时间，未执行剪辑；请改用本地转写。')
    return {'text':str(result.get('text','')),'words':clean,
            'cues':video_intake.cues_valid(result.get('cues') or []),
            'method':'cloud_proxy','asr_route':'cloud_proxy',
            'asr_route_note':'云端代理：本机不持有上游 Key'}


def _via_cloud(audio_path,stage):
    if not cloud_allowed():raise ProviderMissing(CLOUD_ASR_BLOCKED)
    provider=cloud_provider()
    if provider=='proxy':return _via_proxy(audio_path,stage)
    if provider=='volc':return _via_volc(audio_path,stage)
    if provider=='external':return _via_external(audio_path)
    raise ProviderMissing(cloud_setup_hint())


def _reason(exc):
    if isinstance(exc,local_asr.LocalAsrTimeout):return '本地语音识别加载超时'
    text=str(exc).strip()
    return '本地语音识别失败'+(('（'+text.splitlines()[0][:120]+'）') if text else '')


def _audio_seconds(audio_path):
    """取音频时长（秒）。ASR 输入是纯音频，media.probe 只认视频，这里直接用 ffprobe。"""
    try:
        import media
        exe=media.binary('ffprobe')
        if not exe:return 0.0
        obj=json.loads(media.run([exe,'-v','error','-show_format','-of','json',str(audio_path)],60).stdout)
        return max(0.0,float((obj.get('format') or {}).get('duration') or 0))
    except Exception:
        return 0.0


def _cloud_cost_note(audio_path):
    """2026-11-10（试用反馈 2a）：回落云端前说清本次预计消耗，不打断流程。

    单价只认用户自己配置的 IP_CLOUD_ASR_PRICE_PER_MIN（元/分钟）；没配就只报分钟数，
    绝不编造金额，实际以火山控制台账单为准。
    """
    if cloud_gateway.key_source()=='proxy':
        return '本次使用云端转写：由平台提供，不消耗你本机的火山额度。'
    seconds=_audio_seconds(audio_path)
    if seconds<=0:
        return '本次预计消耗：按音频时长计费，单价与金额以火山控制台账单为准。'
    head='本次预计消耗：音频约 %.1f 分钟' % (seconds/60.0)
    price=(os.getenv('IP_CLOUD_ASR_PRICE_PER_MIN','') or '').strip()
    if not price:
        return head+'；单价未配置（IP_CLOUD_ASR_PRICE_PER_MIN，元/分钟），实际以火山控制台账单为准。'
    try:
        return head+' × ¥%s/分钟 ≈ ¥%.2f（以火山控制台账单为准）。' % (price,seconds/60.0*float(price))
    except ValueError:
        return head+'；单价配置无效（IP_CLOUD_ASR_PRICE_PER_MIN=%s），实际以火山控制台账单为准。' % price[:20]


# 2026-11-16 自动兜底：用户要求「本地实在转不动才上云，不再弹窗询问」，所以条件必须
# 写死、可审计、能在界面和日志里对上账。三条触发条件：
#   ① 本地不可用（缺模型/缺内存/被关闭）→ 直接云端；
#   ② 本地超时（启动/加载/转写停滞，阈值见 local_asr 的 IP_ASR_*_TIMEOUT）；
#   ③ 连续失败达 CIRCUIT_FAILS 次 → 暂停本地 CIRCUIT_COOLDOWN 秒，期间直接走云端。
CIRCUIT_FAILS=int(os.getenv('IP_ASR_CIRCUIT_FAILS','2'))
CIRCUIT_COOLDOWN=int(os.getenv('IP_ASR_CIRCUIT_COOLDOWN','600'))
_LOCAL_CIRCUIT={'fails':0,'open_until':0.0,'last_reason':'','last_at':0.0}


def fallback_policy():
    """兜底条件快照：给界面和日志看，别让用户猜什么时候会被切到云端。"""
    return {'mode':asr_mode(),'order':list(ASR_AUTO_ORDER),
            'startup_timeout_seconds':int(os.getenv('IP_ASR_STARTUP_TIMEOUT','600')),
            'stall_timeout_seconds':int(os.getenv('IP_ASR_STALL_TIMEOUT','900')),
            'circuit_fails':CIRCUIT_FAILS,'circuit_cooldown_seconds':CIRCUIT_COOLDOWN,
            'slow_factor':float(os.getenv('IP_ASR_SLOW_FACTOR','2')),
            'circuit_open':time.monotonic()<_LOCAL_CIRCUIT['open_until'],
            'circuit_reason':_LOCAL_CIRCUIT['last_reason']}


def _local_failure(reason):
    state=_LOCAL_CIRCUIT
    state['fails']+=1;state['last_at']=time.monotonic();state['last_reason']=reason[:200]
    if state['fails']>=CIRCUIT_FAILS:
        state['open_until']=time.monotonic()+CIRCUIT_COOLDOWN
    return state


def _local_success():
    _LOCAL_CIRCUIT.update(fails=0,open_until=0.0,last_reason='')


def _audit_fallback(audio_path,fallback,result_ok):
    """把「为什么用了云端」写进日志，含音频时长与当时阈值，事后能复算。"""
    try:
        import applog
        applog.write('warn' if fallback else 'info','asr.fallback',
            '本地转写未完成，已自动切云端' if fallback else '本次走本地转写',
            reason_code=fallback.get('reason_code'),reason=(fallback.get('reason') or '')[:300],
            audio_seconds=round(_audio_seconds(audio_path),1),cloud_ok=bool(result_ok),
            policy=fallback_policy())
    except Exception:
        pass


def asr(audio_path,stage=None):
    """按 IP_ASR_MODE 选通道；auto 时本地优先（不耗积分），本地失败/不可用才回落云端。

    2026-11-14：云端路径统一受账号权益 allow_cloud_asr 控制；关闭时明确说“账号未开通”，
    不再把它混成“未配置”，也不会把音频上传云端。
    2026-11-16：本地转不动时自动上云、不再询问；原因与条件写进结果字段和日志。
    """
    mode=asr_mode();notify=stage or (lambda _m:None)
    if mode=='local':
        if not local_asr.available():
            raise ProviderMissing('本地语音识别不可用：'+'；'.join(local_asr.preflight()['problems'])
                +'。可以把 IP_ASR_MODE 设为 auto 或 cloud 改用云端。')
        return _via_local(audio_path,stage)
    if mode=='cloud':
        if not cloud_allowed():raise ProviderMissing(CLOUD_ASR_BLOCKED)
        if not cloud_config():raise ProviderMissing(cloud_setup_hint())
        return _via_cloud(audio_path,stage)
    # auto：本地优先，本地失败/不可用 → 云端兜底（云端按量扣用户自己的 Key 额度）
    local_error='';fallback={}
    if local_asr.available() and time.monotonic()<_LOCAL_CIRCUIT['open_until']:
        left=max(0,int(_LOCAL_CIRCUIT['open_until']-time.monotonic()))
        local_error=('本地连续失败 %d 次，已按兜底规则暂停本地通道 %d 秒（还剩 %d 秒）'
                     %(_LOCAL_CIRCUIT['fails'],CIRCUIT_COOLDOWN,left))
        fallback={'reason_code':'circuit_open','reason':local_error}
        if cloud_provider():
            notify('本地通道已按兜底规则暂停（'+local_error+'），本次直接用云端识别（会消耗云端额度）。'
                   +_cloud_cost_note(audio_path))
    elif local_asr.available():
        try:
            result=_via_local(audio_path,stage)
            _local_success()
            _audit_fallback(audio_path,{},True)
            return result
        except Exception as exc:
            local_error=str(exc).strip().splitlines()[0][:200] if str(exc).strip() else '未知错误'
            state=_local_failure(local_error)
            if not cloud_provider():
                raise RuntimeError('本地语音识别失败（'+local_error+'），且'+_cloud_missing_note()
                    +'，本次没有取得字幕。下一步：检查本机模型，或稍后重试；原始素材与稿件都保留。') from None
            fallback={'reason_code':'local_timeout' if isinstance(exc,local_asr.LocalAsrTimeout) else 'local_error',
                      'reason':local_error,'fails':state['fails'],
                      'circuit_open':time.monotonic()<state['open_until']}
            notify('本地识别失败（'+local_error+'），已自动切换到云端识别（会消耗云端额度）。'+_cloud_cost_note(audio_path))
    else:
        local_error='本地不可用：'+'；'.join(str(x) for x in (local_asr.preflight().get('problems') or []))[:200]
        fallback={'reason_code':'local_unavailable','reason':local_error}
        if not cloud_provider():
            raise ProviderMissing('本地语音识别不可用（'
                +'；'.join(local_asr.preflight()['problems'])[:300]+'），'+_cloud_missing_note()
                +'，本次没有取得字幕。下一步：恢复本机模型，或稍后再试；原始素材与稿件都保留。')
        notify('本地语音识别不可用，已自动切换到云端识别（会消耗云端额度）。'+_cloud_cost_note(audio_path))
    try:
        cloud_result=_via_cloud(audio_path,stage)
    except Exception as exc:
        _audit_fallback(audio_path,fallback,False)
        raise RuntimeError('本地和云端语音识别都没成功。本地：'+local_error
            +'；云端：'+str(exc).strip()[:400]
            +'。下一步：检查本机模型或稍后重试；原始素材与稿件都保留。') from None
    _audit_fallback(audio_path,fallback,True)
    result=dict(cloud_result)
    result['asr_fallback']={'reason_code':fallback.get('reason_code'),
                            'reason':fallback.get('reason'),
                            'conditions':fallback_policy()}
    result['asr_route_note']=(str(result.get('asr_route_note') or '云端转写')
        +'；本次为云端识别（%s），会消耗云端额度'%(fallback.get('reason_code') or '本地不可用'))
    return result


def asr_route_label():
    """阶段文案：按当前模式与账号权益如实说明音频会不会上传云端。"""
    mode=asr_mode();local_ready=local_asr.available();cloud_ready=bool(cloud_provider())
    if mode=='local' and local_ready:return '本机语音识别中（模型在包内，音频不上传云端）'
    if mode=='cloud' and cloud_ready:return '云端语音识别中（音频会上传云端）'
    if mode=='auto':
        if local_ready:
            # 云端被账号权益关闭/未配置时不会回落，别写成“失败会切云端”。
            return ('本机语音识别中（模型在包内，音频不上传云端）' if not cloud_ready
                    else '本机语音识别中（本地优先；本地失败会自动切云端，届时音频会上传）')
        if cloud_ready:return '云端语音识别中（本机不可用，音频会上传云端）'
    return '语音识别不可用'


def asr_unavailable_reason():
    """转写入口被拦时的准确原因：本地缺什么（模型/内存）、云端为何不能用（未开通/未配置）。"""
    mode=asr_mode();local=local_asr.preflight()
    problems=[str(x) for x in (local.get('problems') or [])]
    if not problems and os.getenv('IP_DISABLE_LOCAL_ASR')=='1':
        problems=['本地语音识别已被关闭（IP_DISABLE_LOCAL_ASR=1）']
    local_note='；'.join(problems or ['原因未记录'])
    if mode=='local':
        return ('本机语音识别不可用（'+local_note+'）；当前设置为只用本地，不会走云端。'
                '可把语音识别通道改成 auto 或 cloud。')
    if mode=='cloud':
        if not cloud_allowed():return CLOUD_ASR_BLOCKED
        return '云端语音识别未配置（没有可用的 Key 或转写适配器）。'+cloud_setup_hint()
    if not cloud_allowed():
        return '本机语音识别不可用（'+local_note+'）；'+CLOUD_ASR_BLOCKED
    return '本机语音识别不可用（'+local_note+'）；云端也未配置（没有可用的 Key 或转写适配器）。'

