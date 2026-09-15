const otcState={module:null,activeRun:null,pollTimer:null,samplePage:1};

function installOtcNavigation(){
 const risk=document.querySelector('.dash-nav[data-page="risk"]');
 if(risk&&!document.querySelector('.dash-nav[data-page="otc-derivatives"]'))risk.insertAdjacentHTML('beforebegin','<button class="dash-nav" data-page="otc-derivatives" onclick="dashGo(\'otc-derivatives\',false,\'backtest\')"><i>⌁</i><span>场外衍生品</span></button>');
 const content=document.querySelector('.dash-content');
 if(content&&!document.getElementById('dashViewotc-derivatives'))content.insertAdjacentHTML('beforeend','<section class="dash-view" data-page="otc-derivatives" id="dashViewotc-derivatives"><div id="dashOtcDerivatives"></div></section>');
}

function otcNumber(value,digits){return value==null?'—':Number(value).toLocaleString('zh-CN',{maximumFractionDigits:digits==null?2:digits})}
function otcPercent(value){return value==null?'—':(Number(value)*100).toFixed(2)+'%'}
function otcStatus(status){return({queued:'排队中',running:'运行中',completed:'已完成',failed:'失败',interrupted:'已中断'})[status]||status||'—'}

function renderOtcDerivatives(tab){
 const host=document.getElementById('dashOtcDerivatives');if(!host)return;
 otcState.module=RAW.otc_derivatives||otcState.module||{};
 const market=otcState.module.market||{},indices=market.indices||[],structures=otcState.module.structures||[];
 host.innerHTML=`<div class="otc-head"><div><span class="eyebrow">OTC DERIVATIVES</span><h2>场外衍生品</h2><p>固定指数白名单上的历史条款回测；行情来自本地Wind缓存，不使用模拟数据。</p></div><span class="dash-pill ${market.status==='current'?'healthy':'attention'}">行情 ${market.status==='current'?'可用':'缺失'} · ${escapeHtml((market.updated_at||'—').replace('T',' '))}</span></div><div class="otc-tabs" role="tablist"><button class="active" role="tab" aria-selected="true">期权回测</button></div><div class="otc-layout"><section class="dash-card otc-form-card"><h3>回测条款</h3><p class="dash-note">自然月观察、非交易日顺延、ACT/365。每个交易日作为一笔建仓样本。</p><form id="otcBacktestForm" onsubmit="submitOtcBacktest(event)"><label>结构<select name="structure" onchange="otcToggleFields(this.value)">${structures.map(x=>`<option value="${x.code}">${escapeHtml(x.name)}</option>`).join('')}</select></label><label>标的指数<select name="index_code">${indices.map(x=>`<option value="${x.code}" ${x.available?'':'disabled'}>${escapeHtml(x.name)}${x.available?'':'（无缓存）'}</option>`).join('')}</select></label><div class="otc-form-grid"><label>历史起始日<input name="start_date" type="date" value="2012-01-01" required></label><label>评价截止日<input name="end_date" type="date" value="${escapeHtml((indices[0]||{}).last_date||'')}" required></label><label>期限（月）<input name="term_months" type="number" min="1" max="60" value="24" required></label><label>敲出锁定期（月）<input name="lock_period_months" type="number" min="1" max="60" value="3" required></label><label>敲入比例<input name="knock_in_ratio" type="number" min="0.3" max="0.99" step="0.001" value="0.70" required></label><label>初始敲出比例<input name="knock_out_initial" type="number" min="0.8" max="1.2" step="0.001" value="1.00" required></label><label>每月降敲<input name="knock_out_decrease_monthly" type="number" min="0" max="0.05" step="0.0001" value="0.005" required></label><label class="otc-snow">前段年化票息<input name="first_coupon" type="number" min="0" max="0.5" step="0.001" value="0.12"></label><label class="otc-snow">后段年化票息<input name="second_coupon" type="number" min="0" max="0.5" step="0.001" value="0.12"></label><label class="otc-snow">票息切换月<input name="coupon_switch_months" type="number" min="1" max="60" value="12"></label><label>最大亏损（可空）<input name="max_loss" type="number" min="0.000001" max="1" step="0.01"></label><label class="otc-dcn">派息障碍<input name="dividend_barrier" type="number" min="0.5" max="0.99" step="0.001" value="0.80"></label><label class="otc-dcn">月派息率<input name="monthly_dividend" type="number" min="0" max="0.05" step="0.0001" value="0.0088"></label><label class="otc-combo">DCN收益组合系数<input name="dcn_weight" type="number" min="0.000001" max="10" step="0.01" value="1"></label><label class="otc-combo">雪球收益组合系数<input name="snowball_weight" type="number" min="0.000001" max="10" step="0.01" value="0.2"></label></div><button id="otcRunButton" class="otc-primary" type="submit" ${market.status==='current'?'':'disabled'}>开始回测</button><div id="otcTaskMessage" class="otc-message" aria-live="polite"></div></form></section><section class="dash-card otc-history"><div class="card-head"><div><h3>最近回测</h3><p>运行记录持久化保存</p></div><button class="link-btn" onclick="refreshOtcModule()">刷新</button></div><div id="otcHistory">${otcHistoryHtml(otcState.module.recent_runs||[])}</div></section></div><div id="otcResult"></div>`;
 otcToggleFields((document.querySelector('#otcBacktestForm [name=structure]')||{}).value);
 if(otcState.activeRun)loadOtcRun(otcState.activeRun);
}

function otcToggleFields(structure){
 document.querySelectorAll('.otc-snow').forEach(x=>x.hidden=structure==='dcn');
 document.querySelectorAll('.otc-dcn').forEach(x=>x.hidden=!['dcn','dcn_snowball_combo'].includes(structure));
 document.querySelectorAll('.otc-combo').forEach(x=>x.hidden=structure!=='dcn_snowball_combo');
}

function otcHistoryHtml(items){
 if(!items.length)return'<div class="empty-state">尚无回测记录</div>';
 return items.map(run=>`<button class="otc-run ${run.id===otcState.activeRun?'active':''}" onclick="loadOtcRun('${run.id}')"><span><b>${escapeHtml((((otcState.module||{}).structures||[]).find(x=>x.code===run.request.structure)||{}).name||run.request.structure)}</b><small>${escapeHtml(run.created_at||'')} · ${escapeHtml(run.request.index_code||'')}</small></span><em class="${run.status}">${otcStatus(run.status)}</em></button>`).join('');
}

async function refreshOtcModule(){
 const response=await fetch('/api/modules/otc-derivatives');if(!response.ok)throw new Error('HTTP '+response.status);
 const data=await response.json();RAW.otc_derivatives=data;otcState.module=data;renderOtcDerivatives('backtest');
}

async function submitOtcBacktest(event){
 event.preventDefault();const form=event.currentTarget,button=document.getElementById('otcRunButton'),message=document.getElementById('otcTaskMessage'),data=new FormData(form),payload={};
 for(const [key,value] of data.entries()){if(value==='')continue;payload[key]=['structure','index_code','start_date','end_date'].includes(key)?value:Number(value)}
 button.disabled=true;message.className='otc-message';message.textContent='正在提交受限回测任务…';
 try{const response=await fetch('/api/otc/backtests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),body=await response.json();if(!response.ok)throw new Error(body.error||'HTTP '+response.status);otcState.activeRun=body.run.id;message.textContent='任务已提交，正在计算…';pollOtcRun(body.run.id)}catch(error){message.className='otc-message error';message.textContent=error.message;button.disabled=false}
}

async function pollOtcRun(runId){
 clearTimeout(otcState.pollTimer);
 try{const state=await fetchOtcRun(runId);if(['queued','running'].includes(state.run.status)){otcState.pollTimer=setTimeout(()=>pollOtcRun(runId),1500);return}await refreshOtcModule();if(state.run.status==='completed')loadOtcRun(runId);else{const message=document.getElementById('otcTaskMessage');if(message){message.className='otc-message error';message.textContent=state.run.error||'回测失败'}}}catch(error){const message=document.getElementById('otcTaskMessage');if(message){message.className='otc-message error';message.textContent=error.message}}
}

async function fetchOtcRun(runId){const response=await fetch('/api/otc/backtests/'+runId);const data=await response.json();if(!response.ok)throw new Error(data.error||'HTTP '+response.status);return data}

async function loadOtcRun(runId){
 otcState.activeRun=runId;const host=document.getElementById('otcResult');if(!host)return;host.innerHTML='<div class="dash-card">正在加载回测结果…</div>';
 try{const data=await fetchOtcRun(runId),run=data.run;if(run.status!=='completed'){host.innerHTML=`<div class="dash-card"><b>${otcStatus(run.status)}</b><p>${escapeHtml(run.error||'任务尚未完成')}</p></div>`;if(['queued','running'].includes(run.status))pollOtcRun(runId);return}const r=data.result,s=r.summary,source=r.source;host.innerHTML=`<section class="dash-card otc-result-head"><div><span class="eyebrow">BACKTEST RESULT</span><h3>${escapeHtml(r.structure_name)} · ${escapeHtml(r.index_name)}</h3><p>实际行情 ${source.actual_start_date} → ${source.actual_end_date} · ${source.price_rows}个交易日 · ${escapeHtml(r.time_convention)} / ACT365</p></div><div class="otc-downloads"><a href="/api/otc/backtests/${runId}/download.xlsx">下载Excel</a><a href="/api/otc/backtests/${runId}/download.json">下载JSON</a></div></section><div class="otc-kpis">${[['全部样本',s.total_samples],['已完成',s.completed_samples],['存续中',s.still_running_samples],['正收益概率',otcPercent(s.positive_return_probability)],['平均绝对收益',otcPercent(s.average_absolute_return)],['平均年化收益',otcPercent(s.average_annualized_return)],['平均持有天数',otcNumber(s.average_holding_calendar_days,1)],['首年敲出概率',otcPercent(s.first_year_knock_out_probability)]].map(x=>`<div><span>${x[0]}</span><strong>${x[1]==null?'—':x[1]}</strong></div>`).join('')}</div><div class="otc-chart-grid"><section class="dash-card"><h3>建仓日样本收益</h3><canvas id="otcReturnChart" width="900" height="280"></canvas></section><section class="dash-card"><h3>合同退出观察月分布</h3><canvas id="otcHoldingChart" width="560" height="280"></canvas></section></div><section class="dash-card"><div class="card-head"><div><h3>逐样本明细</h3><p>已完成和存续样本均保留</p></div><div id="otcSamplePager"></div></div><div class="otc-table-wrap"><table><thead><tr><th>建仓日</th><th>建仓点位</th><th>退出日</th><th>持有自然日</th><th>敲入</th><th>敲出</th><th>状态</th><th>绝对收益</th><th>年化收益</th></tr></thead><tbody id="otcSamples"></tbody></table></div></section><p class="dash-note">该结果是固定历史路径上的条款回测，不是期权定价、投资建议或未来收益预测。敲出线相等时沿用源引擎的严格高于规则；组合系数不归一化。</p>`;drawOtcLine(r.charts.sample_returns||[]);drawOtcBars(r.charts.holding_months||[]);loadOtcSamples(runId,1)}catch(error){host.innerHTML=`<div class="dash-card"><span class="negative">${escapeHtml(error.message)}</span></div>`}
}

async function loadOtcSamples(runId,page){
 const response=await fetch(`/api/otc/backtests/${runId}/samples?page=${page}&page_size=50`),data=await response.json();if(!response.ok)return;
 const body=document.getElementById('otcSamples'),pager=document.getElementById('otcSamplePager');if(!body||!pager)return;
 body.innerHTML=data.items.map(x=>`<tr><td>${x.entry_date}</td><td>${otcNumber(x.entry_price,2)}</td><td>${x.exit_date||'—'}</td><td>${x.holding_calendar_days==null?'—':x.holding_calendar_days}</td><td>${x.knocked_in?'是':'否'}</td><td>${x.knocked_out?'是':'否'}</td><td>${x.still_running?'存续中':'已完成'}</td><td>${otcPercent(x.absolute_return)}</td><td>${otcPercent(x.annualized_return)}</td></tr>`).join('');
 const pages=Math.max(1,Math.ceil(data.total/data.page_size));pager.innerHTML=`<button ${page<=1?'disabled':''} onclick="loadOtcSamples('${runId}',${page-1})">上一页</button><span>${page} / ${pages}</span><button ${page>=pages?'disabled':''} onclick="loadOtcSamples('${runId}',${page+1})">下一页</button>`;
}

function drawOtcLine(points){const c=document.getElementById('otcReturnChart');if(!c||!points.length)return;const ctx=c.getContext('2d'),L=48,R=16,T=20,B=34,w=c.width-L-R,h=c.height-T-B,min=Math.min(0,...points.map(x=>x.value)),max=Math.max(0,...points.map(x=>x.value)),span=max-min||1,x=i=>L+i/Math.max(points.length-1,1)*w,y=v=>T+(max-v)/span*h;ctx.clearRect(0,0,c.width,c.height);ctx.strokeStyle='#cbd5e1';ctx.beginPath();ctx.moveTo(L,y(0));ctx.lineTo(L+w,y(0));ctx.stroke();ctx.strokeStyle='#167c69';ctx.lineWidth=2;ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(x(i),y(p.value)):ctx.moveTo(x(i),y(p.value)));ctx.stroke();ctx.fillStyle='#64748b';ctx.font='11px Microsoft YaHei';ctx.fillText(points[0].date,L,c.height-10);ctx.textAlign='right';ctx.fillText(points[points.length-1].date,L+w,c.height-10)}
function drawOtcBars(points){const c=document.getElementById('otcHoldingChart');if(!c||!points.length)return;const ctx=c.getContext('2d'),L=42,R=14,T=20,B=34,w=c.width-L-R,h=c.height-T-B,max=Math.max(...points.map(x=>x.count),1),slot=w/points.length;ctx.clearRect(0,0,c.width,c.height);points.forEach((p,i)=>{const bh=p.count/max*h,bw=Math.max(2,slot*.65),x=L+i*slot+(slot-bw)/2;ctx.fillStyle='#24578d';ctx.fillRect(x,T+h-bh,bw,bh);if(points.length<=24){ctx.fillStyle='#64748b';ctx.font='10px Microsoft YaHei';ctx.textAlign='center';ctx.fillText(p.month,L+i*slot+slot/2,c.height-12)}})}
