# eLAAD-style synthetic claims

`generate_elaad.py` builds a synthetic, patient-level claims dataset shaped like IQVIA's
Enterprise Longitudinal Access and Adjudication Data (eLAAD). It writes 18 CSVs (pharmacy
lifecycle claims, medical claims with remittance, reference files, and patient-centric
analytics) and a `create_tables.sql` script that creates and loads them in Unity Catalog.

**This is not IQVIA's data dictionary**, which is not publicly available. The tables follow IQVIA's
public descriptions of the dataset and the NCPDP and X12 standards its claims are built on,
so names are illustrative. Every value is synthetic. Products, manufacturers, payers, PBMs,
and assistance programs are fictional. NDCs use the unassigned labeler `00000`, and providers
carry tokens instead of NPIs.

## What eLAAD is, from public sources

- **One patient-level dataset across all therapeutic markets.** It combines pharmacy lifecycle
  claims, paid prescriptions, professional and institutional medical claims, and remittance
  claims. Standard LAAD is built on customizable market definitions, with 36 months of history.
- **Lifecycle claims.** Every adjudication transaction is kept: paid, rejected (with NCPDP
  reject codes), and reversed (approved by the payer but never picked up at the pharmacy).
  Each claim line is marked when it is the finalized transaction.
- **Patient-centric methodology.** A look-forward settles each fill's final status, links
  secondary payers and assistance programs (copay cards), and measures true patient cost:
  out-of-pocket before and after the coupon buy-down.
- **Medical side.** Pre-adjudicated professional and institutional claims come from
  clearinghouses and practice-management systems, and remittance data shows what the payer
  actually paid or denied.
- **Data is collected from pharmacies and switch clearinghouses.** Patient identifiers are
  tokenized by a third party, and data is delivered weekly or monthly.

## Tables

| Feed | Tables |
|---|---|
| Pharmacy lifecycle claims | `rx_claim` (one row per B1 billing or B2 reversal, primary or secondary payer), `rx_claim_reject` |
| Medical claims and remittance | `dx_claim`, `dx_claim_diagnosis`, `dx_claim_line`, `dx_remittance`, `dx_remittance_adjustment` |
| Reference | `patient`, `ref_product`, `ref_plan`, `ref_provider`, `ref_pharmacy`, `ref_reject_code`, `ref_diagnosis`, `ref_procedure`, `ref_adjustment_reason` |
| Patient-centric analytics (derived) | `rx_fill` (initial and final status, rejects, buy-down, true out-of-pocket), `therapy_episode` (line of therapy, source of business, persistence) |

Every column has a comment in `create_tables.sql`. Where a field mirrors an NCPDP standard
field, the comment gives its ID, for example `patient_pay_amt (505-F5)`.

## How the simulation works

Patients with psoriasis, psoriatic arthritis, or atopic dermatitis move through conventional
therapy and fictional branded products: two self-injected biologics, an oral TYK2/JAK inhibitor,
a branded topical, and a buy-and-bill IV biologic. They also fill generic maintenance drugs for
their comorbidities, which gives the all-market view eLAAD adds. Coverage follows each patient
through January plan changes, Medicaid to commercial moves, and aging into Medicare.

The simulation includes:

- Formulary exclusions, prior authorization and step edits with several review rounds, annual
  PA renewals, specialty-network rejects, and administrative rejects.
- Deductibles, coinsurance, the Medicare Part D out-of-pocket cap, and low-income subsidy copays.
- Copay programs that pay as secondary on commercial plans only. Federal programs reject them.
  Accumulator plans do not credit them toward the deductible.
- Free product or cash after a denial.
- Abandonment that rises with out-of-pocket cost.
- Buy-and-bill infusions whose missing authorizations show up only as remittance denials
  (CARC 197).

### Calibration

The simulation's probabilities were tuned until the data reproduces access patterns that studies
measured in real LAAD and claim-lifecycle data:

1. **Pick published targets.** Four measures that sources define precisely: new-to-brand
   outcomes, abandonment by out-of-pocket cost, prior authorization (PA) processing, and first
   reject reasons on branded starts.
2. **Measure them the same way.** A calibration script recomputes each measure with the source's
   definition. For example, a fill is abandoned only when the plan approved it and the pharmacy
   then reversed it.
3. **Use stated rates directly, then tune the rest.** The abandonment curve and the share of PA
   fills that need more than one review come straight from the sources. Other parameters were
   adjusted over four rounds: first-review PA approval (58% to 52%), same-day PA settlement,
   how often prescribers pick a drug the plan excludes, how often specialty prescriptions go to a
   retail pharmacy first (15% to 5%), and administrative rejects on new starts (6% to 10%).
4. **Check rules that must always hold.** No Part D patient pays past the out-of-pocket cap, no
   copay program pays behind Medicare or Medicaid, and every key and reversal links up.

Results with the defaults (seed 42, 2,000 patients). Across seeds 1, 7, 42, 99, and 2026, each
measure stays within a 5-point range.

| Measure | Generator | Published |
|---|---|---|
| New-to-brand fills that end paid / rejected / abandoned | 38% / 49% / 13% | 35% / 49% / 17% for first-year prescriptions of 2020–2024 launches (IQVIA Institute) |
| Abandonment of approved branded new starts under $20 and at $250 and up | 11% / 54% | about 10–15% / near 60% (IQVIA) |
| Fills with a PA reject that are eventually approved | 53% | 54% (Wang et al., 2026) |
| Of those, settled the same day / with several PA reviews | 30% / 58% | 35% / 59% (Wang et al., 2026) |
| First reject on branded starts: PA or step / not covered / administrative / specialty network | 65% / 16% / 12% / 5% | 59% / 24% / 13% / 3% for PCSK9 inhibitors (O'Neil et al., 2022) |

## Generate

```bash
python generate_elaad.py --out ./elaad_data
python generate_elaad.py --out ./elaad_data --patients 20000 --seed 7 --start 2023-07-01 --end 2026-06-30
```

It needs only the Python 3.9+ standard library, takes about two seconds at the defaults, and
writes about 20 MB. The generator checks primary keys, foreign keys, reversal links, and one
final transaction per fill before it writes anything.

## Load into Databricks

```bash
python generate_elaad.py --out ./elaad_data --catalog main --schema elaad_synthetic --volume landing
databricks schemas create elaad_synthetic main
databricks volumes create main elaad_synthetic landing MANAGED
databricks fs cp -r ./elaad_data dbfs:/Volumes/main/elaad_synthetic/landing/
```

Then open `create_tables.sql` in the SQL editor and run it. It creates the 18 tables with column
comments and informational primary and foreign keys, and loads each one with `read_files`.

## Example questions

```sql
-- Abandonment by out-of-pocket band, branded original fills
SELECT CASE WHEN f.oop_after_buydown = 0 THEN '1. $0'
            WHEN f.oop_after_buydown < 20 THEN '2. under $20'
            WHEN f.oop_after_buydown < 125 THEN '3. $20 to $125'
            WHEN f.oop_after_buydown < 250 THEN '4. $125 to $250'
            ELSE '5. $250 and up' END AS oop_band,
       COUNT(*) AS approved_fills,
       ROUND(100.0 * AVG(CASE WHEN f.final_status = 'REVERSED' THEN 1 ELSE 0 END), 1) AS abandonment_pct
FROM rx_fill f
JOIN ref_product p ON p.ndc11 = f.ndc11
WHERE p.brand_generic = 'BRAND' AND f.fill_number = 0 AND f.final_status IN ('PAID', 'REVERSED')
GROUP BY 1
ORDER BY 1;

-- Initial vs final rejection, and days to overcome a reject, by payment type
SELECT pl.payment_type,
       COUNT(*) AS fills,
       ROUND(100.0 * AVG(CASE WHEN f.initial_status = 'REJECTED' THEN 1 ELSE 0 END), 1) AS initial_reject_pct,
       ROUND(100.0 * AVG(CASE WHEN f.final_status = 'REJECTED' THEN 1 ELSE 0 END), 1) AS final_reject_pct,
       ROUND(AVG(CASE WHEN f.initial_status = 'REJECTED' AND f.final_status <> 'REJECTED' THEN f.days_to_final END), 1) AS avg_days_to_overcome
FROM rx_fill f
JOIN ref_product p ON p.ndc11 = f.ndc11
JOIN ref_plan pl ON pl.plan_id = f.primary_plan_id
WHERE p.brand_generic = 'BRAND' AND f.fill_number = 0
GROUP BY pl.payment_type
ORDER BY fills DESC;

-- Copay card penetration and buy-down on commercial branded fills
SELECT p.product_name,
       COUNT(*) AS paid_fills,
       ROUND(100.0 * AVG(CASE WHEN f.copay_card_flag THEN 1 ELSE 0 END), 1) AS copay_card_pct,
       ROUND(AVG(CASE WHEN f.copay_card_flag THEN f.buydown_amt END), 2) AS avg_buydown,
       ROUND(AVG(f.oop_before_buydown), 2) AS avg_oop_before,
       ROUND(AVG(f.oop_after_buydown), 2) AS avg_oop_after
FROM rx_fill f
JOIN ref_product p ON p.ndc11 = f.ndc11
JOIN ref_plan pl ON pl.plan_id = f.primary_plan_id
WHERE p.brand_generic = 'BRAND' AND pl.payment_type = 'COMMERCIAL' AND f.final_status = 'PAID'
GROUP BY p.product_name
ORDER BY paid_fills DESC;

-- Medical-benefit denials for a missing authorization (buy-and-bill infusions)
SELECT pl.payment_type, COUNT(DISTINCT r.claim_id) AS denied_claims, ROUND(SUM(a.adjustment_amt), 2) AS denied_charges
FROM dx_remittance r
JOIN dx_remittance_adjustment a ON a.remit_id = r.remit_id
JOIN ref_plan pl ON pl.plan_id = r.plan_id
WHERE r.claim_status_code = '4' AND a.carc_code = '197'
GROUP BY pl.payment_type
ORDER BY denied_claims DESC;

-- Dispensed fills from raw claims: a PAID primary claim with no reversal
SELECT COUNT(*) AS dispensed_fills
FROM rx_claim c
WHERE c.claim_status = 'PAID' AND c.coverage_seq = 1
  AND NOT EXISTS (SELECT 1 FROM rx_claim r WHERE r.reversal_of_claim_id = c.claim_id);
```

The last query matches `SELECT COUNT(*) FROM rx_fill WHERE final_status = 'PAID'`. That's the
reversal gotcha: a PAID claim alone does not mean the patient picked up the prescription.

## Sources

IQVIA product descriptions:
- [Enterprise LAAD (eLAAD) fact sheet, 2024](https://www.iqvia.com/locations/united-states/library/fact-sheets/iqvias-enterprise-longitudinal-access-and-adjudication-data-elaad)
- [LAAD fact sheet, 2023](https://www.iqvia.com/locations/united-states/library/fact-sheets/longitudinal-access-and-adjudication-data)
- [LAAD Plus fact sheet, 2025](https://www.iqvia.com/locations/united-states/library/fact-sheets/longitudinal-access-and-adjudication-data-plus-laad-plus-solution)
- [Advanced Analytics Solution for LAAD, 2022](https://www.iqvia.com/locations/united-states/library/fact-sheets/advanced-analytics-solution-for-the-longitudinal-access-and-adjudication-data-set)
- [Cost of Access: Patient Affordability Now Shapes Utilization, 2026](https://www.iqvia.com/locations/united-states/blogs/2026/08/cost-of-access-patient-affordability-now-shapes-utilization)
- [IQVIA Institute: U.S. Medicine Use Climbs Amid Continued Affordability and Access Challenges, 2026](https://www.iqvia.com/blogs/2026/06/us-medicine-use-climbs-amid-continued-affordability-and-access-challenges)

Studies that describe the data and its claim lifecycle:
- Jesudian et al., *Adv Ther* 2025, [doi:10.1007/s12325-025-03145-3](https://doi.org/10.1007/s12325-025-03145-3): final claim status and reject reasons.
- Tao et al., *JAMA Netw Open* 2025, [doi:10.1001/jamanetworkopen.2025.44515](https://doi.org/10.1001/jamanetworkopen.2025.44515): dispensed, rejected, and abandoned claims, with a reject-reason table.
- Wang et al., *JAMA Health Forum* 2026, [doi:10.1001/jamahealthforum.2026.0760](https://doi.org/10.1001/jamahealthforum.2026.0760): finalized transactions and PA processing time.
- O'Neil et al., *JAMA Netw Open* 2022, [doi:10.1001/jamanetworkopen.2022.40513](https://doi.org/10.1001/jamanetworkopen.2022.40513): initial vs durable rejection.
- Patel et al., *JAMA Health Forum* 2022, [doi:10.1001/jamahealthforum.2022.1167](https://doi.org/10.1001/jamahealthforum.2022.1167): benefit design and abandonment.
- Gibbons et al., *JAMA Netw Open* 2026, [doi:10.1001/jamanetworkopen.2026.2201](https://doi.org/10.1001/jamanetworkopen.2026.2201): coupon flag, buy-down, and out-of-pocket before and after.
- Gilbert et al., *Pulm Ther* 2021, [doi:10.1007/s41030-021-00147-8](https://doi.org/10.1007/s41030-021-00147-8), and LaMountain et al., *Health Aff Sch* 2024, [doi:10.1093/haschl/qxae090](https://doi.org/10.1093/haschl/qxae090): sourcing and coverage.

Standards and benefit parameters:
- NCPDP reject, DAW, other coverage, program type, and benefit stage values were checked
  against the public-domain VA VistA claims engine tables
  ([WorldVistA/VistA-M](https://github.com/WorldVistA/VistA-M)).
- X12 claim adjustment reason codes are from [x12.org](https://x12.org/codes/claim-adjustment-reason-codes).
- Medicare Part D deductibles, out-of-pocket thresholds, and low-income subsidy copays for 2023–2026
  are from the CMS rate announcements for
  [CY2024](https://www.cms.gov/files/document/2024-announcement-pdf.pdf),
  [CY2025](https://www.cms.gov/files/document/2025-announcement.pdf), and
  [CY2026](https://www.cms.gov/files/document/2026-announcement.pdf).
- Descriptions in this dataset are paraphrased.

## Known simplifications

- The dataset covers one immunology market plus maintenance generics, and every pharmacy reports.
  Real open claims miss some pharmacies, which is why `patient.stable_flag` exists.
- There is no enrollment file. BIN, PCN, ingredient cost, and dispensing fee fields are left out,
  and there are no inpatient claims.
- Reversals of the last fills in the window can post a few days after the data end date.

IQVIA, LAAD, and eLAAD are trademarks of IQVIA Inc. This project is not affiliated with or
endorsed by IQVIA.
