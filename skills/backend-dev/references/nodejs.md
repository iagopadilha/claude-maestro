# NestJS Reference

## Folder Structure

Same architecture as .NET — Domain / Data / Api — separated by folders instead of projects.

```
src/
├── domain/
│   ├── interfaces/
│   │   ├── repositories/      # IOrderRepository, IProductRepository, etc.
│   │   └── services/          # IEmailService, IPaymentGateway, etc.
│   ├── models/                # Domain entities
│   ├── dto/                   # CreateOrderDto, OrderResponseDto, etc.
│   └── enums/
│
├── data/
│   ├── repositories/          # OrderRepository : IOrderRepository
│   ├── services/              # PaymentService : IPaymentService
│   ├── entities/              # TypeORM entities
│   └── migrations/
│
├── api/
│   ├── controllers/           # OrderController, ProductController
│   └── middlewares/
│
├── app.module.ts              # All DI registration
└── main.ts
```

---

## Dependency Injection

```typescript
// app.module.ts — only place where concretes are wired
@Module({
  imports: [TypeOrmModule.forFeature([OrderEntity])],
  controllers: [OrderController],
  providers: [
    { provide: 'IOrderRepository', useClass: OrderRepository },
    { provide: 'IOrderService', useClass: OrderService },
  ],
})
export class AppModule {}
```

---

## Full Example

```typescript
// ── domain ────────────────────────────────────────────────────────────

// domain/models/order.model.ts
export class Order {
  id: string;
  status: string;
  createdAt: Date;
}

// domain/dto/create-order.dto.ts
export class CreateOrderDto {
  @IsUUID() productId: string;
  @IsInt() @Min(1) quantity: number;
}

// domain/interfaces/repositories/order.repository.interface.ts
export interface IOrderRepository {
  findById(id: string): Promise<Order | null>;
  save(order: Order): Promise<Order>;
}

// domain/interfaces/services/order.service.interface.ts
export interface IOrderService {
  create(dto: CreateOrderDto): Promise<Order>;
  findById(id: string): Promise<Order>;
}


// ── data ──────────────────────────────────────────────────────────────

// data/repositories/order.repository.ts
@Injectable()
export class OrderRepository implements IOrderRepository {
  constructor(
    @InjectRepository(OrderEntity)
    private readonly orderTypeOrmRepository: Repository<OrderEntity>,
  ) {}

  async findById(id: string): Promise<Order | null> {
    const entity = await this.orderTypeOrmRepository.findOneBy({ id });
    return entity ? OrderMapper.toDomain(entity) : null;
  }

  async save(order: Order): Promise<Order> {
    const savedOrder = await this.orderTypeOrmRepository.save(OrderMapper.toEntity(order));
    return OrderMapper.toDomain(savedOrder);
  }
}

// data/services/order.service.ts
@Injectable()
export class OrderService implements IOrderService {
  constructor(
    @Inject('IOrderRepository')
    private readonly orderRepository: IOrderRepository,
  ) {}

  async create(dto: CreateOrderDto): Promise<Order> {
    const order = new Order();
    order.id = crypto.randomUUID();
    order.status = 'pending';
    order.createdAt = new Date();
    return this.orderRepository.save(order);
  }

  async findById(id: string): Promise<Order> {
    const order = await this.orderRepository.findById(id);
    if (!order) throw new NotFoundException(`Order ${id} not found`);
    return order;
  }
}


// ── api ───────────────────────────────────────────────────────────────

// api/controllers/order.controller.ts — no business logic
@Controller('orders')
export class OrderController {
  constructor(
    @Inject('IOrderService')
    private readonly orderService: IOrderService,
  ) {}

  @Post()
  create(@Body() dto: CreateOrderDto) {
    return this.orderService.create(dto);
  }

  @Get(':id')
  findOne(@Param('id') id: string) {
    return this.orderService.findById(id);
  }
}
```

---

## Validation & Exceptions

```typescript
// main.ts
app.useGlobalPipes(new ValidationPipe({ whitelist: true, forbidNonWhitelisted: true }));

// Inside services — use NestJS native exceptions
throw new NotFoundException('Order not found');
throw new BadRequestException('Invalid status');
throw new ConflictException('Order already processed');
```
