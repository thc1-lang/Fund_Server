"""Safe orchestration and backward-compatible Sheets/CLI entry points.

The frozen structure is persisted before TEST paths are constructed. Test can
veto deployment but never re-rank, tune parameters or select a replacement.
"""
from __future__ import annotations
from dataclasses import asdict, replace
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
import numpy as np
import pandas as pd
from research_core import *
from research_diagnostics import *
from market_data import coverage_audit, load_market_data, market_data_quality_score, provenance_audit, readiness_report, reconcile_sources

LOG=logging.getLogger('trade_research')
METRIC_ALIASES={
    'average pair return':'mean_return','average return':'mean_return','expected return':'mean_return',
    'median pair return':'median_return','standard deviation':'standard_deviation','vol adjusted return':'trade_sharpe_like',
    'expected r multiple':'expected_r','expected r':'expected_r','profit factor':'profit_factor','pf':'profit_factor',
    'payoff ratio':'payoff_ratio','win rate':'win_probability','positive return rate':'win_probability','loss rate':'loss_probability',
    'stop-first rate':'stop_probability','target-first rate':'target_probability','horizon exit rate':'timeout_probability',
    'same-bar conflict count':'same_bar_conflict_count','same-bar conflict rate':'same_bar_conflict_rate',
    'average days to target':'average_time_to_target','average days to stop':'average_time_to_stop',
    'average holding days':'average_holding_days','median holding days':'median_holding_days',
    'time efficiency ratio':'time_efficiency_ratio','winner stop usage ratio':'winner_stop_usage_ratio',
    'target capture efficiency':'target_capture_efficiency','post-stop recovery ratio':'post_stop_recovery_ratio',
    'sequential trade max drawdown':'sequential_trade_max_drawdown','max drawdown':'sequential_trade_max_drawdown',
    'calmar-style ratio':'calmar_style_ratio','longest losing streak':'longest_losing_streak',
    'max consecutive stop-outs':'consecutive_stop_outs','worst trade':'worst_trade','best trade':'best_trade',
    'raw trades':'raw_trades','overlap ratio':'overlap_ratio','effective sample size':'effective_observations',
    'average entry spacing days':'average_entry_spacing_days',
    'parameter robustness score':'parameter_robustness_score','expected shortfall 5%':'expected_shortfall_return_5',
    'expected shortfall / stop':'expected_shortfall_r_5',
    'median r':'median_r','geometric return':'geometric_mean_trade_return','downside deviation':'downside_deviation',
    'sortino-like statistic':'sortino_like','sharpe-like trade statistic':'trade_sharpe_like',
    'maximum adverse excursion':'maximum_adverse_excursion','maximum favourable excursion':'maximum_favourable_excursion',
}

def strict_json(value):
    if isinstance(value,(Candidate,ResearchConfig,ExecutionConfig)): return strict_json(asdict(value))
    if isinstance(value,dict): return {str(k):strict_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple,np.ndarray)): return [strict_json(v) for v in value]
    if isinstance(value,(pd.Timestamp,datetime,np.datetime64)): return str(value)
    if isinstance(value,(bool,np.bool_)): return bool(value)
    if isinstance(value,(int,np.integer)): return int(value)
    if isinstance(value,(float,np.floating)): return float(value) if np.isfinite(value) else None
    if value is pd.NA or value is pd.NaT: return None
    return value

def write_json(path,value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(strict_json(value),indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(path)

def log_raw_mapping_and_preview(raw,mapping):
    if hasattr(mapping,'date'):
        mapping=asdict(mapping)
    LOG.info('Raw Data mapping:\nsource_date <- %s\ndate        <- %s\nclose       <- %s\nopen        <- %s\nhigh        <- %s\nlow         <- %s\nprice_type=%s ohlc_adjustment=%s close_type=%s',
             mapping.get('source_date'),mapping.get('date'),mapping.get('close'),mapping.get('open'),mapping.get('high'),mapping.get('low'),
             mapping.get('price_type'),mapping.get('ohlc_adjustment'),mapping.get('close_type'))
    columns=['source_date','date','close','open','high','low']
    if set(columns)<=set(raw):
        LOG.info('First 10 rows immediately after Raw Data loading:\n%s',raw[columns].head(10).to_string(index=False))

def readiness_markdown(readiness):
    coverage=readiness.get('coverage',{}); quality=readiness.get('quality',{})
    prov=readiness.get('provenance',{}); ohlc=readiness.get('ohlc_integrity',{})
    reconciliation=readiness.get('reconciliation',{}) or {}
    record=prov.get('record',{}) or {}
    lines=['MARKET DATA READINESS','',f"Instrument: {readiness.get('instrument')}",
           f"Ticker: {record.get('ticker','UNSPECIFIED')}",
           f"Asset class: {readiness.get('asset_class')}",
           f"Price type: {readiness.get('price_type')} / {readiness.get('ohlc_adjustment')}",
           f"Provider: {record.get('provider','UNSPECIFIED')}",
           f"Frequency: {record.get('frequency','')}",
           f"Trading calendar: {record.get('trading_calendar',coverage.get('trading_calendar','UNSPECIFIED'))}",
           f"Timezone: {record.get('timezone','UNSPECIFIED')}",
           f"Currency: {record.get('currency','UNSPECIFIED')}",
           '',f"Rows: {record.get('row_count','')}",
           f"Required start: {coverage.get('required_market_data_start')}",
           f"Actual start: {coverage.get('actual_market_data_start')}",
           f"Required end: {coverage.get('required_market_data_end')}",
           f"Actual end: {coverage.get('actual_market_data_end')}",
           f"Start buffer: {coverage.get('coverage_buffer_days_at_start')} days",
           f"End buffer: {coverage.get('coverage_buffer_days_at_end')} days",
           '',f"OHLC Integrity: {'FAIL' if ohlc.get('material_rows',0) else 'PASS'}",
           f"Range Coverage: {coverage.get('range_coverage_status',coverage.get('status'))}",
           f"Session Completeness: {coverage.get('session_completeness_status','PASS')}",
           f"Expected required-period sessions: {coverage.get('required_period_expected_sessions',coverage.get('calendar_expected_sessions',''))}",
           f"Observed required-period sessions: {coverage.get('required_period_observed_sessions','')}",
           f"Genuine missing sessions: {coverage.get('genuine_missing_session_count',coverage.get('unexplained_gap_count',0))}; duplicates: {coverage.get('duplicate_sessions',0)}",
           f"Long calendar gaps reviewed: {coverage.get('long_gap_count',0)}",
           f"Special exchange closures: {coverage.get('special_closure_gap_count',0)}",
           f"Normal market holidays: {coverage.get('normal_market_holiday_gap_count',0)}",
           f"Unexplained gaps: {coverage.get('unexplained_gap_count',0)}",
           f"Session Consistency: {coverage.get('session_consistency_status','PASS')}; {coverage.get('unexpected_source_session_count',0)} unexpected required-period observations",
           f"Full-history session completeness: {coverage.get('full_history_session_completeness_status','')}; {coverage.get('full_history_missing_session_count',0)} missing expected sessions",
           f"Adjustment consistency: {'FAIL' if quality.get('components',{}).get('adjustment_consistency',100) < 100 else 'PASS'}",
           f"Cross-provider validation: {reconciliation.get('status','NOT_CONFIGURED')}",
           f"Data Quality Score: {quality.get('score')}/100",f"Status: {quality.get('status')}",
           '', 'TRAIN AUTHORISED.' if readiness.get('train_authorised') else 'TRAIN NOT AUTHORISED.']
    return '\n'.join(lines)+'\n'

def write_readiness_artifacts(output_dir,run_id,readiness):
    output_dir=Path(output_dir); output_dir.mkdir(parents=True,exist_ok=True)
    json_path=output_dir/f'{run_id}.market_data_readiness.json'
    md_path=output_dir/f'{run_id}.market_data_readiness.md'
    write_json(json_path,readiness)
    md_path.write_text(readiness_markdown(readiness),encoding='utf-8')
    LOG.info('Market data readiness: %s; report=%s',readiness.get('quality',{}).get('status'),md_path)
    return json_path,md_path

def _reconciliation_for_config(raw,cfg):
    secondary=getattr(cfg,'secondary_data_path',None)
    if not secondary:
        return {'status':'FAIL' if getattr(cfg,'require_secondary_source',False) else 'NOT_CONFIGURED',
                'reason':'No secondary verification source configured'}
    bundle=load_market_data(cfg.instrument,secondary,start=cfg.train_start,end=cfg.test_end,
                            price_type=cfg.price_type,adjustment=cfg.ohlc_adjustment,
                            asset_class=cfg.asset_class,provider=cfg.secondary_provider,
                            trading_calendar=cfg.trading_calendar)
    diagnostics,summary=reconcile_sources(raw,bundle.frame)
    summary.update(status='PASS' if summary['material_discrepancies']==0 and summary['missing_primary_bars']==0 and summary['missing_secondary_bars']==0 else 'REVIEW',
                   diagnostics=diagnostics.to_dict('records'),secondary_provenance=bundle.provenance.as_dict())
    return summary

@contextmanager
def timed(timings,name):
    t=time.perf_counter()
    try: yield
    finally:
        timings[name]=timings.get(name,0)+time.perf_counter()-t
        LOG.info('%s %.3fs',name,timings[name])

def validate_rules(rules):
    for r in rules:
        name=r.get('resolved_name',r.get('name','')).strip().lower()
        if name not in METRIC_ALIASES:
            raise ValueError(f'Unsupported configured metric {name!r}; no placeholder or silent fallback is permitted')
        op=r.get('rule','').strip().lower()
        if op not in ('>','>=','<','<=','=','==','between'):
            raise ValueError(f'Invalid metric rule {op!r}')
        fields=('lower','upper') if op=='between' else ('threshold',)
        if any(r.get(k) is None or not np.isfinite(r[k]) for k in fields):
            raise ValueError(f'Missing/invalid threshold for {name}')
        if op=='between' and r['lower']>r['upper']:
            raise ValueError('Reversed between bounds')
        if not np.isfinite(r.get('weight_num',0)) or r.get('weight_num',0)<0:
            raise ValueError('Invalid metric weight')

def compare(value,rule,threshold,lower=None,upper=None):
    if value is None or (isinstance(value,(float,np.floating)) and np.isnan(value)): return False
    if rule=='>': return bool(value>threshold)
    if rule=='>=': return bool(value>=threshold)
    if rule=='<': return bool(value<threshold)
    if rule=='<=': return bool(value<=threshold)
    if rule in ('=','=='): return bool(value==threshold)
    if rule=='between': return bool(lower<=value<=upper)
    raise ValueError('Unsupported rule')

def score_rules(row,rules):
    checks=[]; hard=True; score=0
    for r in rules:
        key=METRIC_ALIASES[r.get('resolved_name',r['name']).strip().lower()]
        value=row.get(key)
        passed=compare(value,r['rule'].strip().lower(),r.get('threshold'),r.get('lower'),r.get('upper'))
        if r.get('is_hard',False) and not passed: hard=False
        if not r.get('is_hard',False) and passed: score+=r.get('weight_num',0)
        checks.append({'name':r['name'],'metric':key,'value':value,'pass':passed,'hard':r.get('is_hard',False)})
    weighted=sum(r.get('weight_num',0) for r in rules if not r.get('is_hard',False))>0
    return {'hard_pass':hard,'weighted_score':score,'weighted_pass':not weighted or score>=.7,'checks':checks}

def pre_gate(row,cfg,rules):
    checks=score_rules(row,rules)
    return bool(row['mean_return']>0 and row['profit_factor']>=cfg.min_pf
                and row['raw_trades']>=cfg.min_trades and row.get('effective_observations',0)>=cfg.min_ess
                and row['parameter_robustness_score']>=cfg.min_parameter_score
                and row['same_bar_conflict_rate']<=cfg.max_conflict_rate
                and row['sequential_trade_max_drawdown']>=-cfg.max_sequential_drawdown
                and checks['hard_pass'] and checks['weighted_pass'])

def confidence_score(stats,dep,parameter,wf,regime,stress,mc,oos,cfg,quality,temporal=None,evidence=None,edge_quality=None):
    temporal=temporal or {}; evidence=evidence or {}; edge_quality=edge_quality or {}
    components={'conditional_validation_statistical_support':100*stats.get('bootstrap_probability_mean_positive',0),
                'sample_quality':min(100,dep['effective_observations']),
                'parameter_robustness':parameter,
                'walk_forward':(100*wf['pass_fraction'] if wf.get('status') in ('PASS','FAIL') and wf.get('pass_fraction') is not None else None),
                'regime_robustness':regime['score'],'stress_robustness':stress['score'],
                'monte_carlo':(mc.get('robustness') or {}).get('score',100*(1-mc.get('probability_loss',1))),
                'final_oos':100 if oos['status']=='PASS' else 40 if oos['status']=='REVIEW' else None,
                'observed_edge_quality':edge_quality.get('score'),'evidence_sufficiency':evidence.get('score')}
    available=[v for v in (edge_quality.get('score'),evidence.get('score')) if v is not None]
    score=float(math.sqrt(available[0]*available[1])) if len(available)==2 else float(np.mean([v for v in components.values() if v is not None]))
    caps=[]
    if oos['status']=='FAIL': caps.append(('failed_final_oos',25))
    if oos['status']=='REVIEW': caps.append(('inconclusive_final_oos',50))
    if dep['effective_observations']<cfg.min_ess: caps.append(('weak_effective_sample',50))
    if parameter<cfg.min_parameter_score: caps.append(('parameter_fragility',40))
    if stress['worst_scenario'] is not None and stress['worst_scenario']['mean_return']<0: caps.append(('negative_stressed_expectancy',60))
    if stats['overfit_risk'] in ('HIGH','EXTREME','OVERFIT_RISK_UNRESOLVED'): caps.append(('overfit_risk_unresolved',50))
    if wf.get('status')=='INCONCLUSIVE' and temporal.get('status')!='PASS': caps.append(('walk_forward_inconclusive',65))
    elif not wf['enabled'] or (wf.get('pass_fraction') is not None and wf['pass_fraction']<cfg.min_fold_pass): caps.append(('walk_forward_not_supported',50))
    if quality['status']!='PASS': caps.append(('data_quality_review',50))
    if regime['concentrated_profitability'] or regime['time_stability']['recent_deterioration']: caps.append(('regime_or_time_fragility',40))
    if evidence.get('status') in ('REVIEW','INCONCLUSIVE'): caps.append(('evidence_sufficiency_limited',60))
    if temporal.get('status') in ('REVIEW','INCONCLUSIVE'): caps.append(('temporal_robustness_limited',70))
    if caps: score=min(score,min(c[1] for c in caps))
    return {'score':score,'overall_research_confidence':score,'components':components,
            'observed_edge_quality_score':edge_quality.get('score'),'evidence_sufficiency_score':evidence.get('score'),
            'caps':[{'reason':r,'maximum':v} for r,v in caps],
            'interpretation':'Overall research confidence combines observed edge quality with evidence sufficiency; it is a policy score, not a probability of profitability. Conditional validation support is not a probability of a persistent edge, and unavailable OOS components remain pending rather than zero.'}

def parameter_plateau(selected, nearby, cfg):
    """Summarise only the already evaluated neighbourhood around the frozen candidate."""
    selected=selected or {}; nearby=nearby or []
    robust=[r for r in nearby if r.get('candidate') is not None]
    candidates=[r['candidate'] for r in robust]
    if not candidates:
        return {'status':'INCONCLUSIVE','score':selected.get('parameter_robustness_score'),
                'evaluated_neighbour_count':0,'robust_neighbour_count':0,
                'reason':'No evaluated neighbouring candidates were available'}
    scores=np.asarray([float(r.get('parameter_robustness_score',0)) for r in robust])
    good=[r for r in robust if float(r.get('parameter_robustness_score',0))>=cfg.min_parameter_score]
    stops=[float(r['candidate'].stop) for r in good]; targets=[float(r['candidate'].target) for r in good]; horizons=[int(r['candidate'].horizon) for r in good]
    fraction=len(good)/len(robust)
    status='PASS' if len(good)>=3 and fraction>=.67 else 'REVIEW' if good else 'FAIL'
    return {'status':status,'score':float(selected.get('parameter_robustness_score',0)),
            'evaluated_neighbour_count':len(robust),'robust_neighbour_count':len(good),
            'robust_neighbour_fraction':float(fraction),'robust_stop_range':[min(stops),max(stops)] if stops else None,
            'robust_target_range':[min(targets),max(targets)] if targets else None,
            'robust_horizon_range':[min(horizons),max(horizons)] if horizons else None,
            'evaluated_neighbour_keys':[r['candidate'].key for r in robust],
            'note':'Plateau is derived from the finite TRAIN candidates already evaluated before outer validation; it is not a new search or a claim of superiority.'}

def build_ai_analyst_summary(report):
    structure=report.get('recommended_structure')
    validation=report.get('expected_performance') or {}
    evidence=report.get('evidence_sufficiency') or {}
    temporal=report.get('temporal_robustness') or {}
    edge=report.get('observed_edge_quality') or {}
    mc=(report.get('monte_carlo') or {}).get('robustness') or {}
    strengths=[]; limitations=[]
    if validation.get('mean_return',0)>0: strengths.append('Outer validation expectancy is positive.')
    if validation.get('profit_factor',0)>1: strengths.append('Outer validation profit factor is above one.')
    if temporal.get('status')=='PASS': strengths.append('Frozen-candidate temporal robustness passed its configured thresholds.')
    else: limitations.append(f"Temporal robustness status is {temporal.get('status','UNAVAILABLE')}.")
    if evidence.get('independent_confirmation_strength_score',0)<60: limitations.append('Independent confirmation strength remains limited.')
    if not evidence.get('final_oos_available',False): limitations.append('Final OOS evidence is pending and the holdout was not used.')
    if report.get('statistics',{}).get('overfit_risk')=='OVERFIT_RISK_UNRESOLVED': limitations.append('Adaptive-search overfit risk remains unresolved.')
    return {'instrument':report.get('instrument'),'signal_instrument':report.get('signal_instrument'),
            'decision':report.get('final_status'),'selected_structure':structure,
            'parameter_plateau':report.get('parameter_robustness',{}).get('plateau'),
            'observed_edge_quality':edge,'historical_sample_depth':evidence.get('historical_sample_depth'),
            'independent_confirmation_strength':evidence.get('independent_confirmation_strength'),
            'temporal_robustness':{'status':temporal.get('status'),'score':temporal.get('score')},
            'monte_carlo_robustness':mc,'strengths':strengths,'limitations':limitations,
            'language_policy':'Factual research summary. Backtest results are not presented as evidence of persistence.'}

def build_head_of_desk_handoff(report):
    status=report.get('final_status','FAIL'); oos=report.get('oos') or {}; decision=report.get('pre_test_decision') or {}
    actions=[]
    if not (report.get('evidence_sufficiency') or {}).get('final_oos_available',False): actions.append('Complete a separately authorised final OOS evaluation on the untouched holdout.')
    if report.get('statistics',{}).get('overfit_risk')=='OVERFIT_RISK_UNRESOLVED': actions.append('Obtain independent research or live paper evidence before deployment.')
    if report.get('execution_instrument') in (None,'UNSPECIFIED'): actions.append('Specify and validate the execution instrument and venue cost model.')
    return {'ready_for_review':True,'research_status':status,'deployable':bool(status=='PASS'),
            'pre_test_status':decision.get('pre_test_status'),'test_would_be_authorised':decision.get('test_would_be_authorised',False),
            'oos_status':oos.get('status'),'instrument':report.get('instrument'),'signal_instrument':report.get('signal_instrument'),
            'execution_instrument':report.get('execution_instrument'),'selected_structure':report.get('recommended_structure'),
            'risk_and_robustness':{'confidence':report.get('confidence'),'edge_quality':report.get('observed_edge_quality'),
                                   'evidence_sufficiency':report.get('evidence_sufficiency'),'stress':report.get('stress_tests')},
            'required_next_actions':actions,
            'decision_rule':'Deployable only when final_status=PASS and downstream portfolio, execution, and risk approval is recorded.'}

def empty_report(cfg,run_id,timings,quality,reason,train_rows=None,wf=None):
    decision={'pre_test_status':'FAIL','test_would_be_authorised':False,'test_authorised':False,'reasons':[reason],
              'blocking_failures':[reason],'review_items':[],'pending_post_test_gates':[]}
    degradation={'train_to_validation_return_degradation':None,
                  'train_to_validation_pf_degradation':None,
                  'train_to_validation_expected_r_degradation':None,
                  'validation_to_oos_return_degradation':None,
                  'validation_to_oos_pf_degradation':None,
                  'validation_to_oos_expected_r_degradation':None}
    return {'schema_version':'2.0','run_id':run_id,'instrument':cfg.instrument,
            'signal_instrument':getattr(cfg,'signal_instrument',cfg.instrument),
            'execution_instrument':getattr(cfg,'execution_instrument','UNSPECIFIED'),'direction':cfg.direction,
            'recommended_structure':None,'expected_performance':None,'risk':None,'statistics':None,
            'walk_forward':wf,'temporal_robustness':{'status':'INCONCLUSIVE','reason':'No frozen candidate available'},
            'evidence_sufficiency':{'status':'INCONCLUSIVE','score':0,'historical_sample_depth':{'score':None,'components':{}},'independent_confirmation_strength':{'score':None,'components':{}},'final_oos_available':False},'observed_edge_quality':{'status':'NOT_APPLICABLE','score':None},
            'parameter_robustness':{'score':None,'plateau':{'status':'INCONCLUSIVE','evaluated_neighbour_count':0}},
            'ai_analyst_summary':{'instrument':cfg.instrument,'decision':'FAIL','selected_structure':None,'limitations':[reason]},
            'head_of_desk_handoff':{'ready_for_review':True,'research_status':'FAIL','deployable':False,'instrument':cfg.instrument,'required_next_actions':[reason]},
            'regime_analysis':None,'stress_tests':None,'monte_carlo':None,
            'oos':{'status':'NOT_RUN','reason':'No pre-test-qualified frozen candidate','holdout_accessed':False},
            'confidence':{'score':0,'components':{},'caps':[{'reason':reason,'maximum':0}]},
            'degradation':degradation,'pre_test_decision':decision,'test_would_be_authorised':False,
            'pending_post_test_gates':[],'custom_gate_results':[],
            'data_quality':quality,'market_data_readiness':quality.get('market_data_readiness'),
            'deployability':{'status':'FAIL','deployable':False,'reasons':[reason]},
            'warnings':[reason],'failure_conditions':[reason],
            'final_status':'FAIL','configuration':asdict(cfg),'timings':timings,'train_surface':train_rows or []}

def run_research(raw,entries,cfg,output_dir,rules=None,gate_rules=None):
    """Offline callable API; no I/O except local auditable JSON artifacts."""
    cfg.validate(); rules=list(rules or []); validate_rules(rules)
    gate_rules=list(gate_rules or [])
    validate_gate_rules(gate_rules)
    output_dir=Path(output_dir); output_dir.mkdir(parents=True,exist_ok=True)
    run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:10]
    timings={}; start=time.perf_counter(); cache_usage={}; cache_dir=output_dir/'cache'
    LOG.info('run_id=%s instrument=%s direction=%s seed=%s version=%s config_hash=%s execution=%s',run_id,cfg.instrument,cfg.direction,cfg.seed,VERSION,digest(asdict(cfg)),cfg.execution.mode)
    with timed(timings,'data_loading'):
        initial_mapping=raw.attrs.get('raw_data_mapping',{}) if hasattr(raw,'attrs') else {}
        if initial_mapping:
            log_raw_mapping_and_preview(raw,initial_mapping)
        preflight_reconciliation={}
        try:
            preflight_reconciliation=_reconciliation_for_config(raw,cfg)
        except Exception as exc:
            preflight_reconciliation={'status':'FAIL','reason':str(exc)}
        try:
            raw,entries,quality=validate_data(raw,entries,cfg)
        except OHLCValidationError as exc:
            diagnostic_path=output_dir/f'{run_id}.ohlc_diagnostics.csv'
            summary_path=output_dir/f'{run_id}.ohlc_diagnostics.json'
            analysis_path=output_dir/f'{run_id}.ohlc_analysis.json'
            top20_path=output_dir/f'{run_id}.ohlc_top20.csv'
            exc.diagnostics.to_csv(diagnostic_path,index=False)
            write_json(summary_path,{'run_id':run_id,'summary':exc.summary,'diagnostics':exc.diagnostics.to_dict('records')})
            write_json(analysis_path,{'run_id':run_id,**exc.summary})
            pd.DataFrame(exc.summary.get('top_20_largest',[])).to_csv(top20_path,index=False)
            readiness=readiness_report(raw,cfg,exc.summary,raw.attrs.get('market_data_provenance',{}),preflight_reconciliation)
            readiness['train_authorised']=False
            readiness['failure_reason']='; '.join(readiness['quality'].get('hard_caps',[])) or str(exc)
            readiness_json,readiness_md=write_readiness_artifacts(output_dir,run_id,readiness)
            message=(f'MARKET DATA READINESS: FAIL; TRAIN NOT AUTHORISED. {exc} '
                     f'Full diagnostics: {diagnostic_path}; analysis: {analysis_path}; top 20: {top20_path}; '
                     f'readiness: {readiness_md}')
            LOG.error(message)
            raise MarketDataReadinessError(message,exc.diagnostics,exc.summary,readiness) from exc
        except Exception as exc:
            readiness=readiness_report(raw,cfg,None,raw.attrs.get('market_data_provenance',{}),preflight_reconciliation)
            readiness['train_authorised']=False
            readiness['failure_reason']=str(exc)
            _,readiness_md=write_readiness_artifacts(output_dir,run_id,readiness)
            message=f'MARKET DATA READINESS: FAIL; TRAIN NOT AUTHORISED. {exc}; readiness: {readiness_md}'
            LOG.error(message)
            raise MarketDataReadinessError(message,pd.DataFrame(),{'ingestion_error':str(exc)},readiness) from exc
        mapping=quality.get('raw_data_mapping',{})
        log_raw_mapping_and_preview(raw,mapping)
        readiness=readiness_report(raw,cfg,quality.get('ohlc_validation',{}),
                                   quality.get('market_data_provenance',{}),preflight_reconciliation)
        if not readiness['train_authorised']:
            readiness_json,readiness_md=write_readiness_artifacts(output_dir,run_id,readiness)
            message=f'MARKET DATA READINESS: FAIL; TRAIN NOT AUTHORISED. See {readiness_md}'
            raise MarketDataReadinessError(message,pd.DataFrame(quality.get('ohlc_diagnostics',[])),
                                           quality.get('ohlc_validation',{}),readiness)
        quality['market_data_readiness']=readiness
        # Persist the passed readiness decision as an auditable artifact too.
        # Previously only rejected inputs wrote the readiness report, leaving a
        # successful gate decision recoverable only inside the large run JSON.
        write_readiness_artifacts(output_dir,run_id,readiness)
        market=make_market(raw,cfg,quality)
    with timed(timings,'trade_cache_generation'):
        train=build_paths_cached(cache_dir,market,entries,cfg,cfg.train_start,cfg.train_end,split='TRAIN',embargo=False)
        val=build_paths_cached(cache_dir,market,entries,cfg,cfg.validation_start,cfg.validation_end,split='VALIDATION')
        cache_usage.update(train=bool(train.cache_hit),validation=bool(val.cache_hit))
    exclusions={'train':train.exclusions,'validation':val.exclusions}
    embargoes={'train':train.embargo_dates,'validation':val.embargo_dates}
    def finish(report):
        timings['total_compute']=time.perf_counter()-start
        for name in ('train_search','validation','walk_forward','temporal_robustness','test','monte_carlo','stress_testing','sheets_write'):
            timings.setdefault(name,0. if name!='sheets_write' else None)
        report.update(run_id=run_id,analysis_timestamp=datetime.now(timezone.utc).isoformat(),configuration=asdict(cfg),
                      metric_rules=rules,gate_rules=gate_rules,configuration_hash=digest({'config':asdict(cfg),'rules':rules,'gate_rules':gate_rules}),
                      input_fingerprint={'ohlc':market.fingerprint,'entries':frame_hash(entries)},version=VERSION,
                      code_hash=digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('research_*.py')}),
                      timings=timings,cache_usage=cache_usage,exclusions=exclusions,embargo_dates=embargoes,
                      periods={s:{k:getattr(cfg,f'{s}_{k}') for k in ('start','end')} for s in ('train','validation','test')})
        report=strict_json(report)
        write_json(output_dir/f'{run_id}.json',report)
        write_json(output_dir/'latest.json',report)
        return report
    if len(train)<cfg.min_trades or len(val)<cfg.min_trades:
        return finish(empty_report(cfg,run_id,timings,quality,'Insufficient trades after chronological purge/embargo'))
    with timed(timings,'train_search'):
        requested={METRIC_ALIASES[r.get('resolved_name',r['name']).strip().lower()] for r in rules}
        train_rows,candidates=search(train,cfg,requested)
        eligible=[row for row in train_rows if pre_gate(row,cfg,rules)]
    with timed(timings,'walk_forward'):
        wf=walk_forward(market,entries,cfg,lambda row: score_rules(row,rules)['hard_pass'] and score_rules(row,rules)['weighted_pass'],requested)
    if not eligible:
        return finish(empty_report(cfg,run_id,timings,quality,'No TRAIN candidate meets evidence, sample and parameter robustness gates',train_rows,wf))
    shortlisted=eligible[:cfg.shortlist]
    total_trials=len(candidates)+wf['trials_tested']+cfg.prior_research_trials
    # Candidates and thresholds are now fixed for the outer validation sample.
    with timed(timings,'validation'):
        vals=evaluate_candidates(val,[r['candidate'] for r in shortlisted],cfg)
        val_rows=[]
        for tr,rv in zip(shortlisted,vals):
            m=metrics(rv,val,requested=requested); dep=dependence(val,rv)
            decay=m['mean_return']/tr['mean_return'] if tr['mean_return']>0 else None
            row={'candidate':rv.candidate,**m,**dep,'train_parameter_robustness_score':tr['parameter_robustness_score'],
                 'return_retention':decay,'train_mean_return':tr['mean_return'],'train_profit_factor':tr['profit_factor']}
            row['qualified']=bool(m['mean_return']>0 and m['profit_factor']>=cfg.min_pf and dep['effective_observations']>=cfg.min_ess and m['raw_trades']>=cfg.min_trades and m['same_bar_conflict_rate']<=cfg.max_conflict_rate and m['sequential_trade_max_drawdown']>=-cfg.max_sequential_drawdown and decay is not None and decay>=.5)
            # Rank by train plateau support and capped validation retention, never
            # by a single largest observed mean/Sharpe. Stable deterministic ties.
            row['selection_score']=.7*tr['parameter_robustness_score']+.3*100*min(1,max(0,decay or 0))
            val_rows.append(row)
        qualified=sorted([r for r in val_rows if r['qualified']],key=lambda r:(-r['selection_score'],r['candidate']))
    if not qualified:
        report=empty_report(cfg,run_id,timings,quality,'No candidate survives outer validation',train_rows,wf)
        report['validation_candidates']=val_rows
        return finish(report)
    # Exactly one structure is selected before all deeper diagnostics. Failed
    # diagnostics reject the experiment rather than trying more alternatives.
    selected=qualified[0]; c=selected['candidate']
    rv=next(r for r in vals if r.candidate==c)
    tr=next(r for r in train_rows if r['candidate']==c)
    dep=dependence(val,rv)
    train_gate_metrics=dict(tr)
    validation_gate_metrics=metrics(rv,val,True,requested=requested)
    degradation={
        'train_to_validation_return_degradation': (validation_gate_metrics.get('mean_return')-train_gate_metrics.get('mean_return'))/abs(train_gate_metrics.get('mean_return')) if train_gate_metrics.get('mean_return') not in (None,0) else None,
        'train_to_validation_pf_degradation': (validation_gate_metrics.get('profit_factor')-train_gate_metrics.get('profit_factor'))/abs(train_gate_metrics.get('profit_factor')) if train_gate_metrics.get('profit_factor') not in (None,0) and np.isfinite(train_gate_metrics.get('profit_factor')) and np.isfinite(validation_gate_metrics.get('profit_factor')) else None,
        'train_to_validation_expected_r_degradation': (validation_gate_metrics.get('expected_r')-train_gate_metrics.get('expected_r'))/abs(train_gate_metrics.get('expected_r')) if train_gate_metrics.get('expected_r') not in (None,0) else None,
        'validation_to_oos_return_degradation': None,
        'validation_to_oos_pf_degradation': None,
        'validation_to_oos_expected_r_degradation': None,
    }
    # Sparse-signal temporal diagnostics use a single frozen candidate across
    # TRAIN plus outer VALIDATION observations. No period can re-optimise or
    # inspect the FINAL TEST window.
    with timed(timings,'temporal_robustness'):
        pretest_cache=build_paths(market,entries,cfg,cfg.train_start,cfg.validation_end,split=None,embargo=False)
        pretest_result=evaluate_candidates(pretest_cache,[c],cfg)[0]
        temporal=temporal_robustness(pretest_result,pretest_cache,cfg,market,entries,c)
        temporal['temporal_parameter_stability']=temporal_parameter_stability(train,train_rows,c,cfg)
        evidence=evidence_sufficiency(pretest_result,pretest_cache,temporal,wf,regime if 'regime' in locals() else {},cfg,validation_dep=dep,final_oos_available=False)
    with timed(timings,'statistics'):
        stats=bootstrap_statistics(rv,val,cfg,total_trials,[r['trade_sharpe_like'] for r in train_rows])
        vals_train=train.vol[np.isfinite(train.vol)]
        vol_edges=np.quantile(vals_train,[1/3,2/3]) if len(vals_train) else np.array([np.nan,np.nan])
        regime=stability(rv,val,cfg,vol_edges)
        optimistic=evaluate_candidates(val,[c],cfg,replace(cfg.execution,same_bar='target_first'))[0]
        random_order=evaluate_candidates(val,[c],cfg,replace(cfg.execution,same_bar='random'))[0]
        difference=float(optimistic.net.mean()-rv.net.mean())
        path_dependent=bool((optimistic.net.mean()>0)!=(rv.net.mean()>0) or difference>max(.001,abs(rv.net.mean())*.5))
        ambiguity={'stop_first_mean':float(rv.net.mean()),'target_first_mean':float(optimistic.net.mean()),
                   'random_order_mean':float(random_order.net.mean()),'conflict_count':int(rv.conflict.sum()),
                   'conflict_rate':float(rv.conflict.mean()),'bar_path_dependent':path_dependent,
                   'note':'Known opening gaps resolve order; random-order 50/50 is a sensitivity assumption, not inferred intraday probability'}
    with timed(timings,'stress_testing'):
        stress=stress_suite(rv,val,cfg,market,entries)
    sizing=sizing_diagnostics(rv,cfg)
    block=min(len(rv.net),max(cfg.block_length,math.ceil(len(rv.net)/max(dep['effective_observations'],1))))
    with timed(timings,'monte_carlo'):
        jobs=[('iid',False),('block',False),('stationary',False),('block',True)]
        def simulate(job):
            method,uncertain=job
            local_cfg=replace(cfg,memory_mb=max(16,cfg.memory_mb//min(4,cfg.n_jobs)))
            return monte_carlo(rv,local_cfg,method,sizing['frozen_exposure'],optimistic if uncertain else None,uncertain,block)
        parallel=cfg.n_jobs>1 and cfg.mc_sims*len(rv.net)>=cfg.parallel_min_work
        if parallel:
            with ThreadPoolExecutor(max_workers=min(cfg.n_jobs,4)) as pool:
                simulations=list(pool.map(simulate,jobs))
        else: simulations=[simulate(job) for job in jobs]
        mc=dict(zip(['iid','block','stationary','parameter_cost_execution_uncertainty'],simulations))
        mc['parallel_workers']=min(cfg.n_jobs,4) if parallel else 1
        mc['robustness']=monte_carlo_robustness_score(mc,cfg)
    edge_quality=observed_edge_quality(validation_gate_metrics,tr['parameter_robustness_score'],stress,mc,temporal,regime,degradation,cfg)
    evidence=evidence_sufficiency(pretest_result,pretest_cache,temporal,wf,regime,cfg,validation_dep=dep,final_oos_available=False)
    pre_test_gate_results=evaluate_gate_rules(gate_rules,train_gate_metrics,validation_gate_metrics,dep,None,degradation)
    pre_fail=[]; review_items=[]
    if cfg.walk_forward!='none' and wf.get('status')=='FAIL': pre_fail.append('Walk-forward performance failed on adequate usable folds')
    wf_structural_inconclusive=bool(cfg.walk_forward!='none' and wf.get('status')=='INCONCLUSIVE' and wf.get('usable_folds',0)==0 and wf.get('not_created_folds_count',0)>0 and not any(f.get('status') in ('FAIL','REJECTED_TRAIN') for f in wf.get('folds',[])))
    if cfg.walk_forward!='none' and wf.get('status')=='INCONCLUSIVE' and not wf_structural_inconclusive:
        review_items.append('Walk-forward inconclusive for reasons beyond structural history limits')
    if stress['score']<cfg.min_stress_survival*100: pre_fail.append('Stress robustness below frozen threshold')
    if stats.get('bootstrap_probability_mean_positive',0)<cfg.min_bootstrap_positive: pre_fail.append('Bootstrap expectancy evidence insufficient')
    if regime['score']<cfg.min_regime_score or regime['concentrated_profitability'] or regime['time_stability']['recent_deterioration']: pre_fail.append('Regime or temporal robustness insufficient')
    if path_dependent: pre_fail.append('BAR PATH DEPENDENT')
    if mc['block']['probability_loss']>cfg.max_mc_loss_probability or mc['block']['p95_adverse_max_drawdown_severity']>cfg.max_mc_drawdown: pre_fail.append('Monte Carlo risk gate failed')
    if temporal.get('status')=='FAIL': pre_fail.append('Temporal robustness failed')
    elif temporal.get('status') in ('REVIEW','INCONCLUSIVE'): review_items.append(f"Temporal robustness: {temporal.get('status')}")
    if evidence.get('status') in ('REVIEW','INCONCLUSIVE'): review_items.append(f"Evidence sufficiency: {evidence.get('status')}")
    for gate in pre_test_gate_results:
        if gate['result']=='FAIL' and gate.get('Hard Gate',False): pre_fail.append(f"Pre-test gate failed: {gate['Gate']}")
        elif gate['result'] in ('REVIEW','NOT_EVALUATED'): review_items.append(f"Pre-test gate {gate['Gate']}: {gate['result']}")
    alternative_wf_supported=bool(wf_structural_inconclusive and temporal.get('status')=='PASS' and evidence.get('status')=='PASS' and evidence.get('score',0)>=cfg.min_evidence_sufficiency_score)
    if wf_structural_inconclusive and alternative_wf_supported:
        review_items=[x for x in review_items if not x.startswith('Walk-forward')]
    if pre_fail: pre_test_status='FAIL'
    elif wf.get('status')=='INCONCLUSIVE' and not alternative_wf_supported: pre_test_status='INCONCLUSIVE'
    elif review_items: pre_test_status='REVIEW'
    else: pre_test_status='PASS'
    test_would_be_authorised=pre_test_status=='PASS'
    test_authorised=bool(test_would_be_authorised and cfg.allow_final_oos and cfg.allow_test_execution)
    pre_test_decision={'pre_test_status':pre_test_status,'test_would_be_authorised':test_would_be_authorised,'test_authorised':test_authorised,
                       'reasons':list(pre_fail)+list(review_items),'blocking_failures':pre_fail,
                       'review_items':review_items,'pending_post_test_gates':[],
                       'classical_walk_forward_substitution':alternative_wf_supported,
                       'temporal_robustness_status':temporal.get('status'),'evidence_sufficiency_status':evidence.get('status')}
    frozen={'run_id':run_id,'candidate':asdict(c),'execution':asdict(cfg.execution),'configuration':asdict(cfg),
            'metric_rules':rules,'gate_rules':gate_rules,'sizing':sizing,'pre_test_failures':pre_fail,
            'pre_test_decision':pre_test_decision,'degradation':degradation,
            'temporal_robustness':temporal,'evidence_sufficiency':evidence,'observed_edge_quality':edge_quality,
            'candidate_grid':[asdict(x) for x in candidates],'total_trials_counted':total_trials,
            'selection_basis':'TRAIN plateau robustness and outer validation retention; all diagnostics before TEST',
            'frozen_at':datetime.now(timezone.utc).isoformat()}
    freeze_hash=digest(strict_json(frozen))
    frozen['freeze_hash']=freeze_hash
    write_json(output_dir/f'{run_id}.frozen.json',frozen)
    # No TEST path is built unless a clean PRE_TEST PASS is explicitly allowed
    # by configuration. This keeps the real holdout untouched during audits.
    if not test_authorised:
        oos={'status':'NOT_RUN','reason':'Pre-test decision did not authorise the holdout; holdout remains unused','holdout_accessed':False}
        test_result=None; test_cache=None
    elif not cfg.allow_test_execution or not cfg.allow_final_oos:
        oos={'status':'NOT_RUN','reason':'PRE_TEST passed but final OOS execution is locked off; holdout remains unused','holdout_accessed':False}
        test_result=None; test_cache=None
    else:
        # A persistent consumption ledger prevents accidentally calling this OOS
        # "untouched" after a prior experiment on the same instrument/window.
        ledger=output_dir/'oos_ledger.json'
        history=json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else {}
        holdout_key=digest({'instrument':cfg.instrument,'direction':cfg.direction,'start':cfg.test_start,'end':cfg.test_end})
        reused=holdout_key in history
        history.setdefault(holdout_key,[]).append({'run_id':run_id,'freeze_hash':freeze_hash,'timestamp':datetime.now(timezone.utc).isoformat()})
        write_json(ledger,history)  # conservatively mark consumed before execution
        with timed(timings,'test'):
            test_cache=build_paths_cached(cache_dir,market,entries,cfg,cfg.test_start,cfg.test_end,split='TEST')
            cache_usage['test']=bool(test_cache.cache_hit)
            exclusions['test']=test_cache.exclusions; embargoes['test']=test_cache.embargo_dates
            evaluated=evaluate_candidates(test_cache,[c],cfg)
            test_result=evaluated[0] if evaluated else None
            if test_result is None:
                oos={'status':'REVIEW','reason':'No complete test trades','holdout_reused':reused,'holdout_accessed':True}
            else:
                tm=metrics(test_result,test_cache,True); td=dependence(test_cache,test_result)
                pass_test=tm['mean_return']>0 and tm['profit_factor']>=cfg.min_pf and tm['sequential_trade_max_drawdown']>=-cfg.max_sequential_drawdown and tm['same_bar_conflict_rate']<=cfg.max_conflict_rate
                enough=tm['raw_trades']>=cfg.min_trades and td['effective_observations']>=cfg.min_ess
                status='FAIL' if not pass_test else 'PASS' if enough and not reused else 'REVIEW'
                oos={'status':status,'metrics':tm,'dependence':td,'holdout_reused':reused,'holdout_accessed':True,
                     'return_retention_vs_validation':tm['mean_return']/rv.net.mean(),
                     'reason':'Frozen candidate evaluated without reranking; reused or insufficient samples cannot earn PASS',
                     'calendar_portfolio':calendar_portfolio(market,test_cache,test_result,sizing['frozen_exposure'],cfg)}
    if oos.get('metrics'):
        om=oos['metrics']
        degradation['validation_to_oos_return_degradation']=(om.get('mean_return')-validation_gate_metrics.get('mean_return'))/abs(validation_gate_metrics.get('mean_return')) if validation_gate_metrics.get('mean_return') not in (None,0) else None
        degradation['validation_to_oos_pf_degradation']=(om.get('profit_factor')-validation_gate_metrics.get('profit_factor'))/abs(validation_gate_metrics.get('profit_factor')) if validation_gate_metrics.get('profit_factor') not in (None,0) and np.isfinite(validation_gate_metrics.get('profit_factor')) and np.isfinite(om.get('profit_factor')) else None
        degradation['validation_to_oos_expected_r_degradation']=(om.get('expected_r')-validation_gate_metrics.get('expected_r'))/abs(validation_gate_metrics.get('expected_r')) if validation_gate_metrics.get('expected_r') not in (None,0) else None
    # Refresh the evidence artifact after the holdout branch so the report
    # states whether final OOS evidence is actually available.  The pre-test
    # authorization decision above remains frozen and is never retrofitted.
    evidence=evidence_sufficiency(pretest_result,pretest_cache,temporal,wf,regime,cfg,
                                  validation_dep=dep,final_oos_available=bool(oos.get('holdout_accessed',False)))
    post_test_gate_results=evaluate_gate_rules(gate_rules,train_gate_metrics,validation_gate_metrics,dep,oos,degradation)
    pending_post_test_gates=[r for r in post_test_gate_results if r['result']=='PENDING_OOS']
    pre_test_decision['pending_post_test_gates']=[r['Gate'] for r in pending_post_test_gates]
    frozen['pre_test_decision']=pre_test_decision; frozen['degradation']=degradation
    # The frozen manifest includes the final pre-test decision and degradation
    # values. Recompute the digest after those fields are populated so the
    # manifest hash remains self-consistent.
    frozen.pop('freeze_hash',None)
    freeze_hash=digest(strict_json(frozen))
    frozen['freeze_hash']=freeze_hash
    write_json(output_dir/f'{run_id}.frozen.json',frozen)
    custom_gates=post_test_gate_results
    if any(r['result']=='FAIL' and r.get('Stage')=='POST_TEST' for r in custom_gates) and oos.get('status') not in ('NOT_RUN',):
        oos['status']='FAIL'
    elif any(r['result'] in ('REVIEW','NOT_EVALUATED') for r in custom_gates) and oos.get('status')=='PASS':
        oos['status']='REVIEW'
    conf=confidence_score(stats,dep,tr['parameter_robustness_score'],wf,regime,stress,mc,oos,cfg,quality,temporal,evidence,edge_quality)
    if pre_fail: conf['score']=min(25,conf['score']); conf['caps'].append({'reason':'pre_test_gate_failure','maximum':25})
    status='FAIL' if pre_fail or oos.get('status')=='FAIL' else pre_test_status if pre_test_status in ('INCONCLUSIVE','REVIEW') else 'PASS' if oos.get('status')=='PASS' and conf['score']>=60 else 'REVIEW'
    warnings=list(pre_fail)+list(review_items)
    if stats.get('overfit_risk') in ('HIGH','EXTREME','OVERFIT_RISK_UNRESOLVED'): warnings.append('Overfit risk remains unresolved; adaptive-search diagnostics are not independent-hypothesis evidence')
    if oos.get('holdout_reused'): warnings.append('FINAL TEST HAS BEEN USED BEFORE: obtain a new untouched holdout before claiming independent confirmation')
    if quality['status']!='PASS': warnings.append('Data quality requires review')
    if cfg.execution.costs(365,cfg.direction=='SHORT')==0: warnings.append('All configured costs are zero; execution realism requires instrument-specific cost estimates')
    if getattr(cfg,'execution_instrument','UNSPECIFIED') in (None,'','UNSPECIFIED'): warnings.append('Execution instrument is unspecified; signal research is not an execution deployment specification')
    if cfg.walk_forward=='none': warnings.append('Walk-forward disabled; confidence capped')
    expected=metrics(rv,val,True)
    nearby=[r for r in train_rows if r['candidate'].normalisation==c.normalisation and r['candidate'].family==c.family and r['parameter_robustness_score']>=cfg.min_parameter_score and abs(r['candidate'].stop/c.stop-1)<=.2 and abs(r['candidate'].target/c.target-1)<=.2]
    report={'schema_version':'2.0','instrument':cfg.instrument,'signal_instrument':getattr(cfg,'signal_instrument',cfg.instrument),
            'execution_instrument':getattr(cfg,'execution_instrument','UNSPECIFIED'),'direction':cfg.direction,
            'recommended_structure':{**asdict(c),'stop_loss':c.stop,'take_profit':c.target,
                                     'max_holding_sessions':c.horizon if cfg.calendar_months is None else None,
                                     'max_holding_calendar_days':None if cfg.calendar_months is None else None,
                                     'max_holding_days':c.horizon if cfg.calendar_months is None else None,
                                     'max_holding_calendar_months':cfg.calendar_months,
                                     'horizon_unit':'trading_sessions' if cfg.calendar_months is None else 'calendar_months',
                                     'exit_method':c.family,'stop_type':c.normalisation,'target_type':c.normalisation,
                                     'deployable':status=='PASS'},
            'expected_performance':{**expected,'basis':'Cost-adjusted outer validation; selection affected this sample'},
            'risk':{'validation_calendar_portfolio':calendar_portfolio(market,val,rv,sizing['frozen_exposure'],cfg),
                    'position_sizing':sizing,'same_bar_sensitivity':ambiguity},
            'statistics':stats,'walk_forward':wf,'temporal_robustness':temporal,'evidence_sufficiency':evidence,
            'observed_edge_quality':edge_quality,'regime_analysis':regime,'stress_tests':stress,'monte_carlo':mc,
            'oos':oos,'confidence':conf,'data_quality':quality,'sample_quality':dep,
            'degradation':degradation,'pre_test_decision':pre_test_decision,
            'test_would_be_authorised':test_would_be_authorised,
            'pre_test_gate_results':pre_test_gate_results,
            'pending_post_test_gates':pending_post_test_gates,
            'warnings':warnings,'failure_conditions':pre_fail+([oos['reason']] if oos.get('status')=='FAIL' else []),
            'final_status':status,'deployability':{'status':status,'deployable':bool(status=='PASS'),
                                                    'reasons':pre_fail+list(review_items)+([] if status=='PASS' else ['Final status is not PASS'])},
            'market_data_readiness':quality.get('market_data_readiness'),
            'freeze_hash':freeze_hash,'frozen_manifest':f'{run_id}.frozen.json',
            'train_surface':train_rows,'validation_candidates':val_rows,'selected_train_metrics':tr,
            'nearby_robust_horizons':sorted({r['candidate'].horizon for r in nearby}),
            'performance_by_horizon':[{'horizon':h,'median_expectancy':float(np.median([r['mean_return'] for r in train_rows if r['candidate'].horizon==h])),'profitable_fraction':float(np.mean([r['mean_return']>0 for r in train_rows if r['candidate'].horizon==h]))} for h in sorted({r['candidate'].horizon for r in train_rows})],
            'custom_gate_results':custom_gates,
            'analyst_notes':'Consume this JSON. A frozen structure may be present on FAIL/REVIEW for audit; deploy only when final_status=PASS and downstream portfolio/risk approval is obtained.'}
    report['parameter_robustness']={'score':float(tr['parameter_robustness_score']),
                                    'selected_train_metrics':{k:tr.get(k) for k in ('parameter_robustness_score','neighbour_count','local_median_return','local_median_pf','local_median_r','neighbour_profitable_fraction','neighbour_pf_above_one','neighbour_r_positive')},
                                    'plateau':parameter_plateau(tr,nearby,cfg)}
    report['ai_analyst_summary']=build_ai_analyst_summary(report)
    report['head_of_desk_handoff']=build_head_of_desk_handoff(report)
    return finish(report)

GATE_METRICS={
    'minimum validation profit factor':('pre_test_validation','profit_factor'),
    'minimum test profit factor':('post_test','profit_factor'),
    'validation max dd floor':('pre_test_validation','sequential_trade_max_drawdown'),
    'maximum validation max dd':('pre_test_validation','sequential_trade_max_drawdown'),
    'test max dd floor':('post_test','sequential_trade_max_drawdown'),
    'maximum test max dd':('post_test','sequential_trade_max_drawdown'),
    'minimum effective trades':('pre_test_validation','effective_observations'),
    'maximum avg return degradation':('pre_test_train_validation','return_degradation'),
    'maximum average return degradation':('pre_test_train_validation','return_degradation'),
    'avg return degradation floor':('pre_test_train_validation','return_degradation'),
    'average return degradation floor':('pre_test_train_validation','return_degradation'),
    'maximum expected r degradation':('pre_test_train_validation','expected_r_degradation'),
    'expected r degradation floor':('pre_test_train_validation','expected_r_degradation'),
    'minimum validation expected r':('pre_test_validation','expected_r'),
    'minimum test expected r':('post_test','expected_r'),
    'minimum validation final return':('pre_test_validation','sequential_compounded_return'),
    'minimum test final return':('post_test','sequential_compounded_return'),
    'train to validation return degradation':('pre_test_train_validation','return_degradation'),
    'train to validation pf degradation':('pre_test_train_validation','pf_degradation'),
    'train to validation expected r degradation':('pre_test_train_validation','expected_r_degradation'),
    'validation to oos return degradation':('post_test_degradation','return_degradation'),
    'validation to oos pf degradation':('post_test_degradation','pf_degradation'),
    'validation to oos expected r degradation':('post_test_degradation','expected_r_degradation'),
}

def validate_gate_rules(rules):
    for r in rules:
        name=r['Gate'].strip().lower()
        if name not in GATE_METRICS: raise ValueError(f'Unmapped gate: {r["Gate"]}')
        source,_=GATE_METRICS[name]
        # Persist an explicit stage even for legacy sheet rules that omitted
        # the column. Test-dependent rules are POST_TEST; all other mapped
        # gates are PRE_TEST unless the caller explicitly marks them optional.
        if r.get('Stage') is None:
            r['Stage']='POST_TEST' if source.startswith('post_test') else 'PRE_TEST'
        if r['Rule'].strip().lower() not in ('>','>=','<','<=','between'): raise ValueError('Invalid gate rule')
        if r.get('Pass Threshold') is None or not np.isfinite(r['Pass Threshold']): raise ValueError('Invalid gate threshold')
        review=r.get('Review Lower Threshold')
        if review is not None and not np.isfinite(review): r['Review Lower Threshold']=None
        if r['Rule'].lower()=='between' and r.get('Review Lower Threshold') is None: raise ValueError('Between gate needs two bounds')
        if r.get('Stage') is not None and str(r['Stage']).strip().upper() not in ('PRE_TEST','POST_TEST','OPTIONAL_DIAGNOSTIC'):
            raise ValueError('Invalid gate stage')

def evaluate_gate_rules(rules,train_metrics,validation,dep,oos=None,degradation=None):
    rows=[]; oos=oos or {}; test=oos.get('metrics',{}); degradation=degradation or {}
    for r in rules:
        source,key=GATE_METRICS[r['Gate'].strip().lower()]
        stage=str(r.get('Stage') or ('POST_TEST' if source.startswith('post_test') else 'PRE_TEST')).strip().upper()
        if source=='pre_test_validation': actual=dep.get(key) if key=='effective_observations' else validation.get(key)
        elif source=='pre_test_train_validation': actual=degradation.get('train_to_validation_'+key)
        elif source=='post_test': actual=test.get(key)
        elif source=='post_test_degradation': actual=degradation.get('validation_to_oos_'+key.replace('_degradation','_degradation'))
        else: actual=None
        op=r['Rule'].strip().lower(); threshold=r['Pass Threshold']; review=r.get('Review Lower Threshold')
        if actual is None: label='PENDING_OOS' if stage=='POST_TEST' else 'NOT_EVALUATED'
        elif compare(actual,op,threshold,review,threshold): label='PASS'
        elif op!='between' and review is not None and compare(actual,op,review): label='REVIEW'
        else: label='FAIL'
        rows.append({**r,'actual':actual,'result':label,'source':source,'Stage':stage})
    return rows

def flatten(value,prefix=''):
    rows=[]
    if isinstance(value,dict):
        for k,v in value.items(): rows.extend(flatten(v,f'{prefix}.{k}' if prefix else k))
    elif isinstance(value,list): rows.append([prefix,json.dumps(strict_json(value),separators=(',',':'))])
    else: rows.append([prefix,value])
    return rows

def table(rows):
    normalized=[]
    for row in rows or []:
        row=dict(row)
        candidate=row.pop('candidate',None)
        if candidate: row={**candidate,**row}
        normalized.append({k:json.dumps(v) if isinstance(v,(dict,list)) else v for k,v in row.items()})
    return pd.DataFrame(normalized)

def write_sheets(sh,report,legacy):
    """One values update per output sheet, no numerical read-backs from Sheets."""
    def fields(obj): return pd.DataFrame(flatten(obj or {}),columns=['Field','Value'])
    selected=report.get('recommended_structure')
    sections={
        'Train':[('TRAIN MATRIX RESULTS',table(report.get('train_surface'))),('TRAIN SUMMARY',fields(report.get('selected_train_metrics'))),('TRAIN ENTRY EXCLUSIONS',table(report['exclusions'].get('train')))],
        'Validation':[('VALIDATION RESULTS',table(report.get('validation_candidates'))),('VALIDATION ROBUSTNESS REPORT',fields(report.get('selected_train_metrics'))),('VALIDATION SUMMARY',fields(report.get('expected_performance'))),('VALIDATION ENTRY EXCLUSIONS',table(report['exclusions'].get('validation')))],
        'Test':[('TEST RESULTS',fields(report['oos'])),('FINAL OOS SUMMARY',fields({'final_status':report['final_status'],'selected_structure':selected,'frozen_manifest':report.get('frozen_manifest')}))],
        'Gate':[('GATE EVALUATION',table(report.get('custom_gate_results'))),('GATE SUMMARY',fields({'final_status':report['final_status'],'confidence':report['confidence'],'failures':report['failure_conditions']}))],
        'MC':[('MC RESULTS',fields(report.get('monte_carlo')))],
        'Position Sizing':[('POSITION SIZING FINAL RECOMMENDATION SUMMARY',fields((report.get('risk') or {}).get('position_sizing'))),('POSITION SIZING STATUS',fields({'deployable':report['final_status']=='PASS','no_test_sizing_optimisation':True}))],
        'Summary':[('AI ANALYST TRADE STRUCTURE SUMMARY',fields({k:v for k,v in report.items() if k not in ('train_surface','validation_candidates','exclusions')}))],
        'Walk Forward':[('WALK FORWARD',fields(report.get('walk_forward')))],
        'Temporal Robustness':[('TEMPORAL ROBUSTNESS',fields(report.get('temporal_robustness'))),('EVIDENCE SUFFICIENCY',fields(report.get('evidence_sufficiency'))),('OBSERVED EDGE QUALITY',fields(report.get('observed_edge_quality')))],
        'Stress Tests':[('STRESS TESTS',table((report.get('stress_tests') or {}).get('scenarios')))],
        'Performance Diagnostics':[('DATA QUALITY REPORT',fields(report['data_quality'])),('TIMINGS',fields(report['timings'])),('RUN AUDIT',fields({k:report.get(k) for k in ('run_id','configuration_hash','input_fingerprint','code_hash','embargo_dates')}))],
    }
    context=('INSTRUMENT CONTEXT',fields({'instrument':report.get('instrument'),
                                          'signal_instrument':report.get('signal_instrument'),
                                          'execution_instrument':report.get('execution_instrument'),
                                          'direction':report.get('direction'),
                                          'source_of_truth':'Control Panel!C1'}))
    for name in sections:
        sections[name]=[context,*sections[name]]
    for name,parts in sections.items():
        ws=legacy.recreate_sheet(sh,name)
        # Google cells have finite length; split long nested JSON diagnostics into
        # labelled chunks while authoritative complete JSON stays on disk.
        for _,df in parts:
            for column in df.columns:
                df[column]=df[column].map(lambda v: v if not isinstance(v,str) or len(v)<45000 else v[:44000]+' [truncated in Sheets; see run JSON]')
        legacy.write_sheet(ws,parts)

def read_inputs(sh,legacy,cfg):
    if cfg.market_data_path:
        bundle=load_market_data(cfg.instrument,cfg.market_data_path,start=cfg.train_start,end=cfg.test_end,
                                price_type=cfg.price_type,adjustment=cfg.ohlc_adjustment,
                                asset_class=cfg.asset_class,provider=cfg.data_vendor,
                                ticker=cfg.ticker,provider_symbol=cfg.provider_symbol,
                                frequency=cfg.frequency,trading_calendar=cfg.trading_calendar,
                                timezone_name=cfg.timezone,currency=cfg.currency,
                                adjustment_method=cfg.adjustment_method,source_identifier=cfg.source_identifier)
        raw,mapping=bundle.frame,bundle.mapping
    else:
        raw_values=sh.worksheet('Raw Data').get_all_values()
        if not raw_values or not raw_values[0]: raise ValueError('Raw Data has no header row')
        width=len(raw_values[0]); rows=[]; source=[]
        for number,row in enumerate(raw_values[1:],start=2):
            if any(str(x).strip() for x in row):
                rows.append((row[:width]+['']*max(0,width-len(row))))
                source.append(number)
        df=pd.DataFrame(rows,columns=raw_values[0]); df['source_row_number']=source
        raw,mapping=standardise_raw_data(df,cfg.close_type,legacy.parse_date,cfg.price_type,cfg.ohlc_adjustment)
        dates=pd.to_datetime(raw['date'],errors='coerce')
        raw.attrs['market_data_provenance']={
            'instrument':cfg.instrument,'ticker':cfg.ticker,'asset_class':cfg.asset_class,
            'provider':cfg.data_vendor,'provider_symbol':cfg.provider_symbol,
            'frequency':cfg.frequency,'trading_calendar':cfg.trading_calendar,
            'timezone':cfg.timezone,'currency':cfg.currency,
            'price_type':cfg.price_type,'ohlc_adjustment':cfg.ohlc_adjustment,
            'adjustment_method':cfg.adjustment_method,
            'retrieval_timestamp':datetime.now(timezone.utc).isoformat(),
            'first_date':str(dates.min().date()) if dates.notna().any() else '',
            'last_date':str(dates.max().date()) if dates.notna().any() else '',
            'row_count':len(raw),'source_identifier':cfg.source_identifier,
            'source_hash':frame_hash(raw),'data_hash':frame_hash(raw),
        }
    log_raw_mapping_and_preview(raw,mapping)
    values=sh.worksheet('Entries').get_all_values()
    if not values or len(values[0])<7: raise ValueError('Entries needs existing A:G fields')
    df=pd.DataFrame([r+['']*(len(values[0])-len(r)) for r in values[1:] if any(str(x).strip() for x in r)],columns=values[0])
    entries=pd.DataFrame({k:df.iloc[:,i] for i,k in enumerate(('entry_id','entry_date','entry_price','direction','split','valid','source'))})
    entries['entry_date']=entries.entry_date.map(legacy.parse_date)
    return raw,entries

def run_sheets(legacy,config_path=None,output_dir=None,write=True):
    t=time.perf_counter()
    sh=legacy.connect_sheet(); old=legacy.load_control_panel(sh)
    path=Path(config_path) if config_path else Path(legacy.__file__).with_name('research_config.json')
    overrides=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    # Control Panel!C1 is the authoritative live instrument label.  A JSON
    # config may tune research policy, but it must never silently replace the
    # instrument selected by the operator in the sheet.
    try:
        instrument=read_instrument_from_control_panel(sh)
    except ValueError as exc:
        dest=Path(output_dir) if output_dir else Path(legacy.__file__).with_name('research_runs')
        dest.mkdir(parents=True,exist_ok=True)
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-instrument-error'
        payload={'schema_version':'2.0','run_id':stamp,'instrument':None,'signal_instrument':None,
                 'final_status':'FAIL','error':{'type':'CONFIGURATION_ERROR','message':str(exc)},
                 'market_data_readiness':{'status':'FAIL','instrument':'MISSING','source':'Control Panel!C1',
                                          'failure_reason':str(exc),'train_authorised':False},
                 'test_would_be_authorised':False,'oos':{'status':'NOT_RUN','holdout_accessed':False},
                 'warnings':[str(exc)]}
        write_json(dest/f'{stamp}.json',payload); write_json(dest/'latest.json',payload)
        raise
    overrides['instrument']=instrument
    configured_signal=str(overrides.get('signal_instrument','')).strip()
    if not configured_signal or configured_signal.upper() in ('UNSPECIFIED','REPLACE_WITH_INSTRUMENT'):
        overrides['signal_instrument']=instrument
    # Provider identifiers follow the selected instrument when the config has
    # no explicit mapping. This keeps provenance internally consistent without
    # retaining a stale ticker from a previous sheet selection.
    for key in ('ticker','provider_symbol'):
        value=str(overrides.get(key,'')).strip()
        if not value or value.upper() in ('UNSPECIFIED','REPLACE_WITH_INSTRUMENT'):
            overrides[key]=instrument
    source_identifier=str(overrides.get('source_identifier','')).strip()
    if not source_identifier or source_identifier.upper()=='UNSPECIFIED':
        overrides['source_identifier']='Google Sheets Raw Data; instrument selected in Control Panel!C1'
    cfg=ResearchConfig.from_legacy(old,overrides)
    rules=[asdict(m) for m in old.metrics]
    gdf=legacy.load_gate_config(sh)
    gates=gdf.to_dict('records') if not gdf.empty else []
    for g in gates:
        if pd.isna(g.get('Review Lower Threshold')): g['Review Lower Threshold']=None
    validate_rules(rules); validate_gate_rules(gates)
    raw,entries=read_inputs(sh,legacy,cfg)
    loading=time.perf_counter()-t
    dest=Path(output_dir) if output_dir else Path(legacy.__file__).with_name('research_runs')
    report=run_research(raw,entries,cfg,dest,rules,gates)
    report['timings']['sheets_read']=loading
    if write:
        t=time.perf_counter(); write_sheets(sh,report,legacy); report['timings']['sheets_write']=time.perf_counter()-t
    write_json(dest/f'{report["run_id"]}.json',report); write_json(dest/'latest.json',report)
    LOG.info('Completed run %s status=%s JSON=%s',report['run_id'],report['final_status'],dest/'latest.json')
    return report

def read_instrument_from_control_panel(sh):
    """Read and validate the operator-selected instrument from Control Panel!C1."""
    ws=sh.worksheet('Control Panel')
    value=None
    try:
        value=ws.acell('C1').value
    except Exception:
        try:
            values=ws.get('C1')
            value=values[0][0] if values and values[0] else None
        except Exception as exc:
            raise ValueError(f'INSTRUMENT_READ_ERROR: unable to read Control Panel!C1: {exc}') from exc
    text=str(value or '').strip()
    if not text or text.upper() in ('NONE','NULL','NAN','UNSPECIFIED'):
        raise ValueError('MISSING_INSTRUMENT: Control Panel!C1 must contain the live research instrument')
    return text

def cli(legacy):
    parser=argparse.ArgumentParser(description='Chronological trade-structure research engine')
    parser.add_argument('--config',help='JSON config/overrides')
    parser.add_argument('--raw',help='Offline OHLC CSV with date,open,high,low,close')
    parser.add_argument('--market-data',help='Provider-neutral local market-data source (CSV, Parquet or Feather)')
    parser.add_argument('--entries',help='Offline entries CSV with original named fields')
    parser.add_argument('--output',help='Run artifact directory')
    parser.add_argument('--no-sheets-write',action='store_true')
    parser.add_argument('--log-level',default='INFO',choices=['DEBUG','INFO','WARNING','ERROR'])
    args=parser.parse_args()
    logging.basicConfig(level=getattr(logging,args.log_level),format='%(asctime)s %(levelname)s %(message)s')
    if args.raw or args.market_data or args.entries:
        if not all([(args.raw or args.market_data),args.entries,args.config]): parser.error('Offline mode needs --market-data/--raw, --entries and --config')
        data=json.loads(Path(args.config).read_text(encoding='utf-8'))
        rules=data.pop('metric_rules',[]); gates=data.pop('gate_rules',[])
        cfg=ResearchConfig.from_dict(data)
        source=args.market_data or args.raw
        bundle=load_market_data(cfg.instrument,source,start=cfg.train_start,end=cfg.test_end,
                                price_type=cfg.price_type,adjustment=cfg.ohlc_adjustment,
                                asset_class=cfg.asset_class,provider=cfg.data_vendor,
                                ticker=cfg.ticker,provider_symbol=cfg.provider_symbol,
                                frequency=cfg.frequency,trading_calendar=cfg.trading_calendar,
                                timezone_name=cfg.timezone,currency=cfg.currency,
                                adjustment_method=cfg.adjustment_method,source_identifier=cfg.source_identifier)
        report=run_research(bundle.frame,pd.read_csv(args.entries),cfg,args.output or str(Path(legacy.__file__).with_name('research_runs')),rules,gates)
        LOG.info('Completed run %s status=%s',report['run_id'],report['final_status'])
    else: run_sheets(legacy,args.config,args.output,not args.no_sheets_write)
