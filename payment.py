
import logging
import sqlite3
import time
import os # Added import
import shutil # Added import
import asyncio
import uuid # For generating unique order IDs
from decimal import Decimal, ROUND_UP, ROUND_DOWN # Use Decimal for precision
import json # For parsing potential error messages
from datetime import datetime, timezone, timedelta # Added import
from collections import Counter, defaultdict # Added import

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram import helpers
import telegram.error as telegram_error
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaAnimation # Import InputMedia types
# -------------------------

# Import necessary items from utils and user
from utils import ( # Ensure utils imports are correct
    send_message_with_retry, format_currency, ADMIN_ID,
    LANGUAGES, load_all_data, BASKET_TIMEOUT, MIN_DEPOSIT_EUR,
    get_db_connection, MEDIA_DIR, PRODUCT_TYPES, DEFAULT_PRODUCT_EMOJI, # Added PRODUCT_TYPES/Emoji
    clear_expired_basket, # Added import
    _get_lang_data, # <--- *** ADDED IMPORT HERE ***
    log_admin_action, # <<< IMPORT log_admin_action >>>
    get_first_primary_admin_id, # Admin helper function for notifications
    # NEW: Import rate-limited media functions for 100% delivery
    send_media_with_retry, send_media_group_with_retry,
    SOLANA_ADMIN_WALLET, # Import Solana configuration
    add_pending_deposit  # For storing basket snapshot with payment
)

# Import Solana payment functions
from payment_solana import create_solana_payment
# <<< IMPORT USER MODULE >>>
import user

# --- Import Reseller Helper ---
try:
    from reseller_management import get_reseller_discount, get_reseller_discount_with_connection
except ImportError:
    logger_dummy_reseller_payment = logging.getLogger(__name__ + "_dummy_reseller_payment")
    logger_dummy_reseller_payment.error("Could not import get_reseller_discount from reseller_management.py. Reseller discounts will not work in payment processing.")
    # Define dummy functions that always return zero discount
    def get_reseller_discount(user_id: int, product_type: str) -> Decimal:
        return Decimal('0.0')
    
    async def get_reseller_discount_with_connection(cursor, user_id: int, product_type: str) -> Decimal:
        return Decimal('0.0')
# -----------------------------

# --- Import Unreserve Helper ---
# Assume _unreserve_basket_items is defined elsewhere (e.g., user.py or utils.py)
try:
    from user import _unreserve_basket_items
except ImportError:
    # Fallback: Try importing from utils
    try:
        from utils import _unreserve_basket_items
    except ImportError:
        logger_unreserve_import_error = logging.getLogger(__name__)
        logger_unreserve_import_error.error("Could not import _unreserve_basket_items helper function from user.py or utils.py! Un-reserving on failure might not work.")
        # Define a dummy function to avoid crashes, but log loudly
        def _unreserve_basket_items(basket_snapshot: list | None):
            logger_unreserve_import_error.critical("CRITICAL: _unreserve_basket_items function is missing! Cannot un-reserve items on payment failure.")
# ----------------------------------

logger = logging.getLogger(__name__)

# --- Solana Only Payment System ---
# All crypto payments are now processed via Solana (SOL) only.
# See payment_solana.py for payment creation and monitoring.


# --- Callback Handler for Crypto Selection during Refill ---
async def handle_select_refill_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the user selecting SOL for refill, creates Solana payment."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not params:
        logger.warning(f"handle_select_refill_crypto called without asset parameter for user {user_id}")
        await query.answer("Error: Missing crypto choice.", show_alert=True)
        return

    selected_asset_code = params[0].lower()  # Should be 'sol'
    logger.info(f"User {user_id} selected {selected_asset_code} for refill.")

    refill_eur_amount_float = context.user_data.get('refill_eur_amount')
    if not refill_eur_amount_float or refill_eur_amount_float <= 0:
        logger.error(f"Refill amount context lost before asset selection for user {user_id}.")
        await query.edit_message_text("❌ Error: Refill amount context lost. Please start the top up again.", parse_mode=None)
        context.user_data.pop('state', None)
        return

    refill_eur_amount_decimal = Decimal(str(refill_eur_amount_float))

    preparing_invoice_msg = lang_data.get("preparing_invoice", "⏳ Preparing your Solana payment...")
    failed_invoice_creation_msg = lang_data.get("failed_invoice_creation", "❌ Failed to create payment. Please try again later or contact support.")
    back_to_profile_button = lang_data.get("back_profile_button", "Back to Profile")
    back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {back_to_profile_button}", callback_data="profile")]])

    try:
        await query.edit_message_text(preparing_invoice_msg, reply_markup=None, parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Couldn't edit message in handle_select_refill_crypto: {e}")
        await query.answer("Preparing...")

    # Create Solana payment - for refill
    order_id = f"USER{user_id}_REFILL_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    payment_result = await create_solana_payment(
        user_id=user_id,
        order_id=order_id,
        eur_amount=float(refill_eur_amount_decimal)
    )
    
    # Store pending deposit for tracking (refill - not a purchase)
    if 'error' not in payment_result:
        # Get bot_id to track which bot user is interacting with
        bot_id = str(context.bot.id) if context and hasattr(context, 'bot') and context.bot else None
        add_pending_deposit(
            payment_id=order_id,
            user_id=user_id,
            currency='sol',
            target_eur_amount=float(refill_eur_amount_decimal),
            expected_crypto_amount=float(payment_result.get('pay_amount', 0)),
            is_purchase=False,
            basket_snapshot=None,
            discount_code=None,
            bot_id=bot_id
        )

    if 'error' in payment_result:
        error_code = payment_result['error']
        logger.error(f"Failed to create Solana refill payment for user {user_id}: {error_code} - Details: {payment_result}")
        error_message_to_user = failed_invoice_creation_msg
        try:
            await query.edit_message_text(error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        except Exception as edit_e:
            logger.error(f"Failed to edit message with invoice creation error: {edit_e}")
            await send_message_with_retry(context.bot, chat_id, error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        context.user_data.pop('refill_eur_amount', None)
        context.user_data.pop('state', None)
    else:
        logger.info(f"Solana refill payment created successfully for user {user_id}. Order ID: {order_id}")
        context.user_data.pop('refill_eur_amount', None)
        context.user_data.pop('state', None)
        await display_solana_invoice(update, context, payment_result, is_purchase=False)


# --- Callback Handler for SOL Selection during Basket Payment ---
async def handle_select_basket_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles the user selecting SOL for direct basket payment."""
    query = update.callback_query
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not params:
        logger.warning(f"handle_select_basket_crypto called without asset parameter for user {user_id}")
        await query.answer("Error: Missing crypto choice.", show_alert=True)
        return

    selected_asset_code = params[0].lower()  # Should be 'sol'
    logger.info(f"User {user_id} selected {selected_asset_code} for basket payment.")

    # Retrieve stored basket context
    basket_snapshot = context.user_data.get('basket_pay_snapshot')
    final_total_eur_float = context.user_data.get('basket_pay_total_eur')
    discount_code_used = context.user_data.get('basket_pay_discount_code')

    if basket_snapshot is None or final_total_eur_float is None:
        logger.error(f"Basket payment context lost before crypto selection for user {user_id}.")
        await query.edit_message_text("❌ Error: Payment context lost. Please go back to your basket.",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ View Basket", callback_data="view_basket")]]),
                                       parse_mode=None)
        context.user_data.pop('state', None)
        context.user_data.pop('basket_pay_snapshot', None)
        context.user_data.pop('basket_pay_total_eur', None)
        context.user_data.pop('basket_pay_discount_code', None)
        return

    final_total_eur_decimal = Decimal(str(final_total_eur_float))

    # Get language strings
    preparing_invoice_msg = lang_data.get("preparing_invoice", "⏳ Preparing your Solana payment...")
    failed_invoice_creation_msg = lang_data.get("failed_invoice_creation", "❌ Failed to create payment. Please try again later or contact support.")
    back_to_basket_button = lang_data.get("back_basket_button", "Back to Basket")
    back_button_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"⬅️ {back_to_basket_button}", callback_data="view_basket")]])

    try:
        await query.edit_message_text(preparing_invoice_msg, reply_markup=None, parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Couldn't edit message in handle_select_basket_crypto: {e}")
        await query.answer("Preparing...")

    # Create Solana payment - for basket purchase
    order_id = f"USER{user_id}_PURCHASE_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    payment_result = await create_solana_payment(
        user_id=user_id,
        order_id=order_id,
        eur_amount=float(final_total_eur_decimal)
    )
    
    # Store pending deposit with basket info for tracking
    if 'error' not in payment_result:
        # Get bot_id to track which bot user is interacting with
        bot_id = str(context.bot.id) if context and hasattr(context, 'bot') and context.bot else None
        add_pending_deposit(
            payment_id=order_id,
            user_id=user_id,
            currency='sol',
            target_eur_amount=float(final_total_eur_decimal),
            expected_crypto_amount=float(payment_result.get('pay_amount', 0)),
            is_purchase=True,
            basket_snapshot=basket_snapshot,
            discount_code=discount_code_used,
            bot_id=bot_id
        )

    # Store snapshot temporarily BEFORE clearing context, in case we need it for un-reserving
    snapshot_before_clear = context.user_data.get('basket_pay_snapshot')

    # Clear reservation tracking since user proceeded to invoice creation
    from utils import clear_reservation_tracking
    clear_reservation_tracking(user_id)

    # Clear context *after* attempting payment creation
    context.user_data.pop('basket_pay_snapshot', None)
    context.user_data.pop('basket_pay_total_eur', None)
    context.user_data.pop('basket_pay_discount_code', None)
    context.user_data.pop('state', None)

    if 'error' in payment_result:
        error_code = payment_result['error']
        logger.error(f"Failed to create Solana basket payment for user {user_id}: {error_code} - Details: {payment_result}")

        # Un-reserve items if payment creation failed
        logger.info(f"Payment creation failed ({error_code}). Un-reserving items from snapshot.")
        try:
            await asyncio.to_thread(_unreserve_basket_items, snapshot_before_clear)
        except NameError:
            logger.critical("CRITICAL: _unreserve_basket_items function call failed due to NameError!")
        except Exception as unreserve_e:
            logger.error(f"Error occurred during item un-reservation: {unreserve_e}")

        error_message_to_user = failed_invoice_creation_msg
        try:
            await query.edit_message_text(error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
        except Exception as edit_e:
            logger.error(f"Failed to edit message with basket payment creation error: {edit_e}")
            await send_message_with_retry(context.bot, chat_id, error_message_to_user, reply_markup=back_button_markup, parse_mode=None)
    else:
        logger.info(f"Solana basket payment created successfully for user {user_id}. Order ID: {order_id}")
        await display_solana_invoice(update, context, payment_result, is_purchase=True)
        # Important: DO NOT clear the user's actual basket here.


# --- Display Solana Invoice ---
async def display_solana_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, payment_data: dict, is_purchase: bool = False, query=None):
    """Displays the Solana payment invoice details. Can handle both callback queries and text messages."""
    # Use provided query or get from update
    if query is None:
        query = update.callback_query
    
    # Get chat_id from query or update
    if query and query.message:
        chat_id = query.message.chat_id
    else:
        chat_id = update.effective_chat.id
    
    lang = context.user_data.get("lang", "en") if context.user_data else "en"
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    try:
        pay_address = payment_data.get('pay_address')
        pay_amount = payment_data.get('pay_amount')
        pay_currency = payment_data.get('pay_currency', 'SOL').upper()
        payment_id = payment_data.get('payment_id')
        exchange_rate = payment_data.get('exchange_rate')

        if not pay_address or not pay_amount or not payment_id:
            logger.error(f"Missing critical data in Solana payment response: {payment_data}")
            raise ValueError("Missing payment address, amount, or ID")

        # Convert pay_amount to float if it's a string
        pay_amount_float = float(pay_amount) if isinstance(pay_amount, str) else pay_amount
        
        # Format amounts
        pay_amount_display = f"{pay_amount_float:.6f}"
        eur_equivalent = pay_amount_float * exchange_rate if exchange_rate else 0
        eur_display = format_currency(eur_equivalent)

        # Get language strings
        if is_purchase:
            invoice_title = lang_data.get("invoice_title_purchase", "*Payment Invoice Created*")
        else:
            invoice_title = lang_data.get("invoice_title_refill", "*Top-Up Invoice Created*")
        
        amount_label = lang_data.get("amount_label", "*Amount:*")
        payment_address_label = lang_data.get("payment_address_label", "*Payment Address:*")
        
        # Translated important notice
        important_notice = lang_data.get("invoice_important_notice", "⚠️ *Important:* Send the exact amount to this address.")
        confirmation_notice = lang_data.get("invoice_confirmation_notice", "✅ Auto-confirmed in ~1-2 min.")
        valid_notice = lang_data.get("invoice_valid_notice", "⏱️ *Valid for 30 minutes*")
        
        # Build message (removed Payment ID line)
        message_parts = [
            invoice_title,
            "",
            f"{amount_label} `{pay_amount_display}` {pay_currency} (~{eur_display})",
            "",
            f"{payment_address_label}",
            f"`{pay_address}`",
            "",
            important_notice,
            confirmation_notice,
            valid_notice
        ]
        
        final_msg = "\n".join(message_parts)
        
        # Create keyboard
        back_button_text = lang_data.get("back_profile_button", "Back to Profile") if not is_purchase else lang_data.get("back_basket_button", "Back to Basket")
        back_callback = "profile" if not is_purchase else "view_basket"
        
        keyboard = [[InlineKeyboardButton(f"⬅️ {back_button_text}", callback_data=back_callback)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Send message - handle both callback queries and direct messages
        if query:
            try:
                await query.edit_message_text(final_msg, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            except telegram_error.BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    logger.warning(f"Could not edit message for Solana invoice: {e}")
                    await send_message_with_retry(context.bot, chat_id, final_msg, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
                await query.answer()
        else:
            # No callback query - send as new message
            await send_message_with_retry(context.bot, chat_id, final_msg, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        
        user_id_for_log = query.from_user.id if query else update.effective_user.id
        logger.info(f"Displayed Solana invoice for user {user_id_for_log}, Payment ID: {payment_id}")
        
    except Exception as e:
        logger.error(f"Error displaying Solana invoice: {e}", exc_info=True)
        error_msg = lang_data.get("error_displaying_invoice", "\u274C Error displaying payment invoice. Please contact support.")
        if query:
            try:
                await query.edit_message_text(error_msg, parse_mode=None)
            except Exception:
                await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)
        else:
            await send_message_with_retry(context.bot, chat_id, error_msg, parse_mode=None)


# --- Process Successful Refill ---
async def process_successful_refill(user_id: int, amount_to_add_eur: Decimal, payment_id: str, context: ContextTypes.DEFAULT_TYPE, bot_id: str | None = None) -> bool:
    # Get bot - use specific bot_id if provided and context is None
    bot = None
    if context is not None and hasattr(context, 'bot'):
        bot = context.bot
    elif bot_id:
        try:
            import main
            if hasattr(main, 'telegram_apps') and main.telegram_apps and bot_id in main.telegram_apps:
                bot = main.telegram_apps[bot_id].bot
                logger.info(f"Using bot {bot_id} for refill confirmation to user {user_id}")
            elif hasattr(main, 'telegram_apps') and main.telegram_apps:
                for b_id, app in main.telegram_apps.items():
                    bot = app.bot
                    break
        except Exception as e:
            logger.warning(f"Could not get bot instance for refill: {e}")
    
    user_lang = 'en'
    conn_lang = None
    try:
        conn_lang = get_db_connection()
        c_lang = conn_lang.cursor()
        c_lang.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
        lang_res = c_lang.fetchone()
        if lang_res and lang_res['language'] in LANGUAGES:
            user_lang = lang_res['language']
    except sqlite3.Error as e:
        logger.error(f"DB error fetching language for user {user_id} during refill confirmation: {e}")
    finally:
        if conn_lang: conn_lang.close()

    lang_data = LANGUAGES.get(user_lang, LANGUAGES['en'])

    if not isinstance(amount_to_add_eur, Decimal) or amount_to_add_eur <= Decimal('0.0'):
        logger.error(f"Invalid amount_to_add_eur in process_successful_refill: {amount_to_add_eur}")
        return False

    # Use the separate crediting function
    return await credit_user_balance(user_id, amount_to_add_eur, f"Refill payment {payment_id}", context, bot=bot)


# --- HELPER: Finalize Purchase (Send Caption Separately) ---
async def _finalize_purchase(user_id: int, basket_snapshot: list, discount_code_used: str | None, context: ContextTypes.DEFAULT_TYPE, bot=None) -> bool:
    """
    Shared logic to finalize a purchase after payment confirmation (balance or crypto).
    Decrements stock, adds purchase record, sends media first, then text separately,
    cleans up product records.
    """
    # Handle None context (from background job)
    if context is not None:
        chat_id = getattr(context, '_chat_id', None) or getattr(context, '_user_id', None) or user_id
        bot = bot or (context.bot if hasattr(context, 'bot') else None)
        lang, lang_data = _get_lang_data(context)
    else:
        chat_id = user_id
        # Get user language from database
        try:
            conn_lang = get_db_connection()
            c_lang = conn_lang.cursor()
            c_lang.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
            row = c_lang.fetchone()
            lang = row['language'] if row and row['language'] else "en"
            conn_lang.close()
        except Exception as e:
            logger.warning(f"Could not fetch user language for {user_id}: {e}")
            lang = "en"
        lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
        
        # Get bot if not provided
        if bot is None:
            try:
                import main
                if hasattr(main, 'telegram_app') and main.telegram_app:
                    bot = main.telegram_app.bot
                elif hasattr(main, 'telegram_apps') and main.telegram_apps:
                    for bot_id, app in main.telegram_apps.items():
                        bot = app.bot
                        break
            except Exception as e:
                logger.warning(f"Could not get bot instance in _finalize_purchase: {e}")
    
    if not chat_id:
         logger.error(f"Cannot determine chat_id for user {user_id} in _finalize_purchase")
    if not basket_snapshot: logger.error(f"Empty basket_snapshot for user {user_id} purchase finalization."); return False

    conn = None
    processed_product_ids = []
    purchases_to_insert = []
    final_pickup_details = defaultdict(list)
    db_update_successful = False
    total_price_paid_decimal = Decimal('0.0')

    # --- Database Operations (Reservation Decrement, Purchase Record) ---
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Use IMMEDIATE lock to reduce lock conflicts while still preventing race conditions
        logger.info(f"🔄 Starting purchase finalization for user {user_id} with {len(basket_snapshot)} items")
        c.execute("BEGIN IMMEDIATE")
        purchase_time_iso = datetime.now(timezone.utc).isoformat()

        # Pre-validate all products before processing
        product_ids = [item['product_id'] for item in basket_snapshot]
        placeholders = ','.join('?' * len(product_ids))
        c.execute(f"""
            SELECT id, available, reserved FROM products 
            WHERE id IN ({placeholders})
        """, product_ids)
        available_products = {row['id']: {'available': row['available'], 'reserved': row['reserved']} for row in c.fetchall()}
        
        # Check if all products are still available
        for item_snapshot in basket_snapshot:
            product_id = item_snapshot['product_id']
            if product_id not in available_products:
                logger.error(f"Product {product_id} no longer exists for user {user_id}")
                conn.rollback()
                return False
            
            available = available_products[product_id]['available']
            if available <= 0:
                logger.error(f"Product {product_id} no longer available for user {user_id}")
                conn.rollback()
                return False

        for item_snapshot in basket_snapshot: # Iterate directly over the rich snapshot
            product_id = item_snapshot['product_id']
            
            # Decrement stock with better error handling
            avail_update = c.execute("UPDATE products SET available = available - 1 WHERE id = ? AND available > 0", (product_id,))
            
            if avail_update.rowcount == 0:
                logger.error(f"Failed to decrement stock for product {product_id} for user {user_id}")
                conn.rollback()
                return False

            # Product stock successfully decremented. Proceed to record purchase using snapshot data.
            # Details from snapshot:
            item_original_price_decimal = Decimal(str(item_snapshot['price'])) # 'price' in snapshot is original price
            item_product_type = item_snapshot['product_type']
            item_name = item_snapshot['name']
            item_size = item_snapshot['size']
            item_city = item_snapshot['city'] 
            item_district = item_snapshot['district'] 
            item_original_text_pickup = item_snapshot.get('original_text')

            # BULLETPROOF: Calculate reseller discount with comprehensive error handling
            # Use existing connection to avoid database locks
            item_reseller_discount_percent = Decimal('0')
            item_reseller_discount_amount = Decimal('0')
            item_price_paid_decimal = item_original_price_decimal
            
            try:
                logger.info(f"🔄 BULLETPROOF: Calculating reseller discount for user {user_id}, product {item_product_type}")
                item_reseller_discount_percent = await get_reseller_discount_with_connection(c, user_id, item_product_type)
                item_reseller_discount_amount = (item_original_price_decimal * item_reseller_discount_percent / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
                item_price_paid_decimal = item_original_price_decimal - item_reseller_discount_amount
                logger.info(f"✅ BULLETPROOF: Reseller discount calculated: {item_reseller_discount_percent}% = {item_reseller_discount_amount} EUR")
            except Exception as reseller_error:
                logger.warning(f"⚠️ BULLETPROOF: Error calculating reseller discount for user {user_id}, product {item_product_type}: {reseller_error}. Using full price.")
                # Fallback to original price - payment will still succeed
                item_reseller_discount_percent = Decimal('0')
                item_reseller_discount_amount = Decimal('0')
                item_price_paid_decimal = item_original_price_decimal
            total_price_paid_decimal += item_price_paid_decimal
            item_price_paid_float = float(item_price_paid_decimal)

            purchases_to_insert.append((
                user_id, product_id, item_name, item_product_type, item_size,
                item_price_paid_float, item_city, item_district, purchase_time_iso
            ))
            processed_product_ids.append(product_id)
            # For pickup details message, use snapshot's original_text and other details
            final_pickup_details[product_id].append({'name': item_name, 'size': item_size, 'text': item_original_text_pickup, 'type': item_product_type}) # Store type for emoji

        if not purchases_to_insert:
            logger.warning(f"No items processed during finalization for user {user_id}. Rolling back.")
            conn.rollback()
            if chat_id and bot: await send_message_with_retry(bot, chat_id, lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase."), parse_mode=None)
            return False

        c.executemany("INSERT INTO purchases (user_id, product_id, product_name, product_type, product_size, price_paid, city, district, purchase_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", purchases_to_insert)
        c.execute("UPDATE users SET total_purchases = total_purchases + ? WHERE user_id = ?", (len(purchases_to_insert), user_id))
        if discount_code_used:
            # Atomically increment discount code usage only if limit not exceeded
            # This prevents race conditions where multiple users use the same code simultaneously
            update_result = c.execute("""
                UPDATE discount_codes 
                SET uses_count = uses_count + 1 
                WHERE code = ? AND (max_uses IS NULL OR uses_count < max_uses)
            """, (discount_code_used,))
            
            if update_result.rowcount == 0:
                # Check why the update failed
                c.execute("SELECT uses_count, max_uses FROM discount_codes WHERE code = ?", (discount_code_used,))
                code_check = c.fetchone()
                if code_check:
                    if code_check['max_uses'] is not None and code_check['uses_count'] >= code_check['max_uses']:
                        logger.warning(f"Discount code '{discount_code_used}' usage limit exceeded during payment finalization for user {user_id}. Current uses: {code_check['uses_count']}, Max: {code_check['max_uses']}. Purchase completed but usage not incremented.")
                    else:
                        logger.error(f"Unexpected: Failed to increment discount code '{discount_code_used}' for user {user_id}, but code exists with uses: {code_check['uses_count']}, max: {code_check['max_uses']}")
                else:
                    logger.warning(f"Discount code '{discount_code_used}' not found in database during payment finalization for user {user_id}")
            else:
                logger.info(f"Successfully incremented usage count for discount code '{discount_code_used}' for user {user_id}")
        c.execute("UPDATE users SET basket = '' WHERE user_id = ?", (user_id,))
        conn.commit()
        db_update_successful = True
        logger.info(f"Finalized purchase DB update user {user_id}. Processed {len(purchases_to_insert)} items. General Discount: {discount_code_used or 'None'}. Total Paid (after reseller disc): {total_price_paid_decimal:.2f} EUR")

    except sqlite3.Error as e:
        logger.error(f"DB error during purchase finalization user {user_id}: {e}", exc_info=True); db_update_successful = False
        if conn and conn.in_transaction: conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error during purchase finalization user {user_id}: {e}", exc_info=True); db_update_successful = False
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()

    # --- Post-Transaction Cleanup & Message Sending (If DB success) ---
    if db_update_successful:
        # Clear basket from context if available
        if context is not None and hasattr(context, 'user_data') and context.user_data is not None:
            context.user_data['basket'] = []
            context.user_data.pop('applied_discount', None)

        # Fetch Media BEFORE attempting delivery
        media_details = defaultdict(list)
        if processed_product_ids:
            conn_media = None
            try:
                conn_media = get_db_connection()
                c_media = conn_media.cursor()
                media_placeholders = ','.join('?' * len(processed_product_ids))
                c_media.execute(f"SELECT product_id, media_type, telegram_file_id, file_path FROM product_media WHERE product_id IN ({media_placeholders})", processed_product_ids)
                media_rows = c_media.fetchall()
                logger.info(f"Fetched {len(media_rows)} media records for products {processed_product_ids} for user {user_id}")
                for row in media_rows: 
                    media_details[row['product_id']].append(dict(row))
                    logger.debug(f"Media for P{row['product_id']}: {row['media_type']} - FileID: {'Yes' if row['telegram_file_id'] else 'No'}, Path: {row['file_path']}")
            except sqlite3.Error as e: 
                logger.error(f"DB error fetching media post-purchase: {e}")
            finally:
                if conn_media: conn_media.close()

        # CRITICAL: Attempt media delivery and track success
        media_delivery_successful = True
        if chat_id:
            try:
                success_title = lang_data.get("purchase_success", "🎉 Purchase Complete! Pickup details below:")
                await send_message_with_retry(bot, chat_id, success_title, parse_mode=None)

                for prod_id in processed_product_ids:
                    item_details_list = final_pickup_details.get(prod_id)
                    if not item_details_list: continue
                    item_details = item_details_list[0] # First (and likely only) entry for this prod_id
                    item_name, item_size = item_details['name'], item_details['size']
                    item_original_text = item_details['text'] or "(No specific pickup details provided)"
                    product_type = item_details['type'] # <<< USE TYPE FROM SNAPSHOT DATA
                    product_emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
                    item_header = f"--- Item: {product_emoji} {item_name} {item_size} ---"

                    # Prepare combined text caption
                    combined_caption = f"{item_header}\n\n{item_original_text}"
                    if len(combined_caption) > 4090: combined_caption = combined_caption[:4090] + "..." # Adjust for send_message limit

                    media_items_for_product = media_details.get(prod_id, [])
                    photo_video_group_details = []
                    animations_to_send_details = []
                    opened_files = []

                    logger.info(f"Processing media for P{prod_id} user {user_id}: Found {len(media_items_for_product)} media items")

                    # --- Separate Media ---
                    for media_item in media_items_for_product:
                        media_type = media_item.get('media_type')
                        file_id = media_item.get('telegram_file_id')
                        file_path = media_item.get('file_path')
                        logger.debug(f"Processing media item P{prod_id}: Type={media_type}, FileID={'Yes' if file_id else 'No'}, Path={file_path}")
                        if media_type in ['photo', 'video']:
                            photo_video_group_details.append({'type': media_type, 'id': file_id, 'path': file_path})
                        elif media_type == 'gif':
                            animations_to_send_details.append({'type': media_type, 'id': file_id, 'path': file_path})
                        else:
                            logger.warning(f"Unsupported media type '{media_type}' found for P{prod_id}")

                    logger.info(f"Media separation P{prod_id}: {len(photo_video_group_details)} photos/videos, {len(animations_to_send_details)} animations")

                    # --- Send Photos/Videos Group (No Caption) ---
                    if photo_video_group_details:
                        media_group_input = []
                        files_for_this_group = []
                        logger.info(f"Attempting to send {len(photo_video_group_details)} photos/videos for P{prod_id} user {user_id}")
                        
                        # Validate that we don't exceed Telegram's media group limit (10 items)
                        if len(photo_video_group_details) > 10:
                            logger.warning(f"Media group for P{prod_id} has {len(photo_video_group_details)} items, which exceeds Telegram's 10-item limit. Will send in batches.")
                            photo_video_group_details = photo_video_group_details[:10]  # Take only first 10 items
                        
                        try:
                            for item in photo_video_group_details:
                                input_media = None; file_handle = None
                                
                                # Skip file_id completely and go straight to local files for now
                                # This avoids the "wrong file identifier" error entirely
                                logger.debug(f"Using local file for P{prod_id} (skipping file_id due to token change)")
                                
                                # Use file path directly
                                if item['path'] and await asyncio.to_thread(os.path.exists, item['path']):
                                    logger.debug(f"Using file path for P{prod_id}: {item['path']}")
                                    file_handle = await asyncio.to_thread(open, item['path'], 'rb')
                                    opened_files.append(file_handle)
                                    files_for_this_group.append(file_handle)
                                    if item['type'] == 'photo': input_media = InputMediaPhoto(media=file_handle)
                                    elif item['type'] == 'video': input_media = InputMediaVideo(media=file_handle)
                                else:
                                    logger.warning(f"No valid media source for P{prod_id}: Path exists={await asyncio.to_thread(os.path.exists, item['path']) if item['path'] else False}")
                                    
                                if input_media: 
                                    media_group_input.append(input_media)
                                    logger.debug(f"Added media to group for P{prod_id}: {item['type']}")
                                else: 
                                    logger.warning(f"Could not prepare photo/video InputMedia P{prod_id}: {item}")

                            if media_group_input:
                                logger.info(f"Sending media group with {len(media_group_input)} items for P{prod_id} user {user_id}")
                                try:
                                    # Use rate-limited function for 100% delivery guarantee
                                    result = await send_media_group_with_retry(bot, chat_id, media=media_group_input)
                                    if result:
                                        logger.info(f"✅ Successfully sent photo/video group ({len(media_group_input)}) for P{prod_id} user {user_id}")
                                    else:
                                        logger.error(f"❌ Failed to send media group for P{prod_id} user {user_id} after all retries")
                                        raise Exception(f"Media group delivery failed after all retries for P{prod_id}")
                                except Exception as send_error:
                                    # If sending fails due to invalid file IDs, try to rebuild with file paths only
                                    error_message = str(send_error)
                                    logger.warning(f"⚠️ Media group send failed for P{prod_id}: {error_message}")
                                    
                                    if "wrong file identifier" in error_message.lower():
                                        logger.warning(f"Attempting fallback with file paths only for P{prod_id}...")
                                        
                                        # Rebuild media group using only file paths
                                        fallback_media_group = []
                                        fallback_files = []
                                        for item in photo_video_group_details:
                                            if item['path'] and await asyncio.to_thread(os.path.exists, item['path']):
                                                try:
                                                    fallback_file_handle = await asyncio.to_thread(open, item['path'], 'rb')
                                                    fallback_files.append(fallback_file_handle)
                                                    if item['type'] == 'photo': 
                                                        fallback_media_group.append(InputMediaPhoto(media=fallback_file_handle))
                                                    elif item['type'] == 'video': 
                                                        fallback_media_group.append(InputMediaVideo(media=fallback_file_handle))
                                                except Exception as fallback_error:
                                                    logger.error(f"Error preparing fallback media for P{prod_id}: {fallback_error}")
                                        
                                        if fallback_media_group:
                                            try:
                                                # Use rate-limited function for fallback too
                                                fallback_result = await send_media_group_with_retry(bot, chat_id, media=fallback_media_group)
                                                if fallback_result:
                                                    logger.info(f"✅ Successfully sent fallback media group for P{prod_id} user {user_id}")
                                                else:
                                                    raise Exception(f"Fallback media group also failed for P{prod_id}")
                                            except Exception as fallback_send_error:
                                                logger.error(f"❌ Fallback media group send also failed for P{prod_id}: {fallback_send_error}")
                                            finally:
                                                # Close fallback files
                                                for f in fallback_files:
                                                    try:
                                                        if not f.closed: await asyncio.to_thread(f.close)
                                                    except Exception: pass
                                        else:
                                            logger.error(f"❌ No fallback media available for P{prod_id}")
                                    else:
                                        logger.error(f"❌ Media group send failed for P{prod_id} (non-file-ID error): {error_message}")
                            else:
                                logger.warning(f"No media items prepared for sending P{prod_id} user {user_id}")
                        except Exception as group_e:
                            logger.error(f"❌ Error sending photo/video group P{prod_id} user {user_id}: {group_e}", exc_info=True)
                        finally:
                            for f in files_for_this_group:
                             try:
                                 if not f.closed: await asyncio.to_thread(f.close); opened_files.remove(f)
                             except Exception: pass

                    # --- Send Animations (GIFs) Separately (No Caption) ---
                    if animations_to_send_details:
                        logger.info(f"Attempting to send {len(animations_to_send_details)} animations for P{prod_id} user {user_id}")
                        for item in animations_to_send_details:
                            anim_file_handle = None
                            try:
                                # Skip file_id completely and go straight to local files for now
                                # This avoids the "wrong file identifier" error entirely
                                logger.debug(f"Using local file for animation P{prod_id} (skipping file_id due to token change)")
                                media_to_send_ref = None
                                
                                # Use file path directly
                                if item['path'] and await asyncio.to_thread(os.path.exists, item['path']):
                                    logger.debug(f"Using file path for animation P{prod_id}: {item['path']}")
                                    anim_file_handle = await asyncio.to_thread(open, item['path'], 'rb')
                                    opened_files.append(anim_file_handle)
                                    media_to_send_ref = anim_file_handle
                                    # Use rate-limited function for animations
                                    anim_result = await send_media_with_retry(bot, chat_id, media=media_to_send_ref, media_type='animation')
                                    if anim_result:
                                        logger.info(f"✅ Successfully sent animation with file path for P{prod_id} user {user_id}")
                                    else:
                                        logger.error(f"❌ Failed to send animation for P{prod_id} user {user_id} after all retries")
                                        raise Exception(f"Animation delivery failed for P{prod_id}")
                                else:
                                    logger.warning(f"Could not find GIF source for P{prod_id}: Path exists={await asyncio.to_thread(os.path.exists, item['path']) if item['path'] else False}")
                                    continue
                            except Exception as anim_e:
                                logger.error(f"❌ Error sending animation P{prod_id} user {user_id}: {anim_e}", exc_info=True)
                            finally:
                                if anim_file_handle and anim_file_handle in opened_files:
                                    try: await asyncio.to_thread(anim_file_handle.close); opened_files.remove(anim_file_handle)
                                    except Exception: pass

                    # --- Always Send Combined Text Separately ---
                    if combined_caption:
                        logger.debug(f"Sending text details for P{prod_id} user {user_id}: {len(combined_caption)} characters")
                        await send_message_with_retry(bot, chat_id, combined_caption, parse_mode=None)
                        logger.info(f"✅ Successfully sent text details for P{prod_id} user {user_id}")
                    else:
                        # Create a fallback message if both original text and header are missing somehow
                        fallback_text = f"(No details provided for Product ID {prod_id})"
                        await send_message_with_retry(bot, chat_id, fallback_text, parse_mode=None)
                        logger.warning(f"No combined caption text to send for P{prod_id} user {user_id}. Sent fallback.")

                    # --- Close any remaining opened file handles ---
                    for f in opened_files:
                        try:
                            if not f.closed: await asyncio.to_thread(f.close)
                        except Exception as close_e: logger.warning(f"Error closing file handle during final cleanup: {close_e}")

                # --- Final Message to User ---
                leave_review_button = lang_data.get("leave_review_button", "Leave a Review")
                keyboard = [[InlineKeyboardButton(f"✍️ {leave_review_button}", callback_data="leave_review_now")]]
                await send_message_with_retry(bot, chat_id, "Thank you for your purchase!", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
                
            except Exception as media_error:
                logger.critical(f"🚨 CRITICAL: Media delivery failed for user {user_id} after successful payment! Error: {media_error}")
                media_delivery_successful = False
                
                # Notify admin immediately with details
                if get_first_primary_admin_id():
                    admin_msg = f"🚨 URGENT: Media delivery FAILED for user {user_id}\n"
                    admin_msg += f"Payment successful but products not delivered!\n"
                    admin_msg += f"Products: {', '.join([str(pid) for pid in processed_product_ids])}\n"
                    admin_msg += f"Error: {str(media_error)[:200]}\n"
                    admin_msg += f"Action needed: Manual product delivery required!"
                    try:
                        await send_message_with_retry(bot, get_first_primary_admin_id(), admin_msg, parse_mode=None)
                    except Exception as admin_notify_error:
                        logger.error(f"Failed to notify admin about media delivery failure: {admin_notify_error}")
                
                # Send detailed message to user with their purchase info
                user_msg = f"⚠️ PAYMENT SUCCESSFUL - DELIVERY ISSUE\n\n"
                user_msg += f"Your payment was processed successfully, but we encountered a technical issue delivering your products.\n\n"
                user_msg += f"✅ Payment confirmed\n"
                user_msg += f"📦 Products purchased: {len(processed_product_ids)}\n"
                user_msg += f"⚠️ Delivery status: PENDING\n\n"
                user_msg += f"Our support team has been automatically notified and will deliver your products shortly.\n"
                user_msg += f"Please save this message for reference.\n\n"
                user_msg += f"If you don't receive your products within 30 minutes, please contact support."
                await send_message_with_retry(bot, chat_id, user_msg, parse_mode=None)

        # --- Product Record Deletion (ONLY IF MEDIA DELIVERY SUCCESSFUL) ---
        # CRITICAL FIX: Only delete products if media was successfully delivered
        # This allows admin to manually complete orders if media delivery fails
        if processed_product_ids and media_delivery_successful:
            conn_del = None
            try:
                conn_del = get_db_connection()
                c_del = conn_del.cursor()
                ids_tuple_list = [(pid,) for pid in processed_product_ids]
                logger.info(f"Purchase Finalization: Deleting product records after SUCCESSFUL media delivery for user {user_id}. IDs: {processed_product_ids}")
                
                # Delete product media records first
                media_delete_placeholders = ','.join('?' * len(processed_product_ids))
                c_del.execute(f"DELETE FROM product_media WHERE product_id IN ({media_delete_placeholders})", processed_product_ids)
                
                # Delete product records  
                delete_result = c_del.executemany("DELETE FROM products WHERE id = ?", ids_tuple_list)
                conn_del.commit()
                deleted_count = delete_result.rowcount
                logger.info(f"Deleted {deleted_count} purchased product records and their media records for user {user_id}. IDs: {processed_product_ids}")
                
                # Schedule media directory deletion AFTER successful delivery
                for prod_id in processed_product_ids:
                    media_dir_to_delete = os.path.join(MEDIA_DIR, str(prod_id))
                    if await asyncio.to_thread(os.path.exists, media_dir_to_delete):
                        asyncio.create_task(asyncio.to_thread(shutil.rmtree, media_dir_to_delete, ignore_errors=True))
                        logger.info(f"Scheduled deletion of media dir: {media_dir_to_delete}")
                        
            except sqlite3.Error as e: 
                logger.error(f"DB error deleting purchased products: {e}", exc_info=True)
                if conn_del and conn_del.in_transaction: 
                    conn_del.rollback()
            except Exception as e: 
                logger.error(f"Unexpected error deleting purchased products: {e}", exc_info=True)
            finally:
                if conn_del: conn_del.close()
        elif processed_product_ids and not media_delivery_successful:
            # CRITICAL: Media delivery failed - DO NOT DELETE products
            # Keep them in database so admin can manually complete the order
            logger.warning(f"⚠️ SKIPPING product deletion for user {user_id} due to media delivery failure. Products {processed_product_ids} kept for manual recovery.")

        # Only return success if both database and media delivery were successful
        if media_delivery_successful:
            return True # Indicate complete success
        else:
            logger.critical(f"🚨 CRITICAL: Purchase {user_id} - Database updated but media delivery failed! Manual intervention required!")
            return False # Indicate partial failure
    else: # Purchase failed at DB level
        if context is not None and hasattr(context, 'user_data') and context.user_data is not None:
            context.user_data['basket'] = []
            context.user_data.pop('applied_discount', None)
        if chat_id and bot: await send_message_with_retry(bot, chat_id, lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase."), parse_mode=None)
        return False


# --- Process Purchase with Balance (Uses Helper) ---
async def process_purchase_with_balance(user_id: int, amount_to_deduct: Decimal, basket_snapshot: list, discount_code_used: str | None, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handles DB updates when paying with internal balance."""
    chat_id = context._chat_id or context._user_id or user_id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    if not basket_snapshot: logger.error(f"Empty basket_snapshot for user {user_id} balance purchase."); return False
    if not isinstance(amount_to_deduct, Decimal) or amount_to_deduct < Decimal('0.0'): logger.error(f"Invalid amount_to_deduct {amount_to_deduct}."); return False

    conn = None
    db_balance_deducted = False
    balance_changed_error = lang_data.get("balance_changed_error", "❌ Transaction failed: Balance changed.")
    error_processing_purchase_contact_support = lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase. Contact support.")

    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Use IMMEDIATE instead of EXCLUSIVE to reduce lock conflicts
        c.execute("BEGIN IMMEDIATE")
        # 1. Verify balance
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        current_balance_result = c.fetchone()
        if not current_balance_result or Decimal(str(current_balance_result['balance'])) < amount_to_deduct:
             logger.warning(f"Insufficient balance user {user_id}. Needed: {amount_to_deduct:.2f}")
             conn.rollback()
             # --- Unreserve items if balance check fails ---
             logger.info(f"Un-reserving items for user {user_id} due to insufficient balance during payment.")
             # Use asyncio.to_thread for synchronous helper
             await asyncio.to_thread(_unreserve_basket_items, basket_snapshot)
             # --- End Unreserve ---
             if chat_id: await send_message_with_retry(context.bot, chat_id, balance_changed_error, parse_mode=None)
             return False
        # 2. Deduct balance
        amount_float_to_deduct = float(amount_to_deduct)
        update_res = c.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount_float_to_deduct, user_id))
        if update_res.rowcount == 0: logger.error(f"Failed to deduct balance user {user_id}."); conn.rollback(); return False

        conn.commit() # Commit balance deduction *before* finalizing items
        db_balance_deducted = True
        logger.info(f"Deducted {amount_to_deduct:.2f} EUR from balance for user {user_id}.")

    except sqlite3.Error as e:
        logger.error(f"DB error deducting balance user {user_id}: {e}", exc_info=True); db_balance_deducted = False
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()

    # 3. Finalize purchase ONLY if balance was successfully deducted
    if db_balance_deducted:
        logger.info(f"Calling _finalize_purchase for user {user_id} after balance deduction.")
        # Now call the shared finalization logic
        finalize_success = await _finalize_purchase(user_id, basket_snapshot, discount_code_used, context)
        if not finalize_success:
            # Critical issue: Balance deducted but finalization failed.
            logger.critical(f"CRITICAL: Balance deducted for user {user_id} but _finalize_purchase FAILED! Attempting to refund.")
            refund_conn = None
            try:
                refund_conn = get_db_connection()
                refund_c = refund_conn.cursor()
                refund_c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount_float_to_deduct, user_id))
                refund_conn.commit()
                logger.info(f"Successfully refunded {amount_float_to_deduct} EUR to user {user_id} after finalization failure.")
                if chat_id: await send_message_with_retry(context.bot, chat_id, error_processing_purchase_contact_support + " Balance refunded.", parse_mode=None)
            except Exception as refund_e:
                logger.critical(f"CRITICAL REFUND FAILED for user {user_id}: {refund_e}. Manual balance correction required.")
                if get_first_primary_admin_id() and chat_id: # Notify admin if refund fails
                    await send_message_with_retry(context.bot, get_first_primary_admin_id(), f"⚠️ CRITICAL REFUND FAILED for user {user_id} after purchase finalization error. Amount: {amount_to_deduct}. MANUAL CORRECTION NEEDED!", parse_mode=None)
                if chat_id: await send_message_with_retry(context.bot, chat_id, error_processing_purchase_contact_support, parse_mode=None)
            finally:
                if refund_conn: refund_conn.close()
        return finalize_success
    else:
        logger.error(f"Skipping purchase finalization for user {user_id} due to balance deduction failure.")
        # --- Unreserve items if balance deduction failed ---
        logger.info(f"Un-reserving items for user {user_id} due to balance deduction failure.")
        # Use asyncio.to_thread for synchronous helper
        await asyncio.to_thread(_unreserve_basket_items, basket_snapshot)
        # --- End Unreserve ---
        if chat_id: await send_message_with_retry(context.bot, chat_id, error_processing_purchase_contact_support, parse_mode=None)
        return False

# --- Process Successful Crypto Purchase (Uses Helper) ---
async def process_successful_crypto_purchase(user_id: int, basket_snapshot: list, discount_code_used: str | None, payment_id: str, context: ContextTypes.DEFAULT_TYPE, bot_id: str | None = None) -> bool:
    """Handles finalizing a purchase paid via crypto webhook."""
    
    # Handle None context (from background job)
    bot = None
    if context is not None:
        chat_id = getattr(context, '_chat_id', None) or getattr(context, '_user_id', None) or user_id
        lang = context.user_data.get("lang", "en") if context.user_data else "en"
        bot = context.bot if hasattr(context, 'bot') else None
    else:
        chat_id = user_id
        # Get user language from database
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
            row = c.fetchone()
            lang = row['language'] if row and row['language'] else "en"
            conn.close()
        except Exception as e:
            logger.warning(f"Could not fetch user language for {user_id}: {e}")
            lang = "en"
        
        # Get bot from main module - use specific bot_id if provided
        try:
            import main
            if bot_id and hasattr(main, 'telegram_apps') and main.telegram_apps:
                # Use the specific bot the user interacted with
                if bot_id in main.telegram_apps:
                    bot = main.telegram_apps[bot_id].bot
                    logger.info(f"Using bot {bot_id} for delivery to user {user_id}")
                else:
                    logger.warning(f"Bot {bot_id} not found in telegram_apps, using first available")
                    for b_id, app in main.telegram_apps.items():
                        bot = app.bot
                        break
            elif hasattr(main, 'telegram_app') and main.telegram_app:
                bot = main.telegram_app.bot
            elif hasattr(main, 'telegram_apps') and main.telegram_apps:
                # Fallback: Get first bot from multi-bot setup
                for b_id, app in main.telegram_apps.items():
                    bot = app.bot
                    break
        except Exception as e:
            logger.warning(f"Could not get bot instance: {e}")
    
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])

    logger.info(f"Processing successful crypto purchase for user {user_id}, payment {payment_id}. Basket items: {len(basket_snapshot) if basket_snapshot else 0}")

    if not basket_snapshot:
        logger.error(f"CRITICAL: Successful crypto payment {payment_id} for user {user_id} received, but basket snapshot was empty/missing in pending record.")
        if get_first_primary_admin_id() and bot:
            try:
                await send_message_with_retry(bot, get_first_primary_admin_id(), f"⚠️ Critical Issue: Crypto payment {payment_id} success for user {user_id}, but basket data missing! Manual check needed.", parse_mode=None)
            except Exception as admin_notify_e:
                logger.error(f"Failed to notify admin about critical missing basket data: {admin_notify_e}")
        return False # Cannot proceed

    # Call the shared finalization logic
    finalize_success = await _finalize_purchase(user_id, basket_snapshot, discount_code_used, context, bot=bot)

    if finalize_success:
        # _finalize_purchase now handles the user-facing confirmation messages
        logger.info(f"Crypto purchase finalized for {user_id}, payment {payment_id}. _finalize_purchase handled user messages.")
    else:
        # Finalization failed even after payment confirmed. This is bad.
        logger.error(f"CRITICAL: Crypto payment {payment_id} success for user {user_id}, but _finalize_purchase failed! Items paid for but not processed in DB correctly.")
        if get_first_primary_admin_id() and bot:
            try:
                await send_message_with_retry(bot, get_first_primary_admin_id(), f"⚠️ Critical Issue: Crypto payment {payment_id} success for user {user_id}, but finalization FAILED! Check logs! MANUAL INTERVENTION REQUIRED.", parse_mode=None)
            except Exception as admin_notify_e:
                 logger.error(f"Failed to notify admin about critical finalization failure: {admin_notify_e}")
        if chat_id and bot:
            await send_message_with_retry(bot, chat_id, lang_data.get("error_processing_purchase_contact_support", "❌ Error processing purchase. Contact support."), parse_mode=None)

    return finalize_success


# --- NEW: Helper Function to Credit User Balance (Moved from Previous Response) ---
async def credit_user_balance(user_id: int, amount_eur: Decimal, reason: str, context: ContextTypes.DEFAULT_TYPE, bot=None) -> bool:
    """Adds funds to a user's balance and notifies them."""
    if not isinstance(amount_eur, Decimal) or amount_eur <= Decimal('0.0'):
        logger.error(f"Invalid amount provided to credit_user_balance for user {user_id}: {amount_eur}")
        return False

    conn = None
    db_update_successful = False
    amount_float = float(amount_eur)
    new_balance_decimal = Decimal('0.0')

    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        logger.info(f"Attempting to credit balance for user {user_id} by {amount_float:.2f} EUR. Reason: {reason}")

        # Get old balance for logging
        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        old_balance_res = c.fetchone(); old_balance_float = old_balance_res['balance'] if old_balance_res else 0.0

        update_result = c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount_float, user_id))
        if update_result.rowcount == 0:
            logger.error(f"User {user_id} not found during balance credit update. Reason: {reason}")
            conn.rollback()
            return False

        c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
        new_balance_result = c.fetchone()
        if new_balance_result:
             new_balance_decimal = Decimal(str(new_balance_result['balance']))
        else:
             logger.error(f"Could not fetch new balance for {user_id} after credit update."); conn.rollback(); return False

        conn.commit()
        db_update_successful = True
        logger.info(f"Successfully credited balance for user {user_id}. Added: {amount_eur:.2f} EUR. New Balance: {new_balance_decimal:.2f} EUR. Reason: {reason}")

        # Log this as an automatic system action (or maybe under ADMIN_ID if preferred)
        log_admin_action(
             admin_id=0, # Or ADMIN_ID if you want admin to "own" these logs
             action="BALANCE_CREDIT_AUTO",
             target_user_id=user_id,
             reason=reason,
             amount_change=amount_float,
             old_value=old_balance_float,
             new_value=float(new_balance_decimal)
        )

        # Notify User
        bot_instance = bot  # Use passed bot if provided
        if bot_instance is None and context is not None and hasattr(context, 'bot'):
            bot_instance = context.bot
        if bot_instance is None:
            # Get bot from main module for background jobs
            try:
                import main
                if hasattr(main, 'telegram_app') and main.telegram_app:
                    bot_instance = main.telegram_app.bot
                elif hasattr(main, 'telegram_apps') and main.telegram_apps:
                    for b_id, app in main.telegram_apps.items():
                        bot_instance = app.bot
                        break
            except Exception:
                pass
        
        if bot_instance:
            # Get user language for notification - safely handle None context
            lang = "en"
            if context is not None and hasattr(context, 'user_data') and context.user_data is not None:
                lang = context.user_data.get("lang", "en")
            if not lang or lang == "en": # Fallback: Get from DB if not in context
                conn_lang = None
                try:
                    conn_lang = get_db_connection()
                    c_lang = conn_lang.cursor()
                    c_lang.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
                    lang_res = c_lang.fetchone()
                    if lang_res and lang_res['language'] in LANGUAGES: lang = lang_res['language']
                except Exception as lang_e: logger.warning(f"Could not fetch user lang for credit msg: {lang_e}")
                finally:
                     if conn_lang: conn_lang.close()
            lang_data = LANGUAGES.get(lang, LANGUAGES['en'])


            if "Overpayment" in reason:
                # Example message key: "credit_overpayment_purchase"
                notify_msg_template = lang_data.get("credit_overpayment_purchase", "✅ Your purchase was successful! Additionally, an overpayment of {amount} EUR has been credited to your balance. Your new balance is {new_balance} EUR.")
            elif "Underpayment" in reason:
                # Example message key: "credit_underpayment_purchase"
                 notify_msg_template = lang_data.get("credit_underpayment_purchase", "ℹ️ Your purchase failed due to underpayment, but the received amount ({amount} EUR) has been credited to your balance. Your new balance is {new_balance} EUR.")
            else: # Generic credit (like Refill)
                # Example message key: "credit_refill"
                notify_msg_template = lang_data.get("credit_refill", "✅ Your balance has been credited by {amount} EUR. Reason: {reason}. New balance: {new_balance} EUR.")

            notify_msg = notify_msg_template.format(
                amount=format_currency(amount_eur),
                new_balance=format_currency(new_balance_decimal),
                reason=reason # Include reason for generic credits
            )

            await send_message_with_retry(bot_instance, user_id, notify_msg, parse_mode=None)
        else:
             logger.error(f"Could not get bot instance to notify user {user_id} about balance credit.")

        return True

    except sqlite3.Error as e:
        logger.error(f"DB error during credit_user_balance user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
        return False
    except Exception as e:
         logger.error(f"Unexpected error during credit_user_balance user {user_id}: {e}", exc_info=True)
         if conn and conn.in_transaction: conn.rollback()
         return False
    finally:
        if conn: conn.close()
# --- END credit_user_balance ---


# --- Callback Handler Wrapper (to keep main.py structure) ---
async def handle_confirm_pay(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """
    This is a wrapper function.
    The main logic for confirm_pay is now in user.py.
    This function ensures the callback router in main.py finds a handler here.
    """
    logger.debug("Payment.handle_confirm_pay called, forwarding to user.handle_confirm_pay")
    # Call the actual handler which is now located in user.py
    await user.handle_confirm_pay(update, context, params)

# --- UPDATED: Callback Handler for Crypto Payment Cancellation ---
async def handle_cancel_crypto_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles user clicking Cancel Payment button to cancel their crypto payment and unreserve items."""
    query = update.callback_query
    user_id = query.from_user.id
    lang = context.user_data.get("lang", "en")
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    
    # Retrieve stored payment_id from user_data
    pending_payment_id = context.user_data.get('pending_payment_id')
    
    if not pending_payment_id:
        logger.warning(f"User {user_id} tried to cancel crypto payment but no pending_payment_id found in user_data. Session may have expired.")
        await query.answer("No pending payment found. Session may have expired.", show_alert=True)
        # Redirect to shop to help user continue
        await user.handle_shop(update, context)
        return
    
    logger.info(f"User {user_id} requested to cancel crypto payment {pending_payment_id}.")
    
    # Remove the pending payment (this will also unreserve items if it's a purchase)
    removal_success = await asyncio.to_thread(remove_pending_deposit, pending_payment_id, trigger="user_cancellation")
    
    # Clear the stored payment_id from user_data regardless of success/failure
    context.user_data.pop('pending_payment_id', None)
    
    if removal_success:
        cancellation_success_msg = lang_data.get("payment_cancelled_success", "✅ Payment cancelled successfully. Reserved items have been released.")
        logger.info(f"Successfully cancelled payment {pending_payment_id} for user {user_id}")
    else:
        cancellation_success_msg = lang_data.get("payment_cancel_error", "⚠️ Payment cancellation processed, but there may have been an issue. Please contact support if you experience problems.")
        logger.warning(f"Issue occurred during payment cancellation {pending_payment_id} for user {user_id}")
    
    # Determine appropriate back button
    back_button_text = lang_data.get("back_basket_button", "Back to Basket")
    back_callback = "view_basket"
    
    keyboard = [[InlineKeyboardButton(f"⬅️ {back_button_text}", callback_data=back_callback)]]
    
    try:
        await query.edit_message_text(
            cancellation_success_msg, 
            reply_markup=InlineKeyboardMarkup(keyboard), 
            parse_mode=None
        )
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.warning(f"Could not edit message during payment cancellation for user {user_id}: {e}")
        await query.answer("Payment cancelled!")
    
    await query.answer()



