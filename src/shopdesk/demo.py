from datetime import date, datetime
from decimal import Decimal

from shopdesk.integrations.commerce import (
    Account,
    InMemoryCommerceClient,
    LineItem,
    Money,
    Order,
    Shipment,
)

DEMO_CUSTOMER = "cus_demo_1"


def demo_commerce() -> InMemoryCommerceClient:
    orders = [
        Order(
            order_id="100245",
            customer_id=DEMO_CUSTOMER,
            status="shipped",
            placed_at=datetime(2026, 10, 18, 14, 2),
            total=Money(amount=Decimal("89.90"), currency="EUR"),
            items=[LineItem(sku="TS-BLK-M", name="Merino T-shirt, black, M", quantity=2)],
            shipment=Shipment(carrier="DHL", tracking_number="JD014600006281934455",
                              tracking_url="https://www.dhl.com/track?id=JD014600006281934455",
                              estimated_delivery=date(2026, 10, 27)),
            return_window_ends=date(2026, 11, 26),
            shipping_address={"line1": "Hauptstr. 1", "city": "Berlin", "zip": "10115"},
        ),
        Order(
            order_id="100198",
            customer_id=DEMO_CUSTOMER,
            status="delivered",
            placed_at=datetime(2026, 9, 2, 9, 41),
            total=Money(amount=Decimal("149.00"), currency="EUR"),
            items=[LineItem(sku="JK-OLV-L", name="Waxed jacket, olive, L", quantity=1)],
            shipment=Shipment(carrier="DPD", delivered_at=datetime(2026, 9, 5, 11, 20)),
            return_window_ends=date(2026, 10, 5),
        ),
        Order(
            order_id="100777",
            customer_id="cus_someone_else",
            status="processing",
            placed_at=datetime(2026, 10, 20, 8, 0),
            total=Money(amount=Decimal("20.00"), currency="EUR"),
            items=[LineItem(sku="SK-01", name="Wool socks", quantity=1)],
        ),
    ]
    accounts = [
        Account(customer_id=DEMO_CUSTOMER, first_name="Alex", email="alex@example.com",
                member_since=date(2023, 3, 14), loyalty_tier="gold", open_returns=0),
    ]
    return InMemoryCommerceClient(orders, accounts)
