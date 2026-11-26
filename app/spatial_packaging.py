"""Local JD spatial adapter; no service, ASR, model or account access."""
from pathlib import Path
import hashlib, json, math, shutil
ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / 'tools' / 'jd-spatial'
STYLE = 'spatial:jd-spatial-voice-v1'
TEMPLATE = 'jd-spatial-voice-v1'
def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def file_hash(p):
    with Path(p).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()
def bundle():
    active=json.loads((KIT/'active.json').read_text(encoding='utf-8'))
    root=(KIT/active['path']).resolve()
    if not root.is_relative_to((KIT/'versions').resolve()): raise ValueError('空间核心路径越界')
    meta=json.loads((root/'bundle-manifest.json').read_text(encoding='utf-8'))
    if meta['templateId']!=TEMPLATE or meta['bundleDigest']!=active['bundleDigest']: raise ValueError('空间核心身份不一致')
    return root,meta
def verify_bundle():
    root,meta=bundle()
    files=meta['files']
    if digest(files)!=meta['bundleDigest']: raise ValueError('空间核心清单摘要错误')
    for item in files:
        p=(root/item['path']).resolve()
        if not p.is_relative_to(root) or not p.is_file() or file_hash(p)!=item['sha256']: raise ValueError('空间核心资源不完整：'+item['path'])
    return root,meta
def engine_binding():
    _,m=bundle()
    return {k:m[k] for k in ('templateId','coreVersion','contractVersion','bundleDigest')}
def capabilities():
    try:
        verify_bundle()
        return {'ready':True,'missing':[],**engine_binding()}
    except (OSError,ValueError,KeyError) as ex:
        return {'ready':False,'missing':['空间核心包：'+str(ex)]}
def styles():
    if not (KIT/'active.json').is_file(): return []
    try: binding=engine_binding()
    except (OSError,ValueError,KeyError): return []
    return [dict(id=STYLE,family='spatial',spatial_template=True,manual_only=True,fixed_aspect='9:16',
        name='JD · 空间口播',caption='9:16 · 原句空间运镜 · 30fps · 手动选择',mood='',
        colors={'bg':'#111318','ink':'#f2ede3','accent':'#cbb37d'},reason='按原句时间走镜头，先试看再生成',
        engine_binding=binding)]
def validate_options(value,cues):
    value={} if value is None else value
    allowed={'version','groups','emphasis_cue_ids','preset','theme','object_position'}
    if not isinstance(value,dict) or set(value)-allowed: raise ValueError('空间参数包含未登记字段')
    ids={c['id'] for c in cues}
    groups=value.get('groups',[])
    emphasis=value.get('emphasis_cue_ids',[])
    if not isinstance(groups,list) or not isinstance(emphasis,list) or any(i not in ids for i in emphasis) or len(set(emphasis))!=len(emphasis): raise ValueError('空间重点引用无效')
    used=set(); normalized=[]
    order={c['id']:i for i,c in enumerate(cues)}
    for g in groups:
        if not isinstance(g,dict) or set(g)!={'kind','cue_ids'}: raise ValueError('原句结构格式错误')
        kind=g['kind']; selected=g['cue_ids']
        cap=5 if kind=='points' else 6
        if kind not in ('process','points','contrast') or not isinstance(selected,list) or not 2<=len(selected)<=cap or (kind=='contrast' and len(selected)!=3): raise ValueError('原句结构数量或类型无效')
        if any(i not in ids or i in used for i in selected) or len(set(selected))!=len(selected): raise ValueError('原句引用重复或已失效')
        if selected!=sorted(selected,key=order.get): raise ValueError('原句顺序不能改变')
        positions=[order[i] for i in selected]
        if positions!=list(range(positions[0],positions[-1]+1)): raise ValueError('原句结构必须连续，不能遗漏中间句')
        used.update(selected);normalized.append({'kind':kind,'cue_ids':selected})
    preset=value.get('preset','auto');theme=value.get('theme','warm');position=value.get('object_position','center')
    if preset not in ('auto','01','02','03','04','05','06','07','08') or theme not in ('warm','cool') or position not in ('center','top','bottom'): raise ValueError('空间策略无效')
    return {'version':1,'groups':normalized,'emphasis_cue_ids':emphasis,'preset':preset,'theme':theme,'object_position':position}
def normalize_input(cues,options,duration):
    options=validate_options(options,cues);index={c['id']:i for i,c in enumerate(cues)}
    captions=[dict(i=i,start=c['start_ms']/1000,end=c['end_ms']/1000,text=c['text'],originCueId=c['id']) for i,c in enumerate(cues)]
    scenes=[];covered=set();ranges=[]
    def scene(sid,kind,indices):
        blocks=[dict(text=captions[i]['text'],start=captions[i]['start'],end=captions[i]['end'],source_segment_ids=[i]) for i in indices]
        return dict(id=sid,kind=kind,start=min(b['start'] for b in blocks),end=max(b['end'] for b in blocks),blocks=blocks,
            presenter='pip',heading=None,motion=[],emphasis=[c['text'] for c in cues if c['id'] in options['emphasis_cue_ids']],audio_events=[])
    for n,g in enumerate(options['groups']):
        indices=[index[i] for i in g['cue_ids']];s=scene('group-'+str(n),g['kind'],indices)
        if any(s['start']<b and s['end']>a for a,b in ranges): raise ValueError('原句结构时间重叠')
        ranges.append((s['start'],s['end']));covered.update(indices);scenes.append(s)
    for i,c in enumerate(captions):
        if i not in covered and not any(c['start']<b and c['end']>a for a,b in ranges):
            scenes.append(scene('quote-'+str(i),'quote',[i]))
    scenes.sort(key=lambda s:s['start'])
    return dict(version=1,templateId=TEMPLATE,template='spatial-voice',duration=duration,fps=30,captions=captions,contentScenes=scenes,
        sourceMap={str(i):c['id'] for i,c in enumerate(cues)},jdSpatialPolicy={'theme':options['theme'],'preset':options['preset']},
        objectPosition={'center':'50% 50%','top':'50% 20%','bottom':'50% 80%'}[options['object_position']])
def binding_fingerprint(base,clean,packet,options):
    return digest({'base':base,'source_sha256':file_hash(clean['path']),'engine':engine_binding(),'packet':packet,'options':options,'output_policy':1})
def prepare(plan,clean,video,work,renderDuration,rt,env,run,log):
    root,meta=verify_bundle()
    inst=Path(work)/'instance'
    if inst.exists(): raise ValueError('空间实例已存在，保留原件')
    shutil.copytree(root/'template',inst)
    shutil.copytree(root/'_assets',inst/'_assets')
    packet=dict(plan['spatial_input']);count=max(1,round(renderDuration*30))
    packet.update(frames=count,renderDuration=renderDuration,coreDigest=meta['bundleDigest'],coreVersion=meta['coreVersion'],sfxEvents=[])
    packet['jdSpatialClock']={s['id']+':'+str(i):{'start':round(b['start']*30),'end':round(b['end']*30)} for s in packet['contentScenes'] for i,b in enumerate(s['blocks'])}
    (inst/'template-job.json').write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding='utf-8')
    (inst/'frames').mkdir()
    run([rt['ffmpeg'],'-v','error','-n','-i',video,'-vf','fps=30,scale=720:-2,tpad=stop_mode=clone:stop_duration=1',
        '-frames:v',count,'-q:v','3',inst/'frames'/'%06d.jpg'],env,work,log)
    settings={'profile':'motion-three.instance/1','family':TEMPLATE,'width':720 if renderDuration<packet['duration'] else 1080,
        'height':1280 if renderDuration<packet['duration'] else 1920,'fps':30,'duration':count/30}
    # Preview size is passed explicitly, including sources shorter than eight seconds.
    if plan.get('_spatial_preview'): settings.update(width=720,height=1280)
    html=(inst/'index.html').read_text(encoding='utf-8')
    css='<style>html,body{width:%dpx;height:%dpx}#root{transform:scale(%s);transform-origin:0 0}</style>'%(settings['width'],settings['height'],settings['width']/1080)
    (inst/'index.html').write_text(html.replace('</head>',css+'</head>'),encoding='utf-8')
    (inst/'settings.json').write_text(json.dumps(settings,indent=2),encoding='utf-8')
    return inst,{'controls':['spatial-voice'],'engine_binding':engine_binding()}
