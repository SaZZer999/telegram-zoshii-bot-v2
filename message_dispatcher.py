"""Message Dispatcher V1.

The first explicit, ordered routing layer for incoming Telegram text. Owns
NOTHING new — it only formalizes the priority order bot.py's webhook()
already used for a specific slice of its dispatch chain: navigation,
shopping/inventory menu buttons, and shopping_mode/inventory_mode text
dispatch (old Phase A2/A3/B). No pending state, no keyboards, no Gemini
prompts live here; everything it needs from the outside world is passed in
via a `DispatcherDeps` container built and owned by bot.py.

Deliberately NOT here (still bot.py-owned, unchanged): the shared confirm/
cancel button block, the whole Pending Preview Router if/elif chain
(reconciliation clarify, expense-delete selection, active-preview guards,
every clarification state, the Global Household Router, explicit/bare add,
undo, aliases, expenses, the saved-list router), cooking mode, general AI
fallback, and clear_interaction_state's own implementation (only called
through here as an injected callback).

No import of bot.py, database.py, Flask, Telegram, psycopg or any Gemini
SDK — legacy_shopping_flow.py/legacy_inventory_flow.py are safe to import
(they don't import bot.py either), and this module reuses their already
existing ShoppingFlowDeps/InventoryFlowDeps containers instead of
re-declaring every one of their fields here.
"""
from dataclasses import dataclass
from typing import Callable

import legacy_shopping_flow
import legacy_inventory_flow


@dataclass
class DispatcherDeps:
    """Injected callbacks/values — no import of bot.py, ever."""
    send_message: Callable
    clear_interaction_state: Callable
    main_keyboard: dict
    help_text: str
    shopping_deps: legacy_shopping_flow.ShoppingFlowDeps
    inventory_deps: legacy_inventory_flow.InventoryFlowDeps


def _dispatch_navigation(deps, chat_id, text):
    if text == "/start":
        deps.clear_interaction_state(chat_id)
        deps.send_message(
            chat_id,
            "Привіт! Я твій домашній помічник 🏠\n\n"
            "Обери дію на клавіатурі або напиши будь-яке запитання — я відповім за допомогою AI.",
            reply_markup=deps.main_keyboard,
        )
        return True

    if text == "/menu":
        deps.clear_interaction_state(chat_id)
        deps.send_message(chat_id, "Ось головне меню:", reply_markup=deps.main_keyboard)
        return True

    if text == "/help":
        deps.send_message(chat_id, deps.help_text)
        return True

    if text == "⬅️ Головне меню":
        deps.clear_interaction_state(chat_id)
        deps.send_message(chat_id, "Ось головне меню:", reply_markup=deps.main_keyboard)
        return True

    return False


def _dispatch_shopping_menu(deps, chat_id, user_id, display_name, text):
    if text == "🛒 Покупки":
        legacy_shopping_flow.handle_open_shopping_menu(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    if text == "➕ Додати товар":
        legacy_shopping_flow.handle_start_shopping_add(deps.shopping_deps, chat_id)
        return True

    if text == "📋 Показати список":
        legacy_shopping_flow.handle_show_shopping_list(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    if text == "✅ Позначити купленим":
        legacy_shopping_flow.handle_start_mark_bought(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    if text == "🗑️ Видалити товар":
        legacy_shopping_flow.handle_start_delete(deps.shopping_deps, chat_id, user_id, display_name)
        return True

    return False


def _dispatch_inventory_menu(deps, chat_id, user_id, display_name, text):
    if text == "🧊 Запаси":
        legacy_inventory_flow.handle_open_inventory_menu(deps.inventory_deps, chat_id, user_id, display_name)
        return True

    if text == "➕ Додати продукти":
        legacy_inventory_flow.handle_start_inventory_add(deps.inventory_deps, chat_id)
        return True

    if text == "📋 Показати запаси":
        legacy_inventory_flow.handle_show_inventory_list(deps.inventory_deps, chat_id, user_id, display_name)
        return True

    if text == "➖ Використати / прибрати":
        legacy_inventory_flow.handle_start_inventory_remove(deps.inventory_deps, chat_id, user_id, display_name)
        return True

    return False


def dispatch(deps, chat_id, user_id, display_name, text):
    """Route contract: True = fully handled (webhook returns "ok"
    immediately); False = not recognized by Dispatcher V1 (webhook falls
    through, unchanged, to every remaining bot.py-owned branch — the
    aliases/expenses/cooking-mode buttons still living inline in bot.py,
    then the whole Pending Preview Router chain, then general AI-chat).

    Exact internal order — mirrors old Phase A2/A3/B: navigation, shopping
    menu, inventory menu, shopping_mode text, inventory_mode text.
    Navigation is checked first so /start, /menu, /help and "⬅️ Головне
    меню" always work even while a shopping_mode/inventory_mode is active.
    If shopping_mode and inventory_mode were ever both active for the same
    chat_id, shopping_mode wins — same as before this module existed.
    """
    if _dispatch_navigation(deps, chat_id, text):
        return True

    if _dispatch_shopping_menu(deps, chat_id, user_id, display_name, text):
        return True

    if _dispatch_inventory_menu(deps, chat_id, user_id, display_name, text):
        return True

    if legacy_shopping_flow.handle_shopping_mode_text(deps.shopping_deps, chat_id, user_id, display_name, text):
        return True

    if legacy_inventory_flow.handle_inventory_mode_text(deps.inventory_deps, chat_id, user_id, display_name, text):
        return True

    return False
