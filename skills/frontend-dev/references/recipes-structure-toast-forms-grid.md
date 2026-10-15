# Frontend: folder structure, toast, forms, formatting, query keys, grid

Moved out of SKILL.md to keep the fixed context small. Read it when the task touches one of these subjects.

## 5. Folder structure

```
src/
├── components/
│   ├── ui/              # shadcn/ui primitives — do not edit by hand
│   ├── data-grid/       # wrapper around the data grid (every table with columns uses it)
│   ├── charts/          # ChartCard, KPI cards, chart tokens
│   ├── toast.tsx        # showSuccess, showError, showWarning, showInfo, showLoading
│   └── ...              # Shared (confirm-dialog, drawer-shell, section-card, detail-item...)
├── layout/              # Page structure (page-layout, auth-layout, page-header, back-button...)
├── templates/           # Ready-made combinations of page patterns
├── builders/            # Builders (e.g. column-builder for grid columns)
├── mediator/            # Communication between pieces (e.g. grid-mediator)
├── pages/               # One folder per feature
│   └── orders/
│       ├── index.tsx    # the page
│       ├── components/  # components used only by this page
│       ├── hooks/       # hooks of this page
│       │   ├── queries/
│       │   └── mutations/
│       ├── types/       # types of this page
│       ├── validations/ # zod schemas
│       ├── constants/   # constants of this page
│       ├── utils/       # pure functions of this page
│       └── mocks/       # demo data (always with a notice on screen)
├── hooks/
│   ├── queries/         # queries (GET) used by more than one page
│   └── mutations/       # mutations (POST/PUT/DELETE) used by more than one page
├── services/            # HTTP calls organised by domain (services/billing/...)
├── contexts/            # Global React contexts
├── lib/                 # library configuration (axios, query-client, jwt, zod-resolver...)
├── utils/               # pure functions (formatters, converters, request-messages-handler)
├── types/               # global TypeScript types
├── i18n/                # i18next configuration
├── routes/              # route definitions (React Router) and the protected route (Private)
└── styles/              # global CSS (Tailwind, grid styles)
```

**Location rule:** code of one feature stays inside `pages/{feature}/`. What is used by 2 or more
features moves up to the matching global folder.

**Plural folder names** when they hold several items of the same type: `hooks/`, `validations/`, `types/`, `components/`, `mocks/`, `services/`. The fixed folders above keep their names (`layout/`, `lib/`, `i18n/`, `mediator/`).

---

## 6. Toast system

Every project uses one toast system built on top of shadcn/ui. Always import from `@/components/toast`.

```typescript
import { showSuccess, showError, showWarning, showInfo, showLoading } from '@/components/toast'
```

| Function | When to use | Duration |
|----------|-------------|----------|
| `showSuccess` | `onSuccess` of mutations | 4.5s |
| `showError` | Manual errors (API errors are already handled) | 6s |
| `showWarning` | Important warnings | 6s |
| `showInfo` | Informative messages | 5s |
| `showLoading` | Long operations; returns an id to close it manually | no limit |

**Rule:** API errors already trigger `showError` through the `ErrorHandler` (`utils/request-messages-handler`),
called in the `catch` of every service function. Warnings that come in the response (`data.warnings`) go through
the `WarningHandler`. For success, call `showSuccess` by hand in the mutation's `onSuccess` (or in the hook of the
page that called the mutation). Never add an `onError` just to show a toast: the handler already does it.

**Server message with an id:** a generic server-error message that carries a correlation id never becomes a toast.
The id (from the `X-Correlation-ID` header) goes to the error page so support can find the log.

**No repeats:** the same message never shows twice at the same time (keep the existing toast in place and
restart its timer). Do not create another `Toaster` and do not call `useToast` directly. The `Toaster` uses
`<ToastProvider label="Notifications">`.

**A front-end error is also an error toast:** a business rule or validation before sending calls
`showError` directly. A field error stays on the field (`FormMessage`); when saving with errors, one single error
toast: a required field missing → "Fill in the required fields"; only format errors → the field's message.
Use `form.handleSubmit(save, notifyFieldsWithErrors)`. Never `showWarning` for validation.

---

## 7. Forms

- **react-hook-form + zod.** New project: zod 4. Existing project: the version it already uses.
- **Resolver:** always import from `@/lib/zod-resolver`, never straight from `@hookform/resolvers/zod`.
  It keeps the schema, and the label discovers by itself whether a field is required (asterisk and bold).
- **Required comes from the schema.** Do not repeat the rule in the JSX.
- **Error messages say how to fix it:** "Enter the 8 digits. E.g. 8414.30.11", not "Invalid field".
- **The schema message is a translation key** (`'certificate_file_required'`); `FormMessage` translates it with i18next.
- **Schema in the `validations/` folder** of the page; the form type comes from the schema
  (`z.infer<typeof schema>`).
- **Currency, decimal, tax id and similar masked values:** use ready-made inputs (`CurrencyInput`, `DecimalInput`,
  `TaxIdInput`). The value stores only the raw digits; the screen shows it formatted.
- **Unsaved changes:** edit screens warn when leaving with a pending change. When switching items,
  save automatically if the change is valid; if it has an error, open the warning.

---

## 8. Formatting numbers and dates

- One formatter per type, in `utils/` (currency, percentage, date, date and time), locale-aware (`Intl` or `date-fns` with the user's locale).
- Currency and numbers always follow the active locale: never `toFixed` or a hard-coded en-US format on screen.
- Percentage: 2 decimals in the system, 1 decimal in charts.
- Dates with `date-fns` and the active locale.

---

## 9. Query keys (TanStack Query)

- The same key used in `useQuery` is the one used in `invalidateQueries`. A typo here makes the
  screen show stale data with no error at all.
- Keys in kebab-case and hierarchical: `['order-items', orderId]`.
- **New project:** centralize the keys in `lib/query-keys.ts` (example in `references/patterns.md`).
- **Existing project without that file:** follow the keys that already exist; do not create the file for a single screen.

---

## 10. Data grid, permissions and demo data

- **Every table with columns uses the data grid** through one shared list component
  (`components/data-grid/`), never an HTML table, the shadcn table or a bare grid element dropped in the page.
  Columns without `flex` sizing surprises; never auto-height layout for the grid.
- **Full preset on every listing:** own header and filter, selection ("select all" applies to the page only),
  toolbar, row counter, pagination that only appears with more than 15 items, copy on the cell and on right click,
  virtualized rows for large result sets, and server-side filtering/sorting/pagination when the data is large.
- **Action bar:** items with a stable id; pinned ones are saved in `localStorage` with a unique key per
  screen; an action that needs a selection is disabled and states the reason.
- **Permissions:** every logged-in route lives inside `Private`; `hasPermission` checks the right
  project/resource; without permission, show a 403 page.
- **Mocks:** only in `mocks/` and always with the "Demo data" notice on screen.

---
