# Concurrency with DB access: one own scope per stage

Use this reference **only** when a flow needs to run things concurrently (`Task.WhenAll`) and the work touches the database (or other scoped, non-thread-safe state). If nothing runs in parallel, ignore it.

## Why

- `DbContext` is **not thread-safe**. Two tasks sharing one scope/context corrupt each other.
- So every stage that can run in parallel with another gets **its own DI scope** (own `DbContext`, own repositories).
- Parallel **reads** inside a stage use the repository's `*InParallel` methods: each call builds an **isolated `DbContext` through the context factory** (`DbFactory`), so several queries can be awaited together. If a store/table needs this and has no isolated-context factory yet, create one for it instead of sharing a context.
- Stages that write the same tables, or that must be tested as a unit, stay in the same sequential chain (below), not in parallel.

## The pattern (reference: `OrderSyncService`)

1. One shared executor owns scope creation: `ISyncStageExecutor.ExecuteInOwnScope<TService>(stage, service => ...)`. It creates the scope, seeds scoped credentials, runs the action, logs the failed stage with its stack, rethrows with the stage name, and forwards the scope's notifications. Never write a second copy of it.
2. The orchestrator is a list of stages, one block per stage, then `await Task.WhenAll(tasks)`:

```csharp
List<Task> tasks =
[
    _stageExecutor.ExecuteInOwnScope<ISyncOrderLinesService>("order-lines", async service =>
    {
        await service.SyncOrderLinesAsync(orderId, orderNumber);
    }),
    _stageExecutor.ExecuteInOwnScope<ISyncOrderAttachmentsService>("attachments", async service =>
    {
        await service.SyncOrderAttachmentsAsync(orderId, orderNumber);
    }),
];

await Task.WhenAll(tasks);
```

## Rules

- **Independent stages → separate entries in the list**, each in its own scope.
- **Dependent stages (B needs what A saved) → same entry, same scope, called one after the other.** Do not nest scopes inside scopes, do not `await` per stage outside, do not wrap with `Task.Run`, do not add `try/catch` to swallow errors. If A fails the exception stops the chain and B never runs on incomplete data.
- When the sequence spans different services, group them in one small service (Facade) whose method awaits them in order, and run **that** in one scope. Do not resolve services by hand inside the lambda (`GetRequiredService`) and do not create overloads of the executor.
- Prefer the typed `service` parameter over `IServiceProvider`; name nested/extra parameters for what they are (`inputs`, `restock`), never reuse `service`.
- **A step that reads what the parallel stages wrote (e.g. tax calculation over the saved orders/invoices) runs after `await Task.WhenAll(tasks)`, not inside the list.** It needs the final data and, inside the list, it would compete with the other stages for the same tables. It may call the injected service directly, with no scope of its own, as `OrderSyncService` does.
- Give every stage a descriptive name: it is what shows in the log and in the final error.
- Before parallelising, check the dependency between stages by reading what each one reads and writes (e.g. restock reads the orders/purchases that the order-lines stage writes → sequential). Confirm it, do not assume.
- Parallel stages writing at the same time can deadlock in MySQL; keep batches bounded for external calls (`FetchInBatchesAsync`), and save add/update/remove together per entity (see `separate-build-and-save.md`).
