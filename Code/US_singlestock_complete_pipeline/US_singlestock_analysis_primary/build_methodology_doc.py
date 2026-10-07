from pathlib import Path
import json
import pandas as pd
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
import config
from src.scoring import RAW_SIGNALS
from src.transformations import DERIVED_FEATURES
from src.schema import UNAVAILABLE_CALCULATIONS

OUT=Path("US_Single_Stock_Quantitative_Methodology.docx")
DATA_DIR = Path("outputs/strategy_live") if Path("outputs/strategy_live/run_summary.json").exists() else Path("outputs")
doc=Document(); sec=doc.sections[0]
sec.page_width=Inches(8.5); sec.page_height=Inches(11)
sec.top_margin=sec.bottom_margin=sec.left_margin=sec.right_margin=Inches(1)
sec.header_distance=sec.footer_distance=Inches(.492)

def setfont(run,size=11,bold=False,italic=False,color="000000"):
    run.font.name="Calibri"; run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"),"Calibri"); run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"),"Calibri")
    run.font.size=Pt(size); run.bold=bold; run.italic=italic; run.font.color.rgb=RGBColor.from_string(color)

normal=doc.styles["Normal"]; normal.font.name="Calibri"; normal.font.size=Pt(11); normal.paragraph_format.space_after=Pt(6); normal.paragraph_format.line_spacing=1.25
for n,s,b,a,c in [("Heading 1",16,18,10,"2E74B5"),("Heading 2",13,14,7,"2E74B5"),("Heading 3",12,10,5,"1F4D78")]:
    st=doc.styles[n]; st.font.name="Calibri"; st.font.size=Pt(s); st.font.bold=True; st.font.color.rgb=RGBColor.from_string(c); st.paragraph_format.space_before=Pt(b); st.paragraph_format.space_after=Pt(a); st.paragraph_format.keep_with_next=True

def h(text,level=1): doc.add_heading(text,level=level)
def p(text,italic=False):
    q=doc.add_paragraph(); q.paragraph_format.space_after=Pt(6); q.paragraph_format.line_spacing=1.25; r=q.add_run(text); setfont(r,italic=italic)
def eq(text):
    q=doc.add_paragraph(); q.alignment=WD_ALIGN_PARAGRAPH.CENTER; q.paragraph_format.space_after=Pt(7); r=q.add_run(text); setfont(r,italic=True)

q=doc.add_paragraph(); q.alignment=WD_ALIGN_PARAGRAPH.CENTER; q.paragraph_format.space_before=Pt(72); r=q.add_run("US Single-Stock Quantitative Screening Program"); setfont(r,24,True,color="1F4D78")
q=doc.add_paragraph(); q.alignment=WD_ALIGN_PARAGRAPH.CENTER; r=q.add_run("Complete methodology, mathematics, controls and limitations"); setfont(r,13,italic=True,color="555555")
doc.add_page_break()

h("1. Purpose and current scale")
p("The program is an auditable quantitative screen using robust peer-relative evidence consolidated through duplicate groups and families. Its active strategies are Safe, High Growth Potential and Turnaround Story. The shared long/short quant composite is diagnostic, not a fourth strategy.")
s=json.loads((DATA_DIR / "run_summary.json").read_text())
p(f"Latest live run: {s['rows_loaded']:,} rows; {s['rows_eligible']:,} eligible; {s['rows_excluded']} excluded; {s['sectors']} sectors; {s['industries']} industries; {s['insufficient_data_count']} insufficient-data stocks; {s['quant_long_count']} quant longs; {s['quant_short_count']} quant shorts.")

h("2. Universe, schema and cleaning")
p("Eligible types: COM, ADR, CDN, MLP, ADS and ASR. ETFs/CEFs, blank ticker/sector/industry and unapproved types are excluded. Duplicate eligible tickers or missing/duplicate required headers stop the run. All 128 source columns and source-row identifiers are retained. Blank/invalid numeric data and infinities become missing, never zero. No winsorisation and no whole-US peer fallback are used. Percentage points remain percentage points unless explicitly divided by 100.")
eq("safe_div(A,B)=A/B only when A and B exist and B≠0; where economically required, B>0")

h("3. How the scoring system works - end to end")
p("The model does not add raw accounting numbers together. Every usable factor is first converted into a common 0-100 peer score. That normalisation makes unlike variables such as margin, leverage, momentum and market capitalisation combinable. The exact path for one stock is: validate the raw value; calculate the industry/sector peer distribution; convert the value to z and then P; orient P toward LONG and SHORT; average correlated signals inside duplicate groups; average groups into families; average available families into expanded direction scores; calculate coverage and confidence; apply hard gates and score/rank thresholds; finally apply industry and universe count limits.")
h("Stage A - raw value to peer surprise",2)
p("For each factor separately, the program asks: how unusual is this stock relative to an industry distribution that is stabilised by its broader sector? It does not compare a bank margin directly with a software margin. Small industries receive more sector influence; large industries receive more industry influence. The z-score measures distance from that blended expectation in blended standard-deviation units.")
h("Stage B - peer surprise to a common 0-100 scale",2)
p("The capped linear mapping sets the blended peer mean to 50. The cap is read from the Control Panel; observations at or beyond either cap map to 100 or 0. This prevents a single extreme observation from creating an unbounded score, but the raw data itself is not winsorised or altered.")
h("Stage C - economic direction",2)
p("A high observation is not automatically bullish. For a higher-is-better signal, P is the long score. For lower-is-better signals such as leverage, valuation multiples or receivables days, 100-P is the long score. Short evidence is always the complement. Thus the economic direction is encoded after statistical normalisation.")
h("Stage D - prevent double counting",2)
p("Several columns can measure substantially the same exposure. Rather than letting three similar return horizons count as three independent families, signals are first averaged inside named duplicate groups. A signal weight changes influence inside its group. A duplicate-group weight then changes the group's influence inside its family. Only available weights enter denominators, so missing data does not act like zero or neutral evidence.")
h("Stage E - family and total score",2)
p("A family score is a weighted average of its sufficiently available groups. A family with coverage below 60% is omitted. The expanded score is then a weighted average of available family scores. Current family weights are equal, so each admitted family has equal total influence regardless of how many raw columns it contains. This is important: a family with many related variables does not automatically dominate a small family.")
h("Stage F - confidence is separate from conviction",2)
p("Expanded score measures direction and strength; confidence measures completeness. A stock may have a high long score from sparse data yet fail the 85% confidence gate. Conversely, a fully covered stock can score near 50 and have high confidence but no directional conviction. Missing factors are omitted from score averages but reduce coverage and therefore confidence.")
h("Stage G - candidate selection",2)
p("The score is not the candidate list. A name must first have sufficient factor coverage, then meet confidence, hard balance-sheet gates, minimum direction score and industry-rank limits. Only after these tests are survivors sorted across the whole universe and capped at the requested numbers of longs and shorts.")

h("4. Fully worked numerical scoring example")
p("Assume a higher-is-better factor x=15. Its industry has n_i=12, mean 10 and population variance 4. Its sector has mean 8 and variance 9. With shrinkage k=8, the industry weight is 12/(12+8)=0.60.")
eq("Blended mean = 0.60x10 + 0.40x8 = 9.20")
p("The mixture variance includes both within-group variance and the distance between each group mean and the blended mean.")
eq("Variance = 0.60[4+(10-9.2)^2] + 0.40[9+(8-9.2)^2] = 6.96")
eq("Standard deviation = sqrt(6.96) = 2.638")
eq("z = (15-9.2)/2.638 = 2.199")
eq("P = 50 + 50(2.199/3) = 86.65")
p("Because this example is higher-is-better, its long evidence is 86.65 and short evidence is 13.35. If it were lower-is-better, the same unusually high raw value would instead give long=13.35 and short=86.65.")
h("Worked duplicate-group example",2)
p("Suppose the factor above has signal weight 2 and another available factor in the same duplicate group scores 70 with weight 1. The group score is (2x86.65+1x70)/(2+1)=81.10. If the second factor were missing, the result would be 86.65, not (2x86.65+0)/3.")
h("Worked family example",2)
p("Suppose a family contains three possible groups but only two are available: group A=81.10 with group weight 1 and group B=60 with group weight 2. Coverage is 2/3=66.7%, above the 60% gate. The family score is (1x81.10+2x60)/3=67.03. If only one group existed, coverage would be 33.3% and the family would be omitted.")
h("Worked quant-composite example",2)
p("Suppose three admitted equal-weight families have long scores 67.03, 60 and 75. Raw Long=(67.03+60+75)/3=67.34 and Raw Short=32.66. With 90% confidence, published Long=50+0.90(67.34−50)=65.61 and Short=34.39. Net=31.22; it is a directional spread, not an expected return percentage.")
h("Worked confidence example",2)
p("If factor coverage is 75% and family coverage 80%, confidence=100[(0.425x0.75)+(0.25x0.80)]/0.675=76.85%, which fails the 85% candidate gate even though the long score is strong. At 95% factor coverage and 90% family coverage, confidence=93.15%, so the confidence gate passes.")
p("A strong quant score remains separate from implementation: the stock must also pass sector-aware balance-sheet and tradability gates before it is implementation-eligible.")

h("5. Every derived feature and formula")
p("Scored means the feature enters the shared quant and retained-strategy architecture. Audit/policy-only features remain diagnostic.")
for name,spec in DERIVED_FEATURES.items():
    h(name,3); status="SCORED" if spec.family and spec.direction in {"higher_better","lower_better"} else "AUDIT/POLICY ONLY"
    p(f"Formula: {spec.formula}. Units: {spec.units}. Valid when: {spec.valid_when}. Family: {spec.family or 'none'}. Direction: {spec.direction}. Duplicate group: {spec.duplicate_group}. Status: {status}.")
p("Additional constructions: EG2=(F2 consensus EPS/F1 consensus EPS−1)×100, normally requiring both EPS values positive. EPS growth acceleration=EG2−EG1. Book/price divergence=|Last Close−Book Value per Share|/|Last Close| when both are positive. Reconstructed P/B=Last Close/Book Value. Market-cap-per-share=Market Cap/Shares Outstanding.")

h("6. Raw scored factors")
for factor,(family,direction,group) in RAW_SIGNALS.items():
    if direction in {"higher_better","lower_better"}: p(f"{factor}: family={family}; direction={direction}; duplicate group={group}.")
p("Raw current, quick and cash ratios are target-range variables, not directly scored. They enter only through the liquidity-distress policy.")

h("7. Peer-statistics mathematics in detail")
p("For factor x, let nᵢ be valid industry observations, k shrinkage strength, μᵢ/μₛ industry/sector means and σᵢ²/σₛ² their population variances. The sector must have the configured minimum observations or the factor is missing.")
eq("w=nᵢ/(nᵢ+k)")
eq("μ=wμᵢ+(1−w)μₛ")
eq("σ²=w[σᵢ²+(μᵢ−μ)²]+(1−w)[σₛ²+(μₛ−μ)²]")
eq("z=(x−μ)/σ")
eq("P=α·(shrunk empirical percentile)+(1−α)·clip(50+50·robust-z/c,0,100)")
p("P=50 is neutral. Percentile weight α, shrinkage k and cap c are governed by the current Control Panel snapshot. Robust z uses median/MAD; classical mean/standard-deviation z is the fallback when MAD is zero.")
p("The industry weight changes by factor because n_i counts only valid observations for that factor. For example, an industry may have 20 stocks but only 8 valid cash-flow values; its cash-flow weight uses 8. If k=8, n_i=8 gives w=0.5, n_i=24 gives w=0.75, and n_i=72 gives w=0.90. Setting k higher increases sector shrinkage; k=0 uses the industry wherever observations exist.")
p("Population variance uses ddof=0. A zero blended standard deviation makes z and P unavailable because there is no measurable dispersion. A stock missing x is also unavailable. Minimum sector support is governed by MIN_VALID_FACTOR_OBSERVATIONS. These rules prevent artificial scores from tiny or constant samples.")
eq("Tie-aware percentile=(average rank−1)/(n−1)×100")
eq("Robust audit z=0.6745(x−median)/MAD")
p("Industry and sector percentiles are separately calculated and blended by w. Audit fields also store means, medians, standard deviations, MADs, differences, ratios and percentage differences from peer centres.")

h("8. Directional conversion")
eq("Higher-is-better: Long=P; Short=100−P")
eq("Lower-is-better: Long=100−P; Short=P")
p("Available directional pairs sum to 100. Above 50 supports the direction, below 50 opposes it, and 50 is neutral.")

h("9. Liquidity mathematics")
p("For non-financial companies, ordinary/high liquidity is neutral and only low-tail distress is negative. Financials and Financial Services are excluded. Let z be the ratio peer z-score, t the distress threshold, b=−|z-cap| and F the floor.")
eq("Liquidity=50 when z≥t")
eq("Liquidity=F+(50−F)[clip(z,b,t)−b]/(t−b) when z<t")
p("Current t=−1, b=−3 and F=0. Severe distress approaches zero. Working-capital/sales z>2 creates a separate non-directional excess flag.")

h("10. Duplicate groups, families and shared quant composite")
eq("Group score Gg=Σ(aⱼSⱼ)/Σ(aⱼ over available signals)")
eq("Family coverage Cf=available groups/total groups")
eq("Family score Ff=Σ(bgGg)/Σ(bg over available groups), admitted only if Cf≥minimum")
p("Missing signals contribute neither numerator nor denominator. Current minimum family coverage is 60%; custom signal/group weights are blank, hence default 1.")
eq("Raw direction=Σ(WfFf,direction)/Σ(Wf over available families)")
eq("Published direction=50+(Confidence/100)(Raw direction−50)")
eq("Quant Net=Published Long−Published Short")
p("The 12 families include risk/stability. The shared composite weights sum to 100. Final scores require minimum independent-group coverage. Normally Long+Short=100 and Net=2·Long−100.")
p("Family weights operate only across admitted families. If a stock lacks one family, the remaining available family weights are renormalised. A zero-weight family contributes nothing. The model records each family sub-score, coverage and weighted contribution, enabling the final number to be reconstructed exactly.")

h("11. Confidence")
eq("Independent-group coverage=available applicable economic groups/all applicable economic groups")
eq("Family coverage=available family scores/all families")
eq("Confidence=100(wf·factor coverage+wF·family coverage)/(wf+wF)")
p("Evidence counts use economically oriented factor scores. Structurally inapplicable financial-company liquidity does not reduce coverage. Quant candidates require configured confidence.")

h("12. Redundancy and reliability controls")
p("Correlated versions of the same concept share a duplicate-exposure group before family aggregation. Missing metrics are omitted, applicable weights are renormalised, and insufficient coverage withholds the score. Published quant scores are shrunk toward neutral according to observed confidence.")

h("13. Hard gates, ranking and selection")
p("Candidates require SUFFICIENT status, confidence≥85%, directional score≥55, qualifying industry rank and every active risk gate. Debt/equity is already supplied as a ratio and is normalised by the identity transformation; standard-company gates require it to be non-negative and strictly <2.0. Financial and REIT/real-estate balance-sheet gates use sector-appropriate rules, and high-growth/turnaround exceptions are explicit. Tradability and short-readiness remain separate implementation gates.")
p("Within-industry quant long/short rankings use deterministic score, ticker and source-row tie-breaks. The direction composite remains separate from implementation gates.")

h("14. Current Control Panel")
c=pd.read_csv(DATA_DIR / "model_control_panel.csv",keep_default_na=False)
for r in c.itertuples(): p(f"{r.control} = {r.value if r.value!='' else 'BLANK / DISABLED'}")
p("The Google Sheet Control Panel is the run-time source of truth and exposes selection, coverage, peer, liquidity, confidence, risk, eligibility, validation and weight controls.")

h("15. Validation, reconciliation and outputs")
p("The run rejects scores/confidence outside 0–100, non-complementary directional scores, conflicting directions, insufficient-data candidates, breached counts/thresholds/confidence and failed hard gates.")
eq("Reconstructed P/B=Last Close/Book Value per Share")
p("A flag is raised if its relative difference from supplied P/B exceeds 15%. Market-cap-per-share=Market Cap/Shares Outstanding; a flag is raised if relative difference from price exceeds 20%. Broker rating totals are flagged if they differ from 100 by >2 percentage points. These flags are diagnostic, not exclusions.")
p("Live output updates Control Panel, Metric Registry, three strategy summaries and industry/helper tabs. The obsolete Primary Summary is deleted. Local output includes the full audit, quant preview, controls, availability registry and run summary.")

h("16. Unavailable calculations")
for item,reason in UNAVAILABLE_CALCULATIONS.items(): p(f"{item}: {reason}")

h("17. Limitations")
p("The model does not use news, sentiment, insiders, full price history, volatility, Sharpe, RSI, moving averages, options pricing, implied volatility, short interest, borrow cost, transaction costs, position sizing or portfolio construction. Optionability is only an implementation note. Results depend on the supplied point-in-time data; no source-wide as-of date is provided. Peer strength is not absolute quality. Weights and thresholds are assumptions, not empirically optimised estimates. No causal future-return relationship or backtested alpha is established. Book proximity biases toward asset-heavy firms. Long and short real-world risks differ. Independent research and backtesting are required.")

h("18. Execution")
p("After editing the Control Panel: .venv/bin/python main.py --live")
doc.core_properties.title="US Single-Stock Quantitative Screening Program — Complete Methodology"; doc.core_properties.author="Codex"
doc.save(OUT); print(OUT.resolve())
