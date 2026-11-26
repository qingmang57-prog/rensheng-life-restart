"""人生重启：同一EVA核心，复用原文字幕、现有模型接口与任务目录。"""
from pathlib import Path
import hashlib,importlib.util,json,os,re,shutil,subprocess
BASE=Path(__file__).resolve().parent.parent
CORE=BASE/'tools'/'rs-eva-talking'
STYLE='ms:观点变形口播'
def is_style(style):return str(style.get('id',''))==STYLE

# 体验优先导演（方案C P0，2026-11-19）：提示词在适配层，引擎 tools/rs-eva-talking 零改。
# 体验链：先写观众经历的变化（experience）→按内容语义选版式（reason）；mood 只调制强度不指派版式。
EXPERIENCE_PROMPT_VERSION='exp-1'
EXPERIENCE_DIRECTOR_SYSTEM='''你是纪录片级导演，为16:9真人口播决定分镜。只返回JSON，不重写原话、不生成HTML/JS。
先思考观众经历：按顺序通读全部字幕，为每一镜写experience（观众将经历怎样的变化，不超过80字），再据此选版式并写reason（引用本镜原文关键词说明为什么选它，不超过60字）。
版式按本镜内容语义指派，绝不按固定顺序或配额轮播：
列举/并列要点选points；重构、对照、正反转折选contrast；引用原话金句选quote；步骤、方法、隐喻流程选process；开场抓注意选hook；收束宣言选close；需要沉淀停顿的关键转折选paper（一片至多一次，且不与相邻镜重复）。
每镜1至4句（建议4至10秒），按语义转折切镜，不机械按秒数均分；长句用quote或paper，不能丢弃否定、数字和条件；相邻两镜尽量不用同一版式，除非内容确实连续同型。
情绪只调节奏与强度，不决定版式。若提供了模板规格，版式语义与容量以规格为准。
格式 {"scenes":[{"ids":[0,1],"layout":"quote","experience":"观众从…到…（不超过80字）","reason":"原话含…，quote大字沉淀（不超过60字）"}]}。
ids必须是输入字幕的连续编号，按原序不重不漏覆盖全部字幕。禁止额外文案。'''

def _template_spec(selected_id):
    """P1 规格消费：按模板读 MOTION_DESIGN.md 叙事语法/容量契约 ＋ replacement-contract.json
    → (规格块≤2.4k字, spec_sha)。定位机制与 t30 一致（'无角标'→克隆包）；读不到返回 ('','nospec')，不阻断。"""
    try:
        sid=str(selected_id or '')
        if '无角标' not in sid:return '',_NOSPEC_SHA
        pack=BASE/'tools'/'eva'/'模板-观点变形口播-无角标-MS'
        md=(pack/'MOTION_DESIGN.md').read_text(encoding='utf-8')
        rc=json.loads((pack/'replacement-contract.json').read_text(encoding='utf-8'))
        g=re.search(r'## 3\. Narrative grammar.*?(?=\n## \d)',md,re.S)
        c=re.search(r'## 8\. Replaceable inputs.*?(?=\n## \d)',md,re.S)
        layouts='、'.join(rc['replaceable']['scenes']['items']['layout']['values'])
        spec='【模板规格·观点变形口播-无角标】\n%s\n【容量契约】\n%s\n版式枚举：%s（导演按本片原文语义指派，非配额轮播）。长文案拆句或重设计该镜，不缩到不可读。'%(
            (g.group(0).strip() if g else ''),(c.group(0).strip() if c else ''),layouts)
        if len(spec)>2400:spec=spec[:2400]
        sha=hashlib.sha256((md+json.dumps(rc,ensure_ascii=False,sort_keys=True)).encode('utf-8')).hexdigest()
        return spec,sha
    except Exception:
        return '',_NOSPEC_SHA
_NOSPEC_SHA='nospec'

def _core():
    spec=importlib.util.spec_from_file_location('rs_eva_core_life',CORE/'rs_eva_engine.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def _tools():
    import eva_packaging as eva,media
    modules=Path(eva._node_path())
    pw=next((modules/n for n in ('playwright','playwright-core') if (modules/n).is_dir()),None)
    ff=Path(media.binary('ffmpeg'))
    if pw is None:raise ValueError('缺少随包Playwright')
    return dict(node=str(eva._node()),chrome=eva._chrome_exe(),playwright=str(pw),ffmpeg=str(ff),ffprobe=str(ff.with_name('ffprobe.exe')),faceModel=str(CORE/'models'/'yunet.onnx'))

def _choices(plan,work,core):
    rows=plan.get('cues') or [];full_duration=max(float(c['end_ms'])/1000 for c in rows)
    full=core.clean_captions(rows,full_duration)
    import providers
    can_llm=bool(providers.available().get('llm'))
    # 体验优先导演：缓存键加 prompt_version＋spec_sha——提示词或模板规格改版自动失效旧缓存（方案C P0-3/P1）。
    spec,spec_sha=_template_spec((plan.get('selected') or {}).get('id'))
    digest=hashlib.sha256(json.dumps(dict(version=core.VERSION,captions=full,llmAvailable=can_llm,prompt_version=EXPERIENCE_PROMPT_VERSION,spec_sha=spec_sha),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    cache=Path(work).parent/'rs-eva-director-cache';cache.mkdir(exist_ok=True);file=cache/(digest+'.json')
    if file.is_file():
        saved=json.loads(file.read_text(encoding='utf-8'));core.validate_choices(saved['plan'],full);return saved['plan'],saved['mode'],saved.get('reason','')
    mode='source-rule-fallback';reason='未启用生成模型';answer=None
    try:
        import providers
        if can_llm:
            payload=dict(captions=full)
            blocks=None
            if spec:
                payload['capacity']='每镜1至4句/4至10秒；版式字号自适应；长文案拆句或重设计该镜，不缩到不可读'
                blocks=[('spec:'+EXPERIENCE_PROMPT_VERSION,spec)]  # 字节稳定规格段，走前缀缓存（providers.stable_blocks）
            answer=providers.llm_json(EXPERIENCE_DIRECTOR_SYSTEM,payload,stable_blocks=blocks,tag='rs_eva.director')
            core.validate_choices(answer,full);mode='experience-first-director';reason=''
    except Exception as exc:reason='导演调用或校验失败：'+type(exc).__name__
    if mode!='experience-first-director':answer=dict(scenes=core.rule_choices(full))
    # First write wins; never overwrite another preview/export's cached plan.
    try:
        with file.open('x',encoding='utf-8') as f:json.dump(dict(plan=answer,mode=mode,reason=reason,prompt_version=EXPERIENCE_PROMPT_VERSION,spec_sha=spec_sha),f,ensure_ascii=False,indent=2)
    except FileExistsError:
        saved=json.loads(file.read_text(encoding='utf-8'));core.validate_choices(saved['plan'],full);return saved['plan'],saved['mode'],saved.get('reason','')
    return answer,mode,reason

def render_silent(plan,video,duration,work,preview,stage):
    import media
    core=_core();_t30_patch_face(core);caps=core.clean_captions(plan['cues'],duration)
    # t30 资产接线：mood→资产选择（user_materials 优先）+ 模板注入 patch 安装；失败降级纯模板渲染
    _T30_PENDING.clear()
    _T30_PENDING['style']=str((plan.get('selected') or {}).get('id',''))
    try:
        picked=_t30_select_assets(plan)
        _T30_PENDING['assets']=picked
        if picked:
            stage('资产接线：选中 bg=%s fx=%s broll=%d（mood 来源 %s）'%((picked.get('bg') or picked.get('user',{}).get('bg') or '-') and ((picked.get('bg') or {}).get('id') or 'user'),(picked.get('fx') or {}).get('id') or '-',len(picked.get('broll') or []),picked['mood_source']))
            _t30_install_patches()
    except Exception as exc:
        _T30_PENDING['assets']=None
        stage('资产接线未启用（%s: %s）：本次纯模板渲染，不阻断出片'%(type(exc).__name__,exc))
    answer,mode,reason=_choices(plan,work,core)
    # t35 情绪弧线接线：plan['context']['director_brief']（人生侧 director_pass.ensure_brief 产物）
    # 经 mood_bridge 调制现有导演选择；brief 缺失/解析失败一律回退原选择，不阻断出片。
    # 体验优先（方案C P0-4）：LLM base 时 mood 只调制强度（重切镜＋金句卡），不改写 LLM 版式指派；
    # rule 兜底路径行为与改前逐字节一致（零回归）。mode 保持 experience-first-director，reason 注明调制。
    llm_base=(mode=='experience-first-director')
    brief=(plan.get('context') or {}).get('director_brief')
    if brief:
        try:
            import mood_bridge
            segs,quote_cues,info=mood_bridge.brief_to_mood(brief,caps)
            if segs:
                modded=mood_bridge.mood_to_director_choices(caps,segs,quote_cues=quote_cues,base=answer.get('scenes'),llm_base=llm_base)
                answer=dict(scenes=modded)
                if llm_base:
                    reason='mood强度调制（版式保留导演指派）：来源=%s（导演「%s」弧线，%s对齐，%d段%d金句）'%(info['brief_id'],info['overall'],info['method'],info['segments'],info['quote_cues'])
                    stage('情绪桥：导演弧线强度调制已应用，版式保留 experience-first 指派（%s）'%info['method'])
                else:
                    mode='mood-bridge-real'
                    reason='mood来源=%s（导演「%s」弧线，%s对齐，%d段%d金句）'%(info['brief_id'],info['overall'],info['method'],info['segments'],info['quote_cues'])
                    stage('情绪桥：真实导演弧线已应用（%s）'%info['method'])
            else:
                stage('情绪桥未启用：导演简报无有效情绪弧线；保持原导演选择')
        except Exception as exc:
            stage('情绪桥未启用（%s）；保持原导演选择，不阻断出片'%type(exc).__name__)
    choices=[dict(s,ids=[i for i in s['ids'] if i<len(caps)]) for s in answer['scenes']]
    choices=[s for s in choices if s['ids']]
    core.validate_choices(dict(scenes=choices),caps)
    context=plan.get('context') or {};persona=context.get('persona') or {}
    job=dict(video=str(video),work=str(Path(work)/'rs-eva-run'),captions=caps,duration=duration,width=1920,title=context.get('title') or '',brand=persona.get('name') or '',director_choices=dict(scenes=choices),director_mode=mode,director_fallback_reason=reason,silent_only=True)
    # t30 资产接线：job 参数层新键（引擎白名单忽略未知键；渲染消费经模板包 assets.json 注入）
    _p=_T30_PENDING.get('assets') or {}
    if _p:job['assets']=_p
    if _p.get('sfx_events'):job['sfx_events']=_p['sfx_events']
    if mode=='source-rule-fallback':stage('AI导演未启用或未通过校验；本次使用原文保守布局')
    result=core.render(job,_tools(),stage,media.runtime_environment())
    # t30：渲染后落 ⑥ sfx-events.json（mix 唯一出口消费）+ ⑧ 资产选用证据进 director-report
    _picked=_T30_PENDING.get('assets')
    if _picked:
        try:
            run=Path(job['work'])
            (run/'sfx-events.json').write_text(json.dumps(dict(events=_picked.get('sfx_events') or [],note='t30⑥ 词级触发表；t34 音效包到位后映射真音色'),ensure_ascii=False),encoding='utf-8')
            ok=_t30_annotate(run,_picked)
            stage('资产接线：sfx-events.json 写入（%d 事件）；director-report assets 证据%s'%((len(_picked.get('sfx_events') or [])),'已落' if ok else '未落（无 director-report）'))
            if _T30_PENDING.get('inject_note'):stage('资产注入：%s'%_T30_PENDING['inject_note'])
        except Exception as exc:
            stage('资产接线收尾异常（%s），出片不受影响'%type(exc).__name__)
    _T30_PENDING.clear()
    target=Path(work)/'visual.mp4'
    if target.exists():raise FileExistsError(str(target))
    Path(result['video']).replace(target)
    return target

def mix(silent,video,work,duration,bgm,stage):
    import media
    core=_core();root=Path(work)/'rs-eva-run';pack=json.loads((root/'pack-input.json').read_text(encoding='utf-8'))
    events=None
    try:  # t30⑥ SFX 槽位：cue 词级触发表（render_silent 期写入；t34 音效包到位后由表驱动音色映射，混音仍唯一出口）
        events=json.loads((root/'sfx-events.json').read_text(encoding='utf-8'))['events']
        stage('SFX：按 cue 词级触发表混音（%d 事件，唯一混音出口）'%len(events))
    except Exception:
        pass
    if events is None:
        events=[dict(t=max(0,s['start']-.1),type=('dock','tick','chime')[i%3],g=.020) for i,s in enumerate(pack['scenes'][1:])]
    out=Path(work)/'packaged-with-bgm.mp4';stage('观点变形口播：原声＋低音量提示音'+('＋BGM人声闪避' if bgm else ''))
    meta=core.mix_audio(video,silent,out,_tools()['ffmpeg'],duration,music='auto' if bgm else 'off',events=events,env=media.runtime_environment())
    meta['credit']='BGM：'+str(meta.get('musicTitle') or '未添加')+'（本机测试曲，不随模板分发）'
    return out,meta


def binding():
    manifest=CORE/'bundle-manifest.json'
    raw=manifest.read_bytes();data=json.loads(raw.decode('utf-8'))
    return dict(template_version=data['version'],template_digest=hashlib.sha256(raw).hexdigest())

def local_music_status():
    track=_core().find_music()
    return dict(local_bgm_available=track is not None,local_bgm_title=track.stem if track else '')


# ===== t30 资产接线（2026-11-18）：人生侧消费 _资产库-20261118（工坊侧消费 si 素材层登记 sa_*）=====
# 引擎 tools/rs-eva-talking 零改动；patch 均为进程内（t18 双 patch 同型）；资产数据经 work/template/assets/ 注入模板包。
_ASSET_LIB=Path(os.environ.get('RS_ASSET_LIB') or BASE/'_资产库-20261118'/'manifest.json')
_T30_MOODS=('温情陪伴','燃','沉郁叙事','轻快')
_T30_PENDING={}  # {'assets':picked,'style':sid,'inject_note':str} —— 单渲染链内有效，一次一任务

def _t30_select_assets(plan):
    """mood→资产过滤 + user_materials 优先（工坊同型：可选素材逐项核验，缺失→留痕不硬塞）。
    返回 picked dict 或 None（无可选资产时不注入，纯模板渲染）。"""
    manifest=json.loads(_ASSET_LIB.read_text(encoding='utf-8'))
    ctx=plan.get('context') or {}
    brief=ctx.get('director_brief') or {}
    moods=set();src='default-all'
    for seg in (brief.get('moods') or brief.get('segments') or []):
        v=(seg.get('mood') if isinstance(seg,dict) else str(seg)) or ''
        if v in _T30_MOODS:
            moods.add(v);src='director_brief'
    if not moods:
        moods=set(_T30_MOODS)
    um=ctx.get('user_materials') or {}
    picked=dict(mood_source=src,moods=sorted(moods),user={},bg=None,fx=None,broll=[],sfx_events=None,why=[],degrade=[])
    up=um.get('背景') or um.get('bg')
    if up:
        if Path(up).is_file():
            picked['user']['bg']=str(up);picked['why'].append('背景槽=青芒提供素材 %s（优先于库资产）'%Path(up).name)
        else:
            picked['degrade'].append('user_materials.背景 不存在，回退库资产：%s'%up)
    def _hit(it):
        return bool(set(it.get('moods') or [])&moods)
    def _file_of(it,role):
        for f in it['files']:
            if f['role']==role:return f['path']
        return None
    bgs=sorted([i for i in manifest['items'] if i.get('kind')=='background' and _hit(i) and '1920x1080' in (i.get('resolution') or '')],key=lambda x:x['id'])
    fxs=sorted([i for i in manifest['items'] if i.get('kind')=='overlay-effect' and _hit(i)],key=lambda x:x['id'])
    brs=sorted([i for i in manifest['items'] if i.get('kind')=='broll' and _hit(i)],key=lambda x:x['id'])
    if bgs and 'bg' not in picked['user']:
        picked['bg']=dict(id=bgs[0]['id'],file=_file_of(bgs[0],'static-png'),moods=bgs[0].get('moods'),matched=sorted(set(bgs[0].get('moods') or [])&moods))
        picked['why'].append('背景=%s（moods 命中 %s，同 mood 并列按 id 序取首）'%(bgs[0]['id'],picked['bg']['matched']))
    if fxs:
        fdir=_file_of(fxs[0],'png-seq-30fps')
        if fdir:
            picked['fx']=dict(id=fxs[0]['id'],seq=_file_of(fxs[0],'png-seq-30fps'),moods=fxs[0].get('moods'),matched=sorted(set(fxs[0].get('moods') or [])&moods))
            picked['why'].append('特效叠加=%s（库内现成 png-seq，竖版 1080x1920 横版任务居中 contain 不拉伸）'%fxs[0]['id'])
    if brs:
        for b in brs[:2]:
            mp4=_file_of(b,'mp4-loopable')
            if mp4:picked['broll'].append(dict(id=b['id'],file=mp4,moods=b.get('moods'),matched=sorted(set(b.get('moods') or [])&moods)))
        if picked['broll']:picked['why'].append('B-ROLL 池=%s（最多 2 件，插播垫层 0~1.2s）'%','.join(x['id'] for x in picked['broll']))
    cues=[]
    for c in plan.get('cues') or []:
        txt=str(c.get('text') or '');t=round(c.get('start_ms',0)/1000.,3)
        typ='chime' if any(k in txt for k in ('本质','关键','记住','核心')) else ('tick' if len(txt)>24 else 'dock')
        cues.append(dict(t=t,type=typ,g=.020,cue=txt[:18]))
    picked['sfx_events']=cues  # ⑥ 槽位：t34 音效包到位后由包内 cue 表驱动音型映射（现内置三音型 stub）
    if not (picked['user'] or picked['bg'] or picked['fx'] or picked['broll']):
        return None
    return picked

def _t30_inject(tpl_dir,picked):
    """把选中资产物化进 work/template/assets/，写 assets.json 供模板包 runtime 消费。
    bg：png 直拷；fx：库内现成 png-seq 直拷；broll：mp4→png 帧序列（浏览器逐帧渲染最稳）。"""
    import shutil as _sh
    lib=_ASSET_LIB.parent;a=Path(tpl_dir)/'assets';a.mkdir(exist_ok=True)
    meta=dict(bg=None,fx=None,broll=[],note='t30 人生侧注入（引擎零改动；模板包消费）')
    if picked.get('user',{}).get('bg'):
        _sh.copy2(picked['user']['bg'],a/'bg.png');meta['bg']='assets/bg.png';meta['bgSource']='user'
    elif picked.get('bg'):
        _sh.copy2(lib/picked['bg']['file'],a/'bg.png');meta['bg']='assets/bg.png';meta['bgSource']='library'
    if picked.get('fx'):
        src=lib/picked['fx']['seq'];dst=a/'fx-seq'
        if (src).is_dir():
            if dst.exists():_sh.rmtree(dst)
            _sh.copytree(src,dst)
            meta['fx']=dict(dir='assets/fx-seq',id=picked['fx']['id'],fps=30,frames=len(list(dst.glob('*.png'))))
    for i,b in enumerate(picked.get('broll') or []):
        dst=a/('broll-seq-%d'%i);dst.mkdir(exist_ok=True)
        ff=str(_tools()['ffmpeg'])
        r=subprocess.run([ff,'-v','error','-y','-i',str(lib/b['file']),'-t','4','-vf','fps=30,scale=1920:1920:force_original_aspect_ratio=increase,crop=1920:1080','-q:v','3',str(dst/'%06d.png')],capture_output=True,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if r.returncode!=0:
            raise RuntimeError('broll 转帧失败 rc=%d: %s'%(r.returncode,(r.stderr or '')[-200:]))
        meta['broll'].append(dict(dir='assets/broll-seq-%d'%i,id=b['id'],frames=len(list(dst.glob('*.png')))))
    (a/'assets.json').write_text(json.dumps(meta,ensure_ascii=False),encoding='utf-8')
    return meta

def _t30_patch_face(core):
    """⑦ 无脸降级（工坊同型）：引擎门（检出<90% 硬拒）零改动；适配层包装 face_track，
    ValueError→中心窗 faces（窗内容=原帧中心裁切）+降级留痕 report，不崩不阻断。"""
    orig=core.face_track
    def face_track(video,start,duration,ffmpeg,model,env):
        try:
            return orig(video,start,duration,ffmpeg,model,env)
        except ValueError as exc:
            report=dict(samples=0,detected=0,ratio=0.,method='fallback-center-crop',fallback=True,
                        note='人脸有效检出不足90%：按工坊同型降级——人物窗内容取原帧中心裁切，不崩溃不阻断出片',
                        origin_error=str(exc))
            n=max(1,int(duration*6)+1)
            return [dict(t=i/6.,x=.5,y=.42,w=.24,h=.24) for i in range(n)],report
    core.face_track=face_track

def _t30_install_patches():
    import shutil as _sh
    if _T30_PENDING.get('patched'):return
    _orig_ct=_sh.copytree
    def copytree(src,dst,**kw):
        r=_orig_ct(src,dst,**kw)
        try:
            s=str(src).replace('\\','/')
            if s.endswith('rs-eva-talking/template'):
                sid=_T30_PENDING.get('style') or ''
                if '无角标' in sid:  # t18 配方：无角标 sid 用克隆包两文件覆盖引擎自带模板
                    tpl=BASE/'tools'/'eva'/'模板-观点变形口播-无角标-MS'/'content'/'template'
                    for f in ('index.html','runtime.js'):
                        _sh.copy2(tpl/f,Path(dst)/f)
                picked=_T30_PENDING.get('assets')
                if picked:
                    meta=_t30_inject(dst,picked)
                    _T30_PENDING['inject_note']=json.dumps(meta,ensure_ascii=False)
        except Exception as exc:
            _T30_PENDING['inject_note']='inject-failed: %s: %s'%(type(exc).__name__,exc)
        return r
    _sh.copytree=copytree
    _T30_PENDING['patched']=True

def _t30_annotate(run_dir,picked):
    """⑧ 资产选用证据落 director-report.json（引擎先写完，适配层补写 assets 键）。"""
    f=Path(run_dir)/'director-report.json'
    if not f.is_file():return False
    d=json.loads(f.read_text(encoding='utf-8'))
    d['assets']=dict(backend='life-side-direct-read',library=str(_ASSET_LIB.name),
                     picked=picked,engine_zero_change=True,
                     note='t30 选用证据（可审计）：mood 过滤+user_materials 优先+注入清单见 work/template/assets/assets.json')
    f.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
    return True
