# Next.js reference

Look and behavior are the same as in Vite projects. Only the shell changes: routing, error pages and
loading states. The shared UI pieces work in Next by replacing `useNavigate` with `useRouter` from
`next/navigation` and putting `'use client'` on the first line.

## App Router structure

```
app/
├── layout.tsx              # Root layout — the Toaster must live here
├── page.tsx
├── orders/
│   ├── page.tsx            # /orders
│   └── [id]/page.tsx       # /orders/:id
│
src/                        # Same structure as Vite projects, without the pages/ folder (in Next the routes live in app/)
```

---

## Server Components and Client Components

The default is Server Component. Only add `'use client'` when you need it.

- **Data grid:** register the grid modules (e.g. AG Grid `ModuleRegistry.registerModules([AllCommunityModule])`) in a
  `'use client'` provider (`components/providers/grid-provider.tsx`) used in `app/layout.tsx`. In the (server)
  `layout.tsx` the module is not registered in the browser and the grid throws.
- **Page that reads the database** (an ORM in `page.tsx`): the page stays server and fetches the data; the grid, the
  toolbar and the side panel go to a `'use client'` component that receives the data by prop.
- **Dev:** never two `next dev` on the same project (`.next` gets corrupted).

| Needs `'use client'` | Can stay a Server Component |
|----------------------|-----------------------------|
| `useState`, `useEffect` | Showing static data |
| Events (`onClick`) | Initial data fetch |
| Hooks (TanStack Query, Toast, i18next) | Content that matters for SEO |
| Browser APIs | Layout and navigation structure |

```tsx
// app/orders/[id]/page.tsx — Server Component: fetches the initial data directly
export default async function OrderDetailPage({ params }: { params: { id: string } }) {
  const order = await orderService.fetchById(params.id)
  return <OrderDetail order={order} />
}
```

```tsx
// components/order-detail.tsx — Client Component: handles interaction
'use client' // always on the first line of the file

export function OrderDetail({ order }: { order: Order }) {
  const { updateStatus, isPending } = useOrderMutations(order.id)
  // interactive UI here
}
```

**Rule:** Server Components fetch directly with `async/await`. Client Components use TanStack Query
to keep server state cached and be able to refetch.

---

## Root layout setup

```tsx
// app/layout.tsx
import { Toaster } from '@/components/ui/toaster'

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          {children}
          <Toaster /> {/* required: it is what displays the toasts from @/components/toast */}
        </Providers>
      </body>
    </html>
  )
}

// app/providers.tsx
'use client'

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(() => new QueryClient())
  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  )
}
```

---

## Error, 404 and loading pages

| Vite + React Router | Next.js (App Router) |
|---|---|
| `ErrorBoundary` with the 500 page | `app/error.tsx` (and `app/global-error.tsx` for the root layout) |
| wildcard route with the 404 | `app/not-found.tsx` |
| `GenericLoading` while loading | `app/loading.tsx` with the same spinner |
| Back with a fixed destination: `navigate('/route')` | `router.push('/route')` (never `router.back()`: it loops) |
| app base path in Vite | `basePath` in `next.config` |

```tsx
// app/error.tsx — the standard 500 page
'use client'

export default function Error({ reset }: { error: Error; reset: () => void }) {
  // Do not rethrow the error or call console.error here: in development that opens the
  // Next "1 error" badge over the screen, and it must not appear.
  return <ErrorPage code={500} onAction={reset} />
}
```
