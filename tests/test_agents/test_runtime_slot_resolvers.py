import pytest


@pytest.mark.asyncio
async def test_stock_resolver_uses_tool_schema_and_preserves_ambiguous_options():
    from app.agents.runtime import build_slot_resolvers

    class Tool:
        name = "get_meta_tickers_search"
        args = {"q": {"type": "string"}}

        async def ainvoke(self, arguments):
            assert arguments == {"q": "平安"}
            return {"data": [{"code": "000001.SZ", "name": "平安银行"},
                             {"code": "601318.SH", "name": "中国平安"}]}

    resolver = build_slot_resolvers(None, [Tool()])["stock_agent"]
    ambiguous = await resolver({"scenario": "equity_research", "symbols": ["平安"]})
    assert ambiguous["missing"] == ["symbols"]
    assert {o["id"] for o in ambiguous["options"]} == {"000001.SZ", "601318.SH"}
    canonical = await resolver({"scenario": "equity_research", "symbols": ["600519.SH"]})
    assert canonical["symbols"] == ["600519.SH"]


@pytest.mark.asyncio
async def test_product_resolver_maps_catalogue_names_without_fabricating_codes():
    from app.agents.runtime import build_slot_resolvers

    class Library:
        def query_by_code(self, value):
            return {"basic_info": {"code": value}} if value == "110011" else None

        def list_products(self, kind):
            return [{"code": "110011", "name": "易方达产品A"}, {"code": "110012", "name": "易方达产品B"}]

    resolver = build_slot_resolvers(Library(), [])["product_agent"]
    unique = await resolver({"operation": "query", "symbols": ["易方达产品A"]})
    assert unique["codes"] == ["110011"]
    ambiguous = await resolver({"operation": "query", "symbols": ["易方达"]})
    assert ambiguous["missing"] == ["symbols"]
    unresolved = await resolver({"operation": "query", "symbols": ["不存在的基金"]})
    assert unresolved["missing"] == ["symbols"]
    assert unresolved["options"] == []
