from .recipes import router as recipes_router
from .health import router as health_router
from .extract import router as extract_router
from .grocery import router as grocery_router
from .chat import router as chat_router

__all__ = ["recipes_router", "health_router", "extract_router", "grocery_router", "chat_router"]

