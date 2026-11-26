/* 结算单打印页：CSP 禁止内联脚本，打印按钮的点击绑定放在这里。 */
document.addEventListener('click', function (event) {
  var el = event.target.closest('[data-print]');
  if (!el) return;
  event.preventDefault();
  window.print();
});
