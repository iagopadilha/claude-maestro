# Design Patterns

> **YAGNI first**: only suggest a pattern when there is a real need. Always ask before applying unsolicited abstractions.

---

## Strategy via Dependency Injection

**When**: Multiple interchangeable algorithms for the same operation.
**Warning sign**: `if (type === 'pix') ... else if (type === 'card') ...`

```csharp
// .NET — Domain: interface with type discriminator
public interface IPaymentStrategy
{
    string Type { get; }
    Task ProcessPaymentAsync(PaymentRequest request, CancellationToken cancellationToken = default);
}

// Data: one class per strategy
public class PixPaymentStrategy : IPaymentStrategy
{
    public string Type => "pix";
    public async Task ProcessPaymentAsync(PaymentRequest request, CancellationToken cancellationToken = default) { ... }
}

// Api/Program.cs: register all
builder.Services.AddScoped<IPaymentStrategy, PixPaymentStrategy>();
builder.Services.AddScoped<IPaymentStrategy, CreditCardPaymentStrategy>();

// Data: service resolves via DI — no if/else
public class PaymentService : IPaymentService
{
    private readonly IEnumerable<IPaymentStrategy> _strategies;

    public PaymentService(IEnumerable<IPaymentStrategy> strategies)
        => _strategies = strategies;

    public async Task ProcessPaymentAsync(PaymentRequest request, CancellationToken cancellationToken = default)
    {
        var strategy = _strategies.FirstOrDefault(s => s.Type == request.Type)
            ?? throw new NotSupportedException($"Type '{request.Type}' is not supported.");
        await strategy.ProcessPaymentAsync(request, cancellationToken);
    }
}
```

```typescript
// NestJS equivalent — same concept
// app.module.ts
providers: [
  { provide: 'IPaymentStrategy', useClass: PixPaymentStrategy, multi: true },
  { provide: 'IPaymentStrategy', useClass: CreditCardPaymentStrategy, multi: true },
  PaymentService,
]

// data/services/payment.service.ts
@Injectable()
export class PaymentService implements IPaymentService {
  constructor(
    @Inject('IPaymentStrategy')
    private readonly strategies: IPaymentStrategy[],
  ) {}

  async processPayment(request: PaymentRequest): Promise<void> {
    const strategy = this.strategies.find(s => s.type === request.type);
    if (!strategy) throw new BadRequestException(`Type '${request.type}' is not supported.`);
    await strategy.processPayment(request);
  }
}
```

---

## Factory + Strategy

**When**: Same as Strategy, but the lookup or construction logic deserves its own home. If plain DI-resolved Strategy is enough, skip the Factory.

```csharp
// Domain: factory interface
public interface INotificationFactory
{
    INotificationStrategy GetStrategy(string channel);
}

// Data: factory implementation wraps strategy collection
public class NotificationFactory : INotificationFactory
{
    private readonly IEnumerable<INotificationStrategy> _strategies;

    public NotificationFactory(IEnumerable<INotificationStrategy> strategies)
        => _strategies = strategies;

    public INotificationStrategy GetStrategy(string channel)
        => _strategies.FirstOrDefault(s => s.Channel == channel)
            ?? throw new NotSupportedException($"Channel '{channel}' is not supported.");
}

// Usage in service
public class NotificationService : INotificationService
{
    private readonly INotificationFactory _factory;

    public NotificationService(INotificationFactory factory) => _factory = factory;

    public async Task NotifyAsync(NotificationRequest request, CancellationToken cancellationToken = default)
    {
        var strategy = _factory.GetStrategy(request.Channel);
        await strategy.SendAsync(request, cancellationToken);
    }
}
```

---

## Builder

**When**: Constructing complex objects with many optional combinations.
**Warning sign**: Constructor or method with 5+ parameters, many optional.

```csharp
// .NET — Builder as inner class
public class Report
{
    public string Title { get; private set; } = null!;
    public DateRange Period { get; private set; } = null!;
    public string? Format { get; private set; }
    public bool IncludeSummary { get; private set; }

    private Report() {}

    public class Builder
    {
        private readonly Report _report = new();

        public Builder WithTitle(string title) { _report.Title = title; return this; }
        public Builder WithPeriod(DateRange period) { _report.Period = period; return this; }
        public Builder WithFormat(string format) { _report.Format = format; return this; }
        public Builder WithSummary() { _report.IncludeSummary = true; return this; }

        public Report Build()
        {
            if (string.IsNullOrEmpty(_report.Title)) throw new InvalidOperationException("Title is required.");
            if (_report.Period is null) throw new InvalidOperationException("Period is required.");
            return _report;
        }
    }
}

// Usage
var report = new Report.Builder()
    .WithTitle("Monthly Sales")
    .WithPeriod(new DateRange(start, end))
    .WithFormat("pdf")
    .WithSummary()
    .Build();
```

```typescript
// NestJS equivalent
export class ReportBuilder {
  private title?: string;
  private period?: DateRange;
  private format?: string;
  private includeSummary = false;

  withTitle(title: string): this { this.title = title; return this; }
  withPeriod(period: DateRange): this { this.period = period; return this; }
  withFormat(format: string): this { this.format = format; return this; }
  withSummary(): this { this.includeSummary = true; return this; }

  build(): Report {
    if (!this.title) throw new Error('Title is required.');
    if (!this.period) throw new Error('Period is required.');
    return { title: this.title, period: this.period, format: this.format, includeSummary: this.includeSummary };
  }
}
```

---

## Observer / Event-Driven

**When**: Reactions to domain events without coupling emitter to consumers.
**Warning sign**: A service directly calls 3+ unrelated services as part of its core flow.

```csharp
// After creating an order, notify stock, send email, etc. — handlers are decoupled
orderCreatedEvent.Emit(new OrderCreatedEvent(order));
```

---

## SOLID Quick Reference

**S violation** — service doing too much:
```csharp
// ❌ OrderService handles persistence + notification + validation
// ✅ Split: OrderService orchestrates, delegates to specialized services
```

**O violation** — modifying existing class for each new type:
```csharp
// ❌ if (type == "pdf") ... else if (type == "excel") ...
// ✅ interface IReportGenerator + PdfReportGenerator, ExcelReportGenerator
```

**D violation** — most common:
```csharp
// ❌ private readonly repository = new OrderRepository();
// ✅ constructor(IOrderRepository orderRepository)
```

---

## What to Ask Before Applying a Pattern

> "I noticed [situation]. This could be a good moment to apply [pattern], which would allow [benefit]. Want me to apply it now or keep it simple?"

- "You have 3 payment types with conditional logic — want to extract to a Strategy?"
- "This service is calling 4+ unrelated services — want to isolate via domain events?"
- "This construction has 6 parameters — want to introduce a Builder?"
