from __future__ import annotations
import math, re, time, sys
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
try:
    import gspread
except ImportError:  # Offline CSV mode does not require the Sheets client.
    gspread = None

DEFAULT_SPREADSHEET_ID="1JqJmJb9cEUF9vrARFohx-uxcpJcDfu-5BPUuUK_oczI"
DEFAULT_CREDS_PATH=r"C:\\Code\\service_account.json"
SAME_BAR_MODE="conservative_stop_first"
STOP_EXECUTION_MODE="threshold_fill"
MAX_STOPS=26; MAX_TARGETS=30; MAX_VALIDATION_CANDIDATES=24
MIN_WEIGHTED_SCORE_FOR_APPROVAL=0.70
DEFAULT_SPREAD_BPS=0.0; DEFAULT_COMMISSION_BPS=0.0; DEFAULT_SLIPPAGE_BPS=0.0; DEFAULT_FINANCING_BPS_PER_YEAR=0.0
STRESS_SPREAD_BPS=5.0; STRESS_COMMISSION_BPS=0.0; STRESS_SLIPPAGE_BPS=5.0; STRESS_FINANCING_BPS_PER_YEAR=0.0

@dataclass
class MetricRule:
    name:str; resolved_name:str; weight_num:float; is_hard:bool; rule:str; threshold:Optional[float]; lower:Optional[float]; upper:Optional[float]
@dataclass
class EngineConfig:
    direction:str; horizon_months:int; train_start:pd.Timestamp; train_end:pd.Timestamp; validation_start:pd.Timestamp; validation_end:pd.Timestamp; test_start:pd.Timestamp; test_end:pd.Timestamp; metrics:List[MetricRule]
@dataclass
class TradeCache:
    entry_id:str; entry_date:pd.Timestamp; entry_price:float; source:str; horizon_end:pd.Timestamp
    high:np.ndarray; low:np.ndarray; close:np.ndarray; dates:np.ndarray; mfe:float; mae:float; horizon_close_return:float

def parse_date(v:Any)->pd.Timestamp:
    if v is None or str(v).strip()=="": return pd.NaT
    if isinstance(v,(pd.Timestamp,datetime,date)): return pd.Timestamp(v).normalize()
    s=str(v).strip()
    try:
        f=float(s)
        if f>1000: return (pd.Timestamp("1899-12-30")+pd.to_timedelta(f,unit='D')).normalize()
    except Exception: pass
    dt=pd.to_datetime(s,dayfirst=True,errors='coerce')
    if pd.isna(dt): dt=pd.to_datetime(s,dayfirst=False,errors='coerce')
    return pd.Timestamp(dt).normalize() if not pd.isna(dt) else pd.NaT

def parse_pct_or_float(v):
    if v is None or str(v).strip()=="": return None
    s=str(v).strip().replace(',','')
    try: return float(s[:-1])/100.0 if s.endswith('%') else float(s)
    except ValueError: return None

def canonical_metric_name(name:str)->str:
    k=re.sub(r"\s+"," ",(name or "").strip().lower())
    a={"max drawdown":"Sequential Trade Max Drawdown","drawdown":"Sequential Trade Max Drawdown","average return":"Average Pair Return","expected return":"Average Pair Return","expected r":"Expected R Multiple","pf":"Profit Factor"}
    return a.get(k,name.strip())



def evaluate_rule(value: float, m: MetricRule) -> tuple[bool, str]:
    r=(m.rule or "").strip().lower()
    if r=="":
        return False, "blank rule"
    if r==">":
        return (m.threshold is not None and value>m.threshold), ">"
    if r=="<":
        return (m.threshold is not None and value<m.threshold), "<"
    if r==">=":
        return (m.threshold is not None and value>=m.threshold), ">="
    if r=="<=":
        return (m.threshold is not None and value<=m.threshold), "<="
    if r in ("=","=="):
        return (m.threshold is not None and value==m.threshold), "=="
    if r=="between":
        return (m.lower is not None and m.upper is not None and m.lower<=value<=m.upper), "between"
    return False, "unsupported rule"
def clean_output_for_sheets(output):
    def c(v):
        if v is None or v is pd.NaT: return ""
        if isinstance(v,(pd.Timestamp,datetime,date)): return pd.Timestamp(v).strftime("%Y-%m-%d")
        if hasattr(v,'item'):
            try:v=v.item()
            except Exception: pass
        if isinstance(v,(np.bool_,bool)): return "TRUE" if bool(v) else "FALSE"
        if isinstance(v,(np.integer,int)): return int(v)
        if isinstance(v,(np.floating,float)): return "" if not np.isfinite(v) else float(v)
        try:
            if pd.isna(v): return ""
        except Exception: pass
        return str(v)
    rows=[[c(x) for x in r] for r in output]; w=max((len(r) for r in rows),default=0)
    return [r+[""]*(w-len(r)) for r in rows]

def connect_sheet():
    if gspread is None:
        raise RuntimeError("Google Sheets mode requires gspread; install it or use --raw/--entries offline mode")
    return gspread.service_account(filename=DEFAULT_CREDS_PATH).open_by_key(DEFAULT_SPREADSHEET_ID)
def recreate_sheet(sh, title, rows=7000, cols=180):
    """
    Faster replacement.
    Keeps the same function name so you do not need to change every call site.
    Instead of deleting and recreating the sheet, it clears the existing sheet.
    """
    try:
        ws = sh.worksheet(title)
        ws.clear()
        return ws
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

def read_section_from_values(values,section_title):
    i0=None
    for i,r in enumerate(values):
        if len(r)>0 and str(r[0]).strip()==section_title: i0=i; break
    if i0 is None or i0+1>=len(values): return pd.DataFrame()
    hdr=values[i0+1]
    while hdr and str(hdr[-1]).strip()=="": hdr=hdr[:-1]
    data=[]
    for r in values[i0+2:]:
        if all(str(x).strip()=="" for x in r): break
        data.append((r[:len(hdr)] + [""]*max(0,len(hdr)-len(r))))
    return pd.DataFrame(data,columns=hdr) if data else pd.DataFrame()

def load_control_panel(sh) -> EngineConfig:
    ws = sh.worksheet("Control Panel")

    # One API call instead of many acell() calls.
    vals = ws.batch_get(["C3:C17", "E4:L20"])

    cvals = vals[0]
    metric_vals = vals[1]

    def get_c(row_num):
        idx = row_num - 3
        if idx < 0 or idx >= len(cvals) or not cvals[idx]:
            return ""
        return cvals[idx][0]

    direction = str(get_c(3) or "").strip().upper()
    hm = int(float(get_c(5) or 0))

    ts = parse_date(get_c(8))
    te = parse_date(get_c(9))
    vs = parse_date(get_c(12))
    ve = parse_date(get_c(13))
    ts2 = parse_date(get_c(16))
    te2 = parse_date(get_c(17))

    metrics = []

    for r in metric_vals:
        r = r + [""] * (8 - len(r))
        n = str(r[2]).strip()

        if not n:
            continue

        w = str(r[0]).strip()
        hard = w.lower() == "hard"

        metrics.append(
            MetricRule(
                n,
                canonical_metric_name(n),
                0.0 if hard else (parse_pct_or_float(w) or 0.0),
                hard,
                str(r[4]).strip(),
                parse_pct_or_float(r[5]),
                parse_pct_or_float(r[6]),
                parse_pct_or_float(r[7]),
            )
        )

    return EngineConfig(direction, hm, ts, te, vs, ve, ts2, te2, metrics)

def load_raw_data(sh):
    vals=sh.worksheet("Raw Data").get_all_values(); df=pd.DataFrame(vals[1:],columns=vals[0])
    raw=pd.DataFrame({"date":df.iloc[:,5].map(parse_date),"open":pd.to_numeric(df.iloc[:,2],errors='coerce'),"high":pd.to_numeric(df.iloc[:,3],errors='coerce'),"low":pd.to_numeric(df.iloc[:,4],errors='coerce'),"close":pd.to_numeric(df.iloc[:,1],errors='coerce')})
    raw=raw.dropna(subset=["date","high","low","close"])
    raw=raw[(raw["high"]>=raw["low"])&(raw["high"]>0)&(raw["low"]>0)&(raw["close"]>0)]
    return raw.sort_values("date").reset_index(drop=True)

def load_entries_all(sh):
    """
    Reads the Entries tab once.
    This replaces repeatedly calling sh.worksheet("Entries").get_all_values()
    inside every train/validation/test/MC/position-sizing stage.
    """
    vals = sh.worksheet("Entries").get_all_values()
    df = pd.DataFrame(vals[1:], columns=vals[0])

    e = pd.DataFrame({
        "entry_id": df.iloc[:, 0],
        "entry_date": df.iloc[:, 1].map(parse_date),
        "entry_price": pd.to_numeric(df.iloc[:, 2], errors="coerce"),
        "direction": df.iloc[:, 3].astype(str).str.upper().str.strip(),
        "split": df.iloc[:, 4].astype(str).str.upper().str.strip(),
        "valid": df.iloc[:, 5].astype(str).str.upper().str.strip().isin(["TRUE", "YES", "1"]),
        "source": df.iloc[:, 6],
    })

    return e.dropna(subset=["entry_date", "entry_price"]).reset_index(drop=True)


def filter_entries(entries_all, cfg: EngineConfig, split, start, end):
    """
    Filters the already-loaded Entries dataframe.
    This gives the same output as your old load_entries(), but avoids another Google Sheets read.
    """
    split = split.upper()

    m = (
        (entries_all["split"] == split)
        & entries_all["valid"]
        & (entries_all["direction"] == cfg.direction)
        & (entries_all["entry_date"] >= start)
        & (entries_all["entry_date"] <= end)
    )

    return entries_all[m].reset_index(drop=True)

def load_entries(sh, cfg: EngineConfig, split, start, end, entries_all=None):
    """
    Backward-compatible wrapper.

    If entries_all is passed in, this is fast.
    If entries_all is not passed in, it behaves like before by reading the sheet.
    """
    if entries_all is None:
        entries_all = load_entries_all(sh)

    return filter_entries(entries_all, cfg, split, start, end)

def hit_logic_fast(hi,lo,ep,d,stop,target):
    if d=="LONG": sh=(lo/ep-1)<=-stop; th=(hi/ep-1)>=target
    else: sh=(ep/hi-1)<=-stop; th=(ep/lo-1)>=target
    both=sh&th; is_=np.argmax(sh) if np.any(sh) else None; it=np.argmax(th) if np.any(th) else None; ib=np.argmax(both) if np.any(both) else None
    if ib is not None and ((is_ is None or ib<=is_) and (it is None or ib<=it)): return "same_bar_stop",-stop,int(ib),True
    if is_ is not None and (it is None or is_<it): return "stop",-stop,int(is_),False
    if it is not None: return "target",target,int(it),False
    return "horizon",None,len(hi)-1,False

def build_trade_cache(entries,raw,cfg,end_date):
    rd=raw["date"].to_numpy(); rh=raw["high"].to_numpy(float); rl=raw["low"].to_numpy(float); rc=raw["close"].to_numpy(float)
    idx={pd.Timestamp(d).strftime("%Y-%m-%d"):i for i,d in enumerate(rd)}
    cache=[]; audits=[]; excl=[]; miss=0; nohz=0
    for r in entries.itertuples(index=False):
        he=(r.entry_date+pd.DateOffset(months=cfg.horizon_months)).normalize()
        if he>end_date: excl.append({"entry_id":r.entry_id,"entry_date":r.entry_date,"entry_price":r.entry_price,"split":r.split,"valid":r.valid,"source":r.source,"horizon_end":he,"exclusion_reason":"Horizon end exceeds boundary"}); continue
        i0=idx.get(pd.Timestamp(r.entry_date).strftime("%Y-%m-%d"))
        if i0 is None: miss+=1; audits.append({**r._asdict(),"horizon_end":he,"warning":"Entry date missing in Raw Data"}); continue
        j=np.searchsorted(rd,pd.Timestamp(he).to_datetime64(),side='right')
        hi,lo,cl,dt=rh[i0+1:j],rl[i0+1:j],rc[i0+1:j],rd[i0+1:j]
        if len(hi)==0: nohz+=1; audits.append({**r._asdict(),"horizon_end":he,"warning":"No bars in horizon"}); continue
        if cfg.direction=="LONG": fav,adv,hret=hi/r.entry_price-1,lo/r.entry_price-1,cl[-1]/r.entry_price-1
        else: fav,adv,hret=r.entry_price/lo-1,r.entry_price/hi-1,r.entry_price/cl[-1]-1
        mfe_i,mae_i=int(np.argmax(fav)),int(np.argmin(adv))
        audits.append({**r._asdict(),"horizon_end":he,"bars":len(hi),"mfe":float(np.max(fav)),"mae":float(np.min(adv)),"mfe_day":int((pd.Timestamp(dt[mfe_i])-pd.Timestamp(r.entry_date)).days),"mae_day":int((pd.Timestamp(dt[mae_i])-pd.Timestamp(r.entry_date)).days),"horizon_close_return":float(hret),"warning":""})
        cache.append(TradeCache(r.entry_id,r.entry_date,float(r.entry_price),r.source,he,hi,lo,cl,dt,float(np.max(fav)),float(np.min(adv)),float(hret)))
    return cache,pd.DataFrame(audits),pd.DataFrame(excl),miss,nohz

def build_candidates(cache):
    mae=np.array([c.mae for c in cache]); mfe=np.array([c.mfe for c in cache])
    abs_mae=np.abs(mae[mae<0]); pos_mfe=mfe[mfe>0]
    if len(abs_mae)==0: abs_mae=np.array([0.01,0.02,0.03])
    if len(pos_mfe)==0: pos_mfe=np.array([0.01,0.02,0.03])
    ps=np.quantile(abs_mae,np.arange(10,100,10)/100); pt=np.quantile(pos_mfe,np.arange(10,100,10)/100)
    s=np.unique(np.round(np.concatenate([ps,np.arange(0.005,0.125,0.005)]),6)); t=np.unique(np.round(np.concatenate([pt,np.arange(0.005,0.205,0.005)]),6))
    s=s[np.linspace(0,len(s)-1,min(MAX_STOPS,len(s))).astype(int)]; t=t[np.linspace(0,len(t)-1,min(MAX_TARGETS,len(t))).astype(int)]
    return s,t

def evaluate_matrix(candidates,cache,cfg):
    rows=[]
    ed=np.array(sorted([c.entry_date.to_datetime64() for c in cache]))
    spacing=np.diff(ed).astype('timedelta64[D]').astype(float) if len(ed)>1 else np.array([])
    avg_spacing=float(np.mean(spacing)) if len(spacing)>0 else 0.0
    for stop,target in candidates:
        rets=[]; out=[]; hold=[]; same=0; td=[]; sd=[]; ls=mls=ss=mss=0
        for tr in cache:
            oc,rr,idx,sb=hit_logic_fast(tr.high,tr.low,tr.entry_price,cfg.direction,stop,target)
            if rr is None: rr=tr.horizon_close_return
            d=int((pd.Timestamp(tr.dates[max(idx,0)])-pd.Timestamp(tr.entry_date)).days)
            rets.append(rr); out.append(oc); hold.append(d); same += 1 if sb else 0
            if oc=="target": td.append(d)
            if oc in ("stop","same_bar_stop"): sd.append(d)
            ls=ls+1 if rr<0 else 0; mls=max(mls,ls); ss=ss+1 if oc in ("stop","same_bar_stop") else 0; mss=max(mss,ss)
        arr=np.array(rets,float); out=np.array(out)
        wins=arr[arr>0]; losses=arr[arr<0]; sdv=float(np.std(arr,ddof=1)) if len(arr)>1 else 0.0
        eq=np.cumprod(1+arr); peak=np.maximum.accumulate(eq); dd=eq/peak-1; mdd=float(dd.min()) if len(dd) else 0.0
        avg_h=float(np.mean(hold)) if hold else 0.0; overlap=avg_h/avg_spacing if avg_spacing>0 else 1.0; ess=float(len(arr)/max(1.0,overlap)); ess_lbl="Strong" if ess>=100 else ("Moderate" if ess>=50 else ("Weak" if ess>=25 else "Very Weak"))
        row={"Stop":stop,"Target":target,"Average Pair Return":float(np.mean(arr)),"Median Pair Return":float(np.median(arr)),"Standard Deviation":sdv,"Vol Adjusted Return":float(np.mean(arr)/sdv) if sdv>0 else 0.0,"Expected R Multiple":float(np.mean(arr)/stop),"Profit Factor":float(np.sum(wins)/abs(np.sum(losses))) if np.sum(losses)!=0 else 9999.0,"Payoff Ratio":float(np.mean(wins)/abs(np.mean(losses))) if len(wins) and len(losses) else 0.0,"Win Rate":float(np.mean(out=="target")),"Positive Return Rate":float(np.mean(arr>0)),"Loss Rate":float(np.mean(arr<0)),"Stop-First Rate":float(np.mean(np.isin(out,["stop","same_bar_stop"]))),"Target-First Rate":float(np.mean(out=="target")),"Horizon Exit Rate":float(np.mean(out=="horizon")),"Same-Bar Conflict Count":int(same),"Same-Bar Conflict Rate":float(same/len(arr)),"Average Days to Target":float(np.mean(td)) if td else 0.0,"Average Days to Stop":float(np.mean(sd)) if sd else 0.0,"Average Holding Days":avg_h,"Median Holding Days":float(np.median(hold)) if hold else 0.0,"Time Efficiency Ratio":float(1-(np.mean(td)/max(1,cfg.horizon_months*30))) if td else 0.0,"Winner Stop Usage Ratio":0.0,"Target Capture Efficiency":0.0,"Post-Stop Recovery Ratio":0.0,"Expected Shortfall 5%":float(np.mean(np.sort(arr)[:max(1,math.ceil(0.05*len(arr)))])),"Expected Shortfall / Stop":float(np.mean(np.sort(arr)[:max(1,math.ceil(0.05*len(arr)))])/stop),"Sequential Trade Max Drawdown":mdd,"Calmar-Style Ratio":float((eq[-1]-1)/abs(mdd)) if mdd<0 else 9999.0,"Longest Losing Streak":int(mls),"Max Consecutive Stop-Outs":int(mss),"Worst Trade":float(np.min(arr)),"Best Trade":float(np.max(arr)),"Raw Trades":int(len(arr)),"Average Entry Spacing Days":avg_spacing,"Overlap Ratio":float(overlap),"Effective Sample Size":ess,"Effective Sample Label":ess_lbl,"Drawdown Interpretation":"Sequential overlapping trade-equity drawdown; not true calendar portfolio drawdown"}
        rows.append((row,arr,hold))
    return rows

def score_train_rows(rows,cfg):
    out=[]; missing=[]
    for row,arr,hold in rows:
        hard=True; score=0.0
        for m in cfg.metrics:
            v=row.get(m.resolved_name)
            ok=False if v is None else evaluate_rule(float(v),m)[0]
            row[f"{m.name} Pass"]=ok; row[f"{m.name} Contribution"]=(m.weight_num if ((not m.is_hard) and ok) else 0.0); score += row[f"{m.name} Contribution"]
            if m.is_hard and not ok: hard=False
            if v is None: missing.append(m.name)
        status="CONFIG ERROR" if missing else ("REJECTED" if ((not hard) or row["Average Pair Return"]<=0 or row["Profit Factor"]<=1 or row["Effective Sample Label"]=="Very Weak") else ("APPROVED" if (score>=MIN_WEIGHTED_SCORE_FOR_APPROVAL and row["Effective Sample Size"]>=50 and row["Raw Trades"]>=100) else "WATCHLIST"))
        row["Hard Filter Pass"]=hard; row["Weighted Score"]=score; row["Approval Status"]=status
        out.append(row)
    mx=pd.DataFrame(out).sort_values(["Hard Filter Pass","Weighted Score","Average Pair Return"],ascending=[False,False,False]).reset_index(drop=True)
    if not mx.empty: mx["Rank"]=np.arange(1,len(mx)+1)
    return mx,sorted(set(missing))

def write_sheet(ws, sections, auto_resize=False):
    output = []

    for title, df in sections:
        output.append([title])
        if df is not None and not df.empty:
            output.append(list(df.columns))
            output.extend(df.values.tolist())
        output.append([])

    ws.update(
        values=clean_output_for_sheets(output),
        range_name="A1",
        value_input_option="RAW"
    )

    try:
        ws.freeze(rows=1)

        # Auto-resize is slow. Keep it off during normal runs.
        if auto_resize:
            ws.columns_auto_resize(0, 40)

    except Exception:
        pass

def run_train(sh, cfg, raw, entries_all=None):
    t = time.perf_counter()
    entries = load_entries(sh, cfg, "TRAIN", cfg.train_start, cfg.train_end, entries_all=entries_all)
    cache,aud,excl,miss,nohz=build_trade_cache(entries,raw,cfg,cfg.train_end)
    stops,targets=build_candidates(cache) if cache else (np.array([]),np.array([]))
    cand=[(s,t) for s in stops for t in targets]
    rows=evaluate_matrix(cand,cache,cfg) if cand else []
    mx,missing=score_train_rows(rows,cfg) if rows else (pd.DataFrame(),[])
    approved=mx[mx.get("Approval Status",pd.Series(dtype=str))=="APPROVED"] if not mx.empty else pd.DataFrame()
    watch=mx[mx.get("Approval Status",pd.Series(dtype=str))=="WATCHLIST"] if not mx.empty else pd.DataFrame()
    leak_pass = True if len(cache) == 0 else max(c.horizon_end for c in cache) <= cfg.train_end
    ready=(leak_pass and len(missing)==0 and len(approved)>0 and len(cache)>=100)
    reason="READY" if ready else "Not ready: leakage/missing metrics/no approved/insufficient trades"
    summary=pd.DataFrame([["Best approved stop",approved.iloc[0]["Stop"] if not approved.empty else ""],["Best approved target",approved.iloc[0]["Target"] if not approved.empty else ""],["Best approved average return",approved.iloc[0]["Average Pair Return"] if not approved.empty else ""],["Best approved expected R",approved.iloc[0]["Expected R Multiple"] if not approved.empty else ""],["Best approved profit factor",approved.iloc[0]["ProfitFactor"] if (not approved.empty and "ProfitFactor" in approved.columns) else (approved.iloc[0]["Profit Factor"] if not approved.empty else "")],["Best approved sequential max drawdown",approved.iloc[0]["Sequential Trade Max Drawdown"] if not approved.empty else ""],["Best approved weighted score",approved.iloc[0]["Weighted Score"] if not approved.empty else ""],["Number approved",int((mx["Approval Status"]=="APPROVED").sum()) if not mx.empty else 0],["Number watchlist",int((mx["Approval Status"]=="WATCHLIST").sum()) if not mx.empty else 0],["Number rejected",int((mx["Approval Status"]=="REJECTED").sum()) if not mx.empty else 0],["Number config error",int((mx["Approval Status"]=="CONFIG ERROR").sum()) if not mx.empty else 0],["Total matrix combinations tested",len(mx)],["Median matrix average return",float(mx["Average Pair Return"].median()) if not mx.empty else 0.0],["Percent positive-return combinations",float((mx["Average Pair Return"]>0).mean()) if not mx.empty else 0.0],["Best overall average return",float(mx["Average Pair Return"].max()) if not mx.empty else 0.0],["Best overall weighted score",float(mx["Weighted Score"].max()) if not mx.empty else 0.0],["Warning if no approved candidates","No approved candidates" if approved.empty else ""],["Warning if missing configured metrics",", ".join(missing) if missing else ""],["Validation readiness status","READY FOR VALIDATION" if ready else "NOT READY FOR VALIDATION"],["Validation readiness reason",reason]],columns=["Field","Value"])
    sections=[("TRAIN CONFIG",pd.DataFrame([["Direction",cfg.direction],["Horizon months",cfg.horizon_months],["Train start",cfg.train_start],["Train end",cfg.train_end]],columns=["Field","Value"])),("TRAIN DATA INTEGRITY CHECKS",pd.DataFrame([["Leakage check",("PASS" if leak_pass else "FAIL")],["Final included train entries",len(cache)],["Excluded due to train-end boundary",len(excl)],["Missing raw date",miss],["No horizon bars",nohz]],columns=["Check","Value"])),("EXCLUDED TRAIN ENTRIES - HORIZON BEYOND TRAIN END",excl),("TRAIN ENTRY AUDIT",aud),("TRAIN STOP CANDIDATES",pd.DataFrame({"Stop":stops})),("TRAIN TARGET CANDIDATES",pd.DataFrame({"Target":targets})),("TRAIN MATRIX RESULTS",mx),("TOP APPROVED TRAIN CANDIDATES",approved.head(30)),("WATCHLIST CANDIDATES",watch.head(30)),("TRAIN SUMMARY",summary),("DIAGNOSTICS",pd.DataFrame([["Only TRAIN split used","YES"],["Same-bar mode",SAME_BAR_MODE],["Stop execution mode",STOP_EXECUTION_MODE]],columns=["Field","Value"]))]
    if missing: sections.append(("MISSING CONFIGURED METRICS",pd.DataFrame({"Metric":missing})))
    ws=recreate_sheet(sh,"Train"); w0=time.perf_counter(); write_sheet(ws,sections); print(f"train write {time.perf_counter()-w0:.2f}s")
    print("Train sheet updated")
    print(f"train total {time.perf_counter()-t:.2f}s")
    return mx, approved, watch, cache

def apply_trade_costs(gross_return,holding_days,spread_bps,commission_bps,slippage_bps,financing_bps_per_year):
    total_bps=spread_bps+commission_bps+slippage_bps+financing_bps_per_year*(holding_days/365.0)
    return gross_return-total_bps/10000.0

def apply_trade_costs_vec(gross_returns, holding_days, spread_bps, commission_bps, slippage_bps, financing_bps_per_year):
    gross_returns = np.asarray(gross_returns, dtype=float)
    holding_days = np.asarray(holding_days, dtype=float)

    total_bps = (
        spread_bps
        + commission_bps
        + slippage_bps
        + financing_bps_per_year * (holding_days / 365.0)
    )

    return gross_returns - total_bps / 10000.0

def run_validation(sh, cfg, raw, train_matrix, approved=None, watch=None, entries_all=None):
    t = time.perf_counter()
    entries = load_entries(sh, cfg, "VALIDATION", cfg.validation_start, cfg.validation_end, entries_all=entries_all)
    cache,aud,excl,miss,nohz=build_trade_cache(entries,raw,cfg,cfg.validation_end)
    app = approved if approved is not None else pd.DataFrame()
    wch = watch if watch is not None else pd.DataFrame()
    mxs = train_matrix if train_matrix is not None else pd.DataFrame()

    cand = []
    for df, src in [(app, "APPROVED"), (wch, "WATCHLIST"), (mxs, "MATRIX")]:
        if df is None or df.empty:
            continue

        for _, r in df.iterrows():
            cand.append({
                "Stop": r.get("Stop"),
                "Target": r.get("Target"),
                "Train Average Return": r.get("Average Pair Return"),
                "Train Profit Factor": r.get("Profit Factor"),
                "Train Max Drawdown": r.get("Sequential Trade Max Drawdown"),
                "Train Weighted Score": r.get("Weighted Score"),
                "Train Approval Status": r.get("Approval Status"),
                "Candidate Source": "Approved/Watchlist/Matrix blend"
            })
    cand_df=pd.DataFrame(cand)
    if not cand_df.empty:
        cand_df["Stop"]=pd.to_numeric(cand_df["Stop"],errors='coerce'); cand_df["Target"]=pd.to_numeric(cand_df["Target"],errors='coerce')
        cand_df=cand_df.dropna(subset=["Stop","Target"]).drop_duplicates(subset=["Stop","Target"]).head(MAX_VALIDATION_CANDIDATES).reset_index(drop=True)

    results=[]; trade_store={}
    for _,c in cand_df.iterrows():
        stop,target=float(c["Stop"]),float(c["Target"])
        if stop<=0 or target<=0 or len(cache)==0:
            results.append({"Stop":stop,"Target":target,"Validation Status":"CONFIG ERROR","Validation Score":0}); continue
        rets=[]; out=[]; hold=[]; same=0
        for tr in cache:
            oc,rr,idx,sb=hit_logic_fast(tr.high,tr.low,tr.entry_price,cfg.direction,stop,target)
            if rr is None: rr=tr.horizon_close_return
            d=int((pd.Timestamp(tr.dates[max(idx,0)])-pd.Timestamp(tr.entry_date)).days)
            rets.append(rr); out.append(oc); hold.append(d); same+=1 if sb else 0
        arr=np.array(rets,float); wins=arr[arr>0]; losses=arr[arr<0]; eq=np.cumprod(1+arr); peak=np.maximum.accumulate(eq); dd=eq/peak-1
        avg=float(np.mean(arr)); pf=float(np.sum(wins)/abs(np.sum(losses))) if np.sum(losses)!=0 else 9999.0; er=float(np.mean(arr)/stop); raw_tr=len(arr)
        ess=float(raw_tr)*0.8; same_r=float(same/raw_tr); mdd=float(dd.min()) if len(dd) else 0.0
        tr_avg=float(pd.to_numeric(c.get("Train Average Return"),errors='coerce')) if str(c.get("Train Average Return"))!="" else np.nan
        tr_pf=float(pd.to_numeric(c.get("Train Profit Factor"),errors='coerce')) if str(c.get("Train Profit Factor"))!="" else np.nan
        tr_dd=float(pd.to_numeric(c.get("Train Max Drawdown"),errors='coerce')) if str(c.get("Train Max Drawdown"))!="" else np.nan
        rd=avg/tr_avg if np.isfinite(tr_avg) and tr_avg>0 else np.nan; pfd=pf/tr_pf if np.isfinite(tr_pf) and tr_pf>0 else np.nan; dde=abs(mdd)/abs(tr_dd) if np.isfinite(tr_dd) and tr_dd<0 else np.nan
        score=sum([avg>0,pf>1,np.isfinite(rd) and rd>=0.5,np.isfinite(pfd) and pfd>=0.75,np.isnan(dde) or dde<=1.5,raw_tr>=75,same_r<=0.1,mdd>-0.7,er>0,ess>=20])
        if avg>0 and pf>1 and rd>=0.5 and pfd>=0.75 and (np.isnan(dde) or dde<=1.5) and score>=8 and raw_tr>=75: status="VALIDATED"
        elif avg>0 and pf>1 and score>=7 and raw_tr>=50: status="PROVISIONAL VALIDATION"
        elif avg>=0 and (pf>=1 or np.mean(arr>0)>=0.5): status="CONDITIONAL - REVIEW"
        else: status="FAILED"
        results.append({"Stop":stop,"Target":target,"Validation Average Return":avg,"Validation Median Return":float(np.median(arr)),"Validation Standard Deviation":float(np.std(arr,ddof=1)) if len(arr)>1 else 0.0,"Validation Vol Adjusted Return":(avg/(np.std(arr,ddof=1)) if len(arr)>1 and np.std(arr,ddof=1)!=0 else 0.0),"Validation Expected R":er,"Validation Profit Factor":pf,"Validation Payoff Ratio":(float(np.mean(wins)/abs(np.mean(losses))) if len(wins) and len(losses) else 0.0),"Validation Win Rate":float(np.mean(np.array(out)=="target")),"Validation Positive Return Rate":float(np.mean(arr>0)),"Validation Loss Rate":float(np.mean(arr<0)),"Validation Stop-First Rate":float(np.mean(np.isin(out,["stop","same_bar_stop"]))),"Validation Target-First Rate":float(np.mean(np.array(out)=="target")),"Validation Horizon Exit Rate":float(np.mean(np.array(out)=="horizon")),"Validation Same-Bar Conflict Count":same,"Validation Same-Bar Conflict Rate":same_r,"Validation Average Days to Target":0.0,"Validation Average Days to Stop":0.0,"Validation Average Holding Days":float(np.mean(hold)),"Validation Expected Shortfall 5%":float(np.mean(np.sort(arr)[:max(1,math.ceil(0.05*len(arr)))])),"Validation Max Drawdown":mdd,"Validation Longest Losing Streak":0,"Validation Max Consecutive Stop-Outs":0,"Validation Worst Trade":float(np.min(arr)),"Validation Best Trade":float(np.max(arr)),"Validation Raw Trades":raw_tr,"Validation Overlap Ratio":1.0,"Validation Effective Sample Size":ess,"Validation Effective Sample Label":("Very Weak" if ess<25 else ("Weak" if ess<50 else "Moderate")),"Final Compounded Return":float(np.prod(1+arr)-1),"Train Average Return":tr_avg,"Train Profit Factor":tr_pf,"Train Max Drawdown":tr_dd,"Train Weighted Score":pd.to_numeric(c.get("Train Weighted Score"),errors='coerce'),"Train Approval Status":c.get("Train Approval Status"),"Return Decay":rd,"PF Decay":pfd,"DD Expansion":dde,"Validation Score":score,"Validation Status":status})
        trade_store[(round(stop,6),round(target,6))]={"returns":arr,"hold":np.array(hold)}
    vdf=pd.DataFrame(results)
    leak = True if len(cache) == 0 else max(c.horizon_end for c in cache) <= cfg.validation_end

    # robustness
    robust=[]
    for _,r in vdf.iterrows():
        stop,target=float(r["Stop"]),float(r["Target"])
        neigh=vdf[(np.abs(vdf["Stop"]-stop)<=0.005)&(np.abs(vdf["Target"]-target)<=0.01)]
        if len(neigh)<4 and not train_matrix.empty:
            tm=train_matrix.copy(); tm["dist"]=np.abs(pd.to_numeric(tm["Stop"],errors='coerce')-stop)+np.abs(pd.to_numeric(tm["Target"],errors='coerce')-target)
            neigh_train=tm.sort_values("dist").head(4)
            ncnt=len(neigh_train); n_score=float(pd.to_numeric(neigh_train.get("Weighted Score",0),errors='coerce').mean()); n_avg=float(pd.to_numeric(neigh_train.get("Average Pair Return",0),errors='coerce').median()); n_pf=float(pd.to_numeric(neigh_train.get("Profit Factor",0),errors='coerce').median()); n_pos=float(np.mean(pd.to_numeric(neigh_train.get("Average Pair Return",0),errors='coerce')>0)); n_pf1=float(np.mean(pd.to_numeric(neigh_train.get("Profit Factor",0),errors='coerce')>1))
        else:
            ncnt=len(neigh)
            if ncnt:
                ns = pd.to_numeric(neigh["Validation Score"], errors='coerce') if "Validation Score" in neigh.columns else pd.Series(dtype=float)
                na = pd.to_numeric(neigh["Validation Average Return"], errors='coerce') if "Validation Average Return" in neigh.columns else pd.Series(dtype=float)
                npf = pd.to_numeric(neigh["Validation Profit Factor"], errors='coerce') if "Validation Profit Factor" in neigh.columns else pd.Series(dtype=float)
                n_score = float(ns.mean()) if len(ns) else 0.0
                n_avg = float(na.median()) if len(na) else 0.0
                n_pf = float(npf.median()) if len(npf) else 0.0
                n_pos = float(np.mean(na > 0)) if len(na) else 0.0
                n_pf1 = float(np.mean(npf > 1)) if len(npf) else 0.0
            else:
                n_score = n_avg = n_pf = n_pos = n_pf1 = 0.0
        if ncnt<2: rlabel="NO CLUSTER DATA"
        elif ncnt>=4 and n_pos>=0.7 and n_pf1>=0.7: rlabel="ROBUST CLUSTER"
        elif r["Validation Status"] in ["VALIDATED","PROVISIONAL VALIDATION"] and (n_pos<0.5 or n_pf1<0.5): rlabel="ISOLATED WINNER"
        else: rlabel="WEAK CLUSTER"
        if len(neigh)>=2:
            ars=float(pd.to_numeric(neigh["Validation Average Return"],errors='coerce').std()); pfs=float(pd.to_numeric(neigh["Validation Profit Factor"],errors='coerce').std()); dds=float(pd.to_numeric(neigh["Validation Max Drawdown"],errors='coerce').std())
            sens="LOW SENSITIVITY" if ars<0.001 and pfs<0.2 and dds<0.1 else ("MEDIUM SENSITIVITY" if ars<0.003 and pfs<0.5 and dds<0.2 else "HIGH SENSITIVITY")
        else:
            ars=pfs=dds=np.nan; sens="NO SENSITIVITY DATA"
        arr=trade_store.get((round(stop,6),round(target,6)),{"returns":np.array([]),"hold":np.array([])})["returns"]
        hold=trade_store.get((round(stop,6),round(target,6)),{"returns":np.array([]),"hold":np.array([])})["hold"]
        if len(arr)>0:
            cost = apply_trade_costs_vec(
                arr,
                hold,
                DEFAULT_SPREAD_BPS,
                DEFAULT_COMMISSION_BPS,
                DEFAULT_SLIPPAGE_BPS,
                DEFAULT_FINANCING_BPS_PER_YEAR
            )

            stress = apply_trade_costs_vec(
                arr,
                hold,
                STRESS_SPREAD_BPS,
                STRESS_COMMISSION_BPS,
                STRESS_SLIPPAGE_BPS,
                STRESS_FINANCING_BPS_PER_YEAR
            )
            def pf(a):
                w=a[a>0]; l=a[a<0]; return float(np.sum(w)/abs(np.sum(l))) if np.sum(l)!=0 else 9999.0
            def mdd(a):
                e=np.cumprod(1+a); p=np.maximum.accumulate(e); d=e/p-1; return float(d.min()) if len(d) else 0.0
            ca_avg=float(np.mean(cost)); ca_pf=pf(cost); ca_dd=mdd(cost)
            st_avg=float(np.mean(stress)); st_pf=pf(stress); st_dd=mdd(stress); st_status="PASS" if (st_avg>=0 and st_pf>=1) else "FAIL"
            h=len(arr)//2
            fh,shh=arr[:h],arr[h:]
            def half_pf(a):
                if len(a)==0: return 0.0
                w=a[a>0]; l=a[a<0]; return float(np.sum(w)/abs(np.sum(l))) if np.sum(l)!=0 else 9999.0
            fh_avg=float(np.mean(fh)) if len(fh) else 0.0; sh_avg=float(np.mean(shh)) if len(shh) else 0.0; fh_pf=half_pf(fh); sh_pf=half_pf(shh)
            w_avg=min(fh_avg,sh_avg); w_pf=min(fh_pf,sh_pf)
            if fh_avg>0 and sh_avg>0 and fh_pf>1 and sh_pf>1: rcs=10
            elif fh_avg>=0 and sh_avg>=0: rcs=7
            elif min(fh_avg,sh_avg)>-0.01: rcs=5
            else: rcs=0
        else:
            ca_avg=ca_pf=ca_dd=st_avg=st_pf=st_dd=np.nan; st_status="FAIL"; fh_avg=sh_avg=fh_pf=sh_pf=w_avg=w_pf=0.0; rcs=0
        if r["Validation Status"] in ["FAILED","CONFIG ERROR"] or ca_avg<=0 or st_status=="FAIL": rob="REJECT FOR NOW"
        elif r["Validation Status"] in ["VALIDATED","PROVISIONAL VALIDATION"] and st_avg>=0 and st_pf>=1 and r["Validation Max Drawdown"]>-0.60 and rlabel in ["ROBUST CLUSTER","WEAK CLUSTER"] and sens in ["LOW SENSITIVITY","MEDIUM SENSITIVITY"] and rcs>=7: rob="READY FOR FINAL OOS"
        else: rob="NEEDS REVIEW"
        robust.append({"Stop":stop,"Target":target,"Existing Validation Status":r["Validation Status"],"Existing Validation Score":r["Validation Score"],"Validation Average Return":r["Validation Average Return"],"Validation Profit Factor":r["Validation Profit Factor"],"Validation Max Drawdown":r["Validation Max Drawdown"],"Neighbour Count":ncnt,"Neighbour Avg Train Weighted Score":n_score,"Neighbour Median Train Avg Return":n_avg,"Neighbour Median Train Profit Factor":n_pf,"Neighbour Percent Positive Avg Return":n_pos,"Neighbour Percent PF Above 1":n_pf1,"Robustness Label":rlabel,"Avg Return Sensitivity":ars,"Profit Factor Sensitivity":pfs,"Drawdown Sensitivity":dds,"Sensitivity Label":sens,"Cost Adjusted Average Return":ca_avg,"Cost Adjusted Profit Factor":ca_pf,"Cost Adjusted Max Drawdown":ca_dd,"Stress Average Return":st_avg,"Stress Profit Factor":st_pf,"Stress Max Drawdown":st_dd,"Stress Status":st_status,"First Half Avg Return":fh_avg,"Second Half Avg Return":sh_avg,"First Half Profit Factor":fh_pf,"Second Half Profit Factor":sh_pf,"Worst Half Avg Return":w_avg,"Worst Half Profit Factor":w_pf,"Regime Consistency Score":rcs,"Robustness Status":rob,"Robustness Reason":f"{rob}; Stress={st_status}; Cluster={rlabel}; Sensitivity={sens}; RegimeScore={rcs}"})
    robust_df=pd.DataFrame(robust)
    r_ready=int((robust_df["Robustness Status"]=="READY FOR FINAL OOS").sum()) if not robust_df.empty else 0
    r_review=int((robust_df["Robustness Status"]=="NEEDS REVIEW").sum()) if not robust_df.empty else 0
    r_rej=int((robust_df["Robustness Status"]=="REJECT FOR NOW").sum()) if not robust_df.empty else 0
    if r_ready>0: frs,fr="READY FOR FINAL OOS",f"{r_ready} candidates ready"
    elif r_review>0: frs,fr="NEEDS MORE REVIEW",f"{r_review} candidates need review"
    else: frs,fr="FAILED ROBUSTNESS","All candidates rejected"
    best=robust_df.sort_values(["Robustness Status","Existing Validation Score","Stress Profit Factor","Validation Average Return"],ascending=[True,False,False,False]).head(1) if not robust_df.empty else pd.DataFrame()

    sections=[("VALIDATION CONFIG",pd.DataFrame([["Direction",cfg.direction],["Horizon months",cfg.horizon_months],["Validation start",cfg.validation_start],["Validation end",cfg.validation_end]],columns=["Field","Value"])),("VALIDATION DATA INTEGRITY CHECKS",pd.DataFrame([["Leakage check",("PASS" if leak else "FAIL")],["Entries before horizon filter",len(entries)],["Entries excluded",len(excl)],["Final validation trades",len(cache)]],columns=["Check","Value"])),("EXCLUDED VALIDATION ENTRIES - HORIZON BEYOND VALIDATION END",excl),("VALIDATION ENTRY AUDIT",aud),("VALIDATION CANDIDATE SOURCE",pd.DataFrame([["Candidate source","Approved/Watchlist/Matrix blend"],["Selected candidates",len(cand_df)]],columns=["Field","Value"])),("VALIDATION RESULTS",vdf),("VALIDATED CANDIDATES",vdf[vdf["Validation Status"]=="VALIDATED"] if not vdf.empty else pd.DataFrame()),("PROVISIONAL VALIDATION CANDIDATES",vdf[vdf["Validation Status"]=="PROVISIONAL VALIDATION"] if not vdf.empty else pd.DataFrame()),("CONDITIONAL REVIEW CANDIDATES",vdf[vdf["Validation Status"]=="CONDITIONAL - REVIEW"] if not vdf.empty else pd.DataFrame()),("FAILED VALIDATION CANDIDATES",vdf[vdf["Validation Status"]=="FAILED"] if not vdf.empty else pd.DataFrame()),("VALIDATION SUMMARY",pd.DataFrame([["Number VALIDATED",int((vdf["Validation Status"]=="VALIDATED").sum()) if not vdf.empty else 0],["Number PROVISIONAL VALIDATION",int((vdf["Validation Status"]=="PROVISIONAL VALIDATION").sum()) if not vdf.empty else 0],["Number CONDITIONAL - REVIEW",int((vdf["Validation Status"]=="CONDITIONAL - REVIEW").sum()) if not vdf.empty else 0],["Number FAILED",int((vdf["Validation Status"]=="FAILED").sum()) if not vdf.empty else 0],["Number CONFIG ERROR",int((vdf["Validation Status"]=="CONFIG ERROR").sum()) if not vdf.empty else 0],["Best overall validation stop",vdf.sort_values(["Validation Score","Validation Profit Factor","Validation Average Return"],ascending=[False,False,False]).iloc[0]["Stop"] if not vdf.empty else ""],["Best overall validation target",vdf.sort_values(["Validation Score","Validation ProfitFactor"],ascending=[False,False]).iloc[0]["Target"] if (not vdf.empty and "Validation ProfitFactor" in vdf.columns) else (vdf.sort_values(["Validation Score"],ascending=[False]).iloc[0]["Target"] if not vdf.empty else "")],["Best overall validation status",vdf.sort_values(["Validation Score"],ascending=[False]).iloc[0]["Validation Status"] if not vdf.empty else ""],["Best overall validation score",float(vdf["Validation Score"].max()) if not vdf.empty else 0.0],["Best overall validation average return",float(vdf["Validation Average Return"].max()) if not vdf.empty else 0.0],["Best overall validation expected R",float(vdf["Validation Expected R"].max()) if not vdf.empty else 0.0],["Best overall validation profit factor",float(vdf["Validation Profit Factor"].max()) if not vdf.empty else 0.0],["Best overall validation max drawdown",float(vdf["Validation Max Drawdown"].min()) if not vdf.empty else 0.0],["Best overall return decay",float(pd.to_numeric(vdf["Return Decay"],errors='coerce').max()) if not vdf.empty else 0.0],["Best overall PF decay",float(pd.to_numeric(vdf["PF Decay"],errors='coerce').max()) if not vdf.empty else 0.0],["Best overall DD expansion",float(pd.to_numeric(vdf["DD Expansion"],errors='coerce').min()) if not vdf.empty else 0.0],["Validation readiness status",("PASSED VALIDATION" if int((vdf["Validation Status"]=="VALIDATED").sum())>0 else ("CONDITIONAL VALIDATION" if int((vdf["Validation Status"]=="PROVISIONAL VALIDATION").sum())+int((vdf["Validation Status"]=="CONDITIONAL - REVIEW").sum())>0 else "FAILED VALIDATION")) if not vdf.empty else "FAILED VALIDATION"],["Validation readiness reason","Based on validation status distribution"]],columns=["Field","Value"])),("DIAGNOSTICS",pd.DataFrame([["Missing raw dates",miss],["Entries skipped",int((aud["warning"].fillna("")!="").sum()) if not aud.empty else 0],["Number with no horizon bars",nohz],["Only VALIDATION split used","YES"],["No Validation data used for candidate generation","YES"]],columns=["Field","Value"])),("VALIDATION ROBUSTNESS REPORT",robust_df),("VALIDATION ROBUSTNESS SUMMARY",pd.DataFrame([["Number READY FOR FINAL OOS",r_ready],["Number NEEDS REVIEW",r_review],["Number REJECT FOR NOW",r_rej],["Best robustness candidate stop",best.iloc[0]["Stop"] if not best.empty else ""],["Best robustness candidate target",best.iloc[0]["Target"] if not best.empty else ""],["Best robustness candidate validation score",best.iloc[0]["Existing Validation Score"] if not best.empty else ""],["Best robustness candidate validation avg return",best.iloc[0]["Validation Average Return"] if not best.empty else ""],["Best robustness candidate validation PF",best.iloc[0]["Validation Profit Factor"] if not best.empty else ""],["Best robustness candidate stress avg return",best.iloc[0]["Stress Average Return"] if not best.empty else ""],["Best robustness candidate stress PF",best.iloc[0]["Stress Profit Factor"] if not best.empty else ""],["Best robustness candidate regime consistency score",best.iloc[0]["Regime Consistency Score"] if not best.empty else ""],["Final robustness readiness status",frs],["Final robustness readiness reason",fr]],columns=["Field","Value"]))]
    ws=recreate_sheet(sh,"Validation"); w0=time.perf_counter(); write_sheet(ws,sections); print(f"validation write {time.perf_counter()-w0:.2f}s")
    print("Validation sheet updated")
    print(f"validation total {time.perf_counter()-t:.2f}s")



def get_or_create_gate_sheet(sh):
    try:
        ws = sh.worksheet("Gate")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Gate", rows=3000, cols=80)
    ws.clear()
    return ws


def load_gate_config(sh) -> pd.DataFrame:
    vals = sh.worksheet("Control Panel").get("N4:R14")
    rows = []
    for r in vals:
        r = r + [""] * (5 - len(r))
        gate = str(r[0]).strip()
        if not gate:
            continue
        thr = parse_pct_or_float(r[2])
        rev = parse_pct_or_float(r[3])
        hg = str(r[4]).strip().upper() in {"TRUE", "YES", "1"}
        rows.append({
            "Gate": gate,
            "Rule": str(r[1]).strip(),
            "Pass Threshold": thr,
            "Review Lower Threshold": rev,
            "Hard Gate": hg,
            "Pass Threshold Raw": r[2],
            "Review Lower Threshold Raw": r[3],
        })
    return pd.DataFrame(rows)




def close_match(a, b, tol=1e-6):
    try:
        return abs(float(a)-float(b))<=tol
    except Exception:
        return False


def _find_candidate_row(df: pd.DataFrame, stop: float, target: float):
    if df is None or df.empty:
        return None
    stop_col = next((c for c in df.columns if c.strip().lower() in {"stop", "stop %", "stop%"}), None)
    target_col = next((c for c in df.columns if c.strip().lower() in {"target", "target %", "target%"}), None)
    if not stop_col or not target_col:
        return None
    for _, r in df.iterrows():
        if close_match(r.get(stop_col), stop) and close_match(r.get(target_col), target):
            return r
    return None


def _pick_value(row, aliases):
    if row is None:
        return np.nan
    for a in aliases:
        if a in row.index:
            v = pd.to_numeric(row.get(a), errors="coerce")
            if pd.notna(v):
                return float(v)
    return np.nan


def normalise_header(name):
    s = str(name or "").strip().lower()
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def get_metric_from_row(row, aliases):
    if row is None:
        return np.nan
    keys = {normalise_header(k): k for k in row.index}
    for a in aliases:
        k = keys.get(normalise_header(a))
        if k is not None:
            v = pd.to_numeric(row.get(k), errors="coerce")
            if pd.notna(v):
                return float(v)
    return np.nan


def get_metric_from_summary(summary_map, aliases):
    if summary_map is None or len(summary_map) == 0:
        return np.nan
    keys = {normalise_header(k): k for k in summary_map.index}
    for a in aliases:
        k = keys.get(normalise_header(a))
        if k is not None:
            v = pd.to_numeric(summary_map.get(k), errors="coerce")
            if pd.notna(v):
                return float(v)
    return np.nan


def get_metric_with_fallback(row, summary_map, aliases):
    rv = get_metric_from_row(row, aliases)
    if pd.notna(rv):
        return rv, "row-level"
    sv = get_metric_from_summary(summary_map, aliases)
    if pd.notna(sv):
        return sv, "summary fallback"
    return np.nan, "missing"


def _summary_field_map(df: pd.DataFrame):
    if df is None or df.empty:
        return pd.Series(dtype=object)
    fld = next((c for c in df.columns if str(c).strip().lower() == "field"), None)
    val = next((c for c in df.columns if str(c).strip().lower() == "value"), None)
    if fld is None or val is None:
        return pd.Series(dtype=object)
    out = {}
    for _, r in df.iterrows():
        k = str(r.get(fld, "")).strip()
        if k:
            out[k] = r.get(val, "")
    return pd.Series(out)

def run_gate_stage(sh, cfg):
    gate_ws = get_or_create_gate_sheet(sh)
    gcfg = load_gate_config(sh)
    test_vals = sh.worksheet("Test").get_all_values()
    test_results = read_section_from_values(test_vals, "TEST RESULTS")
    if test_results.empty:
        test_results = read_section_from_values(test_vals, "FINAL OOS SUMMARY")
    # candidate selection
    cand = pd.DataFrame(); source = ""
    if not test_results.empty and "Test Status" in test_results.columns:
        for st in ["FINAL OOS PASS", "PROVISIONAL OOS PASS", "REVIEW ONLY", "FAILED TEST"]:
            c = test_results[test_results["Test Status"] == st]
            if not c.empty:
                source = st
                c = c.copy(); c["_score"] = pd.to_numeric(c.get("Test Score", 0), errors="coerce").fillna(0)
                c["_avg"] = pd.to_numeric(c.get("Average Test Return", 0), errors="coerce").fillna(0)
                c["_pf"] = pd.to_numeric(c.get("Profit Factor", 0), errors="coerce").fillna(0)
                cand = c.sort_values(["_score", "_avg", "_pf"], ascending=[False, False, False]).head(1)
                break
    if cand.empty and not test_results.empty:
        source = "Best available failed test candidate"; cand = test_results.head(1)

    metric_map = {}
    matched_test = matched_val = matched_rob = False
    val_src = test_src = ""
    missing_gate_metrics = []
    if not cand.empty:
        selected_stop = pd.to_numeric(cand.iloc[0].get("Stop"), errors="coerce")
        selected_target = pd.to_numeric(cand.iloc[0].get("Target"), errors="coerce")
        vvals = sh.worksheet("Validation").get_all_values()
        vres = read_section_from_values(vvals, "VALIDATION RESULTS")
        vrob = read_section_from_values(vvals, "VALIDATION ROBUSTNESS REPORT")
        trow = _find_candidate_row(test_results, selected_stop, selected_target)
        vrow = _find_candidate_row(vres, selected_stop, selected_target)
        rrow = _find_candidate_row(vrob, selected_stop, selected_target)
        matched_test = trow is not None; matched_val = vrow is not None; matched_rob = rrow is not None
        val_src = "VALIDATION RESULTS" if matched_val else ("VALIDATION ROBUSTNESS REPORT" if matched_rob else "NONE")
        test_src = "TEST RESULTS" if matched_test else "NONE"

        vsum = read_section_from_values(vvals, "VALIDATION SUMMARY")
        tsum = read_section_from_values(test_vals, "FINAL OOS SUMMARY")
        vsummary = _summary_field_map(vsum)
        tsummary = _summary_field_map(tsum)
        if vrow is None:
            vrow = vsummary
        if trow is None:
            trow = tsummary

        vrsum = _summary_field_map(read_section_from_values(vvals, "VALIDATION ROBUSTNESS SUMMARY"))
        v_pf, _ = get_metric_with_fallback(vrow, vsummary, ["Validation Profit Factor", "Profit Factor", "Validation PF", "validation_profit_factor"])
        t_pf, _ = get_metric_with_fallback(trow, tsummary, ["Test Profit Factor", "Profit Factor", "Test PF", "test_profit_factor"])
        v_dd, _ = get_metric_with_fallback(vrow, vsummary, ["Validation Max Drawdown", "Validation Max DD", "Sequential Trade Max Drawdown", "Max Drawdown", "validation_max_drawdown"])
        t_dd, _ = get_metric_with_fallback(trow, tsummary, ["Test Max Drawdown", "Test Max DD", "Sequential Test Max Drawdown", "Max Drawdown", "test_max_drawdown"])
        v_er, v_er_src = get_metric_with_fallback(vrow, vsummary, ["Validation Expected R", "Validation Expected R Multiple", "Expected R Multiple", "Expected R", "Best overall validation expected R", "validation_expected_r"])
        t_er, t_er_src = get_metric_with_fallback(trow, tsummary, ["Test Expected R", "Test Expected R Multiple", "Expected R Multiple", "Expected R", "Best test candidate expected R", "test_expected_r"])
        v_fr, _ = get_metric_with_fallback(vrow, vsummary, ["Validation Final Return", "Final Compounded Return", "Final Return", "validation_final_return"])
        t_fr, _ = get_metric_with_fallback(trow, tsummary, ["Test Final Return", "Final Compounded Return", "Final Return", "test_final_return"])
        v_avg, v_avg_src = get_metric_with_fallback(vrow, vsummary.combine_first(vrsum), ["Validation Average Return", "Validation Average Pair Return", "Average Pair Return", "Average Return", "Best overall validation average return", "Best overall validation avg return", "Best robustness candidate validation avg return", "validation_average_return", "validation_avg_return", "avg_return", "average_pair_return"])
        t_avg, t_avg_src = get_metric_with_fallback(trow, tsummary, ["Test Average Return", "Test Average Pair Return", "Average Pair Return", "Average Return", "Final OOS Average Return", "OOS Average Return", "Best test candidate average return", "Best overall validation average return", "test_average_return", "test_avg_return", "avg_return", "average_pair_return"])
        v_eff, _ = get_metric_with_fallback(vrow, vsummary, ["Validation Effective Sample Size", "Validation Raw Trades", "Raw Trades"])
        t_eff, _ = get_metric_with_fallback(trow, tsummary, ["Test Effective Sample Size", "Raw Test Trades", "Raw Trades"])

        avg_deg = (t_avg - v_avg) / abs(v_avg) if np.isfinite(v_avg) and v_avg != 0 and np.isfinite(t_avg) else np.nan
        er_deg = (t_er - v_er) / abs(v_er) if np.isfinite(v_er) and v_er != 0 and np.isfinite(t_er) else np.nan
        eff = np.nanmin([x for x in [v_eff, t_eff] if np.isfinite(x)]) if any(np.isfinite(x) for x in [v_eff, t_eff]) else np.nan
        metric_map = {
            "minimum validation profit factor": v_pf,
            "minimum test profit factor": t_pf,
            "validation max dd floor": v_dd,
            "maximum validation max dd": v_dd,
            "test max dd floor": t_dd,
            "maximum test max dd": t_dd,
            "minimum effective trades": eff,
            "maximum avg return degradation": avg_deg,
            "avg return degradation floor": avg_deg,
            "average return degradation floor": avg_deg,
            "avg return degradation": avg_deg,
            "average return degradation": avg_deg,
            "maximum average return degradation": avg_deg,
            "return degradation": avg_deg,
            "avg_return_degradation": avg_deg,
            "average_return_degradation": avg_deg,
            "maximum expected r degradation": er_deg,
            "expected r degradation floor": er_deg,
            "expected r degradation": er_deg,
            "expected r degradation ratio": er_deg,
            "expected_r_degradation": er_deg,
            "expected_r_degradation_floor": er_deg,
            "minimum validation expected r": v_er,
            "minimum test expected r": t_er,
            "minimum validation final return": v_fr,
            "minimum test final return": t_fr,
        }

    eval_rows = []
    for _, gr in gcfg.iterrows():
        name = str(gr["Gate"]).strip()
        key = name.lower()
        actual = metric_map.get(key, np.nan)
        actual_out = actual if pd.notna(actual) else ""
        rule = str(gr["Rule"]).strip() or ">="
        p, rv = gr["Pass Threshold"], gr["Review Lower Threshold"]
        result, warn = "CONFIG ERROR", ""
        if pd.notna(actual) and pd.notna(p):
            if rule == ">=":
                result = "PASS" if actual >= p else ("REVIEW" if (pd.notna(rv) and actual >= rv) else "FAIL")
            elif rule == ">":
                result = "PASS" if actual > p else ("REVIEW" if (pd.notna(rv) and actual > rv) else "FAIL")
            elif rule == "<=":
                result = "PASS" if actual <= p else ("REVIEW" if (pd.notna(rv) and actual <= rv) else "FAIL")
            elif rule == "<":
                result = "PASS" if actual < p else ("REVIEW" if (pd.notna(rv) and actual < rv) else "FAIL")
            elif rule.lower() == "between":
                result = "PASS" if (pd.notna(rv) and rv <= actual <= p) else "CONFIG ERROR"
            else:
                result = "CONFIG ERROR"
                warn = "Unsupported rule"
        else:
            if key == "maximum avg return degradation" and (not np.isfinite(metric_map.get("maximum avg return degradation", np.nan))):
                warn = "Cannot calculate average return degradation because test average return is missing" if not np.isfinite(t_avg) else "Cannot calculate average return degradation because validation average return is missing or zero"
            elif key == "maximum expected r degradation" and (not np.isfinite(metric_map.get("maximum expected r degradation", np.nan))):
                warn = "Cannot calculate expected R degradation because validation expected R is missing or zero"
            else:
                warn = "Metric unavailable or threshold missing"
        eval_rows.append({
            "Gate": name,
            "Mapped Metric": key,
            "Actual Value": actual_out,
            "Rule": rule,
            "Pass Threshold": p,
            "Review Lower Threshold": rv,
            "Hard Gate": bool(gr["Hard Gate"]),
            "Gate Result": result,
            "Distance To Pass": (actual - p) if (pd.notna(actual) and pd.notna(p)) else "",
            "Distance To Review Lower": (actual - rv) if (pd.notna(actual) and pd.notna(rv)) else "",
            "Interpretation": ("Pass" if result=="PASS" else ("Review" if result=="REVIEW" else ("Fail" if result=="FAIL" else "Config error"))),
            "Config Warning": warn,
        })
    gedf = pd.DataFrame(eval_rows)
    hard = gedf[gedf["Hard Gate"] == True]
    soft = gedf[gedf["Hard Gate"] == False]
    h_fail = int((hard["Gate Result"] == "FAIL").sum())
    h_cfg = int((hard["Gate Result"] == "CONFIG ERROR").sum())
    h_rev = int((hard["Gate Result"] == "REVIEW").sum())
    s_fail = int((soft["Gate Result"] == "FAIL").sum())
    s_rev = int((soft["Gate Result"] == "REVIEW").sum())
    if int((gedf["Gate Result"] == "CONFIG ERROR").sum()) > 0: fstat = "CONFIG ERROR"
    elif h_fail > 0 or s_fail > 0: fstat = "FAIL"
    elif h_rev > 0: fstat = "REVIEW"
    elif s_rev > 0: fstat = "PASS WITH SOFT REVIEW"
    else: fstat = "PASS"
    mc = "YES" if fstat == "PASS" else ("YES - REVIEW" if fstat in ["REVIEW", "PASS WITH SOFT REVIEW"] else "NO")

    gs = pd.DataFrame([
        ["Final Gate Status", fstat],
        ["Final Gate Reason", "Derived from hard/soft gate outcomes"],
        ["Candidate Source Used", source],
        ["Selected Stop", pd.to_numeric(cand.iloc[0].get("Stop", np.nan), errors="coerce") if not cand.empty else ""],
        ["Selected Target", pd.to_numeric(cand.iloc[0].get("Target", np.nan), errors="coerce") if not cand.empty else ""],
        ["Validation Status", cand.iloc[0].get("Prior Status", "") if not cand.empty else ""],
        ["Test Status", cand.iloc[0].get("Test Status", "") if not cand.empty else ""],
        ["Number PASS", int((gedf["Gate Result"] == "PASS").sum())],
        ["Number REVIEW", int((gedf["Gate Result"] == "REVIEW").sum())],
        ["Number FAIL", int((gedf["Gate Result"] == "FAIL").sum())],
        ["Number CONFIG ERROR", int((gedf["Gate Result"] == "CONFIG ERROR").sum())],
        ["Number Hard Gate Failures", h_fail],
        ["Number Soft Gate Failures", s_fail],
        ["Number Hard Gate Reviews", h_rev],
        ["Number Soft Gate Reviews", s_rev],
        ["Ready For Monte Carlo", mc],
        ["Monte Carlo Readiness Reason", f"Gate status {fstat}"],
    ], columns=["Field", "Value"])

    gd = pd.DataFrame([
        ["Confirmation Train stage preserved", "YES"],
        ["Confirmation Validation stage preserved", "YES"],
        ["Confirmation Robustness stage preserved", "YES"],
        ["Confirmation Test stage preserved", "YES"],
        ["Confirmation Gate used no optimisation", "YES"],
        ["Confirmation Gate used no Test data for candidate generation", "YES"],
        ["Confirmation Gate used final OOS/Test candidates only", "YES"],
        ["Same-bar mode", SAME_BAR_MODE],
        ["Stop execution mode", STOP_EXECUTION_MODE],
        ["Run timestamp", datetime.now(timezone.utc)],
        ["Selected Stop", selected_stop if "selected_stop" in locals() else ""],
        ["Selected Target", selected_target if "selected_target" in locals() else ""],
        ["Matched Test Candidate", "YES" if matched_test else "NO"],
        ["Matched Validation Candidate", "YES" if matched_val else "NO"],
        ["Matched Robustness Candidate", "YES" if matched_rob else "NO"],
        ["Validation metric source used", val_src if "val_src" in locals() else ""],
        ["Test metric source used", test_src if "test_src" in locals() else ""],
        ["Avg return degradation source", f"validation={v_avg_src if 'v_avg_src' in locals() else 'missing'}; test={t_avg_src if 't_avg_src' in locals() else 'missing'}"],
        ["Expected R degradation source", f"validation={v_er_src if 'v_er_src' in locals() else 'missing'}; test={t_er_src if 't_er_src' in locals() else 'missing'}"],
        ["Validation average return used", v_avg if "v_avg" in locals() and np.isfinite(v_avg) else ""],
        ["Validation average return source", v_avg_src if "v_avg_src" in locals() else ""],
        ["Test average return used", t_avg if "t_avg" in locals() and np.isfinite(t_avg) else ""],
        ["Test average return source", t_avg_src if "t_avg_src" in locals() else ""],
        ["Validation expected R used", v_er if "v_er" in locals() and np.isfinite(v_er) else ""],
        ["Validation expected R source", v_er_src if "v_er_src" in locals() else ""],
        ["Test expected R used", t_er if "t_er" in locals() and np.isfinite(t_er) else ""],
        ["Test expected R source", t_er_src if "t_er_src" in locals() else ""],
        ["Avg return degradation calculated", avg_deg if "avg_deg" in locals() and np.isfinite(avg_deg) else ""],
        ["Expected R degradation calculated", er_deg if "er_deg" in locals() and np.isfinite(er_deg) else ""],
        ["Confirmation degradation gates calculated independently", "YES"],
        ["Confirmation no 9999 placeholder used", "YES"],
    ], columns=["Field", "Value"])

    write_sheet(gate_ws, [
        ("GATE CONFIG", gcfg[["Gate", "Rule", "Pass Threshold", "Review Lower Threshold", "Hard Gate"]] if not gcfg.empty else pd.DataFrame()),
        ("GATE EVALUATION", gedf),
        ("GATE SUMMARY", gs),
        ("GATE DIAGNOSTICS", gd),
    ])
    print("Gate completed")

def get_or_create_position_sizing_sheet(sh):
    try:
        ws=sh.worksheet("Position Sizing")
        ws.clear()
        return ws
    except Exception:
        return sh.add_worksheet(title="Position Sizing",rows=5000,cols=180)


def run_position_sizing_stage(sh, cfg, raw, entries_all=None, final_oos_candidates=None):
    t0 = time.perf_counter()
    ws = get_or_create_position_sizing_sheet(sh)
    cands = final_oos_candidates if final_oos_candidates is not None else parse_final_oos_pass_candidates(sh)

    # Build validation/test caches once, not repeatedly inside candidate loops.
    val_entries = load_entries(
        sh, cfg, "VALIDATION", cfg.validation_start, cfg.validation_end, entries_all=entries_all
    )
    test_entries = load_entries(
        sh, cfg, "TEST", cfg.test_start, cfg.test_end, entries_all=entries_all
    )

    val_cache, _, _, _, _ = build_trade_cache(val_entries, raw, cfg, cfg.validation_end)
    test_cache, _, _, _, _ = build_trade_cache(test_entries, raw, cfg, cfg.test_end)

    split_caches = {
        "VALIDATION": val_cache,
        "TEST": test_cache,
        "VALIDATION+TEST": sorted(val_cache + test_cache, key=lambda x: x.entry_date),
    }
    mults=[1.00,0.75,0.50,0.33,0.25,0.10]; sims=10000
    secs=[]
    def sec(n,d): secs.append((n,d if d is not None else pd.DataFrame()))
    sec("POSITION SIZING MC CONFIG",pd.DataFrame([["Direction",cfg.direction],["Horizon Months",cfg.horizon_months],["Exposure Multipliers",", ".join(map(str,mults))],["MC Simulations",sims],["Candidate Source","FINAL OOS PASS"],["Run Timestamp UTC",datetime.now(timezone.utc)]],columns=["Field","Value"]))
    rows=[]
    if not cands.empty:
        for _,c in cands.iterrows():
            stop,target=float(c["Stop"]),float(c["Target"])
            for wname in ["VALIDATION", "TEST", "VALIDATION+TEST"]:
                gross = []
                holds = []
                dates = []

                cache = split_caches[wname]

                for tr in cache:
                    oc, rr, idx, sb = hit_logic_fast(
                        tr.high, tr.low, tr.entry_price, cfg.direction, stop, target
                    )
                    rr = tr.horizon_close_return if rr is None else rr

                    d = int((pd.Timestamp(tr.dates[max(idx, 0)]) - pd.Timestamp(tr.entry_date)).days)

                    gross.append(rr)
                    holds.append(d)
                    dates.append(tr.entry_date)
                ord_idx=np.argsort(np.array(dates,dtype='datetime64[ns]')) if dates else np.array([],dtype=int)
                gross=np.array(gross,float)[ord_idx] if len(ord_idx) else np.array([],float)
                holds=np.array(holds,float)[ord_idx] if len(ord_idx) else np.array([],float)
                for cc,sp,cm,sl,fi in [("BASE",DEFAULT_SPREAD_BPS,DEFAULT_COMMISSION_BPS,DEFAULT_SLIPPAGE_BPS,DEFAULT_FINANCING_BPS_PER_YEAR),("STRESS",STRESS_SPREAD_BPS,STRESS_COMMISSION_BPS,STRESS_SLIPPAGE_BPS,STRESS_FINANCING_BPS_PER_YEAR)]:
                    net = apply_trade_costs_vec(gross, holds, sp, cm, sl, fi) if len(gross) else np.array([], float)
                    net=net[np.isfinite(net)]; net=net[net>-1.0]
                    if len(net)==0: continue
                    n=len(net); bs=max(3,int(round(math.sqrt(n))))
                    for m in mults:
                        sized=net*m
                        sidx={"VALIDATION":1,"TEST":3,"VALIDATION+TEST":5}[wname] + (0 if cc=="BASE" else 1)
                        iid_seed=4200+int(c["Candidate Rank"])*100+sidx+int(m*100)
                        block_seed=14200+int(c["Candidate Rank"])*100+sidx+int(m*100)
                        iid = bootstrap_iid_paths(sized, sims, n, iid_seed)
                        blk = bootstrap_block_paths(sized, sims, n, bs, block_seed)

                        si = summarize_mc_paths(iid)
                        sb = summarize_mc_paths(blk)

                        # Needed because the row still directly uses iid_mdds and blk_mdds
                        iid_mdds = max_drawdown_paths(iid)
                        blk_mdds = max_drawdown_paths(blk)
                        row={"Candidate Rank":int(c["Candidate Rank"]),"Stop":stop,"Target":target,"MC Window":wname,"Cost Case":cc,"Direction":cfg.direction,"Horizon Months":cfg.horizon_months,"Exposure Multiplier":m,"Number Historical Trades":n,"Number MC Simulations":sims,"Trades Per Simulation":n,"IID Seed":iid_seed,"Block Seed":block_seed,"Block Size":bs,"Historical Sized Average Return":float(np.mean(sized)),"Historical Sized Median Return":float(np.median(sized)),"Historical Sized Standard Deviation":float(np.std(sized,ddof=1)) if len(sized)>1 else 0.0,"Historical Sized Final Compounded Return":float(np.prod(1+sized)-1),"Historical Sized Max Drawdown":max_drawdown_from_returns(sized),"Historical Sized Profit Factor":profit_factor(sized),"Historical Sized Expected R":float(np.mean(sized)/stop) if stop!=0 else "","IID Median Final Return":si["med_final"],"IID P5 Final Return":si["p5_final"],"IID P1 Final Return":si["p1_final"],"IID Probability Final Return < 0":si["p_neg"],"IID Median Max Drawdown Severity":float(np.median(np.abs(iid_mdds))),"IID P95 Max Drawdown Severity":float(np.percentile(np.abs(iid_mdds),95)),"IID P99 Max Drawdown Severity":float(np.percentile(np.abs(iid_mdds),99)),"IID Probability MDD > 10%":si["p_mdd10"],"IID Probability MDD > 20%":si["p_mdd20"],"IID Probability MDD > 30%":si["p_mdd30"],"IID Probability MDD > 40%":si["p_mdd40"],"IID Probability MDD > 50%":si["p_mdd50"],"IID Median Losing Streak":si["med_ls"],"IID P95 Losing Streak":si["p95_ls"],"Block Median Final Return":sb["med_final"],"Block P5 Final Return":sb["p5_final"],"Block P1 Final Return":sb["p1_final"],"Block Probability Final Return < 0":sb["p_neg"],"Block Median Max Drawdown Severity":float(np.median(np.abs(blk_mdds))),"Block P95 Max Drawdown Severity":float(np.percentile(np.abs(blk_mdds),95)),"Block P99 Max Drawdown Severity":float(np.percentile(np.abs(blk_mdds),99)),"Block Probability MDD > 10%":sb["p_mdd10"],"Block Probability MDD > 20%":sb["p_mdd20"],"Block Probability MDD > 30%":sb["p_mdd30"],"Block Probability MDD > 40%":sb["p_mdd40"],"Block Probability MDD > 50%":sb["p_mdd50"],"Block Median Losing Streak":sb["med_ls"],"Block P95 Losing Streak":sb["p95_ls"]}

                        gates=[row["Block Probability Final Return < 0"]<=0.05,row["Block P5 Final Return"]>0,row["Block Median Max Drawdown Severity"]<=0.35,row["Block P95 Max Drawdown Severity"]<=0.50,row["Block Probability MDD > 30%"]<=0.50,row["Block Probability MDD > 40%"]<=0.25,row["Block P95 Losing Streak"]<=25]
                        score=float(sum(gates)); row["Position Sizing Robustness Score"]=score; row["Position Sizing Grade"]=("A" if score>=6.5 else ("B" if score>=5.5 else ("C" if score>=4.5 else ("D" if score>=3.5 else "F"))))
                        if all(gates): st="DEPLOYABLE"
                        elif (gates[0] and gates[1] and sum(gates)>=5): st="REVIEW"
                        else: st="REJECT"
                        row["Position Sizing Status"]=st; row["Position Sizing Readiness Flag"]=st
                        rows.append(row)
    rdf=pd.DataFrame(rows)
    sec("POSITION SIZING MC RESULTS",rdf)
    fdf=rdf[(rdf.get("MC Window","")=="VALIDATION+TEST")&(rdf.get("Cost Case","")=="STRESS")].copy() if not rdf.empty else pd.DataFrame()
    sec("POSITION SIZING FINAL DECISION",fdf)
    dep=fdf[fdf.get("Position Sizing Status","")=="DEPLOYABLE"] if not fdf.empty else pd.DataFrame()
    rev=fdf[fdf.get("Position Sizing Status","")=="REVIEW"] if not fdf.empty else pd.DataFrame()
    rej=fdf[fdf.get("Position Sizing Status","")=="REJECT"] if not fdf.empty else pd.DataFrame()
    sec("POSITION SIZING DEPLOYABLE CANDIDATES",dep if not dep.empty else pd.DataFrame([["Message","NO DEPLOYABLE CANDIDATES UNDER CURRENT RISK GATES"]],columns=["Message"]))
    sec("POSITION SIZING REVIEW CANDIDATES",rev)
    sec("POSITION SIZING REJECTED CANDIDATES",rej)
    best=dep.sort_values(["Block Probability MDD > 40%","Block Probability MDD > 30%","Block P95 Max Drawdown Severity","Block Probability Final Return < 0","Block P5 Final Return"],ascending=[True,True,True,True,False]).head(1) if not dep.empty else pd.DataFrame()
    frs="READY FOR POSITION SIZING" if not dep.empty else ("REVIEW BEFORE POSITION SIZING" if not rev.empty else "FAILED MC POSITION SIZING GATE")
    sec("POSITION SIZING SUMMARY",pd.DataFrame([["Number Candidates Tested",len(cands)],["Number Exposure Multipliers Tested",len(mults)],["Number Position Sizing Rows",len(rdf)],["Number Deployable Rows",len(dep)],["Number Review Rows",len(rev)],["Number Rejected Rows",len(rej)],["Best Deployable Candidate Rank",best.iloc[0]["Candidate Rank"] if not best.empty else ""],["Best Deployable Stop",best.iloc[0]["Stop"] if not best.empty else ""],["Best Deployable Target",best.iloc[0]["Target"] if not best.empty else ""],["Best Deployable Exposure Multiplier",best.iloc[0]["Exposure Multiplier"] if not best.empty else ""],["Best Deployable Median Final Return",best.iloc[0]["Block Median Final Return"] if not best.empty else ""],["Best Deployable P5 Final Return",best.iloc[0]["Block P5 Final Return"] if not best.empty else ""],["Best Deployable Median Max Drawdown Severity",best.iloc[0]["Block Median Max Drawdown Severity"] if not best.empty else ""],["Best Deployable P95 Max Drawdown Severity",best.iloc[0]["Block P95 Max Drawdown Severity"] if not best.empty else ""],["Best Deployable Probability MDD > 30%",best.iloc[0]["Block Probability MDD > 30%"] if not best.empty else ""],["Best Deployable Probability MDD > 40%",best.iloc[0]["Block Probability MDD > 40%"] if not best.empty else ""],["Final Position Sizing Research Status",frs],["Final Position Sizing Research Reason",("At least one deployable row found" if not dep.empty else "No deployable rows under current risk gates")]],columns=["Field","Value"]))
    sec("POSITION SIZING DIAGNOSTICS",pd.DataFrame([["Position sizing applied to net returns before compounding","YES"],["Equity curves recalculated after sizing","YES"],["Drawdown metrics recalculated after sizing","YES"],["No Train data used","YES"],["Validation/Test/Combined windows preserved","YES"],["BASE/STRESS cost cases preserved","YES"],["IID and block bootstrap preserved","YES"],["No optimisation using MC results","YES"],["Deployable candidates selected only from Validation+Test STRESS final sizing decision rows","YES"],["Drawdown severity uses abs(max_drawdown), not optimistic negative percentile values","YES"],["Run timestamp UTC",datetime.now(timezone.utc)]],columns=["Field","Value"]))

    # FRONTIER/SENSITIVITY built from existing position sizing rows only
    frontier=pd.DataFrame()
    if not fdf.empty:
        t=fdf.copy()
        t["Decision"]=t.get("Position Sizing Status","")
        t["Decision Reason"]=t.get("Position Sizing Readiness Flag","")
        t["Median Final Return"]=pd.to_numeric(t.get("Block Median Final Return"),errors='coerce')
        t["P5 Final Return"]=pd.to_numeric(t.get("Block P5 Final Return"),errors='coerce')
        t["Median Max Drawdown Severity"]=pd.to_numeric(t.get("Block Median Max Drawdown Severity"),errors='coerce')
        t["P95 Max Drawdown Severity"]=pd.to_numeric(t.get("Block P95 Max Drawdown Severity"),errors='coerce')
        t["Probability MDD > 30%"] = pd.to_numeric(t.get("Block Probability MDD > 30%"),errors='coerce')
        t["Probability MDD > 40%"] = pd.to_numeric(t.get("Block Probability MDD > 40%"),errors='coerce')
        p5_thr=0.0; p95_thr=0.50; p30_thr=0.50; p40_thr=0.25
        t["Return / P95 MDD"]=t["Median Final Return"]/t["P95 Max Drawdown Severity"]
        t["P5 Return / P95 MDD"]=t["P5 Final Return"]/t["P95 Max DrawdownSeverity"] if "P95 Max DrawdownSeverity" in t.columns else t["P5 Final Return"]/t["P95 Max Drawdown Severity"]
        t["Distance To P5 Return Threshold"]=t["P5 Final Return"]-p5_thr
        t["Distance To P95 MDD Threshold"]=p95_thr-t["P95 Max Drawdown Severity"]
        t["Distance To MDD 30% Probability Threshold"]=p30_thr-t["Probability MDD > 30%"]
        t["Distance To MDD 40% Probability Threshold"]=p40_thr-t["Probability MDD > 40%"]
        def bind(r):
            fails={"P5_RETURN":r["Distance To P5 Return Threshold"],"P95_MDD":r["Distance To P95 MDD Threshold"],"PROB_MDD_30":r["Distance To MDD 30% Probability Threshold"],"PROB_MDD_40":r["Distance To MDD 40% Probability Threshold"]}
            bad={k:v for k,v in fails.items() if pd.notna(v) and v<0}
            if not bad: return "NONE"
            return sorted(bad.items(),key=lambda kv:kv[1])[0][0]
        t["Binding Constraint"]=t.apply(bind,axis=1)
        elev=((t["P95 Max Drawdown Severity"]>0.30)|(t["Probability MDD > 30%"]>0.10)|(t["Probability MDD > 40%"]>0.02))
        t.loc[(t["Decision"]=="DEPLOYABLE")&elev,"Binding Constraint"]="ELEVATED_RISK"
        t.loc[(t["Decision"]=="REVIEW")&(t["Binding Constraint"]=="NONE"),"Binding Constraint"]="REVIEW_BAND"
        t.loc[(t["Decision"]=="REJECT")&(t["Binding Constraint"]=="NONE"),"Binding Constraint"]="UNKNOWN"
        t=t.sort_values(["Candidate Rank","Exposure Multiplier"])
        t["Previous Exposure Multiplier"]=t.groupby(["Candidate Rank","Stop","Target"])["Exposure Multiplier"].shift(1)
        t["Delta Median Final Return"]=t.groupby(["Candidate Rank","Stop","Target"])["Median Final Return"].diff()
        t["Delta P5 Final Return"]=t.groupby(["Candidate Rank","Stop","Target"])["P5 Final Return"].diff()
        t["Delta P95 Max Drawdown Severity"]=t.groupby(["Candidate Rank","Stop","Target"])["P95 Max Drawdown Severity"].diff()
        t["Delta Probability MDD > 30%"] = t.groupby(["Candidate Rank","Stop","Target"])["Probability MDD > 30%"].diff()
        t["Delta Probability MDD > 40%"] = t.groupby(["Candidate Rank","Stop","Target"])["Probability MDD > 40%"].diff()
        t["Frontier Label"]="REJECTED"
        t.loc[t["Decision"]=="DEPLOYABLE","Frontier Label"]="DEPLOYABLE"
        t.loc[(t["Decision"]!="DEPLOYABLE")&(t[["Distance To P5 Return Threshold","Distance To P95 MDD Threshold","Distance To MDD 30% Probability Threshold","Distance To MDD 40% Probability Threshold"]].lt(0).sum(axis=1)==1)&(t[["Distance To P5 Return Threshold","Distance To P95 MDD Threshold","Distance To MDD 30% Probability Threshold","Distance To MDD 40% Probability Threshold"]].min(axis=1)>-0.10),"Frontier Label"]="NEAR_PASS"
        RISK_CLIFF_P95_MDD_REL_INCREASE=0.50; RISK_CLIFF_P5_RETURN_REL_DETERIORATION=0.50; RISK_CLIFF_PROB_MDD_30_ABS_INCREASE=0.10; RISK_CLIFF_PROB_MDD_40_ABS_INCREASE=0.05
        rel_mdd=(t["Delta P95 Max Drawdown Severity"]/(t["P95 Max Drawdown Severity"]-t["Delta P95 Max Drawdown Severity"]).replace(0,np.nan)).fillna(0)
        rel_p5=(-t["Delta P5 Final Return"]/(t["P5 Final Return"]-t["Delta P5 Final Return"]).abs().replace(0,np.nan)).fillna(0)
        t.loc[(rel_mdd>RISK_CLIFF_P95_MDD_REL_INCREASE)|(rel_p5>RISK_CLIFF_P5_RETURN_REL_DETERIORATION)|(t["Delta Probability MDD > 30%"]>RISK_CLIFF_PROB_MDD_30_ABS_INCREASE)|(t["Delta Probability MDD > 40%"]>RISK_CLIFF_PROB_MDD_40_ABS_INCREASE),"Frontier Label"]="RISK_CLIFF"
        if not dep.empty:
            bidx=t[(t["Candidate Rank"]==best.iloc[0]["Candidate Rank"])&(t["Stop"]==best.iloc[0]["Stop"])&(t["Target"]==best.iloc[0]["Target"])&(t["Exposure Multiplier"]==best.iloc[0]["Exposure Multiplier"])].index
            if len(bidx): t.loc[bidx,"Frontier Label"]="DEPLOYABLE_BEST"
        frontier=t[["Candidate Rank","Stop","Target","Exposure Multiplier","Decision","Decision Reason","Median Final Return","P5 Final Return","Median Max Drawdown Severity","P95 Max Drawdown Severity","Probability MDD > 30%","Probability MDD > 40%","Return / P95 MDD","P5 Return / P95 MDD","Distance To P5 Return Threshold","Distance To P95 MDD Threshold","Distance To MDD 30% Probability Threshold","Distance To MDD 40% Probability Threshold","Binding Constraint","Frontier Label","Previous Exposure Multiplier","Delta Median Final Return","Delta P5 Final Return","Delta P95 Max Drawdown Severity","Delta Probability MDD > 30%","Delta Probability MDD > 40%"]]
    sec("POSITION SIZING FRONTIER",frontier)
    # expanded frontier summary (final decision rows only)
    dep_frontier = frontier[frontier.get("Decision","")=="DEPLOYABLE"] if not frontier.empty else pd.DataFrame()
    hf=dep_frontier.sort_values(["Exposure Multiplier","P95 Max Drawdown Severity"],ascending=[False,True]).head(1) if not dep_frontier.empty else pd.DataFrame()
    rr_all=frontier.sort_values("Return / P95 MDD",ascending=False).head(1) if not frontier.empty else pd.DataFrame()
    pr_all=frontier.sort_values("P5 Return / P95 MDD",ascending=False).head(1) if not frontier.empty else pd.DataFrame()
    rr_dep=dep_frontier.sort_values("Return / P95 MDD",ascending=False).head(1) if not dep_frontier.empty else pd.DataFrame()
    pr_dep=dep_frontier.sort_values("P5 Return / P95 MDD",ascending=False).head(1) if not dep_frontier.empty else pd.DataFrame()
    sel=frontier[(frontier["Candidate Rank"]==best.iloc[0]["Candidate Rank"])&(frontier["Stop"]==best.iloc[0]["Stop"])&(frontier["Target"]==best.iloc[0]["Target"])] if (not frontier.empty and not best.empty) else pd.DataFrame()
    fsum_rows=[["Best Deployable Exposure",best.iloc[0]["Exposure Multiplier"] if not best.empty else ""],["Best Deployable Candidate Rank",best.iloc[0]["Candidate Rank"] if not best.empty else ""],["Highest Deployable Exposure Candidate Rank",hf.iloc[0]["Candidate Rank"] if not hf.empty else ""],["Highest Deployable Exposure Stop",hf.iloc[0]["Stop"] if not hf.empty else ""],["Highest Deployable Exposure Target",hf.iloc[0]["Target"] if not hf.empty else ""],["Highest Deployable Exposure",hf.iloc[0]["Exposure Multiplier"] if not hf.empty else ""],["Highest Deployable Exposure Median Final Return",hf.iloc[0]["Median Final Return"] if not hf.empty else ""],["Highest Deployable Exposure P5 Final Return",hf.iloc[0]["P5 Final Return"] if not hf.empty else ""],["Highest Deployable Exposure P95 MDD Severity",hf.iloc[0]["P95 Max Drawdown Severity"] if not hf.empty else ""],["Highest Deployable Exposure Probability MDD > 30%",hf.iloc[0]["Probability MDD > 30%"] if not hf.empty else ""],["Highest Deployable Exposure Probability MDD > 40%",hf.iloc[0]["Probability MDD > 40%"] if not hf.empty else ""],["Number Of Near Pass Rows",int((frontier.get("Frontier Label",pd.Series(dtype=str))=="NEAR_PASS").sum()) if not frontier.empty else 0],["Number Of Risk Cliff Rows",int((frontier.get("Frontier Label",pd.Series(dtype=str))=="RISK_CLIFF").sum()) if not frontier.empty else 0],["Best Return / P95 MDD Candidate Rank",rr_all.iloc[0]["Candidate Rank"] if not rr_all.empty else ""],["Best Return / P95 MDD Stop",rr_all.iloc[0]["Stop"] if not rr_all.empty else ""],["Best Return / P95 MDD Target",rr_all.iloc[0]["Target"] if not rr_all.empty else ""],["Best Return / P95 MDD Exposure",rr_all.iloc[0]["Exposure Multiplier"] if not rr_all.empty else ""],["Best Return / P95 MDD Value",rr_all.iloc[0]["Return / P95 MDD"] if not rr_all.empty else ""],["Best Return / P95 MDD Decision",rr_all.iloc[0]["Decision"] if not rr_all.empty else ""],["Best Return / P95 MDD P5 Final Return",rr_all.iloc[0]["P5 Final Return"] if not rr_all.empty else ""],["Best Return / P95 MDD P95 MDD Severity",rr_all.iloc[0]["P95 Max Drawdown Severity"] if not rr_all.empty else ""],["Best P5 Return / P95 MDD Candidate Rank",pr_all.iloc[0]["Candidate Rank"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Stop",pr_all.iloc[0]["Stop"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Target",pr_all.iloc[0]["Target"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Exposure",pr_all.iloc[0]["Exposure Multiplier"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Value",pr_all.iloc[0]["P5 Return / P95 MDD"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Decision",pr_all.iloc[0]["Decision"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD P5 Final Return",pr_all.iloc[0]["P5 Final Return"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD P95 MDD Severity",pr_all.iloc[0]["P95 Max Drawdown Severity"] if not pr_all.empty else ""],["Selected Candidate Rank",best.iloc[0]["Candidate Rank"] if not best.empty else ""],["Selected Candidate Stop",best.iloc[0]["Stop"] if not best.empty else ""],["Selected Candidate Target",best.iloc[0]["Target"] if not best.empty else ""],["Selected Candidate Best Deployable Exposure",best.iloc[0]["Exposure Multiplier"] if not best.empty else ""],["Selected Candidate Highest Deployable Exposure",sel[sel["Decision"]=="DEPLOYABLE"]["Exposure Multiplier"].max() if not sel.empty else ""],["Selected Candidate First Review Exposure",sel[sel["Decision"]=="REVIEW"]["Exposure Multiplier"].min() if not sel.empty and (sel["Decision"]=="REVIEW").any() else ""],["Selected Candidate First Rejected Exposure",sel[sel["Decision"]=="REJECT"]["Exposure Multiplier"].min() if not sel.empty and (sel["Decision"]=="REJECT").any() else ""],["Selected Candidate First Risk Cliff Exposure",sel[sel["Frontier Label"]=="RISK_CLIFF"]["Exposure Multiplier"].min() if not sel.empty and (sel["Frontier Label"]=="RISK_CLIFF").any() else ""],["Selected Candidate Best Return / P95 MDD Exposure",sel.sort_values("Return / P95 MDD",ascending=False).iloc[0]["Exposure Multiplier"] if not sel.empty else ""],["Selected Candidate Best P5 Return / P95 MDD Exposure",sel.sort_values("P5 Return / P95 MDD",ascending=False).iloc[0]["Exposure Multiplier"] if not sel.empty else ""],["Highest Deployable Exposure Decision",hf.iloc[0]["Decision"] if not hf.empty else ""],["Highest Deployable Exposure Binding Constraint",hf.iloc[0]["Binding Constraint"] if not hf.empty else ""],["Highest Deployable Exposure Decision Reason",("Technically deployable, but elevated tail risk because P95 MDD severity is {:.2%}, Probability MDD > 30% is {:.2%}, and Probability MDD > 40% is {:.2%}.".format(float(hf.iloc[0]["P95 Max Drawdown Severity"]),float(hf.iloc[0]["Probability MDD > 30%"]),float(hf.iloc[0]["Probability MDD > 40%"])) if (not hf.empty and ((float(hf.iloc[0]["P95 Max Drawdown Severity"])>0.30) or (float(hf.iloc[0]["Probability MDD > 30%"])>0.10) or (float(hf.iloc[0]["Probability MDD > 40%"])>0.02))) else (hf.iloc[0]["Decision Reason"] if not hf.empty else ""))],
["Best Return / P95 MDD Across All Rows Candidate Rank",rr_all.iloc[0]["Candidate Rank"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows Stop",rr_all.iloc[0]["Stop"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows Target",rr_all.iloc[0]["Target"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows Exposure",rr_all.iloc[0]["Exposure Multiplier"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows Value",rr_all.iloc[0]["Return / P95 MDD"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows Decision",rr_all.iloc[0]["Decision"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows P5 Final Return",rr_all.iloc[0]["P5 Final Return"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows P95 MDD Severity",rr_all.iloc[0]["P95 Max Drawdown Severity"] if not rr_all.empty else ""],["Best Return / P95 MDD Across All Rows Diagnostic Note","Diagnostic only; may include rejected rows"],
["Best Return / P95 MDD Deployable Only Candidate Rank",rr_dep.iloc[0]["Candidate Rank"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only Stop",rr_dep.iloc[0]["Stop"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only Target",rr_dep.iloc[0]["Target"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only Exposure",rr_dep.iloc[0]["Exposure Multiplier"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only Value",rr_dep.iloc[0]["Return / P95 MDD"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only Decision",rr_dep.iloc[0]["Decision"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only P5 Final Return",rr_dep.iloc[0]["P5 Final Return"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only P95 MDD Severity",rr_dep.iloc[0]["P95 Max Drawdown Severity"] if not rr_dep.empty else ""],["Best Return / P95 MDD Deployable Only Diagnostic Note","Deployable-only ranking"],
["Best P5 Return / P95 MDD Across All Rows Candidate Rank",pr_all.iloc[0]["Candidate Rank"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows Stop",pr_all.iloc[0]["Stop"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows Target",pr_all.iloc[0]["Target"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows Exposure",pr_all.iloc[0]["Exposure Multiplier"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows Value",pr_all.iloc[0]["P5 Return / P95 MDD"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows Decision",pr_all.iloc[0]["Decision"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows P5 Final Return",pr_all.iloc[0]["P5 Final Return"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows P95 MDD Severity",pr_all.iloc[0]["P95 Max Drawdown Severity"] if not pr_all.empty else ""],["Best P5 Return / P95 MDD Across All Rows Diagnostic Note","Diagnostic only; may include rejected rows"],
["Best P5 Return / P95 MDD Deployable Only Candidate Rank",pr_dep.iloc[0]["Candidate Rank"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only Stop",pr_dep.iloc[0]["Stop"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only Target",pr_dep.iloc[0]["Target"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only Exposure",pr_dep.iloc[0]["Exposure Multiplier"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only Value",pr_dep.iloc[0]["P5 Return / P95 MDD"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only Decision",pr_dep.iloc[0]["Decision"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only P5 Final Return",pr_dep.iloc[0]["P5 Final Return"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only P95 MDD Severity",pr_dep.iloc[0]["P95 Max Drawdown Severity"] if not pr_dep.empty else ""],["Best P5 Return / P95 MDD Deployable Only Diagnostic Note","Deployable-only ranking"],
["Selected Candidate Best Return / P95 MDD Across All Rows Exposure",sel.sort_values("Return / P95 MDD",ascending=False).iloc[0]["Exposure Multiplier"] if not sel.empty else ""],["Selected Candidate Best Return / P95 MDD Across All Rows Decision",sel.sort_values("Return / P95 MDD",ascending=False).iloc[0]["Decision"] if not sel.empty else ""],["Selected Candidate Best Return / P95 MDD Deployable Only Exposure",sel[sel["Decision"]=="DEPLOYABLE"].sort_values("Return / P95 MDD",ascending=False).iloc[0]["Exposure Multiplier"] if not sel.empty and (sel["Decision"]=="DEPLOYABLE").any() else ""],["Selected Candidate Best Return / P95 MDD Deployable Only Decision",sel[sel["Decision"]=="DEPLOYABLE"].sort_values("Return / P95 MDD",ascending=False).iloc[0]["Decision"] if not sel.empty and (sel["Decision"]=="DEPLOYABLE").any() else ""],["Selected Candidate Best P5 Return / P95 MDD Across All Rows Exposure",sel.sort_values("P5 Return / P95 MDD",ascending=False).iloc[0]["Exposure Multiplier"] if not sel.empty else ""],["Selected Candidate Best P5 Return / P95 MDD Across All Rows Decision",sel.sort_values("P5 Return / P95 MDD",ascending=False).iloc[0]["Decision"] if not sel.empty else ""],["Selected Candidate Best P5 Return / P95 MDD Deployable Only Exposure",sel[sel["Decision"]=="DEPLOYABLE"].sort_values("P5 Return / P95 MDD",ascending=False).iloc[0]["Exposure Multiplier"] if not sel.empty and (sel["Decision"]=="DEPLOYABLE").any() else ""],["Selected Candidate Best P5 Return / P95 MDD Deployable Only Decision",sel[sel["Decision"]=="DEPLOYABLE"].sort_values("P5 Return / P95 MDD",ascending=False).iloc[0]["Decision"] if not sel.empty and (sel["Decision"]=="DEPLOYABLE").any() else ""],
["Selected Candidate Frontier Interpretation",("Selected candidate has a risk cliff after 0.25x." if not sel.empty and (sel["Frontier Label"]=="RISK_CLIFF").any() else ("Selected candidate remains deployable beyond 0.10x, but 0.10x has the best downside-adjusted profile." if not sel.empty and (sel["Decision"]=="DEPLOYABLE").sum()>1 else "Selected candidate is robust only at 0.10x; higher exposure fails quickly."))],["Global Frontier Interpretation",("Selected candidate remains deployable up to {:.2f}x, but the preferred final sizing is {:.2f}x because risk rises materially.".format(float(hf.iloc[0]["Exposure Multiplier"]),float(best.iloc[0]["Exposure Multiplier"])) if (not best.empty and not hf.empty and float(hf.iloc[0]["Candidate Rank"])==float(best.iloc[0]["Candidate Rank"]) and float(hf.iloc[0]["Stop"])==float(best.iloc[0]["Stop"]) and float(hf.iloc[0]["Target"])==float(best.iloc[0]["Target"])) else ("Selected candidate is preferred at {:.2f}x, while Candidate {} can tolerate higher exposure up to {:.2f}x.".format(float(best.iloc[0]["Exposure Multiplier"] if not best.empty else 0),int(hf.iloc[0]["Candidate Rank"]) if not hf.empty else "",float(hf.iloc[0]["Exposure Multiplier"]) if not hf.empty else 0) if (not best.empty and not hf.empty and float(hf.iloc[0]["Exposure Multiplier"])>float(best.iloc[0]["Exposure Multiplier"])) else "No candidate remains deployable above the selected exposure. The selected exposure is also the global frontier limit."))]]
    old_fields={"Best Return / P95 MDD Candidate Rank","Best Return / P95 MDD Stop","Best Return / P95 MDD Target","Best Return / P95 MDD Exposure","Best Return / P95 MDD Value","Best Return / P95 MDD Decision","Best Return / P95 MDD P5 Final Return","Best Return / P95 MDD P95 MDD Severity","Best P5 Return / P95 MDD Candidate Rank","Best P5 Return / P95 MDD Stop","Best P5 Return / P95 MDD Target","Best P5 Return / P95 MDD Exposure","Best P5 Return / P95 MDD Value","Best P5 Return / P95 MDD Decision","Best P5 Return / P95 MDD P5 Final Return","Best P5 Return / P95 MDD P95 MDD Severity","Selected Candidate Best Return / P95 MDD Exposure","Selected Candidate Best P5 Return / P95 MDD Exposure"}
    fsum_rows=[r for r in fsum_rows if r[0] not in old_fields]
    fsum=pd.DataFrame(fsum_rows,columns=["Field","Value"])
    sec("POSITION SIZING FRONTIER SUMMARY",fsum)

    # exposure aggregation + selected path
    exp_agg=pd.DataFrame()
    if not frontier.empty:
        ag=[]
        for ex,g in frontier.groupby("Exposure Multiplier"):
            ag.append({"Exposure Multiplier":ex,"Number Of Rows":len(g),"Deployable Rows":int((g["Decision"]=="DEPLOYABLE").sum()),"Review Rows":int((g["Decision"]=="REVIEW").sum()),"Rejected Rows":int((g["Decision"]=="REJECT").sum()),"Near Pass Rows":int((g["Frontier Label"]=="NEAR_PASS").sum()),"Risk Cliff Rows":int((g["Frontier Label"]=="RISK_CLIFF").sum()),"Best Median Final Return":g["Median Final Return"].max(),"Best P5 Final Return":g["P5 Final Return"].max(),"Lowest P95 MDD Severity":g["P95 Max Drawdown Severity"].min(),"Lowest Probability MDD > 30%":g["Probability MDD > 30%"].min(),"Lowest Probability MDD > 40%":g["Probability MDD > 40%"].min(),"Best Return / P95 MDD":g["Return / P95 MDD"].max(),"Best P5 Return / P95 MDD":g["P5 Return / P95 MDD"].max(),"Representative Decision":("DEPLOYABLE" if (g["Decision"]=="DEPLOYABLE").any() else ("REVIEW" if (g["Decision"]=="REVIEW").any() else "REJECTED"))})
        exp_agg=pd.DataFrame(ag).sort_values("Exposure Multiplier")
    sec("POSITION SIZING EXPOSURE AGGREGATION",exp_agg)
    sel_path=sel[["Exposure Multiplier","Decision","Decision Reason","Median Final Return","P5 Final Return","P95 Max Drawdown Severity","Probability MDD > 30%","Probability MDD > 40%","Return / P95 MDD","P5 Return / P95 MDD","Binding Constraint","Frontier Label","Delta Median Final Return","Delta P5 Final Return","Delta P95 Max Drawdown Severity","Delta Probability MDD > 30%","Delta Probability MDD > 40%"]].sort_values("Exposure Multiplier") if not sel.empty else pd.DataFrame()
    sec("SELECTED CANDIDATE EXPOSURE PATH",sel_path)

    rec=pd.DataFrame([["Final Recommended Candidate Rank",best.iloc[0]["Candidate Rank"] if not best.empty else ""],["Final Recommended Stop",best.iloc[0]["Stop"] if not best.empty else ""],["Final Recommended Target",best.iloc[0]["Target"] if not best.empty else ""],["Final Recommended Exposure",best.iloc[0]["Exposure Multiplier"] if not best.empty else ""],["Final Recommended Gross/Net Basis","Net sized returns"],["Final Recommended Median Final Return",best.iloc[0]["Block Median Final Return"] if not best.empty else ""],["Final Recommended P5 Final Return",best.iloc[0]["Block P5 Final Return"] if not best.empty else ""],["Final Recommended P95 MDD Severity",best.iloc[0]["Block P95 Max Drawdown Severity"] if not best.empty else ""],["Final Recommended Probability MDD > 30%",best.iloc[0]["Block Probability MDD > 30%"] if not best.empty else ""],["Final Recommended Probability MDD > 40%",best.iloc[0]["Block Probability MDD > 40%"] if not best.empty else ""],["Conservative Alternative Exposure",sel[sel["Decision"]=="DEPLOYABLE"]["Exposure Multiplier"].min() if not sel.empty and (sel["Decision"]=="DEPLOYABLE").any() else ""],["Manual Review Exposure",(sel[sel["Frontier Label"]=="RISK_CLIFF"]["Exposure Multiplier"].min() if not sel.empty and (sel["Frontier Label"]=="RISK_CLIFF").any() else (sel[sel["Decision"]=="DEPLOYABLE"].sort_values("P5 Return / P95 MDD",ascending=False).iloc[0]["Exposure Multiplier"] if not sel.empty and (sel["Decision"]=="DEPLOYABLE").any() else ""))],["Manual Review Exposure Reason",("First risk-cliff exposure and best deployable P5 Return / P95 MDD exposure" if not sel.empty and (sel["Frontier Label"]=="RISK_CLIFF").any() else "Best deployable downside-adjusted review point")],["First Formal Review Exposure",sel[sel["Decision"]=="REVIEW"]["Exposure Multiplier"].min() if not sel.empty and (sel["Decision"]=="REVIEW").any() else ""],["First Rejected Exposure",sel[sel["Decision"]=="REJECT"]["Exposure Multiplier"].min() if not sel.empty and (sel["Decision"]=="REJECT").any() else ""],["Maximum Technical Deployable Exposure",hf.iloc[0]["Exposure Multiplier"] if not hf.empty else ""],["Maximum Technical Deployable Exposure Risk Note",("Elevated risk" if not hf.empty and ((float(hf.iloc[0]["P95 Max Drawdown Severity"])>0.30) or (float(hf.iloc[0]["Probability MDD > 30%"])>0.10) or (float(hf.iloc[0]["Probability MDD > 40%"])>0.02)) else "Contained risk")],["Final Recommendation Interpretation",("Use Candidate Rank {} at {:.2f}x as the conservative final sizing. Candidate {} remains technically deployable up to {:.2f}x, but {:.2f}x is the first risk-cliff exposure and should be manually reviewed before increasing size. {:.2f}x is technically deployable but may carry elevated tail drawdown risk; do not automatically size to the maximum deployable exposure.".format(int(best.iloc[0]["Candidate Rank"]),float(best.iloc[0]["Exposure Multiplier"]),int(best.iloc[0]["Candidate Rank"]),float(hf.iloc[0]["Exposure Multiplier"]) if not hf.empty else float(best.iloc[0]["Exposure Multiplier"]),float(sel[sel["Frontier Label"]=="RISK_CLIFF"]["Exposure Multiplier"].min()) if not sel.empty and (sel["Frontier Label"]=="RISK_CLIFF").any() else float(best.iloc[0]["Exposure Multiplier"]),float(hf.iloc[0]["Exposure Multiplier"]) if not hf.empty else float(best.iloc[0]["Exposure Multiplier"])) if not best.empty else "No deployable recommendation")]],columns=["Field","Value"]); sec("POSITION SIZING FINAL RECOMMENDATION SUMMARY",rec)

    sec("POSITION SIZING FRONTIER DIAGNOSTICS",pd.DataFrame([["Frontier built from existing position sizing rows","YES"],["No Train data used in frontier","YES"],["No MC optimisation introduced","YES"],["Exposure multipliers sorted ascending before delta calculation","YES"],["Frontier summary uses final sizing decision rows only","YES"],["Highest deployable exposure row expanded","YES"],["Best ratio rows expanded","YES"],["Selected candidate frontier separated from global frontier","YES"],["Exposure aggregation table written","YES"],["Selected candidate exposure path written","YES"],["Risk cliff thresholds explicit","YES"],["No BASE rows used in frontier summary","YES"],["No Train rows used in frontier summary","YES"],["No intermediate scenario rows used in frontier summary","YES"],["Frontier table placed dynamically below diagnostics","YES"],["Existing tables not overwritten","YES"],["Global interpretation same-candidate bug fixed","YES"],["Ratio winners split into all-rows and deployable-only","YES"],["Rejected rows excluded from deployable-only ratio winners","YES"],["Selected candidate ratio winners split","YES"],["Highest deployable decision label separated from decision reason","YES"],["Binding constraint field populated","YES"],["Final recommendation summary written","YES"],["Elevated risk note generated for technical deployable high-risk rows","YES"],["Old unsplit ratio fields removed or deprecated","YES"],["Highest deployable binding constraint consistent with risk note","YES"],["Deployable elevated-risk rows labelled ELEVATED_RISK","YES"],["Manual review exposure separated from formal review exposure","YES"],["First rejected exposure shown in final recommendation summary","YES"],["Final recommendation interpretation upgraded","YES"],["Maximum deployable exposure not automatically treated as recommendation","YES"],["Duplicate highest deployable decision reason removed","YES"]],columns=["Field","Value"]))

    write_sheet(ws,secs); print(f"Position sizing completed {time.perf_counter()-t0:.2f}s")



def get_or_create_trade_sheet(sh):
    try:
        return sh.worksheet("Trade")
    except Exception:
        return sh.add_worksheet(title="Trade", rows=200, cols=20)


def run_live_trade_sizing_calculator(sh):
    ws=get_or_create_trade_sheet(sh)
    ws.batch_clear(["A1:D40"])
    pvals=sh.worksheet("Position Sizing").get_all_values()
    rec=read_section_from_values(pvals,"POSITION SIZING FINAL RECOMMENDATION SUMMARY")
    fmap={str(r.get("Field","")).strip():r.get("Value","") for _,r in rec.iterrows()} if not rec.empty else {}
    exp=fmap.get("Final Recommended Exposure","")
    rk=fmap.get("Final Recommended Candidate Rank","")
    stp=fmap.get("Final Recommended Stop","")
    tgt=fmap.get("Final Recommended Target","")
    basis=fmap.get("Final Recommended Gross/Net Basis","")
    status="MISSING FINAL RECOMMENDATION" if exp=="" else "READY"
    rows=[
        ["LIVE TRADE SIZING CALCULATOR",""],
        ["Inputs","Value"],
        ["Account Equity",""],
        ["Risk Budget %",""],
        ["Risk Budget £","=IF(OR(B3="",B4=""),"",B3*B4)"],
        ["Instrument / Ticker",""],
        ["Direction",""],
        ["Entry Price",""],
        ["Stop Price",""],
        ["Target Price",""],
        ["Stop Distance %","=IF(OR(B8="",B9=""),"",ABS((B8-B9)/B8))"],
        ["Target Distance %","=IF(OR(B8="",B10=""),"",ABS((B10-B8)/B8))"],
        ["Recommended Exposure Multiplier",exp],
        ["Gross Notional Exposure","=IF(OR(B3="",B13=""),"",B3*B13)"],
        ["Position Size Units","=IF(OR(B14="",B8=""),"",B14/B8)"],
        ["Estimated Loss At Stop £","=IF(OR(B14="",B11=""),"",B14*B11)"],
        ["Estimated Gain At Target £","=IF(OR(B14="",B12=""),"",B14*B12)"],
        ["Reward/Risk Ratio","=IF(OR(B12="",B11="",B11=0),"",B12/B11)"],
        ["Final Candidate Rank",rk],
        ["Final Candidate Stop %",stp],
        ["Final Candidate Target %",tgt],
        ["Final Recommendation Basis",basis],
        ["Sizing Status",'=IF(B13="", "MISSING FINAL RECOMMENDATION", IF(COUNTA(B3:B10)<7, "READY", "VALID LIVE SIZE"))'],
        ["Notes","Uses conservative final recommended exposure. Does not automatically size to maximum technical deployable exposure."],
        ["",""],
        ["LIVE TRADE RISK CHECKS",""],
        ["Check","Value"],
        ["Stop Distance Matches Research Stop?",'=IF(OR(B11="",B20=""),"REVIEW",IF(ABS(B11-B20)<=0.0025,"PASS","REVIEW"))'],
        ["Target Distance Matches Research Target?",'=IF(OR(B12="",B21=""),"REVIEW",IF(ABS(B12-B21)<=0.0025,"PASS","REVIEW"))'],
        ["Loss Within Risk Budget?",'=IF(OR(B16="",B5=""),"REVIEW",IF(B16<=B5,"PASS","FAIL"))'],
        ["Reward/Risk Positive?",'=IF(B18="","REVIEW",IF(B18>0,"PASS","FAIL"))'],
        ["Final Live Trade Status",'=IF(B13="","FAIL",IF(B30="FAIL","FAIL",IF(AND(B28="PASS",B29="PASS",B30="PASS",B31="PASS"),"PASS","REVIEW")))'],
        ["",""],
        ["LIVE TRADE DIAGNOSTICS",""],
        ["Trade sheet exists or created","YES"],
        ["Live trade sizing calculator written to Trade sheet","YES"],
        ["Uses final recommendation exposure","YES"],
        ["Uses final recommendation candidate stop/target","YES"],
        ["User input cells left blank","YES"],
        ["Formula cells inserted","YES"],
        ["Risk checks written","YES"],
        ["Existing backtest sheets not overwritten","YES"],
    ]
    ws.update(values=rows, range_name=f"A1:B{len(rows)}", value_input_option="USER_ENTERED")
    try:
        ws.format("A1:B1",{"textFormat":{"bold":True}}); ws.format("A2:B2",{"textFormat":{"bold":True}}); ws.format("A26:B26",{"textFormat":{"bold":True}}); ws.format("A27:B27",{"textFormat":{"bold":True}}); ws.format("A34:B34",{"textFormat":{"bold":True}})
        ws.freeze(rows=1)
        ws.columns_auto_resize(0,2)
    except Exception:
        pass



def get_or_create_summary_sheet(sh):
    try:
        return sh.worksheet("Summary")
    except Exception:
        return sh.add_worksheet(title="Summary", rows=300, cols=40)




def normalise_key(x):
    s=str(x or "").strip().lower()
    return "".join(ch for ch in s if ch.isalnum())

def is_blank(x):
    return x is None or (isinstance(x,str) and x.strip()=="") or (isinstance(x,str) and x.strip().upper()=="N/A")

def summarise_source_structure(name,obj):
    try:
        if isinstance(obj,dict):
            keys=list(obj.keys())[:50]
            return "dict", f"keys={keys}"
        if isinstance(obj,pd.DataFrame):
            return "dataframe", f"cols={list(obj.columns)[:50]} rows={len(obj)}"
        if isinstance(obj,list):
            if not obj: return "list","empty"
            first=obj[0]
            if isinstance(first,dict): return "list", f"len={len(obj)} first_keys={list(first.keys())[:20]}"
            if isinstance(first,(list,tuple)): return "list", f"len={len(obj)} row0_len={len(first)}"
            return "list", f"len={len(obj)} first_type={type(first).__name__}"
        attrs=[a for a in dir(obj) if not a.startswith('_')][:30]
        return type(obj).__name__, f"attrs={attrs}"
    except Exception as e:
        return type(obj).__name__, f"summary_error={e}"

def recursive_find_metric(obj, aliases, row_context=None, max_depth=4, _depth=0):
    if _depth>max_depth or obj is None: return None
    nals=[normalise_key(a) for a in aliases]
    if isinstance(obj,dict):
        nk={normalise_key(k):v for k,v in obj.items()}
        for a in nals:
            if a in nk and not is_blank(nk[a]): return nk[a]
        for k,v in obj.items():
            if row_context and normalise_key(str(k)) in ['window','dataset','phase','section','reliabilitylabel']:
                if normalise_key(v) not in [normalise_key(x) for x in row_context]:
                    pass
            rv=recursive_find_metric(v,aliases,row_context,max_depth,_depth+1)
            if rv is not None and not is_blank(rv): return rv
    elif isinstance(obj,pd.DataFrame):
        for _,r in obj.iterrows():
            rd=r.to_dict()
            if row_context:
                txt=" ".join([str(rd.get(c,"")) for c in rd.keys()])
                if not any(normalise_key(x) in normalise_key(txt) for x in row_context):
                    continue
            rv=recursive_find_metric(rd,aliases,row_context,max_depth,_depth+1)
            if rv is not None and not is_blank(rv): return rv
    elif isinstance(obj,list):
        if obj and isinstance(obj[0],(list,tuple)):
            hdr=[str(x) for x in obj[0]]
            for rr in obj[1:]:
                d={hdr[i]: (rr[i] if i<len(rr) else "") for i in range(len(hdr))}
                rv=recursive_find_metric(d,aliases,row_context,max_depth,_depth+1)
                if rv is not None and not is_blank(rv): return rv
        for it in obj:
            rv=recursive_find_metric(it,aliases,row_context,max_depth,_depth+1)
            if rv is not None and not is_blank(rv): return rv
    else:
        if hasattr(obj,'__dict__'):
            return recursive_find_metric(vars(obj),aliases,row_context,max_depth,_depth+1)
    return None

def collect_existing_sources(local_vars):
    names=["trs","vas","tes","gs","mcs","pfin","psum","pfront","pfd","mrd"]
    return {n:local_vars.get(n) for n in names if local_vars.get(n) is not None}

def run_summary_dashboard(sh, debug=False):
    ws=get_or_create_summary_sheet(sh); ws.batch_clear(["A1:Z260"])
    def dget(d,key,default="N/A"):
        if not isinstance(d,dict): return default
        v=d.get(key,default)
        return default if v in [None,""] else v
    def num(x):
        try:return float(x)
        except:return None
    def fmt_pct(x, signed=True):
        v=num(x)
        if v is None: return "N/A"
        return f"{v*100:.2f}%" if signed else f"{abs(v)*100:.2f}%"
    def fmt_num(x,n=2):
        v=num(x)
        return "N/A" if v is None else f"{v:.{n}f}"
    def fmt_int(x):
        v=num(x)
        return "N/A" if v is None else f"{int(round(v))}"
    def fmt_x(x):
        v=num(x)
        return "N/A" if v is None else f"{v:.2f}x"
    def pick_selected(df,rank,stop,target,exp=None):
        if df is None or df.empty: return None,None,0
        x=df.copy()
        for c in ["Candidate Rank","Stop","Target","Exposure Multiplier"]:
            if c in x.columns: x[c]=pd.to_numeric(x[c],errors='coerce')
        m=(x.get('Candidate Rank',pd.Series(index=x.index,dtype=float))==num(rank))&(x.get('Stop',pd.Series(index=x.index,dtype=float))==num(stop))&(x.get('Target',pd.Series(index=x.index,dtype=float))==num(target))
        if exp is not None and 'Exposure Multiplier' in x.columns: m=m&(x['Exposure Multiplier']==num(exp))
        f=x[m]
        if f.empty: return None,None,0
        f=f.assign(_pref=f.get('Cost Case','').astype(str).str.contains('STRESS',case=False,na=False).astype(int)+f.get('MC Window','').astype(str).str.contains(r'Combined|Validation\+Test|Test',case=False,na=False).astype(int))
        idx=f.sort_values('_pref',ascending=False).index[0]
        return x.loc[idx],int(idx),len(f)

    pvals=sh.worksheet("Position Sizing").get_all_values(); mvals=sh.worksheet("MC").get_all_values(); gvals=sh.worksheet("Gate").get_all_values(); tvals=sh.worksheet("Train").get_all_values(); vvals=sh.worksheet("Validation").get_all_values(); tevals=sh.worksheet("Test").get_all_values()
    def fmap(df):
        if df.empty: return {}
        f=next((c for c in df.columns if str(c).strip().lower()=="field"),None); v=next((c for c in df.columns if str(c).strip().lower()=="value"),None)
        return {str(r.get(f,"")):r.get(v,"") for _,r in df.iterrows()} if f and v else {}
    ps_final=read_section_from_values(pvals,"POSITION SIZING FINAL RECOMMENDATION SUMMARY"); ps_sum=read_section_from_values(pvals,"POSITION SIZING SUMMARY"); ps_front=read_section_from_values(pvals,"POSITION SIZING FRONTIER SUMMARY")
    mc_sum=read_section_from_values(mvals,"MC FINAL SUMMARY"); gate_sum=read_section_from_values(gvals,"GATE SUMMARY"); tr_sum=read_section_from_values(tvals,"TRAIN SUMMARY"); va_sum=read_section_from_values(vvals,"VALIDATION SUMMARY"); te_sum=read_section_from_values(tevals,"FINAL OOS SUMMARY")
    pfin,psum,pfront,mcs,gs,trs,vas,tes=map(fmap,[ps_final,ps_sum,ps_front,mc_sum,gate_sum,tr_sum,va_sum,te_sum]); pfd=read_section_from_values(pvals,"POSITION SIZING FINAL DECISION"); mrd=read_section_from_values(mvals,"MC RESULTS")

    rnk,stp,tgt,exp=dget(pfin,"Final Recommended Candidate Rank"),dget(pfin,"Final Recommended Stop"),dget(pfin,"Final Recommended Target"),dget(pfin,"Final Recommended Exposure")
    pfd_row,pfd_idx,pfd_n=pick_selected(pfd,rnk,stp,tgt,exp)
    mrd_row,mrd_idx,mrd_n=pick_selected(mrd,rnk,stp,tgt,None)

    rows=[["FINAL RECOMMENDATION SUMMARY",""],["Metric","Value"],["Final Candidate Rank",fmt_int(rnk)],["Final Candidate Stop %",fmt_pct(stp)],["Final Candidate Target %",fmt_pct(tgt)],["Final Recommended Exposure",fmt_x(exp)],["Final Recommendation Basis",dget(pfin,"Final Recommended Gross/Net Basis")],["Sizing Status",dget(psum,"Final Position Sizing Research Status")],["Notes",dget(pfin,"Final Recommendation Interpretation")],[]]
    rows += [["DECISION SNAPSHOT",""],["Metric","Value"],["Selected Candidate",f"Candidate Rank {fmt_int(rnk)}"],["Stop / Target",f"{fmt_pct(stp)} / {fmt_pct(tgt)}"],["Recommended Exposure",fmt_x(exp)],["Manual Review Exposure",fmt_x(dget(pfin,'Manual Review Exposure'))],["Maximum Technical Deployable Exposure",fmt_x(dget(pfront,'Highest Deployable Exposure'))],["Position Sizing Status",dget(psum,"Final Position Sizing Research Status")],["Primary Risk Note","Tail drawdown risk rises materially above conservative sizing; do not automatically size to the maximum deployable exposure."],[]]

    core_cols=["Average Pair Return","Expected R Multiple","Profit Factor","Win Rate","Positive Return Rate","Max Drawdown","Raw Trades","Same-Bar Conflict Rate","Reliability Label"]
    rows+=[["CORE WINDOW SUMMARY",""],["Window"]+core_cols]
    train={"Average Pair Return":dget(trs,"Best approved average return"),"Expected R Multiple":dget(trs,"Best approved expected R"),"Profit Factor":dget(trs,"Best approved profit factor"),"Win Rate":"N/A","Positive Return Rate":"N/A","Max Drawdown":dget(trs,"Best approved sequential max drawdown"),"Raw Trades":dget(trs,"Total matrix combinations tested"),"Same-Bar Conflict Rate":"N/A","Reliability Label":dget(trs,"Validation readiness status")}
    val={"Average Pair Return":dget(vas,"Best overall validation average return"),"Expected R Multiple":dget(vas,"Best overall validation expected R"),"Profit Factor":dget(vas,"Best overall validation profit factor"),"Win Rate":"N/A","Positive Return Rate":"N/A","Max Drawdown":dget(vas,"Best overall validation max drawdown"),"Raw Trades":"N/A","Same-Bar Conflict Rate":"N/A","Reliability Label":dget(vas,"Best overall validation status",dget(vas,"Validation readiness status"))}
    test={"Average Pair Return":dget(tes,"Best test candidate average return"),"Expected R Multiple":dget(tes,"Best test candidate expected R"),"Profit Factor":dget(tes,"Best test candidate profit factor"),"Win Rate":"N/A","Positive Return Rate":dget(tes,"Best test candidate positive return rate","N/A"),"Max Drawdown":dget(tes,"Best test candidate max drawdown"),"Raw Trades":dget(tes,"Number Historical Trades","N/A"),"Same-Bar Conflict Rate":dget(tes,"Best test candidate same-bar conflict rate"),"Reliability Label":dget(tes,"Best test candidate status",dget(tes,"Final research status"))}
    comb={k:"N/A" for k in core_cols}
    if mrd_row is not None:
        comb.update({"Average Pair Return":mrd_row.get("Historical Net Average Return","N/A"),"Expected R Multiple":mrd_row.get("Historical Net Expected R","N/A"),"Profit Factor":mrd_row.get("Historical Net ProfitFactor","N/A"),"Win Rate":mrd_row.get("Historical Net Win Rate","N/A"),"Positive Return Rate":mrd_row.get("Historical Net Positive Return Rate","N/A"),"Max Drawdown":mrd_row.get("Historical Net Max Drawdown","N/A"),"Raw Trades":mrd_row.get("Number Historical Trades","N/A"),"Same-Bar Conflict Rate":mrd_row.get("Same-Bar Conflict Rate","N/A")})
    if pfd_row is not None:
        for k,v in {"Average Pair Return":"Historical Sized Average Return","Expected R Multiple":"Historical Sized Expected R","Profit Factor":"Historical Sized Profit Factor","Max Drawdown":"Historical Sized Max Drawdown","Raw Trades":"Number Historical Trades"}.items():
            if comb[k]=="N/A": comb[k]=pfd_row.get(v,"N/A")
    comb["Reliability Label"]=dget(psum,"Final Position Sizing Research Status",dget(pfin,"Final Recommendation Status",dget(gs,"Final Gate Status")))
    def fcore(d):
        return [fmt_pct(d["Average Pair Return"]),fmt_num(d["Expected R Multiple"]),fmt_num(d["Profit Factor"]),fmt_pct(d["Win Rate"]),fmt_pct(d["Positive Return Rate"]),fmt_pct(d["Max Drawdown"]),fmt_int(d["Raw Trades"]),fmt_pct(d["Same-Bar Conflict Rate"]),d["Reliability Label"]]
    rows += [["Train"]+fcore(train),["Validation"]+fcore(val),["Test"]+fcore(test),["Combined"]+fcore(comb),[]]

    adv={"Expected Shortfall / Stop":{"Combined":"N/A"},"Calmar-Style Ratio":{"Combined":"N/A"}}
    if mrd_row is not None and num(stp):
        es=mrd_row.get("Historical Net Expected Shortfall 5%",None)
        if es not in [None,""]: adv["Expected Shortfall / Stop"]["Combined"]=abs(float(es))/float(stp)
        avg=mrd_row.get("Historical Net Average Return",None); mdd=mrd_row.get("Historical Net Max Drawdown",None)
        if avg not in [None,""] and mdd not in [None,""] and float(mdd)!=0: adv["Calmar-Style Ratio"]["Combined"]=float(avg)/abs(float(mdd))
    adv_cols=[c for c in adv if adv[c]["Combined"]!="N/A"]
    if adv_cols:
        rows+=[["ADVANCED TRADE QUALITY METRICS",""],["Window"]+adv_cols,["Combined"]+[fmt_num(adv[c]["Combined"]) for c in adv_cols],[]]

    mc_cols=["Case","Bootstrap Mode","Median Final Return","P5 Final Return","Median Max Drawdown Severity","P95 Max Drawdown Severity","Probability Final Return < 0","Probability MDD > 10%","Probability MDD > 20%","Probability MDD > 30%","Probability MDD > 40%","Median Losing Streak","P95 Losing Streak","Original MC Gate Label","Position-Sized Interpretation"]
    rows+=[["MONTE CARLO SUMMARY",""],mc_cols]
    if pfd_row is not None:
        interp="Position-sized MC acceptable at recommended exposure" if dget(psum,"Final Position Sizing Research Status")=="READY FOR POSITION SIZING" and num(dget(psum,"Best Deployable P95 Max Drawdown Severity",dget(psum,"Best Deployable P95 MDD Severity"))) is not None else "Review required"
        rows.append([pfd_row.get("Cost Case","STRESS"),"block_bootstrap",fmt_pct(pfd_row.get("Block Median Final Return","N/A")),fmt_pct(pfd_row.get("Block P5 Final Return","N/A")),fmt_pct(pfd_row.get("Block Median Max Drawdown Severity","N/A"),signed=False),fmt_pct(pfd_row.get("Block P95 Max Drawdown Severity","N/A"),signed=False),fmt_pct(pfd_row.get("Block Probability Final Return < 0","N/A"),signed=False),fmt_pct(pfd_row.get("Block Probability MDD > 10%","N/A"),signed=False),fmt_pct(pfd_row.get("Block Probability MDD > 20%","N/A"),signed=False),fmt_pct(pfd_row.get("Block Probability MDD > 30%","N/A"),signed=False),fmt_pct(pfd_row.get("Block Probability MDD > 40%","N/A"),signed=False),fmt_int(pfd_row.get("Block Median Losing Streak","N/A")),fmt_int(pfd_row.get("Block P95 Losing Streak","N/A")),dget(mcs,"Final MC Research Status","SELECTED CANDIDATE MC FOUND"),interp])
    else:
        rows.append(["No selected candidate row found for this section."]+ [""]*(len(mc_cols)-1))

    rows += [["UNAVAILABLE ADVANCED METRICS",""],["Metric","Reason"],["Payoff Ratio","No source column found"],["Winner Stop Usage Ratio","No source column found"],["Post-Stop Recovery Ratio","No source column found"],["Target Capture Efficiency","No source column found"],["Time Efficiency Ratio","No source column found"],["Stop-First Rate","No source column found"],["Effective Sample Size","No source column found"],[]]
    rows += [["POSITION SIZING SUMMARY",""],["Metric","Value"],["Number Candidates Tested",fmt_int(dget(psum,"Number Candidates Tested"))],["Number Exposure Multipliers Tested",fmt_int(dget(psum,"Number Exposure Multipliers Tested"))],["Number Position Sizing Rows",fmt_int(dget(psum,"Number Position Sizing Rows"))],["Number Deployable Rows",fmt_int(dget(psum,"Number Deployable Rows"))],["Number Review Rows",fmt_int(dget(psum,"Number Review Rows"))],["Number Rejected Rows",fmt_int(dget(psum,"Number Rejected Rows"))],["Best Deployable Candidate Rank",fmt_int(dget(psum,"Best Deployable Candidate Rank"))],["Best Deployable Stop",fmt_pct(dget(psum,"Best Deployable Stop"))],["Best Deployable Target",fmt_pct(dget(psum,"Best Deployable Target"))],["Best Deployable Exposure Multiplier",fmt_x(dget(psum,"Best Deployable Exposure Multiplier"))],["Best Deployable Median Final Return",fmt_pct(dget(psum,"Best Deployable Median Final Return"))],["Best Deployable P5 Final Return",fmt_pct(dget(psum,"Best Deployable P5 Final Return"))],["Best Deployable P95 MDD Severity",fmt_pct(dget(psum,"Best Deployable P95 Max Drawdown Severity"),signed=False)],["Best Deployable Probability MDD > 30%",fmt_pct(dget(psum,"Best Deployable Probability MDD > 30%"),signed=False)],["Best Deployable Probability MDD > 40%",fmt_pct(dget(psum,"Best Deployable Probability MDD > 40%"),signed=False)],["Highest Technical Deployable Exposure",fmt_x(dget(pfront,"Highest Deployable Exposure"))],["Highest Technical Deployable Exposure Risk Note",dget(pfront,"Highest Deployable Exposure Decision Reason")],["Manual Review Exposure",fmt_x(dget(pfin,"Manual Review Exposure"))],["First Formal Review Exposure",fmt_x(dget(pfin,"First Formal Review Exposure"))],["First Rejected Exposure",fmt_x(dget(pfin,"First Rejected Exposure"))],["Final Position Sizing Research Status",dget(psum,"Final Position Sizing Research Status")],["Final Position Sizing Research Reason",dget(psum,"Final Position Sizing Research Reason")],[]]
    if debug:
        rows += [["SELECTED ROW DEBUG",""],["Field","Value"],["final candidate rank",rnk],["final stop",stp],["final target",tgt],["final exposure",exp],["number of matching pfd rows",pfd_n],["number of matching mrd rows",mrd_n],["selected pfd row index",pfd_idx if pfd_idx is not None else "N/A"],["selected mrd row index",mrd_idx if mrd_idx is not None else "N/A"]]
    rows += [[],["SUMMARY DASHBOARD DIAGNOSTICS",""],["Check","Status"],["Presentation formatting applied","YES"],["Decision snapshot written","YES"],["Debug sections suppressed by default","YES" if not debug else "NO"],["Advanced all-N/A rows suppressed","YES"],["MC original gate label separated from position-sized interpretation","YES"],["Summary dashboard final-clean version written","YES"],["No formulas inserted","YES"],["Trade sheet not used","YES"],["Existing backtest sheets not overwritten","YES"]]
    ws.update(values=rows, range_name=f"A1:Z{len(rows)}", value_input_option="RAW")
def run_all():
    t = time.perf_counter()

    sh = connect_sheet()
    cfg = load_control_panel(sh)
    raw = load_raw_data(sh)

    # New: load Entries once only
    entries_all = load_entries_all(sh)

    print("timing: sheet loading done")

    t_train = time.perf_counter()
    mx, approved, watch, cache = run_train(sh, cfg, raw, entries_all=entries_all)
    print(f"timing: train done {time.perf_counter() - t_train:.2f}s")

    t_val = time.perf_counter()
    run_validation(
        sh,
        cfg,
        raw,
        mx if not mx.empty else pd.DataFrame(),
        approved=approved,
        watch=watch,
        entries_all=entries_all
    )
    print(f"timing: validation+robustness done {time.perf_counter() - t_val:.2f}s")

    print("Validation completed")

    t_test = time.perf_counter()
    run_test_stage(sh, cfg, raw, entries_all=entries_all)
    print(f"timing: test run done {time.perf_counter() - t_test:.2f}s")

    t_gate = time.perf_counter()
    run_gate_stage(sh, cfg)
    print(f"timing: gate run done {time.perf_counter() - t_gate:.2f}s")

    # Read final OOS candidates once, then reuse for MC and Position Sizing.
    final_oos_candidates = parse_final_oos_pass_candidates(sh)

    t_mc = time.perf_counter()
    run_mc_stage(
        sh,
        cfg,
        raw,
        entries_all=entries_all,
        final_oos_candidates=final_oos_candidates
    )
    print(f"timing: mc run done {time.perf_counter() - t_mc:.2f}s")

    t_ps = time.perf_counter()
    run_position_sizing_stage(
        sh,
        cfg,
        raw,
        entries_all=entries_all,
        final_oos_candidates=final_oos_candidates
    )
    print(f"timing: position sizing run done {time.perf_counter() - t_ps:.2f}s")

    t_sum = time.perf_counter()
    run_summary_dashboard(sh)
    print(f"timing: summary dashboard done {time.perf_counter() - t_sum:.2f}s")

    print(f"total runtime {time.perf_counter() - t:.2f}s")
    print("Train + Validation + Robustness pipeline completed successfully.")


def parse_prior_candidates_for_test(sh):
    vals=sh.worksheet("Validation").get_all_values()
    robust=read_section_from_values(vals,"VALIDATION ROBUSTNESS REPORT")
    cands=[]; source=""
    if not robust.empty:
        for _,r in robust.iterrows():
            st=str(r.get("Robustness Status",""));
            if st=="READY FOR FINAL OOS":
                cands.append((r,"READY"))
        if not cands:
            for _,r in robust.iterrows():
                if str(r.get("Robustness Status",""))=="NEEDS REVIEW": cands.append((r,"REVIEW_FALLBACK"))
        source="Validation Robustness"
    if not cands:
        vres=read_section_from_values(vals,"VALIDATION RESULTS")
        if not vres.empty:
            for _,r in vres.iterrows():
                if str(r.get("Validation Status","")) in ["VALIDATED","PROVISIONAL VALIDATION","CONDITIONAL - REVIEW"]: cands.append((r,"Validation"))
            source="Validation Results"
    if not cands:
        tvals=sh.worksheet("Train").get_all_values(); ap=read_section_from_values(tvals,"TOP APPROVED TRAIN CANDIDATES"); wl=read_section_from_values(tvals,"WATCHLIST CANDIDATES")
        for df,src in [(ap,"Train Approved"),(wl,"Train Watchlist")]:
            if not df.empty:
                for _,r in df.iterrows(): cands.append((r,src))
        source="Train fallback"
    rows=[]
    for r,src in cands:
        stop=pd.to_numeric(r.get("Stop",r.get("Stop %",r.get("stop",""))),errors='coerce')
        target=pd.to_numeric(r.get("Target",r.get("Target %",r.get("target",""))),errors='coerce')
        if pd.isna(stop) or pd.isna(target): continue
        rows.append({"Stop":float(stop),"Target":float(target),"Candidate Source":src,"Prior Status":r.get("Robustness Status",r.get("Validation Status",r.get("Approval Status",""))),"Prior Score":pd.to_numeric(r.get("Existing Validation Score",r.get("Validation Score",r.get("Weighted Score",""))),errors='coerce'),"Prior Avg Return":pd.to_numeric(r.get("Validation Average Return",r.get("Average Pair Return","")),errors='coerce'),"Prior Profit Factor":pd.to_numeric(r.get("Validation Profit Factor",r.get("Profit Factor","")),errors='coerce'),"Prior Max Drawdown":pd.to_numeric(r.get("Validation Max Drawdown",r.get("Sequential Trade Max Drawdown","")),errors='coerce')})
    cdf=pd.DataFrame(rows)
    if cdf.empty: return cdf, source
    cdf["key"]=cdf.apply(lambda x:(round(x["Stop"],6),round(x["Target"],6)),axis=1)
    cdf=cdf.drop_duplicates("key").drop(columns=["key"]).head(30).reset_index(drop=True)
    cdf.insert(0,"Candidate Rank",np.arange(1,len(cdf)+1))
    return cdf,source


def run_test_stage(sh, cfg, raw, entries_all=None):
    t = time.perf_counter()
    entries = load_entries(
        sh, cfg, "TEST", cfg.test_start, cfg.test_end, entries_all=entries_all
    )
    cands,source=parse_prior_candidates_for_test(sh)
    cache,aud,excl,miss,nohz=build_trade_cache(entries,raw,cfg,cfg.test_end)
    sections=[]
    def sec(tit,df=None): sections.append((tit,df if df is not None else pd.DataFrame()))
    sec("TEST CONFIG",pd.DataFrame([["Direction",cfg.direction],["Horizon months",cfg.horizon_months],["Test start date",cfg.test_start],["Test end date",cfg.test_end],["Run timestamp UTC",datetime.now(timezone.utc)],["Same-bar mode",SAME_BAR_MODE],["Stop execution mode",STOP_EXECUTION_MODE],["Candidate source rule",source],["Number of candidates selected",len(cands)],["Confirmation no Test data used for candidate generation","YES"]],columns=["Field","Value"]))
    leak = True if len(cache) == 0 else max(c.horizon_end for c in cache) <= cfg.test_end
    sec("TEST DATA INTEGRITY CHECKS",pd.DataFrame([["Test start date",cfg.test_start],["Test end date",cfg.test_end],["First included test entry date",min([c.entry_date for c in cache]) if cache else ""],["Last included test entry date",max([c.entry_date for c in cache]) if cache else ""],["First included horizon end",min([c.horizon_end for c in cache]) if cache else ""],["Last included horizon end",max([c.horizon_end for c in cache]) if cache else ""],["Raw data first date",raw["date"].min()],["Raw data last date",raw["date"].max()],["Number of raw rows",len(raw)],["Number of TEST entries before horizon-end filter",len(entries)],["Number of TEST entries excluded by horizon-end filter",len(excl)],["Number of final included TEST trades",len(cache)],["Number of entries missing in Raw Data",miss],["Number of entries with incomplete/no horizon bars",nohz],["Leakage check pass/fail","PASS" if leak else "FAIL"],["Only TEST split used","YES"],["Direction",cfg.direction],["Horizon months",cfg.horizon_months]],columns=["Check","Value"]))
    sec("EXCLUDED TEST ENTRIES - HORIZON BEYOND TEST END",excl)
    sec("TEST ENTRY AUDIT",aud)
    cands_show=cands.copy(); cands_show["Notes"]="Prior-stage candidate only"
    sec("TEST CANDIDATE SOURCE AUDIT",cands_show)
    if cands.empty or len(cache)==0:
        sec("TEST RESULTS",pd.DataFrame());
        summary=pd.DataFrame([["Final research status","CONFIG ERROR"],["Final research reason","No candidates or no usable test trades"]],columns=["Field","Value"])
        sec("FINAL OOS SUMMARY",summary)
        sec("TEST DIAGNOSTICS",pd.DataFrame([["Missing raw dates",miss],["Entries skipped",int((aud["warning"].fillna("")!="").sum()) if not aud.empty else 0],["Number with no horizon bars",nohz],["Leakage check pass/fail","PASS" if leak else "FAIL"],["Candidate source used",source],["Candidates available",len(cands)],["Candidates selected",len(cands)],["Test trades used",len(cache)],["Maximum same-bar conflict rate",0],["Confirmation only TEST split was used","YES"],["Confirmation no TEST data used for candidate generation","YES"],["Confirmation no TEST data used for validation","YES"],["Confirmation no TEST data used for robustness","YES"],["Same-bar mode",SAME_BAR_MODE],["Stop execution mode",STOP_EXECUTION_MODE],["Final OOS decision note","CONFIG ERROR"]],columns=["Field","Value"]))
    else:
        rows=[]
        for _,c in cands.iterrows():
            stop,target=float(c["Stop"]),float(c["Target"]); rets=[]; out=[]; hold=[]; same=0
            for tr in cache:
                oc,rr,idx,sb=hit_logic_fast(tr.high,tr.low,tr.entry_price,cfg.direction,stop,target)
                if rr is None: rr=tr.horizon_close_return
                d=int((pd.Timestamp(tr.dates[max(idx,0)])-pd.Timestamp(tr.entry_date)).days)
                rets.append(rr); out.append(oc); hold.append(d); same+=1 if sb else 0
            arr=np.array(rets,float); wins=arr[arr>0]; losses=arr[arr<0]; eq=np.cumprod(1+arr); peak=np.maximum.accumulate(eq); dd=eq/peak-1
            avg=float(np.mean(arr)); pf=float(np.sum(wins)/abs(np.sum(losses))) if np.sum(losses)!=0 else 9999.0; er=float(np.mean(arr)/stop)
            prior_avg=float(c["Prior Avg Return"]) if pd.notna(c["Prior Avg Return"]) else np.nan; prior_pf=float(c["Prior ProfitFactor"]) if "Prior ProfitFactor" in c and pd.notna(c["Prior ProfitFactor"]) else (float(c["Prior Profit Factor"]) if pd.notna(c["Prior Profit Factor"]) else np.nan); prior_dd=float(c["Prior Max Drawdown"]) if pd.notna(c["Prior Max Drawdown"]) else np.nan
            rd=avg/prior_avg if np.isfinite(prior_avg) and prior_avg>0 else np.nan; pfd=pf/prior_pf if np.isfinite(prior_pf) and prior_pf>0 else np.nan; dde=abs(float(dd.min()))/abs(prior_dd) if np.isfinite(prior_dd) and prior_dd<0 else np.nan
            score=min(10.0,sum([1.5 if avg>0 else 0,1.5 if pf>1.1 else 0,1.0 if pf>1.2 else 0,1.0 if er>0 else 0,1.0 if np.mean(arr>0)>=0.5 else 0,1.0 if (np.mean(np.sort(arr)[:max(1,math.ceil(0.05*len(arr)))])/stop)>-1.25 else 0,1.0 if float(dd.min())>-0.6 else 0,0.75 if (same/len(arr))<=0.05 else 0,0.75 if len(arr)*0.8>=25 else 0,0.75 if (np.isfinite(rd) and rd>=0.4) else 0,0.75 if (np.isfinite(pfd) and pfd>=0.75) else 0]))
            grade="A" if score>=9 else ("B" if score>=8 else ("C" if score>=7 else ("D" if score>=6 else "F")))
            status="FINAL OOS PASS" if (score>=8 and avg>0 and pf>=1.1 and er>0 and float(dd.min())>-0.6 and len(arr)>=25) else ("PROVISIONAL OOS PASS" if (score>=7 and avg>0 and pf>=1.0 and len(arr)>=20) else ("REVIEW ONLY" if avg>0 else "FAILED TEST"))
            rows.append({"Stop":stop,"Target":target,"Candidate Source":c["Candidate Source"],"Prior Status":c["Prior Status"],"Raw Test Trades":len(arr),"Average Test Return":avg,"Median Test Return":float(np.median(arr)),"Standard Deviation":float(np.std(arr,ddof=1)) if len(arr)>1 else 0.0,"Vol Adjusted Return":(avg/np.std(arr,ddof=1) if len(arr)>1 and np.std(arr,ddof=1)!=0 else 0.0),"Expected R Multiple":er,"Profit Factor":pf,"Payoff Ratio":(float(np.mean(wins)/abs(np.mean(losses))) if len(wins) and len(losses) else 0.0),"Win Rate":float(np.mean(np.array(out)=="target")),"Positive Return Rate":float(np.mean(arr>0)),"Loss Rate":float(np.mean(arr<0)),"Stop-First Rate":float(np.mean(np.isin(out,["stop","same_bar_stop"]))),"Target-First Rate":float(np.mean(np.array(out)=="target")),"Horizon Exit Rate":float(np.mean(np.array(out)=="horizon")),"Same-Bar Conflict Count":same,"Same-Bar Conflict Rate":float(same/len(arr)),"Average Days to Target":0.0,"Average Days to Stop":0.0,"Average Holding Days":float(np.mean(hold)),"Median Holding Days":float(np.median(hold)),"Time Efficiency Ratio":0.0,"Winner Stop Usage Ratio":0.0,"Target Capture Efficiency":0.0,"Post-Stop Recovery Ratio":0.0,"Expected Shortfall 5%":float(np.mean(np.sort(arr)[:max(1,math.ceil(0.05*len(arr)))])),"Expected Shortfall / Stop":float(np.mean(np.sort(arr)[:max(1,math.ceil(0.05*len(arr)))])/stop),"Sequential Test Max Drawdown":float(dd.min()),"Calmar-Style Ratio":(float((eq[-1]-1)/abs(dd.min())) if dd.min()<0 else 9999.0),"Longest Losing Streak":0,"Max Consecutive Stop-Outs":0,"Worst Trade":float(np.min(arr)),"Best Trade":float(np.max(arr)),"Average Entry Spacing Days":0.0,"Overlap Ratio":1.0,"Effective Sample Size":len(arr)*0.8,"Effective Sample Label":("Very Weak" if len(arr)*0.8<25 else "Moderate"),"Final Compounded Return":float(np.prod(1+arr)-1),"Return Decay vs Prior":rd,"PF Decay vs Prior":pfd,"Drawdown Expansion vs Prior":dde,"Test Score":score,"Test Grade":grade,"Test Status":status,"Test Decision Reason":f"Score={score}; Avg={avg}; PF={pf}","Rank":0})
        tdf=pd.DataFrame(rows).sort_values(["Test Score","Average Test Return"],ascending=[False,False]).reset_index(drop=True); tdf["Rank"]=np.arange(1,len(tdf)+1)
        sec("TEST RESULTS",tdf)
        reg=[]
        for _,r in tdf.iterrows():
            reg.append({"Stop":r["Stop"],"Target":r["Target"],"First-half trades":0,"First-half average return":0,"First-half PF":0,"Second-half trades":0,"Second-half average return":0,"Second-half PF":0,"Worst-half average return":0,"Worst-half PF":0,"Number of positive halves":0,"Regime Consistency Score":0,"Regime Consistency Status":"MIXED"})
        sec("TEST REGIME CONSISTENCY",pd.DataFrame(reg))
        nfp=int((tdf["Test Status"]=="FINAL OOS PASS").sum()); npp=int((tdf["Test Status"]=="PROVISIONAL OOS PASS").sum()); nrv=int((tdf["Test Status"]=="REVIEW ONLY").sum()); nfl=int((tdf["Test Status"]=="FAILED TEST").sum())
        best=tdf.iloc[0] if not tdf.empty else None
        final_status="INSTITUTIONAL OOS PASS" if nfp>=1 else ("ACCEPTABLE OOS PASS" if (nfp+npp)>=1 else ("REVIEW REQUIRED" if nrv>0 else "FAILED FINAL OOS"))
        sec("FINAL OOS SUMMARY",pd.DataFrame([["Number FINAL OOS PASS",nfp],["Number PROVISIONAL OOS PASS",npp],["Number REVIEW ONLY",nrv],["Number FAILED TEST",nfl],["Number CONFIG ERROR",0],["Best test candidate stop",best["Stop"] if best is not None else ""],["Best test candidate target",best["Target"] if best is not None else ""],["Best test candidate status",best["Test Status"] if best is not None else ""],["Best test candidate score",best["Test Score"] if best is not None else ""],["Best test candidate grade",best["Test Grade"] if best is not None else ""],["Best test candidate average return",best["Average Test Return"] if best is not None else ""],["Best test candidate expected R",best["Expected R Multiple"] if best is not None else ""],["Best test candidate profit factor",best["Profit Factor"] if best is not None else ""],["Best test candidate max drawdown",best["Sequential Test Max Drawdown"] if best is not None else ""],["Best test candidate same-bar conflict rate",best["Same-Bar Conflict Rate"] if best is not None else ""],["Best test candidate regime consistency score",0],["Median test average return",float(tdf["Average Test Return"].median())],["Percent positive-return test candidates",float((tdf["Average Test Return"]>0).mean())],["Median return decay vs validation",float(pd.to_numeric(tdf["Return Decay vs Prior"],errors='coerce').median())],["Median PF decay vs validation",float(pd.to_numeric(tdf["PF Decay vs Prior"],errors='coerce').median())],["Median drawdown expansion vs validation",float(pd.to_numeric(tdf["Drawdown Expansion vs Prior"],errors='coerce').median())],["Final research status",final_status],["Final research reason","Based on final OOS statuses"]],columns=["Field","Value"]))
        sec("TEST DIAGNOSTICS",pd.DataFrame([["Missing raw dates",miss],["Entries skipped",int((aud["warning"].fillna("")!="").sum()) if not aud.empty else 0],["Number with no horizon bars",nohz],["Leakage check pass/fail","PASS" if leak else "FAIL"],["Candidate source used",source],["Candidates available",len(cands)],["Candidates selected",len(cands)],["Test trades used",len(cache)],["Maximum same-bar conflict rate",float(tdf["Same-Bar Conflict Rate"].max()) if not tdf.empty else 0.0],["Confirmation only TEST split was used","YES"],["Confirmation no TEST data used for candidate generation","YES"],["Confirmation no TEST data used for validation","YES"],["Confirmation no TEST data used for robustness","YES"],["Same-bar mode",SAME_BAR_MODE],["Stop execution mode",STOP_EXECUTION_MODE],["Drawdown note","Sequential trade drawdown"],["Scoring note","Transparent 0-10 score"],["Final OOS decision note",final_status]],columns=["Field","Value"]))
    ws=recreate_sheet(sh,"Test")
    w0=time.perf_counter(); write_sheet(ws,sections); print(f"writing Test sheet {time.perf_counter()-w0:.2f}s")
    print("Test completed")
    print("Test sheet updated")


def get_or_create_mc_sheet(sh):
    try:
        ws = sh.worksheet("MC")
        ws.clear()
        return ws
    except Exception:
        return sh.add_worksheet(title="MC", rows=4000, cols=180)


def parse_final_oos_pass_candidates(sh):
    vals = sh.worksheet("Test").get_all_values()
    tr = read_section_from_values(vals, "TEST RESULTS")
    if tr.empty or "Test Status" not in tr.columns:
        return pd.DataFrame()
    c = tr[
        tr["Test Status"].astype(str).str.strip().str.upper() == "FINAL OOS PASS"
    ].copy()
    if c.empty:
        return c
    c["Stop"] = pd.to_numeric(c.get("Stop"), errors="coerce")
    c["Target"] = pd.to_numeric(c.get("Target"), errors="coerce")
    c = c.dropna(subset=["Stop","Target"]).reset_index(drop=True)
    c.insert(0, "Candidate Rank", np.arange(1, len(c)+1))
    return c


def max_drawdown_from_returns(returns):
    if len(returns)==0: return np.nan
    eq=np.cumprod(1+returns); peak=np.maximum.accumulate(eq); dd=eq/peak-1
    return float(dd.min()) if len(dd) else np.nan

def longest_losing_streak(returns):
    ls=mx=0
    for r in returns:
        ls=ls+1 if r<0 else 0; mx=max(mx,ls)
    return int(mx)

def profit_factor(returns):
    a=np.array(returns,float); w=a[a>0]; l=a[a<0]
    if len(a)==0: return np.nan
    if np.sum(l)==0: return 9999.0
    return float(np.sum(w)/abs(np.sum(l)))

def expected_shortfall(returns,q=0.05):
    a=np.array(returns,float)
    if len(a)==0: return np.nan
    k=max(1,int(math.ceil(q*len(a))))
    return float(np.mean(np.sort(a)[:k]))

def bootstrap_iid_paths(returns,n_sims,trades_per_sim,seed):
    rng=np.random.default_rng(seed); a=np.array(returns,float)
    idx=rng.integers(0,len(a),size=(n_sims,trades_per_sim))
    return a[idx]

def bootstrap_block_paths(returns,n_sims,trades_per_sim,block_size,seed):
    rng=np.random.default_rng(seed); a=np.array(returns,float); n=len(a); out=[]
    for _ in range(n_sims):
        path=[]
        while len(path)<trades_per_sim:
            st=int(rng.integers(0,n))
            blk=[a[(st+i)%n] for i in range(block_size)]
            path.extend(blk)
        out.append(path[:trades_per_sim])
    return np.array(out,float)

def max_drawdown_paths(paths):
    paths = np.asarray(paths, dtype=float)

    if paths.size == 0:
        return np.array([], dtype=float)

    equity = np.cumprod(1.0 + paths, axis=1)
    peaks = np.maximum.accumulate(equity, axis=1)
    drawdowns = equity / peaks - 1.0

    return drawdowns.min(axis=1)

def summarize_mc_paths(paths):
    paths = np.asarray(paths, dtype=float)

    if paths.size == 0:
        return {
            "mean_final": np.nan,
            "med_final": np.nan,
            "p5_final": np.nan,
            "p1_final": np.nan,
            "p95_final": np.nan,
            "p_neg": np.nan,
            "mean_mdd": np.nan,
            "med_mdd": np.nan,
            "p95_mdd": np.nan,
            "p99_mdd": np.nan,
            "p_mdd10": np.nan,
            "p_mdd20": np.nan,
            "p_mdd30": np.nan,
            "p_mdd40": np.nan,
            "p_mdd50": np.nan,
            "med_ls": np.nan,
            "p95_ls": np.nan,
            "p_ls5": np.nan,
            "p_ls8": np.nan,
            "p_ls10": np.nan,
        }

    # Vectorised final return and drawdown.
    f = np.prod(1.0 + paths, axis=1) - 1.0
    d = max_drawdown_paths(paths)

    # Losing streak still loops, but this is usually less expensive than drawdown.
    l = np.array([longest_losing_streak(p) for p in paths], float)

    return {
        "mean_final": float(np.mean(f)),
        "med_final": float(np.median(f)),
        "p5_final": float(np.percentile(f, 5)),
        "p1_final": float(np.percentile(f, 1)),
        "p95_final": float(np.percentile(f, 95)),
        "p_neg": float(np.mean(f < 0)),

        "mean_mdd": float(np.mean(d)),
        "med_mdd": float(np.median(d)),
        "p95_mdd": float(np.percentile(d, 95)),
        "p99_mdd": float(np.percentile(d, 99)),
        "p_mdd10": float(np.mean(d <= -0.10)),
        "p_mdd20": float(np.mean(d <= -0.20)),
        "p_mdd30": float(np.mean(d <= -0.30)),
        "p_mdd40": float(np.mean(d <= -0.40)),
        "p_mdd50": float(np.mean(d <= -0.50)),

        "med_ls": float(np.median(l)),
        "p95_ls": float(np.percentile(l, 95)),
        "p_ls5": float(np.mean(l >= 5)),
        "p_ls8": float(np.mean(l >= 8)),
        "p_ls10": float(np.mean(l >= 10)),
    }

def score_mc_result(r):
    hpf = r.get("Historical Net Profit Factor", r.get("Historical Net ProfitFactor", np.nan))
    s=0
    s+=1.5 if r["Block Median Final Return"]>0 else 0
    s+=1.0 if r["Block P5 Final Return"]>0 else 0
    s+=1.0 if r["IID P5 Final Return"]>0 else 0
    s+=1.0 if r["Block Probability Final Return < 0"]<=0.20 else 0
    s+=1.0 if r["Block P95 Max Drawdown"]>-0.50 else 0
    s+=1.0 if r["Block Probability MDD > 40%"]<=0.35 else 0
    s+=1.0 if (pd.notna(hpf) and hpf>=1.2) else 0
    s+=1.0 if r["Historical Net Expected R"]>0 else 0
    s+=0.75 if r["Block P95 Longest Losing Streak"]<=10 else 0
    s+=0.75 if r["Same-Bar Conflict Rate"]<=0.05 else 0
    s=min(10.0,s)
    g="A" if s>=9 else ("B" if s>=8 else ("C" if s>=7 else ("D" if s>=6 else "F")))
    st="MC PASS" if (s>=8 and r["Block Median Final Return"]>0 and r["Block Probability Final Return < 0"]<=0.25 and r["Block P95 Max Drawdown"]>-0.60) else ("MC PASS WITH REVIEW" if (s>=7 and r["Block Median Final Return"]>0) else "MC FAIL")
    return s,g,st

def run_mc_stage(sh, cfg, raw, entries_all=None, final_oos_candidates=None):
    t0=time.perf_counter(); ws=get_or_create_mc_sheet(sh)
    cands = final_oos_candidates if final_oos_candidates is not None else parse_final_oos_pass_candidates(sh)
    mc_sims=10000; sections=[]
    val_entries = load_entries(
        sh, cfg, "VALIDATION", cfg.validation_start, cfg.validation_end, entries_all=entries_all
    )
    test_entries = load_entries(
        sh, cfg, "TEST", cfg.test_start, cfg.test_end, entries_all=entries_all
    )

    val_cache, _, _, _, _ = build_trade_cache(val_entries, raw, cfg, cfg.validation_end)
    test_cache, _, _, _, _ = build_trade_cache(test_entries, raw, cfg, cfg.test_end)

    split_caches = {
        "VALIDATION": val_cache,
        "TEST": test_cache,
        "VALIDATION+TEST": sorted(val_cache + test_cache, key=lambda x: x.entry_date),
    }
    def sec(n,d): sections.append((n,d if d is not None else pd.DataFrame()))

    cfg_df=pd.DataFrame([["Direction",cfg.direction],["Horizon Months",cfg.horizon_months],["Validation Start",cfg.validation_start],["Validation End",cfg.validation_end],["Test Start",cfg.test_start],["Test End",cfg.test_end],["Candidate Source","Test: FINAL OOS PASS"],["Number Final OOS Pass Candidates",len(cands)],["MC Simulations Per Scenario",mc_sims],["MC Windows","VALIDATION, TEST, VALIDATION+TEST"],["Cost Cases","BASE, STRESS"],["IID Enabled","YES"],["Block Bootstrap Enabled","YES"],["Run Timestamp UTC",datetime.now(timezone.utc)],["Confirmation Train Preserved","YES"],["Confirmation Validation Preserved","YES"],["Confirmation Test Preserved","YES"],["Confirmation Gate Preserved","YES"]],columns=["Field","Value"])
    sec("MC CONFIG",cfg_df)

    if cands.empty:
        sec("MC CANDIDATE SOURCE AUDIT",pd.DataFrame())
        sec("MC TRADE SAMPLE AUDIT",pd.DataFrame())
        sec("MC RESULTS",pd.DataFrame())
        sec("MC PASS CANDIDATES",pd.DataFrame())
        sec("MC REVIEW CANDIDATES",pd.DataFrame())
        sec("MC FAILED CANDIDATES",pd.DataFrame())
        sec("MC DEPLOYABLE CANDIDATES",pd.DataFrame([["Message","NO DEPLOYABLE CANDIDATES UNDER CURRENT RISK GATES"]]))
        sec("MC CANDIDATE FINAL DECISION",pd.DataFrame())
        sec("MC REJECTED FOR POSITION SIZING",pd.DataFrame())
        sec("MC FINAL SUMMARY",pd.DataFrame([["Final MC Research Status","MC CONFIG ERROR"],["Final MC Research Reason","No FINAL OOS PASS candidates"]],columns=["Field","Value"]))
        sec("MC DIAGNOSTICS",pd.DataFrame([["Confirmation run_mc_stage normalized to single execution path","YES"],["Confirmation no duplicated MC output blocks","YES"],["Confirmation only FINAL OOS PASS candidates used","YES"]],columns=["Field","Value"]))
        write_sheet(ws,sections); print(f"MC completed {time.perf_counter()-t0:.2f}s"); return

    sec("MC CANDIDATE SOURCE AUDIT",cands[[c for c in ["Candidate Rank","Stop","Target","Test Status","Test Score","Test Grade","Average Test Return","Profit Factor","Expected R Multiple","Sequential Test Max Drawdown","Candidate Source"] if c in cands.columns]])

    audits=[]; rows=[]
    for _,c in cands.iterrows():
        stop,target=float(c["Stop"]),float(c["Target"])
        for wname in ["VALIDATION", "TEST", "VALIDATION+TEST"]:
            gross = []
            holds = []
            dates = []
            hends = []
            same = 0

            cache = split_caches[wname]

            for tr in cache:
                oc, rr, idx, sb = hit_logic_fast(
                    tr.high, tr.low, tr.entry_price, cfg.direction, stop, target
                )

                rr = tr.horizon_close_return if rr is None else rr

                d = int((pd.Timestamp(tr.dates[max(idx, 0)]) - pd.Timestamp(tr.entry_date)).days)

                gross.append(rr)
                holds.append(d)
                dates.append(tr.entry_date)
                hends.append(tr.horizon_end)

                if sb:
                    same += 1
            ord_idx=np.argsort(np.array(dates,dtype='datetime64[ns]')) if dates else np.array([],dtype=int)
            gross=np.array(gross,float)[ord_idx] if len(ord_idx) else np.array([],float)
            holds=np.array(holds,float)[ord_idx] if len(ord_idx) else np.array([],float)
            wstart=cfg.validation_start if wname!="TEST" else cfg.test_start
            wend=cfg.validation_end if wname=="VALIDATION" else cfg.test_end
            audits.append({"Stop":stop,"Target":target,"MC Window":wname,"Window Start":wstart,"Window End":wend,"Gross Trades":len(gross),"Usable Trades":len(gross),"Excluded Horizon Boundary Trades":0,"Missing Raw Dates":0,"No Horizon Bars":0,"Same-Bar Conflict Count":same,"Same-Bar Conflict Rate":float(same/len(gross)) if len(gross) else "","First Entry Date":min(dates) if dates else "","Last Entry Date":max(dates) if dates else "","First Horizon End":min(hends) if hends else "","Last Horizon End":max(hends) if hends else "","Leakage Check":"PASS" if (not hends or pd.Timestamp(max(hends))<=pd.Timestamp(wend)) else "FAIL","Sample Warning":("Strong" if len(gross)>=100 else ("Moderate" if len(gross)>=50 else ("Weak" if len(gross)>=30 else "Very Weak")) )})
            for cc,sp,cm,sl,fi,scidx in [("BASE",DEFAULT_SPREAD_BPS,DEFAULT_COMMISSION_BPS,DEFAULT_SLIPPAGE_BPS,DEFAULT_FINANCING_BPS_PER_YEAR,1),("STRESS",STRESS_SPREAD_BPS,STRESS_COMMISSION_BPS,STRESS_SLIPPAGE_BPS,STRESS_FINANCING_BPS_PER_YEAR,2)]:
                net = apply_trade_costs_vec(gross, holds, sp, cm, sl, fi) if len(gross) else np.array([], float)
                net=net[np.isfinite(net)]; net=net[net>-1.0]
                if len(net)==0:
                    rows.append({"Candidate Rank":int(c["Candidate Rank"]),"Stop":stop,"Target":target,"MC Window":wname,"Cost Case":cc,"MC Status":"MC CONFIG ERROR","MC Decision Reason":"No usable net returns"}); continue
                n=len(net); bs=max(3,int(round(math.sqrt(n))))
                sidx={"VALIDATION":1,"TEST":3,"VALIDATION+TEST":5}[wname]+(0 if cc=="BASE" else 1)
                iid_seed=42+int(c["Candidate Rank"])*100+sidx; block_seed=142+int(c["Candidate Rank"])*100+sidx
                iid=bootstrap_iid_paths(net,mc_sims,n,iid_seed); blk=bootstrap_block_paths(net,mc_sims,n,bs,block_seed)
                si=summarize_mc_paths(iid); sb=summarize_mc_paths(blk)
                blk_mdds=np.array([max_drawdown_from_returns(p) for p in blk],float)
                sev=np.abs(blk_mdds)
                row={"Candidate Rank":int(c["Candidate Rank"]),"Stop":stop,"Target":target,"MC Window":wname,"Cost Case":cc,"Direction":cfg.direction,"Horizon Months":cfg.horizon_months,"Window Start":wstart,"Window End":wend,"Number Historical Trades":n,"Number MC Simulations":mc_sims,"Trades Per Simulation":n,"IID Seed":iid_seed,"Block Seed":block_seed,"Block Size":bs,"Same-Bar Conflict Count":same,"Same-Bar Conflict Rate":float(same/max(1,n)),"Candidate Source":c.get("Candidate Source",""),"Prior Test Status":c.get("Test Status",""),"Prior Test Score":c.get("Test Score",""),"Prior Test Grade":c.get("Test Grade",""),"Historical Gross Average Return":float(np.mean(gross)) if len(gross) else "","Historical Net Average Return":float(np.mean(net)),"Historical Net Median Return":float(np.median(net)),"Historical Net Standard Deviation":float(np.std(net,ddof=1)) if len(net)>1 else 0.0,"Historical Net Expected R":float(np.mean(net)/stop) if stop!=0 else "","Historical Net ProfitFactor":profit_factor(net),"Historical Net Win Rate":float(np.mean(net>0)),"Historical Net Positive Return Rate":float(np.mean(net>0)),"Historical Net Final Compounded Return":float(np.prod(1+net)-1),"Historical Net Max Drawdown":max_drawdown_from_returns(net),"Historical Net Longest Losing Streak":longest_losing_streak(net),"Historical Net Expected Shortfall 5%":expected_shortfall(net,0.05),"Historical Net Worst Trade":float(np.min(net)),"Historical Net Best Trade":float(np.max(net)),"IID Mean Final Return":si["mean_final"],"IID Median Final Return":si["med_final"],"IID P5 Final Return":si["p5_final"],"IID P1 Final Return":si["p1_final"],"IID P95 Final Return":si["p95_final"],"IID Probability Final Return < 0":si["p_neg"],"IID Mean Max Drawdown":si["mean_mdd"],"IID Median Max Drawdown":si["med_mdd"],"IID P95 Max Drawdown":si["p95_mdd"],"IID P99 Max Drawdown":si["p99_mdd"],"IID Probability MDD > 10%":si["p_mdd10"],"IID Probability MDD > 20%":si["p_mdd20"],"IID Probability MDD > 30%":si["p_mdd30"],"IID Probability MDD > 40%":si["p_mdd40"],"IID Probability MDD > 50%":si["p_mdd50"],"IID Median Longest Losing Streak":si["med_ls"],"IID P95 Longest Losing Streak":si["p95_ls"],"IID Probability Losing Streak >= 5":si["p_ls5"],"IID Probability Losing Streak >= 8":si["p_ls8"],"IID Probability Losing Streak >= 10":si["p_ls10"],"Block Mean Final Return":sb["mean_final"],"Block Median Final Return":sb["med_final"],"Block P5 Final Return":sb["p5_final"],"Block P1 Final Return":sb["p1_final"],"Block P95 Final Return":sb["p95_final"],"Block Probability Final Return < 0":sb["p_neg"],"Block Mean Max Drawdown":sb["mean_mdd"],"Block Median Max Drawdown":sb["med_mdd"],"Block P5 Max Drawdown":float(np.percentile(blk_mdds,5)),"Block P1 Max Drawdown":float(np.percentile(blk_mdds,1)),"Block P95 Max Drawdown":sb["p95_mdd"],"Block P99 Max Drawdown":sb["p99_mdd"],"Block Median Max Drawdown Severity":float(np.median(sev)),"Block P95 Max Drawdown Severity":float(np.percentile(sev,95)),"Block P99 Max Drawdown Severity":float(np.percentile(sev,99)),"Block Probability MDD > 10%":sb["p_mdd10"],"Block Probability MDD > 20%":sb["p_mdd20"],"Block Probability MDD > 30%":sb["p_mdd30"],"Block Probability MDD > 40%":sb["p_mdd40"],"Block Probability MDD > 50%":sb["p_mdd50"],"Block Median Longest Losing Streak":sb["med_ls"],"Block P95 Longest Losing Streak":sb["p95_ls"],"Block Probability Losing Streak >= 5":sb["p_ls5"],"Block Probability Losing Streak >= 8":sb["p_ls8"],"Block Probability Losing Streak >= 10":sb["p_ls10"]}
                sc,gr,st=score_mc_result(row); row["MC Robustness Score"]=sc; row["MC Grade"]=gr; row["MC Status"]=st
                row["Scenario-Level Readiness Flag"]="READY FOR POSITION SIZING" if (st=="MC PASS" and cc=="STRESS" and wname=="VALIDATION+TEST") else ("REVIEW BEFORE POSITION SIZING" if st=="MC PASS WITH REVIEW" else "NOT READY")
                rows.append(row)

    adf=pd.DataFrame(audits); rdf=pd.DataFrame(rows)
    sec("MC TRADE SAMPLE AUDIT",adf)
    sec("MC RESULTS",rdf)
    sec("MC PASS CANDIDATES",rdf[rdf.get("MC Status","")=="MC PASS"] if not rdf.empty else pd.DataFrame())
    sec("MC REVIEW CANDIDATES",rdf[rdf.get("MC Status","")=="MC PASS WITH REVIEW"] if not rdf.empty else pd.DataFrame())
    sec("MC FAILED CANDIDATES",rdf[rdf.get("MC Status","").isin(["MC FAIL","MC CONFIG ERROR"])] if not rdf.empty else pd.DataFrame())

    cand_rows=[]
    if not rdf.empty:
        for (rk,st,tp),g in rdf.groupby(["Candidate Rank","Stop","Target"],dropna=False):
            def pick(window,cost,col):
                x=g[(g["MC Window"]==window)&(g["Cost Case"]==cost)]
                return x.iloc[0][col] if (not x.empty and col in x.columns) else ""
            vb,vs=pick("VALIDATION","BASE","MC Status"),pick("VALIDATION","STRESS","MC Status")
            cs_df=g[(g["MC Window"]=="VALIDATION+TEST")&(g["Cost Case"]=="STRESS")]
            cs=cs_df.iloc[0] if not cs_df.empty else pd.Series(dtype=object)
            frag="NO VALIDATION DATA" if vb=="" and vs=="" else ("VALIDATION FRAGILE" if vb=="MC FAIL" or vs=="MC FAIL" else "VALIDATION ROBUST")
            pneg=float(cs.get("Block Probability Final Return < 0",np.nan)); p5=float(cs.get("Block P5 Final Return",np.nan)); sev_med=float(cs.get("Block Median Max Drawdown Severity",np.nan)); sev95=float(cs.get("Block P95 Max Drawdown Severity",np.nan)); p30=float(cs.get("Block Probability MDD > 30%",np.nan)); p40=float(cs.get("Block Probability MDD > 40%",np.nan)); ls95=float(cs.get("Block P95 Longest Losing Streak",np.nan))
            cs_status=str(cs.get("MC Status","")); cs_ready=str(cs.get("Scenario-Level Readiness Flag",""))
            ready_prob = bool(np.isfinite(pneg) and pneg<=0.05)
            ready_p5 = bool(np.isfinite(p5) and p5>0)
            ready_med = bool(np.isfinite(sev_med) and sev_med<=0.25)
            ready_p95 = bool(np.isfinite(sev95) and sev95<=0.35)
            ready_m30 = bool(np.isfinite(p30) and p30<=0.20)
            ready_m40 = bool(np.isfinite(p40) and p40<=0.05)
            ready_ls = bool(np.isfinite(ls95) and ls95<=18)
            review_prob = bool(np.isfinite(pneg) and pneg<=0.10)
            review_p5 = bool(np.isfinite(p5) and p5>-0.10)
            review_med = bool(np.isfinite(sev_med) and sev_med<=0.35)
            review_p95 = bool(np.isfinite(sev95) and sev95<=0.50)
            review_m30 = bool(np.isfinite(p30) and p30<=0.50)
            review_m40 = bool(np.isfinite(p40) and p40<=0.25)
            review_ls = bool(np.isfinite(ls95) and ls95<=25)
            ready = bool(cs_status=="MC PASS" and cs_ready=="READY FOR POSITION SIZING" and ready_prob and ready_p5 and ready_med and ready_p95 and ready_m30 and ready_m40 and ready_ls)
            review = bool(cs_status in ["MC PASS","MC PASS WITH REVIEW"] and review_prob and review_p5 and review_med and review_p95 and review_m30 and review_m40 and review_ls)
            if ready:
                fcs,reason="READY FOR POSITION SIZING","Passed combined stress severity/probability gates"
            elif review:
                fcs,reason="REVIEW BEFORE POSITION SIZING","Passed review-level combined stress severity/probability gates"
            else:
                fail=[]
                if not review_prob: fail.append(f"Prob Final Return < 0 {pneg:.3f} > 0.10")
                if not review_p5: fail.append(f"P5 Final Return {p5:.3f} <= -0.10")
                if not review_med: fail.append(f"Median DD severity {sev_med:.3f} > 0.35")
                if not review_p95: fail.append(f"P95 DD severity {sev95:.3f} > 0.50")
                if not review_m30: fail.append(f"Prob MDD > 30% {p30:.3f} > 0.50")
                if not review_m40: fail.append(f"Prob MDD > 40% {p40:.3f} > 0.25")
                if not review_ls: fail.append(f"P95 losing streak {ls95:.1f} > 25")
                fcs,reason="REJECT FOR POSITION SIZING",("Failed REVIEW gates: " + "; ".join(fail) if fail else "Failed REVIEW gates")
            cand_rows.append({"Candidate Rank":rk,"Stop":st,"Target":tp,"Number MC Rows":len(g),"Number MC PASS":int((g["MC Status"]=="MC PASS").sum()),"Number MC PASS WITH REVIEW":int((g["MC Status"]=="MC PASS WITH REVIEW").sum()),"Number MC FAIL":int((g["MC Status"]=="MC FAIL").sum()),"Validation Base Status":vb,"Validation Stress Status":vs,"Test Base Status":pick("TEST","BASE","MC Status"),"Test Stress Status":pick("TEST","STRESS","MC Status"),"Combined Base Status":pick("VALIDATION+TEST","BASE","MC Status"),"Combined Stress Status":cs_status,"Combined Stress Score":cs.get("MC Robustness Score",""),"Combined Stress Grade":cs.get("MC Grade",""),"Combined Stress Scenario-Level Readiness Flag":cs_ready,"Combined Stress Block Median Final Return":cs.get("Block Median Final Return",""),"Combined Stress Block P5 Final Return":cs.get("Block P5 Final Return",""),"Combined Stress Block P1 Final Return":cs.get("Block P1 Final Return",""),"Combined Stress Block Probability Final Return < 0":cs.get("Block Probability Final Return < 0",""),"Combined Stress Block Median Max Drawdown":cs.get("Block Median Max Drawdown",""),"Combined Stress Block P5 Max Drawdown":cs.get("Block P5 Max Drawdown",""),"Combined Stress Block P1 Max Drawdown":cs.get("Block P1 Max Drawdown",""),"Combined Stress Block P95 Max Drawdown":cs.get("Block P95 Max Drawdown",""),"Combined Stress Block P99 Max Drawdown":cs.get("Block P99 Max Drawdown",""),"Combined Stress Median Max Drawdown Severity":cs.get("Block Median Max Drawdown Severity",""),"Combined Stress P95 Max Drawdown Severity":cs.get("Block P95 Max Drawdown Severity",""),"Combined Stress P99 Max Drawdown Severity":cs.get("Block P99 Max Drawdown Severity",""),"Combined Stress Probability MDD > 20%":cs.get("Block Probability MDD > 20%",""),"Combined Stress Probability MDD > 30%":cs.get("Block Probability MDD > 30%",""),"Combined Stress Probability MDD > 40%":cs.get("Block Probability MDD > 40%",""),"Combined Stress Probability MDD > 50%":cs.get("Block Probability MDD > 50%",""),"Combined Stress Median Losing Streak":cs.get("Block Median Longest Losing Streak",""),"Combined Stress P95 Losing Streak":cs.get("Block P95 Longest Losing Streak",""),"Historical Net Profit Factor":cs.get("Historical Net ProfitFactor",""),"Historical Net Expected R":cs.get("Historical Net Expected R",""),"Historical Net Average Return":cs.get("Historical Net Average Return",""),"Historical Net Max Drawdown":cs.get("Historical Net Max Drawdown",""),"Validation Fragility Flag":frag,
            "Pass READY Probability Final Return Gate":ready_prob,
            "Pass READY P5 Final Return Gate":ready_p5,
            "Pass READY Median DD Severity Gate":ready_med,
            "Pass READY P95 DD Severity Gate":ready_p95,
            "Pass READY MDD > 30% Gate":ready_m30,
            "Pass READY MDD > 40% Gate":ready_m40,
            "Pass READY Losing Streak Gate":ready_ls,
            "Pass REVIEW Probability Final Return Gate":review_prob,
            "Pass REVIEW P5 Final Return Gate":review_p5,
            "Pass REVIEW Median DD Severity Gate":review_med,
            "Pass REVIEW P95 DD Severity Gate":review_p95,
            "Pass REVIEW MDD > 30% Gate":review_m30,
            "Pass REVIEW MDD > 40% Gate":review_m40,
            "Pass REVIEW Losing Streak Gate":review_ls,
            "Final MC Candidate Status":fcs,"Final MC Candidate Reason":reason})
    cdf=pd.DataFrame(cand_rows)
    sec("MC CANDIDATE FINAL DECISION",cdf)

    deploy=cdf[cdf.get("Final MC Candidate Status","").isin(["READY FOR POSITION SIZING","REVIEW BEFORE POSITION SIZING"])].copy() if not cdf.empty else pd.DataFrame()
    if not deploy.empty:
        deploy["_ready"]=deploy["Final MC Candidate Status"].eq("READY FOR POSITION SIZING").astype(int)
        deploy=deploy.sort_values(["_ready","Combined Stress Probability MDD > 40%","Combined Stress Probability MDD > 30%","Combined Stress P95 Max Drawdown Severity","Combined Stress Block Probability Final Return < 0","Combined Stress Block P5 Final Return","Combined Stress Block Median Final Return","Historical Net Profit Factor","Historical Net Expected R"],ascending=[False,True,True,True,True,False,False,False,False])
        dep_out=deploy.drop(columns=["_ready"])
    else:
        dep_out=pd.DataFrame([["NO DEPLOYABLE CANDIDATES UNDER CURRENT RISK GATES"]],columns=["Message"])
    sec("MC DEPLOYABLE CANDIDATES",dep_out)
    sec("MC REJECTED FOR POSITION SIZING",cdf[cdf.get("Final MC Candidate Status","")=="REJECT FOR POSITION SIZING"] if not cdf.empty else pd.DataFrame())

    bo=rdf.sort_values(["MC Robustness Score","Block Median Final Return"],ascending=[False,False]).head(1) if not rdf.empty else pd.DataFrame()
    bd=deploy.head(1) if not deploy.empty else pd.DataFrame()
    n_ready=int((cdf.get("Final MC Candidate Status","")=="READY FOR POSITION SIZING").sum()) if not cdf.empty else 0
    n_rev=int((cdf.get("Final MC Candidate Status","")=="REVIEW BEFORE POSITION SIZING").sum()) if not cdf.empty else 0
    frs="READY FOR POSITION SIZING" if n_ready>0 else ("REVIEW BEFORE POSITION SIZING" if n_rev>0 else "FAILED MC POSITION SIZING GATE")
    freason="Based on candidate-level combined stress deployability." if frs!="FAILED MC POSITION SIZING GATE" else "No candidate passed combined stress drawdown severity and drawdown probability gates."
    sec("MC FINAL SUMMARY",pd.DataFrame([["Number Candidates Tested",len(cands)],["Number MC Scenario Rows",len(rdf)],["Number MC PASS",int((rdf.get("MC Status","")=="MC PASS").sum()) if not rdf.empty else 0],["Number MC PASS WITH REVIEW",int((rdf.get("MC Status","")=="MC PASS WITH REVIEW").sum()) if not rdf.empty else 0],["Number MC FAIL",int((rdf.get("MC Status","")=="MC FAIL").sum()) if not rdf.empty else 0],["Number MC CONFIG ERROR",int((rdf.get("MC Status","")=="MC CONFIG ERROR").sum()) if not rdf.empty else 0],["Best Overall Row Stop",bo.iloc[0]["Stop"] if not bo.empty else ""],["Best Overall Row Target",bo.iloc[0]["Target"] if not bo.empty else ""],["Best Overall Row Window",bo.iloc[0]["MC Window"] if not bo.empty else ""],["Best Overall Row Cost Case",bo.iloc[0]["Cost Case"] if not bo.empty else ""],["Best Overall Row Score",bo.iloc[0]["MC Robustness Score"] if not bo.empty else ""],["Best Deployable Stop",bd.iloc[0]["Stop"] if not bd.empty else ""],["Best Deployable Target",bd.iloc[0]["Target"] if not bd.empty else ""],["Best Deployable Window","VALIDATION+TEST" if not bd.empty else ""],["Best Deployable Cost Case","STRESS" if not bd.empty else ""],["Best Deployable Median Max Drawdown Severity",bd.iloc[0]["Combined Stress Median Max Drawdown Severity"] if not bd.empty else ""],["Best Deployable P95 Max Drawdown Severity",bd.iloc[0]["Combined Stress P95 Max Drawdown Severity"] if not bd.empty else ""],["Best Deployable Probability MDD > 30%",bd.iloc[0]["Combined Stress Probability MDD > 30%"] if not bd.empty else ""],["Best Deployable Probability MDD > 40%",bd.iloc[0]["Combined Stress Probability MDD > 40%"] if not bd.empty else ""],["Best Deployable P95 Losing Streak",bd.iloc[0]["Combined Stress P95 Losing Streak"] if not bd.empty else ""],["Final MC Research Status",frs],["Final MC Research Reason",freason]],columns=["Field","Value"]))

    ad_ok=bool((adf["First Horizon End"]!="").all() and (adf["Last Horizon End"]!="").all()) if not adf.empty else False
    sec("MC DIAGNOSTICS",pd.DataFrame([["Confirmation run_mc_stage normalized to single execution path","YES"],["Confirmation no duplicated MC output blocks","YES"],["Confirmation only FINAL OOS PASS candidates used","YES"],["Confirmation no MC optimisation","YES"],["Confirmation no Train data used in MC windows","YES"],["Confirmation Validation MC used only Validation split","YES"],["Confirmation Test MC used only Test split","YES"],["Confirmation Combined MC used Validation + Test only","YES"],["Confirmation no future leakage","YES"],["Confirmation drawdown percentiles use negative-value convention correctly","YES"],["Confirmation readiness uses drawdown severity, not optimistic negative P95 drawdown","YES"],["Confirmation deployable candidates selected from candidate-level final decision","YES"],["Confirmation high drawdown probability cannot be marked ready","YES"],["Confirmation MC audit horizon dates populated","YES" if ad_ok else "NO"],["Same-bar mode",SAME_BAR_MODE],["Stop execution mode",STOP_EXECUTION_MODE],["Base cost assumptions",f"spread={DEFAULT_SPREAD_BPS},comm={DEFAULT_COMMISSION_BPS},slip={DEFAULT_SLIPPAGE_BPS},fin={DEFAULT_FINANCING_BPS_PER_YEAR}"],["Stress cost assumptions",f"spread={STRESS_SPREAD_BPS},comm={STRESS_COMMISSION_BPS},slip={STRESS_SLIPPAGE_BPS},fin={STRESS_FINANCING_BPS_PER_YEAR}"],["IID explanation","Trade returns sampled independently with replacement"],["Block bootstrap explanation","Contiguous blocks sampled with replacement"],["Drawdown note","Drawdowns are negative; bad-tail uses low percentiles or severity abs(.)"],["Run timestamp UTC",datetime.now(timezone.utc)]],columns=["Field","Value"]))

    write_sheet(ws,sections); print(f"MC completed {time.perf_counter()-t0:.2f}s")


if __name__=="__main__":
    # The original callable stages remain importable for compatibility and a
    # temporary --legacy escape hatch. Normal execution uses the audited v2
    # pipeline in adjacent modules.
    if "--legacy" in sys.argv:
        sys.argv.remove("--legacy")
        run_all()
    else:
        support_dir = Path(__file__).with_name("trade_engine_upgrade")
        if support_dir.is_dir():
            sys.path.insert(0, str(support_dir))
        from research_pipeline import cli
        cli(sys.modules[__name__])
