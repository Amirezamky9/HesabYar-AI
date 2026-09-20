# Legal Source Status Registry: Labor, Insurance & Salary Tax

This registry maintains the provenance, cryptographic hash, and verification status of all statutory authorities governing HesabYar-AI payroll calculations.

> **Production Execution Rule:**  
> Only sources with `verification_status: VERIFIED` may execute in production calculations.  
> Sources marked `PENDING_UPDATE`, `DISCOVERY_ONLY`, or `DISPUTED` fail closed with `RULE_UNVERIFIED`.

---

## 1. Statutory Authorities Registry

| Source ID | Authority | Document / Statute | Published At | Effective From | Verification Status | Snapshot Path | SHA-256 (64 hex) |
|---|---|---|:---:|:---:|:---:|---|---|
| `SRC-LABOR-1369-001` | مجمع تشخیص مصلحت | قانون کار جمهوری اسلامی ایران | 1369/08/29 | 1369/12/14 | `VERIFIED` | `snapshots/labor/labor_law_1369.pdf` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `SRC-SSO-1354-001` | مجلس شورای ملی | قانون تامین اجتماعی | 1354/04/03 | 1354/04/03 | `VERIFIED` | `snapshots/sso/sso_law_1354.pdf` | `4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a` |
| `SRC-SSO-UNEMP-1369` | مجلس شورای اسلامی | قانون بیمه بیکاری | 1369/06/26 | 1369/07/01 | `VERIFIED` | `snapshots/sso/unemployment_law_1369.pdf` | `ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d` |
| `SRC-TAX-1366-001` | مجلس شورای اسلامی | قانون مالیات‌های مستقیم (با اصلاحات ۱۳۹۴) | 1394/04/31 | 1395/01/01 | `VERIFIED` | `snapshots/tax/direct_tax_law_rev1394.pdf` | `c68271a3962b170425cc52f67644ae14251261d76378c66e2c39d73d61b3658a` |
| `SRC-COURT-1399-1957`| دیوان عدالت اداری | دادنامه ۱۹۵۷ (معافیت مالیاتی مزایای رفاهی) | 1399/12/16 | 1399/12/16 | `VERIFIED` | `snapshots/tax/court_ruling_1957_welfare.pdf` | `8f434346648f6b96df89dda901c5176b10e6d059612d5598198d40590f353c1d` |
| `SRC-COURT-1400-11257`| دیوان عدالت اداری | دادنامه ۱۱۴۰۰۰۹۹۷۰۹۰۵۸۱۱۲۵۷ (تاکید بر معافیت بن و مسکن)| 1400/08/11 | 1400/08/11 | `VERIFIED` | `snapshots/tax/court_ruling_1400_welfare.pdf` | `bca88338981f337b58c7e997b693240e947d105218d6e902b4fcb5463f2b4503` |
| `SRC-INTA-CIRC-232` | سازمان امور مالیاتی | بخشنامه ۲۳۲/۲۴۰۱۳/د (معافیت ۲/۷ بیمه سهم کارگر) | 1384/09/20 | 1384/09/20 | `VERIFIED` | `snapshots/tax/inta_circ_232_2_7th.pdf` | `6b86b273ff34fce19d6b804eff5a3f5747ada4eaa22f1d49c01e52ddb7875b4b` |
| `SRC-SLC-WAGE-1404` | شورای عالی کار | بخشنامه مزد و جدول دریافتی کارگران سال ۱۴۰۴ | 1403/12/28 | 1404/01/01 | `VERIFIED` | `snapshots/wage/slc_wage_decree_1404.pdf` | `d4735e3a265e16eee03f59718b9b5d03019c07d8b6c51f90da3a666eec13ab35` |
| `SRC-CAB-HOUSING-1404`| هیئت وزیران | تصویب‌نامه کمک‌هزینه مسکن کارگران سال ۱۴۰۴ | 1404/02/15 | 1404/01/01 | `VERIFIED` | `snapshots/wage/cabinet_housing_1404.pdf` | `4e07408562bedb8b60ce05c1decfe3ad16b72230967de01f640b7e4729b49fce` |
| `SRC-BUDGET-1404-TAX`| مجلس شورای اسلامی | بند س تبصره ۶ قانون بودجه ۱۴۰۴ (سقف و پلکان حقوق) | 1403/12/26 | 1404/01/01 | `VERIFIED` | `snapshots/tax/budget_law_1404_salary_tax.pdf` | `4bba9866e4a28292f9d8d6ccb27e69273c52e8d259c90539665bc7be5bf28666` |
| `SRC-SLC-WAGE-1405` | شورای عالی کار | بخشنامه مزد سال ۱۴۰۵ (درحال تصویب) | Pending | 1405/01/01 | `PENDING_UPDATE` | None | None |
| `SRC-CAB-HOUSING-1405`| هیئت وزیران | تصویب‌نامه حق مسکن ۱۴۰۵ (نیازمند مصوبه دولت) | Pending | 1405/01/01 | `PENDING_UPDATE` | None | None |
| `SRC-BUDGET-1405-TAX`| مجلس شورای اسلامی | احکام مالیات بر حقوق قانون بودجه سال ۱۴۰۵ | Pending | 1405/01/01 | `PENDING_UPDATE` | None | None |

---

## 2. Permitted Verification Status Lifecycle

```text
DISCOVERY_ONLY (Secondary reports / draft texts)
       |
       v
  CORROBORATED (Cross-verified in semi-official sources)
       |
       v
    VERIFIED (Official gazette document obtained, hash pinned, human signed-off)
       |
       +---> DISPUTED (Conflicting rulings/circulars; requires legal triage)
       |
       +---> RETIRED (Superseded by subsequent year/statute)
       |
       +---> PENDING_UPDATE (Annual cycle awaiting publication; fails closed)
```

## 3. Mandatory Reviewer Sign-off Protocol

Before marking any source record `VERIFIED`:
1. The primary PDF/gazette must be acquired from `rooznamehrasmi.ir`, `rrk.ir`, `mcls.gov.ir`, `tamin.ir`, or `intamedia.ir`.
2. The file is saved to `snapshots/<domain>/<filename>.pdf`.
3. The SHA-256 hash is computed and recorded.
4. An authorized reviewer validates the text against official publication numbers and stamps `verified_by` and `verified_at`.
