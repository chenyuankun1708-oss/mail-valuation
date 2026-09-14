(function(){
'use strict';
var assets=window.__FOF_ASSETS__||{};
var loading=false;
function loadScript(src){return new Promise(function(resolve,reject){var s=document.createElement('script');s.src=src;s.onload=resolve;s.onerror=function(){reject(new Error('交互代码加载失败'))};document.body.appendChild(s)})}
function loadApplication(){
 if(loading)return;loading=true;
 var home=document.getElementById('bootstrapHome');if(home)home.remove();
 document.body.classList.remove('bootstrap-ready');
 fetch('/api/page-data',{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(function(data){window.__FOF_DATA__=data;return loadScript(assets.core)}).then(function(){return loadScript(assets.dashboard)}).catch(function(error){loading=false;document.body.classList.add('bootstrap-ready');showError(error.message)})
}
function go(hash){location.hash=hash;if(hash!=='home')loadApplication()}
function showError(message){var host=document.getElementById('bootstrapHome')||document.body;host.innerHTML='<div class="boot-error">看板数据加载失败，请刷新重试（'+String(message).replace(/[<>]/g,'')+'）</div>'}
function renderHome(payload){
 document.body.classList.add('bootstrap-ready');
 var groups=[
  {name:'计算收益',note:'查看总览与各层收益',links:[['总览','overview'],['FOF层','returns'],['底层','bottom-returns'],['策略层','strategy']]},
  {name:'数据管理',note:'维护标签、台账与估值表',links:[['标签','labels'],['台账','data/ledger'],['估值表','overview/report']]},
  {name:'其他系统',note:'进入风控与研究工具',links:[['风控系统','risk'],['知识库','knowledge'],['策略实验室','lab']]}
 ];
 var host=document.createElement('main');host.id='bootstrapHome';host.className='boot-home';
 host.innerHTML='<button class="boot-brand" type="button">投资资产组合</button><section class="boot-welcome"><span class="boot-kicker">FOF MANAGEMENT SYSTEM</span><h1>欢迎来到FOF管理系统，想查阅什么功能？</h1><p>数据截止 '+(payload.data.data_cutoff||payload.data.default_end||'—')+' · '+payload.data.products.length+' 只顶层产品</p><div class="boot-groups">'+groups.map(function(group,index){return '<article class="boot-group"><button class="boot-category" type="button" aria-expanded="false" data-index="'+index+'"><b>'+group.name+'</b><span>'+group.note+'</span></button><div class="boot-links">'+group.links.map(function(link){return '<button type="button" data-hash="'+link[1]+'">'+link[0]+'<i>→</i></button>'}).join('')+'</div></article>'}).join('')+'</div></section>';
 document.body.appendChild(host);
 host.querySelector('.boot-brand').onclick=function(){go('home')};
 host.querySelectorAll('.boot-category').forEach(function(button){button.onclick=function(){var open=button.getAttribute('aria-expanded')==='true';host.querySelectorAll('.boot-category').forEach(function(item){item.setAttribute('aria-expanded','false');item.closest('.boot-group').classList.remove('open')});if(!open){button.setAttribute('aria-expanded','true');button.closest('.boot-group').classList.add('open')}}});
 host.querySelectorAll('[data-hash]').forEach(function(button){button.onclick=function(){go(button.dataset.hash)}});
}
function start(){
 var hash=(location.hash||'').replace(/^#/,'');
 if(!hash){history.replaceState(null,'','#home');hash='home'}
 if(hash!=='home'){loadApplication();return}
 fetch('/api/v2/bootstrap',{credentials:'same-origin'}).then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json()}).then(renderHome).catch(function(error){loadApplication()})
}
addEventListener('hashchange',function(){var hash=(location.hash||'').replace(/^#/,'');if(hash&&hash!=='home')loadApplication()});
start();
})();
