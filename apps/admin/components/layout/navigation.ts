/**
 * Navigation modules (US-302) — the 13 §18 modules, in spec order.
 *
 * Module boundaries stay intact: no duplicate CRM module, no Tryton ERP
 * navigation. Modules whose pages land in later phases (or in a sibling
 * story not yet merged) are marked `comingSoon` and render disabled with
 * a coming-soon state. Live links use the exact route paths agreed for
 * Phase 3 — sibling stories build the pages behind them.
 *
 * - داشبورد `/` — US-302 (this story)
 * - حسابداری: سر فصل حساب‌ها `/accounting/accounts` (US-302),
 *   روزنامه‌ها `/accounting/journals` (US-303),
 *   سال‌های مالی `/accounting/fiscal-years` (US-302)
 * - فروش و خرید `/invoices` (US-304)
 * - دریافت و پرداخت `/payments` (US-304)
 * - بانک و صندوق `/banking/statements` (US-304),
 *   صندوق‌ها `/banking/cash-boxes` (US-516)
 * - مرکز تسویه `/settlements` (US-525): گزارش تسویه، برنامهٔ قسط‌ها،
 *   ایمپورت فایل، قراردادها
 * - مرکز چک `/cheques` (US-535): مرکز چک، ثبت چک، در انتظار برگ،
 *   دسته‌چک‌ها، گزارش‌های چک
 * - مرکز اقساط `/installments` (US-545): مرکز اقساط، برنامهٔ جدید،
 *   گزارش‌های اقساط
 * - گزارش‌ها `/reports` (US-305)
 */

import {
  BookMarked,
  BookOpen,
  Bot,
  Building2,
  Calculator,
  CalendarClock,
  CalendarDays,
  ChartColumn,
  CircleDollarSign,
  ClipboardCheck,
  ClipboardList,
  Inbox,
  KeyRound,
  Landmark,
  LayoutDashboard,
  ListTree,
  NotebookPen,
  Package,
  Percent,
  Plug,
  Receipt,
  Send,
  Settings,
  ShieldCheck,
  TicketCheck,
  Users,
  Wallet,
  type LucideIcon,
} from "lucide-react";

export interface NavChild {
  id: string;
  label: string;
  href: string;
  icon: LucideIcon;
}

export interface NavModule {
  id: string;
  label: string;
  icon: LucideIcon;
  /** Live link. Absent for groups (children) and coming-soon modules. */
  href?: string;
  children?: NavChild[];
  /** Visible but disabled, with a coming-soon badge. */
  comingSoon?: boolean;
}

export const NAV_MODULES: NavModule[] = [
  {
    id: "dashboard",
    label: "داشبورد",
    icon: LayoutDashboard,
    children: [
      { id: "dashboard-home", label: "داشبورد", href: "/", icon: LayoutDashboard },
      { id: "review-queue", label: "صف بازبینی", href: "/review-queue", icon: Inbox },
    ],
  },
  {
    id: "accounting",
    label: "حسابداری",
    icon: BookOpen,
    children: [
      { id: "accounts", label: "سر فصل حساب‌ها", href: "/accounting/accounts", icon: ListTree },
      { id: "journals", label: "روزنامه‌ها", href: "/accounting/journals", icon: NotebookPen },
      { id: "fiscal-years", label: "سال‌های مالی", href: "/accounting/fiscal-years", icon: CalendarDays },
    ],
  },
  {
    id: "sales",
    label: "فروش و خرید",
    icon: Receipt,
    children: [
      { id: "invoices", label: "صورت‌حساب‌ها", href: "/invoices", icon: Receipt },
      { id: "manual-entries", label: "فاکتور جدید (دستی)", href: "/invoices/manual", icon: NotebookPen },
    ],
  },
  { id: "payments", label: "دریافت و پرداخت", icon: Wallet, href: "/payments" },
  {
    id: "banking",
    label: "بانک و صندوق",
    icon: Landmark,
    children: [
      { id: "statements", label: "صورت‌حساب‌ها", href: "/banking/statements", icon: Landmark },
      { id: "cash-boxes", label: "صندوق‌ها", href: "/banking/cash-boxes", icon: Wallet },
    ],
  },
  {
    id: "cheques",
    label: "مرکز چک",
    icon: TicketCheck,
    children: [
      { id: "cheques-center", label: "مرکز چک", href: "/cheques", icon: TicketCheck },
      { id: "cheques-new", label: "ثبت چک", href: "/cheques/new", icon: NotebookPen },
      { id: "cheques-intake", label: "در انتظار برگ", href: "/cheques/intake", icon: Inbox },
      { id: "cheques-checkbooks", label: "دسته‌چک‌ها", href: "/cheques/checkbooks", icon: BookMarked },
      { id: "cheques-reports", label: "گزارش‌های چک", href: "/cheques/reports", icon: ChartColumn },
    ],
  },
  {
    id: "installments",
    label: "مرکز اقساط",
    icon: CalendarClock,
    children: [
      { id: "installments-center", label: "مرکز اقساط", href: "/installments", icon: CalendarClock },
      { id: "installments-new", label: "برنامهٔ جدید", href: "/installments/new", icon: NotebookPen },
      { id: "installments-reports", label: "گزارش‌های اقساط", href: "/installments/reports", icon: ChartColumn },
    ],
  },
  {
    id: "settlements",
    label: "مرکز تسویه",
    icon: CircleDollarSign,
    children: [
      { id: "settlement-report", label: "گزارش تسویه", href: "/settlements", icon: ChartColumn },
      { id: "settlement-schedule", label: "برنامهٔ قسط‌ها", href: "/settlements/schedule", icon: CalendarDays },
      { id: "settlement-import", label: "ایمپورت فایل", href: "/settlements/import", icon: Inbox },
      { id: "settlement-contracts", label: "قراردادها", href: "/settlements/contracts", icon: BookMarked },
    ],
  },
  {
    id: "packaging",
    label: "انبار و بسته‌بندی",
    icon: Package,
    children: [
      { id: "packaging-materials", label: "مواد بسته‌بندی", href: "/packaging", icon: Package },
      { id: "packaging-calculator", label: "محاسبه‌گر قیمت", href: "/packaging/calculator", icon: Calculator },
      { id: "packaging-counts", label: "شمارش مواد", href: "/packaging/counts", icon: ClipboardList },
      { id: "packaging-order-cost", label: "هزینهٔ سفارش", href: "/packaging/order-cost", icon: Receipt },
    ],
  },
  { id: "assets", label: "دارایی ثابت", icon: Building2, comingSoon: true },
  { id: "reports", label: "گزارش‌ها", icon: ChartColumn, href: "/reports" },
  {
    id: "compliance",
    label: "استانداردها و انطباق",
    icon: ShieldCheck,
    children: [
      { id: "compliance-overview", label: "مرکز انطباق", href: "/compliance", icon: ShieldCheck },
      { id: "compliance-standards", label: "استانداردها", href: "/compliance/standards", icon: BookMarked },
      { id: "compliance-tax", label: "سیاست مالیاتی", href: "/compliance/tax-policy", icon: Percent },
      { id: "compliance-validations", label: "اعتبارسنجی‌ها", href: "/compliance/validations", icon: ClipboardCheck },
      { id: "compliance-moadian", label: "مودیان", href: "/compliance/moadian", icon: Send },
    ],
  },
  { id: "integrations", label: "یکپارچه‌سازی‌ها", icon: Plug, comingSoon: true },
  { id: "ai", label: "هوش مصنوعی و MCP", icon: Bot, comingSoon: true },
  { id: "api", label: "مدیریت API", icon: KeyRound, comingSoon: true },
  { id: "users", label: "کاربران و دسترسی‌ها", icon: Users, comingSoon: true },
  { id: "settings", label: "تنظیمات", icon: Settings, comingSoon: true },
];

/** Persian labels for breadcrumb segments (route segment -> label). */
export const ROUTE_LABELS: Record<string, string> = {
  "review-queue": "صف بازبینی",
  accounting: "حسابداری",
  accounts: "سر فصل حساب‌ها",
  journals: "روزنامه‌ها",
  "fiscal-years": "سال‌های مالی",
  invoices: "صورت‌حساب‌ها",
  manual: "فاکتور جدید (دستی)",
  payments: "پرداخت‌ها",
  banking: "بانک و صندوق",
  statements: "صورت‌حساب‌های بانکی",
  "cash-boxes": "صندوق‌ها",
  reports: "گزارش‌ها",
  cheques: "مرکز چک",
  // Shared segment: /cheques/new and /installments/new both end in
  // "new", so the label stays generic (US-545).
  new: "ثبت جدید",
  intake: "در انتظار برگ",
  checkbooks: "دسته‌چک‌ها",
  installments: "مرکز اقساط",
  settlements: "مرکز تسویه",
  schedule: "برنامهٔ قسط‌ها",
  import: "ایمپورت فایل",
  imports: "ایمپورت‌های تسویه",
  contracts: "قراردادها",
  packaging: "انبار و بسته‌بندی",
  calculator: "محاسبه‌گر قیمت",
  counts: "شمارش مواد",
  "order-cost": "هزینهٔ سفارش",
  compliance: "استانداردها و انطباق",
  standards: "استانداردها",
  "tax-policy": "سیاست مالیاتی",
  validations: "اعتبارسنجی‌ها",
  moadian: "مودیان",
};

/** True when the current path belongs to this module (for active state). */
export function isModuleActive(module: NavModule, pathname: string): boolean {
  if (module.href === "/") return pathname === "/";
  if (module.href) return pathname === module.href || pathname.startsWith(`${module.href}/`);
  if (module.children) {
    return module.children.some(
      (child) => pathname === child.href || pathname.startsWith(`${child.href}/`),
    );
  }
  return false;
}
