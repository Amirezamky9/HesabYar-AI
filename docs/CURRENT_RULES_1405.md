# HesabYar-AI — Current Rules Baseline 1405

**Snapshot ID:** HYA-RULES-1405-01  
**As of:** 1405/06/29 (2026-09-20)  
**Purpose:** current-year compliance/status overlay for architecture, Skills, standards metadata and future executable policy rules.  
**Normative relationship:** this file is evidence/status documentation. Executable behavior is governed by `docs/ARCHITECTURE.md` and only by source records promoted to `VERIFIED`.

## 1. Verification labels

- **VERIFIED_TEXT:** supported by the text already bundled in this repository or a consolidated legal text with amendment history.
- **CORROBORATED_CURRENT:** supported by current circular/reporting from tax/accounting authorities or multiple reliable current sources, but the official source snapshot still needs to be pinned before production activation.
- **DISCOVERY_ONLY:** useful signal for locating the official document; never executable.
- **BLOCKED_SOURCE_GAP:** the repository source is materially out of date or missing; production/current-period use is prohibited until the correct official revision is imported and verified.
- **TRANSITIONAL:** enacted/current rule whose operational effect depends on rollout, system readiness, later notices or effective dates.

## 2. Accounting standards — current applicability

| Standard | Current 1405 status | Repository status | Required system behavior |
|---|---|---|---|
| 15 — Investments | Revised in 1404; applicable to financial periods beginning 1405/01/01 and later. Current technical Q&A No. 140 dated 1405/05/26 expressly refers to the 1404 revision. | **BLOCKED_SOURCE_GAP** — bundled text is the old standard (its own paragraph 61 says effective 1380/01/01 and it still maps to IAS 25-era wording). | Do not use bundled Standard 15 text/rules to answer, validate, or post for periods beginning on/after 1405/01/01. Import and verify the official revised-1404 text first. |
| 43 — Revenue from Contracts with Customers | Effective for periods beginning 1404/01/01 and later; replaces Standards 3, 9 and 29. | **VERIFIED_TEXT** from bundled source paragraph 131/130. | Use Standard 43 for applicable periods; keep historical standards only for historical periods. |
| 44 — Leases | Approved 1404; effective for periods beginning 1405/01/01 and later; replaces Standard 21. | **VERIFIED_TEXT** from bundled source paragraphs 102–103. | Use Standard 44 for periods beginning on/after 1405/01/01. Standard 21 is historical, not active-current. |
| 21 — Leases | Superseded by Standard 44 for periods beginning 1405/01/01 and later. | Not in the active 35-standard metadata set. | Retain only as historical source if later imported; never label it current for 1405 periods. |

### Accounting references

- Repository: `standards/43-revenue-from-contracts/source/standard.md`, paragraphs 130–132.
- Repository: `standards/44-leases/source/standard.md`, paragraphs 102–104.
- Auditing Organization technical Q&A mirror/current index: https://persianacc.ir/technical-questions-and-answers/
- Current professional notice with direct Auditing Organization download reference for revised Standard 15: https://www.linkedin.com/posts/mh-abdolalipour_%D8%A7%D8%B3%D8%AA%D8%A7%D9%86%D8%AF%D8%A7%D8%B1%D8%AF-%D8%AD%D8%B3%D8%A7%D8%A8%D8%AF%D8%A7%D8%B1%DB%8C-15-%D8%AA%D8%AC%D8%AF%DB%8C%D8%AF-%D9%86%D8%B8%D8%B1-%D8%B4%D8%AF%D9%871404-activity-7499150751647047680-WVYA

## 3. 1405 annual tax parameters relevant to HesabYar

These are **current-year parameter candidates**, not timeless constants.

| Parameter | 1405/current figure | Scope note | Status |
|---|---:|---|---|
| General VAT rate | 10% | 9% statutory base + 1 percentage-point annual increase for the 1405 budget mechanism. Exempt/special-rate items remain separate. | CORROBORATED_CURRENT |
| Article 84 annual salary-tax exemption | 4,800,000,000 IRR | Annual 1405 threshold; payroll brackets must be modeled separately if payroll is added. | CORROBORATED_CURRENT |
| Article 101 annual exemption | 2,800,000,000 IRR | 1405 annual parameter. | CORROBORATED_CURRENT |
| Article 100 simplified/flat-tax sales ceiling | 720,000,000,000 IRR | **Performance year 1404 handled/filed in 1405**, not “1405 performance”. | CORROBORATED_CURRENT |
| Declared-income growth threshold for incentives under Note 7 Art.105 / Note Art.131 | 45% | Compares declared income of performance 1404 with performance 1403. | CORROBORATED_CURRENT |

Critical modeling rule: always persist **rule year, performance year, filing year and effective date separately**. Do not label a value merely “1405” when it legally applies to performance 1404 but is administered in 1405.

Current corroboration:
- Parliament/budget reporting: https://www.tasnimnews.ir/fa/news/1404/11/25/3516503/
- Article 100 current implementation: https://www.tasnimnews.ir/fa/news/1405/06/12/3687803/
- Article 100 public announcement: https://www.tasnimnews.ir/fa/news/1405/02/28/3593811/

Before production activation, pin the official annual tax/budget source to the legal-source registry and hash the snapshot.

## 4. Article 6 — current consolidated rule

The current consolidated Article 6 of the Point-of-Sale Terminals and Taxpayer System law is the basis for the sales-cap policy.

Core rule, subject to the active source version:

- the period cap is tied to five times the corresponding prior-period declared sales for which the related tax was paid/arranged;
- when the relevant prior-period sales are below the Article 101 exemption, the statutory Article 101 basis applies as specified by the law;
- new/no-history taxpayers use the statutory Article 101 basis as specified by the law;
- issuance above the cap is linked to payment/arrangement/guarantee conditions;
- invoices over the applicable cap may lose buyer VAT-credit effect under the law.

Implementation consequence:

- Article 6 is an effective-dated policy handler.
- **Do not encode a timeless “10× cash leverage” formula.**
- **Do not automatically create a corrective invoice.**
- The policy decision must include the source version and the freshness timestamp of any remote cap snapshot.

Current consolidated-law source:
https://www.irancodify.com/b/%D9%82%D8%A7%D9%86%D9%88%D9%86_%D9%BE%D8%A7%DB%8C%D8%A7%D9%86%D9%87_%D9%87%D8%A7%DB%8C_%D9%81%D8%B1%D9%88%D8%B4%DA%AF%D8%A7%D9%87%DB%8C_%D9%88_%D8%B3%D8%A7%D9%85%D8%A7%D9%86%D9%87_%D9%85%D8%A4%D8%AF%DB%8C%D8%A7%D9%86_%D9%85%D8%B5%D9%88%D8%A8_1398-07-21

## 5. Electronic invoice / VAT-credit changes

Tax circular 200/262/د dated 1405/01/11 states that from **1404/10/01**, buyer VAT input-credit entitlement depends on the electronic invoice being registered in the buyer's taxpayer workspace and confirmed by the buyer.

Implementation consequences:

- invoice provenance must distinguish electronic taxpayer-system evidence from legacy/paper evidence;
- VAT-credit eligibility is a versioned policy, not a simple `vat_rate > 0` check;
- buyer confirmation/status is evidence with a timestamp;
- a paper invoice cannot be treated as automatically credit-bearing for periods after this change.

Current mirror of the Tax Administration circular:
https://gzita.com/law/tax/txs5000135448/

## 6. Moadian technical instruction version

Public current discovery indicates an electronic-invoice instruction described as **version 7.9 — Tir 1405**, with changes including new fields and reference/corrective/return-invoice rules.

Status: **DISCOVERY_ONLY**.

The official authority copy has not yet been pinned in this repository. Therefore:

- do not implement v7.9 fields from secondary summaries;
- do not activate a production protocol profile;
- Stage F4 must import the official document, snapshot it, hash it and create contract fixtures from that exact version.

## 7. Electronic commercial books

Electronic commercial books are a current operational requirement, not a future-only feature.

Circular **200/1405/40 dated 1405/05/11** extended the deadline for uploading Q1 1405 financial information in the electronic commercial-books system to 1405/06/31 for taxpayers whose fiscal year started between 1405/01/01 and 1405/01/31.

Implementation consequences:

- HesabYar needs an **Electronic Books Export/Compliance** boundary separate from the internal ledger;
- regulatory export schemas are versioned profiles;
- export batches are immutable and reproducible from a ledger cutoff;
- deadlines belong to a `ComplianceCalendar`, not hard-coded application conditionals.

Current circular mirror:
https://persianacc.ir/%D8%AA%D9%85%D8%AF%DB%8C%D8%AF-%D9%85%D9%87%D9%84%D8%AA-%D8%A7%D8%B1%D8%B3%D8%A7%D9%84-%D8%AF%D9%81%D8%A7%D8%AA%D8%B1-%D8%A7%D9%84%DA%A9%D8%AA%D8%B1%D9%88%D9%86%DB%8C%DA%A9%DB%8C-%D8%A8%D9%87%D8%A7/

## 8. Article 100 current deadline

Circular **200/1405/45 dated 1405/06/15** extended filing/payment for business income returns and the Article 100 flat-tax form for **performance 1404** through **1405/08/30**.

This is a calendar event, not a permanent legal parameter.

Current circular mirror:
https://persianacc.ir/%D8%AA%D9%85%D8%AF%DB%8C%D8%AF-%D9%85%D9%87%D9%84%D8%AA-%D8%AA%D8%B3%D9%84%DB%8C%D9%85-%D8%A7%D8%B8%D9%87%D8%A7%D8%B1%D9%86%D8%A7%D9%85%D9%87-%D8%B5%D8%A7%D8%AD%D8%A8%D8%A7%D9%86-%D9%85%D8%B4%D8%A7/

## 9. Tax loss carryforward

Do not use the old research label “Article 140”.

The currently consolidated Direct Tax Law text states in **Article 148(12)** that verified losses of natural/legal persons, established through review of their books under the law, may be amortized against income of the current/future year(s).

Consolidated legal text:
https://nezamat.ir/post-28895/

Implementation consequence: use an effective-dated legal rule and evidence of verified loss; never infer eligibility merely from an accounting loss.

## 10. Capital-gains / anti-speculation tax law (new 1404 layer)

The **Law on Taxation of Speculation and Trading (مالیات بر سوداگری و سفته‌بازی)** was approved 1404/04/08 and promulgated by presidential letter No. 80519 dated 1404/05/25. It materially amended both the Direct Tax Law and the Taxpayer System law, including new definitions and non-commercial capital-gains provisions.

Important: parts of the implementation framework have rollout windows; for example, the newly added Article 16 bis framework provides up to twenty months for parts of the execution platform.

Status: **TRANSITIONAL**.

Implementation consequences:

- add a versioned `capital_gains_noncommercial` policy family;
- keep tax classification “commercial/non-commercial” separate from HesabYar authorization roles;
- do not activate tax calculations merely because the statute is enacted;
- activation requires the applicable executive-platform/effective-date conditions and current official rules to be verified;
- Moadian protocol profiles must be capable of later supporting new commercial/non-commercial workspace and asset-transfer obligations without changing the ledger domain.

Current consolidated Direct Tax Law:
https://nezamat.ir/post-28895/

Current law copy:
https://zamani.tax/articles/law/%D9%82%D8%A7%D9%86%D9%88%D9%86-%D9%85%D8%A7%D9%84%DB%8C%D8%A7%D8%AA-%D8%A8%D8%B1-%D8%B3%D9%88%D8%AF%D8%A7%DA%AF%D8%B1%DB%8C

## 11. Banking transactions

No amount/count threshold is permitted to classify a bank receipt automatically as taxable income or non-taxable.

The 1405 guidance located during review reinforces evidence-based review of bank transactions, but secondary reproductions differ in scope. Therefore this remains a source-verification-sensitive policy.

Implementation:

```text
bank transaction -> candidate classification -> evidence request
-> documented decision -> review if ambiguous
```

Never:

```text
amount/count threshold -> taxable income
```

## 12. Production activation rule

A fact in this file may be current and still be non-executable.

Promotion to production requires:

1. official/acceptable source document acquired;
2. immutable snapshot stored;
3. SHA-256 recorded;
4. effective dates and affected population/period modeled;
5. status set to `VERIFIED`;
6. golden/contract tests created;
7. reviewer activates the rule/profile.

This prevents a future web update, blog edit or stale Skill from silently changing financial/legal behavior.
