# Sao workflow inventory (research)

**Status:** Research note for the HesabYar Phase 0 Ralph loop (story US-005).
**Source inspected:** `tryton-sao` 6.7.0, cloned to
`~/workspace/projects/hesabyar-reference/sao` (outside this repo),
GitHub mirror `https://github.com/tryton/sao`,
HEAD `8eb1e9ca0f8dca13acea55f9775567a586d5d165` (2022-12-10).
**Date inspected:** 2026-11-16.
**Authority:** design-eng spec §4.3, §4.4, §18
(`~/workspace/user/files/design-eng_0_zaup.md`).
**License record:** see `sao-license.md` in this directory.

> This inventory is written in the author's own words from inspection of the
> Sao source. It contains no Sao source code. No Sao file is copied into this
> repository (see `sao-license.md` for the no-copy rule).

## 1. What Sao is — and what it is not

Sao is Tryton's generic, metadata-driven web client (~27,200 lines of
JavaScript in `src/`, 21 top-level modules plus 5 view modules in
`src/view/`). It contains **no accounting logic and no accounting screens of
its own**: every screen it shows is described by the Tryton server as view
XML plus PYSON expressions, and rendered generically. The server is the
behavioral specification; Sao is only the renderer and the RPC caller
(JSON-RPC over HTTP, `src/rpc.js`, `src/session.js`).

Consequences for this inventory:

- The **client shell** (navigation, tabs, search, wizards, reports,
  permissions plumbing) is inventoried below from the Sao source itself,
  with file references.
- The **accounting flows** (party form, invoice list, move entry, …) are
  server-module views that Sao renders. They are named in §10 from the
  official Tryton 8.2 module documentation, because the Sao source cannot
  name them — it is model-generic. Each flow must be re-verified against
  the working Tryton 8.2 server (Phase 0 stories US-002/US-003) before the
  dashboard rebuilds it.

### Version caveat

The GitHub mirror is frozen at 6.7.0 (last push 2022-12-10). Upstream Sao
development continued in Mercurial (`https://hg.tryton.org/sao`) and
Tryton 8.2 (released 2026-10-05) still ships a Sao web client
(tryton.org download page lists a Web Client for the current series; the
current upstream Sao uses billboard.js charts and ESLint, where 6.7.0 uses
c3 and JSHint). Treat this inventory as the **6.7.0 baseline**: the shell
concepts are stable, but details must be re-checked against the 8.2-era
client/server pair actually deployed.

### Technology baseline (from `package.json`, `bower.json`, `Gruntfile.js`)

| Layer | 6.7.0 stack |
|---|---|
| Language | JavaScript (ES6, per 6.4.0 changelog), jQuery 3 |
| CSS framework | Bootstrap 3.3.7 + `bootstrap-rtl-ondemand` 3.3.4, Less |
| Build | Grunt (concat, uglify, less, jshint, po2json), Bower for deps |
| Charts | c3 0.7 (`src/view/graph.js`) |
| Calendar | fullcalendar 3.10.2 (`src/view/calendar.js`) |
| Dates | moment 2.10 |
| i18n | gettext.js 0.7, 25 locales in `locale/` including `fa.po` |
| CSV | papaparse 5.0 |
| Keyboard | mousetrap 1.6 |
| Drag/order | SortableJS 1.8.4 |
| Tests | QUnit (`tests/`, `grunt test`) |

None of this stack is proposed for the new dashboard (spec §4.4, §5.1:
Next.js 16 / React 19 / TypeScript / Tailwind v4). It is recorded here only
so the rebuild knows what it is replacing.

## 2. Navigation

Source: `src/sao.js`, `src/tab.js`, `index.html`.

- **Server-driven main menu.** The menu is not hard-coded in the client.
  On login, `Sao.get_preferences()` calls `model.res.user.get_preferences`
  and `Sao.menu()` decodes `preferences.pyson_menu` into an action whose
  tree view is rendered as the menu in the left off-canvas sidebar
  (`#menu`, `src/sao.js:786-831`). Activating a row fires the `tree_open`
  keyword (`Sao.main_menu_row_activate`, `src/sao.js:777-785`). The menu
  therefore shows exactly the `ir.ui.menu` entries the server exposes for
  the current user — in a full Tryton install this is the whole ERP tree,
  which spec §18 explicitly forbids exposing by default.
- **Tabs.** Every opened action becomes a tab in the `#tablist` pill bar
  (`src/tab.js`). Three tab kinds: `Sao.Tab.Form` (record screens),
  `Sao.Tab.Board` (dashboards), `Sao.Tab.Wizard` (wizards). Tabs are
  closeable (`Close Tab`, `Alt+W`), re-orderable, and addressable by URL
  hash (`#<database>/<path>`, `Sao.set_url`).
- **Global search.** A navbar search box (`#global-search`,
  `src/sao.js:960-1055`) calls `ir.model.execute('global_search', …)` and
  shows icon + text results across models. It searches record names, not
  menu entries.
- **Favorites.** A star column on the menu tree marks entries as favorites
  (`<model>.favorite` RPC); favorites appear in a navbar dropdown
  (`Sao.favorites_menu`, `src/sao.js:679-743`).
- **User menu.** Shows the server-provided `status_bar` (user/company),
  avatar images when present, a Preferences entry (opens
  `Sao.Window.Preferences`, `src/window.js:870`) and Logout
  (`common.db.logout`).
- **Startup actions.** `preferences.actions` (a list of action ids) are
  executed automatically after login (`src/sao.js:410-412`) — e.g. opening
  a default dashboard.
- **Keyboard shortcuts** (Mousetrap, `src/sao.js:1057-1130`): `Alt+N` New,
  `Ctrl+S` Save, `Ctrl+L` Switch view, `Ctrl+R` Reload/Undo,
  `Ctrl+Shift+D` Duplicate, `Ctrl+D` Delete, `Ctrl+Up/Down` Previous/Next
  record, `Ctrl+F` Search, `Alt+W` Close Tab, `Ctrl+Shift+T` Attachment,
  `Ctrl+Shift+O` Note, `Ctrl+E` Action, `Ctrl+Shift+R` Relate.

Dashboard takeaway: the new left nav (§18, 13 modules) replaces the
server-driven ERP menu tree entirely. Keep the good ideas — favorites,
global record search, keyboard shortcuts, deep-linkable tabs — and drop
the generic menu tree.

## 3. Views

Source: `src/view.js`, `src/view/*.js`, `src/screen.js`.

Sao renders five view types, all parsed from server XML by an
`XMLViewParser` per type (`Sao.View.parse`, `src/view.js:81`):

| View | Class | Notes |
|---|---|---|
| Tree (list) | `Sao.View.Tree` (`src/view/tree.js`, 3,125 lines) | Columns, optional columns, footer aggregates, expanders, editable mode (§5) |
| Form | `Sao.View.Form` (`src/view/form.js`, 5,351 lines) | Full record editing (§4) |
| Graph | `Sao.View.Graph` (`src/view/graph.js`, c3) | Bar/line/pie from server aggregates |
| Calendar | `Sao.View.Calendar` (`src/view/calendar.js`, fullcalendar) | Date-field based event display |
| Board | `Sao.View.Board` (`src/board.js`) | Dashboard: a container of several actions rendered together (§8) |

`Sao.View.ListForm` (`src/view/list_form.js`) renders a form inline inside
a list group. A `Sao.Screen` (`src/screen.js`, 2,270 lines) owns one
model's record group, its current view, search/filter state, and paging
(default fetch limit 1,000, display page 20 — `Sao.config.limit`,
`Sao.config.display_size`, `src/sao.js:337-338`). The `Switch` toolbar
button cycles the view modes an action declares (e.g. `['tree', 'form']`).

## 4. Forms

Source: `src/view/form.js`, `src/tab.js:6-230` (toolbar), `src/model.js`.

### Toolbar (every form tab)

Switch view, Previous / Next Record, Search, New, Save, Reload, Duplicate,
Delete, Attachment, Note, Action, Relate, Print, Email, Export, Import,
Close Tab (`src/tab.js:22-110`). Buttons are enabled/disabled from the
model access rights (§9) and the record state.

### Layout containers

Notebook (tabbed pages) + Page, Group (columns), Expander (collapsible
section), Separator, Label, Link. Visibility, read-only-ness and
required-ness of every widget are driven by PYSON state expressions
evaluated against the record (`Sao.View.Form.StateWidget`,
`Sao.PYSON.Decoder`, `src/pyson.js`).

### Field widgets (all in `src/view/form.js`)

Char, Password, Date, DateTime, Time, TimeDelta, Integer, Float, Numeric
(via `Sao.field.Numeric` in `src/model.js`), Selection, MultiSelection,
Boolean, Text, RichText (contenteditable editor with RTL-aware
justify buttons), URL, Email, CallTo (`tel:`), SIP, HTML, ProgressBar,
Dict (dynamic key/value schema), PYSON, Binary, Image, Document
(file upload/download), Many2One, One2One, Reference (model + record
picker), One2Many (embedded tree/form of child records), Many2Many
(checkable list).

Notable behaviors the dashboard must reproduce deliberately (not by
copying):

- **Many2One**: autocomplete search with create-on-the-fly; opens the
  related record's form. This is the pattern behind picking a party,
  account, journal or currency on every accounting form.
- **One2Many**: invoice lines and move lines are edited inline as an
  embedded editable tree inside the parent form, with per-line
  `on_change` recomputation (amounts, taxes) round-tripping to the server.
- **Monetary display**: 6.2.0 added `monetary` and `symbol` field
  attributes (CHANGELOG); currency symbol rendering is field metadata,
  not a dashboard concern — but the dashboard still needs its own
  IRR/Toman display rules (spec §8.3), which Sao has no notion of.
- **Translate**: translatable fields open a `TranslateDialog` editing
  `ir.translation` rows; a `Translate view` plugin opens the translation
  list for the whole model (`src/plugins.js`).
- **No Jalali support.** Date widgets are Gregorian/moment-based. Jalali
  date entry is a HesabYar-native requirement (spec §5.5, §8.4).

### Record lifecycle (client side)

`Sao.Model` (`src/model.js`, 2,826 lines) tracks new/modified/deleted
records in a group, validates required fields and domains client-side,
sends `on_change` calls, and saves via model `create`/`write`/`delete`
RPC. A posted move cannot be edited — but that rule lives on the server
(Tryton refuses the write); the client only reflects the resulting
read-only state. The dashboard must treat Tryton as the posting authority
the same way (spec §2, §19 rule 8).

## 5. List views

Source: `src/view/tree.js`, `src/screen.js`.

- **Columns** from server XML; `optional` columns can be toggled
  (`tree.js:111-112`, `view.optionals`).
- **Footer aggregates**: columns with a `sum` attribute render an
  aggregate row in `<tfoot>` (`sum_widgets`, `tree.js:123,197-202`) —
  the pattern behind "total debit / total credit" footers on move-line
  lists. (The HEAD commit itself, `8eb1e9c`, fixes tfoot ordering.)
- **Hierarchy**: expanders fold/unfold tree rows (`tree.js:208-220`); the
  chart of accounts renders as an expandable tree this way.
- **Editable tree**: `editable` views allow in-place cell editing without
  opening the form — used by the Open Journal wizard's quick-entry list.
- **Selection**: multi-select with a "number of selected records" display
  (6.4.0 changelog); selection drives bulk actions, export, and report
  execution on the selected ids.
- **Domain tabs**: an action can declare several domains shown as tabs
  above the list, each with a live record count badge, humanized
  (`count_tab_domain`, `screen.js:195-216,362-379`).
- **Sorting** by column header; **grouping** attribute supported
  (6.2.0 changelog).
- **Paging**: server-side search with limit/offset; count via
  `search_count`.

## 6. Search and filters

Source: `src/screen.js:1-260`, `src/common.js` (`ViewSearch`),
`src/window.js:728` (`Sao.Window.Search`).

- **Filter box** above every list: a search entry with a `<datalist>`
  autocomplete fed by the screen's domain parser
  (`domain_parser.completion`, `screen.js:228`), a filter button opening
  the advanced search form, a clear-search button, and prev/next buttons
  to step through matching records.
- **Domain language**: the search entry accepts `field: value` style
  clauses parsed into a Tryton domain; the parser offers field-name
  completion.
- **Saved searches**: `ir.ui.view_search` stores named searches
  (`ViewSearch.load_searches`, `create`, `delete` — `src/common.js:575`);
  any current filter can be bookmarked ("Bookmark this filter",
  `screen.js:245`).
- **Active toggle**: a button switches between active records and
  including inactive/archived ones (`search_active`).
- **Search modal**: `Sao.Window.Search` renders a full search form for
  the model with per-field criteria.
- **Global search**: see §2 (navbar, cross-model, `global_search`).

Dashboard takeaway: the filter-box + saved-search + domain-tab pattern is
the main thing to carry over into list screens (invoices, moves,
payments). The `field: value` domain syntax can be replaced by
purpose-built Persian filter chips; the underlying domain semantics stay
server-side.

## 7. Action flows

Source: `src/action.js` (237 lines — the whole dispatcher).

Four server action types, dispatched by `Sao.Action.exec_action`:

| Action type | Client behavior |
|---|---|
| `ir.action.act_window` | Opens a tab for a model with view modes, PYSON domain/context/order/search value, tab domains, limit, icon. The common case: menu entries and Relate links. |
| `ir.action.wizard` | Opens a `Sao.Wizard` (§8). |
| `ir.action.report` | Executes the report (§8) and downloads the file. |
| `ir.action.url` | Opens the URL in a new tab (`noreferrer,noopener`). |

**Keywords** bind actions to UI affordances without hard-coding:
`form_action` (Action menu on a form), `form_report` (Print menu),
`form_relate` (Relate menu), `tree_open` (menu row activation).
`Sao.Action.exec_keyword` fetches the keyword's actions from
`ir.action.keyword.get_keyword` and, when there are several, asks the
user to pick one (`Sao.common.selection`, "Select your action"). Action
tab titles get a suffix of the selected records' `rec_name`s (up to 5).

**Buttons on forms** (workflow transitions such as posting an invoice)
are server-declared `Button` widgets evaluated through PYSON states;
clicking one calls the model method the server names. Sao never implements
the transition itself — the dashboard's HesabYar REST endpoints must
expose the same transitions as explicit operations (spec §9.2), not as
generic method calls.

## 8. Wizards, reports, boards

### Wizards — source: `src/wizard.js` (388 lines), `src/tab.js:1667`

A wizard is a server-side state machine. The client calls
`wizard.<name>.create`, then renders each state's form view plus its
action buttons, calls `wizard.<name>.execute` with the state's values to
advance, and `wizard.<name>.delete` at the end. States, views and buttons
all come from the server; the client knows only `start_state` /
`end_state` / `screen_state`. Wizard tabs show the action name suffixed
with the active record names.

### Report actions — source: `src/action.js:177-199`

`Sao.Action.exec_report` calls `report.<name>.execute` with the selected
ids, data and context; the server returns `(type, data, print, name)` and
the client downloads `name.type` (`Sao.common.download_file`). Server-side
`direct_print` is passed through but direct printing is a `TODO` in the
client. Reports are therefore file downloads (PDF/ODT/etc. per server),
never in-browser previews.

### Boards — source: `src/board.js`, `src/tab.js:1600`

A board view parses a container of `<action>` elements and renders each
action's view in a panel, reloading them together (`Sao.View.Board`).
This is Sao's only dashboard mechanism: a screen of embedded lists/graphs
with no custom widgets. The HesabYar داشبورد (§18.1) needs purpose-built
KPI/alert widgets, which boards cannot express.

### Attachments, notes, email — source: `src/window.js:581-727,2137+`

`Sao.Window.Attachment` / `Sao.Window.Note` attach files/notes to the
current record (gated on model access, §9); `Sao.Window.Email` composes
an email for the record, with `pyson_email` defaults evaluated from the
record (`Sao.Action.evaluate`). The dashboard needs to decide per module
whether attachments/notes survive (likely: yes on invoices and moves).

### Import / export — source: `src/window.js:1018-1700`

`Sao.Window.Import` / `Sao.Window.Export` (subclasses of `Sao.Window.CSV`)
show a field-picker dialog ("All Fields" → selected columns, with
relation traversal) and parse/generate CSV with papaparse. Binary fields
in CSV import/export are supported (6.2.0 changelog). The dashboard's
import story (if any) belongs to the HesabYar backend, not to a CSV
dialog — but export of list views is a feature users will expect.

### Revisions — source: `src/window.js:949`

`Sao.Window.Revision` lets the user view a record's data as of a past
revision datetime (Tryton's `_history` mechanism). The dashboard's audit
requirements (§18.12) are broader (who did what, HesabYar audit log) and
are not covered by this dialog.

## 9. Permissions

Source: `src/common.js:478-513`, `src/window.js:309-394`,
`src/screen.js:2033`, `src/session.js`, `src/plugins.js`.

- **Enforcement is server-side.** Sao never decides access itself; it
  asks the server and disables UI accordingly.
- **Model access cache**: `Sao.common.ModelAccess` loads
  `model.ir.model.access.get_access` per model (`read`, `write`,
  `create`, `delete`) at login (`Sao.get_preferences`) and caches it.
  Form buttons are disabled from it: New needs `create`, Delete/Undelete
  need `delete`, Add/Remove (One2Many) need `read` (`window.js:309-351`).
  The Translate plugin only appears when the user has read+write on
  `ir.translation` (`src/plugins.js:19`).
- **Field-level states**: `readonly` / `invisible` / `required` come from
  server PYSON per view and per record state; buttons additionally carry
  server-side states.
- **Menus and actions** are filtered server-side per user (the menu tree
  itself is a server query), so an unauthorized module simply never
  appears.
- **Session**: login via `common.db.login` (login + password + language,
  with optional authentication services — 6.6.0 changelog — and device
  cookies), logout via `common.db.logout`; the session token is kept in
  `Sao.Session` and the context (company, language, …) is persisted and
  reloaded (`reload_context`, `session.js:90-108`). Credentials never
  reach any other origin.
- **Notifications**: `model.ir.model.get_notification` names are loaded
  at login (`ModelNotification`); `ModelHistory` loads
  `model.ir.model.list_history`.

Dashboard takeaway: the new dashboard must implement the same
server-truth pattern through HesabYar's own auth (§14 of the spec):
permissions come from the backend; the UI only hides/disables. The
Tryton access matrix still matters because the HesabYar adapter acts
with a Tryton user whose rights bound what it can do.

## 10. Accounting flows Sao renders (server-module views)

Sao itself defines none of these; the flows below are the Tryton 8.2
server modules' views as documented at
https://docs.tryton.org/8.2/ (Account, Account Invoice modules; catalog at
`/8.2/account.html`), rendered through the generic shell above. They are
the flows the dashboard must cover. Dotted model names are Tryton's
standard identifiers for these concepts and must be re-verified against
the live 8.2 server during US-002/US-003.

### Master data

- **Party form** (`party.party`, `party` module): name, code, addresses
  (One2Many), contact mechanisms, categories (Many2Many), identifiers;
  party list with search; invoice address selection per party (Invoice
  module usage guide). Note: §18 has no dedicated parties module —
  parties surface inside sales/purchase and accounting flows.
- **Product form** (`product.product`): accounting category links
  (`account_product` module); products appear as invoice-line picks.
- **Company / currency** (`company.company`, `currency.currency`):
  generic admin forms under settings; the company currency anchors all
  amounts.

### Accounting (§18.2 — حسابداری)

- **Chart of accounts** (`account.account`): hierarchical tree view with
  expanders; each account has a parent, an account type (balance-sheet /
  income-statement placement), optional second currency, reconcile flag.
  Accounts are created from templates; only non-closed accounts accept
  moves.
- **Account move entry** (`account.move` + `account.move.line`): form
  with journal (Many2One), period (Many2One, must be open), date, and
  One2Many lines (account, party, debit, credit, maturity date, second
  currency, tax split). Moves are editable until posted; only balanced
  moves post; posting is permanent. **Open Journal** wizard gives an
  editable-list quick-entry screen per journal + period.
- **Fiscal years & periods** (`account.fiscalyear`, `account.period`):
  lists/forms; **Create Periods** wizard (monthly/quarterly splits),
  **Renew Fiscal Year** wizard, **Balance Non-Deferral** wizard
  (year-end zeroing moves), period close / fiscal-year close flows.
- **Reconciliation**: **Reconcile Lines** wizard (match equal
  debit/credit lines, optional write-off move), **Unreconcile Lines**,
  **Reconcile Accounts** wizard (auto-match per account + party),
  **Group Lines** / **Reschedule Lines** wizards on move lines.
- **Cancel Moves** wizard: creates reversing moves; reconciles the
  reversal with the original where applicable.
- **Tax setup** (`account.tax`, `account.tax.code`, `account.tax.rule`):
  forms + **Test Tax** wizard (dry-run tax application).

### Sales & purchasing (§18.3 — فروش و خرید)

- **Invoice list / form** (`account.invoice`, `account_invoice` module):
  customer and supplier invoices, invoice lines (One2Many), payment terms
  (Many2One), invoice address, tax lines; lifecycle draft → validated →
  posted → paid, with **correct**, **cancel** and **credit note**
  operations (Invoice module usage guide). Payment of an invoice links
  to the payment flow (§18.4).

### Payments, bank & cash (§18.4–18.5 — دریافت و پرداخت، بانک و صندوق)

- **Payments** (`account.payment` module, "Manage payments"): payment
  forms linked to invoices/parties, payment methods, outstanding
  balances via aged balances.
- **Statements** (`account.statement` module, "Book bank statement, cash
  day book etc."): statement lines + reconciliation against move lines;
  statement import wizards exist for AEB43, CODA, MT940, OFX and SEPA
  formats (separate modules).
- **Aged balance** view/report: per-party overdue breakdown by term from
  move-line maturity dates.

### Fixed assets (§18.6 — دارایی ثابت)

- **Assets** (`account_asset` module, "Depreciate fixed assets"): asset
  forms, depreciation runs and disposal — rendered as generic
  forms/lists/wizards; the dashboard needs the depreciation schedule as
  a purpose-built view.

### Reports (§18.7 — گزارش‌ها)

Server report actions, downloaded as files (§8): **General Ledger**,
**Trial Balance**, **Statement** (balance sheet + income statement),
**General Journal**, **Aged Balance**. Cash-flow reporting has no
counterpart in the Account module docs reviewed — the dashboard must
build it from move lines (or confirm a module during US-002).

## 11. Internationalization and RTL

- 25 locales ship in `locale/`; `fa.po` is 85% translated (196 of 230
  entries, counted 2026-11-16) — the Sao shell strings exist in Persian,
  but server module strings come from the server's own translations.
- `Sao.i18n.set_direction` sets `<html dir>` and flips the off-canvas
  menu side (`row-offcanvas-right` for RTL, `src/sao.js:392-398`);
  Bootstrap-RTL is loaded on demand; the rich-text editor flips justify
  buttons (`src/common.js:4167-4219`).
- Numbers/dates use the user's locale from preferences
  (`Sao.i18n.locale`). No Jalali calendar anywhere.

## 12. Mapping to design-eng spec §18 dashboard modules

| §18 module | Sao counterpart (6.7.0 shell + 8.2 server views) | Rebuild notes |
|---|---|---|
| 1. داشبورد | Partial — Board views + Graph views only | KPI widgets, alerts, aging summaries are new; boards cannot express them |
| 2. حسابداری | Full — account tree, move form + lines, journals, fiscal years/periods, Open Journal / Create Periods / Renew Fiscal Year / Balance Non-Deferral / Cancel Moves wizards | Core rebuild; keep balanced-posting + open-period invariants server-side |
| 3. فروش و خرید | Full — invoice list/form/lines, payment terms, correct/cancel/credit-note operations, tax lines | Party picker + invoice address flow carry over |
| 4. دریافت و پرداخت | Full — payment forms, invoice payment flow, payment methods, aged balances | Outstanding-balance views are purpose-built |
| 5. بانک و صندوق | Full — statement booking, Reconcile Lines/Accounts wizards, statement import formats (MT940/OFX/SEPA/CODA/AEB43) | Reconciliation UI is the hardest rebuild; keep wizard semantics |
| 6. دارایی ثابت | Full (generic) — asset forms/lists, depreciation | Depreciation schedule needs a purpose-built view |
| 7. گزارش‌ها | Partial — General Ledger, Trial Balance, Statement (BS/IS), General Journal, Aged Balance report actions | Cash flow has no verified counterpart; in-browser preview replaces file download |
| 8. استانداردها و انطباق | **None** — no Sao counterpart | Entirely HesabYar-native (standards registry, validations, tax policy, e-books, Moadian) |
| 9. یکپارچه‌سازی‌ها | **None** — no Sao counterpart | Entirely HesabYar-native (connectors, mapping, sync logs) |
| 10. هوش مصنوعی و MCP | **None** — no Sao counterpart | Entirely HesabYar-native |
| 11. مدیریت API | **None** — no Sao counterpart | Sao uses JSON-RPC sessions; no API-key/webhook UI exists |
| 12. کاربران و دسترسی‌ها | Partial — generic `res.user` admin screens + preferences exist in Tryton | Service identities, HesabYar roles, audit log are HesabYar-native |
| 13. تنظیمات | Partial — company/currency are generic Tryton model screens | Jalali settings, fiscal preferences, engine selection, system status are HesabYar-native |

## 13. What the rebuild keeps, drops, and invents

**Keep (behavior, reimplemented in React):** tabbed multi-record work
with deep links; favorites; global record search; saved searches +
domain tabs with counts; footer aggregates on money lists; inline
editable lines with server `on_change`; wizard state machines with
per-state forms; report-as-download (plus in-browser preview);
attachments/notes on records; CSV export of lists; keyboard shortcuts;
RTL layout; permission-driven disabling from server truth.

**Drop:** the server-driven ERP menu tree (replaced by the §18 nav); the
generic metadata-driven form renderer (replaced by purpose-built Persian
flows); jQuery/Bootstrap 3/Grunt/Bower; the `field: value` domain syntax
(replaced by filter chips); file-only report delivery.

**Invent (no Sao precedent):** Jalali date entry and display; IRR/Toman
display rules with exact arithmetic; Persian design system
(VibeFarsi/FarsiUI); dashboard KPIs/alerts; cash-flow reporting;
standards/compliance, integrations, MCP/AI, and API-management modules;
Hesa

...[truncated 13571 chars]