from .chat import cooking_router as cooking_chat_router
from .chat import router as chat_router
from .collections import router as collections_router
from .extract import router as extract_router
from .grocery import router as grocery_router
from .health import router as health_router
from .meal_plans import router as meal_plans_router
from .recipes import router as recipes_router
from .tts import router as tts_router
from .users import router as users_router

__all__ = [
    "chat_router",
    "collections_router",
    "cooking_chat_router",
    "extract_router",
    "grocery_router",
    "health_router",
    "meal_plans_router",
    "recipes_router",
    "tts_router",
    "users_router",
]
