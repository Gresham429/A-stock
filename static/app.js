/* 观察台 A-Share Watchdesk —— 前端逻辑 */

/* ── 名词解释（新手向） ── */
const GLOSSARY = {
  '现价':'这只股票此刻的成交价（元/股）。非交易时段显示最近一个交易日的收盘价。',
  '涨跌%':'相比昨天收盘价涨/跌了百分之多少。红色=涨，绿色=跌（A股习惯，和美股相反）。',
  'PE 市盈率':'股价 ÷ 每股年利润。通俗说：按现在的赚钱速度回本要几年。越低越便宜；亏损公司为负。科技股通常偏高。',
  'PB 市净率':'股价 ÷ 每股净资产。衡量股价相对公司“家底”贵不贵，越低越便宜。',
  '年化波动':'价格上下摆动的剧烈程度（近20日测算再年化）。越高越刺激、机会与风险都越大。40%温和，70%+很猛。',
  '20日涨幅':'最近20个交易日（约一个月）累计涨跌幅。判断它是不是已经涨过一波。',
  '区间位置':'现价在近20日“最低~最高”里的百分位。接近100%=近期高位（可能过热、追高危险）；接近0%=近期低位（可能超跌）。',
  '主力净流入':'“大资金”（超大单）净买入金额（亿元）。为正=大资金在买，为负=在卖。持续流入常是看多信号。',
  '1手成本':'买最小单位“1手”（=100股）要多少钱。1万本金买不起1手成本过高的票。',
  '换手率':'当天成交股数占流通股比例。越高说明交易越活跃。',
  '市值':'公司总价值=股价×总股数（亿元）。大市值更稳，小市值弹性大波动大。',
  '情景区间':'按历史波动推算未来1个月价格大概率的范围。±1σ≈68%概率，±2σ≈95%。只说“幅度”，不预测方向。',
  '研报 / 评级 / EPS':'研报=券商分析师的研究报告；评级=操作建议（买入>增持>中性>减持）；EPS=每股收益（利润÷股数），研报里是预测值。',
  '龙虎榜 / 席位':'当天异动的股票被交易所公示买卖最多的营业部“席位”。标“机构专用”=基金等机构，参考价值更高；知名游资席位多为短线炒作。',
  '解禁':'限售股到期可流通。解禁量大（占流通盘比例高）=潜在抛压，短期利空。',
  '成本价 / 盈亏%':'成本价=你买入的每股价；盈亏%=（现价-成本）÷成本。红=赚，绿=亏（A股配色）。',
  '止损 / 止盈':'止损=跌破预设底线就卖、防亏损扩大；止盈=涨到目标就落袋。纪律比预测更重要。',
  '操作词':'buy买入/add加仓=建议买；sell卖出/reduce减仓=建议卖；hold持有=继续拿；watch观望=先等更好时机。',
};

const COLS=[
  {k:'name',t:'名称',s:'name',tip:'股票简称与6位代码'},
  {k:'price',t:'现价',s:'price',tip:GLOSSARY['现价']},
  {k:'chg_pct',t:'涨跌%',s:'chg_pct',tip:GLOSSARY['涨跌%']},
  {k:'pe_ttm',t:'PE',s:'pe_ttm',tip:GLOSSARY['PE 市盈率']},
  {k:'pb',t:'PB',s:'pb',tip:GLOSSARY['PB 市净率']},
  {k:'vol',t:'年化波动',s:'vol',tip:GLOSSARY['年化波动']},
  {k:'cum20',t:'20日涨%',s:'cum20',tip:GLOSSARY['20日涨幅']},
  {k:'range_pos',t:'区间位置',s:'range_pos',tip:GLOSSARY['区间位置']},
  {k:'net20',t:'主力20日',s:'net20',tip:GLOSSARY['主力净流入']},
  {k:'lot_cost',t:'1手成本',s:'lot_cost',tip:GLOSSARY['1手成本']},
  {k:'spark',t:'走势30D',s:null,tip:'近30日收盘价走势迷你图'},
  {k:'del',t:'',s:null,tip:''},
];
let DATA=[], sortKey='net20', sortDir=-1, autoTimer=null, LLM=false, MODEL='', WEB=false;
// 请求令牌：每次发起自增；异步响应回来前若已非最新，则丢弃（防面板/抽屉切换时旧响应错位）
let recSeq=0, detailSeq=0, mktSeq=0;
let TAXO=null;   // 板块两级分类（/api/config 下发）
let WAVE=null, WAVE_PERIOD='day', WAVE_CTX=null, KL_CTX=null, waveTimer=null;   // 行情多周期数据 / 当前周期 / 折线hover几何 / 蜡烛hover几何 / 分时自动刷新定时器
let FOLIO_ADV={};   // 持仓「何时卖」建议缓存 code->{html}|{loading:true}，跨自动刷新保留
let NEWS_FILTER={sector:'',kind:'',code:''}, lastNewsRefresh=0;   // 新闻筛选 / 看盘惰性刷新节流

const clr=v=> v>0?'up':v<0?'down':'flat';
const sgn=v=> v>0?'+':'';
const fmt=(v,d=2)=> v==null||v===''?'—':Number(v).toFixed(d);
const fmtInt=v=> v==null?'—':Math.round(v).toLocaleString();
const esc=s=> (s==null?'':String(s)).replace(/[<>&]/g,m=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[m]));
// AI 结果的时间戳/缓存 meta 行 + 强制刷新按钮（onclick 传重新请求的调用串）
function aiMeta(j,onclick){
  const when=j.analyzed_at?j.analyzed_at.replace('T',' ').slice(0,16):(j.updated||'');
  const age=j.age_min!=null?(j.age_min<1?'刚刚':j.age_min+'分钟前'):'';
  const tag=j.cached?'命中缓存':'实时';
  return `<div class="aimeta">🕐 分析于 ${when}${age?'（'+age+'）':''} · ${tag}`
    +(onclick?` <button class="mini" onclick="${onclick}">🔄 强制刷新</button>`:'')+`</div>`;
}

/* ── 浮动 tooltip（避开表格 overflow 裁剪） ── */
function initTooltips(){
  const tip=document.getElementById('tip');
  document.addEventListener('mouseover',e=>{
    const el=e.target.closest('[data-tip]');
    if(!el||!el.dataset.tip){return;}
    tip.textContent=el.dataset.tip; tip.style.display='block';
    const r=el.getBoundingClientRect();
    let x=r.left, y=r.bottom+8;
    tip.style.left=Math.min(x, window.innerWidth-tip.offsetWidth-14)+'px';
    tip.style.top=y+'px';
  });
  document.addEventListener('mouseout',e=>{
    if(e.target.closest('[data-tip]')) document.getElementById('tip').style.display='none';
  });
}

/* ── 对比表 ── */
function head(){
  document.getElementById('head').innerHTML=COLS.map(c=>{
    const ar=c.s===sortKey?`<span class="ar">${sortDir<0?'▼':'▲'}</span>`:'';
    return `<th ${c.s?`onclick="sortBy('${c.s}')"`:''} ${c.tip?`data-tip="${c.tip.replace(/"/g,'&quot;')}"`:''}>${c.t}${c.tip&&c.t?' ⓘ':''}${ar}</th>`;
  }).join('');
}
function sortBy(k){ if(sortKey===k)sortDir*=-1; else{sortKey=k;sortDir=-1;} render(); }

function sparkSVG(series){
  if(!series||series.length<2) return '';
  const v=series.map(p=>p.close), lo=Math.min(...v), hi=Math.max(...v), w=88,h=26,pad=2,rng=hi-lo||1;
  const pts=v.map((y,i)=>`${(pad+i*(w-2*pad)/(v.length-1)).toFixed(1)},${(h-pad-(y-lo)/rng*(h-2*pad)).toFixed(1)}`).join(' ');
  const col=v[v.length-1]>=v[0]?'var(--up)':'var(--down)';
  return `<svg class="spark" viewBox="0 0 ${w} ${h}"><polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.4"/></svg>`;
}
function rangeBar(p){
  if(p==null) return '—';
  const col= p>=80?'var(--hot)': p<=20?'var(--cold)':'var(--gold)';
  return `<div class="rp"><div class="track"><div class="mk" style="left:${p}%;background:${col}"></div></div><div class="lbl" style="color:${col}">${Math.round(p)}%</div></div>`;
}
function render(){
  head();
  const rows=[...DATA].sort((a,b)=>{
    let x=a[sortKey], y=b[sortKey];
    if(sortKey==='name'){return sortDir*((a.name||'').localeCompare(b.name||'','zh'));}
    x=x==null?-1e18:x; y=y==null?-1e18:y; return sortDir*(x-y);
  });
  if(!rows.length){document.getElementById('rows').innerHTML='<tr><td colspan="12" class="empty">自选股为空，右上角输入代码加入</td></tr>';return;}
  document.getElementById('rows').innerHTML=rows.map(r=>`
    <tr onclick="openDetail('${r.code}')">
      <td class="nm"><div class="n">${r.name||'?'}</div><div class="c">${r.code}</div></td>
      <td class="price ${clr(r.chg_pct)}">${fmt(r.price)}</td>
      <td class="${clr(r.chg_pct)}"><span class="pill" style="background:${r.chg_pct>0?'rgba(255,77,94,.13)':r.chg_pct<0?'rgba(34,201,139,.13)':'transparent'}">${sgn(r.chg_pct)}${fmt(r.chg_pct)}%</span></td>
      <td class="${r.pe_ttm<0?'down':''}">${r.pe_ttm?fmt(r.pe_ttm,1):'亏损'}</td>
      <td>${fmt(r.pb,2)}</td>
      <td>${r.vol!=null?fmt(r.vol,0)+'%':'—'}</td>
      <td class="${clr(r.cum20)}">${r.cum20!=null?sgn(r.cum20)+fmt(r.cum20,1)+'%':'—'}</td>
      <td>${rangeBar(r.range_pos)}</td>
      <td class="${clr(r.net20)}">${r.net20!=null?sgn(r.net20)+fmt(r.net20,1)+'亿':'—'}</td>
      <td>${fmtInt(r.lot_cost)}</td>
      <td>${sparkSVG(r.series)}</td>
      <td><button class="del" onclick="event.stopPropagation();delStock('${r.code}')">✕</button></td>
    </tr>`).join('');
}
async function load(){
  try{
    const j=await (await fetch('/api/overview')).json();
    DATA=j.rows; render();
    document.getElementById('stamp').innerHTML=`更新 <b>${j.updated}</b>`;
  }catch(e){ document.getElementById('rows').innerHTML=`<tr><td colspan="12" class="empty">加载失败：${e}. 后端是否已启动？</td></tr>`; }
}
async function addStock(){
  const el=document.getElementById('addcode'), code=el.value.trim(); if(!code)return;
  const j=await (await fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})})).json();
  if(!j.ok){alert(j.msg||'加入失败');return;} el.value=''; load();
}
async function delStock(code){
  await fetch('/api/watchlist/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})}); load();
}
const REFRESH_MS=30000;   // 自动刷新间隔 30 秒
function toggleAuto(){
  const b=document.getElementById('autobtn');
  if(autoTimer){clearInterval(autoTimer);autoTimer=null;b.textContent='自动刷新 关';b.classList.remove('on');}
  else{autoTimer=setInterval(()=>{load();loadPortfolio();loadMarket();maybeRefreshNews();},REFRESH_MS);b.textContent='自动刷新 开·30s';b.classList.add('on');}
}

/* ── 深挖抽屉 ── */
function tab(p){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.p===p));
  document.querySelectorAll('.pane').forEach(el=>el.classList.remove('on'));
  document.getElementById('pane_'+p).classList.add('on');
}
function closeDrawer(){clearInterval(waveTimer);waveTimer=null;document.getElementById('drawer').classList.remove('open');document.getElementById('scrim').classList.remove('open');}
function loading(id){document.getElementById(id).innerHTML='<div class="paneempty"><span class="spin"></span> 拉取中…</div>';}
async function openDetail(code){
  const gen=++detailSeq;   // 快速切换股票时，作废上一只的在飞请求
  document.getElementById('drawer').classList.add('open');
  document.getElementById('scrim').classList.add('open');
  tab('ov');
  document.getElementById('d_name').textContent='加载中…';
  document.getElementById('d_code').textContent=code;
  document.getElementById('d_price').textContent='—'; document.getElementById('d_chg').textContent='';
  ['ov','rp','lhb','lk','ff'].forEach(p=>loading('pane_'+p));
  // 行情多周期并行加载（分时/5日折线 + 日K蜡烛），默认周期=分时
  WAVE=null; WAVE_PERIOD='day'; renderWave();
  fetch('/api/wave/'+code).then(r=>r.json()).then(w=>{ if(gen!==detailSeq)return; WAVE=w; renderWave(); })
    .catch(()=>{ if(gen!==detailSeq)return; const p=document.getElementById('pane_wave'); if(p)p.innerHTML='<div class="paneempty">行情数据加载失败</div>'; });
  clearInterval(waveTimer); waveTimer=setInterval(()=>tickMinute(code), 30000);   // 分时交易时段每30s自动刷新
  let j;
  try{ j=await (await fetch('/api/detail/'+code)).json(); }
  catch(e){ if(gen!==detailSeq)return; document.getElementById('pane_ov').innerHTML='<div class="paneempty">加载失败：'+e+'</div>'; return; }
  if(gen!==detailSeq) return;   // 已切到别的股票，丢弃过期响应
  renderDetail(j);
}
function renderDetail(j){
  const q=j.quote||{}, m=j.metrics||{}, b=j.band;
  document.getElementById('d_name').textContent=q.name||j.code;
  document.getElementById('d_code').textContent=j.code+(q.industry?` · ${q.industry}`:'');
  document.getElementById('d_price').textContent=fmt(q.price);
  const chgEl=document.getElementById('d_chg');
  chgEl.textContent=`${sgn(q.chg_pct)}${fmt(q.chg_pct)}%`; chgEl.className='small '+clr(q.chg_pct);

  const cell=(k,v,c='',tip='')=>`<div class="cell"><div class="k" ${tip?`data-tip="${tip}"`:''}>${k}${tip?' ⓘ':''}</div><div class="v ${c}">${v}</div></div>`;
  let ov=`<div class="kv">
    ${cell('PE(TTM)', q.pe_ttm?fmt(q.pe_ttm,1):'亏损', q.pe_ttm<0?'down':'', GLOSSARY['PE 市盈率'])}
    ${cell('市净率PB', fmt(q.pb,2),'',GLOSSARY['PB 市净率'])}
    ${cell('总市值', q.mcap_yi?fmt(q.mcap_yi,0)+' 亿':'—','',GLOSSARY['市值'])}
    ${cell('1手成本', fmtInt(q.lot_cost)+' 元','',GLOSSARY['1手成本'])}
    ${cell('年化波动', m.vol!=null?m.vol+'%':'—','',GLOSSARY['年化波动'])}
    ${cell('20日涨幅', m.cum20!=null?sgn(m.cum20)+fmt(m.cum20,1)+'%':'—', clr(m.cum20),GLOSSARY['20日涨幅'])}
    ${cell('区间位置', m.range_pos!=null?Math.round(m.range_pos)+'%':'—', m.range_pos>=80?'up':m.range_pos<=20?'down':'',GLOSSARY['区间位置'])}
    ${cell('主力20日净流入', m.net20!=null?sgn(m.net20)+fmt(m.net20,1)+' 亿':'—', clr(m.net20),GLOSSARY['主力净流入'])}
  </div>`;
  if(b){
    ov+=`<div class="band"><div class="h" data-tip="${GLOSSARY['情景区间']}">未来1个月情景区间（波动率反推，非点位预测）ⓘ</div>
      <div class="row"><span>±1σ（约68%概率落此区间）</span><b>${b.low1} ~ ${b.high1} 元（±${b.sigma_pct}%）</b></div>
      <div class="row"><span>±2σ（约95%概率，极端波动）</span><b>${b.low2} ~ ${b.high2} 元</b></div>
      <div class="note">σ 由近20日实际波动年化后折算到1个月。区间只描述“波动幅度”，不代表方向；涨跌概率各半。</div></div>`;
  }
  // 财报（利润表）
  const fin=j.financials||[];
  if(fin.length){
    ov+='<div class="subh">财报 · 营收/归母净利 + 同比</div><div class="kv">'+fin.slice(0,4).map(f=>
      `<div class="cell"><div class="k">${f.period}</div><div class="v" style="font-size:12.5px;line-height:1.6">`
      +`营收 ${f.revenue_yi??'—'}亿 <span class="${clr(f.revenue_yoy)}" style="font-size:11px">${f.revenue_yoy!=null?sgn(f.revenue_yoy)+f.revenue_yoy+'%':''}</span><br>`
      +`净利 ${f.profit_yi??'—'}亿 <span class="${clr(f.profit_yoy)}" style="font-size:11px">${f.profit_yoy!=null?sgn(f.profit_yoy)+f.profit_yoy+'%':''}</span></div></div>`).join('')+'</div>';
  }
  // 近期新闻
  const news=j.news||[];
  if(news.length){
    ov+='<div class="subh">近期新闻（公司/题材/政策面）</div>'+news.slice(0,5).map(nw=>
      `<div class="item"><div class="ttl small">${nw.title}</div><div class="meta"><span>${(nw.date||'').slice(0,10)}</span><span>${nw.source||''}</span></div></div>`).join('');
  }
  ov+=`<div class="subh">近30日走势</div>${bigSpark(m.series)}`;
  if(LLM) ov+=`<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
    <button class="btn ai" onclick="entryAnalysis('${j.code}')">🎯 深度入场分析（何时/怎么买+卖出策略）</button>
    <button class="btn ai" onclick="askDetailAdvice('${j.code}')">🤖 该买还是该卖</button>
  </div><div id="detailEntry"></div><div id="detailAdvice"></div>`;
  document.getElementById('pane_ov').innerHTML=ov;

  const reps=j.reports||[];
  document.getElementById('pane_rp').innerHTML = reps.length ? reps.map(r=>`
    <div class="item"><div class="top">
      <div class="ttl">${r.pdf?`<a href="${r.pdf}" target="_blank">${r.title}</a>`:r.title}</div>
      ${r.rating?`<span class="tag">${r.rating}</span>`:''}
    </div><div class="meta"><span>${r.date}</span><span>${r.org}</span>
      ${r.eps_this?`<span>今年EPS ${r.eps_this}</span>`:''}${r.eps_next?`<span>明年EPS ${r.eps_next}</span>`:''}
    </div></div>`).join('') : '<div class="paneempty">近期无机构研报（小盘/次新/冷门股常见）</div>';

  const lhb=j.dragon_tiger||{}, recs=lhb.records||[], seats=lhb.seats||{};
  let h='';
  if(recs.length){
    h+='<div class="subh">近半年上榜记录</div>'+recs.map(r=>`
      <div class="item"><div class="top"><div class="ttl small">${r.reason}</div>
        <span class="${clr(r.change_pct)}">${sgn(r.change_pct)}${r.change_pct}%</span></div>
      <div class="meta"><span>${r.date}</span><span class="${clr(r.net_buy_wan)}">净买 ${sgn(r.net_buy_wan)}${fmtInt(r.net_buy_wan)}万</span><span>换手 ${r.turnover}%</span></div></div>`).join('');
    if((seats.buy||[]).length) h+='<div class="subh">最近一次 买入席位 TOP5</div>'+seats.buy.map(s=>`<div class="seat"><span>${s.name}${s.is_inst?'<span class="inst">机构</span>':''}</span><span class="up">买 ${fmtInt(s.buy_wan)}万</span></div>`).join('');
    if((seats.sell||[]).length) h+='<div class="subh">最近一次 卖出席位 TOP5</div>'+seats.sell.map(s=>`<div class="seat"><span>${s.name}${s.is_inst?'<span class="inst">机构</span>':''}</span><span class="down">卖 ${fmtInt(s.sell_wan)}万</span></div>`).join('');
  }else h='<div class="paneempty">近半年未登上龙虎榜</div>';
  document.getElementById('pane_lhb').innerHTML=h;

  const lk=j.lockup||{}, up=lk.upcoming||[], his=lk.history||[];
  const riskTxt={high:'⚠ 高压：90天内有≥5%解禁',mid:'注意：90天内有解禁',none:'✓ 近90天无解禁压力'};
  let lh=`<div class="item"><span class="tag risk-${lk.risk||'none'}">${riskTxt[lk.risk||'none']}</span></div>`;
  if(up.length) lh+='<div class="subh">未来待解禁</div>'+up.map(u=>`<div class="item"><div class="top"><div class="ttl small">${u.type||'限售解禁'}</div><span class="${u.ratio_pct>=5?'up':''}">${u.ratio_pct}% 流通盘</span></div><div class="meta"><span>${u.date}</span><span>${fmtInt(u.shares_wan)}万股</span></div></div>`).join('');
  else lh+='<div class="paneempty">未来一年无待解禁记录</div>';
  if(his.length) lh+='<div class="subh">历史解禁</div>'+his.slice(0,5).map(u=>`<div class="item"><div class="meta"><span>${u.date}</span><span>${u.type||''}</span><span>${u.ratio_pct}%</span></div></div>`).join('');
  document.getElementById('pane_lk').innerHTML=lh;

  document.getElementById('pane_ff').innerHTML = (m.series&&m.series.length)
    ? '<div class="subh">近30日 主力(超大单)净流入</div>'+flowChart(m.series)+'<div class="note">红柱=净流入，绿柱=净流出（单位：亿元）。资金持续流入而股价滞涨，常是吸筹信号。</div>'
    : '<div class="paneempty">暂无资金流数据</div>';
}
function bigSpark(series){
  if(!series||series.length<2) return '<div class="paneempty">无走势数据</div>';
  const v=series.map(p=>p.close),lo=Math.min(...v),hi=Math.max(...v),w=620,h=110,pad=8,rng=hi-lo||1;
  const pts=v.map((y,i)=>`${pad+i*(w-2*pad)/(v.length-1)},${h-pad-(y-lo)/rng*(h-2*pad)}`).join(' ');
  const up=v[v.length-1]>=v[0],col=up?'var(--up)':'var(--down)';
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px">
    <polygon points="${pad},${h-pad} ${pts} ${w-pad},${h-pad}" fill="${up?'rgba(255,77,94,.08)':'rgba(34,201,139,.08)'}"/>
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.6"/>
    <text x="${pad}" y="14" fill="var(--muted)" font-size="10" font-family="monospace">${hi.toFixed(2)}</text>
    <text x="${pad}" y="${h-2}" fill="var(--muted)" font-size="10" font-family="monospace">${lo.toFixed(2)}</text></svg>`;
}
function flowChart(series){
  const s=series.slice(-30),w=620,h=150,pad=16,mid=h/2,mx=Math.max(...s.map(p=>Math.abs(p.main/1e8)),0.01),gap=(w-2*pad)/s.length,bw=gap*0.7;
  const bars=s.map((p,i)=>{const v=p.main/1e8,x=pad+i*gap+gap*0.15,bh=Math.abs(v)/mx*(mid-pad),y=v>=0?mid-bh:mid;return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" fill="${v>=0?'var(--up)':'var(--down)'}" opacity=".85"/>`;}).join('');
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px"><line x1="${pad}" y1="${mid}" x2="${w-pad}" y2="${mid}" stroke="var(--line2)"/>${bars}
    <text x="${pad}" y="12" fill="var(--muted)" font-size="10" font-family="monospace">+${mx.toFixed(1)}亿</text>
    <text x="${pad}" y="${h-4}" fill="var(--muted)" font-size="10" font-family="monospace">-${mx.toFixed(1)}亿</text></svg>`;
}

/* ── 行情多周期（分时 / 5日 折线 · 近1月/近3月/近半年/近1年 日K蜡烛） ── */
const WAVE_PERIODS=[['day','分时'],['5d','5日'],['1m','近1月'],['3m','近3月'],['6m','近半年'],['1y','近1年']];
const WAVE_WINDOW={'1m':30,'3m':90,'6m':180,'1y':365};   // 日K各档=自然日窗口（各不相同，修掉旧版 60日/90天 重叠）
function _daysAgo(n){ const d=new Date(); d.setDate(d.getDate()-n); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`; }
// 在完整日K序列上预计算 MA5/MA20 再截窗，保证窗口内均线也画满（否则窗口前 N 根缺头）
function _dailyWithMA(){
  const d=(WAVE&&WAVE.daily)||[];
  const ma=(i,m)=>{ if(i<m-1) return null; let s=0; for(let k=i-m+1;k<=i;k++) s+=d[k].close; return s/m; };
  return d.map((b,i)=>({...b, ma5:ma(i,5), ma20:ma(i,20)}));
}
function waveSeries(period){
  const W=WAVE||{};
  if(period==='day') return {pts:(W.intraday||[]).map(p=>({label:p.t,value:p.price})), base:W.prev_close, kind:'intra'};
  if(period==='5d')  return {pts:(W.min5||[]).map(p=>({label:p.t,value:p.close})), base:null, kind:'intra'};
  const c=_daysAgo(WAVE_WINDOW[period]||365);
  return {bars:_dailyWithMA().filter(x=>x.date>=c), base:null, kind:'daily'};
}
function _annVol(vals){
  if(vals.length<3) return null;
  const r=[]; for(let i=1;i<vals.length;i++){ if(vals[i-1]>0) r.push(Math.log(vals[i]/vals[i-1])); }
  if(r.length<2) return null;
  const mean=r.reduce((a,b)=>a+b,0)/r.length;
  const v=r.reduce((a,b)=>a+(b-mean)**2,0)/(r.length-1);
  return Math.sqrt(v)*Math.sqrt(252)*100;
}
function waveStats(series,period){
  const daily=series.kind==='daily';
  const vals=daily?series.bars.map(b=>b.close):series.pts.map(p=>p.value); if(vals.length<2) return '';
  const base=series.base!=null?series.base:vals[0];
  const last=vals[vals.length-1];
  const hi=daily?Math.max(...series.bars.map(b=>b.high)):Math.max(...vals);
  const lo=daily?Math.min(...series.bars.map(b=>b.low)):Math.min(...vals);
  const chg=base?(last-base)/base*100:0, amp=lo?(hi-lo)/lo*100:0;
  let s='';
  if(period==='day'&&WAVE.prev_close) s+=`<span>昨收 <b>${(+WAVE.prev_close).toFixed(2)}</b></span>`;
  s+=`<span>期间涨跌 <b class="${clr(chg)}">${sgn(chg)}${chg.toFixed(2)}%</b></span>`
    +`<span>最高 <b>${hi.toFixed(2)}</b></span><span>最低 <b>${lo.toFixed(2)}</b></span>`
    +`<span>振幅 <b>${amp.toFixed(2)}%</b></span>`;
  if(daily){ const vol=_annVol(vals); if(vol!=null) s+=`<span>年化波动 <b>${vol.toFixed(1)}%</b></span>`; }
  return s;
}
function waveCap(period){
  if(period==='day'){
    const ts=WAVE&&WAVE._minTs?` · 刷新于 ${WAVE._minTs}`:'';
    return '当日分时：逐分钟成交价，横向虚线=昨收基准。红涨绿跌，鼠标移上去看该分钟价与涨跌%。交易时段每30s自动刷新'+ts+'。';
  }
  if(period==='5d') return '近 5 交易日 5 分钟线。鼠标移到线上看每根具体价。';
  return '日K蜡烛：实体=开盘↔收盘（红阳/绿阴），影线=最高/最低价；橙线 MA5、蓝线 MA20，下方为成交量。鼠标移上去看当日 OHLC；年化波动由区间内日收益率折算。';
}
function renderWave(){
  const pane=document.getElementById('pane_wave'); if(!pane) return;
  if(!WAVE){ pane.innerHTML='<div class="paneempty"><span class="spin"></span> 拉取行情数据…</div>'; return; }
  const chips=WAVE_PERIODS.map(([k,l])=>`<button class="wave-chip${WAVE_PERIOD===k?' on':''}" onclick="setWavePeriod('${k}')">${l}</button>`).join('');
  pane.innerHTML='<div class="subh">行情 · 分时/日K 多周期（鼠标移上去看每点具体值）</div>'
    +`<div class="wave-chips">${chips}</div><div id="wave_body"></div>`;
  renderWavePeriod();
}
function setWavePeriod(p){ WAVE_PERIOD=p; renderWave(); }
function renderWavePeriod(){
  const body=document.getElementById('wave_body'); if(!body) return;
  const series=waveSeries(WAVE_PERIOD);
  const arr=series.kind==='daily'?series.bars:series.pts;
  if(!arr||arr.length<2){
    WAVE_CTX=null; KL_CTX=null;
    body.innerHTML='<div class="paneempty">该周期暂无数据（分时在非交易时段/新股可能为空，试试 5日 / 近1月）。</div>';
    return;
  }
  let chart, extra='';
  if(series.kind==='daily'){
    chart=candlestick(series.bars);
    extra='<div class="subh">箱形图 · 收盘价分布</div><div class="wavewrap">'+boxplot(series.bars.map(b=>b.close))+'</div>'
      +'<div class="chartcap">箱体=价格中间50%区间（下沿 Q1 / 中线=中位数 / 上沿 Q3），须线到最高/最低；<b>★=当前价</b>。</div>';
  } else {
    chart=waveChart(series);
  }
  body.innerHTML='<div class="wavewrap">'+chart+'</div>'
    +`<div class="wave-stats">${waveStats(series,WAVE_PERIOD)}</div>`
    +`<div class="chartcap">${waveCap(WAVE_PERIOD)}</div>`
    +extra;
}
function waveChart(series){
  const pts=series.pts,n=pts.length,vals=pts.map(p=>p.value);
  const W=640,H=200,padL=48,padR=10,padT=12,padB=22,plotW=W-padL-padR,plotH=H-padT-padB;
  let min=Math.min(...vals),max=Math.max(...vals);
  const pad=(max-min)*0.06||max*0.01||1; min-=pad; max+=pad; const rng=max-min||1;
  const xOf=i=> padL+(n>1? i*plotW/(n-1):plotW/2);
  const yOf=v=> padT+(max-v)/rng*plotH;
  const line=pts.map((p,i)=>`${xOf(i).toFixed(1)},${yOf(p.value).toFixed(1)}`).join(' ');
  const base=series.base!=null?series.base:vals[0];
  const up=vals[n-1]>=base, col=up?'var(--up)':'var(--down)', fill=up?'rgba(255,77,94,.08)':'rgba(34,201,139,.08)';
  const baseY=(base>=min&&base<=max)?yOf(base):null;
  WAVE_CTX={pts,base,geom:{W,padL,padR,padT,plotW,plotH,n,min,max,rng},xOf,yOf};
  return `<svg viewBox="0 0 ${W} ${H}" onmousemove="waveHover(event)" onmouseleave="waveHoverEnd()">
    <polygon points="${padL},${padT+plotH} ${line} ${padL+plotW},${padT+plotH}" fill="${fill}"/>
    ${baseY!=null?`<line x1="${padL}" y1="${baseY.toFixed(1)}" x2="${padL+plotW}" y2="${baseY.toFixed(1)}" stroke="var(--line2)" stroke-dasharray="4 3"/>`:''}
    <polyline points="${line}" fill="none" stroke="${col}" stroke-width="1.6"/>
    <line id="wave_cross" x1="0" y1="${padT}" x2="0" y2="${padT+plotH}" stroke="var(--gold)" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>
    <circle id="wave_dot" r="3.2" fill="var(--gold)" stroke="#05070b" stroke-width="1" style="display:none"/>
    <text x="${padL-4}" y="${padT+6}" fill="var(--muted)" font-size="10" font-family="monospace" text-anchor="end">${max.toFixed(2)}</text>
    <text x="${padL-4}" y="${padT+plotH}" fill="var(--muted)" font-size="10" font-family="monospace" text-anchor="end">${min.toFixed(2)}</text>
    <text x="${padL}" y="${H-6}" fill="var(--muted)" font-size="10" font-family="monospace">${pts[0].label}</text>
    <text x="${padL+plotW}" y="${H-6}" fill="var(--muted)" font-size="10" font-family="monospace" text-anchor="end">${pts[n-1].label}</text>
  </svg>`;
}
function waveHover(e){
  const ctx=WAVE_CTX; if(!ctx) return;
  const svg=e.currentTarget, rect=svg.getBoundingClientRect(), g=ctx.geom;
  const localX=(e.clientX-rect.left)*(g.W/rect.width);
  let i=Math.round((localX-g.padL)/(g.n>1? g.plotW/(g.n-1):1));
  i=Math.max(0,Math.min(g.n-1,i));
  const p=ctx.pts[i], x=ctx.xOf(i), y=ctx.yOf(p.value);
  const cross=document.getElementById('wave_cross'), dot=document.getElementById('wave_dot');
  if(cross){ cross.setAttribute('x1',x); cross.setAttribute('x2',x); cross.style.display=''; }
  if(dot){ dot.setAttribute('cx',x); dot.setAttribute('cy',y); dot.style.display=''; }
  const base=ctx.base!=null?ctx.base:ctx.pts[0].value;
  const chg=base?(p.value-base)/base*100:0;
  const tip=document.getElementById('tip');
  tip.innerHTML=`${p.label}　<b>${p.value.toFixed(2)}</b>　<span style="color:${chg>=0?'#ff4d5e':'#22c98b'}">${sgn(chg)}${chg.toFixed(2)}%</span>`;
  tip.style.display='block';
  tip.style.left=Math.min(e.clientX+14, window.innerWidth-tip.offsetWidth-14)+'px';
  tip.style.top=(e.clientY+16)+'px';
}
function waveHoverEnd(){
  document.getElementById('tip').style.display='none';
  ['wave_cross','wave_dot','kl_cross'].forEach(id=>{ const el=document.getElementById(id); if(el) el.style.display='none'; });
}
/* ── 北京时间交易时段判定（不看本地时区，兼容跨时区机器）+ 分时自动刷新 ── */
function _cnTradingNow(){
  const parts={};
  new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Shanghai',weekday:'short',hour:'2-digit',minute:'2-digit',hour12:false})
    .formatToParts(new Date()).forEach(p=>{ parts[p.type]=p.value; });
  if(parts.weekday==='Sat'||parts.weekday==='Sun') return false;
  let hh=parseInt(parts.hour,10); if(hh===24) hh=0;
  const mins=hh*60+parseInt(parts.minute,10);
  return (mins>=565&&mins<=695)||(mins>=775&&mins<=905);   // 09:25–11:35 / 12:55–15:05
}
function tickMinute(code){
  if(WAVE_PERIOD!=='day' || !WAVE) return;                                  // 只在分时标签刷
  if(!document.getElementById('drawer').classList.contains('open')) return; // 抽屉关了不刷
  if(!_cnTradingNow()) return;                                              // 非交易时段不刷
  const gen=detailSeq;                                                      // 请求令牌，防切股票错位
  fetch('/api/minute/'+code).then(r=>r.json()).then(m=>{
    if(gen!==detailSeq || WAVE_PERIOD!=='day' || !WAVE) return;
    if(m.intraday&&m.intraday.length) WAVE.intraday=m.intraday;
    if(m.prev_close!=null) WAVE.prev_close=m.prev_close;
    WAVE._minTs=new Date().toLocaleTimeString('zh-CN',{hour12:false});
    renderWavePeriod();
  }).catch(()=>{});
}

/* ── 日K蜡烛图（复用于「行情」日K周期）+ 箱形图 ── */
function candlestick(kl){
  const w=640,h=260,padL=46,padR=8,padT=10,volH=50,cH=h-volH-padT-18;
  const hi=Math.max(...kl.map(k=>k.high)),lo=Math.min(...kl.map(k=>k.low)),rng=hi-lo||1;
  const n=kl.length,cw=(w-padL-padR)/n,bw=Math.max(1.5,cw*0.62);
  const yP=p=>padT+(hi-p)/rng*cH;
  const maLine=(key,col)=>{let pts=[];for(let i=0;i<n;i++){const v=kl[i][key];if(v!=null)pts.push(`${(padL+i*cw+cw/2).toFixed(1)},${yP(v).toFixed(1)}`);}return pts.length>1?`<polyline points="${pts.join(' ')}" fill="none" stroke="${col}" stroke-width="1.1" opacity=".9"/>`:'';};
  let candles='';
  for(let i=0;i<n;i++){const k=kl[i],x=padL+i*cw+cw/2,up=k.close>=k.open,col=up?'var(--up)':'var(--down)';
    const yO=yP(k.open),yC=yP(k.close),top=Math.min(yO,yC),bh=Math.max(1,Math.abs(yC-yO));
    candles+=`<line x1="${x.toFixed(1)}" y1="${yP(k.high).toFixed(1)}" x2="${x.toFixed(1)}" y2="${yP(k.low).toFixed(1)}" stroke="${col}" stroke-width="1"/>`
      +`<rect x="${(x-bw/2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" fill="${col}"/>`;}
  const vmax=Math.max(...kl.map(k=>k.volume),1),vY=h-18;
  let vols='';
  for(let i=0;i<n;i++){const k=kl[i],x=padL+i*cw+cw/2,vh=k.volume/vmax*volH;
    vols+=`<rect x="${(x-bw/2).toFixed(1)}" y="${(vY-vh).toFixed(1)}" width="${bw.toFixed(1)}" height="${vh.toFixed(1)}" fill="${k.close>=k.open?'var(--up)':'var(--down)'}" opacity=".5"/>`;}
  let grid='';
  for(let g=0;g<=4;g++){const p=hi-rng*g/4,y=padT+cH*g/4;
    grid+=`<line x1="${padL}" y1="${y.toFixed(1)}" x2="${w-padR}" y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width=".5"/>`
      +`<text x="4" y="${(y+3).toFixed(1)}" fill="var(--muted)" font-size="9" font-family="monospace">${p.toFixed(2)}</text>`;}
  KL_CTX={bars:kl, W:w, padL, cw, n, vY, padT};
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;min-width:520px;height:${h}px" onmousemove="klHover(event)" onmouseleave="waveHoverEnd()">
    ${grid}${maLine('ma5','var(--gold)')}${maLine('ma20','#4aa3ff')}${candles}${vols}
    <line id="kl_cross" x1="0" y1="${padT}" x2="0" y2="${vY.toFixed(1)}" stroke="var(--gold)" stroke-width="1" stroke-dasharray="3 3" style="display:none"/>
    <text x="${padL}" y="${h-4}" fill="var(--muted)" font-size="9" font-family="monospace">${kl[0].date.slice(5)}</text>
    <text x="${w-padR-28}" y="${h-4}" fill="var(--muted)" font-size="9" font-family="monospace">${kl[n-1].date.slice(5)}</text></svg>`;
}
function klHover(e){
  const ctx=KL_CTX; if(!ctx) return;
  const svg=e.currentTarget, rect=svg.getBoundingClientRect();
  const localX=(e.clientX-rect.left)*(ctx.W/rect.width);
  let i=Math.floor((localX-ctx.padL)/ctx.cw); i=Math.max(0,Math.min(ctx.n-1,i));
  const k=ctx.bars[i], x=ctx.padL+i*ctx.cw+ctx.cw/2;
  const cross=document.getElementById('kl_cross');
  if(cross){ cross.setAttribute('x1',x.toFixed(1)); cross.setAttribute('x2',x.toFixed(1)); cross.style.display=''; }
  const prev=i>0?ctx.bars[i-1].close:k.open, chg=prev?(k.close-prev)/prev*100:0;
  const tip=document.getElementById('tip');
  tip.innerHTML=`${k.date}　开<b>${k.open.toFixed(2)}</b> 高<b>${k.high.toFixed(2)}</b> 低<b>${k.low.toFixed(2)}</b> 收<b>${k.close.toFixed(2)}</b>　<span style="color:${chg>=0?'#ff4d5e':'#22c98b'}">${sgn(chg)}${chg.toFixed(2)}%</span>`;
  tip.style.display='block';
  tip.style.left=Math.min(e.clientX+14, window.innerWidth-tip.offsetWidth-14)+'px';
  tip.style.top=(e.clientY+16)+'px';
}
function boxplot(vals){
  const s=[...vals].sort((a,b)=>a-b),n=s.length;
  const q=p=>{const idx=(n-1)*p,l=Math.floor(idx),h2=Math.ceil(idx);return s[l]+(s[h2]-s[l])*(idx-l);};
  const min=s[0],q1=q(.25),med=q(.5),q3=q(.75),max=s[n-1],cur=vals[vals.length-1];
  const w=640,h=128,padL=30,padR=30,y=52,bh=34,rng=(max-min)||1;
  const X=p=>padL+(p-min)/rng*(w-padL-padR);
  const lbl=(p,t,dy)=>`<line x1="${X(p).toFixed(1)}" y1="${y-4}" x2="${X(p).toFixed(1)}" y2="${y+bh+4}" stroke="var(--line2)" stroke-width=".5"/><text x="${X(p).toFixed(1)}" y="${dy}" fill="var(--muted)" font-size="9" font-family="monospace" text-anchor="middle">${t}${p.toFixed(2)}</text>`;
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;min-width:520px;height:${h}px">
    <line x1="${X(min)}" y1="${y+bh/2}" x2="${X(max)}" y2="${y+bh/2}" stroke="var(--muted)" stroke-width="1"/>
    <line x1="${X(min)}" y1="${y+7}" x2="${X(min)}" y2="${y+bh-7}" stroke="var(--muted)"/>
    <line x1="${X(max)}" y1="${y+7}" x2="${X(max)}" y2="${y+bh-7}" stroke="var(--muted)"/>
    <rect x="${X(q1).toFixed(1)}" y="${y}" width="${(X(q3)-X(q1)).toFixed(1)}" height="${bh}" fill="rgba(224,169,46,.14)" stroke="var(--gold)" stroke-width="1"/>
    <line x1="${X(med).toFixed(1)}" y1="${y}" x2="${X(med).toFixed(1)}" y2="${y+bh}" stroke="var(--gold)" stroke-width="1.6"/>
    <text x="${X(cur).toFixed(1)}" y="${y-7}" fill="var(--txt)" font-size="13" text-anchor="middle">★</text>
    ${lbl(min,'低',24)}${lbl(q1,'Q1',108)}${lbl(med,'中',24)}${lbl(q3,'Q3',108)}${lbl(max,'高',24)}</svg>`;
}

/* ── 大盘研判条 ── */
async function loadMarket(force){
  const gen=++mktSeq;
  // 1) 先拉指数(快) → 即时渲染行情条
  try{
    const q=await (await fetch('/api/market/overview?ai=0')).json();
    if(gen!==mktSeq) return;
    renderMarketIdx(q);
  }catch(e){}
  // 2) 再拉完整(含情绪 + AI 研判；命中缓存则秒回，force 时强制重算)
  try{
    const j=await (await fetch('/api/market/overview'+(force?'?refresh=1':''))).json();
    if(gen!==mktSeq) return;
    renderMarketIdx(j); renderMarketDetail(j);
  }catch(e){}
}
function renderMarketIdx(j){
  const idx=j.indices||[];
  let h=idx.map(i=>`<span class="ix"><b>${i.name}</b> <span class="${clr(i.chg_pct)}">${fmt(i.point)} ${sgn(i.chg_pct)}${fmt(i.chg_pct)}%</span></span>`).join('');
  if(j.amount_liang_yi!=null) h+=`<span class="ix"><b>两市成交</b> <span class="pt">${fmtInt(j.amount_liang_yi)}亿</span></span>`;
  document.getElementById('mktIdx').innerHTML = h || '<span class="muted small">指数数据暂缺</span>';
  if(j.ai&&j.ai.one_liner) document.getElementById('mktOne').textContent='📊 '+j.ai.one_liner;
}
function renderMarketDetail(j){
  const ai=j.ai, b=j.breadth||{}, rows=[];
  if(ai){
    if(ai.regime) rows.push(['状态',`<span class="mkt-regime">${ai.regime}</span> ${ai.style||''}`]);
    if(ai.sentiment) rows.push(['赚钱效应',ai.sentiment]);
    if(ai.risk) rows.push(['主要风险',ai.risk]);
    if(ai.guidance) rows.push(['选股指导',ai.guidance]);
  }
  const sem=[];
  if(b.advancers!=null) sem.push(`涨 ${b.advancers} / 跌 ${b.decliners} 家`);
  if(b.limit_up!=null) sem.push(`涨停 ${b.limit_up} / 跌停 ${b.limit_down}`);
  if(sem.length) rows.push(['市场广度',sem.join('　·　')]);
  const ind=x=>`${x.name} <span class="${clr(x.chg_pct)}">${sgn(x.chg_pct)}${fmt(x.chg_pct)}%</span>`;
  if(b.top_industries&&b.top_industries.length) rows.push(['领涨行业', b.top_industries.map(ind).join('　')]);
  if(b.bottom_industries&&b.bottom_industries.length) rows.push(['领跌行业', b.bottom_industries.map(ind).join('　')]);
  let h=rows.map(([k,v])=>`<div class="mrow"><div class="mk">${k}</div><div class="mv">${v}</div></div>`).join('');
  if(!ai&&!rows.length) h='<div class="mrow"><div class="mv muted">大盘研判暂不可用（未配置 AI 或数据拉取失败）。</div></div>';
  const when=j.analyzed_at?j.analyzed_at.replace('T',' ').slice(0,16):(j.updated||'');
  const age=j.age_min!=null?'（'+(j.age_min<1?'刚刚':j.age_min+'分钟前')+'）':'';
  h+=`<div class="mkt-disc">${j.model||''} 大盘研判为参考信号，只据当日客观数据、不预测方向，不构成投资建议。`
    +`　🕐 ${when}${age} · ${j.cached?'命中缓存':'实时'} `
    +`<button class="mini" onclick="loadMarket(true)">🔄 强制刷新</button></div>`;
  document.getElementById('mktDetail').innerHTML=h;
}
function toggleMkt(){ document.getElementById('mktBar').classList.toggle('open'); }

/* 板块两级分组下拉 */
function populateFocus(){
  if(!TAXO) return;
  const sel=document.getElementById('scr_focus');
  let h='<option value="">不限（全市场）</option>';
  for(const [primary, subs] of Object.entries(TAXO)){
    h+=`<option value="${primary}">▶ 整个「${primary}」板块</option>`;
    h+=`<optgroup label="${primary}">`+(subs||[]).map(s=>`<option value="${s}">　${s}</option>`).join('')+'</optgroup>';
  }
  sel.innerHTML=h;
}

/* ── 配置 / AI 可用性 ── */
async function loadConfig(){
  try{
    const j=await (await fetch('/api/config')).json();
    LLM=j.llm_enabled; MODEL=j.model||''; WEB=j.web_search;
    TAXO=j.taxonomy||null; populateFocus();
    document.querySelectorAll('.ai-only').forEach(el=>el.style.display=LLM?'':'none');
    const chip=document.getElementById('aichip');
    chip.textContent=LLM?`🤖 ${MODEL} · 📰新闻${WEB?' · 🌐联网':''}`:'🤖 未配置';
    chip.className='chip'+(LLM?' ok':'');
    chip.dataset.tip = WEB
      ? 'AI 已接入：实时财经/政策快讯(A) + 博查联网搜索(B)。每次分析都会读最新资讯。'
      : 'AI 已接入实时财经/政策快讯(A)。在 .env 填 BOCHA_API_KEY 即可启用联网搜索(B)。';
    if(WEB) checkWebSearch(true);   // 启动时探测博查 key 是否有效 → 到期提醒
  }catch(e){}
}
async function checkWebSearch(probe){
  if(!WEB) return;
  const warn=document.getElementById('webWarn');
  try{
    const j=await (await fetch('/api/websearch/status'+(probe?'?probe=1':''))).json();
    if(j.configured && j.ok===false){
      document.getElementById('webWarnMsg').innerHTML=
        '⚠ 博查联网搜索(B)当前不可用：'+(j.reason||'未知错误')+(j.checked_at?`　·　检测于 ${j.checked_at}`:'');
      warn.style.display='flex';
      const chip=document.getElementById('aichip');
      if(chip.textContent.includes('🌐联网')&&!chip.textContent.includes('⚠'))
        chip.textContent=chip.textContent.replace('🌐联网','🌐联网⚠');
    }else{
      warn.style.display='none';
      const chip=document.getElementById('aichip');
      chip.textContent=chip.textContent.replace('🌐联网⚠','🌐联网');
    }
  }catch(e){}
}

/* ── 每日 AI 推荐 ── */
const ACT={buy:['买入','a-buy'],add:['加仓','a-buy'],hold:['持有','a-hold'],reduce:['减仓','a-sell'],sell:['卖出','a-sell'],watch:['观望','a-watch']};
async function runDaily(force){
  const gen=++recSeq;
  const box=document.getElementById('recBody'); const panel=document.getElementById('recPanel');
  document.getElementById('recTitle').textContent='🤖 自选股推荐（含持仓）';
  document.getElementById('recControls').style.display='none';
  panel.classList.add('open');
  box.innerHTML='<div class="paneempty"><span class="spin"></span> '+MODEL+(force?' 重新分析':' 正在分析')+'自选股与持仓…（命中缓存则秒回，否则推理约 15~40 秒）</div>';
  let j;
  try{ j=await (await fetch('/api/recommend/daily',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})})).json(); }
  catch(e){ if(gen!==recSeq)return; box.innerHTML='<div class="paneempty">请求失败：'+e+'</div>'; return; }
  if(gen!==recSeq) return;   // 已切到别的请求，丢弃这次过期响应
  if(!j.ok){ box.innerHTML='<div class="paneempty">生成失败：'+(j.msg||'')+'</div>'; return; }
  const r=j.result||{};
  let h=aiMeta(j,'runDaily(true)')+`<div class="mview">📊 ${r.market_view||''}</div>`;
  h+=`<div class="ov-note">组合级速览（浅层指标粗筛）。<b>持仓的最终买卖以「🤖 何时卖」为准</b>——本面板持仓默认倾向持有/观望。</div>`;
  h+='<div class="reccards">'+(r.picks||[]).map(p=>{
    const a=ACT[p.action]||['?','a-hold'];
    return `<div class="reccard ${a[1]}"><div class="rc-top"><span class="badge ${a[1]}">${a[0]}</span>
      <span class="rc-name">${p.name||''} <em>${p.code||''}</em>${p.held?' <span class="rule-scen">持仓</span>':''}</span>
      <span class="rc-conf">${({high:'高',mid:'中',low:'低'})[p.confidence]||''}信心</span></div>
      <div class="rc-reason">${p.reason||''}</div>${p.risk?`<div class="rc-risk">⚠ ${p.risk}</div>`:''}</div>`;
  }).join('')+'</div>';
  if(r.holdings_note&&r.holdings_note!=='无') h+=`<div class="hnote">💼 持仓提醒：${r.holdings_note}</div>`;
  h+=`<div class="disc">以上为 ${j.model} 基于当前客观数据生成的参考信号，不构成投资建议，据此操作风险自负。</div>`;
  box.innerHTML=h;
}
function closeRec(){ ++recSeq; document.getElementById('recPanel').classList.remove('open'); }

/* ── 全市场筛选 ── */
function openScreen(){
  ++recSeq;   // 作废在飞的旧请求，避免其晚返回覆盖本面板
  document.getElementById('recTitle').textContent='🔍 全市场选股（结合大盘 · 跨板块 · 可下钻二级）';
  document.getElementById('recControls').style.display='flex';
  document.getElementById('recPanel').classList.add('open');
  document.getElementById('recBody').innerHTML='<div class="paneempty">选资金规模与侧重板块（可选整个一级或某个二级细分）→ 点「开始筛选」。DeepSeek 会先看当前大盘，再从全市场候选里跨板块为你选股。</div>';
}
async function runScreen(force){
  const gen=++recSeq;
  const cap=+document.getElementById('scr_capital').value;
  const focus=document.getElementById('scr_focus').value;
  const box=document.getElementById('recBody');
  box.innerHTML='<div class="paneempty"><span class="spin"></span> '+MODEL+(force?' 重新筛选':' 正在拉取候选池行情并跨板块筛选')+'…（命中缓存则秒回，否则约 40~90 秒）</div>';
  let j;
  try{ j=await (await fetch('/api/recommend/screen',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({capital:cap,focus_sector:focus,force:!!force})})).json(); }
  catch(e){ if(gen!==recSeq)return; box.innerHTML='<div class="paneempty">请求失败：'+e+'</div>'; return; }
  if(gen!==recSeq) return;   // 已切到别的请求，丢弃这次过期响应
  if(!j.ok){ box.innerHTML='<div class="paneempty">筛选失败：'+(j.msg||'')+'</div>'; return; }
  const r=j.result||{};
  const regime=j.market_regime||r.market_regime;
  let h=aiMeta(j,'runScreen(true)')+`<div class="mview">🔍 候选 ${j.candidates||'—'} 只 · 资金 ${(+j.capital).toLocaleString()} 元${j.focus?' · 侧重 '+j.focus:''}${regime?' · 大盘 '+regime:''}<br>📊 ${r.overall||''}</div>`;
  h+='<div class="reccards">'+(r.picks||[]).map(p=>{
    const a=ACT[p.action]||['关注','a-watch'];
    const sec=[p.primary,p.sub||p.sector].filter(Boolean).join('·');
    return `<div class="reccard ${a[1]}"><div class="rc-top"><span class="badge ${a[1]}">${a[0]}</span>
      <span class="rc-name">${p.name||''} <em>${p.code||''}</em></span>
      <span class="rc-sector">${sec}</span></div>
      <div class="rc-reason">${p.reason||''}</div>
      <div class="rc-top" style="margin-top:8px">
        <span class="rc-lot">1手 ${p.lot_cost?(+p.lot_cost).toLocaleString():'—'}元</span>
        ${p.risk?`<span class="rc-risk" style="margin:0">⚠ ${p.risk}</span>`:''}
        <button class="pick-add" onclick="addPick('${p.code}')">＋自选</button>
      </div></div>`;
  }).join('')+'</div>';
  if(r.budget_plan) h+=`<div class="planbox"><b>💰 ${(+j.capital).toLocaleString()}元 配置建议：</b>${r.budget_plan}</div>`;
  if(r.sector_view) h+=`<div class="planbox"><b>🧭 板块简评：</b>${r.sector_view}</div>`;
  h+=`<div class="disc">${j.model} 基于候选池客观指标的筛选参考，不构成投资建议。</div>`;
  box.innerHTML=h;
}
async function addPick(code){
  const j=await (await fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})})).json();
  if(j.ok){ load(); alert(`已加入自选：${j.name||code}`); } else alert(j.msg||'加入失败');
}

/* 单股深度入场分析（是否/何时/怎么买 + 未来卖出策略；不必持仓） */
async function entryAnalysis(code,force){
  const box=document.getElementById('detailEntry');
  box.innerHTML='<div class="paneempty small"><span class="spin"></span> '+MODEL+' 深度入场分析中…（约 20~60 秒）</div>';
  let j; try{ j=await (await fetch('/api/recommend/entry/'+code,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})})).json(); }
  catch(e){ box.innerHTML='<div class="paneempty small">失败：'+e+'</div>'; return; }
  if(j.ok){ box.innerHTML=aiMeta(j,`entryAnalysis('${code}',true)`)+entryHTML(j.advice,j.model); }
  else box.innerHTML='<div class="paneempty small">失败：'+(j.msg||'')+'</div>';
}
function entryHTML(a,model){
  const vmap={'值得入场':['值得入场','a-buy'],'观望等待':['观望','a-watch'],'不建议':['不建议','a-sell']};
  const m=vmap[a.verdict]||['参考','a-hold'];
  return `<div class="advice">
    <div class="rc-top"><span class="badge ${m[1]}" style="font-size:13px;padding:3px 12px">${m[0]}</span>
      <span class="rc-name">${model} · <b>深度入场分析</b> <span class="rc-conf">${({high:'高',mid:'中',low:'低'})[a.confidence]||''}信心</span></span></div>
    ${a.market_fit?`<div class="rc-reason"><b>大盘契合：</b>${esc(a.market_fit)}</div>`:''}
    <div class="akv"><div><span>何时入场</span>${esc(a.entry_when)||'—'}</div>
      <div><span>买入价位</span>${esc(a.entry_zone)||'—'}</div>
      <div><span>怎么买</span>${esc(a.entry_how)||'—'}</div>
      <div><span>止损</span>${esc(a.stop_loss)||'—'}</div>
      <div><span>目标位</span>${esc(a.targets)||'—'}</div>
      <div><span>持有周期</span>${esc(a.hold_horizon)||'—'}</div></div>
    ${a.future_sell_plan?`<div class="adv-hold">🎯 未来卖出策略：${esc(a.future_sell_plan)}</div>`:''}
    ${a.risks?`<div class="rc-risk">⚠ ${esc(a.risks)}</div>`:''}
    <div class="rc-reason"><b>结论：</b>${esc(a.reason)}</div>
    ${a.rule_basis?`<div class="adv-rule">📐 依据规则：${esc(a.rule_basis)}</div>`:''}
    <div class="disc">AI 深度入场参考，遵循价格行为框架规则；不构成投资建议。</div></div>`;
}

/* 抽屉里让 AI 分析单只（不必持仓） */
async function askDetailAdvice(code,force){
  const box=document.getElementById('detailAdvice');
  box.innerHTML='<div class="paneempty small"><span class="spin"></span> '+MODEL+(force?' 重新分析…':' 分析中…')+'</div>';
  const inPortfolio=await isHeld(code);
  if(!inPortfolio){
    box.innerHTML='<div class="advice"><div class="note">提示：先在下方“持仓”里记录这只（含成本价），AI 才能给出结合你成本的卖出/止损建议。当前给通用参考。</div></div>';
  }
  try{
    const j=await (await fetch('/api/recommend/position/'+code,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})})).json();
    if(j.ok){ box.innerHTML=aiMeta(j,`askDetailAdvice('${code}',true)`)+adviceHTML(j.advice, j.model); }
    else if(j.msg && j.msg.includes('不在持仓')){
      box.innerHTML='<div class="advice"><div class="note">这只不在持仓中，无法给结合成本的建议。可在下方“持仓”记录后再试；或直接参考上方“每日推荐”。</div></div>';
    } else box.innerHTML='<div class="paneempty small">失败：'+(j.msg||'')+'</div>';
  }catch(e){ box.innerHTML='<div class="paneempty small">失败：'+e+'</div>'; }
}
async function isHeld(code){ const j=await (await fetch('/api/portfolio')).json(); return (j.holdings||[]).some(h=>h.code===code); }

function adviceHTML(a,model){
  const map={hold:['持有','a-hold'],add:['加仓','a-buy'],reduce:['减仓','a-sell'],sell:['卖出','a-sell']};
  const m=map[a.action]||['参考','a-hold'];
  return `<div class="advice">
    <div class="rc-top"><span class="badge ${m[1]}" style="font-size:13px;padding:3px 12px">${m[0]}</span>
      <span class="rc-name">${model} · <b>持仓权威判断</b>（比自选推荐更深）</span></div>
    ${a.hold_horizon?`<div class="adv-hold">⏳ 建议持有周期：${esc(a.hold_horizon)}</div>`:''}
    <div class="akv"><div><span>卖出条件</span>${esc(a.sell_trigger)||'—'}</div>
    <div><span>加仓条件</span>${esc(a.add_trigger)||'—'}</div>
    <div><span>止损参考</span>${esc(a.stop_loss)||'—'}</div>
    <div><span>止盈参考</span>${esc(a.take_profit)||'—'}</div></div>
    ${a.fundamental?`<div class="rc-reason"><b>📊 基本面：</b>${esc(a.fundamental)}</div>`:''}
    ${a.policy_news?`<div class="rc-reason"><b>📰 政策/新闻：</b>${esc(a.policy_news)}</div>`:''}
    <div class="rc-reason"><b>结论：</b>${esc(a.reason)}</div>
    ${a.rule_basis?`<div class="adv-rule">📐 依据规则：${esc(a.rule_basis)}</div>`:''}
    <div class="disc">AI 参考信号，结合波动史/财报/新闻/规则；**持仓买卖以此为准**（自选推荐仅组合速览）。不构成投资建议。</div></div>`;
}

/* ── 持仓 ── */
async function loadPortfolio(){
  let j; try{ j=await (await fetch('/api/portfolio')).json(); }catch(e){return;}
  const s=j.summary||{}, hs=j.holdings||[];
  const ag=document.getElementById('assetGrid');
  if(s.count){
    const card=(lab,big,cls,sub)=>`<div class="assetCard${cls?' hl':''}"><div class="lab">${lab}</div><div class="big ${cls}">${big}</div><div class="sub ${cls}">${sub}</div></div>`;
    ag.innerHTML=
      card('总市值', fmtInt(s.market_value)+'元','','成本 '+fmtInt(s.cost_value)+'元')
     +card('总盈亏', (s.pnl_amount>=0?'+':'')+fmtInt(s.pnl_amount)+'元', clr(s.pnl_amount), s.pnl_pct!=null?sgn(s.pnl_pct)+s.pnl_pct+'%':'—')
     +card('当日盈亏', (s.today_pnl>=0?'+':'')+fmtInt(s.today_pnl)+'元', clr(s.today_pnl), s.today_pnl_pct!=null?sgn(s.today_pnl_pct)+s.today_pnl_pct+'%':'—')
     +card('持仓', s.count+' 只','','分散度');
  }else ag.innerHTML='<div class="assetCard" style="grid-column:1/-1"><div class="lab">暂无持仓</div><div class="sub muted" style="margin-top:8px">在下方表单录入「代码 + 股数 + 成本价」，即可看总盈亏与当日盈亏，并让 AI 给卖出建议。</div></div>';
  const tb=document.getElementById('folioRows');
  tb.innerHTML = hs.length ? hs.map(h=>`
    <tr>
      <td class="nm"><div class="n">${h.name||h.code}</div><div class="c">${h.code}</div></td>
      <td>${fmtInt(h.shares)}</td>
      <td>${fmt(h.cost_price)}</td>
      <td class="${clr(h.chg_pct)}">${fmt(h.price)}</td>
      <td>${fmtInt(h.market_value)}</td>
      <td class="${clr(h.pnl_pct)}"><b>${h.pnl_pct!=null?sgn(h.pnl_pct)+h.pnl_pct+'%':'—'}</b><div class="small">${sgn(h.pnl_amount)}${fmtInt(h.pnl_amount)}元</div></td>
      <td class="${clr(h.today_pnl)}"><b>${h.today_pnl!=null?(h.today_pnl>=0?'+':'')+fmtInt(h.today_pnl):'—'}</b><div class="small ${clr(h.chg_pct)}">${sgn(h.chg_pct)}${fmt(h.chg_pct)}%</div></td>
      <td>${h.buy_date||'—'}</td>
      <td class="acts">
        ${LLM?`<button class="mini ai" onclick="folioAdvice('${h.code}')">🤖 何时卖</button>`:''}
        <button class="mini" onclick="openDetail('${h.code}')">深挖</button>
        <button class="mini danger" onclick="delHolding('${h.code}','${(h.name||h.code)}')">清仓</button>
      </td>
    </tr><tr class="advrow" id="adv_${h.code}"><td colspan="9"></td></tr>`).join('') : '';
  // 刷新会重建上面的行 → 把已展开的「何时卖」建议重新注入，避免一闪而过被清掉
  const codes=new Set(hs.map(h=>h.code));
  Object.keys(FOLIO_ADV).forEach(code=>{
    if(!codes.has(code)){ delete FOLIO_ADV[code]; return; }   // 已清仓则丢弃缓存
    const row=document.getElementById('adv_'+code); if(!row) return;
    row.classList.add('open');
    row.firstElementChild.innerHTML = FOLIO_ADV[code].html
      || '<div class="paneempty small"><span class="spin"></span> '+MODEL+' 分析何时卖/加/止损…</div>';
  });
}
async function addHolding(){
  const code=document.getElementById('h_code').value.trim();
  const shares=document.getElementById('h_shares').value.trim();
  const cost=document.getElementById('h_cost').value.trim();
  const date=document.getElementById('h_date').value.trim();
  if(!code||!shares||!cost){alert('请填代码、股数、成本价');return;}
  const j=await (await fetch('/api/portfolio/add',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code,shares,cost_price:cost,buy_date:date})})).json();
  if(!j.ok){alert(j.msg||'记录失败');return;}
  ['h_code','h_shares','h_cost','h_date'].forEach(id=>document.getElementById(id).value='');
  loadPortfolio();
}
async function delHolding(code,name){
  if(!confirm(`确认从持仓移除 ${name}（${code}）？（只删记录，不影响你的真实账户）`))return;
  await fetch('/api/portfolio/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
  loadPortfolio();
}
function folioAdvice(code){   // 「🤖 何时卖」按钮：展开/收起切换
  const row=document.getElementById('adv_'+code); if(!row) return;
  if(row.classList.contains('open')){   // 再次点击=收起
    row.classList.remove('open'); row.firstElementChild.innerHTML=''; delete FOLIO_ADV[code]; return;
  }
  row.classList.add('open');
  loadFolioAdvice(code, false);
}
async function loadFolioAdvice(code, force){   // 真正拉取（强制刷新走这里，不切换开合）
  const row=document.getElementById('adv_'+code); if(!row) return;
  FOLIO_ADV[code]={loading:true};   // 标记「用户要看」，刷新时据此重注入(spinner/结果)
  row.firstElementChild.innerHTML='<div class="paneempty small"><span class="spin"></span> '+MODEL+(force?' 重新分析…':' 分析何时卖/加/止损…')+'</div>';
  let html;
  try{
    const j=await (await fetch('/api/recommend/position/'+code,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})})).json();
    html = j.ok ? (aiMeta(j,`loadFolioAdvice('${code}',true)`)+adviceHTML(j.advice,j.model)) : '<div class="paneempty small">失败：'+(j.msg||'')+'</div>';
  }catch(e){ html='<div class="paneempty small">失败：'+e+'</div>'; }
  if(!FOLIO_ADV[code]) return;   // 请求返回前用户已收起 → 丢弃
  FOLIO_ADV[code]={html};        // 缓存结果，跨自动刷新保留
  const r=document.getElementById('adv_'+code);   // 重新按 id 取，避免刷新后旧节点已脱离文档
  if(r){ r.classList.add('open'); r.firstElementChild.innerHTML=html; }
}

/* ── 名词解释总表 ── */
function openGloss(){
  const body=document.getElementById('glossBody');
  body.innerHTML=Object.entries(GLOSSARY).map(([k,v])=>`<div class="gitem"><div class="gk">${k}</div><div class="gv">${v}</div></div>`).join('');
  document.getElementById('glossModal').classList.add('open');
}
function closeGloss(){document.getElementById('glossModal').classList.remove('open');}

/* ── 热点新闻 / 政策（L2 本地资讯库） ── */
function openNews(){ buildNewsChips(); loadNews(); document.getElementById('newsModal').classList.add('open'); }
function closeNews(){ document.getElementById('newsModal').classList.remove('open'); }
function buildNewsChips(){
  const chips=[['全部',{}],['政策',{kind:'政策'}],['市场',{sector:'市场'}]];
  if(TAXO) for(const p of Object.keys(TAXO)) chips.push([p,{sector:p}]);
  document.getElementById('newsChips').innerHTML=chips.map(([label,f])=>{
    const on=(f.sector||'')===NEWS_FILTER.sector&&(f.kind||'')===NEWS_FILTER.kind;
    return `<button class="news-chip${on?' on':''}" onclick="setNewsFilter('${f.sector||''}','${f.kind||''}')">${label}</button>`;
  }).join('');
}
function setNewsFilter(sector,kind){ NEWS_FILTER={sector,kind,code:''}; buildNewsChips(); loadNews(); }
async function newsByCode(){   // L3：按代码看个股新闻 + 后台深抓更久历史
  const code=(document.getElementById('newsCode').value||'').trim();
  if(!/^\d{6}$/.test(code)){ alert('请输入 6 位代码'); return; }
  NEWS_FILTER={sector:'',kind:'',code}; buildNewsChips();
  fetch('/api/news/deepen',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})}).catch(()=>{});
  await loadNews();
  setTimeout(loadNews, 6000);   // 深抓完再刷一次，补上更久历史
}
async function loadNews(){
  const box=document.getElementById('newsBody');
  box.innerHTML='<div class="paneempty"><span class="spin"></span> 读取资讯库…</div>';
  const q=new URLSearchParams({sector:NEWS_FILTER.sector,kind:NEWS_FILTER.kind,code:NEWS_FILTER.code,limit:'80'});
  let j; try{ j=await (await fetch('/api/news?'+q)).json(); }
  catch(e){ box.innerHTML='<div class="paneempty">读取失败：'+e+'</div>'; return; }
  const s=j.status||{};
  document.getElementById('newsStat').textContent=
    `本地库 ${s.total||0} 条 · ${s.oldest||'—'}~${s.newest||'—'} · 抓取于 ${(s.last_fetch||'').replace('T',' ').slice(0,16)||'—'}`;
  const items=j.news||[];
  box.innerHTML = items.length ? items.map(n=>`
    <div class="news-item"><div class="nm2">
      <span class="nd">${n.date||''}</span>
      <span class="nk"${n.kind==='政策'?' style="color:var(--gold);border-color:var(--gold-dim)"':''}>${n.kind||''}</span>
      ${n.code?`<span class="nk">${n.code}</span>`:''}
      ${n.sector1&&n.sector1!=='市场'?`<span class="nk">${n.sector1}${n.sector2?'·'+n.sector2:''}</span>`:''}
      <span class="muted small">${n.source||''}</span>
    </div>${n.url?`<a href="${n.url}" target="_blank">${n.title} ↗</a>`:n.title}</div>`).join('')
    : '<div class="paneempty">该筛选下暂无新闻（库在积累中，点「🔄 刷新」抓一次或换筛选）。</div>';
}
async function refreshNews(){
  const btn=document.getElementById('newsRefreshBtn'); btn.textContent='抓取中…'; btn.disabled=true;
  try{ await fetch('/api/news/refresh',{method:'POST'}); }catch(e){}
  setTimeout(async()=>{ await loadNews(); btn.textContent='🔄 刷新'; btn.disabled=false; }, 6000);
}
// 看盘时惰性刷新：交易时段 + 每 15 分钟一次（piggyback 30s 自动刷新）
function maybeRefreshNews(){
  const now=Date.now(), d=new Date(), h=d.getHours(), wd=d.getDay();
  const trading = wd>=1&&wd<=5 && ((h>=9&&h<12)||(h>=13&&h<15));
  if(trading && now-lastNewsRefresh>15*60*1000){
    lastNewsRefresh=now;
    fetch('/api/news/refresh',{method:'POST'}).catch(()=>{});
  }
}

/* ── 我的笔记（L5 私域信息 + AI 辅助记录） ── */
let NOTE_DRAFT=null;
function openNotes(){ NOTE_DRAFT=null; document.getElementById('noteInput').value=''; document.getElementById('noteDraft').innerHTML=''; loadNotesList(); document.getElementById('notesModal').classList.add('open'); }
function closeNotes(){ document.getElementById('notesModal').classList.remove('open'); }
async function aiStructNote(){
  const content=document.getElementById('noteInput').value.trim(); if(!content){alert('先写点什么');return;}
  const d=document.getElementById('noteDraft'); d.innerHTML='<div class="paneempty small"><span class="spin"></span> '+MODEL+' 整理中…</div>';
  let j; try{ j=await (await fetch('/api/notes/structure',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})})).json(); }
  catch(e){ d.innerHTML='<div class="paneempty small">失败：'+e+'</div>'; return; }
  if(!j.ok){ d.innerHTML='<div class="paneempty small">失败：'+(j.msg||'')+'</div>'; return; }
  NOTE_DRAFT=j.draft||{};
  d.innerHTML=`<div class="note-draft"><b>AI 整理（确认后点「保存」）：</b>
    <div>摘要：${NOTE_DRAFT.summary||'—'}</div>
    <div>类型：${NOTE_DRAFT.kind||'—'} · 标签：${(NOTE_DRAFT.tags||[]).join(' / ')||'—'}</div>
    <div>关联：${(NOTE_DRAFT.codes||[]).join(', ')||'—'}${(NOTE_DRAFT.sectors||[]).length?' · '+(NOTE_DRAFT.sectors||[]).join('·'):''}</div></div>`;
}
async function saveNote(){
  const content=document.getElementById('noteInput').value.trim(); if(!content){alert('先写点什么');return;}
  const body=Object.assign({content}, NOTE_DRAFT||{});
  let j; try{ j=await (await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json(); }
  catch(e){ alert('保存失败：'+e); return; }
  if(!j.ok){ alert(j.msg||'保存失败'); return; }
  document.getElementById('noteInput').value=''; document.getElementById('noteDraft').innerHTML=''; NOTE_DRAFT=null; loadNotesList();
}
async function loadNotesList(){
  const box=document.getElementById('notesList');
  let j; try{ j=await (await fetch('/api/notes?limit=100')).json(); }catch(e){ box.innerHTML='<div class="paneempty">读取失败</div>'; return; }
  const items=j.notes||[];
  box.innerHTML = items.length ? items.map(n=>`
    <div class="note-item"><div class="nm2">
      <span class="nd">${(n.created_at||'').replace('T',' ').slice(0,16)}</span>
      ${n.kind?`<span class="nk">${n.kind}</span>`:''}
      ${n.tags?n.tags.split(',').filter(Boolean).map(t=>`<span class="nk">${t}</span>`).join(''):''}
      ${n.codes?`<span class="nk">${n.codes}</span>`:''}
      <button class="mini danger" style="margin-left:auto" onclick="delNote(${n.id})">删</button>
    </div>${n.ai_summary?`<div class="muted small">📌 ${n.ai_summary}</div>`:''}
    <div>${(n.content||'').replace(/</g,'&lt;')}</div></div>`).join('')
    : '<div class="paneempty">还没有笔记。写点你的判断，AI 分析时会作为你的私域认知参考。</div>';
}
async function delNote(id){ if(!confirm('删除这条笔记？'))return; await fetch('/api/notes/'+id,{method:'DELETE'}); loadNotesList(); }

/* ── 交易规则库（蒸馏自 PA_Agent，可增删改；启用中的注入 AI） ── */
let RULE_FILTER='', RULE_EDIT_ID=null, RULES_DATA=[], RULE_CATS=[];
let RULE_SCEN='', RULE_CAP_OPTS=[], RULE_HOR_OPTS=[];
function openRules(){ RULE_FILTER=''; RULE_EDIT_ID=null; document.getElementById('ruleForm').style.display='none'; loadRules(); document.getElementById('rulesModal').classList.add('open'); }
function closeRules(){ document.getElementById('rulesModal').classList.remove('open'); }
function scenSet(){ return new Set(RULE_SCEN.split(',').map(s=>s.trim()).filter(Boolean)); }
function ruleActive(r){ const t=(r.scenarios||'').split(',').map(s=>s.trim()).filter(s=>s&&s!=='通用'); if(!t.length) return true; const s=scenSet(); return t.some(x=>s.has(x)); }
async function loadRules(){
  const box=document.getElementById('rulesBody'); box.innerHTML='<div class="paneempty"><span class="spin"></span> 读取规则…</div>';
  let j; try{ j=await (await fetch('/api/rules')).json(); }
  catch(e){ box.innerHTML='<div class="paneempty">读取失败：'+e+'</div>'; return; }
  RULES_DATA=j.rules||[]; RULE_CATS=j.categories||[]; RULE_SCEN=j.scenario||''; RULE_CAP_OPTS=j.capital_scenarios||[]; RULE_HOR_OPTS=j.horizon_scenarios||[];
  const s=scenSet();
  const chip=(v,on,fn)=>`<button class="news-chip${on?' on':''}" onclick="${fn}">${v}</button>`;
  document.getElementById('scenCap').innerHTML=RULE_CAP_OPTS.map(v=>chip(v,s.has(v),`setScenCap('${v}')`)).join('');
  document.getElementById('scenHor').innerHTML=RULE_HOR_OPTS.map(v=>chip(v,s.has(v),`setScenHor('${v}')`)).join('');
  document.getElementById('rulesChips').innerHTML=['全部',...RULE_CATS].map(c=>{
    const v=(c==='全部'?'':c), on=v===RULE_FILTER;
    return `<button class="news-chip${on?' on':''}" onclick="setRulesFilter('${v}')">${c}</button>`;
  }).join('');
  document.getElementById('rf_cat').innerHTML=RULE_CATS.map(c=>`<option value="${c}">${c}</option>`).join('');
  const act=RULES_DATA.filter(r=>r.enabled&&ruleActive(r)).length;
  document.getElementById('rulesStat').textContent=`共 ${j.count} 条 · 当前场景[${RULE_SCEN}]下生效 ${act} 条会注入 AI · 蒸馏自 PA_Agent`;
  const cats=RULE_FILTER?[RULE_FILTER]:RULE_CATS;
  let h='';
  for(const cat of cats){
    const items=RULES_DATA.filter(r=>r.category===cat); if(!items.length) continue;
    h+=`<div class="rule-cat">${esc(cat)}（${items.length}）</div>`+items.map(r=>{
      const active=r.enabled&&ruleActive(r), dim=!active;
      return `<div class="rule-item ${dim?'dim':''}">
        <div class="rc"><div class="rt">${esc(r.title)}${r.scenarios?` <span class="rule-scen">${esc(r.scenarios)}</span>`:''}${r.source?` <span class="rule-src">${esc(r.source)}</span>`:''}</div>
          <div class="rd">${esc(r.content)}</div></div>
        <div class="racts">
          <button class="mini" onclick="toggleRule(${r.id},${r.enabled?0:1})">${r.enabled?'停用':'启用'}</button>
          <button class="mini" onclick="editRule(${r.id})">改</button>
          <button class="mini danger" onclick="delRule(${r.id})">删</button>
        </div></div>`;
    }).join('');
  }
  box.innerHTML = h || '<div class="paneempty">该分类暂无规则，点「＋新增规则」加一条。</div>';
}
async function setScenario(v){ await fetch('/api/rules/scenario',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scenario:v})}); loadRules(); }
function setScenCap(v){ const hor=[...scenSet()].filter(x=>RULE_HOR_OPTS.includes(x)); setScenario([v,...hor].join(',')); }
function setScenHor(v){ const cap=[...scenSet()].filter(x=>RULE_CAP_OPTS.includes(x)); setScenario([...cap,v].join(',')); }
function setRulesFilter(cat){ RULE_FILTER=cat; loadRules(); }
function toggleRuleForm(){
  const f=document.getElementById('ruleForm');
  if(f.style.display==='none'){ RULE_EDIT_ID=null; document.getElementById('rf_title').value=''; document.getElementById('rf_content').value=''; document.getElementById('rf_scen').value=''; f.style.display='block'; }
  else f.style.display='none';
}
function editRule(id){
  const r=RULES_DATA.find(x=>x.id===id); if(!r) return;
  RULE_EDIT_ID=id;
  document.getElementById('rf_cat').value=r.category;
  document.getElementById('rf_title').value=r.title;
  document.getElementById('rf_content').value=r.content;
  document.getElementById('rf_scen').value=r.scenarios||'';
  document.getElementById('ruleForm').style.display='block';
}
async function saveRule(){
  const category=document.getElementById('rf_cat').value;
  const title=document.getElementById('rf_title').value.trim();
  const content=document.getElementById('rf_content').value.trim();
  const scenarios=document.getElementById('rf_scen').value.trim();
  if(!title||!content){ alert('标题和内容必填'); return; }
  const url=RULE_EDIT_ID?('/api/rules/'+RULE_EDIT_ID):'/api/rules';
  const method=RULE_EDIT_ID?'PUT':'POST';
  let j; try{ j=await (await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify({category,title,content,scenarios})})).json(); }
  catch(e){ alert('保存失败：'+e); return; }
  if(j.ok!==false){ document.getElementById('ruleForm').style.display='none'; RULE_EDIT_ID=null; loadRules(); } else alert(j.msg||'保存失败');
}
async function toggleRule(id,en){ await fetch('/api/rules/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:en})}); loadRules(); }
async function delRule(id){ if(!confirm('删除这条规则？'))return; await fetch('/api/rules/'+id,{method:'DELETE'}); loadRules(); }

/* ── 模拟委托交易（多存档，按真实行情+A股规则撮合） ── */
let PAPER_ACCTS=[], PAPER_SEL=null;
function openPaper(){ loadPaperAccounts(); document.getElementById('paperModal').classList.add('open'); }
function closePaper(){ document.getElementById('paperModal').classList.remove('open'); }
async function loadPaperAccounts(){
  let j; try{ j=await (await fetch('/api/paper/accounts')).json(); }catch(e){ return; }
  PAPER_ACCTS=j.accounts||[];
  document.getElementById('paperStat').textContent=`${PAPER_ACCTS.length} 个存档 · 按真实行情+A股规则(整手/涨跌停/T+1/手续费)撮合 · 市场${j.market_open?'开市中':'已收市(下单会被拒)'}`;
  if(PAPER_SEL && !PAPER_ACCTS.find(a=>a.id===PAPER_SEL)) PAPER_SEL=null;
  if(!PAPER_SEL && PAPER_ACCTS.length) PAPER_SEL=PAPER_ACCTS[0].id;
  document.getElementById('paperAccts').innerHTML=PAPER_ACCTS.map(a=>`
    <div class="paper-acct ${a.id===PAPER_SEL?'on':''}" onclick="selectPaper(${a.id})">
      <div class="pn">${esc(a.name)}</div>
      <div class="pp">总 ${fmtInt(a.total)} · <span class="${clr(a.pnl)}">${sgn(a.pnl)}${fmtInt(a.pnl)}(${sgn(a.pnl_pct)}${a.pnl_pct}%)</span></div>
    </div>`).join('') || '<div class="paneempty small">还没有存档，下方新建一个。</div>';
  renderPaperDetail();
}
function selectPaper(id){ PAPER_SEL=id; loadPaperAccounts(); }
async function createPaperAccount(){
  const name=document.getElementById('pa_name').value.trim();
  const cap=+document.getElementById('pa_cap').value||100000;
  const j=await (await fetch('/api/paper/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,capital:cap})})).json();
  if(j.ok){ document.getElementById('pa_name').value=''; document.getElementById('pa_cap').value=''; PAPER_SEL=j.id; loadPaperAccounts(); } else alert(j.msg||'新建失败');
}
async function delPaperAccount(id){ if(!confirm('删除这个存档？持仓与订单一并清除。'))return; await fetch('/api/paper/accounts/'+id,{method:'DELETE'}); PAPER_SEL=null; loadPaperAccounts(); }
async function renderPaperDetail(){
  const box=document.getElementById('paperDetail');
  if(!PAPER_SEL){ box.innerHTML=''; return; }
  let j; try{ j=await (await fetch('/api/paper/account/'+PAPER_SEL)).json(); }catch(e){ box.innerHTML='<div class="paneempty">读取失败</div>'; return; }
  if(!j.ok){ box.innerHTML='<div class="paneempty">'+(j.msg||'')+'</div>'; return; }
  const a=j.account;
  let h=`<div class="paper-sum">
    <div>现金 <b>${fmtInt(a.cash)}</b></div><div>持仓市值 <b>${fmtInt(a.market_value)}</b></div>
    <div>总资产 <b>${fmtInt(a.total)}</b></div>
    <div>盈亏 <b class="${clr(a.pnl)}">${sgn(a.pnl)}${fmtInt(a.pnl)}(${sgn(a.pnl_pct)}${a.pnl_pct}%)</b></div>
    <div class="muted">本金 ${fmtInt(a.init_capital)}</div>
    <button class="mini danger" style="margin-left:auto" onclick="delPaperAccount(${a.id})">删存档</button></div>`;
  h+=`<div class="order-form">
    <input id="ord_code" placeholder="代码" maxlength="6" style="width:80px">
    <select id="ord_side"><option value="buy">买入</option><option value="sell">卖出</option></select>
    <select id="ord_otype"><option value="market">市价</option><option value="limit">限价</option></select>
    <input id="ord_price" placeholder="限价(市价留空)" style="width:110px">
    <input id="ord_shares" placeholder="股数(100整数倍)" style="width:130px">
    <button class="btn ai" onclick="placeOrder()">下单</button></div>`;
  const pos=a.positions||[];
  h+='<div class="subh">持仓</div>';
  h+= pos.length ? `<table class="paper-tbl"><thead><tr><th>股票</th><th>股数</th><th>可卖</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th><th></th></tr></thead><tbody>`+pos.map(p=>`
    <tr><td>${esc(p.name)}<span style="color:var(--muted)"> ${p.code}</span></td>
      <td>${p.shares}</td><td>${p.sellable}</td><td>${fmt(p.avg_cost)}</td>
      <td class="${clr(p.chg_pct)}">${fmt(p.price)}</td><td>${fmtInt(p.value)}</td>
      <td class="${clr(p.pnl)}">${sgn(p.pnl)}${fmtInt(p.pnl)} <span class="small">${sgn(p.pnl_pct)}${p.pnl_pct}%</span></td>
      <td><button class="mini" onclick="quickSell('${p.code}',${p.sellable})">卖</button></td></tr>`).join('')+'</tbody></table>'
    : '<div class="paneempty small">空仓。用上面的下单框买入。</div>';
  const ords=j.orders||[];
  if(ords.length){ h+='<div class="subh">最近委托</div>'+ords.slice(0,15).map(o=>`
    <div class="ord-log ${o.status==='rejected'?'rej':''}">${(o.ts||'').replace('T',' ').slice(5,16)} ${o.side==='buy'?'买':'卖'} ${esc(o.name)} ${o.shares}股 @${fmt(o.price)} · ${o.status==='filled'?'成交(费'+fmt(o.fee)+')':'✗ '+esc(o.note||'拒单')}</div>`).join(''); }
  box.innerHTML=h;
}
function quickSell(code,sellable){ document.getElementById('ord_code').value=code; document.getElementById('ord_side').value='sell'; document.getElementById('ord_shares').value=sellable||''; }
async function placeOrder(){
  if(!PAPER_SEL) return;
  const code=document.getElementById('ord_code').value.trim();
  const side=document.getElementById('ord_side').value, otype=document.getElementById('ord_otype').value;
  const price=+document.getElementById('ord_price').value||0, shares=+document.getElementById('ord_shares').value||0;
  if(!/^\d{6}$/.test(code)){ alert('请输入 6 位代码'); return; }
  if(shares<=0){ alert('请输入股数'); return; }
  const j=await (await fetch('/api/paper/order/'+PAPER_SEL,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,side,otype,price,shares})})).json();
  if(j.ok){ document.getElementById('ord_shares').value=''; loadPaperAccounts(); }
  else alert('未成交：'+(j.msg||''));
}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDrawer();closeGloss();closeRec();closeNews();closeNotes();closeRules();closePaper();}});
initTooltips();
loadConfig();
load();
loadPortfolio();
loadMarket();   // 顶部大盘研判条
toggleAuto();   // 默认开启自动刷新（30s）
