"""Public-page intake and evidence-based normalization. No cookie harvesting or challenge solving."""
import html
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import douyin_session

ROOT = Path(__file__).resolve().parent
MEDIA_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MAX_MEDIA_BYTES = 2 * 1024 * 1024 * 1024

METRICS={'likes':'点赞','favorites':'收藏','shares':'转发','comments':'评论','views':'播放'}
PLATFORM_HOSTS=('douyin.com','iesdouyin.com','weixin.qq.com','channels.weixin.qq.com')


def timestamp():return datetime.now(timezone.utc).isoformat()


def allowed_url(url):
    p=urlsplit(url);host=(p.hostname or '').lower()
    return p.scheme=='https' and not p.username and not p.password and p.port in (None,443) and any(host==h or host.endswith('.'+h) for h in PLATFORM_HOSTS)


def share_info(text):
    match=re.search(r'https?://[^\s<>\[\]"\u3000]+',str(text))
    url=match.group(0).rstrip('。，,；;！!）)】}') if match else ''
    if url and not allowed_url(url):raise ValueError('当前链接入口支持抖音和视频号的 HTTPS 分享链接。其他资料可手动新建作品。')
    p=urlsplit(url);query=parse_qs(p.query);host=p.hostname or ''
    platform='douyin' if 'douyin.com' in host else 'wechat_channels' if 'weixin.qq.com' in host else 'unknown'
    video_id=''
    if platform=='douyin':
        m=re.search(r'/(?:share/)?video/(\d+)',p.path)
        video_id=m.group(1) if m else (query.get('modal_id') or query.get('vid') or [''])[0]
        if video_id and not re.fullmatch(r'\d{8,30}',video_id):raise ValueError('视频编号格式无效。')
    return {'url':url,'canonical_url':'https://www.douyin.com/video/'+video_id if video_id else url,'platform':platform,'video_id':video_id,'share_text':str(text)[:12000]}


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if not allowed_url(newurl):raise ValueError('平台跳转目标不受支持，已停止读取。')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def public_page(url):
    if not allowed_url(url):raise ValueError('链接目标不受支持。')
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'text/html,application/json'})
    try:
        with urllib.request.build_opener(PublicRedirect()).open(request,timeout=25) as r:
            raw=r.read(6*1024*1024+1)
            if len(raw)>6*1024*1024:raise ValueError('页面过大，已停止读取。')
            return raw.decode('utf-8','replace'),r.url
    except urllib.error.HTTPError as e:
        if e.code in (401,403,412,429):raise ValueError('平台要求登录、验证或限制访问，未绕过校验。') from None
        raise ValueError(f'平台页面返回 HTTP {e.code}。') from None
    except (urllib.error.URLError,TimeoutError):raise ValueError('暂时无法连接平台页面，链接已保留。') from None


class PageScripts(HTMLParser):
    def __init__(self):super().__init__();self.current=None;self.blocks=[]
    def handle_starttag(self,tag,attrs):
        if tag=='script':self.current={'attrs':dict(attrs),'data':''}
    def handle_data(self,data):
        if self.current is not None:self.current['data']+=data
    def handle_endtag(self,tag):
        if tag=='script' and self.current is not None:self.blocks.append(self.current);self.current=None


def embedded_video(page,video_id):
    parser=PageScripts();parser.feed(page);roots=[]
    for b in parser.blocks:
        raw=b['data'].strip();attrs=b['attrs']
        if attrs.get('id')=='RENDER_DATA':raw=unquote(raw)
        prefix=re.match(r'\s*(?:window\.)?(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*',raw)
        if prefix:raw=raw[prefix.end():]
        if raw.startswith(('{','[')):
            try:roots.append(json.JSONDecoder().raw_decode(raw)[0])
            except (ValueError,RecursionError):continue
    queue=roots;seen=0
    while queue and seen<50000:
        obj=queue.pop();seen+=1
        if isinstance(obj,dict):
            key=str(obj.get('aweme_id',obj.get('awemeId','')))
            if key==video_id and (obj.get('video') or obj.get('desc') is not None):return obj
            queue.extend(v for v in obj.values() if isinstance(v,(dict,list)))
        elif isinstance(obj,list):queue.extend(v for v in obj if isinstance(v,(dict,list)))
    return None


def metric(value):
    if value is None or value=='':return None
    if isinstance(value,bool):return None
    if isinstance(value,int) and value>=0:return value
    if isinstance(value,str) and re.fullmatch(r'\d+',value):return int(value)
    # Rounded display strings remain rounded; never manufacture precise totals.
    if isinstance(value,str) and re.fullmatch(r'\d+(?:\.\d+)?[万亿wWkK]',value):return value
    return None


def cues_valid(rows,kind='cue'):
    if not isinstance(rows,list) or len(rows)>20000:raise ValueError('字幕时间轴必须是有效数组。')
    out=[];ids=set()
    for i,row in enumerate(rows):
        if not isinstance(row,dict) or not isinstance(row.get('text'),str):raise ValueError('字幕缺少正文。')
        a,b=float(row['start_ms']),float(row['end_ms'])
        if not math.isfinite(a) or not math.isfinite(b) or a<0 or b<=a or b>86400000:raise ValueError('字幕时间戳无效，不能分析前2秒。')
        key=str(row.get('id') or f'{kind}_{i+1}')[:100]
        if key in ids:raise ValueError('字幕片段编号重复。')
        ids.add(key);out.append({'id':key,'text':row['text'][:12000],'start_ms':round(a),'end_ms':round(b)})
    return sorted(out,key=lambda x:(x['start_ms'],x['end_ms']))


def opening_window(cues,words):
    rows=words or cues
    hits=[{**r,'crosses_end':r['end_ms']>2000} for r in rows if r['start_ms']<2000 and r['end_ms']>0]
    opening_text=''.join((' ' if i and (not words or re.search(r'[a-zA-Z0-9]$',hits[i-1]['text']) and re.match(r'[a-zA-Z0-9]',r['text'])) else '')+r['text'] for i,r in enumerate(hits))
    return {'start_ms':0,'end_ms':2000,'text':opening_text,'segments':hits,
            'precision':'word' if words else 'cue_overlap' if hits else 'unavailable',
            'note':('词级时间戳；跨越2秒的词已标出，仍需播放核对。' if words else '句级字幕可能跨越2秒，显示关联句，不能当作前2秒逐字精确稿。') if hits else '尚无带时间轴的字幕，不能把第一句话猜成前2秒。'}


def normalize(payload,info,method):
    if not isinstance(payload,dict):raise ValueError('解析服务返回格式无效。')
    cues=cues_valid(payload.get('cues',[]));words=cues_valid(payload.get('words',[]),'word')
    text=payload.get('transcript','')
    if not isinstance(text,str):raise ValueError('解析服务 transcript 必须是文字。')
    if not text:text='\n'.join(x['text'] for x in (cues or words))
    meta=payload.get('metadata') or {}
    if not isinstance(meta,dict):raise ValueError('视频资料格式无效。')
    metrics=meta.get('metrics') or {}
    if not isinstance(metrics,dict):raise ValueError('互动数据格式无效。')
    clean_meta={'platform':info['platform'],'video_id':str(meta.get('video_id') or info['video_id']),
        'title':str(meta.get('title') or payload.get('title') or '')[:500],'author':str(meta.get('author') or '')[:200],
        'published_at':str(meta.get('published_at') or '')[:80],
        'duration_ms':meta.get('duration_ms') if isinstance(meta.get('duration_ms'),(int,float)) and math.isfinite(meta['duration_ms']) and meta['duration_ms']>0 else None,
        'metrics':{name:metric(metrics.get(name)) for name in METRICS},'collected_at':timestamp(),'method':method}
    status='ready' if text.strip() else 'partial' if clean_meta['title'] or any(v is not None for v in clean_meta['metrics'].values()) else 'blocked'
    message=str(payload.get('message') or ('字幕已取得。' if status=='ready' else '已取得部分视频信息，字幕待取得。' if status=='partial' else '未取得视频正文或互动数据，需要可用的解析服务或正常授权。'))[:1200]
    return {'status':status,'transcript':text[:120000],'metadata':clean_meta,'cues':cues,'words':words,
            'opening':opening_window(cues,words),'message':message,'canonical_url':info['canonical_url'],'analysis':None}


def _count_or_dash(value):
    """互动数：只认真实数字；字段缺失/None 一律标「—」，不估算、不补零。"""
    if isinstance(value, bool):
        return '—'
    if isinstance(value, (int, float)):
        try:
            if math.isfinite(float(value)) and float(value) >= 0:
                return int(value)
        except (TypeError, ValueError):
            return '—'
    return '—'


def interactions_of(raw):
    """从 detail 的 statistics 提取 赞/评/藏/转（+播放）；缺失标 —。"""
    stats = (raw or {}).get('statistics') if isinstance(raw, dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    return {'likes': _count_or_dash(stats.get('digg_count')),
            'comments': _count_or_dash(stats.get('comment_count')),
            'favorites': _count_or_dash(stats.get('collect_count')),
            'shares': _count_or_dash(stats.get('share_count')),
            'views': _count_or_dash(stats.get('play_count'))}


def from_douyin(raw,info):
    stats=raw.get('statistics') or {};author=raw.get('author') or {};video=raw.get('video') or {}
    created=raw.get('create_time');published=''
    if isinstance(created,(int,float)):
        try:published=datetime.fromtimestamp(created,timezone.utc).isoformat()
        except (ValueError,OverflowError,OSError):pass
    result=normalize({'metadata':{'title':raw.get('desc',''),'author':author.get('nickname',''),'published_at':published,
        'duration_ms':video.get('duration'),'video_id':str(raw.get('aweme_id',info['video_id'])),
        'metrics':{'likes':stats.get('digg_count'),'favorites':stats.get('collect_count'),'shares':stats.get('share_count'),'comments':stats.get('comment_count'),'views':stats.get('play_count')}},
        'transcript':'','message':'已读取公开视频资料；本页面没有可用的字幕时间轴，需要转写服务或字幕数据。'},info,'douyin_public_page')
    # 2026-11-17 t108：互动数据随文案回传（缺字段 —），供选题页展示；与豆包文字兜底区分。
    result['interactions']=interactions_of(raw)
    result['interactions_source']='douyin_detail_api'
    result['need_login']=False
    return result


def public_resolve(share):
    info=share_info(share)
    if not info['url']:raise ValueError('没有识别到 HTTPS 分享链接；分享口令已保留，需要平台适配。')
    if info['platform']!='douyin':
        return normalize({'message':'视频号分享链接已保留；当前没有可用的视频号解析服务，未读取微信凭证。'},info,'public_page')
    try:page,final=public_page(info['canonical_url'])
    except ValueError as e:return normalize({'message':str(e)},info,'public_page')
    if not info['video_id']:
        info=share_info(final)
        if not info['video_id']:return normalize({'message':'短链接没有返回可识别的视频编号，需要解析服务。'},info,'public_page')
    # Stop at the challenge. Do not solve signatures or replay harvested cookies.
    if '__ac_nonce' in page and not ('RENDER_DATA' in page or '_ROUTER_DATA' in page):
        return normalize({'message':'抖音返回了访问校验页面，未取得字幕和互动数据。需要正常授权会话或可用的数据服务。'},info,'public_page')
    raw=embedded_video(page,info['video_id'])
    if not raw:return normalize({'message':'公开页面未提供这个视频的可读取资料；链接已保留，需要解析服务。'},info,'public_page')
    return from_douyin(raw,info)


def find_aweme(roots,video_id=''):
    """BFS over parsed embedded JSON objects for the aweme detail. Shared by page and browser paths."""
    queue=list(roots);seen=0;fallback=None
    while queue and seen<50000:
        obj=queue.pop();seen+=1
        if isinstance(obj,dict):
            key=str(obj.get('aweme_id',obj.get('awemeId','')))
            if obj.get('video') and key and (not video_id or key==video_id):
                if key==video_id:return obj
                if fallback is None:fallback=obj
            queue.extend(v for v in obj.values() if isinstance(v,(dict,list)))
        elif isinstance(obj,list):queue.extend(v for v in obj if isinstance(v,(dict,list)))
    return fallback


def browser_python():
    # 2026-11-19（t168）跨平台：Windows venv 在 Scripts/，mac/Linux 在 bin/。
    default_sub = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python3'
    return Path(os.getenv('IP_BROWSER_PYTHON', str(ROOT/'.runtime/browser-env'/default_sub)))


def browser_resolve(share,timeout=240):
    """Resolve via a real browser session. The browser passes the platform challenge itself;
    we only read the embedded public data and the page's own public detail API response.
    No cookies are harvested or replayed."""
    info=share_info(share)
    if info['platform']!='douyin':raise ValueError('浏览器解析当前仅支持抖音链接。')
    python=browser_python();script=ROOT/'browser_fetch.py'
    if not script.is_file():
        raise ValueError('缺少程序文件 browser_fetch.py：可能被杀毒软件隔离，或安装包不完整。'
                         '到杀毒软件隔离区恢复该文件并加信任，或重新解压完整包；也可改用手工粘贴文案。')
    if not python.is_file():
        raise ValueError('视频解析所需的浏览器运行环境没有随包安装（缺少 .runtime/browser-env）；'
                         '请使用完整包，或改用手工粘贴文案。')
    import browser_fetch
    if not browser_fetch.browser_executable():
        raise ValueError(browser_fetch.BROWSER_HINT)
    session_state=douyin_session.load()[1]
    if session_state.get('state')!='ok':
        # 2026-11-17 t108：没有可用登录态 → 明确 need_login（交 t107 弹登录），不拿豆包文字冒充互动数据。
        result=normalize({'message':douyin_session.describe(session_state)+
            '（豆包/转写只能做文字兜底，抓不到赞/评/藏/转；登录后可重试真抓。）'},info,'douyin_browser')
        result['status']='blocked'
        result['need_login']=True
        result['interactions']={'likes':'—','comments':'—','favorites':'—','shares':'—','views':'—'}
        result['interactions_source']='need_login'
        return result
    url=info['canonical_url'] or info['url']
    command=[str(python),str(script),'--url',url]
    if info['video_id']:command+=['--aweme-id',info['video_id']]
    # 系统 TEMP 可能不可写（历史坑：patchright mkdtemp EPERM），统一指到包内运行目录。
    scratch=Path(os.getenv('IP_DATA_DIR',str(ROOT/'.runtime')))/'tmp'
    scratch.mkdir(parents=True,exist_ok=True)
    child_env=dict(os.environ);child_env['TEMP']=child_env['TMP']=str(scratch)
    # 子进程 stdout 走管道时默认按系统 GBK 编码，标题里的 emoji 会让整个采集崩掉。
    child_env['PYTHONIOENCODING']='utf-8'
    # 2026-11-17 t67：起进程前自检 patchright，缺失时给人话原因（治笼统的“浏览器解析未完成”）。
    try:
        probe=subprocess.run([str(python),'-c','import patchright'],
                             capture_output=True,timeout=30,env=child_env,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ValueError('浏览器环境自检超时（patchright 无响应）；请重装浏览器环境，或直接粘贴文案。') from None
    if probe.returncode!=0:
        detail=(probe.stderr or b'').decode('utf-8','replace')[:160]
        raise ValueError('浏览器环境未装好（patchright 缺失：%s）；请使用完整包或重装浏览器环境，也可以直接粘贴文案。' % detail)
    proc=subprocess.run(command,capture_output=True,timeout=timeout,cwd=str(ROOT),env=child_env,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if proc.returncode!=0:
        # browser_fetch prints its error JSON to stdout (stderr stays empty); read both.
        detail=proc.stderr.decode('utf-8','replace')[:300].strip()
        if not detail:
            try:
                detail=str(json.loads(proc.stdout.decode('utf-8','replace')).get('error',''))[:300]
            except ValueError:
                detail=proc.stdout.decode('utf-8','replace')[:300].strip()
        raise ValueError('浏览器解析未完成：'+detail)
    try:payload=json.loads(proc.stdout.decode('utf-8','replace'))
    except ValueError:raise ValueError('浏览器解析返回格式无效。') from None
    if not payload.get('ok'):raise ValueError('浏览器解析未完成：'+str(payload.get('error',''))[:300])
    video_id=info['video_id'] or ''
    if not video_id:
        m=re.search(r'/video/(\d+)',payload.get('url',''))
        video_id=m.group(1) if m else ''
    raw=None;method='douyin_browser_page';filter_info=None
    probe=payload.get('api_probe') or {}
    if isinstance(probe,dict) and probe.get('ok'):
        try:probe_data=json.loads(probe.get('text',''))
        except ValueError:probe_data=None
        if isinstance(probe_data,dict):
            detail=probe_data.get('aweme_detail') or probe_data
            if isinstance(detail,dict) and (detail.get('video') or detail.get('aweme_id')):
                raw=detail;method='douyin_browser_api'
            elif isinstance(probe_data.get('filter_detail'),dict):
                filter_info=probe_data['filter_detail']
    if raw is None:
        for item in payload.get('api_responses') or []:
            try:candidate=json.loads(item.get('json_text',''))
            except ValueError:continue
            detail=(candidate.get('aweme_detail') if isinstance(candidate,dict) else None) or candidate
            if isinstance(detail,dict) and (detail.get('video') or detail.get('aweme_id')):
                raw=detail;method='douyin_browser_api';break
    if raw is None:
        roots=[]
        for name,blob in (('RENDER_DATA',payload.get('render_data')),('_ROUTER_DATA',payload.get('router')),('__INITIAL_STATE__',payload.get('initial_state'))):
            if not blob:continue
            if name=='RENDER_DATA':blob=unquote(blob)
            prefix=re.match(r'\s*(?:window\.)?(?:_ROUTER_DATA|__INITIAL_STATE__)\s*=\s*',blob)
            if prefix:blob=blob[prefix.end():]
            blob=blob.strip().rstrip(';')
            if blob.startswith(('{','[')):
                try:roots.append(json.JSONDecoder().raw_decode(blob)[0])
                except (ValueError,RecursionError):continue
        raw=find_aweme(roots,video_id)
    if not raw:
        parts=['公开页面未提供这个视频的可读取资料；链接已保留。']
        if isinstance(filter_info,dict):
            reason=str(filter_info.get('detail_msg') or filter_info.get('filter_reason') or '').strip()
            if reason:parts.append('平台原因：'+reason)
        note=douyin_session.describe(douyin_session.load()[1])
        if note and not note.startswith('已使用'):parts.append(note)
        # 风控/未登录：给出 need_login 语义（由上层路由原样回传用户）
        parts.append('（未取得互动数据；豆包/转写只能做文字兜底，登录后重试可抓赞/评/藏/转。）')
        raise ValueError(' '.join(parts))
    result=from_douyin(raw,info)
    result['method']=method
    result['session_state']=session_state.get('state')
    result['canonical_url']='https://www.douyin.com/video/'+str(raw.get('aweme_id',video_id))
    result['message']='已通过本机浏览器会话读取公开视频资料。'+(result['message'] or '')
    video=raw.get('video') or {}
    play=video.get('play_addr') or {}
    urls=[u for u in (play.get('url_list') or []) if isinstance(u,str)]
    for rate in video.get('bit_rate') or []:
        more=(rate.get('play_addr') or {}).get('url_list') or []
        urls.extend(u for u in more if isinstance(u,str))
    ordered=[u for u in urls if '/playwm/' not in u]+[u for u in urls if '/playwm/' in u]
    result['media_url']=ordered[0] if ordered else None
    result['video_id']=str(raw.get('aweme_id',video_id))
    return result


class MediaRedirect(urllib.request.HTTPRedirectHandler):
    """媒体 CDN 跳转：允许 https 与 http 中间跳（抖音 CDN 会先 302 到 http 再回 https），仍禁 userinfo/异常端口。"""
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        p=urlsplit(newurl)
        if p.scheme not in ('https','http') or p.username or p.password or p.port not in (None,80,443):
            raise ValueError('媒体跳转目标不受支持，已停止下载。')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download_media(url,target,max_bytes=MAX_MEDIA_BYTES,timeout=300):
    """Download a media file whose URL came from the platform's own embedded data. Bounded size."""
    if not isinstance(url,str) or not url.startswith('https://'):raise ValueError('媒体地址无效。')
    request=urllib.request.Request(url,headers={'User-Agent':MEDIA_USER_AGENT,'Accept':'*/*','Referer':'https://www.douyin.com/'})
    total=0
    with urllib.request.build_opener(MediaRedirect()).open(request,timeout=timeout) as r:
        length=r.headers.get('Content-Length')
        if length and length.isdigit() and int(length)>max_bytes:raise ValueError('媒体文件过大，已停止下载。')
        with open(target,'wb') as out:
            while True:
                chunk=r.read(256*1024)
                if not chunk:break
                total+=len(chunk)
                if total>max_bytes:raise ValueError('媒体文件超过 2GB 上限，已停止下载。')
                out.write(chunk)
    if total==0:raise ValueError('下载内容为空。')
    return total


def validate_analysis(result,source):
    if not isinstance(result,dict):raise ValueError('分析结果格式无效。')
    reference=source['transcript'];opening=source['opening'];warnings=[];blocks=[]
    for b in result.get('structure',[])[:8]:
        quote=str(b.get('quote','')).strip()
        if not quote or quote not in reference:
            warnings.append('一条结构建议的引文未能回源，已省略。');continue
        ids=[str(x) for x in b.get('cue_ids',[]) if any(c['id']==str(x) and quote in c['text'] for c in source['cues'])]
        blocks.append({'label':str(b.get('label','内容'))[:30],'quote':quote,'cue_ids':ids,'note':str(b.get('note',''))[:600]})
    hook=result.get('hook') or {};quote=str(hook.get('quote','')).strip()
    if quote and quote in opening['text']:
        hook={'quote':quote,'type':str(hook.get('type',''))[:60],'reason':str(hook.get('reason',''))[:1000],'status':'model_interpretation'}
    else:hook=None
    return {'status':'ready','summary':str(result.get('summary',''))[:3000],'hook':hook,'structure':blocks,
            'key_points':[str(x)[:800] for x in result.get('key_points',[])[:8]],'warnings':warnings,
            'method':'model_interpretation','note':'基于已取得的字幕分析；没有观看画面，不能推断镜头、表情、配乐或真实留存。'}


# ---------------------------------------------------------------- 抖音移动 Feed 解析（2026-11-17 t67）
# 方法论来自公开资料 ucmao/media-parser docs/parsers/douyin.md（学习其公开做法，不抄代码/密钥）：
#   移动端 Feed API 免签名免 cookie → playwm→play + 1080p + H.264 优选 → 原生 AI 字幕(VTT) → 缓存。
# 只在本地跑；失败按"真实原因"回话，不假装成功。

FEED_HOSTS = (
    'https://api5-normal-c-hl.amemv.com/aweme/v1/feed/',
    'https://aweme.snssdk.com/aweme/v1/feed/',
)
MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 '
             '(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
DOUYIN_PARSER_VERSION = 't67-1'
DOUYIN_CACHE_TTL = int(os.getenv('IP_DOUYIN_CACHE_TTL', '7200') or 7200)   # 元数据缓存 2 小时；media_url 时效更短，超过 30 分钟重新取流
DOUYIN_MEDIA_TTL = int(os.getenv('IP_DOUYIN_MEDIA_TTL', '1800') or 1800)


def _douyin_cache_dir():
    return Path(os.getenv('IP_DATA_DIR', str(ROOT / '.runtime'))) / 'cache' / 'douyin'


def _cache_read(aweme_id):
    try:
        path = _douyin_cache_dir() / (str(aweme_id) + '.json')
        if not path.is_file():
            return None
        blob = json.loads(path.read_text(encoding='utf-8'))
        age = time.time() - float(blob.get('cached_at') or 0)
        if age > DOUYIN_CACHE_TTL:
            return None
        if blob.get('parser_version') != DOUYIN_PARSER_VERSION:
            return None
        blob['age'] = int(age)
        return blob
    except (OSError, ValueError):
        return None


def _cache_write(aweme_id, payload):
    try:
        folder = _douyin_cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        data = dict(payload)
        data['cached_at'] = time.time()
        data['parser_version'] = DOUYIN_PARSER_VERSION
        (folder / (str(aweme_id) + '.json')).write_text(
            json.dumps(data, ensure_ascii=False)[:400000], encoding='utf-8')
    except OSError:
        pass


def _http_json(url, timeout=12):
    request = urllib.request.Request(url, headers={
        'User-Agent': MOBILE_UA, 'Accept': 'application/json',
        'Referer': 'https://www.douyin.com/', 'Accept-Language': 'zh-CN,zh;q=0.9'})
    with urllib.request.build_opener(MediaRedirect()).open(request, timeout=timeout) as resp:
        raw = resp.read(4 * 1024 * 1024)
    return json.loads(raw.decode('utf-8', 'replace'))


def _http_text(url, timeout=12):
    request = urllib.request.Request(url, headers={
        'User-Agent': MOBILE_UA, 'Accept': '*/*', 'Referer': 'https://www.douyin.com/'})
    with urllib.request.build_opener(MediaRedirect()).open(request, timeout=timeout) as resp:
        return resp.read(2 * 1024 * 1024).decode('utf-8', 'replace')


def follow_short(url, timeout=12):
    """短链 → 最终地址。只跟同协议跳转，不取 cookie。"""
    try:
        page, final = public_page(url)
        return final or url
    except ValueError:
        return url


def feed_detail(aweme_id):
    """移动 Feed 免签名直连；返回 aweme_detail（dict）。失败给明确原因。"""
    aweme_id = str(aweme_id or '').strip()
    if not re.fullmatch(r'\d{8,30}', aweme_id):
        raise ValueError('没有解析出视频编号，无法走移动 Feed。')
    last = None
    for host in FEED_HOSTS:
        url = (host + '?aweme_id=' + aweme_id +
               '&aid=1128&version_code=290100&device_platform=iphone&os_version=17.0&app_name=aweme')
        try:
            data = _http_json(url)
        except (OSError, ValueError) as exc:
            last = '%s(%s)' % (type(exc).__name__, str(exc)[:60])
            continue
        filter_detail = data.get('filter_detail') if isinstance(data, dict) else None
        if isinstance(filter_detail, dict) and filter_detail:
            reason = str(filter_detail.get('detail_msg') or filter_detail.get('filter_reason') or '').strip()
            raise ValueError('平台返回：这条视频看不了（%s）。' % (reason or '已删除/私密/地区限制'))
        items = (data.get('aweme_list') or []) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and str(item.get('aweme_id') or '') == aweme_id and item.get('video'):
                return item
        if items:
            # 推荐流里夹了别的作品但没有目标本身：继续试备用节点。
            last = '返回了推荐流，未包含目标视频'
            continue
        last = '返回空列表(status=%s)' % (data.get('status_code') if isinstance(data, dict) else '?')
    raise ValueError('移动 Feed 没有拿到这条视频（%s）。' % (last or '未知原因'))


def _clean_play(url):
    text = str(url or '')
    if not text:
        return ''
    text = text.replace('/playwm/', '/play/')
    text = re.sub(r'ratio=\d+p', 'ratio=1080p', text)
    return text


def choose_media(video):
    """选流：1080p H.264 构造 > play_addr_h264(CDN) > play（无水印）> 265 > lowbr > download（带水印，最后）。
    全部做 playwm→play / ratio=1080p 清洗；下载 403 的根因是用了带水印端，这里默认不走 download_addr。"""
    candidates = []

    def add(urls, rank, tag, bitrate=0, h265=None):
        for raw in urls or []:
            if not isinstance(raw, str) or not raw.startswith('https://'):
                continue
            candidates.append({'url': _clean_play(raw), 'rank': rank, 'tag': tag,
                               'bitrate': int(bitrate or 0), 'h265': h265})

    uri = (video.get('play_addr_h264') or {}).get('uri') or (video.get('play_addr') or {}).get('uri') or ''
    if uri:
        add(['https://www.iesdouyin.com/aweme/v1/play/?video_id=%s&ratio=1080p&line=0' % uri], 0, '1080p-h264')
    add((video.get('play_addr_h264') or {}).get('url_list') or [], 1, 'h264-cdn')
    add((video.get('play_addr') or {}).get('url_list') or [], 2, 'play')
    add((video.get('play_addr_265') or {}).get('url_list') or [], 3, 'h265')
    add((video.get('play_addr_lowbr') or {}).get('url_list') or [], 4, 'lowbr')
    play = video.get('play_addr') or {}
    add((play.get('url_list') or [])[2:3], 2, 'play-node3')
    if not candidates:
        add((video.get('download_addr') or {}).get('url_list') or [], 9, 'download-watermarked')
    if not candidates:
        return {'url': '', 'note': '视频流地址未取到'}
    candidates.sort(key=lambda c: (c['rank'], -c['bitrate']))
    best = candidates[0]
    return {'url': best['url'], 'tag': best['tag'], 'bitrate': best['bitrate'],
            'h265': best['h265'], 'candidates': len(candidates)}


def _parse_vtt(text):
    cues = []
    lines = str(text or '').replace('\r\n', '\n').split('\n')
    idx = 0
    stamp = re.compile(r'(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})')
    while idx < len(lines):
        line = lines[idx].strip()
        m = stamp.search(line)
        if not m:
            idx += 1
            continue
        start = ((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) * 1000
                 + int((m.group(4) + '000')[:3]))
        end = ((int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))) * 1000
               + int((m.group(8) + '000')[:3]))
        idx += 1
        body = []
        while idx < len(lines) and lines[idx].strip():
            body.append(re.sub(r'<[^>]+>', '', lines[idx]).strip())
            idx += 1
        text = ' '.join(x for x in body if x).strip()
        if text:
            cues.append({'id': 'c%d' % (len(cues) + 1), 'start_ms': max(0, start),
                         'end_ms': max(start + 200, end), 'text': text})
    return cues


def native_captions(video):
    """原生 AI 字幕：cla_info.caption_infos / subtitle_infos 里的 WebVTT。"""
    infos = []
    cla = video.get('cla_info') or {}
    if isinstance(cla, dict):
        infos.extend(cla.get('caption_infos') or [])
    infos.extend(video.get('caption_infos') or [])
    infos.extend(video.get('subtitle_infos') or [])
    seen = set()
    for info in infos:
        if not isinstance(info, dict):
            continue
        url = str(info.get('url') or info.get('caption_url') or '')
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            cues = _parse_vtt(_http_text(url))
        except (OSError, ValueError):
            continue
        if cues:
            return {'cues': cues, 'source': url, 'lang': info.get('lang') or info.get('language_code') or '',
                    'caption_format': info.get('caption_format') or 'webvtt'}
    return {'cues': [], 'source': '', 'lang': '', 'caption_format': ''}


def ssr_detail(aweme_id):
    """分享页 SSR 兜底：移动端 UA 取 _ROUTER_DATA / videoInfoRes.item_list。"""
    for url in ('https://www.iesdouyin.com/share/video/%s/' % aweme_id,
                'https://www.douyin.com/video/%s' % aweme_id):
        try:
            page, final = public_page(url)
        except ValueError:
            continue
        if 'RENDER_DATA' in page or '_ROUTER_DATA' in page or 'videoInfoRes' in page:
            raw = embedded_video(page, str(aweme_id))
            if raw:
                return raw
    return None


def resolve_share(share, use_cache=True, allow_browser=False):
    """抖音分享链/短链/精选 modal → 无水印流＋元数据＋原生字幕（纯 HTTP，快路径）。"""
    info = share_info(share)
    if not info['url']:
        raise ValueError('没有识别到 HTTPS 分享链接；分享口令已保留，需要平台适配。')
    if info['platform'] != 'douyin':
        raise ValueError('这条不是抖音链接，移动 Feed 快路径不处理。')
    video_id = info['video_id']
    canonical = info['canonical_url']
    if not video_id:
        final = follow_short(info['url'])
        info = share_info(final) if share_info(final)['video_id'] else info
        video_id = info['video_id']
        canonical = info['canonical_url'] or final
    if not video_id:
        raise ValueError('短链没有指向一条有效视频（可能已失效、被删除或跳转了首页）。')
    cached = _cache_read(video_id) if use_cache else None
    if cached and cached.get('result') and cached.get('age', 0) <= DOUYIN_MEDIA_TTL:
        result = dict(cached['result'])
        result['method'] = 'douyin_feed_cache'
        result['cache_hit'] = True
        result['cache_age_s'] = cached.get('age')
        return result
    try:
        raw = feed_detail(video_id)
    except ValueError as exc:
        # 容灾：移动端分享页 SSR（_ROUTER_DATA）。仍拿不到就按真实原因回话。
        raw = ssr_detail(video_id)
        if not raw:
            raise ValueError('%s 链接可能已失效、被删除或仅粉丝可见；换一条新鲜分享链接，或直接粘贴文案。' % str(exc)[:120])
    result = from_douyin(raw, share_info(canonical))
    media = choose_media(raw.get('video') or {})
    captions = native_captions(raw.get('video') or {})
    if captions['cues']:
        result['cues'] = captions['cues']
        result['transcript'] = '\n'.join(c['text'] for c in captions['cues'])[:120000]
        result['status'] = 'ready'
        result['opening'] = opening_window(captions['cues'], [])
        result['message'] = '已取到原生 AI 字幕（WebVTT），不需要下载转写。'
    result['media_url'] = media.get('url') or None
    result['media_meta'] = {k: media.get(k) for k in ('tag', 'bitrate', 'h265', 'candidates')}
    result['captions'] = {'source': captions['source'], 'lang': captions['lang'],
                          'format': captions['caption_format'], 'cues': len(captions['cues'])}
    result['method'] = 'douyin_feed'
    result['cache_hit'] = False
    result['canonical_url'] = canonical
    result['video_id'] = video_id
    _cache_write(video_id, {'result': result})
    return result
