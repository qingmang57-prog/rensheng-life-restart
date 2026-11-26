/* 右下角版本角标：读取 /version.json，不依赖 app.js。 */
(function () {
  function render(version) {
    if (document.getElementById('version-badge')) return;
    var el = document.createElement('div');
    el.id = 'version-badge';
    el.textContent = 'v' + version;
    el.title = '当前版本';
    el.className = 'version-badge';
    var footer = document.querySelector('.page-footer');
    (footer || document.body).appendChild(el);
  }
  function load() {
    fetch('version.json', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (v) { if (v && v.version) render(v.version); })
      .catch(function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', load);
  else load();
})();
