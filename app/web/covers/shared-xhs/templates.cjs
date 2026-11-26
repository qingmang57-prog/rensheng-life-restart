/* Adapted from AlineFan/xhs-cover-skill (MIT). See LICENSE-AlineFan.txt.
 * Original A/B/C layout and colors. Fixed fields; no model, CDN or runtime network. */
'use strict';
const fs=require('node:fs'),path=require('node:path');
const VERSION='1.0.1';
const sourceCss=fs.readFileSync(path.join(__dirname,'reference.css'),'utf8');
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function clean(f){const out={};for(const [k,max] of [['title',12],['subtitle',18],['badge',20],['account',30]]){out[k]=String(f[k]??'').trim();if(Array.from(out[k].replace(/[【】]/g,'')).length>max)throw Error(k+' 超过 '+max+' 字，请缩短后再生成');}if(!out.title)throw Error('请填写封面标题');return out;}
function title(s){return esc(s).replace(/【([^】]+)】/g,'<span class="accent">$1</span>');}
function photo(p){const src=String(p?.image||'');if(!src)return '';if(!/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(src)&&!/^[\w.-]+\.(png|jpe?g|webp)$/i.test(src))throw Error('图片仅支持本地图片');return src;}
function render(id,fields={},options={}){
 const f=clean(fields),n=Array.from(f.title.replace(/[【】]/g,'')).length;
 const image=photo(options.person);if(id!=='shared-xhs-title'&&!image)throw Error('请先选择一张本地图片或已有形象');
 const brand=[f.badge,f.account].filter(Boolean).map(esc).join(' · ');
 const img=image?'<img data-cover-photo src="'+esc(image)+'" alt="">':'';
 const sz=n<=6?46:n<=9?40:34;let body;
 if(id==='shared-xhs-title')body='<div id="styleA">'+(image?'<div class="sA-banner">'+img+'</div>':'')+'<div class="sA-content"><div class="sA-brand" data-field="brand">'+brand+'</div><div class="sA-title" data-field="title" style="font-size:'+sz+'px">'+title(f.title)+'</div><div class="sA-divider"></div><div class="sA-sub" data-field="subtitle">'+esc(f.subtitle)+'</div></div></div>';
 else if(id==='shared-xhs-card')body='<div id="styleB" class="theme-aurora"><div class="sB-layout sB-l1 active"><div class="sBL-tag" data-field="brand">'+brand+'</div><div class="sBL-title" data-field="title" style="font-size:'+sz+'px">'+title(f.title)+'</div><div class="sBL-divider"></div><div class="sBL-sub" data-field="subtitle">'+esc(f.subtitle)+'</div><div class="sBL-img">'+img+'</div></div></div>';
 else if(id==='shared-xhs-person')body='<div id="styleC"><div class="sC-aurora-ov"></div><div class="sC-person">'+img+'</div><div class="sC-overlay"><div class="sC-top"><div class="sC-super" data-field="brand">'+brand+'</div><div class="sC-main" data-field="title" style="font-size:'+Math.min(sz,38)+'px">'+title(f.title)+'</div></div><div class="sC-bottom"><div class="sC-sub" data-field="subtitle">'+esc(f.subtitle)+'</div></div></div></div>';
 else throw Error('未知封面模板');
 const font=options.fontData||'';if(font&&!/^data:font\/ttf;base64,[A-Za-z0-9+/=]+$/.test(font))throw Error('字体来源不合法');
 return '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=1080"><title>小红书封面</title><style>'+sourceCss+(font?'\n@font-face{font-family:SharedXhs;src:url("'+font+'")}':'')+'html,body{width:1080px;height:1440px;min-height:0;margin:0;padding:0;display:block;overflow:hidden;background:none}body{font-family:"Microsoft YaHei",sans-serif}.sA-brand,.sBL-tag,.sC-super{white-space:normal;overflow:visible;max-width:100%;overflow-wrap:anywhere}.sBL-img img{object-fit:contain}.cover-outer{position:absolute;left:0;top:0;width:360px;height:480px;border-radius:0;box-shadow:none;transform:scale(3);transform-origin:top left;overflow:hidden}.sA-title,.sBL-title,.sC-main{overflow-wrap:anywhere;white-space:pre-wrap}.sA-sub,.sBL-sub,.sC-sub{overflow-wrap:anywhere}.sBL-img{flex:1;min-height:110px}.sC-person img{max-width:88%}</style></head><body data-template="'+id+'" data-version="'+VERSION+'"><div class="cover-outer">'+body+'</div><script>window.CONSUMER_READY=document.fonts.ready;window.renderAt=async()=>{};</script></body></html>';
}
const templates=[['shared-xhs-title','小红书·大字标题',false,'原作者 A：橙色侧线、大字、可选图片'],['shared-xhs-person','小红书·人物出镜',true,'原作者 C：柔色背景、真人、描边标题'],['shared-xhs-card','小红书·图文卡片',true,'原作者 B：上文下图、渐变卡片']].map(([id,name,person,structure])=>({id,name,types:['通用观点','认知/思维','知识科普','自我成长'],structure,version:VERSION,shared:true,personDefault:{on:person,x:'center'},requiresPerson:person,fieldsDefault:{title:'变强才是关键',subtitle:'',badge:'',account:''},fieldsExtra:[],render:(f,o)=>render(id,f,o)}));
module.exports={version:VERSION,canvasSize:{width:1080,height:1440},templates,byId:id=>templates.find(x=>x.id===id),render};
