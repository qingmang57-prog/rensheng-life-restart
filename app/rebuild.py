"""成片重建（Rebuild）：把源时间保留区间，变成一条可直接发布的口播成片。

只做决策，不碰 ffmpeg——渲染在 media.render_rebuild / media.normalize_loudness。

口径来源：`剪辑/剪辑逻辑.md`（单一真源）。参数冲突以 `参考库_20261031/reference_pack.json` 为准。
纯标准库实现，不引入 numpy：主进程要保持零依赖。
"""
import math

# 来自 10 条人剪成片实测反推，见 剪辑逻辑.md §3
PARAMS={
    'avg_seg_s':7.2,        # 平均段长目标；切口预算 = 目标秒数 ÷ 它
    'speed_max':1.25,       # 变速硬顶，再快出"赶稿感"
    'speed_min':1.00,
    'min_seg_s':1.00,       # 单段绝对下限
    'min_block_s':2.00,     # 合并后块长下限，低于它并到邻块
    'gap_merge_s':0.60,     # 间隙小于它的相邻块先连通
    'merge_gap_s':2.20,     # 碎块并邻允许的最大间隙
    'snap_window_s':0.35,   # 切点吸附搜索窗口
    'ear_s':0.06,           # 吸附后外扩，吃掉 ASR 字头偏晚
    'delete_ratio_lo':0.30,
    'delete_ratio_hi':0.55,
    'cps_target':5.09,      # 不直接约束剪辑，只用于事后核对
}

GATE={
    'quiet_ratio_max':0.06,     # 干声态静音占比
    'longest_gap_max_s':1.00,   # 干声态最长空隙
    'shortest_seg_min_s':1.00,
    'budget_tolerance':0.20,    # 段数可超预算的上限比例
    'lufs_lo':-10.5, 'lufs_hi':-8.5,
    'lra_max':4.0,
}


class Unreachable(ValueError):
    """目标时长在语速上限内不可达——只许少删内容或放宽 target，不许拉倍速。"""


def _dur(seg):return seg['end_ms']-seg['start_ms']


def govern(segments, p=None):
    """段治理：先把极近的间隙连通，再把碎块并到邻块。

    口播的呼吸单元通常 0.6s 以内，把一个气口拆成两段只会多两个接头。
    """
    p={**PARAMS,**(p or {})}
    segs=sorted([{'start_ms':int(s['start_ms']),'end_ms':int(s['end_ms'])} for s in segments],
                key=lambda x:x['start_ms'])
    if not segs:return []
    out=[dict(segs[0])]
    for s in segs[1:]:
        if s['start_ms']-out[-1]['end_ms']<p['gap_merge_s']*1000:
            out[-1]['end_ms']=max(out[-1]['end_ms'],s['end_ms'])
        else:
            out.append(dict(s))
    # 碎块并邻：反复处理直到稳定，避免一次扫描漏掉连锁
    for _ in range(12):
        changed=False
        i=0
        while i<len(out):
            b=out[i]
            if _dur(b)>=p['min_block_s']*1000 or len(out)==1:
                i+=1;continue
            prev_gap=(b['start_ms']-out[i-1]['end_ms']) if i>0 else 9e9
            next_gap=(out[i+1]['start_ms']-b['end_ms']) if i<len(out)-1 else 9e9
            if min(prev_gap,next_gap)>p['merge_gap_s']*1000:
                i+=1;continue
            if prev_gap<=next_gap and i>0:
                out[i-1]['end_ms']=b['end_ms'];out.pop(i)
            else:
                out[i+1]['start_ms']=b['start_ms'];out.pop(i)
            changed=True
        if not changed:break
    return [s for s in out if _dur(s)>=400]


def budget(segments, target_s, p=None):
    """切口预算：段数上限 = 目标成片秒数 ÷ 平均段长目标。

    这是整条链路最值钱的一刀：切口数由"成片想多长、每段多长"决定，
    不由"删了几处"决定。每删一处就多两个接头。
    """
    p={**PARAMS,**(p or {})}
    limit=max(3,int(round(target_s/p['avg_seg_s'])))
    rows=[dict(s) for s in segments]
    merges=[]
    while len(rows)>limit and len(rows)>1:
        gaps=[(rows[i+1]['start_ms']-rows[i]['end_ms'],i) for i in range(len(rows)-1)]
        gaps.sort()
        gap,i=gaps[0]
        merged={'start_ms':rows[i]['start_ms'],'end_ms':rows[i+1]['end_ms']}
        merges.append({'at_ms':rows[i]['end_ms'],'gap_ms':int(gap)})
        rows=rows[:i]+[merged]+rows[i+2:]
    return rows,limit,merges


def snap(segments, silences, p=None):
    """切点吸附：把段界推进最近的静音里，再外扩一点点吃字头误差。

    silences 来自 ffmpeg silencedetect（毫秒区间）。ASR 时间戳不能当切点用——
    字尾吞音、相邻词重叠，只有"这里有没有声音"能回答从哪切。
    """
    p={**PARAMS,**(p or {})}
    win=int(p['snap_window_s']*1000);ear=int(p['ear_s']*1000)
    gaps=[(int(a),int(b)) for a,b in silences]
    if not gaps:return [dict(s) for s in segments]

    def inside(t):
        for a,b in gaps:
            if a<=t<=b:return (a,b)
        return None

    def before(t):
        best=None
        for a,b in gaps:
            if b<=t and t-b<=win:
                if best is None or b>best[1]:best=(a,b)
        return best

    def after(t):
        best=None
        for a,b in gaps:
            if a>=t and a-t<=win:
                if best is None or a<best[0]:best=(a,b)
        return best

    rows=[dict(s) for s in segments]
    for i,r in enumerate(rows):
        lo=rows[i-1]['end_ms']+400 if i>0 else 0
        hi=rows[i+1]['start_ms']-400 if i<len(rows)-1 else 10**9
        # 段首：落到某个静音的起点（已在静音里就不动）
        if inside(r['start_ms']) is None:
            cands=[]
            b=before(r['start_ms'])
            if b and r['start_ms']-b[0]<=win:cands.append(b[0])
            a=after(r['start_ms'])
            if a and a[0]-r['start_ms']<=win:cands.append(a[0])
            if cands:
                cand=min(cands,key=lambda x:abs(x-r['start_ms']))
                if lo<=cand<=hi:r['start_ms']=cand
        # 段尾：落到某个静音的终点
        if inside(r['end_ms']) is None:
            cands=[]
            b=before(r['end_ms'])
            if b and r['end_ms']-b[1]<=win:cands.append(b[1])
            a=after(r['end_ms'])
            if a and a[1]-r['end_ms']<=win:cands.append(a[1])
            if cands:
                cand=min(cands,key=lambda x:abs(x-r['end_ms']))
                if lo<=cand<=hi:r['end_ms']=cand
    # 外扩 + 邻段避让
    for i,r in enumerate(rows):
        lo=rows[i-1]['end_ms']+40 if i>0 else 0
        hi=rows[i+1]['start_ms']-40 if i<len(rows)-1 else 10**9
        r['start_ms']=max(lo,r['start_ms']-ear)
        r['end_ms']=min(hi,r['end_ms']+ear)
    return [s for s in rows if _dur(s)>=400]


def assign_speed(segments, target_s, p=None, first_seg_slow=0.0):
    """由目标时长反推基准倍速，触顶就抛 Unreachable。

    宁可多删内容，不许把语速拉到破人设。首段可给一点减速（开头第一句要慢一点进）。
    """
    p={**PARAMS,**(p or {})}
    net=sum(_dur(s) for s in segments)/1000.0
    if not segments:raise ValueError('没有可用段落。')
    base=net/target_s if target_s else 1.0
    base=max(p['speed_min'],min(p['speed_max'],base))
    reachable=net/p['speed_max']
    if reachable>target_s*1.05:
        raise Unreachable(
            f'目标不可达：净保留 {net:.0f}s ÷ 上限 {p["speed_max"]:.2f}x = 最短 {reachable:.0f}s，'
            f'超过目标 {target_s:.0f}s。两条路：①多删内容 ②把目标放宽到 ≥{reachable:.0f}s。不许拉倍速凑时长。')
    rows=[dict(s) for s in segments]
    for i,r in enumerate(rows):
        sp=base
        if i==0 and first_seg_slow:
            sp=max(p['speed_min'],base-first_seg_slow)
        r['speed']=round(min(p['speed_max'],sp),4)
    return rows,round(base,4),round(net,2)


def check_reachable(segments, target_s, p=None):
    """先判可达，再谈切几刀。

    合并相邻段 = 少删一刀，代价是把中间内容放回来，净时长只会变长。
    所以"合并"永远解决不了"内容超量"，只能在时长有富余时用来降切口密度。
    """
    p={**PARAMS,**(p or {})}
    net=sum(_dur(s) for s in segments)/1000.0
    reachable=net/p['speed_max']
    if reachable>target_s*1.05:
        raise Unreachable(
            f'内容超量：保留 {net:.0f}s，即使按语速上限 {p["speed_max"]:.2f}x 压缩，'
            f'最短也还有 {reachable:.0f}s，超过目标 {target_s:.0f}s。'
            f'两条路：①回清理多删内容 ②把目标放宽到 ≥{reachable:.0f}s。不许拉倍速凑时长。')
    return net,reachable


def build(segments, silences, target_s, params=None):
    """总入口：段治理 → 可达性 → 切口预算 → 边界吸附 → 变速。返回可直接渲染的 plan。"""
    p={**PARAMS,**(params or {})}
    segments=[s for s in segments if int(s.get('end_ms',0))-int(s.get('start_ms',0))>=400]
    if not segments:raise ValueError('没有有效片段可剪：保留区间为空或过短。')
    governed=govern(segments,p)
    check_reachable(governed,target_s,p)
    capped,limit,merges=budget(governed,target_s,p)
    if sum(_dur(s) for s in capped)/1000.0/p['speed_max']>target_s*1.05:
        # 合并后反而到不了目标：宁可刀密一点，也不丢时长
        capped,limit,merges=governed,len(governed),[]
    snapped=snap(capped,silences,p)
    rows,base,net=assign_speed(snapped,target_s,p)
    out_s=sum(_dur(r)/1000.0/r['speed'] for r in rows)
    return {'plan':rows,'params':p,
            'summary':{'input_segments':len(segments),'governed':len(governed),
                       'budget_limit':limit,'segments':len(rows),'cuts':max(0,len(rows)-1),
                       'net_seconds':net,'base_speed':base,'estimated_seconds':round(out_s,2),
                       'target_seconds':round(target_s,2),'merged_by_budget':len(merges),
                       'avg_seg_s':round(out_s/max(1,len(rows)),2)}}


def judge_structure(summary, dry_stats=None):
    """结构闸：段数/最短段/干声态静音。dry_stats 为空时只判结构。"""
    fails=[];warns=[]
    if summary['segments']>summary['budget_limit']*(1+GATE['budget_tolerance']):
        fails.append(f'段数 {summary["segments"]} 超过预算 {summary["budget_limit"]} 的 20%')
    if summary['estimated_seconds']>summary['target_seconds']*1.05:
        fails.append(f'预估 {summary["estimated_seconds"]:.0f}s 超目标 {summary["target_seconds"]:.0f}s')
    if summary['base_speed']>=PARAMS['speed_max']-1e-6:
        warns.append('基准倍速已触顶，语速到极限，建议少删一点留余量')
    if dry_stats:
        if dry_stats['quiet_ratio']>GATE['quiet_ratio_max']:
            fails.append(f'干声态静音占比 {dry_stats["quiet_ratio"]*100:.1f}% 超 {GATE["quiet_ratio_max"]*100:.0f}%')
        if dry_stats['longest_gap_s']>GATE['longest_gap_max_s']:
            fails.append(f'干声态最长空隙 {dry_stats["longest_gap_s"]:.2f}s 超 {GATE["longest_gap_max_s"]:.2f}s')
    return {'ok':not fails,'fails':fails,'warns':warns}


def judge_loudness(measured):
    """响度闸：交付态。LUFS 是硬的，LRA 越界只提示。"""
    fails=[];warns=[]
    if measured is None:
        fails.append('读不到响度')
        return {'ok':False,'fails':fails,'warns':warns}
    if not (GATE['lufs_lo']<=measured['lufs']<=GATE['lufs_hi']):
        fails.append(f'响度 {measured["lufs"]} LUFS 不在 {GATE["lufs_lo"]}~{GATE["lufs_hi"]}')
    if measured['lra']>GATE['lra_max']:
        warns.append(f'动态 LRA {measured["lra"]} 超 {GATE["lra_max"]}，段落感偏重')
    return {'ok':not fails,'fails':fails,'warns':warns}
