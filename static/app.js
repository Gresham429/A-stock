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
let recSeq=0, detailSeq=0;

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
  const gen=++detailSeq;   // 快速切换股票时，作废上一只的在飞请求
  document.getElementById('drawer').classList.add('open');
  document.getElementById('scrim').classList.add('open');
  tab('ov');
  document.getElementById('d_name').textContent='加载中…';
  document.getElementById('d_code').textContent=code;
  document.getElementById('d_price').textContent='—'; document.getElementById('d_chg').textContent='';
  ['ov','kl','rp','lhb','lk','ff'].forEach(p=>loading('pane_'+p));
  // K线独立并行加载(快)，先渲染
  fetch('/api/kline/'+code).then(r=>r.json()).then(k=>{ if(gen!==detailSeq)return; renderKline(k.kline||[]); })
    .catch(()=>{ if(gen!==detailSeq)return; document.getElementById('pane_kl').innerHTML='<div class="paneempty">K线加载失败</div>'; });
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

/* ── K线蜡烛图 + 箱形图 ── */
function renderKline(kl){
  const pane=document.getElementById('pane_kl');
  if(!kl||!kl.length){ pane.innerHTML='<div class="paneempty">暂无K线数据</div>'; return; }
  const win=kl.slice(-60);
  pane.innerHTML=`<div class="subh">日K线 · 近${win.length}日（红涨绿跌 · MA5/MA20 · 成交量）</div>`
    +'<div class="chartwrap">'+candlestick(win)+'</div>'
    +'<div class="chartcap">蜡烛体=当日开盘↔收盘，上下影线=最高/最低价；<span class="up">红实体=收阳(涨)</span>、<span class="down">绿实体=收阴(跌)</span>。橙线 MA5、蓝线 MA20，下方为成交量。</div>'
    +'<div class="subh">箱形图 · 近'+win.length+'日收盘价分布</div>'
    +'<div class="chartwrap">'+boxplot(win.map(k=>k.close))+'</div>'
    +'<div class="chartcap">箱体=价格中间50%区间（下沿 Q1 / 中线=中位数 / 上沿 Q3），须线到最高/最低；<b>★=当前价</b>。价格长期停在箱体内、一旦突破箱体常有方向性行情。</div>';
}
function candlestick(kl){
  const w=640,h=260,padL=46,padR=8,padT=10,volH=50,cH=h-volH-padT-18;
  const hi=Math.max(...kl.map(k=>k.high)),lo=Math.min(...kl.map(k=>k.low)),rng=hi-lo||1;
  const n=kl.length,cw=(w-padL-padR)/n,bw=Math.max(1.5,cw*0.62);
  const yP=p=>padT+(hi-p)/rng*cH;
  const ma=(i,m)=>{ if(i<m-1)return null; let s=0; for(let k=i-m+1;k<=i;k++)s+=kl[k].close; return s/m; };
  const maLine=(m,col)=>{let pts=[];for(let i=0;i<n;i++){const v=ma(i,m);if(v!=null)pts.push(`${(padL+i*cw+cw/2).toFixed(1)},${yP(v).toFixed(1)}`);}return pts.length>1?`<polyline points="${pts.join(' ')}" fill="none" stroke="${col}" stroke-width="1.1" opacity=".9"/>`:'';};
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
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;min-width:520px;height:${h}px">
    ${grid}${maLine(5,'var(--gold)')}${maLine(20,'#4aa3ff')}${candles}${vols}
    <text x="${padL}" y="${h-4}" fill="var(--muted)" font-size="9" font-family="monospace">${kl[0].date.slice(5)}</text>
    <text x="${w-padR-28}" y="${h-4}" fill="var(--muted)" font-size="9" font-family="monospace">${kl[n-1].date.slice(5)}</text></svg>`;
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

/* ── 配置 / AI 可用性 ── */
async function loadConfig(){
  try{
    const j=await (await fetch('/api/config')).json();
    LLM=j.llm_enabled; MODEL=j.model||''; WEB=j.web_search;
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
async function runDaily(){
  const gen=++recSeq;
  const box=document.getElementById('recBody'); const panel=document.getElementById('recPanel');
  document.getElementById('recTitle').textContent='🤖 自选股推荐（含持仓）';
  document.getElementById('recControls').style.display='none';
  panel.classList.add('open');
  box.innerHTML='<div class="paneempty"><span class="spin"></span> '+MODEL+' 正在分析自选股与持仓…（推理模型约需 15~40 秒）</div>';
  let j;
  try{ j=await (await fetch('/api/recommend/daily',{method:'POST'})).json(); }
  catch(e){ if(gen!==recSeq)return; box.innerHTML='<div class="paneempty">请求失败：'+e+'</div>'; return; }
  if(gen!==recSeq) return;   // 已切到别的请求，丢弃这次过期响应
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
function closeRec(){ ++recSeq; document.getElementById('recPanel').classList.remove('open'); }

/* ── 全市场筛选 ── */
function openScreen(){
  ++recSeq;   // 作废在飞的旧请求，避免其晚返回覆盖本面板
  document.getElementById('recTitle').textContent='🔍 全市场选股（跨板块 · 侧重科技）';
  document.getElementById('recControls').style.display='flex';
  document.getElementById('recPanel').classList.add('open');
  document.getElementById('recBody').innerHTML='<div class="paneempty">选资金规模与侧重板块 → 点「开始筛选」。DeepSeek 会从 44 只科技股里按你的资金和板块跨板块选股。</div>';
}
async function runScreen(){
  const gen=++recSeq;
  const cap=+document.getElementById('scr_capital').value;
  const focus=document.getElementById('scr_focus').value;
  const box=document.getElementById('recBody');
  box.innerHTML='<div class="paneempty"><span class="spin"></span> '+MODEL+' 正在拉取候选池行情并跨板块筛选…（约 40~90 秒）</div>';
  let j;
  try{ j=await (await fetch('/api/recommend/screen',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({capital:cap,focus_sector:focus})})).json(); }
  catch(e){ if(gen!==recSeq)return; box.innerHTML='<div class="paneempty">请求失败：'+e+'</div>'; return; }
  if(gen!==recSeq) return;   // 已切到别的请求，丢弃这次过期响应
  if(!j.ok){ box.innerHTML='<div class="paneempty">筛选失败：'+(j.msg||'')+'</div>'; return; }
  const r=j.result||{};
  let h=`<div class="mview">🔍 候选 ${j.candidates} 只 · 资金 ${(+j.capital).toLocaleString()} 元${j.focus?' · 侧重 '+j.focus:''}<br>📊 ${r.overall||''}</div>`;
  h+='<div class="reccards">'+(r.picks||[]).map(p=>{
    const a=ACT[p.action]||['关注','a-watch'];
    return `<div class="reccard ${a[1]}"><div class="rc-top"><span class="badge ${a[1]}">${a[0]}</span>
      <span class="rc-name">${p.name||''} <em>${p.code||''}</em></span>
      <span class="rc-sector">${p.sector||''}</span></div>
      <div class="rc-reason">${p.reason||''}</div>
      <div class="rc-top" style="margin-top:8px">
        <span class="rc-lot">1手 ${p.lot_cost?(+p.lot_cost).toLocaleString():'—'}元</span>
        ${p.risk?`<span class="rc-risk" style="margin:0">⚠ ${p.risk}</span>`:''}
        <button class="pick-add" onclick="addPick('${p.code}')">＋自选</button>
      </div></div>`;
  }).join('')+'</div>';
  if(r.budget_plan) h+=`<div class="planbox"><b>💰 ${(+j.capital).toLocaleString()}元 配置建议：</b>${r.budget_plan}</div>`;
  if(r.sector_view) h+=`<div class="planbox"><b>🧭 板块简评：</b>${r.sector_view}</div>`;
  h+=`<div class="disc">${j.model} 基于候选池客观指标的筛选参考，${j.updated} · 不构成投资建议。</div>`;
  box.innerHTML=h;
}
async function addPick(code){
  const j=await (await fetch('/api/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code})})).json();
  if(j.ok){ load(); alert(`已加入自选：${j.name||code}`); } else alert(j.msg||'加入失败');
}

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
    ${a.fundamental?`<div class="rc-reason"><b>📊 基本面：</b>${a.fundamental}</div>`:''}
    ${a.policy_news?`<div class="rc-reason"><b>📰 政策/新闻：</b>${a.policy_news}</div>`:''}
    <div class="rc-reason"><b>结论：</b>${a.reason||''}</div>
    <div class="disc">AI 参考信号，结合了波动史/财报/新闻，但不构成投资建议。</div></div>`;
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
