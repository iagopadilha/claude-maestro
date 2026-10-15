# Backend: descriptive naming, examples, splitting, fetch-build-save, logs

Moved out of SKILL.md to keep the fixed context small. Read it when the task touches naming, splitting a service or logging.

**Descriptive naming (explicit rule).** Applies to interfaces, classes, methods, parameters and private fields. **No abbreviations** (`Repo`, `Svc`, `Mgr`, `Notif`, `Cfg`, `Req`, `Res`, `Ctx`). Method = verb + object + destination (what it does, on whom / to where). Interface = name of the responsibility, never a generic `IService`. One name per concept.

| Bad | Good |
|-----|------|
| `ShipmentRepo` / `_shipmentRepo` | `ShipmentRepository` / `_shipmentRepository` |
| `_notification` | `_shipmentNotificationService` |
| `NotifyWebhookAsync` | `NotifyCarrierWebhookAsync` (to where) |
| `ProcessShipmentAsync` | `ProcessCarrierWebhookAsync` (what, about whom) |
| `IService` / `IEmailService` doing e-mail + webhook | `IFollowerEmailNotificationService` + `ICarrierWebhookNotificationService` |

```csharp
// C#
private readonly IShipmentRepository _shipmentRepository;
Task NotifyCarrierWebhookAsync(Shipment shipment, CancellationToken cancellationToken);
```
```typescript
// NestJS / TS
constructor(private readonly shipmentRepository: IShipmentRepository) {}
notifyCarrierWebhook(shipment: Shipment): Promise<void>;
```

**Split into smaller parts.** Extract object construction/application and event classification into separate factories (`ShipmentFactory`, `ShipmentEventFactory`); the service only orchestrates: validate -> fetch -> factory -> save -> notify. Distinct responsibilities (e-mail to followers vs. send to an external webhook) live in distinct interfaces/services.

**Fetch → build → save (approved shape).** For any service that fetches, maps and persists (synchronizations, imports): fetch everything first (concurrently), a **factory builds** the objects in memory and returns an `Add/Update/Remove` object (it never touches the database), and one shared saver persists it together. Never fetch or save inside a per-item loop, and **never declare a class inside another class** (own file). Factory is common but **any design pattern may be used when it fits** (Strategy for interchangeable rules per regime/type/provider, Template Method for a shared skeleton, Chain of Responsibility, Facade, Decorator, Observer…) — fit first, pattern second, never pattern-spray; help the user analyse by showing what is mixed and the split options before applying. Keep related logic together, split distinct responsibilities, and name every class/method for what it really does. The extracted piece does not have to be a factory: it can also be a **separate service** (validation, calculation, classification, integration), a shared saver, a result object or a helper — choose by responsibility, not by default and not by "feature". Full recipe, rules, "which shape for which piece" table and the Refactoring.Guru mapping: `references/separate-build-and-save.md`.

**Structured logs.** Named properties, never interpolation: `_logger.LogInformation("Webhook received {Event} {ExternalReference}", eventName, externalReference)`. Wrap the flow in `BeginScope` with trace fields (tenant/company ids, entity id, event, external reference). Never log a secret or full signature (mask/truncate). In webhook flows always carry fields that locate the record in the database (entity id + external reference). Do not add logs nobody asked for.

Example (order-sync integration, before → after):
- `TenantResolverIntegrationService` → `TenantLookupService` (only reads the identity provider)
- `AddressingIntegrationFactory` → `BatchTenantsFactory` (decides use / create / block)
- `Resolve` → `QueryTenantStatus`, `UseOrCreateTenants`, `GetTenantName` (one name per action)
- `LoadBatch` → `ReadTenantsFromIdentityProviderForBatch`; `ResolveBatch` → `ClassifyBatchTenants`
