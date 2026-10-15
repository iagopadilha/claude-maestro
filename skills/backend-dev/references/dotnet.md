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
