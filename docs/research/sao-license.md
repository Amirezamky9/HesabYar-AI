# Sao license manifest (research)

**Status:** License record for the HesabYar Phase 0 Ralph loop (story US-005).
**Date verified:** 2026-11-16.
**Companion document:** `sao-inventory.md` (workflow inventory, same directory).
**Authority:** design-eng spec §4.3 (Sao: what we clone and what we do not)
and §19 rule 13 ("No copied third-party source without preserving and
reviewing its license").

> **Rule (binding for this project): no Sao source code is copied into the
> HesabYar application — under `src/`, `apps/`, or anywhere else in this
> repository — without a separate, explicit GPL compliance decision recorded
> here first.** The inventory in `sao-inventory.md` is original prose
> describing observed behavior; describing a workflow does not copy code.

## 1. Identity of the reference copy

| Fact | Value | Verified from |
|---|---|---|
| Package name | `tryton-sao` | `package.json` → `"name": "tryton-sao"` |
| Package version | `6.7.0` | `package.json` → `"version": "6.7.0"` |
| Description | "Tryton webclient" | `package.json` → `"description"` |
| License (SPDX) | `GPL-3.0+` (GPL-3.0-or-later) | `package.json` → `"license": "GPL-3.0+"` |
| License text | GNU General Public License, Version 3, 29 June 2007 | `LICENSE` (full GPLv3 text, first line verified) |
| License grant | "...either version 3 of the License, or (at your option) any later version." | `COPYRIGHT` |
| GitHub mirror | `https://github.com/tryton/sao` | clone remote (`git remote -v`) |
| Mirror status | **Public archive, archived: true** | GitHub repo page ("Public archive", "Archived: Yes") and GitHub API (`"archived": true`) |
| Last push to mirror | `2022-12-10T11:16:50Z` | GitHub API (`pushed_at`) |
| Clone HEAD | `8eb1e9ca0f8dca13acea55f9775567a586d5d165` — "Prepend tfoot before thead" (2022-12-10) | `git log -1` in the clone; matches GitHub "Current commit" |
| Default branch | `develop` | GitHub API (`default_branch`) |
| Mirror created | 2013-05-22 | GitHub API (`created_at`) |
| Upstream repository | Mercurial, `http://hg.tryton.org/sao` (homepage `https://hg.tryton.org/sao`) | `package.json` → `repository.url`; GitHub API (`homepage`) |
| Local reference clone | `~/workspace/projects/hesabyar-reference/sao` (2.3 MB, `--depth 1`) | `ls` / `du` (see §4) |
| Latest CHANGELOG entry | Version 6.6.0 — 2022-10-31 | `CHANGELOG` (6.7.0 is the in-development version in `package.json`) |
| Copyright holders | Nicolas Évrard (2012–2022), Cédric Krier (2012–2022), Bertrand Chenal (2012–2014), B2CK SPRL (2012–2022), Jitbit (2019), Thomas Park (2013), Maxime Richez (2020–2021), SALUC SA (2020–2021) | `COPYRIGHT` |
| License tag in `bower.json` | `"GPL"` | `bower.json` → `"license": "GPL"` |
| GitHub license detection | GNU General Public License v3.0 (`gpl-3.0`, SPDX `GPL-3.0`) | GitHub API (`license`) |

Notes on the verified facts:

- The design-eng spec states the mirror was archived/read-only "since
  2022-12-30". The GitHub API does not expose the date the archive flag
  was set, so that exact date could not be independently re-verified; it
  is consistent with the last push (2022-12-10) and is recorded here as
  stated in the spec, not as a re-verified fact.
- GitHub's license detector reports plain `GPL-3.0`; the authoritative
  statement is the package's own `GPL-3.0+` plus the "or (at your option)
  any later version" grant in `COPYRIGHT`. This manifest records
  **GPL-3.0-or-later**.
- `package.json` declares the repository type as `hg` (Mercurial):
  the GitHub repository is a mirror, not the canonical home.

## 2. Archived mirror ≠ abandoned upstream

The GitHub mirror being archived does **not** mean Sao itself is
abandoned (spec §4.3 says the same). Evidence checked 2026-11-16:

- Tryton 8.2 was released 2026-10-05 and tryton.org's download page
  lists a Web Client (Sao) for the current series.
- The current upstream Sao (tryton/tryton monorepo, `sao/`) differs from
  the frozen 6.7.0 mirror: billboard.js charts instead of c3, ESLint
  config instead of JSHint-only tooling.

Consequence: the 6.7.0 mirror is a **frozen research snapshot**. Any
future decision to reuse Sao code must be evaluated against the upstream
version actually reused, not assumed identical to this snapshot, and its
license state re-verified at that time.

## 3. The no-copy rule

1. **No Sao source file** — JavaScript, Less/CSS, HTML, locale (`.po`),
   image, or build file — may be copied, pasted, or vendored into this
   repository (including `src/`, `apps/`, `templates/`, `examples/`, and
   any future frontend directory) without a separate GPL compliance
   decision.
2. **Describing behavior is allowed.** The inventory in
   `sao-inventory.md` describes workflows, screen names, RPC method
   names, and action types in original words. Interface facts (model
   names, method names, action types) are needed for interoperability
   with the Tryton server and are not Sao source code.
3. **Reimplementing behavior is allowed** when the new code is
   independently written (spec §4.3: "The default new React UI should
   use independently written components based on observed workflows, not
   copied Sao source"). Do not port legacy jQuery widgets into React;
   reimplement the valuable behavior cleanly.
4. **Do not embed the Sao client** inside the Next.js dashboard
   (spec §4.3).
5. **Dependency code is also not copied.** Sao's Bower dependencies
   (jQuery, Bootstrap, moment, c3, fullcalendar, papaparse, mousetrap,
   SortableJS, gettext.js, bootstrap-rtl-ondemand) are not vendored
   either; their individual licenses were not audited because no
   dependency code is used. If any reuse is ever proposed, audit each
   dependency's license as part of the compliance decision.

## 4. Separation evidence (verified 2026-11-16)

The reference clone lives **outside** this repository:

- Clone path: `~/workspace/projects/hesabyar-reference/sao`
- Repository path: `~/workspace/projects/hesabyar/`
- `git log --oneline -1` in the clone:
  `8eb1e9c Prepend tfoot before thead` (2022-12-10)
- `git remote -v` in the clone: `origin https://github.com/tryton/sao.git`
- Clone contents (top level): `CHANGELOG`, `COPYRIGHT`, `Gruntfile.js`,
  `LICENSE`, `README.md`, `bower.json`, `images`, `index.html`,
  `locale`, `package.json`, `src`, `tests`

No Sao source exists under this repo's code directories (run from
`~/workspace/projects/hesabyar/` on branch `ralph/phase-0`):

```text
$ grep -rn "Sao\.|tryton-sao" src/ ; echo "exit: $?"
exit: 1            # 1 = no matches
$ grep -rni "sao" src/ ; echo "exit: $?"
exit: 1            # 1 = no matches
$ find src -iname '*sao*' | wc -l
0
$ ls -d apps
ls: cannot access 'apps': No such file or directory
```

The only occurrences of the string `sao` anywhere in the repo are prose
mentions in `prd.json`, the sibling ADR draft
(`docs/adr/0002-tryton-as-accounting-engine.md`), and these two research
documents — no code. Re-run the greps above before any release that adds
frontend code.

## 5. If Sao source reuse is ever proposed

Per spec §4.3, the following must happen **before** any copy, in this order:

1. Create a separate fork for the reused code; do not mix copied GPL
   code into an MIT-only frontend and assume the license is unchanged.
2. Conduct a source-level license review: confirm the exact upstream
   version, its license header on every copied file, and the licenses
   of any dependencies the copied code pulls in.
3. Preserve all copyright notices (`COPYRIGHT`) and the `LICENSE` text.
4. Comply with GPL-3.0-or-later obligations for the derivative
   distribution — in particular, the GPL's copyleft: distributing a
   derivative work requires distributing it under GPL-3.0-or-later with
   corresponding source available. Get explicit project approval for
   that licensing consequence; it changes the license posture of
   whatever it is combined with.
5. Record the decision, the exact files copied, their upstream
   provenance (repo + commit), and the compliance steps taken, in this
   manifest. Update the repository's top-level license documentation
   accordingly.

Until all five steps are done, the rule in §3 stands and no copy happens.

## 6. Related licenses (context, not reuse)

- This repository's own license is recorded in its top-level `LICENSE`
  (see repo root). Nothing in this manifest changes it.
- VibeFarsi/FarsiUI (the dashboard's UI sources, spec §21) are MIT;
  preserve their notices for any copied code per spec §21. They are
  unrelated to Sao and unaffected by this manifest.
- Tryton server modules are GPL-3.0+ as well, but HesabYar talks to
  Tryton over its REST/JSON-RPC API (spec §4.2) rather than copying or
  linking server code; server licensing does not propagate into the
  HesabYar codebase through API use. The Tryton server's own license
  compliance (notices, source availability for the deployed server) is
  a deployment concern, tracked separately — it is out of scope for
  this manifest.

## 7. Verification log

| Date | Check | Result |
|---|---|---|
| 2026-11-16 | `package.json` name/version/license | `tryton-sao` / `6.7.0` / `GPL-3.0+` |
| 2026-11-16 | `LICENSE` first lines | GPLv3, 29 June 2007 |
| 2026-11-16 | `COPYRIGHT` grant + holders | "…or (at your option) any later version"; 8 holders listed in §1 |
| 2026-11-16 | GitHub repo page + API | Public archive, `archived: true`, last push 2022-12-10, HEAD `8eb1e9c` matches clone |
| 2026-11-16 | Clone location + remotes | Outside repo at `~/workspace/projects/hesabyar-reference/sao`; remote is `github.com/tryton/sao` |
| 2026-11-16 | Grep `src/` (+ `apps/` absent) | No Sao source under `src/`; `apps/` does not exist |
| 2026-11-16 | Ruff (`uv run ruff check .`) | All checks passed |
| 2026-11-16 | Pyright (`uv run pyright`) | 0 errors, 0 warnings, 0 informations |
