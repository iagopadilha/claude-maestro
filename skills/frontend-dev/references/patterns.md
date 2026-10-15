# React patterns: component, hook, query, service

## Service: HTTP calls, no state

Functions declared one by one and exported together in an object at the end of the file.
Always typed: no `any` in parameters or responses.
Each function handles the error in the `catch` with the `ErrorHandler` (`utils/request-messages-handler`), which
shows the error toast. Warnings that come in the response (`data.warnings`) go through the `WarningHandler`.

```typescript
// services/order-service.ts
import { api } from '@/lib/axios'
import { ErrorHandler, WarningHandler } from '@/utils/request-messages-handler'

interface FetchOrdersParams {
  currentPage: number
  itemsPerPage: number
  searchText?: string
}

const fetchAll = async (params: FetchOrdersParams): Promise<Order[]> => {
  try {
    const { data } = await api.get('/api/v1/orders', { params })
    if (data?.warnings) WarningHandler(data.warnings)
    return data.data
  } catch (error: unknown) {
    return ErrorHandler(error)
  }
}

const fetchById = async (id: string): Promise<Order> => {
  try {
    const { data } = await api.get(`/api/v1/orders/${id}`)
    if (data?.warnings) WarningHandler(data.warnings)
    return data.data
  } catch (error: unknown) {
    return ErrorHandler(error)
  }
}

const create = async (dto: CreateOrderDto): Promise<Order> => {
  try {
    const { data } = await api.post('/api/v1/orders', dto)
    if (data?.warnings) WarningHandler(data.warnings)
    return data.data
  } catch (error: unknown) {
    return ErrorHandler(error)
  }
}

const update = async (id: string, dto: UpdateOrderDto): Promise<Order> => {
  try {
    const { data } = await api.put(`/api/v1/orders/${id}`, dto)
    if (data?.warnings) WarningHandler(data.warnings)
    return data.data
  } catch (error: unknown) {
    return ErrorHandler(error)
  }
}

export const orderService = {
  fetchAll,
  fetchById,
  create,
  update,
}
```

> The `ErrorHandler` shows the error and rethrows. That way the mutation falls into the error state and
> `onSuccess` does not run. Without the `try/catch`, the API error **does not show on screen**: the axios
> interceptor only handles 401 (expired session).

> A project with many domains may split services into subfolders by domain
> (`services/billing/`, `services/shipping/`).

---

## Hook: state, queries and mutations

```typescript
// pages/orders/hooks/use-order.ts
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { showSuccess } from '@/components/toast'
import { orderService } from '@/services/order-service'

export function useOrder(id: string) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const { data: order, isLoading, error } = useQuery({
    queryKey: ['order', id],
    queryFn: () => orderService.fetchById(id),
    enabled: !!id,
  })

  const { mutate: updateStatus, isPending } = useMutation({
    mutationFn: (dto: UpdateOrderDto) => orderService.update(id, dto),
    onSuccess: () => {
      showSuccess(t('order_updated'))
      queryClient.invalidateQueries({ queryKey: ['order', id] })
    },
    // no onError: the service's catch (ErrorHandler) already showed the error
  })

  return { order, isLoading, error, updateStatus, isPending }
}
```

**Split when the hook grows (target: around 150 lines):**
```typescript
// the main hook only COMPOSES hooks with one subject each (SRP)
export function useOrderDetailPage() {
  const { id = '' } = useParams<{ id: string }>()
  const { t } = useTranslation()
  const { order, isLoading } = useOrderDetails(id)               // queries only
  const { approveOrder, isApprovingOrder } = useOrderMutations(id) // mutations only
  const filters = useOrderFilters()                              // local filter state only
  return { t, order, isLoading, approveOrder, isApprovingOrder, filters }
}
```

A hook can call a hook. A hook that serves more than one page and has a single subject (e.g. `useDialogOpening`,
`useDebounce`) is **generic** and goes to the global `hooks/`.

---

## Component: visual only, uses its own hook

The component **declares nothing**: no `useState`, `useEffect`, `useMemo`, `useTranslation`,
`useParams`, `useNavigate`, no derived variable, no function with logic. It calls **its own single hook**
(`use{Component}`) and only renders what the hook returns.

```tsx
// pages/order-detail/index.tsx
import { useOrderDetailPage } from './hooks/use-order-detail-page'

export default function OrderDetailPage() {
  const { t, order, isLoading, approveOrder, isApprovingOrder } = useOrderDetailPage()

  if (isLoading) return <GenericLoading />
  if (!order) return <EmptyState />

  return (
    <PageLayout>
      <BackButton />
      <OrderHeader order={order} />
      <OrderActions
        approveLabel={t('approve')}
        onApprove={approveOrder}
        loading={isApprovingOrder}
      />
    </PageLayout>
  )
}
```

```tsx
// routes/routes.tsx: the route imports the page by its folder
import OrderDetailPage from '@/pages/order-detail'
```

**Rules:**
- The page lives in `pages/<route>/index.tsx` and is exported with `export default`
- Every component has its hook in `hooks/use-<component>.ts`. No logic in the component body
- Only accepted in the component body: its own hook and reusable **generic** hooks with a single subject
- Purely visual `useState` (open/close, active tab) also goes to the hook
- No `fetch`, `axios` or `useEffect` inside the component
- `interface`, `type`, `enum` and constants **never** live in the component or hook file: they go to `types/` and `constants/`
- When the JSX grows, extract subcomponents (each with its own hook if it has logic)

---

## Splitting components

```tsx
// Build the screen from focused pieces; each piece handles its own logic in its own hook
export default function OrderDetailPage() {
  const { order } = useOrderDetailPage()
  return (
    <PageLayout>
      <BackButton />
      <OrderHeader order={order} />
      <OrderItems />
      <OrderActions />
    </PageLayout>
  )
}
```

---

## Form: react-hook-form + zod

```typescript
// pages/orders/validations/create-order-schema.ts
import { z } from 'zod'

// the message is the translation key; FormMessage translates it with i18next
export const createOrderSchema = z.object({
  number: z.string().min(1, 'enter_the_order_number'),          // "Enter the order number. E.g. 2026-0001"
  totalValue: z.number({ message: 'enter_the_total_value' }),    // "Enter the total value. E.g. 1,250.00"
  notes: z.string().optional(),
})

export type CreateOrderForm = z.infer<typeof createOrderSchema>
```

```typescript
// pages/orders/hooks/use-create-order.ts
import { useForm } from 'react-hook-form'
import { zodResolver } from '@/lib/zod-resolver' // never from '@hookform/resolvers/zod'
import { createOrderSchema, type CreateOrderForm } from '../validations/create-order-schema'

export function useCreateOrder() {
  const form = useForm<CreateOrderForm>({
    resolver: zodResolver(createOrderSchema),
    defaultValues: { number: '', notes: '' },
  })
  // ...submit with the mutation
  return { form }
}
```

> The `zodResolver` from `@/lib/zod-resolver` keeps the schema. That way `FormLabel` knows by itself which
> fields are required and shows the asterisk and bold, without repeating the rule in the JSX.

---

## TypeScript

```typescript
// Props: always explicit
interface OrderCardProps {
  order: Order
  onSelect: (id: string) => void
  highlighted?: boolean
}

// API types: same as the backend DTOs
interface Order {
  id: string
  status: OrderStatus
  items: OrderItem[]
  createdAt: string
  total: number
}

enum OrderStatus {
  Pending = 'Pending',
  Approved = 'Approved',
  Cancelled = 'Cancelled',
}

// Never
const params: any = { ... }

// Always
interface FetchOrdersParams { ... }
```

---

## Query keys (TanStack Query)

The key is the name under which the query result is stored in the cache. The same key must be
used in the query and in the invalidation after saving; if they differ, the screen keeps the old
data and no error appears.

```typescript
// lib/query-keys.ts: centralized and hierarchical
export const queryKeys = {
  orders: ['orders'] as const,
  order: (id: string) => ['orders', id] as const,
  filteredOrders: (filters: OrderFilters) => ['orders', 'filters', filters] as const,
}

// Invalidation after the mutation
queryClient.invalidateQueries({ queryKey: queryKeys.orders })
```

> A project that does not have `lib/query-keys.ts` yet: follow the keys that already exist, in kebab-case
> (`['order-items', orderId]`), and use the same key in the query and in the invalidation.
