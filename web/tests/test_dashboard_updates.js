/** Regression checks for an empty first load followed by a real publication. */
'use strict';
const vm = require('vm');
const { evaluate, assert } = require('./test_render_all_robustness');

const kickoff = new Date(Date.now()+3600000).toISOString();
const pick = {model_forecast:true,source:'league-dixon-coles-v2',basis:'model_only',
  match_id:'artificial-fixture',dedupe_key:'artificial-pick',sport_key:'soccer_epl',
  home_team:'Test Home',away_team:'Test Away',market:'btts',outcome_name:'Yes',
  commence_time:kickoff,p_true:.6,fair_odds:1/.6,best_odds:null,best_ev:null,
  is_recommendation:false};
const forecast = {count:1,matches:[{match_id:pick.match_id,home:pick.home_team,
  away:pick.away_team,league:pick.sport_key,commence_at:kickoff,
  model:{p_home:.5,p_draw:.3,p_away:.2},micro:{p_btts:.6,p_over_2_5:.5,
    most_likely_scores:[{score:'1-1',p:.12}]},
  uncertainty:{level:'high',reasons:['Short training history']}}]};
function response(data, status=200) {
  return {ok:status===200,status,json:async()=>data};
}
function state(sandbox, value) {
  sandbox.testData = value;
  vm.runInContext('Object.assign(state, testData)',sandbox);
}
const checks = [];
function check(name, fn) { checks.push([name,fn]); }

check('a publication refreshes forecasts after an empty first load', async () => {
  const {sandbox,getEl} = evaluate();
  let published = false;
  sandbox.fetch = async path => response(path==='/api/dashboard'
    ? {summary:{},active_picks:published?[pick]:[],pipeline:{state:published?'ok':'running'}}
    : path==='/api/forecast' ? published?forecast:{count:0,matches:[]} : {});
  await sandbox.loadData();
  assert(getEl('forecast-board').innerHTML.includes('No upcoming forecasts'), 'initial state is missing');
  published = true;
  await sandbox.pollDashboard();
  assert(getEl('forecast-board').innerHTML.includes('Test Home'), 'new forecast was never fetched');
  assert(getEl('forecast-board').innerHTML.includes('1-1 (12%)'), 'published score shape was lost');
  assert(getEl('forecast-board').innerHTML.includes('Short training history'), 'reason strings were lost');
  assert(!getEl('picks-grid').innerHTML.includes('locked-card'), 'model forecast was locked by a missing rank');
});

check('model forecasts are public research cards without invented executable edges', () => {
  const {sandbox,getEl} = evaluate();
  state(sandbox,{data:{active_picks:[pick],summary:{}}});
  sandbox.renderPicks();
  const html = getEl('picks-grid').innerHTML;
  assert(html.includes('Model forecast') && html.includes('no stake recommended'), 'research label missing');
  assert(!html.includes('Flagship Diamonds') && !html.includes('locked-card'), 'legacy claims were imposed');
  assert(!html.includes('undefined') && !html.includes('+0.0%'), 'missing fields fabricated values');
  assert(getEl('slate-edges-count').textContent==='0 Executable Edges', 'unpriced research counted as executable');
});

check('initial transport failure keeps polling and recovers', async () => {
  const {sandbox,getEl} = evaluate();
  let timers = 0;
  sandbox.setInterval = () => {timers++; return 1;};
  sandbox.fetch = async () => {throw new Error('test network outage');};
  await sandbox.loadData();
  assert(timers>0, 'failed first load never scheduled a retry');
  sandbox.fetch = async path => response(path==='/api/dashboard'
    ? {active_picks:[pick],summary:{}} : forecast);
  await sandbox.pollDashboard();
  assert(getEl('picks-grid').innerHTML.includes('Test Home'), 'retry did not recover');
});

check('public data starts loading while session initialization is pending', async () => {
  const {sandbox} = evaluate();
  let loaded = false;
  sandbox.setupEventListeners = () => {};
  sandbox.setupAuthUI = () => {};
  sandbox.auth = {init:()=>new Promise(()=>{})};
  sandbox.loadData = async () => {loaded = true;};
  sandbox.initApp();
  assert(loaded, 'public data waited indefinitely for session initialization');
});

check('daily board renders only saved fixtures, escapes source fields, and handles an empty period', async () => {
  const {sandbox,getEl} = evaluate();
  const calls = [];
  sandbox.fetch = async path => {calls.push(path); return response({success:true,
    timezone:'Africa/Lagos',worker_observed_at:new Date().toISOString(),
    board:{today:[{home_team:'<img onerror=x>',away_team:'Test Away',
      sport_key:'soccer_epl',commence_time:kickoff,status:'IN_PLAY',score:[1,0],
      source:'test-source'}],tomorrow:[],this_week:[]}});};
  await sandbox.loadDailyBoard('today');
  const html = getEl('daily-board-content').innerHTML;
  assert(calls[0]==='/api/daily-board', 'saved calendar was never fetched');
  assert(html.includes('&lt;img onerror=x&gt;') && html.includes('1–0'), 'source content or observed score lost');
  assert(!html.includes('Arsenal') && !html.includes('1.85'), 'placeholder fixtures leaked into the calendar');
  sandbox.switchBoardTab('tomorrow');
  await new Promise(resolve=>setImmediate(resolve));
  assert(getEl('daily-board-content').innerHTML.includes('No fixtures recorded'), 'empty period or global event dependency failed');
});

check('overlapping polls cannot duplicate requests', async () => {
  const {sandbox} = evaluate();
  let calls=0, release;
  const wait=new Promise(resolve=>{release=resolve;});
  sandbox.fetch=async()=>{calls++; await wait; return response({});};
  const first=sandbox.pollDashboard();
  await sandbox.pollDashboard();
  assert(calls===2, 'a second poll started while the first was still running');
  release();
  await first;
});

check('an empty day exposes the already collected future fixtures', async () => {
  const {sandbox,getEl} = evaluate();
  const future={home_team:'Test Home',away_team:'Test Away',sport_key:'soccer_epl',
    commence_time:kickoff,status:'SCHEDULED',source:'test-source'};
  sandbox.fetch=async()=>response({success:true,timezone:'Africa/Lagos',
    worker_observed_at:new Date().toISOString(),
    board:{today:[],tomorrow:[],this_week:[future],all_upcoming:[future]}});
  await sandbox.loadDailyBoard('today');
  const empty=getEl('daily-board-content').innerHTML;
  assert(empty.includes('1 verified upcoming fixture is recorded'), 'future coverage was presented as missing data');
  assert(empty.includes('Next kickoff:') && empty.includes('View Next Matches'), 'next fixture or upcoming navigation missing');
  sandbox.switchBoardTab('upcoming');
  await new Promise(resolve=>setImmediate(resolve));
  assert(getEl('daily-board-content').innerHTML.includes('Test Home'), 'upcoming navigation did not reveal the saved fixture');
});

check('an empty forecast explains its published horizon', () => {
  const {sandbox,getEl}=evaluate();
  state(sandbox,{forecast:{count:0,matches:[]},data:{pipeline:{window_hours:24}}});
  sandbox.renderForecastBoard();
  const empty=getEl('forecast-board').innerHTML;
  assert(empty.includes('next 24 hours') && empty.includes('Daily Board → Next Matches'), 'forecast window was unexplained');
});

check('default calendar shows the next matches even beyond this week', async () => {
  const {sandbox,getEl}=evaluate();
  sandbox.fetch=async()=>response({success:true,timezone:'Africa/Lagos',
    board:{today:[],tomorrow:[],this_week:[],all_upcoming:[{
      home_team:'Distant Home',away_team:'Distant Away',sport_key:'soccer_epl',
      commence_time:new Date(Date.now()+14*86400000).toISOString(),status:'SCHEDULED'}]}});
  await sandbox.loadDailyBoard();
  assert(getEl('daily-board-content').innerHTML.includes('Distant Home'), 'default hid the nearest distant match');
  assert(vm.runInContext('state.dailyBoardTab',sandbox)==='upcoming', 'default tab is date-restricted');
});

check('forecast cards order kickoff before probability and marquee flags', () => {
  const {sandbox,getEl}=evaluate();
  const match=(id,hours,p,flags={})=>({...forecast.matches[0],match_id:id,home:id,
    commence_at:new Date(Date.now()+hours*3600000).toISOString(),
    model:{p_home:p,p_draw:(1-p)/2,p_away:(1-p)/2},...flags});
  const simultaneous=new Date(Date.now()+3600000).toISOString();
  const high=match('Early High',1,.8),low=match('Early Low',1,.5);
  high.commence_at=low.commence_at=simultaneous;
  state(sandbox,{forecast:{count:3,top_pick:'Later Marquee',matches:[
    match('Later Marquee',100,.9,{marquee:true,is_top_pick:true}),low,high]}});
  sandbox.renderForecastBoard();
  const html=getEl('forecast-board').innerHTML;
  assert(html.indexOf('Early High')<html.indexOf('Early Low'), 'simultaneous stronger forecast was buried');
  assert(html.indexOf('Early Low')<html.indexOf('Later Marquee'), 'marquee promotion hid the closest match');
  assert(html.includes('Upcoming Match Forecasts') && !html.includes('Fixtures Forecast Today'), 'future fixtures labeled today');
});

check('pick cards order kickoff before winning probability', () => {
  const {sandbox,getEl}=evaluate();
  const simultaneous=new Date(Date.now()+3600000).toISOString();
  state(sandbox,{data:{summary:{},active_picks:[
    {...pick,home_team:'Later High',dedupe_key:'later',p_true:.9,
      commence_time:new Date(Date.now()+100*3600000).toISOString()},
    {...pick,home_team:'Early Low',dedupe_key:'low',p_true:.5,commence_time:simultaneous},
    {...pick,home_team:'Early High',dedupe_key:'high',p_true:.8,commence_time:simultaneous}]}});
  sandbox.renderPicks();
  const html=getEl('picks-grid').innerHTML;
  assert(html.indexOf('Early High')<html.indexOf('Early Low'), 'simultaneous stronger pick was buried');
  assert(html.indexOf('Early Low')<html.indexOf('Later High'), 'farther pick preceded an earlier kickoff');
});

check('past kickoff predictions stay visible awaiting a confirmed result without fabricated CLV', () => {
  const {sandbox,getEl} = evaluate();
  state(sandbox,{data:{awaiting_results:[pick],settled_ledger:[]}});
  sandbox.renderLedger();
  const html=getEl('ledger-tbody').innerHTML;
  assert(html.includes('Test Home') && html.includes('Awaiting confirmed result'), 'pending prediction vanished');
  assert(!html.includes('+0.0%') && html.includes('n/a'), 'missing closing data fabricated a CLV measurement');
});

(async()=>{
  let passed=0;
  for(const [name,run] of checks){
    try{await run(); passed++;}
    catch(error){console.error(name+': '+error.message); process.exitCode=1;}
  }
  console.log(`${passed} passed, ${checks.length-passed} failed · dashboard updates`);
})();
