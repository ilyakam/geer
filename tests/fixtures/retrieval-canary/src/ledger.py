class ReceiptLedger:
    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, object]] = {}

    def record(self, payload: dict[str, object]) -> None:
        receipt_id = str(payload["receipt_id"])
        self.receipts.setdefault(receipt_id, payload)
