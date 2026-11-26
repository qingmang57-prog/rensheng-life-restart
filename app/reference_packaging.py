"""Reference-derived templates using the workbench's bundled Motion frame pump."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KIT = ROOT.parent / 'tools' / 'smart-packaging'
LIB = KIT / 'templates' / 'motion-three'
META = {
    'boundary': ('画幅实验室', '3:4 · 连续口播与三幕图解', {'bg':'#101114','ink':'#f2f1eb','accent':'#2864ef'}),
    'card-theatre': ('奶油卡片剧场', '16:9 · 连续口播与六张知识卡', {'bg':'#fff8e8','ink':'#182233','accent':'#3262ed'}),
    'dark-matrix': ('深场作品矩阵', '16:9 · 作品墙，保留原声、不显示人物', {'bg':'#090b10','ink':'#f4f3ee','accent':'#527cff'}),
}

def catalog():
    p = LIB / 'catalog.json'
    return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {'templates': {}}

def styles():
    cat = catalog()
    return [dict(id=k, family=k, mood='', name=v[0], caption=v[1], colors=v[2],
                 reference_template=True, fixed_aspect='3:4' if k=='boundary' else '16:9')
            for k,v in META.items() if k in cat['templates'] and (LIB/cat['templates'][k]['folder']/'index.html').is_file()]

def defaults(style_id, cues, context):
    clauses = [x.strip() for c in cues for x in re.split(r'[，,。；;！!？?\n]',c['text']) if x.strip()]
    title = str(context.get('title') or '本期分享')[:30]
    content = {'title': title, 'brand': str(context.get('persona',{}).get('name') or '')[:30], 'position':'center'}
    if style_id=='boundary':
        content.update(headline=(clauses[0] if clauses else title)[:24],note='\n'.join(clauses[1:3])[:60],
                       steps='\n'.join(x[:14] for x in clauses[1:4]),closing=(clauses[-1] if clauses else '')[:28])
    else:
        count = 6 if style_id=='card-theatre' else 40
        content['cards']='\n'.join(x[:12] for x in clauses[:count])
    return content

def validate_content(style_id, value):
    if style_id not in META or not isinstance(value,dict): raise ValueError('模板内容格式无效。')
    limits={'title':40,'brand':30,'position':6}
    limits.update({'headline':36,'note':60,'steps':50,'closing':36} if style_id=='boundary' else {'cards':600})
    if set(value)-set(limits): raise ValueError('包含未登记的模板字段。')
    out={}
    for key,limit in limits.items():
        item=value.get(key,'center' if key=='position' else '')
        if not isinstance(item,str) or len(item)>limit: raise ValueError('模板文字过长或格式无效：'+key)
        out[key]=item.strip()
    if out['position'] not in ('center','top','bottom'): raise ValueError('人物取景位置无效。')
    if not out['title']: raise ValueError('请填写本期主题。')
    if style_id=='boundary':
        lines=[x.strip() for x in out['steps'].splitlines() if x.strip()]
        if len(lines)>3 or any(len(x)>14 for x in lines): raise ValueError('步骤最多三行，每行14字。')
    else:
        lines=[x.strip() for x in out['cards'].splitlines() if x.strip()]
        max_count=6 if style_id=='card-theatre' else 40
        if not 1<=len(lines)<=max_count or any(len(x)>12 for x in lines):
            raise ValueError(f'卡片需1至{max_count}行，每行最多12字。')
    return out

def content_for(store, clean_id, style_id, cues, context):
    saved=store.get('packaging_content',clean_id) or {}
    value=saved.get('by_style',{}).get(style_id)
    return validate_content(style_id,value if value is not None else defaults(style_id,cues,context))

def prepare(plan, clean, video, duration, work, preview, rt, env, run, log):
    sid=plan['selected']['id']; content=validate_content(sid,plan['template_content'])
    cues=[dict(start=c['start_ms']/1000,end=min(c['end_ms']/1000,duration),text=c['text'])
          for c in plan['cues'] if c['start_ms']/1000<duration]
    cues=[c for c in cues if c['end']>c['start']]
    task=dict(template=sid,video=str(video),work=str(work/'template-run'),duration=duration,
              fps=24 if preview else 30,width=(720 if preview else 1440) if sid=='boundary' else (960 if preview else 1920),
              brand=content['brand'],title=content['title'],captions=cues,prepare_only=True,
              object_position={'center':'50% 50%','top':'50% 20%','bottom':'50% 80%'}[content['position']])
    if sid=='boundary':
        steps=[x.strip() for x in content['steps'].splitlines() if x.strip()]
        task['text']={'#thesis-content .title':content['headline'],'#thesis-content .note':content['note'],
            '#thesis-content .pill':'','#scene-method .eyebrow':'02 / 本期要点','#method-note':'',
            '#scene-progress .number':'03','#scene-progress .label':'本期行动',
            '#end-copy':content['closing'],'#scene-progress .note':'',
            '#thesis-content .eyebrow':'01 / 核心观点','#scene-progress .eyebrow':'03 / 带走一句话',
            '.footer':'','.ratio':'3:4'}
        for i in range(3):task['text']['#m'+str(i+1)]=(str(i+1).zfill(2)+' '+steps[i]) if i<len(steps) else ''
    else:
        task['items']=[{'title':x.strip(),'tag':''} for x in content['cards'].splitlines() if x.strip()]
        if sid=='card-theatre': task['text']={'.mini':'','.signature':''}
        if sid=='dark-matrix':
            task['text']={'.metrics > div:first-child strong':str(len(task['items'])),
                '.metrics > div:first-child label':'条内容要点','.metrics > div:nth-child(2) label':'张主题卡片',
                '.metrics em':'','.edge':'','.inside small':'','.tile-code':''}
    task_file=work/'template-job.json'
    task_file.write_text(json.dumps(task,ensure_ascii=False,indent=2),encoding='utf-8')
    run([rt['python'],KIT/'engine/template_job.py',task_file],env,KIT,log)
    instance=work/'template-run'/'instance'
    cfgfile=instance/'template-job.json'; cfg=json.loads(cfgfile.read_text(encoding='utf-8'))
    cfg['hideUnusedItems']=True
    if sid!='boundary':cfg['activeItems']=len(task['items'])
    cfgfile.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    return instance, {'controls':['reference-'+sid], 'content':content, 'aspect':plan['selected']['fixed_aspect']}
