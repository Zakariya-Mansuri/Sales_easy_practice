"""Default verified system data (fictional, from the brief).

Requests may supply their own record set; IDs are always assigned here, by
position, so the model and the UI refer to the same rows.
"""

from .schemas import StockRecord

DEFAULT_RECORDS: list[StockRecord] = [
    StockRecord(outlet="Indiranagar Mart", sku="Cola 500ml", system_stock=0),
    StockRecord(outlet="Metro Fresh", sku="Cola 500ml", system_stock=48),
    StockRecord(outlet="City Corner", sku="Juice 1L", system_stock=12),
]


def with_ids(records: list[StockRecord]) -> list[dict]:
    return [{"record_id": f"R{i}", **r.model_dump()} for i, r in enumerate(records, start=1)]
