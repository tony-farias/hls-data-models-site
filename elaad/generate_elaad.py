#!/usr/bin/env python3
"""Generate a synthetic, eLAAD-style longitudinal claims dataset.

IQVIA describes Enterprise LAAD (eLAAD) as one patient-centric dataset that integrates
pharmacy lifecycle claims (every paid, rejected, and reversed transaction), paid
prescriptions, professional and institutional medical claims, and remittance (835)
data across all therapeutic markets, with a look-forward methodology that settles
each fill's final status, links secondary payers and assistance programs, and
measures true patient cost exposure.

IQVIA's data dictionary is not public. This replica follows public descriptions of
the dataset and the NCPDP and X12 standards those claims are built on, so tables and
columns are illustrative and every value is fictional: products, manufacturers,
payers, PBMs, programs, and tokens are invented, NDCs use the unassigned labeler
00000, and providers carry tokens instead of NPIs. Code descriptions are paraphrased.

Market-access mechanics in the simulation, calibrated to published LAAD figures:
formulary exclusions, prior authorization (PA) and step edits with multiple review
rounds, annual PA renewals, administrative and specialty-network rejects, January
plan changes, deductibles and the Medicare Part D out-of-pocket cap, copay programs
that pay as secondary on commercial plans only (accumulator plans do not credit them),
free product or cash after a denial, abandonment that rises with out-of-pocket cost,
and buy-and-bill infusions whose PA denials surface only in remittance data.

Writes one CSV per table and create_tables.sql (Unity Catalog DDL with comments,
informational keys, and read_files loads from a Volume).

Usage:
  python generate_elaad.py --out ./elaad_data
  python generate_elaad.py --out ./elaad_data --patients 20000 --seed 7 \\
      --catalog main --schema elaad_synthetic --volume landing
Standard library only; Python 3.9 or later.
"""
import argparse
import csv
import hashlib
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------- schema spec
# feed: rx = pharmacy lifecycle claims, dx = medical claims and remittance, ref = reference, derived = built on top
S, I, D, B = "STRING", "INT", "DATE", "BOOLEAN"
M, Q = "DECIMAL(12,2)", "DECIMAL(12,3)"
GAP_DAYS = 60  # allowed gap after supply runs out before a therapy episode ends


def table(name, feed, comment, columns, pk, fks=()):
    return {"name": name, "feed": feed, "comment": comment, "columns": columns, "pk": list(pk), "fks": list(fks)}


SCHEMA = [
    table("patient", "ref",
          "One row per de-identified patient token. Open claims have no enrollment file, so the activity "
          "window and stability flag stand in for eligibility.",
          [("patient_id", S, "De-identified patient token; links pharmacy and medical claims."),
           ("birth_year", I, "Year of birth."),
           ("gender", S, "F, M, or U."),
           ("patient_state", S, "Two-letter state of residence."),
           ("patient_zip3", S, "First three digits of the residence ZIP code."),
           ("first_activity_date", D, "First pharmacy or medical claim observed."),
           ("last_activity_date", D, "Last pharmacy or medical claim observed."),
           ("has_rx_activity", B, "At least one pharmacy claim."),
           ("has_dx_activity", B, "At least one medical claim."),
           ("stable_flag", B, "Claims observed in both halves of the data window: a visibility proxy for open claims.")],
          ["patient_id"]),
    table("ref_product", "ref",
          "Product master keyed by package NDC, covering every market a patient fills in. Brands and "
          "manufacturers are fictional; labeler 00000 is never assigned, so no code matches a real product.",
          [("ndc11", S, "11-digit package NDC (synthetic)."),
           ("product_name", S, "Brand name, or the molecule for generics."),
           ("molecule_name", S, "Active ingredient."),
           ("manufacturer_name", S, "Labeler."),
           ("market", S, "Market definition the product rolls up to."),
           ("therapeutic_class", S, "Mechanism or class within the market."),
           ("strength", S, "Strength as labeled."),
           ("dosage_form", S, "Dosage form."),
           ("route", S, "Route of administration."),
           ("package_quantity", Q, "Billing units per package."),
           ("brand_generic", S, "BRAND or GENERIC."),
           ("specialty_flag", B, "Plans require a specialty pharmacy."),
           ("benefit_type", S, "PHARMACY (Rx claims) or MEDICAL (buy-and-bill, Dx claims)."),
           ("launch_date", D, "US launch date; null for old generics.")],
          ["ndc11"]),
    table("ref_plan", "ref",
          "Payer and plan master with PBM, payment type, and NCPDP program type. Copay programs, free-product "
          "programs, and cash appear as their own plans so secondary payers can be identified.",
          [("plan_id", S, "Plan identifier."),
           ("plan_name", S, "Plan name."),
           ("payer_name", S, "Payer or program sponsor."),
           ("pbm_name", S, "Pharmacy benefit manager or claims processor."),
           ("payment_type", S, "COMMERCIAL, MEDICARE, MEDICAID_FFS, MANAGED_MEDICAID, CASH, or ASSISTANCE."),
           ("plan_type", S, "PPO, HMO, HDHP, PDP, MAPD, FFS, MANAGED_MEDICAID, CASH, COPAY_CARD, or FREE_PRODUCT."),
           ("program_type_code", S, "NCPDP other payer program type: 3 commercial, 14 Part D PDP, 13 Medicare Advantage, "
                                    "16 Part B, 1 Medicaid, 11 Medicaid managed care, 15 cash, 6 manufacturer copay program, "
                                    "7 manufacturer free product."),
           ("plan_state", S, "State for state Medicaid plans; null for national plans.")],
          ["plan_id"]),
    table("ref_provider", "ref",
          "Prescriber, rendering, referring, and billing provider master. Tokens only, no NPIs.",
          [("provider_id", S, "De-identified provider token."),
           ("provider_type", S, "INDIVIDUAL or ORGANIZATION."),
           ("specialty", S, "Primary specialty."),
           ("provider_state", S, "Practice state."),
           ("provider_zip5", S, "Practice ZIP code.")],
          ["provider_id"]),
    table("ref_pharmacy", "ref", "Dispensing pharmacy master.",
          [("pharmacy_id", S, "De-identified pharmacy token."),
           ("pharmacy_service_type", S, "NCPDP pharmacy service type: 1 community/retail, 6 mail order, 8 specialty."),
           ("pharmacy_channel", S, "RETAIL_CHAIN, RETAIL_INDEPENDENT, MAIL_ORDER, or SPECIALTY."),
           ("pharmacy_state", S, "Pharmacy state."),
           ("pharmacy_zip3", S, "Pharmacy ZIP3.")],
          ["pharmacy_id"]),
    table("ref_reject_code", "ref", "NCPDP reject codes that appear in this dataset, grouped the way access studies report them.",
          [("reject_code", S, "NCPDP reject code (511-FB)."),
           ("reject_description", S, "Short description."),
           ("reject_group", S, "PRODUCT_NOT_COVERED, PRIOR_AUTHORIZATION, REFILL_RESTRICTION, DISTRIBUTION_LIMITATION, "
                               "PLAN_LIMITATION, ELIGIBILITY, CLINICAL, or ADMINISTRATIVE.")],
          ["reject_code"]),
    table("ref_diagnosis", "ref", "ICD-10-CM codes that appear in this dataset.",
          [("icd10_code", S, "ICD-10-CM code as billed, without the dot."),
           ("icd10_description", S, "Short description."),
           ("condition_group", S, "PSORIASIS, PSORIATIC_ARTHRITIS, ATOPIC_DERMATITIS, COMORBIDITY, or SCREENING.")],
          ["icd10_code"]),
    table("ref_procedure", "ref", "CPT and HCPCS codes that appear in this dataset, with short descriptions written for this demo.",
          [("procedure_code", S, "CPT or HCPCS code."),
           ("code_system", S, "CPT or HCPCS."),
           ("procedure_description", S, "Short description.")],
          ["procedure_code"]),
    table("ref_adjustment_reason", "ref", "X12 claim adjustment reason codes (CARC) that appear in remittance data.",
          [("carc_code", S, "Claim adjustment reason code."),
           ("carc_description", S, "Short description."),
           ("adjustment_category", S, "PATIENT_COST_SHARE, CONTRACTUAL, AUTHORIZATION, COVERAGE, or ADMINISTRATIVE.")],
          ["carc_code"]),
    table("rx_claim", "rx",
          "Pharmacy lifecycle claims: one row per adjudication transaction (billing, secondary billing, or "
          "reversal). Count a fill as dispensed only when its PAID claim has no reversal.",
          [("claim_id", S, "Transaction identifier."),
           ("rx_id", S, "De-identified prescription token; every submission, refill, and reversal of one prescription shares it."),
           ("fill_number", I, "NCPDP fill number (403-D3): 0 = original, 1 and up = refills."),
           ("patient_id", S, "Patient token."),
           ("transaction_type", S, "NCPDP transaction code: B1 billing, B2 reversal."),
           ("claim_status", S, "PAID, REJECTED, or REVERSED."),
           ("final_claim_flag", B, "This transaction settled the fill: no later submission for the same fill follows it."),
           ("coverage_seq", I, "1 = primary payer; 2 = secondary payer, such as a manufacturer copay program."),
           ("date_written", D, "Date the prescription was written (414-DE)."),
           ("service_date", D, "Date of service (401-D1)."),
           ("transaction_date", D, "Adjudication date; reversals post days after the fill."),
           ("ndc11", S, "Product submitted."),
           ("quantity", Q, "Quantity dispensed in billing units (442-E7)."),
           ("days_supply", I, "Days supply (405-D5)."),
           ("daw_code", S, "Dispense-as-written code (408-D8)."),
           ("prescriber_id", S, "Prescriber token."),
           ("pharmacy_id", S, "Pharmacy token."),
           ("plan_id", S, "Plan that adjudicated this transaction."),
           ("other_coverage_code", S, "NCPDP other coverage code (308-C8): 01 none, 08 billing patient responsibility only."),
           ("benefit_stage", S, "Part D benefit stage on paid claims: 1 deductible, 2 initial, 4 catastrophic."),
           ("plan_pay_amt", M, "Total amount paid by this plan (509-F9); null on rejects and reversals."),
           ("patient_pay_amt", M, "Patient pay amount after this plan (505-F5)."),
           ("deductible_amt", M, "Amount applied to periodic deductible (517-FH)."),
           ("copay_amt", M, "Amount of copay (518-FI)."),
           ("coinsurance_amt", M, "Amount of coinsurance (572-4U)."),
           ("reversal_of_claim_id", S, "On REVERSED rows, the PAID claim being reversed because the prescription was not picked up.")],
          ["claim_id"],
          [("patient_id", "patient", "patient_id"), ("ndc11", "ref_product", "ndc11"),
           ("prescriber_id", "ref_provider", "provider_id"), ("pharmacy_id", "ref_pharmacy", "pharmacy_id"),
           ("plan_id", "ref_plan", "plan_id")]),
    table("rx_claim_reject", "rx", "Reject codes on REJECTED transactions, in the order the payer returned them (up to five).",
          [("claim_id", S, "Rejected transaction."),
           ("reject_seq", I, "1 = first reject code returned."),
           ("reject_code", S, "NCPDP reject code.")],
          ["claim_id", "reject_seq"],
          [("claim_id", "rx_claim", "claim_id"), ("reject_code", "ref_reject_code", "reject_code")]),
    table("dx_claim", "dx", "Medical claim headers from professional (837P) and institutional (837I) claims, as submitted.",
          [("claim_id", S, "Medical claim identifier."),
           ("patient_id", S, "Patient token."),
           ("claim_type", S, "PROFESSIONAL or INSTITUTIONAL."),
           ("service_from_date", D, "First date of service."),
           ("service_to_date", D, "Last date of service."),
           ("place_of_service", S, "CMS place-of-service code on professional claims: 11 office, 10 telehealth at home."),
           ("bill_type", S, "Type of bill on institutional claims: 0131 hospital outpatient."),
           ("rendering_provider_id", S, "Provider who performed or administered the service."),
           ("billing_provider_id", S, "Provider or organization that billed the claim."),
           ("referring_provider_id", S, "Referring provider, when reported."),
           ("plan_id", S, "Payer billed. Members with a stand-alone Part D plan bill medical care to Original Medicare."),
           ("total_charge_amt", M, "Total billed charges.")],
          ["claim_id"],
          [("patient_id", "patient", "patient_id"), ("rendering_provider_id", "ref_provider", "provider_id"),
           ("billing_provider_id", "ref_provider", "provider_id"), ("referring_provider_id", "ref_provider", "provider_id"),
           ("plan_id", "ref_plan", "plan_id")]),
    table("dx_claim_diagnosis", "dx", "Diagnosis codes on a medical claim, in billed order.",
          [("claim_id", S, "Medical claim."),
           ("diagnosis_seq", I, "1 = principal diagnosis."),
           ("icd10_code", S, "ICD-10-CM code.")],
          ["claim_id", "diagnosis_seq"],
          [("claim_id", "dx_claim", "claim_id"), ("icd10_code", "ref_diagnosis", "icd10_code")]),
    table("dx_claim_line", "dx", "Service lines on a medical claim. Buy-and-bill drugs carry a HCPCS code plus the NDC.",
          [("claim_id", S, "Medical claim."),
           ("line_number", I, "Line number."),
           ("service_date", D, "Line date of service."),
           ("procedure_code", S, "CPT or HCPCS code."),
           ("ndc11", S, "NDC billed with a drug HCPCS code; null otherwise."),
           ("units", Q, "Billed units."),
           ("line_charge_amt", M, "Billed charge for the line.")],
          ["claim_id", "line_number"],
          [("claim_id", "dx_claim", "claim_id"), ("procedure_code", "ref_procedure", "procedure_code"),
           ("ndc11", "ref_product", "ndc11")]),
    table("dx_remittance", "dx", "Payer remittance (835) for a medical claim. Not every claim has a remittance in open data.",
          [("remit_id", S, "Remittance identifier."),
           ("claim_id", S, "Medical claim paid or denied."),
           ("plan_id", S, "Paying plan."),
           ("paid_date", D, "Remittance date."),
           ("claim_status_code", S, "X12 claim status: 1 processed as primary, 4 denied."),
           ("allowed_amt", M, "Allowed amount."),
           ("paid_amt", M, "Amount paid by the plan."),
           ("patient_resp_amt", M, "Patient responsibility.")],
          ["remit_id"],
          [("claim_id", "dx_claim", "claim_id"), ("plan_id", "ref_plan", "plan_id")]),
    table("dx_remittance_adjustment", "dx", "Adjustments on a remittance, with group code and reason code.",
          [("remit_id", S, "Remittance."),
           ("adjustment_seq", I, "Adjustment order."),
           ("group_code", S, "CO contractual obligation, PR patient responsibility."),
           ("carc_code", S, "Claim adjustment reason code."),
           ("adjustment_amt", M, "Adjusted amount.")],
          ["remit_id", "adjustment_seq"],
          [("remit_id", "dx_remittance", "remit_id"), ("carc_code", "ref_adjustment_reason", "carc_code")]),
    table("rx_fill", "derived",
          "Derived patient-centric view: one row per prescription fill, with the look-forward applied. Initial "
          "and final status, reject history, secondary payer, and true out-of-pocket cost.",
          [("rx_id", S, "Prescription token."),
           ("fill_number", I, "Fill number."),
           ("patient_id", S, "Patient token."),
           ("ndc11", S, "Product."),
           ("prescriber_id", S, "Prescriber token."),
           ("primary_plan_id", S, "Primary plan on the final transaction."),
           ("final_payment_type", S, "Payment type of the plan that settled the fill; ASSISTANCE means free product."),
           ("first_submit_date", D, "First submission."),
           ("final_date", D, "Date of the transaction that settled the fill."),
           ("initial_status", S, "Status of the first submission: PAID or REJECTED."),
           ("final_status", S, "Status after the look-forward: PAID, REJECTED, or REVERSED (abandoned at the pharmacy)."),
           ("reject_count", I, "Rejected submissions in the fill."),
           ("first_reject_code", S, "First reject code returned, if any."),
           ("days_to_final", I, "Days from first submission to the settling transaction."),
           ("total_claim_cost", M, "Plan pay plus patient pay before any buy-down; null when never paid."),
           ("oop_before_buydown", M, "Patient pay after the primary plan; null when never paid."),
           ("buydown_amt", M, "Amount a copay program paid as secondary."),
           ("oop_after_buydown", M, "Patient out-of-pocket after all payers; null when never paid."),
           ("copay_card_flag", B, "A manufacturer copay program paid as secondary.")],
          ["rx_id", "fill_number"],
          [("patient_id", "patient", "patient_id"), ("ndc11", "ref_product", "ndc11"),
           ("prescriber_id", "ref_provider", "provider_id"), ("primary_plan_id", "ref_plan", "plan_id")]),
    table("therapy_episode", "derived",
          "Derived from dispensed fills and medical administrations: continuous treatment episodes per patient "
          "and molecule, with line of therapy and source of business.",
          [("episode_id", S, "Episode identifier."),
           ("patient_id", S, "Patient token."),
           ("market", S, "Market definition."),
           ("molecule_name", S, "Molecule."),
           ("benefit_type", S, "PHARMACY or MEDICAL."),
           ("line_of_therapy", I, "Advanced lines in order within the market; 0 for conventional and maintenance generics."),
           ("source_of_business", S, "NEW_TO_MARKET, SWITCH, RESTART, or CONVENTIONAL."),
           ("episode_start_date", D, "First fill or administration."),
           ("episode_end_date", D, "Last fill or administration plus its days supply, capped at the data end date."),
           ("dispensed_fills", I, "Dispensed fills or administrations."),
           ("days_on_therapy", I, "Days from start to end."),
           ("discontinued", B, f"Supply ran out more than {GAP_DAYS} days before the data end date.")],
          ["episode_id"],
          [("patient_id", "patient", "patient_id")]),
]
SPEC = {t["name"]: t for t in SCHEMA}

# ---------------------------------------------------------------- reference data (all fictional)
IMMUNOLOGY = "Immunology (psoriasis, psoriatic arthritis, atopic dermatitis)"


def product(ndc, name, molecule, maker, market, cls, strength, form, route, package, kind, specialty, benefit,
            launch, qty, days, cost):
    return dict(ndc11=ndc, product_name=name, molecule_name=molecule, manufacturer_name=maker, market=market,
                therapeutic_class=cls, strength=strength, dosage_form=form, route=route, package_quantity=package,
                brand_generic=kind, specialty_flag=specialty, benefit_type=benefit, launch_date=launch,
                qty=qty, days=days, cost=cost)


PRODUCTS = [
    product("00000110101", "Zorvane", "zorvanumab", "Northwind Biologics", IMMUNOLOGY, "IL-17A inhibitor", "150 mg/mL",
            "Autoinjector", "Subcutaneous", 1, "BRAND", True, "PHARMACY", date(2019, 4, 15), 2, 28, 7400.0),
    product("00000120102", "Duvessa", "duvessimab", "Fabrikam Pharma", IMMUNOLOGY, "IL-13 inhibitor", "300 mg/2 mL",
            "Prefilled pen", "Subcutaneous", 2, "BRAND", True, "PHARMACY", date(2021, 9, 1), 4, 28, 3900.0),
    product("00000130130", "Quellora", "quellacitinib", "Northwind Biologics", IMMUNOLOGY, "TYK2/JAK inhibitor", "15 mg",
            "Tablet", "Oral", 30, "BRAND", True, "PHARMACY", date(2022, 6, 20), 30, 30, 6100.0),
    product("00000130230", "Quellora", "quellacitinib", "Northwind Biologics", IMMUNOLOGY, "TYK2/JAK inhibitor", "30 mg",
            "Tablet", "Oral", 30, "BRAND", True, "PHARMACY", date(2022, 6, 20), 30, 30, 6100.0),
    product("00000140160", "Taluvia", "taluvimod", "Fabrikam Pharma", IMMUNOLOGY, "Topical non-steroidal immunomodulator",
            "1%", "Cream", "Topical", 60, "BRAND", False, "PHARMACY", date(2023, 2, 1), 60, 30, 1450.0),
    product("00000150110", "Velnexa", "velnexumab", "Northwind Biologics", IMMUNOLOGY, "IL-23 inhibitor", "100 mg/10 mL",
            "Vial", "Intravenous", 1, "BRAND", True, "MEDICAL", date(2020, 11, 2), 1, 56, 11800.0),
    product("00000910124", "methotrexate", "methotrexate", "Generic labeler", IMMUNOLOGY,
            "Conventional systemic (antimetabolite)", "2.5 mg", "Tablet", "Oral", 100, "GENERIC", False, "PHARMACY",
            None, 24, 28, 18.0),
    product("00000920180", "triamcinolone", "triamcinolone acetonide", "Generic labeler", IMMUNOLOGY,
            "Topical corticosteroid", "0.1%", "Cream", "Topical", 80, "GENERIC", False, "PHARMACY", None, 80, 30, 14.0),
    # maintenance generics for comorbidities: the all-market view eLAAD adds over a single-market LAAD
    product("00000930130", "lisinopril", "lisinopril", "Generic labeler", "Cardiovascular", "ACE inhibitor", "10 mg",
            "Tablet", "Oral", 30, "GENERIC", False, "PHARMACY", None, 30, 30, 4.0),
    product("00000940130", "atorvastatin", "atorvastatin", "Generic labeler", "Cardiovascular", "Statin", "20 mg",
            "Tablet", "Oral", 30, "GENERIC", False, "PHARMACY", None, 30, 30, 6.0),
    product("00000950160", "metformin", "metformin", "Generic labeler", "Diabetes", "Biguanide", "500 mg",
            "Tablet", "Oral", 60, "GENERIC", False, "PHARMACY", None, 60, 30, 5.0),
    product("00000960130", "sertraline", "sertraline", "Generic labeler", "Central nervous system", "SSRI", "50 mg",
            "Tablet", "Oral", 30, "GENERIC", False, "PHARMACY", None, 30, 30, 7.0),
]
MAINTENANCE = {"I10": "lisinopril", "E785": "atorvastatin", "E119": "metformin", "F32A": "sertraline"}


def plan(plan_id, name, payer, pbm, payment_type, plan_type, program, formulary=None, accumulator=False, state=None):
    return dict(plan_id=plan_id, plan_name=name, payer_name=payer, pbm_name=pbm, payment_type=payment_type,
                plan_type=plan_type, program_type_code=program, plan_state=state, formulary=formulary or {},
                accumulator=accumulator)


# formulary status: PA = prior authorization, STEP = step edit plus PA, EXCLUDED = not covered
PLANS = [
    plan("C001", "Aldermoor Choice PPO", "Aldermoor Health", "Corvex Rx", "COMMERCIAL", "PPO", "3",
         {"Zorvane": "PA", "Duvessa": "PA", "Quellora": "PA", "Taluvia": "PREFERRED"}),
    plan("C002", "Aldermoor Select HMO", "Aldermoor Health", "Corvex Rx", "COMMERCIAL", "HMO", "3",
         {"Zorvane": "PA", "Duvessa": "PA", "Quellora": "EXCLUDED", "Taluvia": "STEP"}),
    plan("C003", "Brightvale Employer Choice", "Brightvale Health", "Tellwind Pharmacy Benefits", "COMMERCIAL", "PPO",
         "3", {"Zorvane": "EXCLUDED", "Duvessa": "PA", "Quellora": "PA", "Taluvia": "STEP"}, accumulator=True),
    plan("C004", "Kestrelton National HDHP", "Kestrelton Insurance", "Corvex Rx", "COMMERCIAL", "HDHP", "3",
         {"Zorvane": "PA", "Duvessa": "PA", "Quellora": "PA", "Taluvia": "STEP"}, accumulator=True),
    plan("C005", "Orrin Valley HMO", "Orrin Valley Health", "Arbolin PBM", "COMMERCIAL", "HMO", "3",
         {"Zorvane": "PREFERRED", "Duvessa": "EXCLUDED", "Quellora": "PA", "Taluvia": "EXCLUDED"}),
    plan("M001", "Aldermoor Medicare Rx", "Aldermoor Health", "Corvex Rx", "MEDICARE", "PDP", "14",
         {"Zorvane": "PA", "Duvessa": "PA", "Quellora": "PA", "Taluvia": "STEP"}),
    plan("M002", "Brightvale Advantage Plus", "Brightvale Health", "Tellwind Pharmacy Benefits", "MEDICARE", "MAPD",
         "13", {"Zorvane": "PA", "Duvessa": "PA", "Quellora": "EXCLUDED", "Taluvia": "EXCLUDED"}),
    plan("M003", "Kestrelton Senior Rx", "Kestrelton Insurance", "Arbolin PBM", "MEDICARE", "PDP", "14",
         {"Zorvane": "EXCLUDED", "Duvessa": "PA", "Quellora": "PA", "Taluvia": "STEP"}),
    plan("M900", "Original Medicare (Parts A and B)", "Medicare fee-for-service", None, "MEDICARE", "FFS", "16"),
    plan("D100", "Orrin Valley Community Care", "Orrin Valley Health", "Arbolin PBM", "MANAGED_MEDICAID",
         "MANAGED_MEDICAID", "11", {"Zorvane": "PA", "Duvessa": "PA", "Quellora": "STEP", "Taluvia": "EXCLUDED"}),
    plan("X001", "Cash / self-pay", "Cash", None, "CASH", "CASH", "15"),
    plan("A101", "Zorvane Copay Savings Program", "Northwind Biologics", "Copay program processor", "ASSISTANCE",
         "COPAY_CARD", "6"),
    plan("A102", "Quellora Copay Card", "Northwind Biologics", "Copay program processor", "ASSISTANCE", "COPAY_CARD", "6"),
    plan("A103", "Duvessa Copay Program", "Fabrikam Pharma", "Copay program processor", "ASSISTANCE", "COPAY_CARD", "6"),
    plan("A104", "Taluvia Savings Card", "Fabrikam Pharma", "Copay program processor", "ASSISTANCE", "COPAY_CARD", "6"),
    plan("F101", "Northwind Patient Assistance Foundation", "Northwind Biologics", "Free product processor",
         "ASSISTANCE", "FREE_PRODUCT", "7"),
    plan("F102", "Fabrikam Cares Patient Assistance", "Fabrikam Pharma", "Free product processor", "ASSISTANCE",
         "FREE_PRODUCT", "7"),
]
MEDICAID_FFS_FORMULARY = {"Zorvane": "STEP", "Duvessa": "STEP", "Quellora": "PA", "Taluvia": "EXCLUDED"}
COPAY_PROGRAMS = {  # product -> (program plan, patient pays at least, annual program maximum)
    "Zorvane": ("A101", 5.0, 16000.0), "Quellora": ("A102", 5.0, 15000.0),
    "Duvessa": ("A103", 0.0, 13000.0), "Taluvia": ("A104", 10.0, 3000.0)}
FREE_PRODUCT = {"Northwind Biologics": "F101", "Fabrikam Pharma": "F102"}
BENEFITS = {  # commercial designs: (copay, amount) or (coins, rate) per tier
    "PPO": dict(deductible=500.0, ded_on_generic=False, generic=("copay", 10.0), brand=("copay", 60.0),
                specialty=("coins", 0.25), oop_max=6000.0),
    "HMO": dict(deductible=0.0, ded_on_generic=False, generic=("copay", 5.0), brand=("copay", 50.0),
                specialty=("copay", 150.0), oop_max=5000.0),
    "HDHP": dict(deductible=3200.0, ded_on_generic=True, generic=("coins", 0.2), brand=("coins", 0.2),
                 specialty=("coins", 0.2), oop_max=7000.0),
}
# Medicare Part D standard benefit: deductible, annual out-of-pocket threshold, low-income subsidy (LIS) copays
PART_D_DEDUCTIBLE = {2023: 505.0, 2024: 545.0, 2025: 590.0, 2026: 615.0}
PART_D_OOP_CAP = {2023: 7400.0, 2024: 8000.0, 2025: 2000.0, 2026: 2100.0}
LIS_COPAY = {2023: (4.15, 10.35), 2024: (4.50, 11.20), 2025: (4.90, 12.15)}
STATES = {  # state: (patient weight, ZIP3s)
    "CA": (.14, ["900", "941", "958"]), "TX": (.11, ["750", "770", "787"]), "FL": (.10, ["331", "336", "328"]),
    "NY": (.09, ["100", "112", "146"]), "PA": (.06, ["191", "152", "171"]), "IL": (.06, ["606", "617", "625"]),
    "OH": (.055, ["432", "441", "452"]), "GA": (.05, ["303", "317", "310"]), "NC": (.05, ["275", "282", "286"]),
    "MI": (.045, ["482", "495", "488"]), "NJ": (.04, ["070", "085", "077"]), "WA": (.04, ["980", "992", "983"]),
    "AZ": (.04, ["850", "857", "863"]), "MA": (.035, ["021", "010", "017"]), "CO": (.035, ["802", "809", "805"]),
}
PROVIDERS_PER_STATE = {"Dermatology": 6, "Rheumatology": 3, "Family Medicine": 2, "Internal Medicine": 2,
                       "Nurse Practitioner": 1, "Infusion Center": 1}
REJECT_CODES = [  # code, description, group (NCPDP codes; descriptions paraphrased)
    ("70", "Product or service not covered (plan exclusion)", "PRODUCT_NOT_COVERED"),
    ("MR", "Product not on formulary", "PRODUCT_NOT_COVERED"),
    ("75", "Prior authorization required", "PRIOR_AUTHORIZATION"),
    ("3Y", "Prior authorization denied", "PRIOR_AUTHORIZATION"),
    ("608", "Step therapy: alternate drug required first", "PRIOR_AUTHORIZATION"),
    ("79", "Refill too soon", "REFILL_RESTRICTION"),
    ("4W", "Must fill through specialty pharmacy", "DISTRIBUTION_LIMITATION"),
    ("76", "Plan limitations exceeded", "PLAN_LIMITATION"),
    ("7X", "Days supply exceeds plan limitation", "PLAN_LIMITATION"),
    ("65", "Patient not covered", "ELIGIBILITY"),
    ("69", "Filled after coverage terminated", "ELIGIBILITY"),
    ("88", "DUR reject", "CLINICAL"),
    ("07", "Missing or invalid cardholder ID", "ADMINISTRATIVE"),
    ("52", "Non-matched cardholder ID", "ADMINISTRATIVE"),
    ("M6", "Host eligibility error", "ADMINISTRATIVE"),
    ("41", "Submit bill to other processor or primary payer", "ADMINISTRATIVE"),
    ("887", "Previous payer is a federal program; copay assistance not allowed", "ADMINISTRATIVE"),
]
ADMIN_REJECTS = {"07": .35, "52": .25, "M6": .25, "41": .15}
DIAGNOSES = [
    ("L400", "Psoriasis vulgaris", "PSORIASIS"),
    ("L4050", "Arthropathic psoriasis, unspecified", "PSORIATIC_ARTHRITIS"),
    ("L4059", "Other psoriatic arthropathy", "PSORIATIC_ARTHRITIS"),
    ("L209", "Atopic dermatitis, unspecified", "ATOPIC_DERMATITIS"),
    ("L2089", "Other atopic dermatitis", "ATOPIC_DERMATITIS"),
    ("I10", "Essential hypertension", "COMORBIDITY"),
    ("E119", "Type 2 diabetes without complications", "COMORBIDITY"),
    ("E785", "Hyperlipidemia, unspecified", "COMORBIDITY"),
    ("F32A", "Depression, unspecified", "COMORBIDITY"),
    ("Z111", "Screening for respiratory tuberculosis", "SCREENING"),
]
PROCEDURES = [  # code, system, description, charge
    ("99204", "CPT", "New patient office visit, moderate complexity", 265.0),
    ("99213", "CPT", "Established patient office visit, low complexity", 135.0),
    ("99214", "CPT", "Established patient office visit, moderate complexity", 195.0),
    ("86480", "CPT", "Tuberculosis test, interferon-gamma release assay", 120.0),
    ("86580", "CPT", "Tuberculosis skin test", 30.0),
    ("96365", "CPT", "IV infusion, therapeutic, first hour", 290.0),
    ("J3590", "HCPCS", "Unclassified biologics (billed with NDC)", None),
]
CHARGES = {code: charge for code, _, _, charge in PROCEDURES}
ADJUSTMENT_REASONS = [  # X12 CARC codes; descriptions paraphrased
    ("1", "Deductible", "PATIENT_COST_SHARE"),
    ("2", "Coinsurance", "PATIENT_COST_SHARE"),
    ("3", "Copay", "PATIENT_COST_SHARE"),
    ("45", "Charge above the contracted or allowed amount", "CONTRACTUAL"),
    ("197", "Prior authorization absent", "AUTHORIZATION"),
    ("50", "Not deemed medically necessary by the payer", "COVERAGE"),
    ("16", "Claim lacks information or has a billing error", "ADMINISTRATIVE"),
]
CANDIDATES = {"PSO": {"Zorvane": .4, "Quellora": .3, "Velnexa": .3},
              "PSA": {"Zorvane": .4, "Quellora": .35, "Velnexa": .25},
              "AD": {"Duvessa": .55, "Quellora": .45}}
INDICATION_CODES = {"PSO": ["L400"], "PSA": ["L4050", "L4059"], "AD": ["L209", "L2089"]}
COMORBIDITIES = {"I10": .25, "E119": .12, "E785": .2, "F32A": .15}


def pick(r, weights):
    """Weighted choice from a {value: weight} dict."""
    roll, total = r.random() * sum(weights.values()), 0.0
    for value, weight in weights.items():
        total += weight
        if roll < total:
            return value
    return value


def by_year(values, year):
    """Value for the year, else the latest earlier year, else the earliest year."""
    earlier = [y for y in values if y <= year]
    return values[max(earlier)] if earlier else values[min(values)]


def abandon_probability(oop, brand, new_start):
    """Chance a paid claim is reversed (never picked up). Brand new starts run about 10% at $0
    out-of-pocket and near 60% above $250; generics and refills abandon less."""
    for limit, chance in ((0.01, .10), (20, .12), (50, .18), (125, .28), (250, .42), (float("inf"), .58)):
        if oop < limit:
            break
    return chance * (1.0 if brand else .4) * (1.0 if new_start else .35)


class Patient:
    def __init__(self, **values):
        self.__dict__.update(values)


class Generator:
    def __init__(self, seed=42, patients=2000, start=date(2023, 7, 1), end=date(2026, 6, 30)):
        self.r, self.seed, self.n, self.start, self.end = random.Random(seed), seed, patients, start, end
        self.rows = {t["name"]: [] for t in SCHEMA}
        self.counters = defaultdict(int)
        self.products = {p["ndc11"]: p for p in PRODUCTS}
        self.by_name = defaultdict(list)
        for p in PRODUCTS:
            self.by_name[p["product_name"]].append(p)
        self.plans = {}

    # ---- identifiers
    def token(self, prefix, kind, n, width=10):
        return prefix + hashlib.sha256(f"{self.seed}:{kind}:{n}".encode()).hexdigest()[:width].upper()

    def next_id(self, prefix, kind):
        self.counters[kind] += 1
        return f"{prefix}{100000000 + self.counters[kind]}"

    def new_rx(self, d):
        """A new prescription token and its written date."""
        self.counters["rx"] += 1
        return self.token("RX", "rx", self.counters["rx"]), d - timedelta(days=self.r.randint(0, 3))

    # ---- reference files
    def build_reference(self):
        r, rows = self.r, self.rows
        rows["ref_product"] = PRODUCTS
        plans = [dict(p) for p in PLANS]
        for state in STATES:
            plans.append(plan(f"MCD-{state}", f"{state} Medicaid fee-for-service", f"{state} Medicaid agency",
                              "State fiscal agent", "MEDICAID_FFS", "FFS", "1", MEDICAID_FFS_FORMULARY, state=state))
        rows["ref_plan"] = plans
        self.plans = {p["plan_id"]: p for p in plans}
        self.providers = defaultdict(lambda: defaultdict(list))
        self.pharmacies = defaultdict(lambda: defaultdict(list))
        for state, (_, zips) in STATES.items():
            for specialty, count in PROVIDERS_PER_STATE.items():
                for _ in range(count):
                    pid = self.token("PR", "provider", len(rows["ref_provider"]), 8)
                    rows["ref_provider"].append(dict(
                        provider_id=pid, specialty=specialty, provider_state=state,
                        provider_zip5=r.choice(zips) + f"{r.randint(1, 99):02d}",
                        provider_type="ORGANIZATION" if specialty == "Infusion Center" else "INDIVIDUAL"))
                    self.providers[state][specialty].append(pid)
            for channel, count in (("RETAIL_CHAIN", 3), ("RETAIL_INDEPENDENT", 2)):
                for _ in range(count):
                    self.add_pharmacy(channel, state, r.choice(zips))
        for channel, count in (("MAIL_ORDER", 2), ("SPECIALTY", 4)):
            for _ in range(count):
                state = r.choice(list(STATES))
                self.add_pharmacy(channel, state, r.choice(STATES[state][1]), national=True)
        rows["ref_reject_code"] = [dict(reject_code=c, reject_description=d, reject_group=g) for c, d, g in REJECT_CODES]
        rows["ref_diagnosis"] = [dict(icd10_code=c, icd10_description=d, condition_group=g) for c, d, g in DIAGNOSES]
        rows["ref_procedure"] = [dict(procedure_code=c, code_system=s, procedure_description=d) for c, s, d, _ in PROCEDURES]
        rows["ref_adjustment_reason"] = [dict(carc_code=c, carc_description=d, adjustment_category=g)
                                         for c, d, g in ADJUSTMENT_REASONS]

    def add_pharmacy(self, channel, state, zip3, national=False):
        pid = self.token("PH", "pharmacy", len(self.rows["ref_pharmacy"]), 8)
        service_type = {"MAIL_ORDER": "6", "SPECIALTY": "8"}.get(channel, "1")
        self.rows["ref_pharmacy"].append(dict(pharmacy_id=pid, pharmacy_service_type=service_type,
                                              pharmacy_channel=channel, pharmacy_state=state, pharmacy_zip3=zip3))
        self.pharmacies["*" if national else state][channel].append(pid)

    # ---- patients and coverage
    def simulate(self, i):
        r = self.r
        ind = pick(r, {"PSO": .55, "PSA": .20, "AD": .25})
        age = int(r.triangular(18, 82, 30 if ind == "AD" else 50))
        state = pick(r, {s: w for s, (w, _) in STATES.items()})
        here = self.providers[state]
        p = Patient(
            id=self.token("P", "patient", i, 11), indication=ind, birth_year=self.start.year - age, state=state,
            gender="U" if r.random() < .01 else ("F" if r.random() < (.56 if ind == "AD" else .5) else "M"),
            lis=r.random() < .25,
            comorbid=[c for c, chance in COMORBIDITIES.items() if r.random() < chance * (1.5 if age >= 55 else .7)],
            specialist=r.choice(here["Rheumatology" if ind == "PSA" else "Dermatology"]),
            pcp=r.choice(here["Family Medicine"] + here["Internal Medicine"] + here["Nurse Practitioner"]),
            infusion_site=r.choice(here["Infusion Center"]),
            retail=r.choice(self.pharmacies[state]["RETAIL_CHAIN"] * 2 + self.pharmacies[state]["RETAIL_INDEPENDENT"]),
            specialty_rx=r.choice(self.pharmacies["*"]["SPECIALTY"]), mail=r.choice(self.pharmacies["*"]["MAIL_ORDER"]),
            uses_mail=r.random() < .2, pcp_prescribes_conventional=r.random() < .25, mild=False,
            pa={}, copay_enrolled=set(), conventional_history=False, last_rx_plan=None, tb_done=False,
            blocked_by_exclusion=False, acc=defaultdict(lambda: {"ded": 0.0, "oop": 0.0}),
            program_used=defaultdict(float), part_d_oop=defaultdict(float))
        index = self.start + timedelta(days=r.randint(0, int((self.end - self.start).days * .8)))
        p.timeline = self.plan_timeline(p)
        self.rows["patient"].append(dict(patient_id=p.id, birth_year=p.birth_year, gender=p.gender,
                                         patient_state=state, patient_zip3=r.choice(STATES[state][1])))
        self.visits(p, index)
        self.maintenance(p)
        self.journey(p, index)

    def plan_timeline(self, p):
        """Coverage changes: January plan switches, Medicaid to commercial, aging into Medicare."""
        r = self.r

        def choose(channel, current=None):
            options = {"COMMERCIAL": {"C001": .3, "C002": .2, "C003": .2, "C004": .15, "C005": .15},
                       "MEDICARE": {"M001": .35, "M002": .4, "M003": .25},
                       "MEDICAID": {f"MCD-{p.state}": .55, "D100": .45},
                       "CASH": {"X001": 1.0}}[channel]
            options = {k: v for k, v in options.items() if k != current} or options
            return pick(r, options)

        age = self.start.year - p.birth_year
        channel = "MEDICARE" if age >= 65 else pick(r, {"COMMERCIAL": .78, "MEDICAID": .2, "CASH": .02})
        current = choose(channel)
        events = [(date.min, current)]
        turns_65 = p.birth_year + 65
        medicare_at = date(turns_65, r.randint(1, 12), 1) if self.start.year < turns_65 <= self.end.year else None
        for year in range(self.start.year + 1, self.end.year + 1):
            if medicare_at and medicare_at.year < year and channel != "MEDICARE":
                break
            roll = r.random()
            if channel == "COMMERCIAL" and roll < .10:
                current = choose("COMMERCIAL", current)
            elif channel == "MEDICAID" and roll < .12:
                channel, current = "COMMERCIAL", choose("COMMERCIAL")
            elif channel == "MEDICARE" and roll < .06:
                current = choose("MEDICARE", current)
            else:
                continue
            events.append((date(year, 1, 1), current))
        if medicare_at and channel != "MEDICARE":
            events = [e for e in events if e[0] < medicare_at] + [(medicare_at, choose("MEDICARE"))]
        return events

    def plan_at(self, p, d):
        current = p.timeline[0][1]
        for start, plan_id in p.timeline:
            if start <= d:
                current = plan_id
        return self.plans[current]

    def medical_plan(self, p, d):
        coverage = self.plan_at(p, d)
        return "M900" if coverage["plan_type"] == "PDP" else coverage["plan_id"]

    # ---- medical claims and remittance
    def dx_codes(self, p, comorbid_chance=.5):
        r = self.r
        codes = [r.choice(INDICATION_CODES[p.indication])]
        if p.indication == "PSA" and r.random() < .7:
            codes.append("L400")
        return codes + [c for c in p.comorbid if r.random() < comorbid_chance]

    def dx_claim(self, p, d, provider, lines, codes, institutional=False, pos="11", billing=None, referring=None,
                 denial=None):
        """One 837 claim plus, for most claims, its 835 remittance."""
        r, rows = self.r, self.rows
        claim_id = self.next_id("MC", "dx")
        total = 0.0
        for n, (code, ndc, units) in enumerate(lines, 1):
            charge = round(self.products[ndc]["cost"] * 1.3 * units if ndc else CHARGES[code] * units, 2)
            total += charge
            rows["dx_claim_line"].append(dict(claim_id=claim_id, line_number=n, service_date=d, procedure_code=code,
                                              ndc11=ndc, units=units, line_charge_amt=charge))
        for seq, code in enumerate(dict.fromkeys(codes), 1):
            rows["dx_claim_diagnosis"].append(dict(claim_id=claim_id, diagnosis_seq=seq, icd10_code=code))
        plan_id = self.medical_plan(p, d)
        rows["dx_claim"].append(dict(
            claim_id=claim_id, patient_id=p.id, claim_type="INSTITUTIONAL" if institutional else "PROFESSIONAL",
            service_from_date=d, service_to_date=d, place_of_service=None if institutional else pos,
            bill_type="0131" if institutional else None, rendering_provider_id=provider,
            billing_provider_id=billing or provider, referring_provider_id=referring, plan_id=plan_id,
            total_charge_amt=round(total, 2)))
        if plan_id != "X001" and (denial or r.random() < .7):
            self.remit(p, claim_id, plan_id, d, round(total, 2), denial or ("16" if r.random() < .02 else None))

    def remit(self, p, claim_id, plan_id, d, charge, denial):
        r = self.r
        remit_id = self.next_id("RM", "remit")
        adjustments = []
        if denial:
            allowed = paid = patient = 0.0
            status = "4"
            adjustments.append(("CO", denial, charge))
        else:
            allowed = round(charge * r.uniform(.55, .8), 2)
            channel = self.plans[plan_id]["payment_type"]
            if channel in ("MEDICAID_FFS", "MANAGED_MEDICAID"):
                patient, carc = 0.0, None
            elif channel == "MEDICARE" or allowed >= 500:
                patient, carc = round(allowed * .2, 2), "2"
            else:
                patient, carc = min(allowed, r.choice([0.0, 30.0, 50.0])), "3"
            paid, status = round(allowed - patient, 2), "1"
            adjustments.append(("CO", "45", round(charge - allowed, 2)))
            if patient:
                adjustments.append(("PR", carc, patient))
        self.rows["dx_remittance"].append(dict(remit_id=remit_id, claim_id=claim_id, plan_id=plan_id,
                                               paid_date=d + timedelta(days=r.randint(14, 45)),
                                               claim_status_code=status, allowed_amt=allowed, paid_amt=paid,
                                               patient_resp_amt=patient))
        for seq, (group, code, amount) in enumerate(adjustments, 1):
            self.rows["dx_remittance_adjustment"].append(dict(remit_id=remit_id, adjustment_seq=seq, group_code=group,
                                                              carc_code=code, adjustment_amt=amount))

    def visits(self, p, index):
        """Diagnosis visit, specialist follow-ups, and primary-care visits for comorbidities."""
        r = self.r
        d = max(self.start, index - timedelta(days=r.randint(0, 30)))
        referral = p.pcp if r.random() < .5 else None
        self.dx_claim(p, d, p.specialist, [("99204", None, 1)], self.dx_codes(p), referring=referral)
        while True:
            d += timedelta(days=r.randint(75, 180))
            if d > self.end or r.random() < .07:
                break
            self.dx_claim(p, d, p.specialist, [(r.choice(["99213", "99214"]), None, 1)], self.dx_codes(p),
                          pos="10" if r.random() < .12 else "11")
        if p.comorbid:
            d = self.start + timedelta(days=r.randint(0, 180))
            while d <= self.end:
                codes = r.sample(p.comorbid, len(p.comorbid)) + (self.dx_codes(p, 0)[:1] if r.random() < .3 else [])
                self.dx_claim(p, d, p.pcp, [("99213", None, 1)], codes)
                d += timedelta(days=r.randint(150, 330))

    def tb_screen(self, p, d):
        """Tuberculosis test before a first systemic biologic or JAK."""
        if p.tb_done:
            return
        p.tb_done = True
        when = max(self.start, d - timedelta(days=self.r.randint(14, 45)))
        self.dx_claim(p, when, p.specialist, [(self.r.choice(["86480", "86580"]), None, 1)],
                      ["Z111"] + self.dx_codes(p, 0)[:1])

    # ---- treatment
    def maintenance(self, p):
        """Generic maintenance fills for comorbidities, from the start of the data window."""
        for code in p.comorbid:
            prod = self.by_name[MAINTENANCE[code]][0]
            start = self.start + timedelta(days=self.r.randint(0, 120))
            self.pharmacy_course(p, prod, start, (self.end - start).days + 30, prescriber=p.pcp)

    def journey(self, p, index):
        r, t = self.r, index
        severe = r.random() < .3
        p.mild = p.indication in ("PSO", "AD") and not severe and r.random() < .3
        if not severe:
            conventional = ("methotrexate" if p.indication == "PSA" or (p.indication == "PSO" and not p.mild and r.random() < .5)
                            else "triamcinolone")
            prescriber = p.pcp if p.pcp_prescribes_conventional else p.specialist
            t, _ = self.pharmacy_course(p, self.by_name[conventional][0], t, r.randint(60, 300), prescriber=prescriber)
            p.conventional_history = True
            if r.random() < .3:
                return
            t += timedelta(days=r.randint(0, 30))
        tried, switches = set(), 0
        while t <= self.end and switches < 3:
            name = self.choose_branded(p, t, tried)
            if not name:
                return
            tried.add(name)
            t, status = self.course(p, name, t)
            if status == "END":
                return
            if status == "BLOCKED":  # never started, or a new plan stopped covering it: try an alternative
                t += timedelta(days=r.randint(7, 28))
                continue
            if r.random() >= .55:
                return
            switches += 1
            t += timedelta(days=r.randint(10, 45))

    def choose_branded(self, p, t, tried):
        r = self.r
        if p.mild:
            if "Taluvia" not in tried:
                return "Taluvia"
            if r.random() >= .25:
                return None
            p.mild = False
        coverage = self.plan_at(p, t)
        weights = {}
        for name, weight in CANDIDATES[p.indication].items():
            if name in tried:
                continue
            status = coverage["formulary"].get(name, "PREFERRED")
            if self.by_name[name][0]["benefit_type"] == "MEDICAL":
                status = "MEDICAL"
            if status == "EXCLUDED" and p.blocked_by_exclusion:  # prescribers learn the formulary from the reject
                weight = 0
            elif status == "STEP":
                weight *= .8
            if weight > 0:
                weights[name] = weight
        return pick(r, weights) if weights else None

    def course(self, p, name, t):
        r = self.r
        options = self.by_name[name]
        prod = options[1] if len(options) > 1 and p.indication == "AD" and r.random() < .3 else options[0]
        if name in COPAY_PROGRAMS and self.plan_at(p, t)["payment_type"] == "COMMERCIAL" and r.random() < .6:
            p.copay_enrolled.add(name)
        if name in ("Zorvane", "Quellora", "Velnexa"):
            self.tb_screen(p, t)
        length = int(r.weibullvariate(240 if name == "Taluvia" else 520, 1.15))
        if prod["benefit_type"] == "MEDICAL":
            return self.infusion_course(p, prod, t, length)
        return self.pharmacy_course(p, prod, t, length, prescriber=p.specialist)

    def infusion_course(self, p, prod, t, length):
        """Buy-and-bill IV product: administrations appear only on medical claims, and a missing
        authorization surfaces as a denied remittance (CARC 197) rather than a pharmacy reject."""
        r = self.r
        stop = t + timedelta(days=length)
        hospital = r.random() < .5  # hospital outpatient: the infusion center bills, the specialist renders
        billing = p.infusion_site if hospital else None
        lines = [("96365", None, 1), ("J3590", prod["ndc11"], 1)]
        if r.random() < .25:  # first infusion billed before the authorization was in place
            self.dx_claim(p, t, p.specialist, lines, self.dx_codes(p, 0), institutional=hospital, billing=billing,
                          denial="197")
            t += timedelta(days=r.randint(10, 30))
        while t <= self.end:
            self.dx_claim(p, t, p.specialist, lines, self.dx_codes(p, 0), institutional=hospital, billing=billing)
            t += timedelta(days=prod["days"] + max(0, int(r.gauss(2, 4))))
            if t > stop:
                return t, "DISCONTINUED"
        return t, "END"

    def pharmacy_course(self, p, prod, t, length, prescriber):
        r = self.r
        stop = t + timedelta(days=length)
        (rx_id, written), fill, rx_start, dispensed = self.new_rx(t), 0, t, 0
        while t <= self.end:
            outcome, when, days = self.fill(p, prod, rx_id, written, fill, t, dispensed == 0, prescriber)
            if outcome == "DISPENSED":
                dispensed += 1
                gap = max(0, int(r.gauss(3, 5))) + (r.randint(20, 50) if r.random() < .06 else 0)
                t = when + timedelta(days=days + gap)
                if t > stop:
                    return t, "DISCONTINUED"
                fill += 1
                if fill > 11 or (t - rx_start).days > 330:  # prescription expires: new script
                    (rx_id, written), fill, rx_start = self.new_rx(t), 0, t
                continue
            if outcome == "REVERSED":
                name = prod["product_name"]
                if name in COPAY_PROGRAMS and self.plan_at(p, t)["payment_type"] == "COMMERCIAL" and r.random() < .5:
                    p.copay_enrolled.add(name)
                if dispensed == 0 and r.random() >= .45:
                    return t + timedelta(days=r.randint(7, 30)), "BLOCKED"
                if dispensed > 0 and r.random() < .4:
                    return t + timedelta(days=r.randint(7, 30)), "DISCONTINUED"
                t += timedelta(days=r.randint(3, 12))
                (rx_id, written), fill, rx_start = self.new_rx(t), 0, t
                continue
            return t + timedelta(days=r.randint(7, 21)), "BLOCKED"
        return t, "END"

    # ---- pharmacy adjudication
    def rx_row(self, base, d, plan_id, status, coverage_seq=1, transaction_type="B1", **fields):
        row = dict(base, claim_id=self.next_id("RC", "rx_claim"), transaction_type=transaction_type,
                   claim_status=status, coverage_seq=coverage_seq, service_date=d, transaction_date=d,
                   plan_id=plan_id, final_claim_flag=False, **fields)
        self.rows["rx_claim"].append(row)
        return row

    def reject(self, base, d, plan_id, codes, coverage_seq=1):
        row = self.rx_row(base, d, plan_id, "REJECTED", coverage_seq)
        for seq, code in enumerate(codes, 1):
            self.rows["rx_claim_reject"].append(dict(claim_id=row["claim_id"], reject_seq=seq, reject_code=code))
        return row

    def reversal(self, paid, d):
        keep = ("rx_id", "fill_number", "patient_id", "date_written", "ndc11", "quantity", "days_supply", "daw_code",
                "prescriber_id", "pharmacy_id", "other_coverage_code")
        row = self.rx_row({k: paid[k] for k in keep}, paid["service_date"], paid["plan_id"], "REVERSED",
                          paid["coverage_seq"], "B2", reversal_of_claim_id=paid["claim_id"])
        row["transaction_date"] = d
        return row

    def fill(self, p, prod, rx_id, written, fill, d, new_start, prescriber):
        """Submit one fill: rejects and resubmissions, the paid claim, a copay program or free product,
        then pickup or reversal. Returns (outcome, date, days supply)."""
        r = self.r
        coverage = self.plan_at(p, d)
        plan_id, name = coverage["plan_id"], prod["product_name"]
        brand = prod["brand_generic"] == "BRAND"
        pharmacy, days = self.dispensing(p, prod)
        base = dict(rx_id=rx_id, fill_number=fill, patient_id=p.id, date_written=written, ndc11=prod["ndc11"],
                    days_supply=days, quantity=round(prod["qty"] * days / prod["days"], 3),
                    daw_code="1" if r.random() < .03 else "0", prescriber_id=prescriber, pharmacy_id=pharmacy,
                    other_coverage_code="01")
        rejected = 0
        if p.last_rx_plan and p.last_rx_plan != plan_id and r.random() < .35:  # pharmacy billed the old plan
            self.reject(base, d, p.last_rx_plan, ["69"] if r.random() < .5 else ["65"])
            rejected += 1
        p.last_rx_plan = plan_id
        if r.random() < (.1 if new_start else .025):  # administrative reject, fixed the same day
            self.reject(base, d, plan_id, [pick(r, ADMIN_REJECTS)])
            rejected += 1
        if prod["specialty_flag"] and pharmacy != p.specialty_rx and coverage["payment_type"] != "CASH":
            self.reject(base, d, plan_id, ["4W"])  # retail tried first: transfer to the specialty pharmacy
            rejected += 1
            base["pharmacy_id"] = pharmacy = p.specialty_rx
            d += timedelta(days=r.randint(1, 5))
        status = "COVERED" if not brand or coverage["payment_type"] == "CASH" else coverage["formulary"].get(name, "PREFERRED")
        key = (name, plan_id)
        approved_until = p.pa.get(key)
        if status == "EXCLUDED" and not (approved_until and approved_until >= d):
            self.reject(base, d, plan_id, ["70", "MR"] if r.random() < .5 else ["70"])
            rejected += 1
            p.blocked_by_exclusion = True
            if r.random() >= .1:
                return self.after_denial(p, prod, base, d, days)
            d += timedelta(days=r.randint(7, 30))  # formulary exception granted
            p.pa[key] = d + timedelta(days=365)
        elif status in ("PA", "STEP") and not (approved_until and approved_until >= d):
            # published PA processing: 59% of fills see more than one PA review, 35% settle the same day
            renewal = approved_until is not None
            step = status == "STEP" and not p.conventional_history and not renewal
            self.reject(base, d, plan_id, ["608", "75"] if step else ["75"])
            rejected += 1
            multiple = r.random() < (.3 if renewal else .59)
            same_day = r.random() < (.25 if multiple else .8)
            if multiple:  # pharmacy resubmits while the review is pending
                for _ in range(1 + min(2, int(r.expovariate(1.0)))):
                    d += timedelta(days=0 if same_day else r.randint(1, 4))
                    self.reject(base, d, plan_id, ["75"])
                    rejected += 1
            if r.random() >= (.9 if renewal else .25 if step else .52):
                if r.random() < .5:
                    d += timedelta(days=r.randint(1, 10))
                    self.reject(base, d, plan_id, ["3Y"])
                return self.after_denial(p, prod, base, d, days)
            d += timedelta(days=0 if same_day else r.randint(1, 14))
            p.pa[key] = d + timedelta(days=365)
        if not rejected and not new_start and r.random() < .04:
            self.reject(base, d - timedelta(days=r.randint(4, 8)), plan_id, ["79"])
            rejected += 1
        if new_start and prod["specialty_flag"] and r.random() < .08:  # loading-dose quantity over the plan limit
            self.reject(base, d, plan_id, ["76"])
            d += timedelta(days=1)
        if not brand and days > 30 and r.random() < .05:
            self.reject(base, d, plan_id, ["7X"])
            base["days_supply"] = days = prod["days"]
            base["quantity"] = prod["qty"]
        if r.random() < .01:
            self.reject(base, d, plan_id, ["88"])
        if d > self.end:
            return "REJECTED", d, days
        return self.pay(p, prod, coverage, base, d, days, new_start)

    def after_denial(self, p, prod, base, d, days):
        """Denied brand: a few patients get free product from the manufacturer or pay cash."""
        r = self.r
        roll = r.random()
        if prod["brand_generic"] == "BRAND" and roll < .07 and d + timedelta(days=21) <= self.end:
            when = d + timedelta(days=r.randint(7, 21))
            program = self.plans[FREE_PRODUCT[prod["manufacturer_name"]]]
            self.rx_row(dict(base), when, program["plan_id"], "PAID", plan_pay_amt=round(prod["cost"] * days / prod["days"], 2),
                        patient_pay_amt=0.0, deductible_amt=0.0, copay_amt=0.0, coinsurance_amt=0.0)
            return "DISPENSED", when, days
        if roll < .08:
            cash = self.plans["X001"]
            gross = round(prod["cost"] * days / prod["days"], 2)
            self.rx_row(dict(base), d, cash["plan_id"], "PAID", plan_pay_amt=0.0, patient_pay_amt=gross)
            return "DISPENSED", d, days
        return "REJECTED", d, days

    def pay(self, p, prod, coverage, base, d, days, new_start):
        r = self.r
        name, plan_id = prod["product_name"], coverage["plan_id"]
        brand = prod["brand_generic"] == "BRAND"
        gross = round(prod["cost"] * days / prod["days"], 2)
        pay, ded, copay, coins, stage = self.cost_share(p, coverage, prod, gross, d)
        primary = self.rx_row(base, d, plan_id, "PAID", plan_pay_amt=round(gross - pay, 2), patient_pay_amt=pay,
                              deductible_amt=ded, copay_amt=copay, coinsurance_amt=coins, benefit_stage=stage)
        final, secondary, covered = pay, None, 0.0
        if name in COPAY_PROGRAMS and name in p.copay_enrolled and pay > 0:
            program, floor, annual_max = COPAY_PROGRAMS[name]
            secondary_base = dict(base, other_coverage_code="08")
            if coverage["payment_type"] != "COMMERCIAL":  # federal primary: the program must refuse
                if r.random() < .3:
                    self.reject(secondary_base, d, program, ["887"], coverage_seq=2)
            else:
                covered = round(min(max(pay - floor, 0.0), annual_max - p.program_used[(d.year, program)]), 2)
                if covered > 0:
                    final = round(pay - covered, 2)
                    secondary = self.rx_row(secondary_base, d, program, "PAID", 2, plan_pay_amt=covered,
                                            patient_pay_amt=final, deductible_amt=0.0, copay_amt=final,
                                            coinsurance_amt=0.0)
        if r.random() < abandon_probability(final, brand, new_start):
            back = d + timedelta(days=r.randint(5, 14))  # returned to stock
            for claim in (primary, secondary):
                if claim:
                    self.reversal(claim, back)
            return "REVERSED", d, days
        # picked up: accumulators. Accumulator plans credit only what the patient paid, not the copay program.
        acc = p.acc[(d.year, plan_id)]
        share = (final / pay if pay else 0.0) if coverage.get("accumulator") else 1.0
        acc["ded"] += (ded or 0.0) * share
        acc["oop"] += pay * share
        if coverage["payment_type"] == "MEDICARE":
            p.part_d_oop[d.year] += pay
        if secondary:
            p.program_used[(d.year, secondary["plan_id"])] += covered
        return "DISPENSED", d, days

    def dispensing(self, p, prod):
        if prod["specialty_flag"]:
            return (p.specialty_rx if self.r.random() < .95 else p.retail), prod["days"]
        if prod["brand_generic"] == "GENERIC" and p.uses_mail:
            return p.mail, prod["days"] * 3
        return p.retail, prod["days"]

    def cost_share(self, p, coverage, prod, gross, d):
        """Patient responsibility on the primary plan: (patient pay, deductible, copay, coinsurance, Part D stage)."""
        payment, year = coverage["payment_type"], d.year
        generic = prod["brand_generic"] == "GENERIC"
        tier = "generic" if generic else "specialty" if prod["specialty_flag"] else "brand"
        if payment == "CASH":
            return gross, None, None, None, None
        if payment in ("MEDICAID_FFS", "MANAGED_MEDICAID"):
            c = min(gross, 1.0 if generic else 3.0)
            return c, 0.0, c, 0.0, None
        if payment == "MEDICARE":
            if p.lis:
                c = min(gross, by_year(LIS_COPAY, year)[0 if generic else 1])
                return c, 0.0, c, 0.0, "4" if p.part_d_oop[year] >= by_year(PART_D_OOP_CAP, year) else "2"
            design = dict(deductible=by_year(PART_D_DEDUCTIBLE, year), ded_on_generic=False, generic=("copay", 5.0),
                          brand=("coins", .25), specialty=("coins", .25), oop_max=by_year(PART_D_OOP_CAP, year),
                          catastrophic=.05 if year < 2024 else 0.0)
        else:
            design = BENEFITS[coverage["plan_type"]]
        acc = p.acc[(year, coverage["plan_id"])]
        room = max(0.0, design["oop_max"] - acc["oop"])
        if room <= 0:
            c = round(gross * design.get("catastrophic", 0.0), 2)
            return c, 0.0, 0.0, c, "4" if payment == "MEDICARE" else None
        ded = 0.0
        if design["deductible"] and (tier != "generic" or design["ded_on_generic"]):
            ded = min(gross, max(0.0, design["deductible"] - acc["ded"]))
        kind, value = design[tier]
        rest = gross - ded
        share = min(value, rest) if kind == "copay" else rest * value
        ded = round(min(ded, room), 2)
        share = round(max(0.0, min(share, room - ded)), 2)
        copay, coins = (share, 0.0) if kind == "copay" else (0.0, share)
        stage = ("1" if ded > 0 else "2") if payment == "MEDICARE" else None
        return round(ded + share, 2), ded, copay, coins, stage

    # ---- derived tables
    def derive(self):
        rows = self.rows
        by_fill = defaultdict(list)
        for c in rows["rx_claim"]:
            by_fill[(c["rx_id"], c["fill_number"])].append(c)
        for claims in by_fill.values():  # look-forward: the last primary transaction settles the fill
            primary = [c for c in claims if c["coverage_seq"] == 1]
            max(primary, key=lambda c: (c["transaction_date"], c["claim_id"]))["final_claim_flag"] = True
            for c in claims:
                if c["coverage_seq"] == 2 and not any(x["coverage_seq"] == 2 and x["claim_id"] > c["claim_id"] for x in claims):
                    c["final_claim_flag"] = True
        first_code = {x["claim_id"]: x["reject_code"] for x in rows["rx_claim_reject"] if x["reject_seq"] == 1}
        reversed_ids = {c["reversal_of_claim_id"] for c in rows["rx_claim"] if c["claim_status"] == "REVERSED"}
        for (rx_id, fill), claims in by_fill.items():
            primary = [c for c in claims if c["coverage_seq"] == 1]
            submissions = [c for c in primary if c["transaction_type"] == "B1"]
            first = min(submissions, key=lambda c: (c["transaction_date"], c["claim_id"]))
            final = next(c for c in primary if c["final_claim_flag"])
            paid = next((c for c in submissions if c["claim_status"] == "PAID"), None)
            secondary = next((c for c in claims if c["coverage_seq"] == 2 and c["claim_status"] == "PAID"), None)
            rejects = [c for c in submissions if c["claim_status"] == "REJECTED"]
            settled_plan = self.plans[(paid or final)["plan_id"]]
            rows["rx_fill"].append(dict(
                rx_id=rx_id, fill_number=fill, patient_id=first["patient_id"], ndc11=final["ndc11"],
                prescriber_id=first["prescriber_id"], primary_plan_id=final["plan_id"],
                final_payment_type=settled_plan["payment_type"], first_submit_date=first["transaction_date"],
                final_date=final["transaction_date"], initial_status=first["claim_status"],
                final_status="REVERSED" if paid and paid["claim_id"] in reversed_ids else final["claim_status"],
                reject_count=len(rejects), first_reject_code=first_code.get(rejects[0]["claim_id"]) if rejects else None,
                days_to_final=(final["transaction_date"] - first["transaction_date"]).days,
                total_claim_cost=round(paid["plan_pay_amt"] + paid["patient_pay_amt"], 2) if paid else None,
                oop_before_buydown=paid["patient_pay_amt"] if paid else None,
                buydown_amt=secondary["plan_pay_amt"] if secondary else 0.0,
                oop_after_buydown=(secondary or paid)["patient_pay_amt"] if paid else None,
                copay_card_flag=secondary is not None))

        events = defaultdict(list)  # (patient, molecule) -> [(date, days supply, product)]
        for c in rows["rx_claim"]:
            if c["coverage_seq"] == 1 and c["claim_status"] == "PAID" and c["claim_id"] not in reversed_ids:
                prod = self.products[c["ndc11"]]
                events[(c["patient_id"], prod["molecule_name"])].append((c["service_date"], c["days_supply"], prod))
        claim_patient = {c["claim_id"]: c["patient_id"] for c in rows["dx_claim"]}
        denied = {x["claim_id"] for x in rows["dx_remittance"] if x["claim_status_code"] == "4"}
        for line in rows["dx_claim_line"]:
            if line["ndc11"] and line["claim_id"] not in denied:
                prod = self.products[line["ndc11"]]
                events[(claim_patient[line["claim_id"]], prod["molecule_name"])].append(
                    (line["service_date"], prod["days"], prod))
        by_patient = defaultdict(list)
        for (patient_id, molecule), items in events.items():
            items.sort(key=lambda e: e[0])
            episode = None
            for d, days, prod in items:
                if episode is None or d > episode["supply_end"] + timedelta(days=GAP_DAYS):
                    episode = dict(patient_id=patient_id, market=prod["market"], molecule_name=molecule,
                                   benefit_type=prod["benefit_type"], generic=prod["brand_generic"] == "GENERIC",
                                   episode_start_date=d, supply_end=d, dispensed_fills=0)
                    by_patient[patient_id].append(episode)
                episode["dispensed_fills"] += 1
                episode["supply_end"] = max(episode["supply_end"], d + timedelta(days=days - 1))
        for patient_id in sorted(by_patient):
            lines, previous = defaultdict(int), {}
            for e in sorted(by_patient[patient_id], key=lambda e: (e["episode_start_date"], e["molecule_name"])):
                if e["generic"]:
                    e["line_of_therapy"], e["source_of_business"] = 0, "CONVENTIONAL"
                else:
                    market = e["market"]
                    if market not in previous:
                        lines[market], source = 1, "NEW_TO_MARKET"
                    elif e["molecule_name"] == previous[market]:
                        source = "RESTART"
                    else:
                        lines[market], source = lines[market] + 1, "SWITCH"
                    previous[market] = e["molecule_name"]
                    e["line_of_therapy"], e["source_of_business"] = lines[market], source
                end = min(e["supply_end"], self.end)
                e.update(episode_id=self.next_id("EP", "episode"), episode_end_date=end,
                         days_on_therapy=(end - e["episode_start_date"]).days + 1,
                         discontinued=(self.end - e["supply_end"]).days > GAP_DAYS)
                rows["therapy_episode"].append(e)

        activity = defaultdict(list)
        for c in rows["rx_claim"]:
            activity[c["patient_id"]].append(("rx", c["service_date"]))
        for c in rows["dx_claim"]:
            activity[c["patient_id"]].append(("dx", c["service_from_date"]))
        midpoint = self.start + (self.end - self.start) / 2
        for p in rows["patient"]:
            seen = activity[p["patient_id"]]
            p.update(first_activity_date=min(d for _, d in seen), last_activity_date=max(d for _, d in seen),
                     has_rx_activity=any(k == "rx" for k, _ in seen), has_dx_activity=any(k == "dx" for k, _ in seen),
                     stable_flag=any(d < midpoint for _, d in seen) and any(d >= midpoint for _, d in seen))

    def run(self):
        self.build_reference()
        for i in range(self.n):
            self.simulate(i)
        self.derive()
        validate(self.rows)
        return self.rows


def validate(rows):
    """Primary keys are unique and non-null; every foreign key resolves; reversals point at PAID claims;
    each fill has exactly one final primary transaction."""
    keys = {}
    for t in SCHEMA:
        seen = set()
        for row in rows[t["name"]]:
            key = tuple(row.get(c) for c in t["pk"])
            if None in key or key in seen:
                raise SystemExit(f"{t['name']}: bad or duplicate primary key {key}")
            seen.add(key)
        keys[t["name"]] = seen
    for t in SCHEMA:
        for column, parent, _ in t["fks"]:
            missing = {row[column] for row in rows[t["name"]] if row.get(column) is not None} - {k[0] for k in keys[parent]}
            if missing:
                raise SystemExit(f"{t['name']}.{column}: {len(missing)} values missing from {parent}")
    paid = {c["claim_id"] for c in rows["rx_claim"] if c["claim_status"] == "PAID"}
    if any(c["reversal_of_claim_id"] not in paid for c in rows["rx_claim"] if c["claim_status"] == "REVERSED"):
        raise SystemExit("rx_claim: a reversal does not point at a PAID claim")
    finals = defaultdict(int)
    for c in rows["rx_claim"]:
        if c["coverage_seq"] == 1 and c["final_claim_flag"]:
            finals[(c["rx_id"], c["fill_number"])] += 1
    if len(finals) != len(rows["rx_fill"]) or set(finals.values()) != {1}:
        raise SystemExit("rx_claim: every fill needs exactly one final primary transaction")


def format_value(value, sql_type):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    if sql_type.startswith("DECIMAL"):
        return f"{value:.{sql_type.rstrip(')').split(',')[1]}f}"
    return str(value)


def write_csvs(rows, out):
    for t in SCHEMA:
        with open(out / f"{t['name']}.csv", "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow([c for c, _, _ in t["columns"]])
            for row in rows[t["name"]]:
                writer.writerow([format_value(row.get(c), ty) for c, ty, _ in t["columns"]])


def sql_string(text):
    return "'" + text.replace("'", "''") + "'"


def create_tables_sql(catalog, schema, volume):
    fq = f"{catalog}.{schema}"
    out = [f"-- Synthetic eLAAD-style schema for Unity Catalog. Upload the CSVs to /Volumes/{catalog}/{schema}/{volume}/",
           "-- first, then run this script on a SQL warehouse. Keys are informational, as in all Unity Catalog tables.",
           f"CREATE SCHEMA IF NOT EXISTS {fq} COMMENT 'Synthetic eLAAD-style claims; all values are fictional.';",
           f"CREATE VOLUME IF NOT EXISTS {fq}.{volume};", ""]
    out += [f"DROP TABLE IF EXISTS {fq}.{t['name']};" for t in reversed(SCHEMA)]
    for t in SCHEMA:
        lines = [f"  {c} {ty}{' NOT NULL' if c in t['pk'] else ''} COMMENT {sql_string(note)}" for c, ty, note in t["columns"]]
        lines.append(f"  CONSTRAINT {t['name']}_pk PRIMARY KEY ({', '.join(t['pk'])})")
        lines += [f"  CONSTRAINT {t['name']}_{c}_fk FOREIGN KEY ({c}) REFERENCES {fq}.{parent} ({pc})"
                  for c, parent, pc in t["fks"]]
        columns = ", ".join(c for c, _, _ in t["columns"])
        out += ["", f"CREATE TABLE {fq}.{t['name']} (", ",\n".join(lines), f") COMMENT {sql_string(t['comment'])};",
                f"INSERT INTO {fq}.{t['name']} ({columns})",
                f"SELECT {columns} FROM read_files('/Volumes/{catalog}/{schema}/{volume}/{t['name']}.csv',",
                "  format => 'csv', header => true,",
                f"  schema => '{', '.join(f'{c} {ty}' for c, ty, _ in t['columns'])}');"]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="directory for the CSVs and create_tables.sql")
    ap.add_argument("--patients", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", type=date.fromisoformat, default=date(2023, 7, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 6, 30))
    ap.add_argument("--catalog", default="main")
    ap.add_argument("--schema", default="elaad_synthetic")
    ap.add_argument("--volume", default="landing")
    args = ap.parse_args()
    rows = Generator(args.seed, args.patients, args.start, args.end).run()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csvs(rows, out)
    (out / "create_tables.sql").write_text(create_tables_sql(args.catalog, args.schema, args.volume))
    for t in SCHEMA:
        print(f"{t['name']:<26} {len(rows[t['name']]):>9,} rows")
    print(f"wrote {len(SCHEMA)} CSVs and create_tables.sql to {out}")


if __name__ == "__main__":
    main()
