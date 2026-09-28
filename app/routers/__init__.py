"""汇总已上线的业务路由。"""

from app.routers.admin import router as admin_router
from app.routers.agent import router as agent_router
from app.routers.allocation import router as allocation_router
from app.routers.portfolio import router as portfolio_router
from app.routers.product import router as product_router
from app.routers.user import router as user_router

routers = (
    user_router,
    product_router,
    portfolio_router,
    allocation_router,
    admin_router,
    agent_router,
)

__all__ = [
    "admin_router",
    "agent_router",
    "allocation_router",
    "portfolio_router",
    "product_router",
    "routers",
    "user_router",
]
