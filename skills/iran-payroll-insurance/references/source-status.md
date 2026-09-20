# Legal Source Status Registry: Labor, Insurance & Salary Tax

This registry maintains the provenance, canonical authority, verification status, and cryptographic integrity of all statutory authorities governing HesabYar-AI payroll calculations.

> **Production Execution Rule:**  
> Only sources with `verification_status: VERIFIED` may execute in production calculations.  
> Sources marked `CORROBORATED` or `DISCOVERY_ONLY` have verified citations and canonical URLs, but pending local physical snapshot acquisition.  
> Sources marked `PENDING_UPDATE`, `DISCOVERY_ONLY`, or `DISPUTED` fail closed with `RULE_UNVERIFIED`.
> Never fabricate a cryptographic hash or snapshot path. If a snapshot file is not physically verified on disk, `snapshot_path` and `actual SHA-256` MUST be `None`.

---

## 1. Statutory Authorities Registry

| Source ID | Authority | Doc Number | Title | Published At | Effective From | Effective To | Status | Canonical URL | Snapshot Path | Actual SHA-256 | Supersedes / Superseded By | Verified By | Verified At |
|---|---|---|---|:---:|:---:|:---:|:---:|---|---|---|---|---|---|
| `SRC-LABOR-1369-001` | مجمع تشخیص مصلحت نظام | قانون کار ۱۳۶۹ | قانون کار جمهوری اسلامی ایران | 1369/08/29 | 1369/12/14 | None | `CORROBORATED` | https://rc.majlis.ir/fa/law/show/91756 | None | None | None | None | None |
| `SRC-SSO-1354-001` | مجلس شورای ملی | قانون تامین اجتماعی ۱۳۵۴ | قانون تامین اجتماعی | 1354/04/03 | 1354/04/03 | None | `CORROBORATED` | https://rc.majlis.ir/fa/law/show/97151 | None | None | None | None | None |
| `SRC-SSO-UNEMP-1369` | مجلس شورای اسلامی | قانون بیمه بیکاری ۱۳۶۹ | قانون بیمه بیکاری | 1369/06/26 | 1369/07/01 | None | `CORROBORATED` | https://rc.majlis.ir/fa/law/show/91629 | None | None | None | None | None |
| `SRC-TAX-1366-001` | مجلس شورای اسلامی | ق.م.م اصلاحی ۱۳۹۴ | قانون مالیات‌های مستقیم با اصلاحات ۱۳۹۴ | 1394/04/31 | 1395/01/01 | None | `CORROBORATED` | https://intamedia.ir/laws-and-regulations | None | None | None | None | None |
| `SRC-COURT-1399-1957` | دیوان عدالت اداری | دادنامه ۱۹۵۷ | دادنامه ۱۹۵۷ هیئت عمومی دیوان عدالت اداری (معافیت مزایای رفاهی) | 1399/12/16 | 1399/12/16 | None | `CORROBORATED` | https://divan-edalat.ir/show-verdict/1957 | None | None | None | None | None |
| `SRC-COURT-1400-11257` | دیوان عدالت اداری | دادنامه ۱۱۴۰۰۰۹۹۷۰۹۰۵۸۱۱۲۵۷ | دادنامه ۱۱۲۵۷ هیئت عمومی دیوان عدالت اداری (معافیت بن و مسکن) | 1400/08/11 | 1400/08/11 | None | `CORROBORATED` | https://divan-edalat.ir/show-verdict/11257 | None | None | None | None | None |
| `SRC-INTA-CIRC-232` | سازمان امور مالیاتی | بخشنامه ۲۳۲/۲۴۰۱۳/د | بخشنامه ۲۳۲/۲۴۰۱۳/د (معافیت ۲/۷ بیمه سهم کارگر از مالیات حقوق) | 1384/09/20 | 1384/09/20 | None | `CORROBORATED` | https://intamedia.ir/circulars/232-24013 | None | None | None | None | None |
| `SRC-SLC-WAGE-1404` | شورای عالی کار | بخشنامه دستمزد ۱۴۰۴ | بخشنامه دستمزد و تعیین حداقل دستمزد کارگران سال ۱۴۰۴ | 1403/12/28 | 1404/01/01 | 1404/12/29 | `CORROBORATED` | https://mcls.gov.ir/wage1404 | None | None | superseded_by: SRC-SLC-WAGE-1405 | None | None |
| `SRC-CAB-HOUSING-1404` | هیئت وزیران | تصویب‌نامه حق مسکن ۱۴۰۴ | تصویب‌نامه هیئت وزیران درخصوص کمک‌هزینه مسکن کارگران سال ۱۴۰۴ | 1404/02/15 | 1404/01/01 | 1404/12/29 | `CORROBORATED` | https://dotic.ir/housing1404 | None | None | superseded_by: SRC-CAB-HOUSING-1405 | None | None |
| `SRC-BUDGET-1404-TAX` | مجلس شورای اسلامی | بند س تبصره ۶ بودجه ۱۴۰۴ | احکام مالیات بر درآمد حقوق قانون بودجه سال ۱۴۰۴ کل کشور | 1403/12/26 | 1404/01/01 | 1404/12/29 | `CORROBORATED` | https://rc.majlis.ir/fa/law/show/budget1404 | None | None | superseded_by: SRC-BUDGET-1405-TAX | None | None |
| `SRC-SLC-WAGE-1405` | شورای عالی کار | مصوبه دستمزد ۱۴۰۵ | بخشنامه دستمزد و حداقل مزد کارگران سال ۱۴۰۵ | Pending | 1405/01/01 | 1405/12/29 | `PENDING_UPDATE` | https://mcls.gov.ir/wage1405 | None | None | supersedes: SRC-SLC-WAGE-1404 | None | None |
| `SRC-CAB-HOUSING-1405` | هیئت وزیران | تصویب‌نامه حق مسکن ۱۴۰۵ | تصویب‌نامه هیئت وزیران موضوع کمک‌هزینه مسکن سال ۱۴۰۵ | Pending | 1405/01/01 | 1405/12/29 | `PENDING_UPDATE` | https://dotic.ir/housing1405 | None | None | supersedes: SRC-CAB-HOUSING-1404 | None | None |
| `SRC-BUDGET-1405-TAX` | مجلس شورای اسلامی | احکام مالیات حقوق بودجه ۱۴۰۵ | تبصره احکام مالیات بر درآمد حقوق قانون بودجه سال ۱۴۰۵ کل کشور | Pending | 1405/01/01 | 1405/12/29 | `PENDING_UPDATE` | https://rc.majlis.ir/fa/law/show/budget1405 | None | None | supersedes: SRC-BUDGET-1404-TAX | None | None |

---

## 2. Permitted Verification Status Lifecycle

```text
DISCOVERY_ONLY (Secondary reports / uncorroborated draft texts)
       |
       v
  CORROBORATED (Cross-verified in official/semi-official sources, canonical URL confirmed)
       |
       v
    VERIFIED (Official gazette document obtained locally on disk, SHA-256 pinned, human signed-off)
       |
       +---> DISPUTED (Conflicting rulings/circulars; requires legal triage)
       |
       +---> RETIRED (Superseded by subsequent year/statute)
       |
       +---> PENDING_UPDATE (Annual cycle awaiting publication; fails closed)
```

## 3. Mandatory Reviewer Sign-off Protocol

Before marking any source record `VERIFIED`:
1. The primary PDF/gazette must be acquired from an official authority (`rooznamehrasmi.ir`, `rrk.ir`, `mcls.gov.ir`, `tamin.ir`, `dotic.ir`, `rc.majlis.ir`, or `intamedia.ir`).
2. The file is saved to disk under `snapshots/<domain>/<filename>.pdf`.
3. The real SHA-256 hash is computed from the file on disk and recorded.
4. An authorized reviewer validates the text against official publication numbers and stamps `verified_by` and `verified_at`.
5. Never fabricate or placeholder a hash. Any source without a verified local snapshot file MUST remain `CORROBORATED` or `DISCOVERY_ONLY` with `snapshot_path: None` and `actual SHA-256: None`.
