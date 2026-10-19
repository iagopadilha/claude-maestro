# .NET Reference

## Project Structure

```
ProductName.sln
├── ProductName.Domain/        # Pure core — no external dependencies
│   ├── Interfaces/
│   │   ├── Repositories/      # IOrderRepository, IProductRepository, etc.
│   │   └── Services/          # IEmailService, IPaymentGateway, etc.
│   ├── Models/                # Domain entities + EF Core models
│   ├── DTOs/                  # CreateOrderDto, OrderResponseDto, etc.
│   └── Enums/
│
├── ProductName.Data/          # Adapters — depends only on Domain
│   ├── Repositories/          # OrderRepository : IOrderRepository
│   ├── Services/              # PaymentService : IPaymentService
│   ├── Context/               # AppDbContext
│   └── Migrations/
│
└── ProductName.Api/           # Composition root
    ├── Controllers/           # OrderController, ProductController
    ├── Middlewares/
    └── Program.cs             # All DI registration
```

---

## Dependency Injection

```csharp
// Program.cs — only place where concretes are wired
builder.Services.AddScoped<IOrderRepository, OrderRepository>();
builder.Services.AddScoped<IOrderService, OrderService>();
```

| Lifetime | When to use |
|----------|-------------|
| `AddScoped` | Default — services and repositories (per HTTP request) |
| `AddSingleton` | Expensive stateless services (cache, config) |
| `AddTransient` | Lightweight stateless utilities |

---

### Separate registration per area (pattern once the DI grows)

When dependency registration is split, use this shape.

- A single entry point, `ResolveDependencies`, called once from `Program.cs`. It registers nothing itself: it only calls the areas, in order.
- One static class per business area in `Configuration/DependencyInjection/`, named `<Area>DependencyInjection`, with an `Add<Area>Dependencies` method that returns the `IServiceCollection`.
- Each area registers everything that belongs to it: validators, repositories, factories and services, in that order, grouped by blank lines.
- Cross-cutting things (logging, HTTP client, per-environment addresses, database) go in `Infrastructure`. Clients of external APIs go in `Integration`.
- A new feature registers in its own area; a single file must not grow back. Two features touching different areas never edit the same file.

```csharp
// Configuration/DependencyInjectionConfig.cs
public static class DependencyInjectionConfig
{
    public static IServiceCollection ResolveDependencies(this IServiceCollection services)
    {
        services.AddInfrastructureDependencies();
        services.AddIntegrationDependencies();
        services.AddCreditDependencies();
        services.AddShipmentTrackingDependencies();

        return services;
    }
}

// Configuration/DependencyInjection/CreditDependencyInjection.cs
public static class CreditDependencyInjection
{
    public static IServiceCollection AddCreditDependencies(this IServiceCollection services)
    {
        services.AddScoped<IValidator<RegisterCreditPurchaseDto>, RegisterCreditPurchaseDtoValidator>();

        services.AddScoped<ICreditPurchaseRepository, CreditPurchaseRepository>();
        services.AddScoped<ICustomerCreditRepository, CustomerCreditRepository>();

        services.AddScoped<ICustomerCreditSummaryFactory, CustomerCreditSummaryFactory>();

        services.AddScoped<ICreditService, CreditService>();

        return services;
    }
}

// Program.cs
builder.Services.ResolveDependencies();
```

When splitting an existing DI, keep the same implementations with the same lifetimes and compare the registration count before and after.

---

## Per-environment URLs (ASPNETCORE_ENVIRONMENT)

A link to the portal or to another app is never fixed in a `const`/`static`. An injectable class reads the environment in its constructor; whoever builds an e-mail, webhook or response receives the interface.

```csharp
// Domain/Interfaces
public interface IPortalAddresses
{
    string TrackingUrl { get; }
    string ShipmentUrl(ShipmentType type, long shipmentId);
}

// Data/Utils
public class PortalAddresses : IPortalAddresses
{
    private const string ProductionBase = "https://app.example.com";
    private const string DevelopmentBase = "https://dev.example.com";

    // ASPNETCORE_ENVIRONMENT = Development uses dev; any other environment uses production.
    public PortalAddresses(IHostEnvironment hostEnvironment)
    {
        var portalBase = hostEnvironment.IsDevelopment() ? DevelopmentBase : ProductionBase;

        TrackingUrl = $"{portalBase}/tracking/";
    }

    public string TrackingUrl { get; }

    public string ShipmentUrl(ShipmentType type, long shipmentId) => $"{TrackingUrl}#/{type.Modal()}/{shipmentId}";
}

// DI: reads the configuration once
services.AddSingleton<IPortalAddresses, PortalAddresses>();
```

- If another app's URL already has its own key in the project's secret manager or configuration (`ExternalApi:*`), read that key instead of deciding by environment.
- Accepted exception, commented in the code: a public static file that only exists in one environment (for example an e-mail icon served only in production).

---

## JSON (Newtonsoft.Json)

Project convention: pick ONE JSON library per flow. When the project uses Newtonsoft.Json, do not mix it with System.Text.Json in the same flow: Newtonsoft cannot write `JsonElement` (it outputs `{"ValueKind":1}`).

- External call with Flurl: pass the object to `PostJsonAsync(obj)` and let the client's serializer work. Do not serialize to a string first.
- camelCase contract without an attribute on every property: swap the serializer only for that call, using `CamelCasePropertyNamesContractResolver`.
- **Serializer and converter settings are always global.** Before touching JSON in a project, look for the global configuration (`NewtonsoftJsonConfig`, `JsonConvert.DefaultSettings`, converters registered once). If none exists, create it globally and make callers inherit it. Never fix it per call with a private settings field; only a contract-specific add-on (for example camelCase for one integration) stays on the call.
- Global default, set once: `JsonConvert.DefaultSettings` in a `NewtonsoftJsonConfig` class called from `Program.cs`. It applies to `JsonConvert` and to Flurl's Newtonsoft serializer (including a `NewtonsoftJsonSerializer` created with its own settings, which inherits the default).
- The default reads untyped JSON (`JToken`, `object`) keeping dates as text (`DateParseHandling.None`) and decimals as received (`FloatParseHandling.Decimal`). Without it, date text becomes `DateTime` (time zone and format may change) and `1.50` becomes `1.5`. Useful for third-party payload passthrough and data migration.
- Typed DTOs do not change: a `DateTime`, `DateTimeOffset`, `double` or `decimal` property converts normally with or without this default.
- `JToken.Parse` **ignores** the global default. For third-party JSON use `JsonConvert.DeserializeObject<JToken>(json)`.
- Keys inside a `JToken` are not changed by the camelCase resolver.

```csharp
// Configuration/NewtonsoftJsonConfig.cs, called from Program.cs: builder.Services.AddNewtonsoftJsonConfig();
public static IServiceCollection AddNewtonsoftJsonConfig(this IServiceCollection services)
{
    JsonConvert.DefaultSettings = () => new JsonSerializerSettings
    {
        DateParseHandling = DateParseHandling.None,
        FloatParseHandling = FloatParseHandling.Decimal,
        ReferenceLoopHandling = ReferenceLoopHandling.Ignore,
    };

    return services;
}

// Flurl client: inherits the default
new FlurlClientBuilder().UseNewtonsoft()...

// Reading the raw payload
var shipment = JsonConvert.DeserializeObject<JToken>(payloadJson);

// camelCase sending only for this call (inherits the global default and adds the resolver)
private static readonly NewtonsoftJsonSerializer WebhookSerializer = new(new JsonSerializerSettings
{
    ContractResolver = new CamelCasePropertyNamesContractResolver(),
});

await _flurlClient.Request(url)
    .WithSettings(settings => settings.JsonSerializer = WebhookSerializer)
    .PostJsonAsync(notification, cancellationToken: cancellationToken);
```

### DTO shape
Class with one property per line and the attribute on the line above the property. No positional `record` with `[property: ...]` attributes inside the parameter list. No `[JsonPropertyName]` when the serializer's naming policy already produces the contract name (for example `JsonSerializerDefaults.Web` turns `TrackingUrl` into `trackingUrl`); keep it only when the JSON name cannot come from the property name. No blank line between plain properties.

---

## Full Example

```csharp
// ── Domain ────────────────────────────────────────────────────────────

// Models/Order.cs
public class Order
{
    public Guid Id { get; set; }
    public string Status { get; set; } = null!;
    public DateTime CreatedAt { get; set; }
}

// DTOs/CreateOrderDto.cs
public record CreateOrderDto(Guid ProductId, int Quantity);
public record OrderResponseDto(Guid Id, string Status, DateTime CreatedAt);

// Interfaces/Repositories/IOrderRepository.cs
public interface IOrderRepository
{
    Task<Order?> GetByIdAsync(Guid id, CancellationToken cancellationToken = default);
    Task AddAsync(Order order, CancellationToken cancellationToken = default);
}

// Interfaces/Services/IOrderService.cs
public interface IOrderService
{
    Task<OrderResponseDto> CreateAsync(CreateOrderDto dto, CancellationToken cancellationToken = default);
}


// ── Data ──────────────────────────────────────────────────────────────

// Repositories/OrderRepository.cs
public class OrderRepository : IOrderRepository
{
    private readonly AppDbContext _context;

    public OrderRepository(AppDbContext context) => _context = context;

    public async Task<Order?> GetByIdAsync(Guid id, CancellationToken cancellationToken = default)
        => await _context.Orders.FindAsync([id], cancellationToken);

    public async Task AddAsync(Order order, CancellationToken cancellationToken = default)
    {
        await _context.Orders.AddAsync(order, cancellationToken);
        await _context.SaveChangesAsync(cancellationToken);
    }
}

// Services/OrderService.cs
public class OrderService : IOrderService
{
    private readonly IOrderRepository _orderRepository;

    public OrderService(IOrderRepository orderRepository)
        => _orderRepository = orderRepository;

    public async Task<OrderResponseDto> CreateAsync(CreateOrderDto dto, CancellationToken cancellationToken = default)
    {
        var order = new Order { Id = Guid.NewGuid(), Status = "Pending", CreatedAt = DateTime.UtcNow };
        await _orderRepository.AddAsync(order, cancellationToken);
        return new OrderResponseDto(order.Id, order.Status, order.CreatedAt);
    }
}


// ── Api ───────────────────────────────────────────────────────────────

// Controllers/OrderController.cs — no business logic
[ApiController]
[Route("api/orders")]
public class OrderController : ControllerBase
{
    private readonly IOrderService _orderService;

    public OrderController(IOrderService orderService) => _orderService = orderService;

    [HttpPost]
    public async Task<IActionResult> Create([FromBody] CreateOrderDto dto, CancellationToken cancellationToken)
    {
        var result = await _orderService.CreateAsync(dto, cancellationToken);
        return CreatedAtAction(nameof(Create), new { result.Id }, result);
    }
}
```

---

## Result Pattern

```csharp
public class Result<T>
{
    public bool IsSuccess { get; }
    public T? Value { get; }
    public string? Error { get; }

    private Result(bool isSuccess, T? value, string? error)
    {
        IsSuccess = isSuccess;
        Value = value;
        Error = error;
    }

    public static Result<T> Success(T value) => new(true, value, null);
    public static Result<T> Failure(string error) => new(false, default, error);
}
```
