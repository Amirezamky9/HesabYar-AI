# Third-party notices

The HesabYar admin dashboard copies components from two MIT-licensed
Persian component registries (spec §5.2). Both follow the source-owned
approach: components are copied into this repo and may be changed
locally. Their full license texts are preserved alongside the copied
files, as required for substantial copied code.

## VibeFarsi — MIT

- Source: https://vibefarsi.ir (registry https://vibefarsi.ir/r)
- CLI: `vibefarsi@2.0.0` — `npx vibefarsi init` / `npx vibefarsi add`
- License: `components/ui/LICENSE-VibeFarsi.md` (Copyright (c) 2026 VibeFarsi)
- Used for Persian-specific patterns: Jalali calendar/date picker,
  Persian digits, Toman display, amount input, theme tokens (paper),
  Vazirmatn font setup.
- Copied files: `components/ui/calendar.tsx`, `components/ui/date-picker.tsx`,
  `components/ui/amount-input.tsx`, `components/ui/price.tsx`,
  `lib/jalali.ts` (extended in place with strict ISO helpers — see the
  "HesabYar additions" section in the file), `lib/utils.ts` (the `cn`
  helper was upgraded to clsx + tailwind-merge), `lib/float.tsx` (mount
  gate rewritten with `useSyncExternalStore` for the React hooks lint
  rule), `lib/number-to-words.ts`, `styles/tokens.css` (paper tokens,
  with the field-shape and display-font deviations documented in the
  file header), `app/fonts.ts`, `docs/agents-md-persian.md`,
  `docs/ui-craft-rules.md`.

## FarsiUI — MIT

- Source: https://farsiui.ir (registry https://farsiui.ir/r)
  and https://github.com/MiladJoodi/FarsiUI
- CLI: `farsiui@0.1.34` — `farsiui add` (style `base-nova`, Base UI primitives)
- License: `components/ui/LICENSE-FarsiUI.md` (MIT; the license file
  carries the upstream shadcn copyright — FarsiUI is a fork of shadcn/ui)
- Used for accessible primitives: button, input, select, dialog, drawer,
  sheet, table, badge, skeleton, command, tabs, card, label, textarea,
  checkbox, switch, tooltip, popover, dropdown-menu, scroll-area,
  separator, alert-dialog, pagination, input-group, sonner.
- Copied files: all of the above under `components/ui/`, plus
  `lib/digits.ts` and `hooks/use-persian-digits-input.ts`.
- Local changes to copied FarsiUI files (all small, all documented in
  the diff): `cn` imports rewritten from the `cn` package to the single
  shared `@/lib/utils` helper; three strict-TypeScript fixes in
  `input.tsx`, `textarea.tsx` (React 19 data-attribute indexing, ref
  types) and `sonner.tsx` (removed the unsupported `lang` prop).

## Other runtime dependencies

See `package.json` / `pnpm-lock.yaml`. Notable: Next.js (MIT), React
(MIT), Tailwind CSS (MIT), Base UI / Radix-style primitives (MIT),
`lucide-react` (ISC), `sonner` (MIT), `cmdk` (MIT), `next-themes` (MIT),
Vazirmatn font (OFL-1.1, via `next/font/google`).
