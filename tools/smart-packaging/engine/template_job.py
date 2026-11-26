"""Reusable three-reference templates on the existing MotionGit renderAt frame pump.
Local only. Never deletes or overwrites an existing work directory.
"""
from pathlib import Path
import json, math, os, re, shutil, subprocess, sys
ENGINE = Path(__file__).resolve().parent
LIB = ENGINE.parent / 'templates' / 'motion-three'
BASE = ENGINE.parent.parent  # 包内 tools/


def _node_exe():
    """包内 node 优先；环境变量次之；最后才用系统 PATH。"""
    for cand in (os.environ.get('DW_NODE'), os.environ.get('IP_PACKAGING_NODE'),
                 str(BASE / 'node/bin/node.exe'), str(BASE / 'node/node.exe')):
        if cand and Path(cand).is_file():
            return cand
    return shutil.which('node') or ''


def _chrome_exe():
    """包内 chromium 优先；环境变量次之；最后才找系统 Chrome/Edge。"""
    for key in ('IP_PACKAGING_CHROME', 'CHROME_PATH'):
        value = os.environ.get(key, '')
        if value and Path(value).is_file():
            return value
    for root in (BASE.parent / 'app/.runtime/browser-env', BASE / 'chromium'):
        if not root.is_dir():
            continue
        for name in ('chrome.exe', 'headless_shell.exe'):
            for exe in sorted(root.rglob(name)):
                if exe.is_file():
                    return str(exe)
    for cand in (r'C:/Program Files/Google/Chrome/Application/chrome.exe',
                 r'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
                 r'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'):
        if Path(cand).is_file():
            return cand
    return ''

def run_template_job(job, log=print):
    catalog = json.loads((LIB / 'catalog.json').read_text(encoding='utf-8'))
    key = job['template']
    if job.get('music'):
        raise ValueError('This template route preserves original audio; music mixing is not enabled')
    if key not in catalog['templates']:
        raise ValueError('Unknown template: ' + str(key))
    spec = catalog['templates'][key]
    video = Path(job['video']).resolve(strict=True)
    env = dict(os.environ)
    ffdir = env.get('DW_FFMPEG', '') or str(BASE / 'ffmpeg/bin')
    if Path(ffdir).is_dir(): env['PATH'] = ffdir + os.pathsep + env.get('PATH', '')
    def run(args):
        p = subprocess.run([str(x) for x in args], env=env, capture_output=True,
                           encoding='utf-8', errors='replace')
        if p.returncode: raise RuntimeError(p.stderr[-2500:] or p.stdout[-2500:])
        return p.stdout
    probe = json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',video]))
    source_duration = float(probe['format']['duration'])
    start = float(job.get('start',0))
    fps = float(job.get('fps',30))
    requested = float(job.get('duration',source_duration-start))
    if not all(math.isfinite(x) for x in (start,fps,requested)) or not 1 <= fps <= 60 or start < 0 or requested <= 0 or start+requested > source_duration+.01:
        raise ValueError('Invalid start/duration/fps, or requested clip exceeds source')
    count = max(1,round(requested*fps)); duration = count/fps
    width = int(job.get('width',spec['width']))
    if width < 320 or width > 3840 or width % 2: raise ValueError('width must be even, 320..3840')
    height = round(width*spec['height']/spec['width']/2)*2
    captions = job.get('captions',[])
    if not isinstance(captions,list): raise ValueError('captions must be [{start,end,text}]')
    if job.get('subtitles'):
        text = Path(job['subtitles']).resolve(strict=True).read_text(encoding='utf-8-sig')
        def stamp(s):
            h,m,sec = s.replace(',','.').split(':'); return int(h)*3600+int(m)*60+float(sec)
        captions=[]
        for block in re.split(r'\r?\n\s*\r?\n',text.strip()):
            lines=block.splitlines(); at=next((i for i,x in enumerate(lines) if '-->' in x),None)
            if at is None: continue
            a,b=lines[at].split('-->')
            captions.append(dict(start=stamp(a.strip()),end=stamp(b.strip()),text='\n'.join(lines[at+1:])))
    for c in captions:
        if not isinstance(c.get('text'),str) or not 0 <= float(c['start']) < float(c['end']) <= duration+.05:
            raise ValueError('Caption times use clip-relative seconds and must fit output')
    items=job.get('items',[])
    if not isinstance(items,list) or len(items)>spec['maxItems']: raise ValueError('Too many template items')
    textmap=job.get('text',{})
    if not isinstance(textmap,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in textmap.items()):
        raise ValueError('text must map CSS selectors to plain text')
    work=Path(job.get('work') or (video.parent/('motion-'+key+'-'+video.stem))).resolve()
    if work.exists(): raise FileExistsError('Choose a new work folder; preserving existing: '+str(work))
    work.mkdir(parents=True)
    inst=work/'instance'; shutil.copytree(LIB/spec['folder'],inst)
    (work/'job.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
    log('[template] '+spec['name']+' / existing renderAt engine')
    # Flatten HyperFrames subcompositions into scoped native DOM sections.
    html=(inst/'index.html').read_text(encoding='utf-8')
    html=re.sub(r'<script>(.*?)</script>',lambda m:'<script>(()=>{'+m[1]+'})();</script>',html,flags=re.S)
    scene_scripts=[]
    for name in ('thesis','method','progress') if key=='boundary' else ():
        sub=(inst/'compositions'/f'{name}.html').read_text(encoding='utf-8')
        styles=''.join(re.findall(r'<style>(.*?)</style>',sub,re.S)).replace('#root','#scene-'+name)
        scripts=re.findall(r'<script>(.*?)</script>',sub,re.S)
        markup=re.sub(r'<style>.*?</style>|<script>.*?</script>|</?template>', '',sub,flags=re.S)
        markup=markup.replace('id="root"','id="scene-'+name+'"')
        pattern=r'(<div id="'+name+r'"[^>]*>)(</div>)'
        html,n=re.subn(pattern,lambda m:m[1]+'<style>'+styles+'</style>'+markup+m[2],html)
        if n!=1: raise RuntimeError('Composition mount mismatch: '+name)
        scene_scripts += ['<script>(()=>{'+s+'})();</script>' for s in scripts]
    html=html.replace('</body>',''.join(scene_scripts)+'</body>')
    # Presenter video is decoded into deterministic frames. No browser seek/freeze path.
    html=re.sub(r'<video\b([^>]*)></video>',lambda m:'<img'+re.sub(r'\s(?:src="[^"]*"|muted|playsinline)','',m[1])+' alt="">',html)
    html=html.replace('.window video{','.window #presenter{')
    presenter=inst/'frames'; presenter.mkdir()
    if key!='dark-matrix':
        log('[media] Extract continuous presenter frames')
        run(['ffmpeg','-v','error','-n','-ss',start,'-i',video,'-vf',f'fps={fps},scale=720:-2,tpad=stop_mode=clone:stop_duration=1',
             '-frames:v',count,'-q:v','3',presenter/'%06d.jpg'])
        missing=[i for i in (1,count) if not (presenter/f'{i:06d}.jpg').exists()]
        if missing: raise RuntimeError('Presenter frame sequence incomplete')
    bound_items=[]
    for i,item in enumerate(items):
        item={'title':item} if isinstance(item,str) else dict(item)
        if item.get('image'):
            p=Path(item['image']).resolve(strict=True)
            if p.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'): raise ValueError('Unsupported image format')
            dest=inst/'assets'/f'item-{i}{p.suffix.lower()}'; shutil.copy2(p,dest)
            item['image']=dest.relative_to(inst).as_posix()
        bound_items.append(item)
    config=dict(template=key,duration=duration,fps=fps,frames=count,brand=job.get('brand'),
                title=job.get('title'),text=textmap,items=bound_items,captions=captions,
                objectPosition=job.get('object_position','50% 50%'))
    (inst/'template-job.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    bootstrap=(LIB/'native-runtime.js').read_text(encoding='utf-8')
    css=f'<style>html,body{{width:{width}px;height:{height}px}}#root{{width:{spec["width"]}px;height:{spec["height"]}px;transform:scale({width/spec["width"]});transform-origin:0 0}}[data-composition-src]{{inset:0}}.native-caption{{white-space:pre-line}}.caption{{overflow-wrap:anywhere}}</style>'
    html=html.replace('</head>',css+'</head>').replace('</body>','<script>'+bootstrap+'</script></body>')
    (inst/'index.html').write_text(html,encoding='utf-8')
    settings=dict(profile='motion-three.instance/1',family=key,width=width,height=height,fps=fps,duration=duration)
    (inst/'settings.json').write_text(json.dumps(settings,indent=2),encoding='utf-8')
    out=work/'video.mp4'
    if job.get('prepare_only'):
        result=dict(instance=str(inst),work=str(work),prepared=True)
    else:
        log('[render] Shared MotionGit frame pump')
        node = _node_exe()
        if not node:
            raise RuntimeError('包装运行环境缺失，请重新下载完整安装包。（缺少：Node 运行时）')
        chrome = _chrome_exe()
        if not chrome:
            raise RuntimeError('包装运行环境缺失，请重新下载完整安装包。（缺少：本地浏览器）')
        env['CHROME_PATH'] = chrome
        run([node, ENGINE/'render.mjs', '--root', inst, '--out', out])
        final=out
        if any(s.get('codec_type')=='audio' for s in probe['streams']):
            final=work/'video-with-audio.mp4'
            run(['ffmpeg','-v','error','-n','-i',out,'-ss',start,'-i',video,'-map','0:v:0','-map','1:a:0',
                 '-t',duration,'-c:v','copy','-af','apad','-c:a','aac','-b:a','192k','-movflags','+faststart',final])
        result=dict(video=str(final),instance=str(inst),work=str(work))
    recipe=dict(profile='motion-three.recipe/1',template=key,templateVersion=catalog['version'],
                engine='engine/render.mjs',source=str(video),start=start,duration=duration,
                subtitles='user-provided' if captions else 'none',audio='original voice; no synthetic SFX',
                settings=settings)
    (work/'recipe.json').write_text(json.dumps(recipe,ensure_ascii=False,indent=2),encoding='utf-8')
    result['recipe']=str(work/'recipe.json')
    (work/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    log('[done] '+str(final if not job.get('prepare_only') else inst))
    return result

if __name__ == "__main__":
    task = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print(json.dumps(run_template_job(task), ensure_ascii=False))
