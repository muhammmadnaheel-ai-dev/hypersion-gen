"""Curated regional document formats. This library never contains uploaded records."""

TEMPLATES = {
    "en_PK": {
        "currency_symbol": "Rs",
        "date_format": "DD MMM YYYY",
        "invoice": {"title": "TAX INVOICE", "tax_label": "Sales tax", "sections": ["issuer", "customer", "items", "totals"]},
        "statement": {"title": "ACCOUNT STATEMENT", "sections": ["account", "period", "transactions", "closing balance"]},
    },
    "en_US": {
        "currency_symbol": "$",
        "date_format": "MMM DD, YYYY",
        "invoice": {"title": "INVOICE", "tax_label": "Sales tax", "sections": ["issuer", "customer", "items", "totals"]},
        "statement": {"title": "BANK STATEMENT", "sections": ["account", "period", "transactions", "closing balance"]},
    },
    "en_GB": {
        "currency_symbol": "£",
        "date_format": "DD MMM YYYY",
        "invoice": {"title": "VAT INVOICE", "tax_label": "VAT", "sections": ["issuer", "customer", "items", "totals"]},
        "statement": {"title": "ACCOUNT STATEMENT", "sections": ["account", "period", "transactions", "closing balance"]},
    },
}


def retrieve_template(locale: str, kind: str) -> dict:
    region=TEMPLATES.get(locale,TEMPLATES["en_US"])
    return {"locale":locale if locale in TEMPLATES else "en_US", "currency_symbol":region["currency_symbol"], "date_format":region["date_format"], **region[kind]}
