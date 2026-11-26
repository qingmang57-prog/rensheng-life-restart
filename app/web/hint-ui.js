'use strict';
/* 按钮悬停说明：把「这个按钮到底干什么」写成一句人话，悬停即见。
   只描述操作结果，不写形容词。文案与按钮实际行为一一对应，改行为要同步改这里。 */

const ACTION_HINTS={
  /* 内容工厂 · 入口 */
  'create':'新建一个作品，从选题或素材开始。',
  'resolve':'读取这个链接的真实内容；平台要求登录或校验时会停下，不猜。',
  'refresh':'重新读一遍最新状态，不改任何数据。',
  'generate':'按当前输入生成一版结果，旧的保留。',
  'from-knowledge':'用我的经验库里挑中的资料来写，保留出处。',
  'confirm':'确认这一版稿件。确认后剪片、包装、发布都按这一版走，改稿要重新确认。',

  /* 拍完 · 主链 */
  'prepare-video':'转写 → 清长静音 → 上字幕 → 出一版待审片。有声的嗯啊不会自动删。',
  'prepare-reference':'重新转写这条参考视频，旧转写保留。',
  'prepare-content':'把字幕拆解成一份稿子，只整理你讲过的内容，不加事实。',
  'transcribe':'本地识别语音，拿到每个词的起止时间。没有它就没法按词剪。',
  'auto-cut':'只剪掉长停顿的静音段，说话内容一个字不动。',
  'manual-cut':'按你填的「开始-结束」秒数真实剪切，顺序就是输出顺序。',
  'remove-words':'按面板上划掉的全部词重剪一版，之前剪掉的重复、停顿保持剪掉，不会还原。',
  'review':'把这版标记为已审阅。没确认过就不能进交付。',

  /* 语义净剪 */
  'semantic-propose':'让模型找重复话、改口和口头禅。只出建议，不动视频。',
  'semantic-apply':'按你勾选的建议真实剪掉音视频，生成一版清理版。',

  /* 成片重建 */
  'rebuild-estimate':'只算这条素材能不能到目标时长，不渲染，秒回。',
  'rebuild-render':'按目标时长出成片：变速、响度归一、过双闸。要等几十秒。',

  /* 锚点 / 包装 */
  'anchor-align':'用锚点把稿子和声音对齐，看哪里讲快了讲慢了。',
  'anchor-apply':'按锚点方案剪一版。',
  'packaging-auto':'由模型按内容自动选一套包装风格。',
  'packaging-style':'换成这套包装风格，只改样式不动内容。',
  'packaging-edit':'改这版包装里的文字和强调词。',
  'packaging-preview':'只渲一帧预览，先看效果再决定要不要正式渲。',
  'packaging-render':'正式渲染这版包装，比较慢，出完整视频。',
  'package-lite':'出一版轻量交付包，只含视频和字幕。',
  'motion-preview':'用本地模板只渲一帧，先看人物位置对不对。',
  'motion-render':'用本地模板正式渲染横屏成片，耗时较长。',

  /* 发布 */
  'publish-login':'扫码登录平台账号。只有登录了才能真发。',
  'publish-plan':'先出一份发布预案给你核对，不会真发出去。',
  'publish-submit':'把这一版真发到平台。发了就收不回，先确认内容。',
  'publish-refresh':'重新查一次登录状态和发布结果。',
  'release-download':'下载这次交付的完整包。',
  'release-copy':'复制这份文案到剪贴板。',
  'matrix-login':'登录矩阵里的各个账号。',
  'matrix-plan':'出一份矩阵发布计划，先看再发。',
  'matrix-submit':'按计划提交到矩阵账号。',

  /* 第二大脑 */
  'new-knowledge':'新建一条认知笔记。',
  'brain-view':'打开这条资料的详情和出处。',
  'import-brain':'把选中的资料导入我的经验库，保留来源。',
  'extract-knowledge':'从这份内容里提炼可复用的认知点。',
  'suggestion':'让模型给改动建议，不会直接改你的稿。',
  'accept':'采用这条建议。',

  /* 选题 / 创作 */
  'drawer-judge':'打开你核实过的观点，挑 1–3 条当写稿依据。',
  'drawer-voices':'打开采集到的真实评论，挑一句观众的提问起题。',
  'drawer-memories':'打开以前发过的作品，换个角度再讲一遍。',
  'drawer-anchors':'挑一个大家都熟的日子当由头，比如开学、年底。',
  'drawer-none':'没素材也能开工，挑个问题回答就行。',
  'pick-voice':'用这句评论起题，起题句会填好，可以再改。',
  'pick-memory':'用这条老作品起题，换个切角，不重复上次讲法。',
  'pick-anchor':'用这个时间点起题，起题句会填好，可以再改。',
  'pick-direction':'选这个切入方向，只决定从哪儿切，讲什么仍由你定。',
  'bench-topics':'看这个话题被拆成了哪些角度。',
  'bench-select':'选中这一条，后面用它写稿。',
  'bench-use-topic':'用这个选题去写稿。',
  'bench-comments':'看这条作品的真实评论。',
  'bench-collect':'拉取这条作品的互动数据。',
  'bench-page':'换一页看。',
  'creation-write':'按这个方向写一版讲稿。',
  'creation-write-dialog':'打开写稿对话框，先填方向再写。',
  'creation-more':'再给几个不同角度的方向。',
  'creation-continue':'接着这一版往下写，不重头来。',
  'creation-auto':'让模型自己挑一个方向写完。',
  'creation-adopt':'用这一版当稿子。',
  'creation-history':'看这个选题以前写过哪些版本。',
  'director-brief':'按当前定稿给分镜建议，不改稿子。',

  /* 复盘 */
  'feedback-start-topic':'开始收集这个话题下的真实评论。',
  'feedback-comments':'看这条作品的评论，只看真实抓到的。',
  'feedback-collect':'重新拉一次这条作品的互动数据。',
  'feedback-classify':'让模型给评论分类，方便看观众到底在问什么。',
  'feedback-insight':'从评论里提炼能用的选题判断。',
  'feedback-comment-page':'换一页评论。'
};

function hintFor(el){
  const key=el.dataset.action;
  if(key&&ACTION_HINTS[key])return ACTION_HINTS[key];
  return el.getAttribute('title')||'';
}

function applyHints(root){
  (root||document).querySelectorAll('[data-action],[data-tc]').forEach(el=>{
    /* 首页快速开始卡自带说明文字，不挂气泡，保证八张卡悬停观感完全一致 */
    if(el.classList.contains('home-tile'))return;
    const text=hintFor(el);
    if(!text)return;
    el.setAttribute('data-hint',text);
    if(el.hasAttribute('title'))el.removeAttribute('title');
  });
}

applyHints(document);
let hintQueued=false;
new MutationObserver(()=>{
  if(hintQueued)return;
  hintQueued=true;
  requestAnimationFrame(()=>{hintQueued=false;applyHints(document);});
}).observe(document.body,{childList:true,subtree:true});
