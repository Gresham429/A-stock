// 主题切换（选股看板 / 复盘台 / 登录页共用）。
//
// 默认浅色（用户 2026-09-17 子项目 D：整站浅白色），选择记在 localStorage。
// `data-theme` 必须**在样式生效前**就写好，否则深色底会先闪一下；
// 所以三个页面在 <head> 最前面各有一行内联脚本先设属性，本文件只负责按钮与持久化。
(function () {
  "use strict";
  var KEY = "astock-theme";
  var DARK = "#0a0d12", LIGHT = "#f4f6f8";
  var root = document.documentElement;

  function current() {
    return root.getAttribute("data-theme") === "dark" ? "dark" : "light";
  }

  function paint(theme) {
    root.setAttribute("data-theme", theme);
    // 手机浏览器的地址栏配色跟着走，不然浅色页配深色状态栏会很难看
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? DARK : LIGHT);
    var btn = document.getElementById("themeBtn");
    if (btn) {
      btn.textContent = theme === "dark" ? "浅色" : "深色";
      btn.setAttribute("title", theme === "dark" ? "切到浅色" : "切到深色");
    }
  }

  function toggle() {
    var next = current() === "dark" ? "light" : "dark";
    try { localStorage.setItem(KEY, next); } catch (e) { /* 隐私模式下写不进去也不影响本次切换 */ }
    paint(next);
  }

  window.__astockTheme = { current: current, toggle: toggle, paint: paint };

  document.addEventListener("DOMContentLoaded", function () {
    paint(current());
    var btn = document.getElementById("themeBtn");
    if (btn && !btn.getAttribute("onclick")) btn.addEventListener("click", toggle);
  });
})();
