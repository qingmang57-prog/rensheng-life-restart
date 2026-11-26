"""Shared, source-bound EVA talking renderer. No network, paid calls or source mutation.
Adapters supply a validated director plan; fallback is explicitly labelled.
"""
from pathlib import Path
import json, math, os, re, hashlib, shutil, subprocess, wave
VERSION='1.0.0'
ROOT=Path(__file__).resolve().parent
TEMPLATE_ID='rs-eva-talking-169'
LAYOUTS=('hook','quote','points','process','contrast','paper','close')
GEOMETRIES=[dict(x=1140,y=195,w=650,h=650,r=325),dict(x=95,y=150,w=775,h=735,r=38),dict(x=1190,y=240,w=600,h=600,r=44),dict(x=95,y=240,w=610,h=610,r=305),dict(x=1135,y=180,w=680,h=680,r=40),dict(x=1155,y=215,w=630,h=630,r=315),dict(x=95,y=150,w=775,h=735,r=40),dict(x=110,y=240,w=610,h=610,r=48),dict(x=1150,y=215,w=640,h=640,r=320)]
def dump(path,data):
    Path(path).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
def run(args,env=None):
    p=subprocess.run([str(x) for x in args],env=env,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode: raise RuntimeError(p.stderr.decode('utf-8','replace')[-3000:])
    return p.stdout

def clean_captions(rows,duration):
    result=[]
    for row in rows:
        a=max(0.,float(row.get('start',row.get('start_ms',0)/1000)))
        b=min(duration,float(row.get('end',row.get('end_ms',duration*1000)/1000)))
        text=str(row.get('text',row.get('speak',''))).strip()
        if text and b>a: result.append(dict(start=a,end=b,text=text))
    result.sort(key=lambda x:x['start'])
    if not result: raise ValueError('没有带时间码的原文，不能包装成有内容的视频')
    for i,c in enumerate(result): c['i']=i
    return result

DIRECTOR_SYSTEM='''为16:9真人口播选择模板版式。只返回JSON，不重写原话、不生成HTML/JS。
格式 {"scenes":[{"ids":[0,1],"layout":"quote"}]}。
ids必须是输入字幕的连续编号，按原序不重不漏覆盖全部字幕。
每镜1至4句，建议4至10秒；按语义转折切镜，不机械按秒数均分。
layout只允许hook(开场)、quote(金句)、points(并列)、process(步骤)、contrast(对照)、paper(关键转折)、close(结尾)。
长句用quote或paper，不能丢弃否定、数字和条件。禁止额外文案。'''

def validate_choices(plan,captions):
    choices=plan.get('scenes') if isinstance(plan,dict) else None
    if not isinstance(choices,list) or not choices: raise ValueError('导演未返回场景')
    seen=[]
    for scene in choices:
        ids=scene.get('ids');layout=scene.get('layout')
        if layout not in LAYOUTS or not isinstance(ids,list) or not 1<=len(ids)<=4: raise ValueError('导演版式或容量无效')
        if any(type(i) is not int for i in ids) or ids!=list(range(ids[0],ids[-1]+1)): raise ValueError('字幕编号必须连续')
        seen+=ids
    if seen!=list(range(len(captions))): raise ValueError('导演字幕编号必须按原序、不重不漏')
    return choices

def rule_choices(captions):
    groups=[];ids=[]
    for i,c in enumerate(captions):
        if ids and (len(ids)>=3 or c['end']-captions[ids[0]]['start']>9 or sum(len(captions[k]['text']) for k in ids)+len(c['text'])>56):
            groups.append(ids);ids=[]
        ids.append(i)
    if ids:groups.append(ids)
    out=[]
    for j,ids in enumerate(groups):
        text=''.join(captions[i]['text'] for i in ids)
        layout='hook' if j==0 else 'close' if j==len(groups)-1 else 'process' if re.search('首先|其次|第一|第二|最后',text) else 'contrast' if re.search('不是|但是|而是',text) and len(ids)<=3 else ('quote','points','paper')[j%3]
        out.append(dict(ids=ids,layout=layout))
    return out

def compile_choices(choices,captions,duration):
    validate_choices({'scenes':choices},captions)
    out=[]
    for j,c in enumerate(choices):
        blocks=[dict(captions[i]) for i in c['ids']]
        a=0. if j==0 else blocks[0]['start'];b=captions[choices[j+1]['ids'][0]]['start'] if j+1<len(choices) else duration
        layout=c['layout'];geo=dict(GEOMETRIES[j%len(GEOMETRIES)])
        if layout=='paper':geo=dict(GEOMETRIES[4])
        out.append(dict(id='scene-%03d'%j,start=a,end=b,layout=layout,geo=geo,heading='',blocks=blocks))
    return out

def external_scenes(scenes,captions,duration):
    out=[];previous=0.
    kinds={'quote':'quote','summary':'paper','points':'points','process':'process','contrast':'contrast','talking':'quote'}
    norm=lambda s:re.sub(r'\s+','',str(s))
    for j,s in enumerate(scenes):
        a=float(s['start']);b=float(s['end'])
        if not (math.isfinite(a) and math.isfinite(b) and 0<=a<b<=duration+.05) or a<previous-.001:raise ValueError('导演场景越界或重叠')
        blocks=[]
        for block in s.get('blocks') or []:
            t0=max(a,float(block['start']));t1=min(b,float(block['end']));text=str(block['text'])
            source=''.join(c['text'] for c in captions if c['start']<t1+.001 and c['end']>t0-.001)
            if t1<=t0 or not text or norm(text) not in norm(source):raise ValueError('导演重点不是对应时间的原话')
            blocks.append(dict(start=t0,end=t1,text=text))
        if not blocks:blocks=[dict(c) for c in captions if c['start']<b and c['end']>a][:3]
        if not blocks:raise ValueError('导演场景没有对应台词')
        if len(blocks)>4:raise ValueError('单镜最多四个重点，请拆分场景')
        layout=kinds.get(s.get('kind'),'quote');geo=dict(GEOMETRIES[j%len(GEOMETRIES)])
        if layout=='paper':geo=dict(GEOMETRIES[4])
        # Gaps retain the previous composition; voice/captions are never stretched.
        if out:out[-1]['end']=a
        heading=s.get('heading');heading=str(heading.get('text','')) if isinstance(heading,dict) else ''
        if heading and norm(heading) not in norm(''.join(c['text'] for c in captions if c['start']<b and c['end']>a)):raise ValueError('导演小标题脱离原话')
        out.append(dict(id=str(s.get('id','scene-%03d'%j)),start=0. if not out else a,end=b,layout=layout,geo=geo,heading=heading,blocks=blocks));previous=b
    if not out:raise ValueError('导演没有有效场景')
    out[-1]['end']=duration
    return out

class YuNetORT:
    """YuNet equations verified against OpenCV face_detect.cpp (Apache-2.0).
    Uses bundled onnxruntime; does not require OpenCV installation in DeepWrap.
    """
    def __init__(self,model):
        import onnxruntime as ort
        opts=ort.SessionOptions();opts.intra_op_num_threads=2;opts.inter_op_num_threads=1
        self.session=ort.InferenceSession(Path(model).read_bytes(),sess_options=opts,providers=['CPUExecutionProvider'])
        self.name=self.session.get_inputs()[0].name
        self.size=self.session.get_inputs()[0].shape[-1]
        if self.size!=640:raise ValueError('需要已验证的640×640 YuNet模型')
    def detect(self,image):
        import numpy as np
        blob=np.zeros((1,3,640,640),np.float32);blob[0,:,:360,:640]=image.transpose(2,0,1)
        outputs=dict(zip([x.name for x in self.session.get_outputs()],self.session.run(None,{self.name:blob})))
        faces=[]
        for stride in (8,16,32):
            scores=np.sqrt(np.clip(outputs['cls_'+str(stride)].reshape(-1),0,1)*np.clip(outputs['obj_'+str(stride)].reshape(-1),0,1))
            bbox=outputs['bbox_'+str(stride)].reshape(-1,4)
            for idx in np.flatnonzero(scores>=.7):
                y,x=divmod(int(idx),640//stride);dx,dy,dw,dh=bbox[idx]
                cx=(x+float(dx))*stride;cy=(y+float(dy))*stride;w=math.exp(float(dw))*stride;h=math.exp(float(dh))*stride
                if 0<cx<640 and 0<cy<360:faces.append([cx-w/2,cy-h/2,w,h,float(scores[idx])])
        keep=[]
        for f in sorted(faces,key=lambda z:z[-1],reverse=True):
            def iou(g):
                inter=max(0,min(f[0]+f[2],g[0]+g[2])-max(f[0],g[0]))*max(0,min(f[1]+f[3],g[1]+g[3])-max(f[1],g[1]));return inter/max(1e-6,f[2]*f[3]+g[2]*g[3]-inter)
            if all(iou(g)<=.3 for g in keep):keep.append(f)
        return None,np.array(keep,np.float32) if keep else None

def face_track(video,start,duration,ffmpeg,model,env):
    import numpy as np
    det=YuNetORT(model)
    cmd=[str(ffmpeg),'-v','error','-ss',str(start),'-i',str(video),'-t',str(duration),'-vf','fps=6,scale=640:360','-f','rawvideo','-pix_fmt','bgr24','pipe:1']
    p=subprocess.Popen(cmd,env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    rows=[];last=None
    try:
        while True:
            raw=p.stdout.read(640*360*3)
            if len(raw)!=640*360*3:break
            _,faces=det.detect(np.frombuffer(raw,np.uint8).reshape(360,640,3))
            chosen=None
            if faces is not None:
                # Foreground subject first, nearby candidates preferred over background faces.
                f=max(faces,key=lambda z:float(z[2]*z[3])/(1+5*((z[0]+z[2]/2)/640-last['x'])**2) if last else float(z[2]*z[3]))
                chosen=dict(x=float((f[0]+f[2]/2)/640),y=float((f[1]+f[3]/2)/360),w=float(f[2]/640),h=float(f[3]/360));last=chosen
            rows.append(dict(t=len(rows)/6,face=chosen))
    finally:
        p.stdout.close();code=p.wait()
    if code:raise RuntimeError('人脸采样解码失败')
    detected=sum(r['face'] is not None for r in rows)
    if not rows or detected/len(rows)<.90:raise ValueError('人脸有效检出不足90%，请换单人口播净底片或手动核对取景，未强行出片')
    first=next(r['face'] for r in rows if r['face']);prev=first
    for r in rows:
        prev=r['face'] or prev;r['face']=prev
    smooth=[]
    for i,r in enumerate(rows):
        near=rows[max(0,i-3):i+4]
        smooth.append(dict(t=r['t'],**{k:sum(q['face'][k] for q in near)/len(near) for k in ('x','y','w','h')}))
    return smooth,dict(samples=len(rows),detected=detected,ratio=detected/len(rows),method='YuNet ONNX Runtime 6Hz, foreground continuity + 7-sample smooth')

def find_music():
    env=os.getenv('RS_EVA_BGM_DIR','').strip()
    config=ROOT/'local-assets.json'
    folder=Path(env) if env else None
    if folder is None and config.is_file():folder=Path(json.loads(config.read_text(encoding='utf-8'))['bgmDirectory'])
    if folder and folder.is_dir():
        for name in ('舒缓BGM.mp3','放松鼓点钢琴.WAV','通用.mp3'):
            p=folder/name
            if p.is_file():return p
    return None

def mix_audio(video,silent,out,ffmpeg,duration,start=0.,music='auto',events=None,env=None):
    import numpy as np
    sr=48000;count=round(duration*sr)
    raw=run([ffmpeg,'-v','error','-ss',start,'-i',video,'-t',duration,'-vn','-ar',sr,'-ac',2,'-f','f32le','pipe:1'],env)
    voice=np.frombuffer(raw,dtype='<f4').reshape(-1,2).copy();voice=np.pad(voice,((0,max(0,count-len(voice))),(0,0)))[:count];fx=np.zeros_like(voice)
    for n,e in enumerate(events or []):
        at=float(e['t']);g=min(.025,max(0,float(e.get('g',.020))));kind=e.get('type','dock')
        t=np.arange(int((.09 if kind=='tick' else .32 if kind=='chime' else .18)*sr))/sr
        sig=(np.sin(2*np.pi*1120*t)*np.exp(-58*t) if kind=='tick' else (np.sin(2*np.pi*660*t)+.4*np.sin(2*np.pi*990*t))*np.exp(-13*t) if kind=='chime' else np.sin(2*np.pi*(330*t-200*t*t))*np.exp(-28*t))*np.minimum(1,t/.006)*g
        i=round(at*sr);n=min(len(sig),len(fx)-i)
        if i>=0 and n>0:fx[i:i+n]+=sig[:n,None]
    audio_dir=Path(out).parent/'audio';audio_dir.mkdir(exist_ok=True)
    def wav(name,x):
        with wave.open(str(audio_dir/name),'wb') as w:w.setnchannels(2);w.setsampwidth(2);w.setframerate(sr);w.writeframes((np.clip(x,-1,1)*32767).astype('<i2').tobytes())
    track=find_music() if music not in ('none','off',False) else None
    bg=np.zeros_like(voice);duck_min=1.;duck_max=1.
    if track:
        raw=run([ffmpeg,'-v','error','-stream_loop','-1','-i',track,'-t',duration,'-vn','-af','loudnorm=I=-30:TP=-6:LRA=7','-ar',sr,'-ac',2,'-f','f32le','pipe:1'],env)
        bg=np.frombuffer(raw,dtype='<f4').reshape(-1,2).copy();bg=np.pad(bg,((0,max(0,count-len(bg))),(0,0)))[:count]
        block=960;controls=[];gain=1.
        for i in range(0,count,block):
            rms=float(np.sqrt(np.mean(voice[i:i+block]**2)))
            target=.38 if rms>.018 else 1.
            alpha=.65 if target<gain else .045;gain+=alpha*(target-gain);controls.append(gain)
        duck=np.interp(np.arange(count),np.arange(len(controls))*block,controls);fade=np.minimum(1,np.arange(count)/sr/1.2)*np.minimum(1,(count-1-np.arange(count))/sr/min(2.5,duration/3));bg*=duck[:,None]*fade[:,None]
        duck_min=float(min(controls));duck_max=float(max(controls))
    mixed=voice+fx+bg;peak=float(np.abs(mixed).max());gain=min(1,.96/max(.00001,peak));mixed*=gain
    wav('voice.wav',voice);wav('sfx.wav',fx);wav('bgm-ducked.wav',bg);wav('mixed.wav',mixed)
    run([ffmpeg,'-v','error','-n','-i',silent,'-i',audio_dir/'mixed.wav','-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','192k','-t',duration,'-movflags','+faststart',out],env)
    report=dict(musicMixed=track is not None,musicTitle=track.stem if track else None,musicRequested=music,musicMissing=music not in ('none','off',False) and track is None,localOnly=True,distribution='do not bundle local music',duckGainMin=duck_min,duckGainMax=duck_max,voicePeak=float(np.abs(voice).max()),bgmPeak=float(np.abs(bg).max()),sfxPeak=float(np.abs(fx).max()),mixPeakBefore=peak,finalGain=gain,sfxMixed=bool(events),paidCalls=0,subjectiveListening='待用户试听')
    dump(Path(out).with_suffix('.audio.json'),report);return report

def render(job,tools,log=print,env=None):
    env=dict(os.environ if env is None else env)
    work=Path(job['work']).resolve()
    if work.exists():raise FileExistsError('保留已有任务，需使用新目录：'+str(work))
    work.mkdir(parents=True)
    tmp=work/'child-temp';tmp.mkdir();env.update(TEMP=str(tmp),TMP=str(tmp),TMPDIR=str(tmp))
    video=Path(job['video']).resolve(strict=True);ffmpeg=tools['ffmpeg']
    probe=json.loads(run([tools['ffprobe'],'-v','error','-show_format','-show_streams','-of','json',video],env))
    vs=next(s for s in probe['streams'] if s.get('codec_type')=='video');source_duration=float(probe['format']['duration'])
    start=float(job.get('start',0));requested=float(job.get('duration',source_duration-start));fps=30
    if not math.isfinite(requested) or not math.isfinite(start) or start<0 or requested<=0 or requested+start>source_duration+.05:raise ValueError('片段时间越界')
    count=max(1,math.ceil(requested*fps-1e-7));duration=count/fps
    caps=clean_captions(job.get('captions') or [],requested)
    if job.get('content_scenes') is not None:
        scenes=external_scenes(job['content_scenes'],caps,requested);director=dict(mode='validated-system-director',fallback=False)
    else:
        choices=job.get('director_choices');mode=job.get('director_mode')
        if choices is not None: choices=validate_choices(choices,caps);director=dict(mode=mode or 'provided-director',fallback=mode=='source-rule-fallback',reason=job.get('director_fallback_reason',''))
        else:choices=rule_choices(caps);director=dict(mode='source-rule-fallback',fallback=True,reason=job.get('director_fallback_reason') or '未提供经过校验的AI分镜；保守原文布局')
        scenes=compile_choices(choices,caps,requested)
    scenes[-1]['end']=duration
    dump(work/'director-report.json',dict(**director,templateVersion=VERSION,scenes=scenes))
    shutil.copytree(ROOT/'template',work/'template')
    log('[EVA] 跟踪前景人脸，圆形/方形变化保持脸在中央')
    faces,face_report=face_track(video,start,requested,ffmpeg,tools['faceModel'],env)
    dump(work/'face-track-report.json',face_report)
    presenter=work/'host-hd';presenter.mkdir()
    w=min(1920,int(vs['width']));w=w//2*2;h=round(w*int(vs['height'])/int(vs['width'])/2)*2
    run([ffmpeg,'-v','error','-n','-ss',start,'-i',video,'-t',requested,'-vf',f'fps=30,scale={w}:{h},tpad=stop_mode=clone:stop_duration=0.1','-frames:v',count,'-q:v','2','-start_number','0',presenter/'%06d.jpg'],env)
    if not (presenter/('%06d.jpg'%(count-1))).is_file():raise RuntimeError('人像帧不足，不能截短交付')
    input=dict(profile='rensheng.talking/1',templateId=TEMPLATE_ID,version=VERSION,width=1920,height=1080,outputWidth=int(job.get('width',1920)),fps=fps,duration=duration,frameCount=count,host=dict(width=w,height=h,fps=fps,count=count,pattern='host-hd/%06d.jpg'),title=str(job.get('title') or video.stem),author=str(job.get('brand') or '本期分享'),captions=caps,faces=faces,scenes=scenes,reviewFlags=[])
    dump(work/'pack-input.json',input);dump(work/'runner-config.json',tools)
    if job.get('prepare_only'):return dict(instance=str(work),work=str(work),prepared=True)
    log('[EVA] 逐帧渲染 %s 秒，导演来源 %s'%(round(duration,3),director['mode']))
    silent=work/'visual.mp4'
    run([tools['node'],ROOT/'render.cjs','--root',work,'--render','--out',silent],env)
    if job.get('silent_only'):
        recipe=work/'recipe.json';dump(recipe,dict(template=TEMPLATE_ID,templateVersion=VERSION,director=director,faces=face_report,source=str(video),duration=duration))
        return dict(video=str(silent),instance=str(work),work=str(work),recipe=str(recipe),director=director)
    events=[] if job.get('sfx') in ('off',False) else ([dict(t=float(e['t']),type={'shine':'chime','typing':'tick'}.get(e.get('type'),e.get('type','dock')),g=.02*min(1.,max(0.,float(e.get('g',1))))) for e in job['sfx_events']] if 'sfx_events' in job else [dict(t=max(0,s['start']-.1),type=('dock','tick','chime')[i%3],g=.020) for i,s in enumerate(scenes[1:])])
    output=work/'video.mp4';audio=mix_audio(video,silent,output,ffmpeg,duration,start,job.get('bgm','auto'),events,env)
    out_probe=json.loads(run([tools['ffprobe'],'-v','error','-show_format','-show_streams','-of','json',output],env))
    actual=float(out_probe['format']['duration'])
    if abs(actual-requested)>1/fps+.025:raise RuntimeError('导出时长核对失败')
    if not any(s['codec_type']=='audio' for s in out_probe['streams']):raise RuntimeError('缺少原声音轨')
    recipe=work/'recipe.json';dump(recipe,dict(profile='motion-three.recipe/1',template=TEMPLATE_ID,templateVersion=VERSION,source=str(video),start=start,duration=actual,sourceDuration=requested,frameCount=count,faces=face_report,director=director,audio=audio,music_mixed=audio['musicMixed'],sfx_mixed=audio['sfxMixed']))
    result=dict(video=str(output),instance=str(work),work=str(work),recipe=str(recipe),music_mixed=audio['musicMixed'],sfx_mixed=audio['sfxMixed'],director=director)
    dump(work/'result.json',result);log('[EVA] 成片 '+str(output));return result
