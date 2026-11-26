(function(root,factory){const api=factory(root.JDSpatialPlan||(typeof require==='function'?require('./plan.js'):null));if(typeof module==='object')module.exports=api;root.JDSpatialPose=api;})(typeof window==='object'?window:globalThis,function(P){
'use strict';
const mix=(a,b,u)=>a+(b-a)*u,clamp=P.clamp,ease=u=>{u=clamp(u);return u*u*(3-2*u);};
function leafMotion(events,t){
 const out={x:0,y:0,scale:1,opacity:1,blur:0,clip:0};
 for(const e of events){
  const dt=t-e.at,p=clamp(dt/e.duration);
  if(e.primitive==='emphasis'){if(dt<0||dt>e.duration)continue;const b=Math.sin(p*Math.PI);if(e.mode==='fade')out.opacity*=1-.28*b;else if(e.mode==='blur')out.blur+=2*b;else out.scale*=1+.06*b;continue;}
  if(e.primitive==='exit'&&dt<0)continue;
  const exit=e.primitive==='exit',q=exit?p:1-ease(p);out.opacity=exit?1-p:ease(p);
  if(e.mode==='up')out.y=70*q;if(e.mode==='down')out.y=-70*q;if(e.mode==='left')out.x=-90*q;if(e.mode==='right')out.x=90*q;
  if(e.mode==='pop')out.scale=1-.28*q;if(e.mode==='zoom')out.scale=1+.18*q;if(e.mode==='slam')out.scale=1+.5*q;
  if(e.mode==='blur')out.blur=18*q;if(e.mode==='mask')out.clip=100*q;
 }return out;
}
function poseAt(plan,t){
 if(typeof t!=='number'||!Number.isFinite(t))throw Error('INVALID_TIME');
 t=Math.max(0,Math.min(plan.duration-1/plan.fps,t));
 const scene=plan.windows.find(s=>s.start<=t&&t<s.end);
 let seg=plan.cameraSegments.find(s=>s.start<=t&&t<s.end);
 if(!seg)seg=[...plan.cameraSegments].reverse().find(s=>s.start<=t)||plan.cameraSegments[0];
 let camera={x:0,y:0,z:0,scale:1,yaw:0,pitch:0},mode='static',group=null;
 if(seg){
  group=plan.groups.find(g=>g.key===seg.groupKey);
  const at=Math.min(t,seg.end),moving=at<seg.moveEnd&&seg.moveEnd>seg.start;
  if(moving){
   const raw=clamp((at-seg.start)/(seg.moveEnd-seg.start)),u=ease(raw),a=seg.from,b=seg.to;
   Object.keys(camera).forEach(k=>camera[k]=mix(a[k],b[k],u));
   const dx=b.x-a.x,dy=b.y-a.y;
   camera.x+=-dy*.1*4*u*(1-u);camera.y+=dx*.1*4*u*(1-u);
   camera.yaw+=Math.sin(Math.PI*u)*seg.anglePulse;
   camera.scale*=1-.025*Math.sin(Math.PI*u);mode=seg.mode;
  }else{
   const emphasis=seg.emphasis;
   camera=emphasis?{...seg.to}:P.holdPose(seg.to,Math.max(0,at-seg.holdStart),seg.limit);
   mode=emphasis&&t>=emphasis.start&&t<emphasis.end?'emphasis':'hold';
  }
 }
 const emph=plan.emphasisWindows.find(e=>e.start<=t&&t<e.end);
 const contextStart=Math.max(0,...plan.windows.filter(w=>w.talking&&w.end<=t).map(w=>w.end));
 const cards=plan.anchors.map(a=>{
  const visible=!!scene&&!scene.talking&&a.start>=contextStart&&a.start<=t&&t<a.retentionEnd;
  const focused=a.group===group?.key;
  let zoom=1;
  if(emph&&a.key===emph.targetKey)zoom=1+.06*Math.sin(Math.PI*clamp((t-emph.start)/(emph.end-emph.start)));
  const events=(a.motion||[]).filter(e=>e.target===a.role||e.target==='scene');
  const leaf=leafMotion(events.filter(e=>!(emph&&e.primitive==='emphasis'&&a.key===emph.targetKey)),t-a.sceneStart);
  return {key:a.key,visible,focused,x:a.x+leaf.x,y:a.y+leaf.y,clip:leaf.clip,z:a.z+(focused?0:-35),yaw:a.yaw,pitch:a.pitch,scale:zoom*(focused?1:.972)*leaf.scale,opacity:(focused?1:.34)*leaf.opacity,blur:(focused?0:9)+leaf.blur};
 });
 return {t,camera,mode,presenter:scene?.presenter||'primary',showWorld:!!scene&&!scene.talking&&plan.mode==='spatial',activeGroup:group?.key||null,cards};
}
return {poseAt,leafMotion};
});
