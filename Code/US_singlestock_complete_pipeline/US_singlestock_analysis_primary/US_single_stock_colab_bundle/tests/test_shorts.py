import numpy as np
import pandas as pd
import pytest
import config
from src.shorts import score_shorts, short_sheet_values
from src.ranking import rank_quant_candidates


def fixture():
    scores=np.array([80.,75.,70.,65.,60.,55.,45.,40.,35.,30.,25.,20.])
    short_scores=np.clip(105-scores,0,100)
    x=pd.DataFrame({'Ticker':[f'S{i:02}' for i in range(12)],'Source Row':range(2,14),
        'Industry':['A']*6+['B']*6,'Score Confidence':90.,'Data Status':'SUFFICIENT',
        'Expanded Long Score':scores,'Expanded Short Score':short_scores,
        'Expanded Raw Short Score':50+(short_scores-50)/.9,'Short Composite Weight Coverage':1.,
        'Short Thesis Gate Pass':True,'Short Business Type Gate Pass':True,
        'short_tradability_gate_pass':True,'short_implementation_validation_required':True})
    for f,w in config.FAMILY_WEIGHTS.items():
        x[f'{f} Long Sub-score']=100-x['Expanded Raw Short Score']
        x[f'Quant Composite {f} Short Contribution Points']=x['Expanded Raw Short Score']*config.SHORT_FAMILY_WEIGHTS[f]/sum(config.SHORT_FAMILY_WEIGHTS.values())
    return x


def test_independent_short_selection_reconciles():
    x=fixture();a=score_shorts(x)
    b=rank_quant_candidates(x,config.TOP_N_LONG,config.TOP_N_LONG,config.MIN_LONG_SCORE,config.MIN_SHORT_SCORE,
        config.MAX_QUANT_LONGS_TOTAL,config.SHORT_DISPLAY_TOP_N,config.MIN_CANDIDATE_CONFIDENCE)
    assert a['Short Selected'].equals(b['Quantitative Candidate'].eq('QUANT SHORT'))
    assert not np.allclose(a['Short Score']+x['Expanded Long Score'],100)
    assert np.allclose(a.filter(regex='Contribution$').sum(axis=1),a['Short Raw Score'])


def test_no_extra_earnings_or_momentum_gates():
    x=fixture();x['F1 Consensus Est.']=-1;x['% Price Change (4 Weeks)']=20
    assert score_shorts(x)['Short Selected'].sum()>0


def test_cap_confidence_and_shared_data_veto():
    try:
        config.apply_control_panel([['','SHORT_DISPLAY_TOP_N',2]])
        x=fixture();assert score_shorts(x)['Short Selected'].sum()==2
        x['Score Confidence']=0;assert not score_shorts(x)['Short Selected'].any()
        x['Score Confidence']=100;x['Data Status']='INSUFFICIENT DATA'
        assert not score_shorts(x)['Short Selected'].any()
        config.apply_control_panel([['','SHORT_DISPLAY_TOP_N',0]])
        x=fixture();s=score_shorts(x)
        assert ['Display count is set to zero.'] in short_sheet_values(pd.concat([x,s],axis=1))
    finally:config.apply_control_panel([])


def test_short_weights_are_independent_and_invalid_display_count():
    try:
        config.apply_control_panel([['','MODEL_VERSION',config.MODEL_VERSION],['','WEIGHT_GROWTH',30]])
        assert config.short_weights()['GROWTH']==5
        assert config.FAMILY_WEIGHTS['GROWTH']==30
        config.apply_control_panel([['','MODEL_VERSION',config.MODEL_VERSION],['','SHORT_WEIGHT_GROWTH',15]])
        assert config.short_weights()['GROWTH']==15
        with pytest.raises(ValueError):config.apply_control_panel([['','SHORT_DISPLAY_TOP_N',1.5]])
    finally:config.apply_control_panel([])


def test_short_candidate_pool_defaults_to_fifty_with_separate_score_floor():
    config.apply_control_panel([])
    assert config.SHORT_DISPLAY_TOP_N == 50
    assert config.MAX_QUANT_SHORTS_TOTAL == 50
    assert config.MIN_SHORT_SCORE == 58.9


def test_short_sheet_reason_is_column_e_and_responds_to_score():
    source = fixture()
    scored = pd.concat([source, score_shorts(source)], axis=1)
    selected = scored.loc[scored["Short Selected"]].sort_values("Short Rank")
    assert not selected.empty
    values = short_sheet_values(scored)
    assert values[2][4] == "Shortlist Reason"
    assert "research candidate only" in values[3][4]
    changed = scored.copy()
    changed.loc[selected.index[0], "Expanded Short Score"] += 1
    assert short_sheet_values(changed)[3][4] != values[3][4]
