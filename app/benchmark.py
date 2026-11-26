"""Reproducible local resource benchmark; source video is never overwritten."""
import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime,timezone,timedelta
from pathlib import Path

import media
import providers
from resource_probe import hardware,monitored_run,gpu_snapshot

ROOT=Path(__file__).resolve().parent


def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def worker(args):
    video=args.video.resolve();folder=args.out.resolve()
    if args.worker=='audio':media.extract_audio(video,folder/'reference-audio.mp3')
    elif args.worker=='silence':
        plan=media.silence_plan(video,media.probe(video),'natural')
        (folder/'cleanup-plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    elif args.worker=='cut':
        plan=json.loads((folder/'cleanup-plan.json').read_text(encoding='utf-8'))
        mapping=media.render_cut(video,media.probe(video),plan['segments'],folder/'clean-video.mp4')
        (folder/'source-output-map.json').write_text(json.dumps(mapping,ensure_ascii=False,indent=2),encoding='utf-8')
    elif args.worker=='asr':
        transcript=providers.asr(folder/'reference-audio.mp3')
        (folder/'transcript.json').write_text(json.dumps(transcript,ensure_ascii=False,indent=2),encoding='utf-8')


def benchmark(args):
    video=args.video.resolve()
    if not video.is_file():raise ValueError('请指定存在的视频文件。')
    output=(args.out or ROOT/'.runtime/benchmarks'/uuid.uuid4().hex).resolve()
    if output.exists():raise ValueError('输出目录必须是新目录，避免混用旧测量和旧成片。')
    output.mkdir(parents=True)
    info=media.probe(video);source_hash=sha256(video)
    result={'version':'1.0','measured_at':datetime.now(timezone(timedelta(hours=8))).isoformat(),'input':{'filename':video.name,'classification':args.input_kind,'sha256':source_hash,**info},
        'hardware':hardware(),'stages':[],
        'notes':['仅测本机单任务，不能据此承诺云服务器并发。','原文件只读，所有中间文件和输出保存在独立目录。','内存统计为采样到的进程树峰值，可能漏掉短时峰值。','未取得GPU数据不等于这台电脑没有显卡。']}
    print(json.dumps({'status':'started','input_kind':args.input_kind,'duration_ms':info['duration_ms'],'output':str(output)},ensure_ascii=False),flush=True)
    for kind,label in [('audio','音频抽取'),('silence','静音分析'),('cut','音视频清理编码')]:
        if kind=='cut' and not (output/'cleanup-plan.json').is_file():
            result['stages'].append({'label':kind,'status':'skipped','reason':'未产生有效静音方案。'});continue
        stage=monitored_run([sys.executable,str(Path(__file__).resolve()),'--worker',kind,'--video',str(video),'--out',str(output)],output,kind,timeout=900)
        stage['description']=label
        if kind=='cut' and stage['status']=='succeeded':stage['output_video']=media.probe(output/'clean-video.mp4')
        result['stages'].append(stage)
        print(json.dumps({k:v for k,v in stage.items() if k not in ('samples','measurement_note')},ensure_ascii=False),flush=True)
    if args.asr and providers.available()['asr'] and (output/'reference-audio.mp3').is_file():
        stage=monitored_run([sys.executable,str(Path(__file__).resolve()),'--worker','asr','--video',str(video),'--out',str(output)],output,'asr',timeout=600)
        stage['description']='已配置服务的转写客户端';stage['measurement_note']='只测本机音频提交与等待，不代表远端模型的CPU、内存或显存。'
        result['stages'].append(stage)
    else:
        result['stages'].append({'label':'asr','status':'not_measured','reason':'未配置并启用真实ASR；没有安装本地模型。不能给出转写资源数字。'})
    if args.motion_preview or args.motion_render:
        adapter=Path(os.getenv('IP_MOTION_ADAPTER',str(ROOT.parent/'tools'/'motion-adapter'/'pack.py')))
        interpreter=Path(os.getenv('IP_MOTION_PYTHON',str(ROOT.parent/'tools'/'motion-python'/'Scripts'/'python.exe')))
        if adapter.is_file() and interpreter.is_file():
            brief={'title':'资源基准测试','language':'zh','soundEffects':'none','groups':[{'id':'B1','title':'运行测试'},{'id':'B4','title':'记录结果'}],
                'basic':{'keyword':'资源测试','explanation':'测量实际耗时与内存，不猜服务器配置。','summary':'记录结果','action':'等待真实讲稿素材完成验收。'}}
            brief_file=output/'motion-brief.json';brief_file.write_text(json.dumps(brief,ensure_ascii=False),encoding='utf-8')
            command=[str(interpreter),str(adapter),'--brief',str(brief_file),'--out',str(output/'motion')]
            if args.motion_render:command.append('--render')
            stage=monitored_run(command,output,'motion_render' if args.motion_render else 'motion_preview',timeout=1800 if args.motion_render else 300,env=media.runtime_environment())
            expected=output/'motion'/('silent-with-audio.mp4' if args.motion_render else 'preview.png')
            if stage['status']=='succeeded' and not expected.is_file():stage.update(status='failed',reason='没有返回预期产物。')
            stage['description']='纯文字模板探针（未合成本人画面）';stage['reference_video_bound']=False
            stage['measurement_note']+=' 此步未绑定输入视频，不代表完整真人包装耗时。'
            result['stages'].append(stage)
        else:result['stages'].append({'label':'motion_preview','status':'not_measured','reason':'模板适配器或运行环境不可用。'})
    else:result['stages'].append({'label':'motion_render','status':'not_measured','reason':'未执行完整包装；真实人物合成还需要素材和人脸几何。'})
    result['gpu_device_after']=gpu_snapshot()
    result['source_unchanged']=sha256(video)==source_hash
    if not result['source_unchanged']:raise RuntimeError('源文件校验值发生变化，请检查外部编辑。')
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    rows=['# 本机资源测试结果','',f"输入类型：{args.input_kind}；时长 {info['duration_ms']/1000:.2f}s；{info['width']}×{info['height']}。",'',
          '| 环节 | 状态 | 耗时秒 | 观察到的进程树 RSS 峰值 MiB |','|---|---|---:|---:|']
    for s in result['stages']:rows.append(f"| {s.get('description',s['label'])} | {s['status']} | {s.get('wall_seconds','未测')} | {s.get('observed_tree_rss_peak_mib','未测')} |")
    rows+=['','源文件校验未改变。','','## 测量限制','']+['- '+x for x in result['notes']]
    rows+=['','转写未测时不推测其资源；模板预览不等于整片渲染。JSON 内保留采样与逐步日志，便于复核。']
    (output/'report.md').write_text('\n'.join(rows)+'\n',encoding='utf-8')
    print(json.dumps({'status':'completed','result':str(output/'result.json'),'source_unchanged':True},ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description='本机单任务资源基准；不覆盖原文件。')
    parser.add_argument('--video',required=True,type=Path)
    parser.add_argument('--out',type=Path)
    parser.add_argument('--input-kind',choices=['real_talking_head','template_test','synthetic'],default='real_talking_head')
    parser.add_argument('--asr',action='store_true',help='仅在服务已配置时提交音频，可能产生服务调用费用')
    parser.add_argument('--motion-preview',action='store_true')
    parser.add_argument('--motion-render',action='store_true')
    parser.add_argument('--worker',choices=['audio','silence','cut','asr'],help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:worker(args)
    else:benchmark(args)


if __name__=='__main__':main()
