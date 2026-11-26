(function(root,factory){const api=factory();if(typeof module==='object')module.exports=api;root.JDSpatialPlan=api;})(typeof window==='object'?window:globalThis,function(){
'use strict';
const finite=n=>typeof n==='number'&&Number.isFinite(n);
const clamp=(n,a=0,b=1)=>Math.max(a,Math.min(b,n));
const fail=(code)=>{throw Error(code);};
const halfEven=n=>{const a=Math.floor(n),d=n-a;return d===.5?(a%2?a+1:a):Math.round(n);};
function compileSpatialPlan(input,measurements={},policy={}){
 const fps=input.fps||30,duration=input.duration,scenes=input.contentScenes;
 const frameTime=t=>halfEven(t*fps)/fps;
 if(!finite(duration)||duration<=0||fps!==30)fail('INVALID_CLOCK');
 if(scenes!=null&&!Array.isArray(scenes))fail('INVALID_SCENES');
 const mode=scenes==null?'template-fallback':scenes.length?'spatial':'empty';
 const enabled=code=>!policy.preset||policy.preset==='auto'||policy.preset===code||['01','02','03'].includes(code);
 const anchors=[],groups=[],diagnostics=[],windows=[];
 const captions=input.captions||[];for(const c of captions){if(typeof c.text!=='string'||!finite(c.start)||!finite(c.end)||c.start<0||c.end<=c.start||c.end>duration+.05)fail('INVALID_CAPTION');}
 const sources=new Map(captions.map((c,i)=>[c.i??i,c]));
 let index=0;const sceneIds=new Set();let previousEnd=0;
 for(const s of scenes||[]){
  if(!s||typeof s.id!=='string'||!s.id||sceneIds.has(s.id))fail('INVALID_SCENE_ID');sceneIds.add(s.id);
  if(!['talking','quote','summary','points','process','contrast'].includes(s.kind))fail('INVALID_SCENE_KIND');
  if(s.start<previousEnd-.001)fail('OVERLAPPING_SCENES');previousEnd=s.end;
  if(s.presenter&&!['primary','pip','hidden'].includes(s.presenter))fail('INVALID_PRESENTER');
  const legalModes=['up','down','left','right','pop','zoom','slam','blur','mask','fade'];
  const targets=['scene','background','heading',...(s.kind==='points'?['items']:s.kind==='process'?['steps']:s.kind==='contrast'?['left','right','conclusion']:[s.kind])];
  for(const e of s.motion||[]){if(!['enter','exit','emphasis'].includes(e.primitive)||!legalModes.includes(e.mode)||!targets.includes(e.target)||!finite(e.at)||!finite(e.duration)||e.at<0||e.duration<=0||e.at+e.duration>s.end-s.start+.001||(e.primitive==='emphasis'&&!['pop','zoom','slam','fade','blur'].includes(e.mode)))fail('INVALID_MOTION');}
  if(s.heading){const h=s.heading;if(typeof h.text!=='string'||!finite(h.start)||!finite(h.end)||h.start<s.start-.001||h.end>s.end+.001||!Array.isArray(h.source_segment_ids)||h.source_segment_ids.some(id=>!sources.has(id))||!h.source_segment_ids.map(id=>sources.get(id).text).join('').includes(h.text))fail('INVALID_HEADING');}
  if(!finite(s.start)||!finite(s.end)||s.start<0||s.end<=s.start||s.end>duration+.05)fail('INVALID_SCENE_TIME');
  if(!Array.isArray(s.blocks)||s.blocks.length>(s.kind==='points'?5:6))fail('SCENE_CAPACITY');
  if(s.kind==='talking'){if(s.blocks.length||(s.motion||[]).length)fail('INVALID_TALKING');windows.push({start:s.start,end:s.end,presenter:'primary',talking:true});continue;}
  if(['points','process'].includes(s.kind)&&s.blocks.length<2)fail('SCENE_CAPACITY');
  if(s.kind==='contrast'&&s.blocks.length!==3)fail('SCENE_CAPACITY');
  if(['quote','summary'].includes(s.kind)&&s.blocks.length!==1)fail('SCENE_CAPACITY');
  const byTime=new Map();
  s.blocks.forEach((b,i)=>{
   if(typeof b.text!=='string'||!b.text.trim()||!finite(b.start)||!finite(b.end)||b.start<s.start-.001||b.end>s.end+.001||b.end<=b.start)fail('INVALID_BLOCK');
   if(!Array.isArray(b.source_segment_ids)||!b.source_segment_ids.length||b.source_segment_ids.some(id=>!sources.has(id)))fail('INVALID_SOURCE_REFERENCE');
   const referenced=b.source_segment_ids.map(id=>sources.get(id));
   if(Math.abs(b.start-Math.min(...referenced.map(c=>c.start)))>.001||Math.abs(b.end-Math.max(...referenced.map(c=>c.end)))>.001)fail('BLOCK_SOURCE_TIME_DRIFT');
   const original=b.source_segment_ids.map(id=>sources.get(id).text).join('');if(!original.includes(b.text))fail('FABRICATED_BLOCK');
   const k=String(b.start);if(!byTime.has(k))byTime.set(k,[]);byTime.get(k).push({b,i});
  });
  for(const entries of byTime.values()){
   const x=index%2===0?index*640:index*640+180,y=index*170,z=-index*120;
   const key=s.id+':g'+groups.length;
   const cols=entries.length>1?2:1,rows=Math.ceil(entries.length/cols);
   const group={cameraStart:(input.jdSpatialClock?.[s.id+':'+entries[0].i]?.start??halfEven(entries[0].b.start*fps))/fps,key,sceneId:s.id,kind:s.kind,start:entries[0].b.start,end:s.end,sourceEnd:Math.max(...entries.map(v=>v.b.end)),x,y,z,presenter:s.presenter||'pip',indices:[],emphasis:s.emphasis||[],motion:s.motion||[],sceneStart:s.start,sceneEnd:s.end,illustration:s.illustration||null};
   entries.forEach(({b,i},j)=>{
    const a={key:s.id+':'+i,group:key,sceneId:s.id,blockIndex:i,text:b.text,source_segment_ids:b.source_segment_ids||[],start:b.start,end:b.end,retentionEnd:duration,x:x+(cols===1?0:(j%2?230:-230)),y:y+(Math.floor(j/cols)-(rows-1)/2)*245,z,width:entries.length===1?820:430,height:entries.length===1?440:215,yaw:entries.length===1?10:(j%2?-12:12),pitch:6,sceneStart:s.start,motion:s.motion||[],role:s.kind==='process'?'steps':s.kind==='points'?'items':s.kind==='contrast'?['left','right','conclusion'][i]:s.kind};
    group.indices.push(anchors.length);anchors.push(a);
   });
   groups.push(group);index++;
  }
  windows.push({start:s.start,end:s.end,presenter:s.presenter||'pip',talking:false});
 }
 groups.sort((a,b)=>a.start-b.start||a.key.localeCompare(b.key));
 const cameraSegments=[],emphasisWindows=[];
 let from={x:groups[0]?.x||0,y:groups[0]?.y||0,z:groups[0]?.z||0,scale:1,yaw:0,pitch:0},lastEnd=0;
 for(let i=0;i<groups.length;i++){
  const g=groups[i],next=groups[i+1],start=g.cameraStart,end=Math.min(frameTime(g.end),next?next.cameraStart:frameTime(duration));
  if(end<=start)continue;
  const process=enabled('06')&&g.kind==='process'&&groups.filter(v=>v.sceneId===g.sceneId).length>1;
  let transition=i===0||(!enabled('04')&&!process)?0:Math.min(.8,Math.max(0,end-start-.6));
  if(transition<.6)transition=0;
  if(!transition&&i>0){const dx=from.x-g.x,dy=from.y-g.y,dz=from.z-g.z;g.indices.forEach(j=>{anchors[j].x+=dx;anchors[j].y+=dy;anchors[j].z+=dz;});g.x=from.x;g.y=from.y;g.z=from.z;diagnostics.push({code:'NO_MOVE_WINDOW',sceneId:g.sceneId});}
  const to=(!transition&&i>0)?{...from}:{x:g.x,y:g.y,z:g.z,scale:1,yaw:-4,pitch:process?16:0};
  const eligible=anchors.map((a,j)=>({a,j})).filter(v=>v.a.sceneId===g.sceneId&&v.a.start<=g.start).map(v=>v.j);
  const hits=g.indices.filter(j=>g.emphasis.some(word=>anchors[j].text.includes(word)));
  let emphasis=null;
  if(enabled('08')&&hits.length===1&&eligible.length>1){
   const events=g.motion.filter(e=>e.primitive==='emphasis');
   const ev=events.find(e=>e.mode==='zoom'||e.mode==='pop');
   const ea=ev?g.sceneStart+ev.at:start+transition+.15,ed=ev?ev.duration:.7;
   if(ea>=start+transition&&ea+ed<=end)emphasis={targetKey:anchors[hits[0]].key,start:ea,end:ea+ed,alignment:ev?'event':'block'};
   else diagnostics.push({code:'NO_HOLD_WINDOW',sceneId:g.sceneId});
  }else if(hits.length>1)diagnostics.push({code:'AMBIGUOUS_FOCUS',sceneId:g.sceneId});
  if(emphasis)emphasisWindows.push(emphasis);
  const segment={groupKey:g.key,start,end,moveEnd:start+transition,mode:transition?(process?'walk':'diagonal'):'hold',from:{...from},to,emphasis,holdStart:start+transition,limit:enabled('05')&&g.indices.length<5?1.065:1,anglePulse:8};
  cameraSegments.push(segment);
  const held=emphasis?to:holdPose(to,Math.max(0,end-segment.holdStart),segment.limit);
  from={...held};lastEnd=end;
 }
 return {version:1,fps,duration,durationFrames:Math.round(duration*fps),logicalCanvas:{width:1080,height:1920},mode,anchors,groups,windows,cameraSegments,emphasisWindows,diagnostics,policy,measurements};
}
function holdPose(p,dt,limit){const span=limit-1;return {...p,scale:p.scale*(span>0?1+span*(1-Math.exp(-.03*dt/span)):1),yaw:p.yaw+(limit>1?Math.min(.5,dt*.05):0)};}
return {compileSpatialPlan,holdPose,clamp};
});
