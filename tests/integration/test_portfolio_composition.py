"""生产组合根负责构造基础设施并注入服务层。"""

from __future__ import annotations

from app.services import portfolio_service


def test_portfolio_factory_injects_shared_store_and_library(monkeypatch):
    class Store:
        pass

    class Library:
        def query_by_code(self, product_code: str):
            return {
                "performance": {
                    "nav": 1.25,
                    "as_of": "2026-09-23",
                    "source": "product_performance",
                },
            }

    store = Store()
    library = Library()
    monkeypatch.setattr(portfolio_service, "_service", None)
    monkeypatch.setattr("app.crud.portfolio_crud.get_portfolio_store", lambda: store)
    monkeypatch.setattr("app.crud.product_crud.get_product_library", lambda: library)

    service = portfolio_service.get_portfolio_service()

    assert portfolio_service.get_portfolio_service() is service
    assert service.store is store
    assert service.library is library
    assert service.nav_source.latest_nav("110011").nav == 1.25
