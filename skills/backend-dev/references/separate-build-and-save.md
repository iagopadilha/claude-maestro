# Separation: fetch → build → save

How to split a big synchronization/import service. Born in an order-sync refactor
(`SyncOrderItemsService` went from 240 lines to 100; construction moved to a factory).
This shape is the preferred one and should be applied wherever a service is growing.

## The shape

```
Service (orchestrates)         Factory (builds, never saves)        Base/Repository (saves)
─────────────────────          ────────────────────────────         ───────────────────────
1. fetch everything first  →   2. build objects in memory      →    3. save once, together
   (remote reads + DB reads,      (map, validate, decide             (add + update + remove
    all concurrent)                add / update / remove)             in one Task.WhenAll)
```

- **Service**: validates input, fetches, calls the factory, saves, notifies. No mapping, no `if exists/else`, no remove calculations.
- **Factory** (`I{Subject}Factory` in Domain, `{Subject}Factory` in Data, registered in DI): receives DTOs and the existing rows, returns what has to be persisted. **Never touches the database**, never calls a repository.
- **Result object** (`SyncChanges<T>`: `ToAdd`, `ToUpdate`, `ToRemove`): what the factory returns and the saver receives. One generic class, in its **own file** (Domain DTO).
- **Saver** (`SaveTogetherAsync<T>(repository, changes)` in `BaseService`): add, update and remove touch different rows and each `*InParallel` call uses its own DbContext, so they run in one `Task.WhenAll`.

## Separate into what fits: factory is one option, not the only one

The goal is **separation of responsibilities**, not "always make a factory". Pick the shape by what the extracted piece does:

| The piece… | Extract to | Example |
|------------|-----------|---------|
| builds objects (map, validate, decide add/update/remove) | **Factory** (`I{Subject}Factory`) | `OrderItemsFactory`, `StockRestockFactory` |
| has its own business rule or lifecycle, reused or testable alone | **A separate service** (`I{Responsibility}Service`) | a validation service, a calculation service, an external-API/sync-of-one-concern service |
| validates input/rules | **Validation service** or FluentValidation validator (project convention) | `{Entity}Validation` / `I{Subject}ValidationService` |
| only carries data between steps | **Result object / DTO in its own file** | `SyncChanges<T>`, `StockBalanceRestock` |
| persists the same way in many places | **Shared saver** (base method or small class) | `SaveTogetherAsync<T>` |
| is a pure rule with no state | **Static helper inside the class that owns it** (or a small class if shared) | `ItemKey`, `AllocateActualToItem` |

### Design patterns are on the table too (not only Factory)

Factory is the common one, but **any design pattern may be used when it fits and makes the code easier to understand**. Look at what varies, and pick:

| Force you see in the code | Pattern (Refactoring.Guru) | How it shows up here |
|---------------------------|---------------------------|----------------------|
| The same step has several interchangeable rules/algorithms chosen at runtime (per regime, per type, per client) | **Strategy** | `I{Variant}{Subject}Strategy` implementations injected as `IEnumerable<>`, the service picks one and delegates (e.g. calculation per regime, classification per event) |
| Same skeleton in several flows, only some steps differ | **Template Method** | base class with the fixed fetch → build → save order; subclasses fill the steps (e.g. two sibling sync flows) |
| A request passes through ordered checks/handlers, any of which can stop or handle it | **Chain of Responsibility** | validation or classification steps chained |
| A messy subsystem needs one simple entry point | **Facade** | a service that hides several services/repositories behind one method |
| Add behavior (log, cache, retry, metrics) around a service without editing it | **Decorator** / **Proxy** | wrap the service with the same interface |
| Something happens and several parties react | **Observer** | events instead of the service calling each party |
| Build one object step by step with many optional parts | **Builder** | rather than a giant constructor |
| An action must be queued, retried, undone or logged as an object | **Command** | |

Rules for using patterns:
- **Fit first, pattern second.** Name the smell and the force, then pick the simplest move. A plain method, a separate service or a factory beats a pattern that does not match the force (no pattern-spraying; Singleton almost never).
- **Help with the analysis.** When reviewing or building code, say what is mixed together, propose the separation (and the pattern if one truly fits), list the options with one-line trade-offs, and let the user decide. Do not apply a big structural change without showing the split first.
- Everything that varies by "which one" (regime, type, provider) is a Strategy candidate; everything that is "how to build it" is a Factory candidate; everything that is "one responsibility with its own rule" is a separate service candidate.

### Keep related logic together; keep names descriptive

- **Separate responsibilities, but do not scatter one idea across many files.** The goal is understanding: a reader should find one concern in one place. If a rule only makes sense next to its caller and is small, keep it there. Do not leave everything in one giant class either — that is the opposite smell.
- **Every method, class and service says what it actually does, and on what.** `BuildImportBalances`, `CalculateTaxSavingsOfInvoice`, `ValidateRestockItems` — never `Process`, `Resolve`, `Execute`, `Handle`, `Helper`. If you cannot name it clearly, the piece is probably doing two things: split it again.
- Applies equally to the orchestrating service's own methods: they read like the steps of the flow.

- Do **not** split by "features" automatically or by file size alone. Split where there is a distinct responsibility; if two things always change together and are tiny, leave them together (YAGNI).
- A separate **service** is the right call when the piece decides or computes something on its own (validation, calculation, classification, an integration), has a clear interface name, and could be injected elsewhere. A **factory** is the right call when the piece only constructs things from inputs.
- Several small focused classes beat one big one, but keep one subject per class and a descriptive name. Register every new class in DI (interface + implementation).
- The orchestrating service keeps only: fetch → call the pieces → save → notify.

## Rules

1. **Fetch all first, then build, then save once.** Never fetch or save inside the per-item loop (N+1). One query with `IN (ids)` and a `ToLookup` beats one query per item.
2. **One save per entity type per run**, not one per parent. Accumulate (`Accumulate`) and save the whole batch.
3. **Never declare a class inside a class** (nested `private sealed class`, nested records for data). It is an anti-pattern: give it its own file. A small data holder goes to a DTO folder; behavior goes to a factory or helper.
4. **Do not copy the building block per entity.** If two flows differ only in type and fields, keep two small methods in the same factory; if the loop is identical, parameterize it (see Duplicate Code below).
5. **Names only need to be descriptive** — say what it builds/saves and on what. `BuildOrders`, `BuildInvoices`, `SaveTogetherAsync` are examples, not a required vocabulary; any clear name works (`CreateRestockBalances`, `ReconcileLedgerEntries`). Never `Process`, `Resolve`, `Handle`. Keep building and saving visibly different in the name.
6. **Keep behavior identical when extracting.** Same decision per item, same removal criteria, same order of dependent saves (parents before children). Only change concurrency when the user asks, and say so.
7. **One factory per subject** (e.g. order items), not one per entity, unless it gets large. Do not create layers "just in case" (YAGNI).

## Mapping to Refactoring.Guru (use together with `refactoring-guru-techniques`)

| What you do | Refactoring.Guru name | Smell it fixes |
|-------------|----------------------|----------------|
| Service loop → factory class | **Extract Class** / **Move Method** | Large Class, Long Method |
| Mapping + validation + audit fields in the factory | **Replace Constructor with Factory Method** (creation logic out of the caller) | Long Method, Feature Envy |
| `toAdd` / `toUpdate` / `toRemove` lists passed around → `SyncChanges<T>` | **Introduce Parameter Object** / **Replace Array with Object** | Data Clumps, Long Parameter List |
| Same persist block in N services → `SaveTogetherAsync` | **Extract Method** + **Parameterize Method** (then **Pull Up Method** to the base) | Duplicate Code |
| Build separate from save | **Separate Query from Modifier** | hidden side effects |
| `if (x == null) {…} if (x != null) {…}` → `if/else` | **Decompose Conditional** | Switch/conditional noise |
| `_user.GetUserName()` read each item → once before the loop | **Extract Variable** / **Inline Temp** (inverse) | repeated calls |
| Nested class → own file | **Extract Class** | Large Class, Temporary Field |

Pattern vocabulary: the factory here is plain construction logic moved to a class (closest to **Factory Method** in intent); do not
turn it into Abstract Factory or Builder unless there is a real family of products or stepwise construction.

## Checklist when a service grows past ~150 lines

- [ ] Is anything fetched or saved inside a loop? Move the query out (lookup/`IN`) or accumulate and save once.
- [ ] Is there mapping, validation or "create or update" logic in the service? Move to a factory.
- [ ] Do 2+ places repeat "add / update / remove together"? Use the shared saver.
- [ ] Any class declared inside another class? Move to its own file.
- [ ] Does the factory ever call a repository? It must not.
- [ ] Same save order for parent → child entities, same decisions per item (behavior unchanged)?
