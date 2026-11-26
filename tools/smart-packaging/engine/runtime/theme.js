/* theme.js — 主题令牌层（P0.4 衍生核心：场景与风格解耦 80/20）
   80%：场景骨架读 CSS 变量（--bg/--ink/--accent/--pop/--dim），令牌由 family theme 注入；
   20%：家族覆写注册表（家族可覆盖动效曲线/材质/独家组件），防"通用模板感"的逃生舱。 */
window.THEME = window.USER_INPUT.theme || {};
(function applyTheme() {
  const t = window.THEME, r = document.documentElement.style;
  const map = { bg: '--bg', ink: '--ink', accent: '--accent', pop: '--pop', dim: '--dim', accent2: '--accent2' };
  for (const [k, css] of Object.entries(map))
    if (t.colors && t.colors[k]) r.setProperty(css, t.colors[k]);
})();
/* 家族覆写注册表：familyOverrides[family][组件名] = fn(...) —— 场景代码显式查表调用，
   未注册则走默认实现。新增家族时必须至少注册 1 项独家覆写（设计规范红线）。 */
window.FAMILY_OVERRIDES = window.FAMILY_OVERRIDES || window.USER_INPUT.familyOverrides || {};
window.override = (name, fallback) => {
  const fam = window.USER_INPUT.family;
  const reg = window.FAMILY_OVERRIDES[fam];
  return (reg && reg[name] !== undefined) ? reg[name] : fallback;   // 原始值：字符串/对象/函数由消费方自适配
};
