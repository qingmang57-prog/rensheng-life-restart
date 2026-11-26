(function(root){'use strict';
const $=s=>document.querySelector(s), make=(tag,cls,parent)=>{let n=document.createElement(tag);n.className=cls;parent.appendChild(n);return n;};
async function decode(img){if(!img.getAttribute('src'))return;await img.decode();if(!img.naturalWidth)throw Error('FRAME_DECODE_FAILED:'+img.getAttribute('src'));}
async function mount(cfg,policy){
 if(cfg.templateId!=='jd-spatial-voice-v1'||cfg.template!=='spatial-voice')throw Error('SPATIAL_IDENTITY');
 await document.fonts.load('650 62px JDSpatial');await document.fonts.load('400 42px JDSpatial');await document.fonts.ready;
 const sceneRoot=$('#world'),nodes=new Map(),measurements={},images=new Map(),headings=new Map();
 const preliminary=JDSpatialPlan.compileSpatialPlan(cfg,{},policy);
 for(const a of preliminary.anchors){
  const node=make('article','spatial-card',sceneRoot);node.dataset.key=a.key;
  node.style.width=a.width+'px';node.style.height=a.height+'px';
  make('span','card-index',node).textContent=String(a.blockIndex+1).padStart(2,'0');
  const marker=make('i','card-center',node);Object.assign(marker.style,{position:'absolute',left:'50%',top:'50%',width:'0',height:'0'});
  const copy=make('div','card-copy',node);copy.textContent=a.text;
  let size=a.width>500?62:44;
  for(;size>=36;size-=2){copy.style.fontSize=size+'px';if(copy.scrollHeight<=node.clientHeight-84)break;}
  const fits=copy.scrollHeight<=node.clientHeight-84;
  measurements[a.key]={width:a.width,height:a.height,fontSize:Math.max(size,36),fits};
  nodes.set(a.key,node);
 }
 const plan=JDSpatialPlan.compileSpatialPlan(cfg,measurements,policy);
 const bad=Object.entries(measurements).filter(([,v])=>!v.fits);
 if(bad.length){plan.mode='layout-fallback';plan.diagnostics.push({code:'TEXT_OVERFLOW',keys:bad.map(([k])=>k)});}
 for(const s of cfg.contentScenes||[]){
  if(s.heading){const h=make('div','scene-heading',sceneRoot);h.textContent=s.heading.text;Object.assign(h.style,{position:'absolute',width:'820px',fontSize:'36px',lineHeight:'1.3',color:'#d5bc88',whiteSpace:'pre-wrap',visibility:'hidden'});headings.set(s.id,{el:h,block:s.heading});}
  if(!s.illustration)continue;
  const img=make('img','scene-illustration',sceneRoot);img.alt='';
  img.src=s.illustration.path;await decode(img);img.style.visibility='hidden';images.set(s.id,img);
 }
 const presenter=$('#presenter'),caption=$('#caption'),wall=$('#wordwall');
 const report={version:1,templateId:cfg.templateId,coreVersion:cfg.coreVersion||'1.0.2',coreDigest:cfg.coreDigest||null,mode:plan.mode,inputSceneCount:(cfg.contentScenes||[]).length,anchorCount:plan.anchors.length,diagnostics:plan.diagnostics,measurements,available:{'01':true,'02':plan.anchors.length>0,'03':plan.anchors.length>1,'04':plan.cameraSegments.some(s=>s.mode==='diagonal'),'05':plan.cameraSegments.length>0,'06':plan.cameraSegments.some(s=>s.mode==='walk'),'07':plan.anchors.length>1,'08':plan.emphasisWindows.length>0}};
 report.used={};let lastPose=null;
 async function renderAt(t){
  const pose=JDSpatialPose.poseAt(plan,t);lastPose=pose;
  if(pose.showWorld){report.used['01']=true;report.used['02']=true;if(pose.cards.some(v=>v.visible&&!v.focused)){report.used['03']=true;report.used['07']=true;}if(pose.mode==='diagonal')report.used['04']=true;if(pose.mode==='hold')report.used['05']=true;if(pose.mode==='walk')report.used['06']=true;if(pose.mode==='emphasis')report.used['08']=true;}
  const c=pose.camera;
  sceneRoot.style.transform='scale('+c.scale+') rotateX('+(-c.pitch)+'deg) rotateY('+(-c.yaw)+'deg) translate3d('+(-c.x)+'px,'+(-c.y)+'px,'+(-c.z)+'px)';
  sceneRoot.style.visibility=pose.showWorld?'visible':'hidden';
  for(const v of pose.cards){
   const node=nodes.get(v.key);
   node.style.visibility=v.visible?'visible':'hidden';
   node.style.clipPath=v.clip?'inset(0 '+v.clip+'% 0 0)':'none';
   node.style.opacity=String(v.opacity);node.style.filter='blur('+v.blur+'px)';
   node.style.transform='translate3d('+v.x+'px,'+v.y+'px,'+v.z+'px) translate(-50%,-50%) rotateY('+v.yaw+'deg) rotateX('+v.pitch+'deg) scale('+v.scale+')';
  }
  for(const [sid,h]of headings){const sourceScene=(cfg.contentScenes||[]).find(s=>s.id===sid),leaf=JDSpatialPose.leafMotion((sourceScene.motion||[]).filter(e=>e.target==='heading'||e.target==='scene'),pose.t-sourceScene.start);h.el.style.opacity=String(leaf.opacity);h.el.style.filter='blur('+leaf.blur+'px)';h.el.style.clipPath=leaf.clip?'inset(0 '+leaf.clip+'% 0 0)':'none';const g=plan.groups.find(x=>x.key===pose.activeGroup);h.el.style.visibility=pose.showWorld&&g?.sceneId===sid&&h.block.start<=pose.t&&pose.t<h.block.end?'visible':'hidden';if(g)h.el.style.transform='translate3d('+g.x+'px,'+(g.y-320+leaf.y)+'px,'+g.z+'px) translate(-50%,-100%) translateX('+leaf.x+'px) scale('+leaf.scale+')';}
  for(const [sid,img]of images){
   const group=plan.groups.find(g=>g.key===pose.activeGroup);
   img.style.visibility=pose.showWorld&&group?.sceneId===sid?'visible':'hidden';
   if(group)img.style.transform='translate3d('+group.x+'px,'+(group.y+(group.indices.length===1?350:Math.ceil(group.indices.length/2)*122.5+170))+'px,'+group.z+'px) translate(-50%,-50%)';
  }
  const line=(cfg.captions||[]).find(x=>Number(x.start)<=pose.t&&pose.t<Number(x.end));
  caption.textContent=line?line.text:'';caption.style.visibility=line?'visible':'hidden';
  const known=(cfg.captions||[]).filter(x=>x.start<=pose.t).map(x=>x.text).join('  ');
  wall.textContent=known;const activeScene=(cfg.contentScenes||[]).find(s=>s.start<=pose.t&&pose.t<s.end),bg=JDSpatialPose.leafMotion((activeScene?.motion||[]).filter(e=>e.target==='background'),pose.t-(activeScene?.start||0));wall.style.opacity=String(.025*bg.opacity);wall.style.filter='blur('+bg.blur+'px)';wall.style.clipPath=bg.clip?'inset(0 '+bg.clip+'% 0 0)':'none';wall.style.transform='translate3d('+(-pose.t*3+bg.x)+'px,'+(-pose.t*1.5+bg.y)+'px,0) scale('+bg.scale+')';
  $('#progress').style.transform='scaleX('+pose.t/cfg.duration+')';
  const primary=pose.presenter==='primary',hidden=pose.presenter==='hidden';
  const box=$('#presenter-box');box.classList.remove('primary');box.style.visibility=hidden?'hidden':'visible';
  const rect=p=>p==='primary'?[64,145,952,510]:[596,150,420,500];
  const current=plan.windows.find(s=>s.start<=pose.t&&pose.t<s.end);
  const previous=current?plan.windows.filter(s=>s.end<=current.start).slice(-1)[0]:null;
  const progress=current?Math.min(1,Math.max(0,(pose.t-current.start)/.55)):1,u=progress*progress*(3-2*progress),a=rect(previous?.presenter||pose.presenter),b=rect(pose.presenter),r=b.map((v,i)=>a[i]+(v-a[i])*u);
  Object.assign(box.style,{left:r[0]+'px',top:r[1]+'px',width:r[2]+'px',height:r[3]+'px',right:'auto'});
  const total=Number(cfg.frames)||Math.ceil((cfg.renderDuration||cfg.duration)*cfg.fps);
  const frame=Math.max(1,Math.min(total,Math.floor(pose.t*cfg.fps+1e-6)+1));
  presenter.style.objectPosition=cfg.objectPosition||'50% 50%';
  const src='frames/'+String(frame).padStart(6,'0')+'.jpg';
  if(presenter.getAttribute('src')!==src)presenter.src=src;
  await decode(presenter);
 }
 await renderAt(0);
 return {plan,report,renderAt,imgWait:async()=>{await decode(presenter);},debug:()=>({pose:lastPose,plan,geometry:[...nodes].map(([key,n])=>({key,rect:n.getBoundingClientRect().toJSON(),center:n.querySelector('.card-center').getBoundingClientRect().toJSON(),visible:getComputedStyle(n).visibility}))})};
}
root.JDSpatialDOM={mount};
})(window);
