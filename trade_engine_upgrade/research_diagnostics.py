"""Dependence-aware resampling, chronological validation and risk diagnostics."""
from __future__ import annotations
from dataclasses import asdict, replace
from statistics import NormalDist
from collections import Counter
import math
import numpy as np
import pandas as pd
from research_core import (Candidate, Evaluation, ResearchConfig, build_paths,
    evaluate_candidates, search, metrics, dependence, pf, equity_paths, drawdowns, streaks)

def sample_indices(n,sims,steps,rng,method='block',block_length=5):
    if n<1 or sims<1 or steps<1 or block_length<1:
        raise ValueError('Resampling requires positive sample/simulation/step/block sizes')
    if method=='iid': return rng.integers(0,n,size=(sims,steps))
    if method=='block':
        b=min(n,int(block_length)); count=math.ceil(steps/b)
        starts=rng.integers(0,n,size=(sims,count,1))
        return ((starts+np.arange(b))%n).reshape(sims,-1)[:,:steps]
    if method=='stationary':
        restart=rng.random((sims,steps))<1/min(n,block_length)
        restart[:,0]=True
        last=np.maximum.accumulate(np.where(restart,np.arange(steps),0),axis=1)
        starts=rng.integers(0,n,size=(sims,steps))
        return (np.take_along_axis(starts,last,axis=1)+np.arange(steps)-last)%n
    raise ValueError('Unknown bootstrap method')

def probability_interval(p,n):
    z=1.959963984540054; den=1+z*z/n
    mid=(p+z*z/(2*n))/den
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [max(0,mid-radius),min(1,mid+radius)]

def monte_carlo(result,cfg,method='block',exposure=1.,optimistic=None,parameter_uncertainty=False,block_length=None):
    n=len(result.net)
    if not n: return {'status':'UNAVAILABLE','reason':'No observations'}
    if exposure<0 or exposure>1: raise ValueError('Monte Carlo exposure must be 0..1')
    rng=np.random.default_rng(cfg.seed)
    finals=[]; dds=[]; losses=[]; ruins=[]
    b=block_length or cfg.block_length
    batch=min(cfg.mc_batch,max(1,cfg.memory_mb*1024**2//max(1,n*8*16)))
    for start in range(0,cfg.mc_sims,batch):
        count=min(batch,cfg.mc_sims-start)
        idx=sample_indices(n,count,n,rng,method,b)
        if parameter_uncertainty:
            # Two-stage predictive bootstrap: one historical block-resampled pool
            # per future path. Explicit sensitivity scenario, not a posterior.
            pool=sample_indices(n,count,n,rng,'block',b)
            idx=np.take_along_axis(pool,idx,axis=1)
        gross=result.gross[idx]
        if optimistic is not None:
            choose=(rng.random(idx.shape)<.5)&result.conflict[idx]
            gross=np.where(choose,optimistic.gross[idx],gross)
        cost=result.cost[idx]
        if cfg.cost_uncertainty:
            # Separate recurring/cash execution components with independent
            # path-level shocks. Mean-one multipliers; costs never turn negative.
            fixed=float(cfg.execution.costs(0,False))
            carry=np.maximum(0,cost-fixed)
            sigma=cfg.cost_uncertainty
            shock=rng.lognormal(-sigma*sigma/2,sigma,(count,2))
            cost=fixed*shock[:,0,None]+carry*shock[:,1,None]
        paths=(gross-cost)*exposure
        eq=equity_paths(paths); peak=np.maximum(1,np.maximum.accumulate(eq,axis=1))
        finals.append(eq[:,-1]-1); dds.append(-(eq/peak-1).min(axis=1))
        losses.append(streaks(paths)); ruins.append((eq<=cfg.ruin_equity).any(axis=1))
    f=np.concatenate(finals); dd=np.concatenate(dds); ls=np.concatenate(losses); ruin=np.concatenate(ruins)
    out={'status':'AVAILABLE','method':method,'simulations':cfg.mc_sims,'seed':cfg.seed,'trades_per_path':n,
         'block_length':b,'exposure':exposure,'parameter_uncertainty':parameter_uncertainty,
         'mean_ending_return':float(f.mean()),'median_ending_return':float(np.median(f)),
         'p05_ending_return':float(np.quantile(f,.05)),'p01_ending_return':float(np.quantile(f,.01)),
         'p95_ending_return':float(np.quantile(f,.95)),'probability_positive':float(np.mean(f>0)),
         'probability_loss':float(np.mean(f<0)),'median_max_drawdown_severity':float(np.median(dd)),
         'p95_adverse_max_drawdown_severity':float(np.quantile(dd,.95)),
         'expected_longest_losing_streak':float(ls.mean()),'p95_longest_losing_streak':float(np.quantile(ls,.95)),
         'probability_specified_losing_streak':float(np.mean(ls>=cfg.losing_streak_threshold)),
         'losing_streak_threshold':cfg.losing_streak_threshold,
         'probability_ruin_threshold':float(ruin.mean()),'ruin_equity_threshold':cfg.ruin_equity,
         'return_predictive_interval_95':np.quantile(f,[.025,.975]).tolist(),
         'mean_ending_return_mc_standard_error':float(f.std(ddof=1)/np.sqrt(len(f))),
         'interpretation':'Sequential trade-equity scenarios at frozen exposure; overlapping positions are not a calendar portfolio',
         'uncertainty_note':'Resampling probabilities are empirical scenario frequencies, not guaranteed future probabilities'}
    for threshold in (.1,.2,.3,.4,.5):
        out[f'probability_drawdown_gt_{round(threshold*100)}']=float(np.mean(dd>threshold))
    for key in ('probability_loss','probability_positive','probability_ruin_threshold'):
        out[key+'_mc_interval_95']=probability_interval(out[key],cfg.mc_sims)
    return out

def monte_carlo_robustness_score(simulations, cfg=None):
    """Conservatively aggregate all resampling and cost/parameter scenarios."""
    by_method={}
    for name, scenario in (simulations or {}).items():
        if not isinstance(scenario,dict) or scenario.get('status')!='AVAILABLE':
            continue
        loss=scenario.get('probability_loss')
        dd=scenario.get('p95_adverse_max_drawdown_severity')
        if loss is None or dd is None:
            continue
        # Both loss frequency and adverse drawdown are policy-relevant.  The
        # minimum is deliberate: a strong iid result cannot hide a weak block,
        # stationary, or parameter/cost uncertainty result.
        scenario_score=100*min(max(0.,1-float(loss)),max(0.,1-float(dd)))
        by_method[name]=float(max(0.,min(100.,scenario_score)))
    if not by_method:
        return {'status':'NOT_APPLICABLE','score':None,'by_method':{},'note':'No usable Monte Carlo scenario was available'}
    method=min(by_method,key=by_method.get)
    return {'status':'PASS' if by_method[method]>=60 else 'REVIEW',
            'score':float(by_method[method]),'by_method':by_method,
            'minimum_method':method,
            'note':'Conservative minimum across iid, block, stationary, and parameter/cost uncertainty scenarios; scenario frequencies are conditional resampling diagnostics, not future probabilities.'}

def bootstrap_statistics(result,cache,cfg,total_trials=1,trial_sharpes=None):
    a=result.net; n=len(a)
    dep=dependence(cache,result); ess=dep['effective_observations']
    if n<2:
        return {'status':'INSUFFICIENT','dependence':dep,'overfit_risk':'OVERFIT_RISK_UNRESOLVED',
                'overfit_risk_diagnostic':'EXTREME','overfit_classification':'OVERFIT_RISK_UNRESOLVED',
                'overfit_evidence_available':False,'trials_counted':int(total_trials),
                'trial_count_interpretation':f'{int(total_trials)} is a conservative search-count diagnostic, not a count of independent hypotheses'}
    b=min(n,max(cfg.block_length,int(math.ceil(n/max(ess,1)))))
    rng=np.random.default_rng(cfg.seed+100)
    means=[]; pfs=[]; wins=[]; downside=[]
    for start in range(0,cfg.bootstrap_reps,cfg.mc_batch):
        count=min(cfg.mc_batch,cfg.bootstrap_reps-start)
        paths=a[sample_indices(n,count,n,rng,'block',b)]
        means.extend(paths.mean(axis=1)); wins.extend((paths>0).mean(axis=1))
        loss=-np.minimum(paths,0).sum(axis=1); win=np.maximum(paths,0).sum(axis=1)
        ratios=np.divide(win,loss,out=np.full(count,np.nan),where=loss>0)
        ratios[(loss==0)&(win>0)]=np.inf
        pfs.extend(ratios)
        k=max(1,math.ceil(.05*n))
        downside.extend(np.sort(paths,axis=1)[:,:k].mean(axis=1))
    means=np.array(means); pfs=np.array(pfs); wins=np.array(wins)
    # Centered block bootstrap diagnostic with explicit trial-count adjustment.
    p=(1+np.sum((means-a.mean())>=a.mean()))/(len(means)+1)
    adjusted=min(1.,p*max(1,total_trials))
    se=float(np.std(means,ddof=1))
    # Order-statistic quantiles preserve infinite PF rather than inf-inf -> NaN.
    def interval(v):
        valid=np.asarray(v)[~np.isnan(v)]
        return np.quantile(valid,[.025,.975],method='inverted_cdf').tolist() if len(valid) else [np.nan,np.nan]
    out={'status':'AVAILABLE','dependence':dep,'block_length':b,'bootstrap_repetitions':cfg.bootstrap_reps,
         'expected_return_interval_95':interval(means),'profit_factor_interval_95':interval(pfs),
         'win_probability_interval_95':interval(wins),'downside_mean_interval_95':interval(downside),
         'standard_error_mean':se,'t_statistic_diagnostic':float(a.mean()/se) if se>0 else None,
         'bootstrap_probability_mean_positive':float(np.mean(means>0)),
         'bootstrap_probability_pf_above_one':float(np.mean(pfs>1)),
         'undefined_pf_resample_fraction':float(np.mean(np.isnan(pfs))),
         'centered_bootstrap_p_diagnostic':float(p),'bonferroni_p_diagnostic':float(adjusted),
         'trials_counted':int(total_trials),'probabilistic_sharpe_ratio':None,'deflated_sharpe_ratio':None,
         'assumptions':['Circular block resampling assumes local stationarity and adequate block length.',
                        'Bootstrap positive fraction is not a Bayesian probability of a true edge.',
                        'Bonferroni diagnostic counts all searched candidates plus declared previous trials; it is not a calibrated adaptive-search Reality Check.',
                        'No reliable numerical probability of overfitting is inferred from these diagnostics.'],
         'pbo_cscv':None,'white_reality_check':None,'hansen_spa':None,
         'advanced_test_omission':'Not claimed: heterogeneous adaptive families/overlapping observations lack the aligned independent trial experiment needed for reliable inference',
         'overfit_classification':'OVERFIT_RISK_UNRESOLVED',
         'overfit_evidence_available':False,
         'trial_count_interpretation':f'{int(total_trials)} is a conservative search-count diagnostic, not a count of independent hypotheses'}
    if n>=30 and dep['overlap_ratio']<.01 and dep['autocorrelation_effective_observations']>=.9*n and a.std(ddof=1)>0:
        sr=a.mean()/a.std(ddof=1); z=(a-a.mean())/a.std()
        skew=float(np.mean(z**3)); kurt=float(np.mean(z**4))
        variance=(1-skew*sr+(kurt-1)*sr*sr/4)/(n-1)
        if variance>0:
            out['probabilistic_sharpe_ratio']=NormalDist().cdf(sr/math.sqrt(variance))
            sharpes=np.asarray(trial_sharpes if trial_sharpes is not None else [],float)
            sharpes=sharpes[np.isfinite(sharpes)]
            if len(sharpes)>1 and total_trials>1:
                euler=.5772156649015329; nd=NormalDist()
                expected_max=sharpes.std(ddof=1)*((1-euler)*nd.inv_cdf(1-1/total_trials)+euler*nd.inv_cdf(1-1/(total_trials*math.e)))
                out['deflated_sharpe_ratio']=nd.cdf((sr-expected_max)/math.sqrt(variance))
                out['dsr_assumption']='Independent-trial count upper-bound approximation; correlated/adaptive searches limit interpretation'
        out['sharpe_inference_note']='Conditional IID/asymptotic trade-level diagnostic, nonannualised; screening cannot prove independence'
    else:
        out['sharpe_inference_note']='PSR/DSR withheld: overlap, autocorrelation, insufficient observations or degenerate variance violate working assumptions'
    prob=out['bootstrap_probability_mean_positive']
    out['overfit_risk_diagnostic']='EXTREME' if ess<20 or prob<.6 else 'HIGH' if adjusted>.05 or ess<50 else 'MODERATE' if ess<100 else 'LOW'
    # The implemented diagnostics cannot establish overfit evidence because
    # the adaptive search is correlated and no valid CSCV/PBO, SPA or Reality
    # Check is available. Preserve the conservative numeric diagnostic above,
    # but expose the decision label as unresolved rather than evidence.
    out['overfit_risk']='OVERFIT_RISK_UNRESOLVED'
    return out

def subset(result,mask):
    return Evaluation(result.candidate,*(getattr(result,k)[mask] for k in ('gross','net','hold_days','exit_index','outcome','conflict','stop_distance','cost','partial_index')))

def stability(result,cache,cfg,vol_edges):
    a=result.net
    records=[]
    vol=np.where(cache.vol<vol_edges[0],'low',np.where(cache.vol<vol_edges[1],'medium','high'))
    vol[~np.isfinite(cache.vol)]='unknown'
    trend=np.where(cache.trend>cache.vol*np.sqrt(cfg.feature_lookback),'positive',np.where(cache.trend<-cache.vol*np.sqrt(cfg.feature_lookback),'negative','sideways'))
    trend[~np.isfinite(cache.trend)|~np.isfinite(cache.vol)]='unknown'
    for name,labels in (('volatility',vol),('trend',trend)):
        for label in np.unique(labels):
            mask=labels==label; m=metrics(subset(result,mask))
            records.append({'dimension':name,'regime':label,'observations':int(mask.sum()),'mean_return':m['mean_return'],
                            'profit_factor':m['profit_factor'],'win_probability':m['win_probability'],'expected_r':m['expected_r'],
                            'gross_positive_pnl_fraction':float(np.maximum(a[mask],0).sum()/np.maximum(a,0).sum()) if (a>0).any() else None,
                            'sufficient_sample':int(mask.sum())>=max(10,cfg.min_trades//3)})
    observed=[r for r in records if r['regime']!='unknown']
    qualified=[r for r in observed if r['sufficient_sample']]
    supported_performance=100*np.mean([r['mean_return']>0 and r['profit_factor']>1 for r in qualified]) if qualified else 0.
    # Coverage is a separate confidence dimension.  A single populated
    # volatility/trend bucket cannot earn a 100 score simply because it was
    # profitable; unsupported buckets remain visible in the denominator.
    coverage_score=100*len(qualified)/len(observed) if observed else 0.
    score=float(math.sqrt(max(0.,supported_performance)*max(0.,coverage_score)))
    concentrated=any(r['gross_positive_pnl_fraction'] is not None and r['gross_positive_pnl_fraction']>.8 and r['observations']<max(10,.2*len(a)) for r in records)
    years=pd.DatetimeIndex(cache.entry_dates).year
    annual=[]
    for y in sorted(set(years)):
        m=metrics(subset(result,years==y)); annual.append({'year':int(y),**m})
    rolling=[]
    window=max(10,cfg.min_trades)
    for end in range(window,len(a)+1,max(1,window//2)):
        mask=np.zeros(len(a),bool); mask[end-window:end]=True
        rolling.append({'end_date':str(pd.Timestamp(cache.entry_dates[end-1]).date()),**metrics(subset(result,mask))})
    means=np.array([r['mean_return'] for r in annual])
    def slope(key):
        vals=np.array([r[key] for r in annual]); mask=np.isfinite(vals)
        return float(np.polyfit(np.arange(len(vals))[mask],vals[mask],1)[0]) if mask.sum()>1 else None
    temporal={'yearly':annual,'rolling_n_trades':rolling,'profitable_year_fraction':float(np.mean(means>0)) if len(means) else None,
              'expectancy_trend':slope('mean_return'),'profit_factor_trend':slope('profit_factor'),'win_probability_trend':slope('win_probability'),
              'recent_deterioration':bool(len(means)>1 and means[-1]<0 and means[-1]<np.median(means[:-1])),
              'structural_break_test':None,'note':'Descriptive time stability; no unsupported structural-break significance claim'}
    return {'regimes':records,'score':float(score),
            'supported_regime_performance_score':float(supported_performance),
            'regime_coverage_score':float(coverage_score),
            'overall_regime_robustness_score':float(score),
            'supported_regime_count':len(qualified),'observed_regime_count':len(observed),
            'concentrated_profitability':concentrated,
            'worst_regime':min(qualified,key=lambda r:r['mean_return']) if qualified else None,
            'volatility_thresholds_train_only':list(vol_edges),'time_stability':temporal}

def _time_fold_windows(market,cfg):
    dates=market.dates[(market.dates>=np.datetime64(cfg.train_start))&(market.dates<=np.datetime64(cfg.train_end))]
    n=len(dates)
    if cfg.walk_forward=='none': return []
    if n<=cfg.min_train_bars+cfg.folds:
        return []
    boundaries=np.linspace(cfg.min_train_bars,n,cfg.folds+1,dtype=int)
    folds=[]
    for k in range(cfg.folds):
        cut=boundaries[k]; end=boundaries[k+1]-1
        start=0 if cfg.walk_forward=='expanding' else max(0,cut-cfg.rolling_train_bars)
        folds.append((dates[start],dates[cut-1],dates[cut],dates[end]))
    return folds

def _adaptive_fold_windows(market,entries,cfg):
    """Build chronological folds from eligible entry counts after purge."""
    eligible=entries[entries.valid & entries.direction.eq(cfg.direction) & entries.split.eq('TRAIN') &
                     entries.entry_date.between(cfg.train_start,cfg.train_end)].sort_values(['entry_date','entry_id'],kind='stable')
    dates=pd.DatetimeIndex(eligible.entry_date.drop_duplicates().sort_values())
    required_train=max(int(cfg.min_train_trades_per_fold),int(cfg.min_trades))
    required_validation=int(cfg.min_validation_trades_per_fold)
    specs=[]; not_created=[]; lower=max(required_train-1,0); fold=1
    while fold<=cfg.folds and lower < len(dates):
        found=None
        for train_end_idx in range(lower,len(dates)-required_validation):
            for validation_end_idx in range(train_end_idx+required_validation,len(dates)):
                validation_start_idx=train_end_idx+1
                train_start=dates[0] if cfg.walk_forward=='expanding' else dates[max(0,train_end_idx-int(cfg.rolling_train_bars)+1)]
                train_end=dates[train_end_idx]
                validation_start=dates[validation_start_idx]
                validation_end=dates[validation_end_idx]
                train=build_paths(market,entries,cfg,train_start,train_end,split='TRAIN',embargo=fold>1)
                validation=build_paths(market,entries,cfg,validation_start,validation_end,split='TRAIN',embargo=True)
                if len(train)>=required_train and len(validation)>=required_validation:
                    found=(train_start,train_end,validation_start,validation_end)
                    break
            if found is not None: break
        if found is None:
            not_created.append({'fold':fold,'status':'NOT_CREATED_INSUFFICIENT_HISTORY',
                                'reason':'No chronological train/validation windows meet post-purge observation requirements',
                                'required_train_trades':required_train,
                                'required_validation_trades':required_validation,
                                'available_entry_dates':int(len(dates))})
            break
        specs.append(found)
        validation_end_idx=int(np.searchsorted(dates,found[3],side='right'))-1
        lower=max(required_train-1,validation_end_idx)
        fold+=1
    return specs,not_created

def fold_windows(market,cfg,entries=None):
    """Return time-based windows for compatibility; adaptive construction is entry-aware."""
    if entries is not None and cfg.walk_forward_fold_method in ('trade_count','adaptive'):
        return _adaptive_fold_windows(market,entries,cfg)[0]
    return _time_fold_windows(market,cfg)

def walk_forward(market,entries,cfg,rule_check=None,requested=()):
    folds=[]; total_trials=0
    not_created=[]
    if cfg.walk_forward_fold_method in ('trade_count','adaptive'):
        windows,not_created=_adaptive_fold_windows(market,entries,cfg)
    else:
        windows=_time_fold_windows(market,cfg)
    for i,(ts,te,vs,ve) in enumerate(windows):
        train=build_paths(market,entries,cfg,ts,te,split='TRAIN',embargo=i>0)
        val=build_paths(market,entries,cfg,vs,ve,split='TRAIN',embargo=True)
        fold={'fold':i+1,'train_start':str(pd.Timestamp(ts).date()),'train_end':str(pd.Timestamp(te).date()),
              'validation_start':str(pd.Timestamp(vs).date()),'validation_end':str(pd.Timestamp(ve).date()),
              'train_exclusions':train.exclusions,'validation_exclusions':val.exclusions,'embargo_dates':val.embargo_dates,'pass':False}
        required_train=max(int(cfg.min_train_trades_per_fold),int(cfg.min_trades))
        required_validation=int(cfg.min_validation_trades_per_fold)
        if len(train)<required_train or len(val)<required_validation:
            fold.update(status='NOT_CREATED_INSUFFICIENT_HISTORY',mean_return=None,profit_factor=None,expected_r=None,
                        required_train_trades=required_train,required_validation_trades=required_validation)
            not_created.append({'fold':i+1,'status':'NOT_CREATED_INSUFFICIENT_HISTORY',
                                'reason':'Time window cannot meet post-purge observation requirements',
                                'required_train_trades':required_train,'required_validation_trades':required_validation,
                                'train_observations':len(train),'validation_observations':len(val)})
        else:
            rows,candidates=search(train,cfg,requested); total_trials+=len(candidates)
            eligible=[r for r in rows if r['mean_return']>0 and r['profit_factor']>=cfg.min_pf and r['parameter_robustness_score']>=cfg.min_parameter_score and (rule_check is None or rule_check(r))]
            if not eligible:
                fold.update(status='REJECTED_TRAIN',mean_return=None,profit_factor=None,expected_r=None)
            else:
                chosen=eligible[0]['candidate']
                result=evaluate_candidates(val,[chosen],cfg)[0]; m=metrics(result); dep=dependence(val,result)
                passed=m['mean_return']>0 and m['profit_factor']>=cfg.min_pf and dep['effective_observations']>=cfg.min_ess
                fold.update(status='PASS' if passed else 'FAIL',selected=asdict(chosen),selected_key=chosen.key,pass_=bool(passed),**m)
                fold['pass']=bool(passed)
        folds.append(fold)
    usable=[r for r in folds if r.get('mean_return') is not None and r.get('status') in ('PASS','FAIL')]
    means=np.array([r['mean_return'] for r in usable]); pfs=np.array([r['profit_factor'] for r in usable]); ers=np.array([r['expected_r'] for r in usable])
    keys=[r['selected_key'] for r in usable]
    frequency=max(Counter(keys).values())/len(keys) if keys else 0.
    param_disp={}
    for field in ('stop','target','horizon'):
        values=np.array([r['selected'][field] for r in usable])
        param_disp[field+'_coefficient_of_variation']=float(values.std()/values.mean()) if len(values) and values.mean()>0 else None
    if cfg.walk_forward=='none': wf_status='DISABLED'
    elif not usable: wf_status='INCONCLUSIVE'
    elif (sum(r['pass'] for r in usable)/len(usable))>=cfg.min_fold_pass: wf_status='PASS'
    else: wf_status='FAIL'
    return {'enabled':cfg.walk_forward!='none','mode':cfg.walk_forward,'fold_method':cfg.walk_forward_fold_method,
            'status':wf_status,'folds':folds,'number_of_folds':len(folds),'created_folds':len(folds),
            'not_created_folds':not_created,'not_created_folds_count':len(not_created),
            'min_train_trades_per_fold':int(cfg.min_train_trades_per_fold),
            'min_validation_trades_per_fold':int(cfg.min_validation_trades_per_fold),
            'usable_folds':len(usable),'profitable_folds':int((means>0).sum()),'losing_folds':int((means<0).sum()),
            'median_fold_return':float(np.median(means)) if len(means) else None,'worst_fold_return':float(means.min()) if len(means) else None,
            'median_pf':float(np.median(pfs)) if len(pfs) else None,'worst_pf':float(pfs.min()) if len(pfs) else None,
            'median_expected_r':float(np.median(ers)) if len(ers) else None,'worst_expected_r':float(ers.min()) if len(ers) else None,
            'parameter_stability':{'modal_selection_fraction':frequency,**param_disp},
            'pass_fraction':sum(r['pass'] for r in usable)/len(usable) if usable else None,
            'performance_dispersion':float(means.std()) if len(means) else None,
            'degradation_slope':float(np.polyfit(np.arange(len(means)),means,1)[0]) if len(means)>1 else None,
            'trials_tested':total_trials,'note':'Nested train-only folds refit candidate grids and select using each fold training data. Structurally impossible folds are not performance failures. Final validation and final test are absent.'}

def stress_suite(result,cache,cfg,market=None,entries=None):
    a=result.net; rows=[]; rng=np.random.default_rng(cfg.seed+200)
    base_cost_configured=bool(np.nanmax(np.asarray(cfg.execution.costs(365,cache.direction=='SHORT'),dtype=float))>0)
    def add(name,arr,applicable=True,reason=None):
        if not applicable:
            rows.append({'scenario':name,'status':'NOT_APPLICABLE','mean_return':None,'profit_factor':None,
                         'passed':None,'score_excluded':True,'reason':reason or 'Scenario requires a configured non-zero base cost'})
            return
        if not len(arr):
            rows.append({'scenario':name,'status':'UNAVAILABLE','mean_return':None,'profit_factor':None,'passed':None,'score_excluded':True}); return
        rows.append({'scenario':name,'status':'EVALUATED','mean_return':float(arr.mean()),'profit_factor':pf(arr),
                     'sequential_trade_max_drawdown':float(drawdowns(arr)),'passed':bool(arr.mean()>0 and pf(arr)>1),'score_excluded':False})
    add('base',a)
    for mult in (1.5,2.,3.):
        add(f'cost_{mult:g}x',result.gross-result.cost*mult,applicable=base_cost_configured,
            reason='Base execution cost is zero; cost multiplier has no defined economic interpretation')
    for bps in (5,15,30): add(f'extra_slippage_{bps}bps',a-bps/10000)
    winners=np.flatnonzero(a>0); losing=a[a<0]
    for amount in (.05,.10,.15):
        degraded=a.copy(); count=min(len(winners),int(math.ceil(len(a)*amount)))
        if count:
            ids=rng.choice(winners,count,replace=False)
            degraded[ids]=rng.choice(losing,count) if len(losing) else -result.stop_distance[ids]-result.cost[ids]
        add(f'win_probability_minus_{round(amount*100)}pp',degraded)
    for amount in (.05,.1,.2):
        add(f'winner_magnitude_minus_{round(amount*100)}pct',np.where(a>0,a*(1-amount),a))
        add(f'loser_magnitude_plus_{round(amount*100)}pct',np.where(a<0,a*(1+amount),a))
    combined=np.where(a<0,a*1.2,a*.8)-2*result.cost-.003
    count=min(len(winners),math.ceil(len(a)*.1))
    if count:
        ids=rng.choice(winners,count,replace=False)
        combined[ids]=-result.stop_distance[ids]*1.2-result.cost[ids]*3-.003
    add('combined_adverse',combined)
    c=result.candidate
    for field in ('stop','target'):
        for multiplier in (.8,.9,1.1,1.2):
            new=replace(c,**{field:getattr(c,field)*multiplier})
            try: evaluated=evaluate_candidates(cache,[new],cfg)[0]
            except ValueError:
                add(f'{field}_{multiplier:g}x',np.array([])); continue
            add(f'{field}_{multiplier:g}x',evaluated.net)
    if cfg.calendar_months is None:
        for horizon in sorted(set(cfg.horizons)):
            if horizon!=c.horizon: add(f'horizon_{horizon}',evaluate_candidates(cache,[replace(c,horizon=horizon)],cfg)[0].net)
    if market is not None and entries is not None:
        for delay in (1,2,3):
            delayed=build_paths(market,entries,cfg,cfg.validation_start,cfg.validation_end,split='VALIDATION',delay=delay)
            rr=evaluate_candidates(delayed,[c],cfg)
            add(f'delayed_entry_{delay}_bars',rr[0].net if rr else np.array([]))
    available=[r for r in rows if r['mean_return'] is not None]
    evaluable=[r for r in rows if r.get('passed') is not None]
    survived=sum(bool(r['passed']) for r in evaluable)
    return {'scenarios':rows,'scenarios_evaluated':len(evaluable),'scenarios_excluded':len(rows)-len(evaluable),
            'scenarios_survived':survived,'scenarios_failed':sum(not bool(r['passed']) for r in evaluable),
            'score':100*survived/len(evaluable) if evaluable else None,'worst_scenario':min(available,key=lambda r:r['mean_return']) if available else None,
            'base_expectancy':float(a.mean()),'note':'Win-rate shocks are absolute percentage points. Delays enter at delayed close; all paths remain inside validation.'}

def calendar_portfolio(market,cache,result,exposure,cfg):
    """Daily close mark-to-market, fixed units per trade, no daily rebalancing.

    Entry sizing uses previous close equity, capped at gross notional exposure.
    Terminal cash P&L reconciles to the execution engine, including partial exits.
    Intraday margin and liquidation liquidity remain outside this daily model.
    """
    n=len(cache)
    if not n: return {'status':'UNAVAILABLE'}
    start=int(cache.entry_indices.min()); end=int((cache.entry_indices+result.exit_index+1).max())
    units=np.zeros(n); entered=np.zeros(n,bool); realized=np.zeros(n); capital=1.; equity=[]; gross=[]
    entry_lookup={}
    for k,i in enumerate(cache.entry_indices): entry_lookup.setdefault(int(i),[]).append(k)
    closed=np.zeros(n,bool); sign=1 if cache.direction=='LONG' else -1
    for day in range(start,end+1):
        # New close-time orders are funded from prior close equity, deterministic
        # chronological ID priority when gross cap binds.
        mark=np.zeros(n)
        active=entered&~closed
        for k in np.flatnonzero(active):
            j=day-cache.entry_indices[k]-1
            if j>=result.exit_index[k]:
                realized[k]=units[k]*result.net[k]; closed[k]=True
            elif j>=0:
                raw=sign*(market.close[day]/cache.entry_prices[k]-1)
                partial=result.partial_index[k]>=0 and j>=result.partial_index[k]
                if partial:
                    _,tg=(result.stop_distance[k],result.candidate.target*(1 if result.candidate.normalisation=='percent' else (cache.atr[k] if result.candidate.normalisation=='atr' else cache.vol[k])))
                    raw=cfg.partial_fraction*tg+(1-cfg.partial_fraction)*raw
                elapsed=(market.dates[day]-cache.entry_dates[k]).astype('timedelta64[D]').astype(int)
                charged=float(elapsed)
                if partial:
                    charged-=cfg.partial_fraction*(elapsed-cache.days[k,result.partial_index[k]])
                mark[k]=units[k]*(raw-cfg.execution.costs(charged,cache.direction=='SHORT'))
        current=1+realized.sum()+mark.sum()
        current=max(0,float(current))
        active=entered&~closed
        remaining=np.ones(n)
        for k in np.flatnonzero(active):
            if result.partial_index[k]>=0 and day-cache.entry_indices[k]-1>=result.partial_index[k]: remaining[k]=1-cfg.partial_fraction
        used=float(np.sum(units[active]*remaining[active]*market.close[day]/cache.entry_prices[active]))
        for k in entry_lookup.get(day,[]):
            allocation=min(exposure*capital,max(0,cfg.portfolio_gross_cap*current-used)) if capital>0 else 0
            units[k]=allocation; entered[k]=True; used+=allocation
            # Execution costs enter the next close mark and the terminal realized
            # P&L through result.net. Charging them here would double count.
        equity.append(current); gross.append(used/current if current>0 else 0.)
        capital=current
    eq=np.array(equity); peaks=np.maximum(1,np.maximum.accumulate(eq))
    return {'status':'AVAILABLE','calendar_max_drawdown':float((eq/peaks-1).min()),'ending_return':float(eq[-1]-1),
            'maximum_gross_exposure':float(max(gross)),'average_gross_exposure':float(np.mean(gross)),
            'skipped_for_cap':int((units==0).sum()),'daily_equity':[{'date':str(pd.Timestamp(d).date()),'equity':float(e)} for d,e in zip(market.dates[start:end+1],eq)],
            'assumptions':'Daily-close MTM, fixed initial units per entry, costs accrued, partial realization, cash notional exposure, fixed gross cap. No cross-asset margin/netting or intraday liquidation.'}

def _subset_cache(cache, mask):
    """Keep PathCache arrays aligned when a frozen result is split by time."""
    mask=np.asarray(mask,bool)
    return replace(cache, **{name:getattr(cache,name)[mask] for name in
        ('ids','entry_dates','entry_prices','entry_indices','lengths','fav','adv','close','opening','days','atr','vol','trend')})

def _relative_change(recent, prior):
    if recent is None or prior is None or not np.isfinite(recent) or not np.isfinite(prior):
        return None
    return float((recent-prior)/abs(prior)) if abs(prior)>1e-12 else float(recent-prior)

def _temporal_groups(entry_dates, cfg):
    """Return chronological masks without ever shuffling observations."""
    dates=pd.DatetimeIndex(entry_dates)
    n=len(dates); minimum=int(cfg.temporal_min_trades_per_block)
    max_blocks=min(int(cfg.temporal_max_blocks), n//minimum if minimum else 0)
    if max_blocks<int(cfg.temporal_min_blocks): return []
    for k in range(max_blocks,int(cfg.temporal_min_blocks)-1,-1):
        if cfg.temporal_robustness_method=='equal_trade_count_periods':
            groups=[idx for idx in np.array_split(np.arange(n),k) if len(idx)]
        else:
            lo=dates.min().to_datetime64(); hi=(dates.max()+pd.Timedelta(nanoseconds=1)).to_datetime64()
            bounds=np.linspace(lo.astype('int64'),hi.astype('int64'),k+1).astype('datetime64[ns]')
            labels=np.searchsorted(bounds[1:],dates.to_numpy(),side='right')
            groups=[np.flatnonzero(labels==i) for i in range(k)]
            groups=[idx for idx in groups if len(idx)]
        if len(groups)==k and min(map(len,groups))>=minimum:
            return groups
    return []

def _period_metrics(result,cache,mask,period):
    rr=subset(result,mask); cc=_subset_cache(cache,mask); m=metrics(rr,cc,True); d=dependence(cc,rr)
    return {'period':int(period),'start_date':str(pd.Timestamp(cc.entry_dates.min()).date()),
            'end_date':str(pd.Timestamp(cc.entry_dates.max()).date()),'raw_trades':int(len(rr.net)),
            'effective_observations':float(d.get('effective_observations',0.)),
            'mean_return':m.get('mean_return'),'median_return':m.get('median_return'),
            'expected_r':m.get('expected_r'),'profit_factor':m.get('profit_factor'),
            'win_rate':m.get('win_probability'),'payoff_ratio':m.get('payoff_ratio'),
            'sequential_trade_max_drawdown':m.get('sequential_trade_max_drawdown'),
            'expected_shortfall_return':m.get('expected_shortfall_return_5'),
            'average_holding_sessions':m.get('average_holding_sessions'),
            'average_holding_calendar_days':m.get('average_holding_calendar_days'),
            'stop_rate':m.get('stop_probability'),'target_rate':m.get('target_probability'),
            'timeout_rate':m.get('timeout_probability')}

def _concentration(result,periods):
    a=np.asarray(result.net,float); total=float(a.sum()); winners=np.sort(a[a>0])[::-1]
    def contribution(k): return float(winners[:min(k,len(winners))].sum()/total) if total>0 else None
    block_pnl=[float(np.sum(result.net[np.asarray(p['_positions'],int)])) for p in periods]
    best=max(block_pnl) if block_pnl else None
    return {'top_1_trade_contribution':contribution(1),'top_3_trade_contribution':contribution(3),
            'top_5_trade_contribution':contribution(5),'top_10_percent_trade_contribution':contribution(max(1,math.ceil(.1*len(a)))),
            'best_temporal_block_contribution':float(best/total) if best is not None and total>0 else None,
            'total_net_return':total,
            'interpretation':'Winner concentration is descriptive; positive-skewed strategies can naturally have concentrated winners and this is not a standalone rejection rule.'}

def _purged_temporal_cv(market,entries,cfg,candidate,periods):
    if market is None or entries is None or not cfg.temporal_cv_enabled:
        return {'status':'NOT_APPLICABLE','blocks':[],'note':'Optional purged temporal CV disabled.'}
    rows=[]; insufficient=False
    for i,p in enumerate(periods,1):
        start=pd.Timestamp(p['start_date']); end=pd.Timestamp(p['end_date'])
        block=build_paths(market,entries,cfg,start,end,split=None,embargo=True)
        if len(block)<cfg.temporal_min_trades_per_block:
            insufficient=True
            rows.append({'period':i,'status':'INCONCLUSIVE','raw_trades':int(len(block)),
                         'reason':'Purging and embargo leave fewer than the configured minimum observations'})
            continue
        rr=evaluate_candidates(block,[candidate],cfg)[0]
        m=metrics(rr,block,True); d=dependence(block,rr)
        rows.append({'period':i,'status':'PASS' if m.get('mean_return',0)>0 and m.get('profit_factor',0)>1 and m.get('expected_r',0)>0 else 'FAIL',
                     'raw_trades':int(len(rr.net)),'effective_observations':float(d.get('effective_observations',0.)),
                     'mean_return':m.get('mean_return'),'profit_factor':m.get('profit_factor'),'expected_r':m.get('expected_r')})
    status='INCONCLUSIVE' if insufficient or not rows else 'PASS' if all(r['status']=='PASS' for r in rows) else 'FAIL'
    return {'status':status,'blocks':rows,'note':'Frozen-candidate chronological stability diagnostic with horizon purging and embargo; it is not independent OOS confirmation and performs no parameter optimisation.'}

def temporal_robustness(result,cache,cfg,market=None,entries=None,candidate=None):
    """Evaluate a frozen candidate across chronological PRE-TEST blocks."""
    n=len(result.net)
    groups=_temporal_groups(cache.entry_dates,cfg)
    base={'method':cfg.temporal_robustness_method,'status':'INCONCLUSIVE','blocks':[],
          'number_of_blocks':0,'positive_blocks':0,'pf_gt_one_blocks':0,'expected_r_positive_blocks':0,
          'positive_period_fraction':None,'pf_gt_one_period_fraction':None,'expected_r_positive_period_fraction':None,
          'worst_period_mean_return':None,'worst_period_pf':None,'worst_period_expected_r':None,
          'recent_period_mean_return':None,'recent_period_pf':None,'recent_period_expected_r':None,
          'recent_vs_prior_mean_return_change':None,'recent_vs_prior_pf_change':None,'recent_vs_prior_expected_r_change':None,
          'dispersion_expectancy':None,'dispersion_pf':None,'lobo_robustness':{'status':'INCONCLUSIVE','blocks':[]},
          'pnl_concentration':{},'purged_temporal_cv':{'status':'NOT_APPLICABLE','blocks':[]},
          'temporal_parameter_stability':{'status':'NOT_APPLICABLE'},'score':None,
          'note':'Temporal robustness uses a frozen candidate on PRE-TEST observations only; it is stability evidence, not independent OOS confirmation.'}
    if not groups:
        base['reason']=f'Fewer than {cfg.temporal_min_blocks} chronological blocks can satisfy the configured minimum of {cfg.temporal_min_trades_per_block} trades per block.'
        return base
    periods=[]
    for i,idx in enumerate(groups,1):
        mask=np.zeros(n,bool); mask[idx]=True
        row=_period_metrics(result,cache,mask,i); row['_positions']=idx.tolist(); periods.append(row)
    public=[{k:v for k,v in row.items() if not k.startswith('_')} for row in periods]
    positive=sum(r['mean_return']>0 for r in periods); pf_good=sum(r['profit_factor']>1 for r in periods); er_good=sum(r['expected_r']>0 for r in periods)
    prior=periods[:-1]; recent=periods[-1]
    def avg(key):
        vals=[r[key] for r in prior if r[key] is not None and np.isfinite(r[key])]
        return float(np.mean(vals)) if vals else None
    prior_mean,prior_pf,prior_er=avg('mean_return'),avg('profit_factor'),avg('expected_r')
    concentration=_concentration(result,periods)
    # Leave-one-block-out keeps the frozen candidate and only deletes history.
    lobo=[]
    for i in range(len(groups)):
        mask=np.ones(n,bool); mask[groups[i]]=False
        lobo.append({'removed_period':i+1,**_period_metrics(result,cache,mask,i+1)})
    lobo_good=sum((r.get('mean_return') is not None and r.get('mean_return')>0 and r.get('profit_factor',0)>1 and r.get('expected_r',0)>0) for r in lobo)
    lobo_status='INCONCLUSIVE' if not lobo else 'PASS' if lobo_good==len(lobo) else 'REVIEW' if lobo_good>=math.ceil(.67*len(lobo)) else 'FAIL'
    lobo_summary={'status':lobo_status,'blocks':lobo,'minimum_leave_one_block_out_mean':min((r['mean_return'] for r in lobo),default=None),
                  'minimum_leave_one_block_out_pf':min((r['profit_factor'] for r in lobo),default=None),
                  'minimum_leave_one_block_out_expected_r':min((r['expected_r'] for r in lobo),default=None),
                  'profitable_deletion_fraction':float(lobo_good/len(lobo)) if lobo else None,
                  'note':'LOBO deletes each chronological block from the frozen PRE-TEST history. It is a sensitivity analysis, not independent OOS testing.'}
    # A score describes temporal edge consistency only; evidence quantity is
    # scored separately below.
    recent_score=1. if recent['mean_return']>0 and recent['profit_factor']>1 and recent['expected_r']>0 else 0.
    lobo_score=float(lobo_good/len(lobo)) if lobo else 0.
    purged_cv=_purged_temporal_cv(market,entries,cfg,candidate,public) if candidate is not None else {'status':'NOT_APPLICABLE','blocks':[]}
    concentration_penalty=min(20.,max(0.,(concentration.get('top_10_percent_trade_contribution') or 0.)-.5)*40.)
    score=max(0.,100*(.28*positive/len(periods)+.24*pf_good/len(periods)+.24*er_good/len(periods)+.12*recent_score+.12*lobo_score)-concentration_penalty)
    status='FAIL' if recent_score==0 and recent['mean_return']<0 else 'REVIEW' if purged_cv.get('status')=='FAIL' or lobo_status=='FAIL' else 'PASS' if score>=cfg.min_temporal_robustness_score and positive/len(periods)>=cfg.min_temporal_positive_fraction and pf_good/len(periods)>=cfg.min_temporal_pf_positive_fraction and er_good/len(periods)>=cfg.min_temporal_expected_r_positive_fraction else 'REVIEW'
    base.update({'status':status,'blocks':public,'number_of_blocks':len(periods),'positive_blocks':int(positive),
                 'pf_gt_one_blocks':int(pf_good),'expected_r_positive_blocks':int(er_good),
                 'positive_period_fraction':float(positive/len(periods)),'pf_gt_one_period_fraction':float(pf_good/len(periods)),
                 'expected_r_positive_period_fraction':float(er_good/len(periods)),
                 'worst_period_mean_return':float(min(r['mean_return'] for r in periods)),
                 'worst_period_pf':float(min(r['profit_factor'] for r in periods)),
                 'worst_period_expected_r':float(min(r['expected_r'] for r in periods)),
                 'recent_period_mean_return':recent['mean_return'],'recent_period_pf':recent['profit_factor'],'recent_period_expected_r':recent['expected_r'],
                 'recent_vs_prior_mean_return_change':_relative_change(recent['mean_return'],prior_mean),
                 'recent_vs_prior_pf_change':_relative_change(recent['profit_factor'],prior_pf),
                 'recent_vs_prior_expected_r_change':_relative_change(recent['expected_r'],prior_er),
                 'dispersion_expectancy':float(np.std([r['mean_return'] for r in periods])),
                 'dispersion_pf':float(np.std([r['profit_factor'] for r in periods])),
                 'lobo_robustness':lobo_summary,'pnl_concentration':{**concentration,'robustness_score_penalty':concentration_penalty},'score':float(score),
                 'purged_temporal_cv':purged_cv})
    return base

def temporal_parameter_stability(train_cache,train_rows,candidate,cfg):
    """Assess the frozen TRAIN neighbourhood across time without selecting per block."""
    groups=_temporal_groups(train_cache.entry_dates,cfg)
    if not groups: return {'status':'INCONCLUSIVE','score':None,'reason':'TRAIN cannot form the configured temporal blocks.'}
    nearby=[]
    for row in train_rows:
        cc=row.get('candidate')
        if cc is None or cc.family!=candidate.family or cc.normalisation!=candidate.normalisation: continue
        if abs(cc.stop/candidate.stop-1)<=.2 and abs(cc.target/candidate.target-1)<=.2 and abs(cc.horizon/candidate.horizon-1)<=.5:
            nearby.append(cc)
    unique={x.key:x for x in nearby}; candidates=list(unique.values())[:25]
    if not candidates: candidates=[candidate]
    per_block=[]
    for i,idx in enumerate(groups,1):
        mask=np.zeros(len(train_cache),bool); mask[idx]=True; scores=[]
        block=_subset_cache(train_cache,mask)
        for cc in candidates:
            rr=evaluate_candidates(train_cache,[cc],cfg)[0]
            scores.append(float(metrics(subset(rr,mask),block).get('mean_return',np.nan)))
        finite=[x for x in scores if np.isfinite(x)]
        per_block.append({'period':i,'nearby_candidates':len(candidates),'positive_fraction':float(np.mean(np.asarray(finite)>0)) if finite else None,'median_neighbourhood_expectancy':float(np.median(finite)) if finite else None})
    score=100*float(np.mean([r['positive_fraction'] for r in per_block]))
    return {'status':'PASS' if score>=cfg.min_temporal_robustness_score else 'REVIEW','score':score,
            'nearby_candidates_evaluated':len(candidates),'selected_candidate':asdict(candidate),'blocks':per_block,
            'note':'TRAIN-only neighbourhood diagnostic. Parameters are not reselected per period and VALIDATION/TEST never influence this score.'}

def evidence_sufficiency(pretest_result,pretest_cache,temporal,wf,regime,cfg,validation_dep=None,final_oos_available=False):
    dep=dependence(pretest_cache,pretest_result); n=len(pretest_result.net); ess=float(dep.get('effective_observations',0.)); dates=pd.DatetimeIndex(pretest_cache.entry_dates)
    years=max(0.,(dates.max()-dates.min()).days/365.25) if len(dates)>1 else 0.
    blocks=int(temporal.get('number_of_blocks',0)); regimes=len([r for r in (regime or {}).get('regimes',[]) if r.get('regime')!='unknown'])
    depth_components={'raw_pre_test_observations':min(100.,100*n/max(1,cfg.temporal_min_blocks*cfg.temporal_min_trades_per_block)),
                      'pre_test_effective_observations':min(100.,100*ess/max(1,2*cfg.min_ess)),
                      'usable_temporal_blocks':min(100.,100*blocks/max(1,cfg.temporal_min_blocks)),
                      'low_overlap':max(0.,100*(1-float(dep.get('overlap_ratio',1.)))),
                      'signal_spacing':min(100.,100/(1+max(0.,dep.get('average_concurrent_trades',1)-1)*4)),
                      'history_length':min(100.,100*years/10.),
                      'regime_coverage':min(100.,100*regimes/3.)}
    outer_ess=float((validation_dep or {}).get('effective_observations',0.))
    outer_raw=int((validation_dep or {}).get('raw_observations', (validation_dep or {}).get('raw_trades',0)) or 0)
    purged_status=(temporal.get('purged_temporal_cv') or {}).get('status','NOT_APPLICABLE')
    purged_score={'PASS':100.,'REVIEW':50.,'FAIL':0.}.get(purged_status,0.)
    wf_folds=int(wf.get('usable_folds',0) or 0)
    independent_components={
        'outer_validation_effective_observations':min(100.,100*outer_ess/max(1,2*cfg.min_ess)),
        'outer_validation_raw_observations':min(100.,100*outer_raw/max(1,cfg.min_trades)),
        'classical_walk_forward_usable_folds':min(100.,100*wf_folds/max(1,cfg.temporal_min_blocks)),
        'purged_temporal_cv':purged_score,
        # Pending final OOS is reported explicitly and excluded from the
        # pre-test denominator until that analysis is actually run.
        'final_oos_availability':100. if final_oos_available else None,
    }
    depth_score=float(np.mean(list(depth_components.values()))) if depth_components else 0.
    independent_values=[v for v in independent_components.values() if v is not None]
    independent_score=float(np.mean(independent_values)) if independent_values else 0.
    # The minimum prevents a long history from compensating for weak outer or
    # independent confirmation.  Both dimensions remain visible in JSON.
    score=float(min(depth_score,independent_score))
    if not blocks: status='INCONCLUSIVE'
    elif independent_score < cfg.min_evidence_sufficiency_score: status='REVIEW'
    elif depth_score >= cfg.min_evidence_sufficiency_score and independent_score >= cfg.min_evidence_sufficiency_score: status='PASS'
    else: status='REVIEW'
    return {'status':status,'score':score,'historical_sample_depth_score':depth_score,
            'independent_confirmation_strength_score':independent_score,
            'historical_sample_depth':{'score':depth_score,'components':depth_components},
            'independent_confirmation_strength':{'score':independent_score,'components':independent_components},
            'components':{**depth_components,**independent_components},'raw_pre_test_observations':n,
            'effective_observations':ess,'outer_validation_effective_observations':outer_ess,
            'outer_validation_raw_observations':outer_raw,'history_years':years,'temporal_blocks':blocks,
            'classical_wf_usable_folds':wf_folds,'purged_temporal_cv_status':purged_status,
            'final_oos_available':bool(final_oos_available),'final_oos_status':'AVAILABLE' if final_oos_available else 'PENDING',
            'regime_blocks':regimes,'note':'Historical sample depth and independent confirmation strength are separate. The conservative overall score is their minimum; long history cannot compensate for weak validation, purged CV, walk-forward, or final OOS evidence.'}

def observed_edge_quality(validation_metrics,parameter_score,stress,mc,temporal,regime,degradation,cfg):
    """Score observed edge characteristics without rewarding sample size."""
    pf_value=validation_metrics.get('profit_factor'); er=validation_metrics.get('expected_r'); mean=validation_metrics.get('mean_return')
    mc_score=(mc.get('robustness') or {}).get('score') if isinstance(mc,dict) else None
    if mc_score is None and isinstance(mc,dict):
        mc_score=100*max(0.,1-mc.get('probability_loss',1))
    components={'validation_expectancy':100. if mean is not None and mean>0 else 0.,
                'validation_profit_factor':min(100.,100*pf_value/2.) if pf_value is not None and np.isfinite(pf_value) else 0.,
                'validation_expected_r':min(100.,100*max(0.,er)/.5) if er is not None and np.isfinite(er) else 0.,
                'parameter_robustness':float(parameter_score),'stress_robustness':float(stress.get('score',0.)),
                'monte_carlo_robustness':float(mc_score or 0.),
                'temporal_consistency':float(temporal.get('score',0.) or 0.),
                'regime_consistency':float(regime.get('score',0.)),
                'train_to_validation_stability':100.*max(0.,min(1.,1+float(degradation.get('train_to_validation_return_degradation') or 0.)))}
    score=float(np.mean(list(components.values())))
    status='PASS' if score>=60 else 'REVIEW' if score>=40 else 'FAIL'
    return {'status':status,'score':score,'components':components,
            'note':'Observed edge quality describes the frozen candidate’s measured performance and robustness. It excludes evidence quantity and is not a probability of a persistent edge.'}

def sizing_diagnostics(result,cfg,confidence=0):
    m=metrics(result); a=result.net
    p=m['win_probability']; payoff=m['payoff_ratio']
    kelly=p-(1-p)/payoff if np.isfinite(payoff) and payoff>0 else None
    exposure=min(cfg.max_exposure,cfg.risk_fraction/float(np.quantile(result.stop_distance,.95)))
    return {'frozen_exposure':float(exposure),'risk_fraction':cfg.risk_fraction,'max_exposure':cfg.max_exposure,
            'fixed_fractional_notional_per_equity_at_median_stop':float(cfg.risk_fraction/np.median(result.stop_distance)),
            'kelly_binary_payoff_diagnostic':kelly,'half_kelly_diagnostic':kelly/2 if kelly is not None else None,
            'quarter_kelly_diagnostic':kelly/4 if kelly is not None else None,
            'mean_variance_kelly_diagnostic':float(a.mean()/a.var(ddof=1)) if len(a)>1 and a.var(ddof=1)>0 else None,
            'volatility_targeting_note':'Annualised volatility target unavailable from overlapping irregular trade returns; use calendar portfolio returns downstream',
            'policy':'Exposure frozen before final test using risk budget / validation 95th percentile stop, capped. Kelly never selects exposure. Portfolio Manager and Risk Manager must apply aggregate limits.'}
