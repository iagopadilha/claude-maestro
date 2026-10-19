# Generic repository, BaseService and what NOT to do

Reference for `backend-dev`. Applies to any new .NET backend and to whatever a feature touches. In a project marked `LEGACY` (rule N0) only a minimal edit is allowed: point the problem out instead of fixing it.

## 1. The generic repository saves right away

In each operation the generic repository adds **and saves** (`DbSet.Add` + `SaveChanges`). The caller does not have to remember a `SaveChangesAsync` afterwards.

```csharp
public virtual async Task Add(TEntity entity)
{
    DbSet.Add(entity);
    await SaveChanges();
}

public virtual async Task Update(TEntity entity)
{
    DbSet.Update(entity);
    await SaveChanges();
}

public virtual async Task Remove(TEntity entity)
{
    DbSet.Remove(entity);
    await SaveChanges();
}
```

- **Batch** has its own version that saves **once** (`Add(IEnumerable<T>)` with `AddRange`, `Update(IEnumerable<T>)` with `UpdateRange`, `RemoveAll`). Use it to update many rows in one transaction, without one save per row.
- **`...WithoutSaving`** (`AddWithoutSaving`, `UpdateWithoutSaving`, `RemoveWithoutSaving`) exist for the case where **several repositories write in the same transaction** and the service calls `SaveChanges` once at the end. It is a justified exception, not the common path.
- **The change tracker and the save log live in the generic `SaveChanges`.** The service does not create a method just to save with a log.

## 2. Anti-patterns

### 2.1 Private method only to save, with try/catch and log

```csharp
// NO: a whole method to call SaveChanges with a log
private async Task SaveShipmentsAsync<TShipment>(IShipmentRepository<TShipment> repository, int companyCount)
{
    try { await repository.SaveChangesAsync(); }
    catch (Exception exception) { _logger.LogError(exception, "Failed to save..."); throw; }
    _logger.LogInformation("Shipments saved.");
}
```

Why it is bad: it takes space, hurts reading and repeats what the generic `SaveChanges` should do once for everyone. **Right:** call `await repository.UpdateMany(shipments)` (or `Update`) directly; the log and the tracker stay in the generic; the error bubbles up to the central handling point.

### 2.2 try/catch around every call

try/catch does not wrap each operation. It lives **at the central point the project already uses** to handle errors (a method or middleware that already has the try/catch), and the caller just calls. If the spec requires that a step failing must not bring the flow down (e-mail after saving, an optional lookup to another service), that is a **named step of the flow** whose name says so, not a generic wrapper.

### 2.3 Invented log scope or context

```csharp
// NO: own helper to open a "company scope" (BeginScope) in several places
private IDisposable? OpenCompanyScope(...) => _logger.BeginScope(...);
```

It looks like a scope meant for parallelism and makes the flow non-deterministic. Besides, the rest of the project does not do it: it is a **pattern break**. An own scope only for **real concurrency or parallelism** (see `concurrency-own-scope.md`) and only where the project already does it. In an ordinary sequential flow, use the fields already in the request log.

### 2.4 Private method that only forwards, only filters or only has a try/catch

A private method makes sense when it **does something**: decides, validates, builds with a rule (for example "should notify followers", a validation). Do not create a private method whose body is:
- **only one call** with a log or try/catch around it;
- **only filter or query logic** (a short `Where`, `track ? DbSet : DbSet.AsNoTracking()`): it stays at the point of use or, if several callers use it, becomes a repository method;
- **only a try/catch**, or **only a save with a log**.

Delete it and call directly. The separation has to make sense to whoever reads.

### 2.5 Class that only forwards (Middle Man) and parameter that only travels (tramp data)

Two different smells. Use the right name in review.

- **Middle Man** (Refactoring.Guru): a **class** in which almost every method just delegates to another, without deciding, validating, building or keeping anything. It is 2.4 at class scale.
- **Tramp data**: a **parameter** that crosses several layers that do not use it, only to reach whoever uses it far below. The symptom is a signature that grows in every layer on the path.

Typical case: a `CarrierRepository` with two methods, both pure delegation to an `ICarrierExternalApiService`.

```csharp
// NO: repository that only forwards to the external service
public async Task<string?> GetCarrierToken()
{
    return await _carrierExternalApiService.GetAuthToken();
}

public async Task<DomainDto?> FindDomain(string domainName, string carrierToken)
{
    return await _carrierExternalApiService.FindDomain(domainName, carrierToken);
}
```

What makes the case worse:
- **The layer is not respected.** Other classes call `ICarrierExternalApiService` directly. "Everything external goes through the repository" does not hold here, and the same service ends up with two paths.
- **The contract changes for no reason.** `GetAuthToken()` returns `string` and throws on failure. The wrapper returns `string?`, and the caller starts handling a null that never happens (`GetCarrierToken() ?? ...`).
- **The `async/await` only forwards the call.** It creates a state machine and adds nothing.

How to solve it (pick one):
1. **Remove Middle Man:** the caller injects the real service, and the intermediate class and interface are deleted.
2. **Give the class a real responsibility:** if the layer must exist, it has to do something. In this case the candidate is **caching the token until it expires**. Without the cache, every factory made a POST to the auth endpoint, and in a batch that meant one login per item.

A repository that only forwards is not a layer, it is an indirection. If there is no rule to put in the class, delete the class.

**When you see it, act.** This is a refactoring point, not just a comment:
- **In code the task already touches:** refactor in the same change, by option 1 or 2, and make sure behavior does not change (see `behavior-regression.md`).
- **In code outside the task, or in `LEGACY` (N0):** do not touch it. Point it out in the delivery with the location, the smell and the suggested fix.

## 3. BaseService

`BaseService` gathers what every service repeats (notify an error, validate with FluentValidation, integration failure). If a service needs something common, **add it to `BaseService`** (or to the generic repository); do not create a private copy in the service.

## 4. When the generic does NOT fit

Only in very specific cases: **concurrency or parallelism** (several stages writing at the same time, each with its own context). `concurrency-own-scope.md` covers it. Outside that case, improve the generic instead of detouring around it.

## 5. Quick checklist

- [ ] Does the project's generic repository save right away (or is there a justified `WithoutSaving` for the batch transaction)?
- [ ] Is the save log/tracker in the generic `SaveChanges`, not in a private service method?
- [ ] No try/catch wrapping an ordinary call; handling is at the central point?
- [ ] No private method that only forwards a call?
- [ ] No class (repository, service, facade) that only delegates (Middle Man), no parameter that only crosses layers unused (tramp data), and no wrapper that turns `T` into `T?` for no reason?
- [ ] No invented log scope in a sequential flow?
- [ ] Anything common that was missing went to the generic/`BaseService`, not to a service helper?

## 6. Tracked entity in `Update`

`DbSet.Update(entity)` marks the **whole** entity as modified. On an entity the context already tracks (read with tracking on), that rewrites every column, including stale values if another write happened in between. So the generic `Update` only calls `DbSet.Update` when the entity is **detached**; when tracked, the change tracker writes only what changed. Example: replacing the followers of a shipment must not overwrite the `Payload` that a webhook has just saved.
