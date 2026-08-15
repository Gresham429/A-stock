// 复盘台前端：拉最近一份复盘 → 渲染 情绪档位 / 情绪硬指标 / AI研判 / 文稿。
// 无数据时保留骨架并提示「生成」。红涨绿跌（A股惯例）。
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const UP = "var(--up)", DOWN = "var(--down)", TXT = "var(--txt)", DIM = "var(--dim)";
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const colorOf = (v) => (v > 0 ? UP : v < 0 ? DOWN : TXT);
  const pctStr = (v) => (v == null ? "—" : (v > 0 ? "+" : "") + v + "%");

  // ── 载入 ──
  async function load() {
    let env = null;
    try { env = await (await fetch("/api/review/latest")).json(); } catch (e) { /* offline */ }
    if (!env || env.empty || !env.metrics) { setEmpty(); return; }
    render(env);
  }

  function setEmpty() {
    $("statusChip").textContent = "🚧 尚无数据";
    $("stamp").textContent = "还没有复盘 —— 点右上「↻ 生成今日复盘」";
  }

  // ── 渲染 ──
  function render(env) {
    const m = env.metrics, ai = env.ai || {}, focus = ai.focus || null;
    const d = env.target_date_dash || env.target_date || "";
    $("stamp").textContent = `复盘 ${d} · 生成于 ${env.generated_at || ""}`;
    $("statusChip").textContent = focus ? "✅ 已生成 · AI 研判" : "✅ 已生成 · 仅硬指标";
    $("statusChip").style.borderStyle = "solid";

    renderPhase(m, focus);
    renderMetrics(m);
    renderAI(focus);
    renderAnalysts(ai.analysts);
    renderArticle(ai.article);
  }

  function renderAnalysts(analysts) {
    const el = $("analystRow");
    if (!el) return;
    if (!analysts || !analysts.length) {
      el.innerHTML = `<div class="mcard"><div class="mc-desc">本场未生成分析师研判（未配 DeepSeek key 或调用失败）。</div></div>`;
      return;
    }
    el.innerHTML = analysts.map((a) =>
      `<div class="mcard" style="border-left-color:var(--gold)">` +
      `<div class="mc-h"><span class="mc-name">${esc(a.title)}</span></div>` +
      `<div class="mc-desc" style="color:var(--txt);line-height:1.65;margin-top:8px">${esc(a.report)}</div></div>`
    ).join("");
  }

  // 情绪档位条：高亮 AI 选中的档位 + 周期第几天 + 一句话
  function renderPhase(m, focus) {
    const phase = focus && focus.emotion_phase;
    document.querySelectorAll("#phaseSteps .ps").forEach((el) => {
      const b = el.querySelector("b");
      const on = b && phase && b.textContent.trim() === phase;
      el.style.borderColor = on ? "var(--gold)" : "var(--line2)";
      el.style.background = on ? "linear-gradient(135deg,#3a2a08,#241a06)" : "var(--panel)";
      el.style.color = on ? "var(--gold)" : "var(--muted)";
      if (b) b.style.color = on ? "var(--gold)" : "var(--txt)";
    });
    const cy = m.cycle_position || {};
    const cyc = cy.available
      ? `情绪周期第 ${cy.day_n} 天（低点 ${cy.trough_date} 起，${cy.rising ? "回升中" : "走弱中"}）`
      : "情绪周期：历史样本不足（需累积交易日）";
    const one = focus && focus.market_oneliner ? ` · <b style="color:var(--gold)">${esc(focus.market_oneliner)}</b>` : "";
    $("phNote").innerHTML = esc(cyc) + one;
  }

  function card(name, val, valColor, desc) {
    return `<div class="mcard"><div class="mc-h"><span class="mc-name">${name}</span></div>` +
      `<div class="mc-val" style="color:${valColor}">${val}</div>` +
      `<div class="mc-desc">${desc}</div></div>`;
  }

  function renderMetrics(m) {
    const me = m.money_effect || {}, pr = m.promotion || {}, cp = m.consec_premium || {};
    const ld = m.ladder || {}, cy = m.cycle_position || {}, tt = m.theme_tree || {};
    const le = m.loss_effect || {}, sq = m.seal_quality || {}, b = m.breadth || {};
    const p12 = pr.one_to_two || {};
    const themes = (tt.top || []).slice(0, 5).map((t) => `${t.theme}(${t.count})`).join("、") || "—";
    const cyVal = cy.available ? `第${cy.day_n}天` : "—";
    const cards = [
      card("赚钱效应", pctStr(me.median), colorOf(me.median),
        `昨涨停股今日<b>中位数</b> · 翻红率 ${me.red_rate ?? "—"}% · 再涨停 ${me.again_rate ?? "—"}%（均值 ${pctStr(me.avg)}）`),
      card("晋级率 1进2", (p12.rate ?? "—") + "%", colorOf((p12.rate ?? 0) - 40),
        `${p12.promoted ?? "—"}/${p12.base ?? "—"} 晋级 · 2进3 ${pr.two_to_three?.rate ?? "—"}% · 3板+ ${pr.three_plus?.rate ?? "—"}%`),
      card("连板溢价", pctStr(cp.median), colorOf(cp.median),
        `昨≥2板今承接 · 翻红率 ${cp.red_rate ?? "—"}%（${cp.n ?? 0} 只）`),
      card("连板梯队", (ld.highest ?? 0) + " 板", ld.gaps && ld.gaps.length ? UP : TXT,
        (ld.gaps && ld.gaps.length ? `⚠️ 断层缺 ${ld.gaps.join("/")} 板（最高标悬空）· ` : "梯队连续 · ") +
        `${JSON.stringify(ld.tiers || {})}`),
      card("情绪周期", cyVal, cy.available && cy.rising ? UP : (cy.available ? DOWN : DIM),
        cy.available ? `本轮低点 ${cy.trough_date} 起 · ${cy.rising ? "回升中" : "走弱中"}` : "历史样本不足，需累积交易日"),
      card("题材热点", (tt.top && tt.top[0] ? tt.top[0].theme : "—"), "var(--gold)",
        `按涨停家数：${esc(themes)}`),
      card("亏钱效应", (le.deep5 ?? 0) + " 只", (le.deep5 || 0) > 0 ? DOWN : TXT,
        `昨涨停今跌超5% · 跌停 ${le.limit_down ?? 0} 只 · 最惨 ${pctStr(le.worst)}`),
      card("封板质量", (sq.never_broken_rate ?? "—") + "%", TXT,
        `从未开板率 · 早盘封 ${sq.opening ?? 0} · 尾盘封 ${sq.late ?? 0} · 均炸板 ${sq.avg_broken_times ?? 0} 次`),
    ];
    $("mgrid").innerHTML = cards.join("");
  }

  function renderAI(focus) {
    if (!focus) {
      $("aiSlots").innerHTML = `<div class="ai-slot"><div class="s-body">` +
        `本场未生成 AI 研判（未配 DeepSeek key 或调用失败）。硬指标不受影响。</div></div>`;
      return;
    }
    const dirs = (focus.focus_directions || []).map((d) =>
      `<div class="ai-slot"><div class="s-lbl">${esc(d.direction || "")}</div>` +
      `<div class="s-body"><b>依据</b> ${esc(d.logic || "")}<br><b style="color:var(--up)">风险</b> ${esc(d.risk || "")}</div></div>`).join("");
    const risks = (focus.risk_alerts || []).map((r) => "· " + esc(r)).join("<br>") || "—";
    const vers = (focus.verification_items || []).map((v) => "· " + esc(v)).join("<br>") || "—";
    $("aiSlots").innerHTML =
      `<div class="ai-slot"><div class="s-lbl">情绪档位</div><div class="s-body" style="font-size:18px;font-weight:800;color:var(--gold)">${esc(focus.emotion_phase || "—")}</div></div>` +
      dirs +
      `<div class="ai-slot"><div class="s-lbl">风险提示</div><div class="s-body" style="color:var(--txt)">${risks}</div></div>` +
      `<div class="ai-slot"><div class="s-lbl">明日验证条件</div><div class="s-body" style="color:var(--txt)">${vers}</div></div>`;
  }

  let _article = "";
  function renderArticle(text) {
    _article = text || "";
    if (!text) {
      $("artNote").textContent = "本场未生成文稿（未配 DeepSeek key 或调用失败）";
      return;
    }
    const html = esc(text)
      .replace(/^#{1,6}\s*(.+)$/gm, '<b style="color:var(--gold)">$1</b>')
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>");
    $("artBody").innerHTML = "<p>" + html + "</p>";
    $("artBody").style.color = "var(--txt)";
    const btn = $("copyBtn");
    btn.disabled = false;
    btn.onclick = () => {
      navigator.clipboard.writeText(_article).then(() => {
        btn.textContent = "已复制 ✓";
        setTimeout(() => (btn.textContent = "复制全文"), 1600);
      });
    };
  }

  // ── 生成按钮：POST run → 轮询 status → 重载 ──
  function wireGen() {
    const btn = $("genBtn");
    if (!btn) return;
    btn.onclick = async () => {
      btn.disabled = true;
      $("statusChip").textContent = "⏳ 生成中…（取数+指标+AI，约 1~2 分钟）";
      try {
        await fetch("/api/review/run", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
      } catch (e) { /* ignore */ }
      poll(btn);
    };
  }

  function poll(btn) {
    const timer = setInterval(async () => {
      let s = null;
      try { s = await (await fetch("/api/review/status")).json(); } catch (e) { return; }
      if (s && !s.running) {
        clearInterval(timer);
        btn.disabled = false;
        if (s.error) {
          $("statusChip").textContent = "⚠️ 生成失败";
          $("stamp").textContent = "生成失败：" + s.error;
        } else {
          load();
        }
      }
    }, 3000);
  }

  wireGen();
  load();
})();
