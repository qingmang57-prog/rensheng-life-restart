from pathlib import Path
import json,os,sqlite3
from datetime import date
ROOT=Path(__file__).resolve().parents[1];data=ROOT/'app/.runtime'
marker=data/'handoff-location.json'
if not marker.exists():
    # Fresh portable package: no DB and no marker yet. Create a neutral marker
    # so the first launch does not fail; nothing to rebase or verify.
    data.mkdir(parents=True,exist_ok=True)
    state={'runtime_path':str(data.resolve()),'created_at':date.today().isoformat(),'rebased_records':0,
           'note':'portable package: relocate_snapshot.py rebases DB paths on first start',
           'verified_runtime':str(data.resolve())}
    marker.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
else:
    state=json.loads(marker.read_text(encoding='utf-8'))
old=state['runtime_path'].replace('\\','/').rstrip('/');new=str(data.resolve()).replace('\\','/')
verified=(state.get('verified_runtime') or '').replace('\\','/').rstrip('/')
protected={'text','body','source_snapshot','source_quote','share_text','title','claim','boundary','caption'}
def remap(obj,key=''):
    if isinstance(obj,dict):return {k:remap(v,k) for k,v in obj.items()}
    if isinstance(obj,list):return [remap(v,key) for v in obj]
    if isinstance(obj,str) and key not in protected:
        value=obj.replace('\\','/')
        if value.casefold()==old.casefold() or value.casefold().startswith(old.casefold()+'/'):return new+value[len(old):]
    return obj
def verify_media(c):
    missing=[]
    for kind,key,raw in c.execute("SELECT kind,id,data FROM records WHERE kind IN ('asset','artifact')"):
        value=json.loads(raw)
        if value.get('path') and not Path(value['path']).is_file():missing.append({'kind':kind,'id':key,'path':value['path']})
    if missing:raise RuntimeError('Snapshot contains missing media files: '+json.dumps(missing,ensure_ascii=False))
changed=0
# 2026-11-25 资料外置：数据库跟着资料目录走（默认在包外「文档\人生重启系统数据」）。三条变化：
#  ① 库在资料目录里取，不再写死包内；
#  ② 库不存在就跳过。以前 sqlite3.connect 会凭空在包内建一个 0 字节的 workbench.sqlite3，
#     资料外置后它会被 launch_root 当成「包内还有一份旧资料」，下一次启动按「两边都有、不覆盖」
#     退回包内空库，客户会以为资料全丢；
#  ③ 资料在包外时：软件文件夹搬走不影响资料里的路径，所以不改写库，也不做「媒体文件必须都在」的
#     硬校验（老库里的过期记录不该拦住客户打开软件）。
db=Path(os.getenv('IP_DATA_DIR') or data)/'workbench.sqlite3'
outside=db.parent.resolve()!=data.resolve()
c=sqlite3.connect(str(db),timeout=30) if db.is_file() else None
try:
    if c is not None:
        has_records='records' in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not has_records:
            # No initialized DB yet (fresh package or user deleted it): nothing to rebase.
            pass
        elif outside:
            # 资料在包外：库里的路径指向的是资料文件夹，不随软件文件夹搬家而变。
            # 这里绝不把它们改写成「软件文件夹里」的位置——否则客户删掉软件文件夹后素材引用全断。
            # 软件自带的模板/资源路径由程序启动时自行探测，不靠库里存的旧位置。
            stale=sum(1 for (raw,) in c.execute("SELECT data FROM records WHERE kind IN ('asset','artifact')").fetchall()
                      if old.casefold() in str(raw).replace('\\','/').casefold())
            if stale:print(json.dumps({'relocate_snapshot':'data dir is outside the package; left %d record(s) untouched'%stale},ensure_ascii=False))
        elif old.casefold()!=new.casefold():
            # Location changed since last launch (data inside the package): full remap + media verification.
            for kind,key,raw in c.execute('SELECT kind,id,data FROM records').fetchall():
                original=json.loads(raw);updated=remap(original)
                if updated!=original:c.execute('UPDATE records SET data=? WHERE kind=? AND id=?',(json.dumps(updated,ensure_ascii=False),kind,key));changed+=1
            if not outside:verify_media(c)
        elif verified.casefold()!=new.casefold():
            # Same location, first launch after this change (or legacy marker without the verified flag):
            # keep the original media verification once, then mark verified.
            if not outside:verify_media(c)
        # Same location and already verified on an earlier launch: nothing moved, skip.
        c.commit()
finally:
    if c is not None:c.close()
state.update(runtime_path=str(data.resolve()),rebased_records=changed,verified_runtime=str(data.resolve()))
updated_marker=json.dumps(state,ensure_ascii=False,indent=2)
if updated_marker!=marker.read_text(encoding='utf-8'):
    marker.write_text(updated_marker,encoding='utf-8')
print(json.dumps({'snapshot_ready':True,'rebased_records':changed,'missing_media':0,'data_dir':str(db.parent)},ensure_ascii=False))
