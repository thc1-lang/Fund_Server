from pathlib import Path
import json
import pandas as pd
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

import config
from src.scoring import RAW_SIGNALS
from src.transformations import DERIVED_FEATURES
from src.schema import UNAVAILABLE_CALCULATIONS


OUT = Path("US_Single_Stock_Complete_Architecture_Report.docx")
DATA_DIR = Path("outputs/strategy_live") if Path("outputs/strategy_live/run_summary.json").exists() else Path("outputs")
doc = Document()
section = doc.sections[0]
section.page_width = Inches(8.5)
section.page_height = Inches(11)
section.top_margin = section.bottom_margin = Inches(0.72)
section.left_margin = section.right_margin = Inches(0.78)


def font(run, size=9.5, bold=False, italic=False, color="000000"):
    run.font.name = "Calibri"
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), "Calibri")
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), "Calibri")
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    run.font.color.rgb = RGBColor.from_string(color)


normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(9.5)
normal.paragraph_format.space_after = Pt(4)
normal.paragraph_format.line_spacing = 1.08
for name, size, before, after, color in [
    ("Heading 1", 15, 13, 6, "1F4E78"),
    ("Heading 2", 12, 9, 4, "2E74B5"),
    ("Heading 3", 10.5, 6, 3, "385D8A"),
]:
    style = doc.styles[name]
    style.font.name = "Calibri"
    style.font.size = Pt(size)
    style.font.bold = True
    style.font.color.rgb = RGBColor.from_string(color)
    style.paragraph_format.space_before = Pt(before)
    style.paragraph_format.space_after = Pt(after)
    style.paragraph_format.keep_with_next = True


def heading(text, level=1):
    doc.add_heading(text, level=level)


def para(text, bold_lead=None, italic=False):
    q = doc.add_paragraph()
    q.paragraph_format.space_after = Pt(4)
    q.paragraph_format.line_spacing = 1.08
    if bold_lead and text.startswith(bold_lead):
        r = q.add_run(bold_lead)
        font(r, bold=True)
        r = q.add_run(text[len(bold_lead):])
        font(r, italic=italic)
    else:
        r = q.add_run(text)
        font(r, italic=italic)
    return q


def bullet(text, level=0):
    q = doc.add_paragraph(style="List Bullet" if level == 0 else "List Bullet 2")
    q.paragraph_format.space_after = Pt(2)
    q.paragraph_format.line_spacing = 1.04
    font(q.add_run(text))


def equation(text):
    q = doc.add_paragraph()
    q.alignment = WD_ALIGN_PARAGRAPH.CENTER
    q.paragraph_format.space_after = Pt(4)
    q.paragraph_format.keep_together = True
    font(q.add_run(text), size=9.5, italic=True, color="1F1F1F")


def table(headers, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.autofit = True
    for i, value in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = str(value)
        shade = OxmlElement("w:shd")
        shade.set(qn("w:fill"), "D9EAF7")
        cell._tc.get_or_add_tcPr().append(shade)
        for r in cell.paragraphs[0].runs:
            font(r, size=8, bold=True)
    header_props = t.rows[0]._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    header_props.append(repeat)
    for values in rows:
        row = t.add_row()
        row_props = row._tr.get_or_add_trPr()
        no_split = OxmlElement("w:cantSplit")
        row_props.append(no_split)
        cells = row.cells
        for i, value in enumerate(values):
            cells[i].text = "" if value is None else str(value)
            for q in cells[i].paragraphs:
                q.paragraph_format.space_after = Pt(0)
                for r in q.runs:
                    font(r, size=7.7)
    if widths:
        for row in t.rows:
            for i, width in enumerate(widths):
                row.cells[i].width = Inches(width)
    doc.add_paragraph().paragraph_format.space_after = Pt(1)
    return t


def page_break():
    doc.add_page_break()


# Title
q = doc.add_paragraph()
q.alignment = WD_ALIGN_PARAGRAPH.CENTER
q.paragraph_format.space_before = Pt(95)
font(q.add_run("US Single-Stock Analysis Program"), size=23, bold=True, color="1F4E78")
q = doc.add_paragraph()
q.alignment = WD_ALIGN_PARAGRAPH.CENTER
font(q.add_run("Complete architecture, methodology and mathematical specification"), size=13, italic=True, color="555555")
q = doc.add_paragraph()
q.alignment = WD_ALIGN_PARAGRAPH.CENTER
q.paragraph_format.space_before = Pt(18)
font(q.add_run("Behavioral explanation only — no source-code listing"), size=10, color="666666")
page_break()


heading("1. What the system is")
para("The program is an auditable cross-sectional screening engine for US-listed single stocks. It ingests a point-in-time fundamental and market dataset, verifies the schema, builds economically interpretable features, compares every usable factor with industry and sector peers, converts heterogeneous data to a common 0–100 scale, aggregates evidence through duplicate groups and factor families, measures data confidence separately from directional conviction, applies hard risk gates, and produces tightly limited long and short candidate universes.")
para("It is a screening and ranking system, not an intrinsic-value model, return forecast, portfolio optimizer, position-sizing engine, execution algorithm, or trade recommendation. A score describes relative evidence in the supplied cross-section. It is not a predicted percentage return or probability of profit.")
para("The active strategies are Safe, High Growth Potential and Turnaround Story. Correlated signals consolidate before family aggregation, and the shared diagnostic direction composite is reliability-adjusted toward neutral.")

if (DATA_DIR / "run_summary.json").exists():
    summary = json.loads((DATA_DIR / "run_summary.json").read_text())
    keys = ["rows_loaded", "rows_eligible", "rows_excluded", "sectors", "industries", "insufficient_data_count", "quant_long_count", "quant_short_count"]
    if all(k in summary for k in keys):
        para(
            f"Latest recorded run: {summary['rows_loaded']:,} source rows; {summary['rows_eligible']:,} eligible; "
            f"{summary['rows_excluded']:,} excluded; {summary['sectors']} sectors; {summary['industries']} industries; "
            f"{summary['insufficient_data_count']:,} insufficient-data names; {summary['quant_long_count']} quant longs; "
            f"{summary['quant_short_count']} quant shorts."
        )


heading("2. System architecture at a glance")
para("The architecture is a staged DataFrame pipeline. Each stage adds columns without discarding the original source fields, so a final score can be traced back through its family contribution, duplicate group, oriented peer score, peer statistics, transformed feature, and raw source value.")
table(
    ["Stage", "Responsibility", "Result"],
    [
        ("1. Input", "Read Dataset and Control Panel from one local workbook or one authenticated Google Sheets session.", "Raw rows, workbook metadata, controls."),
        ("2. Configuration", "Reset defaults, apply runtime control values transactionally, validate all ranges and weights.", "One internally consistent model configuration."),
        ("3. Contract", "Check mandatory headers and reject duplicates or omissions.", "Known source schema."),
        ("4. Cleaning", "Normalize identity fields and coerce genuine numeric columns; non-finite values become missing.", "Typed, source-row-traceable dataset."),
        ("5. Universe", "Exclude blank identities and disallowed security types; reject duplicate eligible tickers.", "Eligible and excluded tables."),
        ("6. Features", "Calculate sign-safe ratios, yields, margins, growth, balances, flags and reconciliation fields.", "Raw plus derived feature table."),
        ("7. Peer engine", "Calculate factor-specific industry/sector distributions, shrinkage statistics, z-scores, percentiles and peer scores.", "Comparable 0–100 factor evidence."),
        ("8. Policy", "Apply asymmetric liquidity treatment and financial-sector exclusions.", "Directional liquidity evidence and flags."),
        ("9. Models", "Calculate the shared quant composite and three retained strategy scores with contribution diagnostics.", "Scores, coverage and contributions."),
        ("10. Selection", "Apply deterministic ranks and separate balance-sheet and implementation gates.", "Quant candidates and implementation readiness."),
        ("11. Validation", "Recompute invariants and reject impossible or policy-breaking output.", "Verified results or a failed run."),
        ("12. Publication", "Reconcile destination tabs, optionally batch-write live sheets, and always create local audit outputs.", "Human summaries plus full audit trail."),
    ],
    [0.7, 3.4, 2.6],
)


heading("3. Execution modes and state changes")
heading("3.1 Local or dry-run mode", 2)
para("The source is a workbook export. The program reads the Dataset tab and, when present, the Control Panel tab. It performs the entire calculation and writes local audit artifacts. It does not modify Google Sheets.")
heading("3.2 Live mode", 2)
para("The program authenticates once, reads both the source Dataset and Control Panel in the same session, validates the destination workbook and its tabs, calculates the analysis, then updates the destination in batches. Live writing is explicit: merely running the model locally never changes the shared workbook.")
heading("3.3 Safety boundaries", 2)
bullet("Source and destination workbook identity is checked. Using the same workbook is refused unless explicitly permitted by configuration.")
bullet("Required industry tabs must already exist. Missing tabs stop the write; unexpected tabs are reported but protected summary/source tabs are excluded from reconciliation.")
bullet("The complete analysis is computed and validated before publication, reducing the chance of publishing a partially calculated model.")
bullet("Each written sheet is read back at its first-cell marker. A mismatch causes failure rather than silent success.")


heading("4. Input contract, cleaning and universe")
heading("4.1 Schema contract", 2)
para("The Dataset must contain the required identity, classification, valuation, estimates, balance-sheet, cash-flow, profitability, market, broker and trading fields defined by the model contract. Duplicate column names are invalid because they would make a calculation ambiguous. Missing required columns are invalid because the program does not silently invent data.")
heading("4.2 Cleaning rules", 2)
bullet("Ticker, company name, sector, industry and security type are treated as identity text and stripped of surrounding whitespace.")
bullet("A non-identity column is converted to numeric when it contains numeric observations. Empty strings, invalid numerals and positive or negative infinity become missing values.")
bullet("Missing is never silently replaced by zero. Zero has economic meaning; missing means the observation cannot contribute.")
bullet("Every row receives a Source Row identifier corresponding to its original spreadsheet row, which is retained for audit and deterministic tie-breaking.")
bullet("Percentage-point source fields remain percentage points unless a formula explicitly converts them to decimals by dividing by 100.")
heading("4.3 Universe eligibility", 2)
para("A row is excluded using a priority reason: blank ticker, blank sector, blank industry, or security type outside the approved set. Security-type matching is case-insensitive. The current allowed types are COM, ADR, CDN, MLP, ADS and ASR. ETFs and closed-end funds are therefore excluded unless the control policy is deliberately changed.")
para("After exclusions, duplicate eligible tickers cause the run to fail. This avoids treating two rows for the same instrument as independent observations and prevents ambiguous candidate output.")


heading("5. Mathematical primitives and missing-data rules")
heading("5.1 Safe division", 2)
equation("safe_ratio(A,B) = A / B only when both values exist and B ≠ 0")
para("Where economic interpretation requires a positive base, the denominator must also be greater than zero. Invalid observations become missing rather than infinite, extreme by construction, or zero.")
heading("5.2 Governed unit normalisation", 2)
equation("decimal_percentage = source_percentage_points / 100")
para("Every model-critical field has an explicit source unit and transformation in the unit registry. Percentage-point fields use the conversion above only when their registry entry requires it. Total Debt/Equity is already a ratio and therefore uses the identity transformation: a source value of 0.03821 remains 0.03821.")
heading("5.3 Natural logarithm", 2)
equation("log(x) = ln(x), valid only for x > 0")
para("The natural log compresses right-skewed positive variables such as market capitalisation. Multiplicative changes become additive: doubling has the same log distance regardless of starting scale.")
heading("5.4 Signed logarithm", 2)
equation("signed_log(x) = sign(x) × ln(1 + |x|)")
para("This compresses magnitude while preserving zero and sign. It is useful for cash-flow values that may legitimately be positive or negative.")
heading("5.5 Positive inverse", 2)
equation("inverse_positive(x) = 1 / x only when x > 0")
para("This converts positive valuation multiples into yields. Non-positive multiples are treated as economically non-meaningful for this transformation, not as extraordinarily cheap securities.")
heading("5.6 Missing-data principle", 2)
para("A missing factor is excluded from the relevant weighted numerator and denominator. It is never entered as zero and never treated as neutral 50. However, it reduces factor or family coverage, which reduces confidence and may make the stock insufficient for selection. This separates the score implied by available evidence from the amount of evidence supporting it.")


heading("6. Complete derived-feature registry")
para("Every governed derived feature is listed below. ‘Scored’ means it may enter the expanded model when its inputs and peer statistics are valid. ‘Diagnostic/policy’ means it is retained for reconciliation, confidence, risk or a separate rule but is not automatically treated as one-sided alpha evidence.")
feature_rows = []
for name, spec in DERIVED_FEATURES.items():
    scored = bool(spec.family and spec.direction in {"higher_better", "lower_better"})
    feature_rows.append((
        name,
        spec.formula,
        spec.units,
        spec.valid_when,
        spec.family or "None",
        spec.direction,
        spec.duplicate_group,
        "Scored" if scored else "Diagnostic/policy",
    ))
table(["Feature", "Formula", "Units", "Validity", "Family", "Direction", "Duplicate group", "Use"], feature_rows)
heading("6.1 Additional constructed fields", 2)
equation("EG2 growth (%) = (F2 consensus EPS / F1 consensus EPS − 1) × 100")
para("By default both consensus EPS values must be positive. This prevents sign flips around losses from being interpreted as ordinary percentage growth. The program records a reason when the value is unavailable.")
equation("EPS growth acceleration = EG2 growth − EG1 growth")
equation("Book/price divergence = |Last Close − Book Value per Share| / |Last Close|")
equation("Reconstructed P/B = Last Close / Book Value per Share")
equation("Market capitalisation per share = Market Cap / Shares Outstanding")
para("Book/price divergence and reconstructed P/B require positive price and book value. Market-cap-per-share requires positive shares outstanding. Optionability is converted into a known true/false/unknown implementation flag and a feasibility note; it does not create alpha evidence.")


heading("7. Peer-family construction: why industries and sectors are blended")
para("Financial ratios are structurally different across industries. The system therefore avoids comparing all US stocks as one population. Each factor has its own industry and sector statistics because the number of valid observations differs by factor. A company may belong to an industry with many members but still have a small effective peer sample for a sparsely reported factor.")
para("Pure industry statistics can be unstable for small groups. Pure sector statistics can erase genuine industry structure. The model uses continuous empirical shrinkage: a small industry receives more sector influence and a large industry receives more industry influence. There is no abrupt switch at an arbitrary industry count and no whole-US fallback.")
heading("7.1 Factor-specific sample sizes", 2)
equation("nᵢ = number of valid observations for this factor in the stock’s industry")
equation("nₛ = number of valid observations for this factor in the stock’s sector")
para("The factor is unavailable when the sector has fewer than MIN_VALID_FACTOR_OBSERVATIONS; the current value is shown in the recorded Control Panel section.")
heading("7.2 Shrinkage weight", 2)
equation("w = nᵢ / (nᵢ + k)")
para("k is the shrinkage strength and is governed by the Control Panel. A higher k increases sector influence. At k=0, the industry receives full weight wherever it has observations.")
heading("7.3 Blended location", 2)
equation("μ = w μᵢ + (1 − w) μₛ")
para("μᵢ and μₛ are the factor’s industry and sector means. The same weight is used to blend the medians for robust diagnostics.")
heading("7.4 Blended dispersion", 2)
equation("σ² = w[σᵢ² + (μᵢ − μ)²] + (1 − w)[σₛ² + (μₛ − μ)²]")
equation("σ = √σ²")
para("The variance is a mixture variance, not merely a weighted average of variances. The squared distance between each component mean and the blended mean preserves dispersion caused by different industry and sector centres. Component variances use the population convention, dividing by n rather than n−1. A zero or unavailable blended standard deviation makes the z-score unavailable because the peer population has no measurable dispersion.")
heading("7.5 Standardised peer surprise", 2)
equation("z = (x − μ) / σ")
para("z measures how many blended standard deviations the stock lies above or below its peer expectation. Positive z is statistically high; negative z is statistically low. Economic desirability is handled later, because high leverage and high profitability have opposite meanings.")
heading("7.6 Capped linear peer score", 2)
equation("P = clip[50 + 50 × clip(z, −c, c) / c, 0, 100]")
para("P=50 is the blended peer mean. The current cap c is governed by the Control Panel; observations at or beyond either cap map to 100 or 0. The cap limits score influence but does not change or winsorise the underlying source observation.")
heading("7.7 Percentile and robust diagnostics", 2)
equation("Tie-aware percentile = (average rank − 1) / (n − 1) × 100")
para("The endpoints are 0 and 100. A one-observation group is assigned percentile 50. Industry and sector percentiles are calculated separately, then blended with the same industry weight.")
equation("Robust z = 0.6745 × (x − blended median) / blended MAD")
para("MAD is the median absolute deviation. The factor 0.6745 makes the robust score comparable with an ordinary z-score under a normal distribution. Robust z, medians and percentiles remain visible audit diagnostics.")
heading("7.8 Stored peer diagnostics", 2)
para("For every enriched factor, the audit table stores comparison-group description, industry and sector valid counts, industry weight, blended mean, median, standard deviation and MAD, separate and blended percentiles, ordinary and robust z-scores, 0–100 peer score, absolute differences from mean/median, ratios to mean/median, and percentage difference from the mean. These fields make each factor score independently reconstructable.")


heading("8. Direction: turning statistical rank into long and short evidence")
equation("Higher-is-better factor: Long evidence = P; Short evidence = 100 − P")
equation("Lower-is-better factor: Long evidence = 100 − P; Short evidence = P")
para("For an available factor, long and short evidence sum to 100. A value above 50 supports that direction, below 50 opposes it, and exactly 50 is neutral. The factor registry—not the sign of z alone—defines the economic meaning.")
para("Target-range and two-sided-risk features are not forced into a one-sided rule. Confidence-only variables are retained for diagnostics. This prevents the system from making unsupported claims such as ‘the highest current ratio is always best’ or ‘beta furthest from one is always bullish or bearish.’")


heading("9. Complete active raw-factor registry")
raw_rows = []
for factor, (family, direction, group) in RAW_SIGNALS.items():
    raw_rows.append((factor, family, direction, group, "Directly scored" if direction in {"higher_better", "lower_better"} else "Separate policy"))
table(["Source factor", "Family", "Direction", "Duplicate group", "Treatment"], raw_rows)
para("The active registry is dynamic: a governed raw or derived factor enters only when the required calculated peer-score column exists. The denominator for factor coverage is the active scored registry for that run. Current, quick and cash ratios are present in the raw registry as target-range variables and become directional only through the asymmetric liquidity policy described below.")


heading("10. Duplicate groups: controlling correlated evidence")
para("Multiple source columns often measure the same economic exposure. Examples include overlapping return horizons, several profitability ratios, multiple earnings-surprise observations, and related estimate revisions. If they were all counted as independent evidence, a family with many redundant columns would dominate the model. Duplicate groups create a first aggregation layer.")
equation("Group Long Gg,L = Σ(aⱼ × Lⱼ) / Σ(aⱼ over available signals)")
equation("Group Short Gg,S = Σ(aⱼ × Sⱼ) / Σ(aⱼ over available signals)")
para("aⱼ is the signal weight. Default signal weight is 1 unless overridden in the Control Panel weight map. Only signals with valid scores enter the numerator and denominator. If one of two group members is missing, the existing member retains its own value; the missing member does not dilute the result.")
para("A duplicate group is therefore an exposure bucket, not another data transformation. It controls influence while preserving all underlying factor-level audit fields.")


heading("11. Families: the second aggregation layer")
para("The model organises evidence into 12 economic families: valuation; growth; profitability and returns; balance sheet and leverage; liquidity and efficiency; earnings quality; estimates and revisions; shareholder and yield; market behaviour; broker and target; tradability; and risk and stability.")
heading("11.1 Family coverage", 2)
equation("Family internal coverage Cf = number of available duplicate groups / total configured groups in that family")
para("A family is admitted only when Cf meets MIN_FAMILY_COVERAGE, currently 60%. A family below the threshold is missing for that stock, even if one attractive group exists. This prevents a broad family score from being represented by too little of its intended evidence.")
heading("11.2 Family score", 2)
equation("Family Long Ff,L = Σ(bg × Gg,L) / Σ(bg over available groups)")
equation("Family Short Ff,S = Σ(bg × Gg,S) / Σ(bg over available groups)")
para("bg is the duplicate-group weight, defaulting to 1. Available group weights are renormalised. The model stores each family’s long score, short score and internal coverage.")


heading("12. Family weighting and shared quant composite")
para("Family weights control the influence of each admitted economic family and are editable in the Control Panel. Signals first consolidate through duplicate-exposure groups, so repeated versions of the same concept do not dominate merely because the Dataset supplies several columns.")
equation("Raw direction = Σ(Wf × Ff,direction) / Σ(Wf over admitted families)")
equation("Published direction = 50 + (Confidence / 100) × (Raw direction − 50)")
equation("Quant Net = Published Long − Published Short")
para("Weights are renormalised over valid structurally applicable families; missing data is not zero-filled. With complementary evidence, Long + Short = 100. The net score is a directional spread—not an expected return.")
para("The audit output stores every family score, configured and applied weight, weighted numerator and final contribution point. Their sum exactly reconstructs the raw score; the confidence formula exactly reconstructs the published score.")


heading("13. Coverage, evidence counts and confidence")
heading("13.1 Independent-group coverage", 2)
equation("Independent-group coverage = available applicable duplicate groups / applicable duplicate groups")
para("The final quant composite is withheld unless independent-group coverage reaches MIN_FACTOR_COVERAGE. Duplicate source fields cannot inflate completeness, and structural financial-company liquidity exclusions leave the denominator.")
heading("13.2 Top-level family coverage", 2)
equation("Top-level family coverage = number of admitted family scores / number of active families")
para("This differs from a family’s internal coverage. Internal coverage decides whether that individual family is admitted; top-level coverage measures how much of the whole architecture survived.")
heading("13.3 Confidence", 2)
equation("Confidence = 100 × (wf × factor coverage + wF × family coverage) / (wf + wF)")
para("Confidence is clipped to 0–100. It is a completeness measure, not a direction score. A stock can have high confidence and neutral conviction, or strong apparent conviction but inadequate confidence.")
heading("13.4 Evidence counts", 2)
para("The program counts economically oriented factor scores above 50, below 50 and equal to 50 as positive, negative and neutral evidence. Lower-is-better signals are reversed before counting.")


heading("14. Liquidity and working-capital policy")
para("Current ratio, quick ratio and cash ratio do not obey a simple ‘higher is always better’ rule. Extremely low liquidity can indicate distress, while very high liquidity is not automatically a bullish signal. The policy is deliberately asymmetric: ordinary or high liquidity is neutral; only the low tail creates negative evidence.")
equation("Liquidity score = 50 when z ≥ t")
equation("Liquidity score = F + (50 − F) × [clip(z,b,t) − b] / (t − b), when z < t")
para("t is the distress threshold, F is the floor, and b=−|z cap|; all are governed by the current Control Panel snapshot. The score never exceeds 50, so excess liquidity does not create positive alpha evidence.")
para("The three liquidity policy scores are inserted into the liquidity-and-efficiency family as distinct duplicate groups. Financial companies are excluded because bank and financial balance sheets make ordinary industrial liquidity ratios structurally incomparable. The excluded sectors are controlled by FINANCIAL_SECTORS.")
para("Working-capital-to-sales has a separate excessive-working-capital flag when its peer z-score exceeds the configured threshold, currently +2. This is an audit warning, not automatically a bullish or bearish score.")


heading("15. Robust peer-score blend")
equation("Peer score = α × shrunk empirical percentile + (1−α) × capped robust-z mapping")
para("α is configurable and its current value is shown in the Control Panel section. Robust z uses the peer median and MAD; classical mean/standard-deviation z is used only when MAD is zero. Raw observations remain unchanged. This makes outliers less able to distort the common 0–100 scale while retaining ordinal peer information.")


heading("16. Redundancy and missing-data safeguards")
para("Correlated signals share duplicate-exposure groups; missing signals and families contribute neither zero nor weight. Applicable weights are renormalised only after minimum-data checks. Structurally inapplicable ratios are explicitly excluded rather than counted as missing. The removed requested-core/secondary path no longer creates contradictory or redundant rankings.")


heading("17. Hard gates and risk policy")
para("A high score is necessary but not sufficient. Hard gates remove names that violate non-negotiable data or risk requirements before final selection.")
heading("17.1 Sector-aware balance-sheet gates", 2)
equation("Debt/equity ratio = source Total Debt/Equity ratio (identity normalisation)")
para("Standard companies use the configured leverage threshold. Financials and REIT/real-estate companies use sector-appropriate evidence rather than being forced through the standard-company gate. High-growth and turnaround exceptions are explicit, strategy-specific and auditable. Missing evidence never becomes zero or an automatic pass.")
heading("17.2 Book-value divergence gate", 2)
equation("Divergence = |share price − book value per share| / |share price|")
equation("Gate passes only when divergence ≤ MAX_BOOK_PRICE_DIVERGENCE")
para("This gate is optional and is currently disabled when the Control Panel cell is blank. If the desired policy is ‘no divergence,’ set the maximum to 0; then price and book value must match exactly. Because exact equality is very restrictive and sensitive to data timing and rounding, the control allows any non-negative tolerance when required. When enabled, missing price, missing book value, or invalid non-positive inputs fail the gate.")
heading("17.3 Data and score gates", 2)
bullet("Data Status must be SUFFICIENT.")
bullet("Score Confidence must meet MIN_CANDIDATE_CONFIDENCE, currently 85.")
bullet("Expanded Long or Short Score must meet its direction threshold from the Control Panel.")
bullet("The stock must qualify within its industry rank limit and then survive the global direction cap.")


heading("18. Ranking, overlap resolution and configurable universe size")
heading("18.1 Industry ranking", 2)
para("Expanded and requested scores are ranked separately by direction within each industry. Only sufficient rows participate in meaningful expanded ranking. The deterministic ordering is score descending, ticker alphabetically, then Source Row. This makes tied runs reproducible.")
heading("18.2 Per-industry limits", 2)
para("TOP_N_LONG and TOP_N_SHORT define how many candidates may qualify from each industry. They are independently changeable in the Control Panel and are currently 3 each. Changing these controls changes concentration by industry without altering any score calculation.")
heading("18.3 Long/short overlap", 2)
para("A ticker cannot be selected both long and short. If it provisionally qualifies for both directions, the direction with the higher expanded score is retained. Exact symmetry is resolved deterministically by the model ordering rather than allowing a conflict.")
heading("18.4 Universe-wide counts", 2)
para("MAX_QUANT_LONGS_TOTAL and MAX_QUANT_SHORTS_TOTAL cap diagnostic quant candidates. Investment attractiveness remains separate from balance-sheet, tradability and short-borrow implementation validation.")


heading("19. Reconciliation and diagnostic calculations")
heading("19.1 Price-to-book reconciliation", 2)
equation("Reconstructed P/B = Last Close / Book Value per Share")
equation("Relative difference = |reconstructed P/B / supplied P/B − 1|")
para("A diagnostic flag is raised when the relative difference exceeds the configured tolerance, currently 15%. This flag checks source consistency; it is distinct from the optional book/price hard gate.")
heading("19.2 Market-cap-per-share reconciliation", 2)
equation("Market-cap-per-share = Market Cap (millions) / Shares Outstanding (millions)")
equation("Relative difference from price = |market-cap-per-share / Last Close − 1|")
para("A flag is raised above the configured tolerance, currently 20%. Because both numerator and denominator use millions, the units cancel correctly to currency per share.")
heading("19.3 Broker-rating reconciliation", 2)
para("The bullish, neutral and bearish broker-rating percentages are summed. A diagnostic flag is raised when the total differs from 100 by more than the configured tolerance, currently 2 percentage points.")
para("Reconciliation flags are warnings, not automatic candidate exclusions unless a separate hard gate explicitly uses the underlying measure.")


heading("20. Control Panel: complete runtime configuration")
para("The Control Panel is the runtime source of truth for changeable model policy. Before every application, controls are reset to base defaults; supplied rows are then parsed into a temporary configuration and validated. Only a fully valid set is committed. If any value is invalid, the previous/base configuration is restored, so a partly applied panel cannot contaminate a run. Missing panel rows revert to defaults rather than retaining stale values from a previous run.")
if (DATA_DIR / "model_control_panel.csv").exists():
    controls = pd.read_csv(DATA_DIR / "model_control_panel.csv", keep_default_na=False)
    recorded = dict(zip(controls["control"], controls["value"]))
    rows = []
    for section_name, control_name, current_value, description in config.control_panel_rows():
        value = recorded.get(control_name, current_value)
        if control_name == "SIGNAL_WEIGHTS_JSON":
            value = recorded.get("SIGNAL_WEIGHTS", value)
        elif control_name == "DUPLICATE_GROUP_WEIGHTS_JSON":
            value = recorded.get("DUPLICATE_GROUP_WEIGHTS", value)
        rows.append((section_name, control_name, value if str(value) != "" else "BLANK / DISABLED", description))
    table(["Section", "Control", "Recorded value", "Meaning"], rows)
else:
    para("The generated control snapshot was unavailable, so current values could not be embedded. The live Control Panel remains authoritative.")
heading("20.1 Control validation", 2)
para("Counts must be positive integers; the peer minimum must be at least 2; coverage settings must lie from 0 to 1; confidence and score thresholds must lie from 0 to 100; shrinkage must be non-negative; z cap must be positive; the direction threshold must not exceed the z cap; debt/equity maximum must be positive; book divergence must be blank or non-negative; liquidity thresholds and floors must lie in their valid ranges; confidence weights must be non-negative with a positive total; tolerances must be non-negative; the eligible-type list cannot be empty; and all signal, group and family weights must be finite and non-negative with at least one positive family weight.")
heading("20.2 Advanced weights", 2)
para("Signal-weight and duplicate-group-weight overrides are supplied as named weight maps. Unnamed signals/groups default to 1. Family weights are separately exposed. These controls alter relative influence but do not alter peer statistics, factor direction or raw feature formulas.")


heading("21. Publication and workbook architecture")
heading("21.1 Human-facing summaries", 2)
bullet("The obsolete Primary Summary is deleted; the three retained strategy summaries remain.")
bullet("Industry Summary aggregates counts and diagnostic statistics by industry.")
bullet("Each industry tab contains diagnostics, quant-long, quant-short and full-ranking sections.")
bullet("The Control Panel is published with editable values visually distinguished from labels and descriptions.")
heading("21.2 Batch-write design", 2)
para("Sheet values are converted to JSON-safe native values. Missing and non-finite values are rendered as ‘NO DATA’; dates are rendered in ISO form. Existing merges and conditional formatting are cleared, target ranges are cleared, values are written in raw mode, and formatting is rebuilt. Requests are chunked to limit API payload size.")
heading("21.3 Retry behavior", 2)
para("Transient Google errors—request timeout, rate limiting and common 5xx responses—are retried up to six attempts. A valid Retry-After header is respected; otherwise delay increases exponentially from five seconds and is capped at sixty seconds. Connection and timeout errors follow the same retry policy. Non-transient errors are raised immediately.")
heading("21.4 Layout behavior", 2)
para("The writer expands sheets when necessary, freezes headers, applies section-specific colours and number formats, resizes columns, and caps widths. Industry section ranges are calculated from actual row counts, so long and short formatting remains aligned when candidate counts differ.")


heading("22. Local audit architecture")
para("Local outputs are produced even for live runs. They provide a durable record independent of the presentation workbook.")
bullet("Full audit CSV containing original fields, derived features, peer statistics, family scores, contributions, ranks, gates and flags.")
bullet("Parquet version when the available runtime supports it.")
bullet("Quant and industry summary CSVs.")
bullet("One collision-safe ranking preview per industry; stale previews from earlier runs are removed.")
bullet("Excluded-universe CSV with one explicit exclusion reason per row.")
bullet("Feature registry describing inputs, formulas, units, validity, family, direction and duplicate group.")
bullet("Control snapshot, unavailable-calculation registry and tab reconciliation manifest.")
bullet("JSON and CSV run summaries containing counts, metadata and output paths.")
para("Industry filenames are sanitised. Empty or colliding names receive a stable hash suffix, preventing one industry preview from overwriting another.")


heading("23. Validation and fail-closed behavior")
para("Validation is a final independent defence. A run fails instead of publishing when any invariant below is breached.")
bullet("Expanded, requested and confidence scores must stay within 0–100.")
bullet("Available expanded and requested long/short pairs must sum to 100 within numerical tolerance.")
bullet("A ticker cannot hold conflicting selected directions.")
bullet("A selected candidate must have SUFFICIENT data, pass the confidence threshold, pass score thresholds, satisfy per-industry and global count limits, and pass every active risk gate.")
bullet("Every candidate must pass the sector-aware balance-sheet gate applicable to its company type and strategy.")
bullet("When enabled, book/price divergence must be present and no greater than its maximum.")
bullet("Destination reconciliation must find every required tab before live writing begins.")
para("This is fail-closed design: missing information that is required to prove a hard condition does not count as a pass.")


heading("24. Module-by-module architecture")
module_rows = [
    ("Main orchestration", "Parses execution options, enforces mode exclusivity, runs every pipeline stage in order, reports progress, validates and returns the run summary."),
    ("Configuration", "Loads environment/base defaults, defines all control descriptions, resets and applies the Control Panel transactionally, parses booleans/lists/weight maps and validates parameter ranges."),
    ("Schema", "Defines source-field mappings, required headers, identity/date fields and the explicit unavailable-calculation registry."),
    ("Reader", "Reads local workbook tabs or Google Sheets; discovers service-account credentials; captures workbook metadata; extracts Control Panel rows below its Parameter/Value header."),
    ("Cleaner", "Normalises identity strings, converts numeric columns and attaches Source Row lineage."),
    ("Universe", "Applies eligibility rules, assigns exclusion reasons and rejects duplicate eligible tickers."),
    ("Transformations", "Implements safe numeric primitives, EG2 sign rules, the governed feature registry, feature calculation, reason columns, optionability notes and registry export."),
    ("Peer statistics", "Calculates factor-specific hierarchical statistics, mixture variance, z-scores, percentiles, robust diagnostics, common peer scores and liquidity policy fields."),
    ("Quant composite", "Orients factor evidence, aggregates signals through duplicate groups and families, calculates coverage, raw scores, contributions and reliability-adjusted scores."),
    ("Strategy overlays", "Reweights the same audited family evidence for Safe, High Growth Potential and Turnaround Story."),
    ("Ranking", "Creates deterministic industry ranks, applies sufficient-data, confidence, score, debt and book gates, resolves overlaps and enforces global long/short caps."),
    ("Validation", "Checks numeric ranges, score complementarity, candidate exclusivity, limits, thresholds, confidence, hard gates and reconciliation warnings."),
    ("Sheet writer", "Builds summaries and industry payloads, validates destinations, reconciles tabs, cleans/formats sheets, batches requests, retries transient failures and verifies writes."),
    ("Audit writer", "Writes full and summary artifacts, registries, manifests, run metadata and collision-safe industry previews."),
    ("Tests", "Exercises schema, transformations, robust peer blending, no-US fallback, redundancy groups, structural exclusions, payout/liquidity policies, ranking, gates, summaries, reconstruction and transactional controls."),
]
table(["Component", "Responsibility"], module_rows)


heading("25. Data lineage through the scoring architecture")
table(
    ["Layer", "Example", "Question answered"],
    [
        ("Raw source", "Net Margin %, Last Close, Total Debt/Equity", "What did the vendor supply?"),
        ("Derived feature", "Earnings yield, debt/equity ratio, book divergence", "What economically meaningful quantity is calculated?"),
        ("Peer statistics", "Industry/sector counts, μ, σ, z, percentile", "How does the value compare with relevant peers?"),
        ("Peer score", "P from 0 to 100", "What common-scale statistical position does it occupy?"),
        ("Oriented evidence", "Long=P or 100−P", "Is high or low economically desirable?"),
        ("Duplicate group", "Weighted mean of correlated signals", "How is repeated exposure prevented from dominating?"),
        ("Family", "Weighted group mean subject to coverage", "What does one economic theme say?"),
        ("Expanded score", "Weighted admitted-family mean", "What is the total long/short conviction?"),
        ("Confidence", "Weighted factor/family completeness", "How much of the intended model supports the score?"),
        ("Candidate", "Gates + ranks + caps", "Is the evidence strong, complete and policy-compliant enough to enter the limited universe?"),
    ],
)


heading("26. Fully worked end-to-end numerical example")
heading("26.1 Peer calculation", 2)
para("For an illustrative worked example, assume a higher-is-better factor x=15, industry nᵢ=12 with mean 10 and variance 4, sector mean 8 and variance 9, k=8 and c=3. These example parameters are pedagogical, not the live controls.")
equation("w = 12 / (12 + 8) = 0.60")
equation("μ = 0.60×10 + 0.40×8 = 9.20")
equation("σ² = 0.60[4+(10−9.2)²] + 0.40[9+(8−9.2)²] = 6.96")
equation("σ = √6.96 = 2.638;  z = (15−9.2)/2.638 = 2.199")
equation("P = 50 + 50×(2.199/3) = 86.65")
para("Because the factor is higher-is-better, Long=86.65 and Short=13.35. If it were lower-is-better, the orientations would reverse.")
heading("26.2 Duplicate group", 2)
para("Suppose this signal has weight 2 and another available signal in the same group scores 70 with weight 1.")
equation("Group Long = (2×86.65 + 1×70) / 3 = 81.10")
para("If the second factor were missing, the valid-weight denominator would be 2 and the group score would remain 86.65.")
heading("26.3 Family", 2)
para("Suppose the family has three intended groups. Two are available: group A=81.10 with weight 1 and group B=60 with weight 2.")
equation("Family internal coverage = 2/3 = 66.7%, so it passes the 60% requirement")
equation("Family Long = (1×81.10 + 2×60) / 3 = 67.03")
heading("26.4 Expanded model", 2)
para("Suppose three admitted, equal-weight family long scores are 67.03, 60 and 75.")
equation("Expanded Long = (67.03+60+75)/3 = 67.34")
equation("Expanded Short = (32.97+40+25)/3 = 32.66")
equation("Expanded Net = 67.34−32.66 = 34.68")
heading("26.5 Confidence", 2)
para("If factor coverage is 95% and top-level family coverage is 90%:")
equation("Confidence = 100×(0.425×0.95 + 0.25×0.90)/(0.425+0.25) = 93.15%")
para("A high quant rank does not by itself authorize implementation. Sector-aware balance-sheet, tradability and—when short—borrow validation are reported separately.")


heading("27. Automated test coverage and the guarantees it provides")
test_rows = [
    ("Schema contract", "Required headers are recognised and incomplete schemas fail."),
    ("Units and transformations", "Percentage-point conversion, logs, signed logs, safe ratios, inverses and EG2 sign rules behave as specified."),
    ("Peer engine", "Statistics are calculated, industry/sector shrinkage changes continuously and no whole-US fallback appears."),
    ("Registry governance", "Derived features have declared direction/group metadata; confidence-only and unavailable features cannot leak into scoring."),
    ("Universe", "ETFs/CEFs, blank peer keys and duplicate tickers are handled correctly."),
    ("Destination safety", "Exact tab matching and source/destination protections work."),
    ("Writer", "Values are serialisable, retry headers are parsed, API writes are batched, filenames are collision-safe and section formatting follows variable row counts."),
    ("Selection", "Thresholds prevent overlap, tie-breaking is deterministic, per-industry limits and global caps are enforced."),
    ("Risk gates", "Candidate confidence, debt/equity and optional book divergence are enforced."),
    ("Reliability", "Low confidence shrinks the raw quant composite toward neutral."),
    ("Liquidity", "Policy is asymmetric and financial sectors are excluded."),
    ("Summaries", "Three strategy summaries and industry diagnostics are present."),
    ("Controls", "Every model control is represented, applied, validated, transactional and reset when missing."),
]
table(["Test area", "Behavior protected"], test_rows)
para("Tests demonstrate implementation consistency with the written rules. They do not demonstrate investment alpha. Backtesting, out-of-sample validation, turnover analysis and transaction-cost modelling remain separate research tasks.")


heading("28. Explicitly unavailable or deliberately excluded calculations")
if UNAVAILABLE_CALCULATIONS:
    for item, reason in UNAVAILABLE_CALCULATIONS.items():
        bullet(f"{item}: {reason}")
para("The program also does not calculate volatility, Sharpe ratio, RSI, moving averages, implied volatility, options value, short interest, borrow cost, news sentiment, insider signals, position size, portfolio risk, transaction costs or expected returns because the required time series, market microstructure or external data is not present in the supplied architecture.")


heading("29. Interpretation guide")
bullet("Expanded Long above 50 means the weighted available families lean long; Expanded Short above 50 means they lean short.")
bullet("A high score with low confidence is not eligible under the current candidate gate.")
bullet("A high-confidence score near 50 means broad data but weak direction.")
bullet("A family score shows one theme; its contribution shows that theme after its family weight, before final renormalisation.")
bullet("The z-score expresses statistical distance from blended peers; the peer score expresses the same position on 0–100; orientation expresses economic meaning.")
bullet("The selected universe is intentionally smaller than the ranked universe. Counts are maxima, not quotas.")
bullet("Standard-company leverage limits do not override the separate financial and REIT/real-estate gate logic. Book/price equality is mandatory only when the divergence control is enabled and set to 0.")


heading("30. Limitations and model-risk statement")
para("The system is point-in-time and depends entirely on the quality, definitions and timing of supplied vendor data. It has no global source-wide as-of timestamp guarantee. Peer-relative strength is not absolute business quality or intrinsic value. Industry classifications can be imperfect. Linear z mapping, family definitions, directions, thresholds and weights are modelling assumptions rather than statistically estimated truths.")
para("Explicit family weights improve interpretability but do not prove optimal predictive power. Shrinkage stabilises small peer groups but may blur real industry distinctions. Missing-data renormalisation prevents artificial zeros but can change the effective model from stock to stock; coverage and confidence are designed to disclose that fact. The book-proximity gate can structurally favour asset-heavy businesses and reject valid high-intangible models. Long and short implementations have asymmetric financing, gap, borrow and loss risks that the score does not model.")
para("The program has not, by architecture alone, established causality, future-return predictiveness or investable alpha. Independent point-in-time backtesting, survivorship-bias controls, look-ahead checks, turnover, capacity, costs and risk management are required before investment use.")


heading("31. One-sentence summary")
para("The program converts heterogeneous stock fundamentals and market data into factor-specific industry/sector-relative evidence, prevents correlated signals from dominating through duplicate groups, balances economic themes through family weights, separates conviction from data completeness, and admits only high-confidence, risk-compliant, top-ranked names into user-sized long and short universes while preserving a complete audit trail.")

doc.core_properties.title = "US Single-Stock Analysis Program — Complete Architecture and Mathematical Specification"
doc.core_properties.author = "Codex"
doc.core_properties.subject = "Full behavioral architecture, scoring, weighting, peer methodology, controls and validation"
doc.save(OUT)
print(OUT.resolve())
