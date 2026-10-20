# XLSM audit notes: Freefincal robo tool (Jul 2025) and AUG-2021 screener compared with pm-india

Audit of 20 Oct 2026. The raw dumps and scripts referenced here were produced
in a scratch directory (`/tmp/xlsm-audit/`, robo_cells.tsv, screener_cells.tsv,
robo_vba.txt, screener_vba.txt, compare.py and its output); they are
reproducible from the workbooks, which live outside the repo at
`/home/tiny/Freefincal-Robo-advisory-tool-July 2025.xlsm` and
`/home/tiny/AUG-2021-freefincal-Equity-mutual-fund-outperformance-macro-screener.xlsm`.
What the audit found and what was fixed because of it is summarized in
`docs/goal-planner-parity.md`.

Files produced by the audit:
- `robo_vba.txt`, `screener_vba.txt`: full olevba 0.60.2 output for each workbook.
- `robo_cells.tsv`, `screener_cells.tsv`: every non-empty cell, as `cell<TAB>F|V<TAB>formula or value<TAB>=> cached value` (made by `dump.py`, openpyxl; newlines inside formulas are flattened).
- `robo_names.txt`: defined names. Useful ones: `ygoal1`=Step 4A!B4, `goal1`=Step 4A!B18, `infgr`=Step 4B!B8, `typegr1`=Step 4B!AT1, `time`/`rinf`/`int*`/`k*`/`b*r` are the bucket-sheet locals.
- Per-sheet slices: `s3.tsv` (Step 3), `s4a.tsv`, `s4b.tsv`, `s6b.tsv`, `lsb.tsv` (low stress buckets), `inf.tsv` (income flooring), `bb.tsv` (bucket backend).
- `compare.py` / `compare_output.txt`: my Python re-implementation of the workbook formulas, compared with `core/goals.py` on sample inputs. It ran under PYTHONDONTWRITEBYTECODE, so nothing was written into the repo.
- `screener_check.py`, `screener_check2.py` and their `*_output.txt`: consistency checks on the screener's values.

## Robo workbook: VBA (olevba)
The VBA does no planning math. It does only these things:
- `Module2.OtherGoals1..6`: GoalSeek. `Cells(n1,10).GoalSeek Goal:=B18, ChangingCell:=I3` finds the monthly SIP. `Cells(n1,14).GoalSeek ... M3` finds the lump sum. If B5>=B4 (the SIP would start on or after the goal date) it sets both to 0 and shows a message box. If goal year > `Step 2!B7` (years to retirement) it warns that the goal falls after retirement.
- `Module3.Retirement`: GoalSeek `Cells(P1,14)` (Step 3 N column) to `B11` (net corpus) by changing `M2`. If B5<=0 (already retired) it sets M2=0 and shows: "If the amount that you have in hand is not sufficient to provide an inflation protected income for the first 15 years in retirement, we recommend that you purchase an annuity".
- `Sheet3.Worksheet_Change` (Step 1): if age D7<26 it hides the retirement sheets and only shows other goals.
- `Module4.DIYBuckets` (unhides a sheet), `Module5.Auto_Open` (disclaimer), `Module6.Unified` (stub), `Module8.Resetall`, `Module7`/`ThisWorkbook` BeforeSave (save-as blocking).

## Robo workbook: core formulas (sheet!cell)
- Settings: C3 6% (pre-retirement inflation), C4 90 (life expectancy), C5 6% (retirement inflation), C7 10% (equity), C8 7% (all fixed income), H7 4% (annuity rate). B13 6% (income bucket ladder return), B14:B16 5% (ladder return inside each bucket). C36 10% and C37 7% (recurring goal inflation and return). C39/C40/C41/C42 = 10%, 10%, 6%, 7% (per one-time goal). T2:U12 is an age table that nothing references (dead).
- Settings!C10 (planning period) = `C4-MAX(age,retire)+IF(married, IF(F8<D7, D7-F8, 0), 0)`.
- Bucket tenures: E13 `MIN(C10,15)`. E14 `IF(ROUND(40%*C10)<15, ROUND(50%*(C10-E13)), ROUND(25%*C10))`, floored at 0. E15 is the same with 25%/20%. E16 is the remainder.
- Bucket equity: C20 `IF(C10<=25,20%,IF(C10<=35,40%,50%))`. C21 30/50/70%. C22 70/100%. E20:E22 = `C*C7 + D*C8`. C14:C16 = `ROUND(E20,2)` (or the K18 reduced-return variant).
- low stress buckets!F17:F19 = `ROUND(IF(B28="Yes",'bucket backend'!Z2,Settings!C14),3)`. B28:B30 default to "yes", so F17:F19 are the glide-path geometric averages T2/W2/Z2 = `PRODUCT(1+r_y)^(1/n)-1`, which give 7.8/8.6/9.3% with defaults. Two workbook bugs: F18 and F19 both read B29, and F19 should read B30. Also, the low bucket glide (Settings!E146) starts at 40% for 15 years or fewer, not at C20.
- F6 = `B6*(1+B9)^B7` (annual expense inflated to retirement). The expense in year j is `F6*(1+rinf)^j`.
- Income bucket: M10.. = `MAX(0, E_j - incomes - AH)/(1+6%)^j`, and M6 = SUM.
- Low/medium/high bucket: `R5 = Σ (E - incomes)/(1+5%)^o` (laddered at 5% inside the bucket), then `R6 = R5/(1+F17)^(years before the bucket starts)`. W6 and AB6 work the same way.
- G25 = ΣG16:G19. **F13 (emergency) = `IF(F10>G25,5%*F10,5%*G25)`**. **F9 (gross) = `G25+F13-Step2!D44`**. In the income flooring sheet it is `+F12`.
- Step 3!B11 (net) = `F9 - SUM(Step2!C14:C18)`. Step 2!C14:C16 = value×(1+r)^years for equity 10%, taxable FI C8−1% = 6%, tax-free 7%. C17/C18 are the retirement lump sums.
- Post-retirement income (low stress AE10): equals C32 (`=B32*(1+B33)^B7`) in the start year F32, then grows by (1+B33) each year, and is 0 after the stop year G32. Years are 1-based and inclusive.
- AH column: other-goal SIPs still running after retirement (Step 6B cols M and O) are added to the retirement expense.
- Income flooring: L51 = `(year-1 expense - incomes)*Step3!B9`, a FLAT nominal amount subtracted from every year. F12 = `L51/Settings!H7` (a 4% annuity).
- Step 3 pre-retirement glide: H = Settings E25 `IF(C25<10, G25, 60%)` falling to G25 `IF(retire>=60, 20%, 'low stress buckets'!D41)` over segments C26/C27/C28. L = `H*C7+(1-H)*C8` (fixed income here is 7%, not 6%).
- **Step 3 SIP accumulation: `N2=M2*12*(1+L2)`, `N3=(12*M3+N2)*(1+L3)`, `M3=M2*(1+step)`**. Contributions are annual and compound at the annual rate.
- Step 4A one-time goal: B9 = `B6*(1+infl)^B4`. B19 = `Σ value*(1+r)^(B4-B5)`. B18 = `IF(B9<0,0,B9-B16-B19)`. F (glide) uses Settings E45/G45 and C46:C48. H = `F*C40+G*C41`. **J3=`I3*12*(1+H3)`, J4=`(12*I4+J3)*(1+H4)`**. Lump sum N3=`M3*(1+H3)`, N4=`N3*(1+H4)`.
- Step 4B recurring goal: occurrence i is paid in year B4+i·B5, B6 times. F = `B7*(1+infgr)^(years)`. **G = `F*(r-g)/(12*((1+r)^n-(1+g)^n))`**, divided by a further (1+r) if typegr1=1. Here r = 7% (C37), g = the B10 step-up, n = B4 for the first occurrence and B5 (the interval) after that. Each occurrence has its own sinking fund, and investing for the next one starts after the previous one.
- Step 6A/6B: the independent and unified portfolio approaches. Unified withdrawals are 20%/yr over 5 years (Settings C116:C120), split 60/40. The unified glide uses Settings E123=60% to G123=20%, return `AB*B111+AC*B112`. Cash flow is checked against salary growing at Step 2!B25 (K9), and an EMI column exists.
- annuity laddering: a stand-alone module that buys an annuity every 10 years (not read in detail). DIY Bucket calculator: hidden, a clone of low stress buckets.

## Numbers (compare_output.txt)
Case 1: age 35, spouse 30, retire at 50, ₹50k/month, 5% step-up, nothing saved.

| | Workbook (my re-implementation) | App |
|---|---|---|
| Bucket returns | 6/7.8/8.6/9.3% | 6/8.5/9.1/10% |
| Bucket investments (₹) | 2.16 Cr / 1.29 / 0.72 / 0.51 | 2.16 / 0.99 / 0.55 / 1.29 |
| Emergency | ₹23.4 L | ₹7.2 L |
| Gross corpus | ₹4.91 Cr | ₹5.06 Cr |
| SIP | ₹1,00,287 | ₹1,04,142 |
| Post-retirement equity | 35% | 44% |

The re-implementation reproduces the workbook's cached values for bucket tenures (15/11/9/10) and glide returns (0.0782/0.0859/0.0933). It has not been validated against Excel for the corpus or SIP, because no Excel or LibreOffice is available here.

One-time goals:
- ₹20 L, 10 years, rigid: SIP is ₹24,435 in the workbook vs ₹25,074 in the app; lump sum is ₹26.9 L vs ₹26.4 L.

Recurring goal (₹2 L/yr, first payment in 5 years, 4 payments):
- Workbook: per-occurrence SIPs of 4,251, then 29,526, 32,479 and 35,726 per month.
- App: one ₹17,783/month SIP to a ₹13.6 L PV at 6%.

Income floor at 50%:
- Workbook flat-annuity corpus: ₹1.80 Cr.
- App indexed annuity: ₹3.24 Cr.

## Screener (verified against docs/mf-forecasting-research.md §5)
- No formulas on the Results or Excess sheets (0 formulas, about 11k values each). Screener Inputs has one formula (M2=F3).
- VBA: 15 threshold filters, 15 clears, Category/Benchmark filters and their clears, Macro3 ShowAllData, Macro5, ExcessReturn ($AG$372), FilterAug21 ($AG$360) and Filtertwo. There is no calculation. The doc's "15 filter macros and nothing else" undercounts, but its "no calculation" claim is correct.
- Ranges: RO filters and Category/Benchmark use `$A$1:$AG$1298`. UP/DC filters **and the RO clears** use `$A$1:$AG$298`. There are 359 data rows (last row 360), so 62 rows fall outside the 298 range. The doc's bug claim is confirmed.
- The doc says the score equals count/fund_n in 351 of 359 rows, with 8 deviating, and reads the deviations as data artifacts. **Contradicted.** The score equals `outperformed / MIN(index_n, fund_n)` in **1647/1647** window cells. Measured against fund_n alone, 6 rows (30 cells) deviate, not 8. The denominator is the common-window count, applied consistently.
- Window counts reach 3,515 for 1Y windows, so the workbook rolls daily. pm-india rolls monthly (screener.py:8-9, 332-345). This is a deliberate change, made in the doc.
- The doc's claim that excess return = fund − bench and excess risk = fund sd − index sd in every row: confirmed, 0 violations. The median 1Y index stdev is 0.039, consistent with a non-annualised monthly-return stdev, but that basis is not stated in the workbook.
- 20 of 359 rows pass the ExcessReturn macro, which requires all 5 windows. Single-window passes are 83/104/91/74/59.
- Category→benchmark pairs in the workbook: AggHybrid→Crisil6535 + Nifty100. Contra/DivYield/Value/Sectoral/Sectoral-intl/LargeCap→Nifty100. ELSS→N200. Focussed/L&M/MultiCap→LargeMid250. Mid/Small→Midcap150. These match BENCHMARK_MAP for every category the workbook has.
- Threshold defaults are 0.7 for all 15 inputs. The app defaults to outperf 0.6 and up/down None (views_screener.py:68-70).
- How upside and downside consistency are defined is not in the workbook (values only), so the app's definition (screener.py:387-390) cannot be checked against it.
