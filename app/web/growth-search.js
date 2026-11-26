'use strict';
window.ipGrowthSearch = (() => {
  let data = null, busy = false, mounted = false;
  const e = value => esc(value);
  const icon = name => (window.growthIcon ? window.growthIcon(name) : '');
  const topics = ['金钱', '创业', '合伙', '信任', '风险', '失败', '家庭', '意义', '精力', '选择'];

  const kindLabel = item => ({answer: '本人回答', knowledge: '认知库', fragment: '成长碎片'}[item.kind] || item.kind);
  const kindClass = item => 'g-search-kind--' + (item.kind || 'other');

  function resultCard(item) {
    const evidence = item.evidence || {};
    const links = [];
    if (item.kind === 'knowledge') links.push(`<a class="g-text-link" href="#/knowledge/${encodeURIComponent(item.id)}">查看原认知 ${icon('arrow')}</a>`);
    (item.fragment_ids || []).slice(0, 1).forEach(id => links.push(`<button class="g-text-link" data-g="fragment" data-id="${e(id)}">查看这条碎片 ${icon('arrow')}</button>`));
    if (item.kind === 'answer') links.push(`<button class="g-text-link" data-g="answer-detail" data-id="${e(item.id)}">查看原回答 ${icon('arrow')}</button>`);
    return `<article class="g-search-result"><div class="g-search-result-top"><span class="g-search-kind ${kindClass(item)}">${e(kindLabel(item))}</span><span>${e(item.pillar || item.category || '')}</span></div><button class="g-card-title" data-g="fragment" data-id="${e((item.fragment_ids || [])[0] || '')}" ${item.fragment_ids?.length ? '' : 'disabled'}>${e(item.title)}</button><p class="g-excerpt">${e(item.excerpt)}</p>${item.boundary ? `<p class="g-search-boundary">边界：${e(item.boundary)}</p>` : ''}<details class="g-search-evidence"><summary>看证据</summary><pre>${e(evidence.answer || evidence.claim || evidence.text || '没有可展示原文。')}</pre>${evidence.question ? `<p class="g-note">原问题：${e(evidence.question)}</p>` : ''}${(evidence.probes || []).map(p => `<p class="g-note">追问：${e(p.question)}<br>回答：${e(p.answer)}</p>`).join('')}</details><div class="g-search-actions">${links.join('')}</div></article>`;
  }

  function groupTree() {
    return (data.groups || []).map(group => `<details class="g-search-group" open><summary>${icon('folder')}<span>${e(group.title)}</span><small>${group.results.length}</small></summary><div>${group.results.map(resultCard).join('')}</div></details>`).join('');
  }

  function questionPanel() {
    const list = data.questions || [];
    if (!list.length) return '';
    return `<section class="g-panel g-space"><div class="g-section-head"><div><h2>还缺的认知</h2><p class="g-note">搜索没找到足够依据时，先补这几题。</p></div></div><div class="g-cards">${list.map(q => `<article class="g-search-result"><div class="g-search-result-top"><span>${e(q.pillar)}</span><span>${e(q.level)}</span></div><button class="g-card-title" data-gs="ask">${e(q.text)}</button><p class="g-note">答完会自动进入对应支柱。</p></article>`).join('')}</div></section>`;
  }

  function view() {
    if (!data) return '<section class="g-panel"><p class="g-note">正在搜索…</p></section>';
    const stats = data.stats || {};
    const chips = topics.map(t => `<button type="button" data-gs="quick" data-topic="${e(t)}">${e(t)}</button>`).join('');
    return `<section class="g-panel"><form data-gs-form="search" class="g-search-form"><label class="g-searchbox">${icon('search')}<input type="search" name="q" value="${e(data.query || '')}" placeholder="搜：金钱驾驭、如何创业、看人、低谷" aria-label="认知搜索"></label><button class="g-primary" type="submit">搜索</button><label class="g-inline-check"><input type="checkbox" name="archived" ${data.include_archived ? 'checked' : ''}>包含归档</label></form><div class="g-search-chips">${chips}</div><p class="g-note">${data.topic ? '主题：' + e(data.topic) + ' · ' : ''}本人回答 ${stats.answers || 0} · 认知库 ${stats.knowledge || 0} · 碎片 ${stats.fragments || 0} · 目标 ${stats.goals || 0}</p></section>${data.results?.length ? `<section class="g-panel g-space"><div class="g-section-head"><div><h2>认知技能树</h2><p class="g-note">核心认知、经历证据和适用边界。系统只给依据，不替你判断。</p></div></div>${groupTree()}</section>` : `<section class="g-panel g-space"><div class="g-empty"><span>${icon('search')}</span><h2>这块还没有沉淀</h2><p>先补几题，让经历变成能调用的认知。</p></div></section>`}${questionPanel()}`;
  }

  function paint() {
    const host = document.getElementById('g-search-view');
    if (host) host.innerHTML = view();
  }

  async function run(query = '', includeArchived = false) {
    if (busy) return;
    busy = true;
    try {
      const params = new URLSearchParams({q: query});
      if (includeArchived) params.set('archived', '1');
      data = Object.assign({include_archived: includeArchived}, await api('/growth/search?' + params));
      paint();
    } catch (err) {
      toast(err.message);
    } finally {
      busy = false;
    }
  }

  document.addEventListener('submit', ev => {
    const form = ev.target.closest('[data-gs-form=search]');
    if (!form) return;
    ev.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    location.hash = '#/restart?view=search&q=' + encodeURIComponent(String(values.q || '').trim()) + (values.archived ? '&archived=1' : '');
  });

  document.addEventListener('click', ev => {
    const button = ev.target.closest('[data-gs]');
    if (!button || button.disabled) return;
    const action = button.dataset.gs;
    if (action === 'quick') {
      location.hash = '#/restart?view=search&q=' + encodeURIComponent(button.dataset.topic || '');
    }
    if (action === 'ask') {
      location.hash = '#/restart?view=ask';
    }
  });

  async function render(query) {
    const value = (query.get('q') || '').trim();
    const includeArchived = query.get('archived') === '1';
    data = {query: value, include_archived: includeArchived, results: [], groups: [], questions: [], stats: {}};
    await run(value, includeArchived);
    return '<div id="g-search-view">' + view() + '</div>';
  }

  function mount() {
    if (mounted) return;
    mounted = true;
    const input = document.querySelector('#g-search-view input[name=q]');
    if (input) input.focus();
  }

  function unmount() {
    data = null;
    busy = false;
    mounted = false;
  }

  return {render, mount, unmount};
})();
