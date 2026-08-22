SELLERS = ("Olmas", "Otabek", "Ali", "Abbos")


def normalize_seller(value: str) -> str:
    normalized = value.strip().casefold()
    for seller in SELLERS:
        if seller.casefold() == normalized:
            return seller
    raise ValueError("Выберите продавца кнопкой: Olmas, Otabek, Ali или Abbos")
