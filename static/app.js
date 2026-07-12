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
let DATA=[], sortKey='net20', sortDir=-1, autoTimer=null, LLM=false, MODEL='';

const clr=v=> v>0?'up':v<0?'down':'flat';
const sgn=v=> v>0?'+':'';
const fmt=(v,d=2)=> v==null||v===''?'—':Number(v).toFixed(d);
const fmtInt=v=> v==null?'—':Math.round(v).toLocaleString();

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
function toggleAuto(){
  const b=document.getElementById('autobtn');
  if(autoTimer){clearInterval(autoTimer);autoTimer=null;b.textContent='自动刷新 关';b.classList.remove('on');}
  else{autoTimer=setInterval(()=>{load();loadPortfolio();},60000);b.textContent='自动刷新 开·60s';b.classList.add('on');}
}

/* ── 深挖抽屉 ── */
function tab(p){
  document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.dataset.p===p));
  document.querySelectorAll('.pane').forEach(el=>el.classList.remove('on'));
  document.getElementById('pane_'+p).classList.add('on');
}
function closeDrawer(){document.getElementById('drawer').classList.remove('open');document.getElementById('scrim').classList.remove('open');}
function loading(id){document.getElementById(id).innerHTML='<div class="paneempty"><span class="spin"></span> 拉取中…</div>';}
async function openDetail(code){
  document.getElementById('drawer').classList.add('open');
  document.getElementById('scrim').classList.add('open');
  tab('ov');
  document.getElementById('d_name').textContent='加载中…';
  document.getElementById('d_code').textContent=code;
  document.getElementById('d_price').textContent='—'; document.getElementById('d_chg').textContent='';
  ['ov','rp','lhb','lk','ff'].forEach(p=>loading('pane_'+p));
  let j;
  try{ j=await (await fetch('/api/detail/'+code)).json(); }
  catch(e){ document.getElementById('pane_ov').innerHTML='<div class="paneempty">加载失败：'+e+'</div>'; return; }
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
  ov+=`<div class="subh">近30日走势</div>${bigSpark(m.series)}`;
  if(LLM) ov+=`<button class="btn ai" style="margin-top:14px" onclick="askDetailAdvice('${j.code}')">🤖 让 ${MODEL} 分析这只该买还是该卖</button><div id="detailAdvice"></div>`;
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

/* ── 配置 / AI 可用性 ── */
async function loadConfig(){
  try{
    const j=await (await fetch('/api/config')).json();
    LLM=j.llm_enabled; MODEL=j.model||'';
    document.querySelectorAll('.ai-only').forEach(el=>el.style.display=LLM?'':'none');
    const chip=document.getElementById('aichip');
    chip.textContent=LLM?`🤖 ${MODEL}`:'🤖 未配置'; chip.className='chip'+(LLM?' ok':'');
  }catch(e){}
}

/* ── 每日 AI 推荐 ── */
const ACT={buy:['买入','a-buy'],add:['加仓','a-buy'],hold:['持有','a-hold'],reduce:['减仓','a-sell'],sell:['卖出','a-sell'],watch:['观望','a-watch']};
async function runDaily(){
  const box=document.getElementById('recBody'); const panel=document.getElementById('recPanel');
  panel.classList.add('open');
  box.innerHTML='<div class="paneempty"><span class="spin"></span> '+MODEL+' 正在分析自选股与持仓…（推理模型约需 15~40 秒）</div>';
  let j;
  try{ j=await (await fetch('/api/recommend/daily',{method:'POST'})).json(); }
  catch(e){ box.innerHTML='<div class="paneempty">请求失败：'+e+'</div>'; return; }
  if(!j.ok){ box.innerHTML='<div class="paneempty">生成失败：'+(j.msg||'')+'</div>'; return; }
  const r=j.result||{};
  let h=`<div class="mview">📊 ${r.market_view||''}</div>`;
  h+='<div class="reccards">'+(r.picks||[]).map(p=>{
    const a=ACT[p.action]||['?','a-hold'];
    return `<div class="reccard ${a[1]}"><div class="rc-top"><span class="badge ${a[1]}">${a[0]}</span>
      <span class="rc-name">${p.name||''} <em>${p.code||''}</em></span>
      <span class="rc-conf">${({high:'高',mid:'中',low:'低'})[p.confidence]||''}信心</span></div>
      <div class="rc-reason">${p.reason||''}</div>${p.risk?`<div class="rc-risk">⚠ ${p.risk}</div>`:''}</div>`;
  }).join('')+'</div>';
  if(r.holdings_note&&r.holdings_note!=='无') h+=`<div class="hnote">💼 持仓提醒：${r.holdings_note}</div>`;
  h+=`<div class="disc">以上为 ${j.model} 基于当前客观数据生成的参考信号，${j.updated} · 不构成投资建议，据此操作风险自负。</div>`;
  box.innerHTML=h;
}
function closeRec(){document.getElementById('recPanel').classList.remove('open');}

/* 抽屉里让 AI 分析单只（不必持仓） */
async function askDetailAdvice(code){
  const box=document.getElementById('detailAdvice');
  box.innerHTML='<div class="paneempty small"><span class="spin"></span> '+MODEL+' 分析中…</div>';
  const inPortfolio=await isHeld(code);
  if(!inPortfolio){
    box.innerHTML='<div class="advice"><div class="note">提示：先在下方“持仓”里记录这只（含成本价），AI 才能给出结合你成本的卖出/止损建议。当前给通用参考。</div></div>';
  }
  try{
    const j=await (await fetch('/api/recommend/position/'+code,{method:'POST'})).json();
    if(j.ok){ box.innerHTML=adviceHTML(j.advice, j.model); }
    else if(j.msg && j.msg.includes('不在持仓')){
      box.innerHTML='<div class="advice"><div class="note">这只不在持仓中，无法给结合成本的建议。可在下方“持仓”记录后再试；或直接参考上方“每日推荐”。</div></div>';
    } else box.innerHTML='<div class="paneempty small">失败：'+(j.msg||'')+'</div>';
  }catch(e){ box.innerHTML='<div class="paneempty small">失败：'+e+'</div>'; }
}
async function isHeld(code){ const j=await (await fetch('/api/portfolio')).json(); return (j.holdings||[]).some(h=>h.code===code); }

function adviceHTML(a,model){
  const map={hold:['持有','a-hold'],add:['加仓','a-buy'],reduce:['减仓','a-sell'],sell:['卖出','a-sell']};
  const m=map[a.action]||['参考','a-hold'];
  return `<div class="advice"><div class="rc-top"><span class="badge ${m[1]}">${m[0]}</span><span class="rc-name">${model} 建议</span></div>
    <div class="akv"><div><span>卖出条件</span>${a.sell_trigger||'—'}</div>
    <div><span>加仓条件</span>${a.add_trigger||'—'}</div>
    <div><span>止损参考</span>${a.stop_loss||'—'}</div>
    <div><span>止盈参考</span>${a.take_profit||'—'}</div></div>
    <div class="rc-reason">${a.reason||''}</div>
    <div class="disc">AI 参考信号，不构成投资建议。</div></div>`;
}

/* ── 持仓 ── */
async function loadPortfolio(){
  let j; try{ j=await (await fetch('/api/portfolio')).json(); }catch(e){return;}
  const s=j.summary||{}, hs=j.holdings||[];
  const sb=document.getElementById('folioSum');
  if(s.count){
    sb.innerHTML=`<span>持仓 <b>${s.count}</b> 只</span><span>市值 <b>${fmtInt(s.market_value)}</b> 元</span>
      <span>成本 ${fmtInt(s.cost_value)} 元</span>
      <span class="${clr(s.pnl_amount)}">盈亏 <b>${sgn(s.pnl_amount)}${fmtInt(s.pnl_amount)}</b> 元（${s.pnl_pct!=null?sgn(s.pnl_pct)+s.pnl_pct+'%':'—'}）</span>`;
  }else sb.innerHTML='<span class="muted">还没有持仓记录。填下面的表单：代码 + 股数 + 你的买入成本价。</span>';
  const tb=document.getElementById('folioRows');
  tb.innerHTML = hs.length ? hs.map(h=>`
    <tr>
      <td class="nm"><div class="n">${h.name||h.code}</div><div class="c">${h.code}</div></td>
      <td>${fmtInt(h.shares)}</td>
      <td>${fmt(h.cost_price)}</td>
      <td class="${clr(h.chg_pct)}">${fmt(h.price)}</td>
      <td>${fmtInt(h.market_value)}</td>
      <td class="${clr(h.pnl_pct)}"><b>${h.pnl_pct!=null?sgn(h.pnl_pct)+h.pnl_pct+'%':'—'}</b><div class="small">${sgn(h.pnl_amount)}${fmtInt(h.pnl_amount)}元</div></td>
      <td>${h.buy_date||'—'}</td>
      <td class="acts">
        ${LLM?`<button class="mini ai" onclick="folioAdvice('${h.code}')">🤖 何时卖</button>`:''}
        <button class="mini" onclick="openDetail('${h.code}')">深挖</button>
        <button class="mini danger" onclick="delHolding('${h.code}','${(h.name||h.code)}')">清仓</button>
      </td>
    </tr><tr class="advrow" id="adv_${h.code}"><td colspan="8"></td></tr>`).join('') : '';
  if(!hs.length) tb.innerHTML='';
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
async function folioAdvice(code){
  const row=document.getElementById('adv_'+code); const cell=row.firstElementChild;
  if(row.classList.contains('open')){ row.classList.remove('open'); cell.innerHTML=''; return; }
  row.classList.add('open');
  cell.innerHTML='<div class="paneempty small"><span class="spin"></span> '+MODEL+' 分析何时卖/加/止损…';
  try{
    const j=await (await fetch('/api/recommend/position/'+code,{method:'POST'})).json();
    cell.innerHTML = j.ok ? adviceHTML(j.advice,j.model) : '<div class="paneempty small">失败：'+(j.msg||'')+'</div>';
  }catch(e){ cell.innerHTML='<div class="paneempty small">失败：'+e+'</div>'; }
}

/* ── 名词解释总表 ── */
function openGloss(){
  const body=document.getElementById('glossBody');
  body.innerHTML=Object.entries(GLOSSARY).map(([k,v])=>`<div class="gitem"><div class="gk">${k}</div><div class="gv">${v}</div></div>`).join('');
  document.getElementById('glossModal').classList.add('open');
}
function closeGloss(){document.getElementById('glossModal').classList.remove('open');}

document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDrawer();closeGloss();closeRec();}});
initTooltips();
loadConfig();
load();
loadPortfolio();
