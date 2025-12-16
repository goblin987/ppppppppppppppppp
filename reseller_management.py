# =============================================================================
# IMPROVED RESELLER MANAGEMENT SYSTEM
# Features:
#   - Search by @username OR user ID
#   - Global discount (apply to ALL product types at once)
#   - Quick preset templates (10%, 15%, 20%, 25%)
#   - One-click enable + set discount
#   - Clear status indicators
# =============================================================================

import sqlite3
import logging
import time
from decimal import Decimal, ROUND_DOWN # Use Decimal for precision
import math # For pagination calculation

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import telegram.error as telegram_error
# -------------------------

# Import shared elements from utils
from utils import (
    ADMIN_ID, LANGUAGES, get_db_connection, send_message_with_retry,
    PRODUCT_TYPES, format_currency, log_admin_action, load_all_data,
    DEFAULT_PRODUCT_EMOJI,
    # Import action constants for logging
    ACTION_RESELLER_ENABLED, ACTION_RESELLER_DISABLED,
    ACTION_RESELLER_DISCOUNT_ADD, ACTION_RESELLER_DISCOUNT_EDIT,
    ACTION_RESELLER_DISCOUNT_DELETE,
    # Admin helper functions
    is_primary_admin, is_secondary_admin, is_any_admin
)

# Logging setup specific to this module
logger = logging.getLogger(__name__)

# Constants
USERS_PER_PAGE_DISCOUNT_SELECT = 10 # Keep for selecting reseller for discount mgmt
QUICK_DISCOUNT_PRESETS = [10, 15, 20, 25, 30]  # Quick preset percentages

# --- Helper Function to Get Reseller Discount ---
# (Keep this function as is)
async def get_reseller_discount_with_connection(cursor, user_id: int, product_type: str) -> Decimal:
    """Fetches the discount percentage for a specific reseller and product type using existing cursor."""
    discount = Decimal('0.0')
    
    try:
        # Enhanced logging for debugging
        logger.info(f"Checking reseller discount for user {user_id}, product type '{product_type}'")
        
        cursor.execute("SELECT is_reseller FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        
        if not res:
            logger.warning(f"User {user_id} not found in database for reseller discount check")
            return discount
            
        is_reseller = res['is_reseller']
        logger.info(f"User {user_id} reseller status: {is_reseller} (1=reseller, 0=not reseller)")
        
        if res and res['is_reseller'] == 1:
            # User is a reseller, get their discount for this product type
            cursor.execute("""
                SELECT discount_percentage FROM reseller_discounts 
                WHERE reseller_user_id = ? AND product_type = ?
            """, (user_id, product_type))
            
            discount_result = cursor.fetchone()
            if discount_result:
                discount = Decimal(str(discount_result['discount_percentage']))
                logger.info(f"Reseller discount for user {user_id}, type '{product_type}': {discount}%")
            else:
                logger.info(f"No specific discount found for reseller {user_id}, type '{product_type}'. Using 0%")
        else:
            logger.info(f"User {user_id} is not a reseller (is_reseller={is_reseller}), returning 0% discount")
            
    except sqlite3.Error as e:
        logger.error(f"DB error fetching reseller discount for user {user_id}, type {product_type}: {e}")
        return Decimal('0.0')  # Return 0% discount on error
    except Exception as e:
        logger.error(f"Unexpected error in reseller discount check for user {user_id}: {e}", exc_info=True)
        return Decimal('0.0')
    
    return discount

def get_reseller_discount(user_id: int, product_type: str) -> Decimal:
    """Fetches the discount percentage for a specific reseller and product type."""
    discount = Decimal('0.0')
    conn = None
    max_retries = 3
    retry_delay = 0.1  # 100ms
    
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            
            # Enhanced logging for debugging
            logger.info(f"Checking reseller discount for user {user_id}, product type '{product_type}'")
            
            c.execute("SELECT is_reseller FROM users WHERE user_id = ?", (user_id,))
            res = c.fetchone()
            
            if not res:
                logger.warning(f"User {user_id} not found in database for reseller discount check")
                return discount
                
            is_reseller = res['is_reseller']
            logger.info(f"User {user_id} reseller status: {is_reseller} (1=reseller, 0=not reseller)")
            
            if res and res['is_reseller'] == 1:
                # Check what discount records exist for this user
                c.execute("SELECT product_type, discount_percentage FROM reseller_discounts WHERE reseller_user_id = ?", (user_id,))
                all_discounts = c.fetchall()
                logger.info(f"User {user_id} has {len(all_discounts)} discount records: {[(d['product_type'], d['discount_percentage']) for d in all_discounts]}")
                
                c.execute("""
                    SELECT discount_percentage FROM reseller_discounts
                    WHERE reseller_user_id = ? AND product_type = ?
                """, (user_id, product_type))
                discount_res = c.fetchone()
                if discount_res:
                    discount = Decimal(str(discount_res['discount_percentage']))
                    logger.info(f"✅ Found reseller discount for user {user_id}, type '{product_type}': {discount}%")
                else:
                    logger.info(f"❌ No reseller discount found for user {user_id}, type '{product_type}' (user is reseller but no specific discount set)")
            else:
                logger.info(f"User {user_id} is not a reseller (is_reseller={is_reseller}), returning 0% discount")
            
            # Success - break out of retry loop
            break
            
        except sqlite3.Error as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                logger.warning(f"Database locked for reseller discount check (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                if conn: 
                    conn.close()
                    conn = None
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                logger.error(f"DB error fetching reseller discount for user {user_id}, type {product_type}: {e}")
                break
        except Exception as e:
            logger.error(f"Unexpected error fetching reseller discount: {e}", exc_info=True)
            break
        finally:
            if conn: 
                conn.close()
                conn = None
    
    return discount


# ==================================
# --- Admin: Manage Reseller Status --- (REVISED FLOW)
# ==================================

async def handle_manage_resellers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Main reseller management menu - improved with overview and quick actions."""
    query = update.callback_query
    if not is_primary_admin(query.from_user.id): return await query.answer("Access Denied.", show_alert=True)

    # Fetch reseller stats
    total_resellers = 0
    recent_resellers = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE is_reseller = 1")
        result = c.fetchone()
        total_resellers = result['count'] if result else 0
        
        # Get recent resellers with their discount count
        c.execute("""
            SELECT u.user_id, u.username, 
                   (SELECT COUNT(*) FROM reseller_discounts rd WHERE rd.reseller_user_id = u.user_id) as discount_count
            FROM users u 
            WHERE u.is_reseller = 1 
            ORDER BY u.user_id DESC 
            LIMIT 5
        """)
        recent_resellers = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching reseller stats: {e}")
    finally:
        if conn: conn.close()

    msg = "👥 **RESELLER MANAGEMENT**\n\n"
    msg += f"📊 Active Resellers: **{total_resellers}**\n\n"
    
    if recent_resellers:
        msg += "Recent Resellers:\n"
        for r in recent_resellers:
            username = r['username'] or f"ID_{r['user_id']}"
            discount_count = r['discount_count']
            status_icon = "✅" if discount_count > 0 else "⚠️"
            msg += f"  {status_icon} @{username} ({discount_count} discounts)\n"
        msg += "\n"
    
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "💡 **Quick Actions:**"

    keyboard = [
        [InlineKeyboardButton("🔍 Find User (ID or @username)", callback_data="reseller_search_user")],
        [InlineKeyboardButton("📋 View All Resellers", callback_data="manage_reseller_discounts_select_reseller|0")],
        [InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")]
    ]

    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer()


async def handle_reseller_search_user(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Prompts admin to search for a user by ID OR username."""
    query = update.callback_query
    if not is_primary_admin(query.from_user.id): return await query.answer("Access Denied.", show_alert=True)

    context.user_data['state'] = 'awaiting_reseller_manage_id'

    prompt_msg = ("🔍 **Find User**\n\n"
                  "Enter one of the following:\n"
                  "• **User ID** (e.g., `123456789`)\n"
                  "• **Username** (e.g., `@johndoe` or `johndoe`)\n\n"
                  "💡 Tip: You can copy user ID from their Telegram profile or from purchase notifications.")
    keyboard = [[InlineKeyboardButton("⬅️ Back", callback_data="manage_resellers_menu")]]

    await query.edit_message_text(prompt_msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer("Enter User ID or @username in chat.")


async def handle_reseller_manage_id_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering a User ID OR USERNAME for reseller status management."""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_primary_admin(admin_id): return
    if context.user_data.get("state") != 'awaiting_reseller_manage_id': return
    if not update.message or not update.message.text: return

    search_text = update.message.text.strip()
    target_user_id = None
    search_by_username = False

    # Check if it's a username (starts with @ or is not a number)
    if search_text.startswith('@'):
        search_text = search_text[1:]  # Remove @ prefix
        search_by_username = True
    elif not search_text.isdigit():
        search_by_username = True
    else:
        try:
            target_user_id = int(search_text)
            if target_user_id == admin_id:
                await send_message_with_retry(context.bot, chat_id, "❌ You cannot manage your own reseller status.", parse_mode=None)
                return
        except ValueError:
            search_by_username = True

    # Clear state
    context.user_data.pop('state', None)

    # Fetch user info
    conn = None
    user_info = None
    multiple_results = []
    
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        if search_by_username:
            # Search by username (case-insensitive, partial match)
            c.execute("""
                SELECT user_id, username, is_reseller, balance, total_purchases 
                FROM users 
                WHERE LOWER(username) LIKE LOWER(?) 
                ORDER BY total_purchases DESC
                LIMIT 10
            """, (f"%{search_text}%",))
            multiple_results = c.fetchall()
            
            if len(multiple_results) == 1:
                user_info = multiple_results[0]
                target_user_id = user_info['user_id']
            elif len(multiple_results) > 1:
                # Show selection list
                msg = f"🔍 Found {len(multiple_results)} users matching '{search_text}':\n\n"
                keyboard = []
                for user in multiple_results:
                    uname = user['username'] or f"ID_{user['user_id']}"
                    is_res = "✅" if user['is_reseller'] == 1 else "❌"
                    purchases = user['total_purchases'] or 0
                    keyboard.append([InlineKeyboardButton(
                        f"{is_res} @{uname} ({purchases} purchases)", 
                        callback_data=f"reseller_view_user|{user['user_id']}"
                    )])
                keyboard.append([InlineKeyboardButton("⬅️ Search Again", callback_data="reseller_search_user")])
                keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="manage_resellers_menu")])
                
                await send_message_with_retry(context.bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
                return
            else:
                await send_message_with_retry(context.bot, chat_id, 
                    f"❌ No users found matching '{search_text}'.\n\n💡 Tips:\n• Make sure the user has used /start in this bot\n• Try searching by User ID instead", 
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Try Again", callback_data="reseller_search_user")]]),
                    parse_mode=None)
                return
        else:
            # Search by user ID
            c.execute("SELECT user_id, username, is_reseller, balance, total_purchases FROM users WHERE user_id = ?", (target_user_id,))
            user_info = c.fetchone()
            
    except sqlite3.Error as e:
        logger.error(f"DB error searching user: {e}")
        await send_message_with_retry(context.bot, chat_id, "❌ Database error.", parse_mode=None)
        return
    finally:
        if conn: conn.close()

    if not user_info:
        await send_message_with_retry(context.bot, chat_id, 
            f"❌ User ID {target_user_id} not found.\n\n💡 The user must have pressed /start in this bot first.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Try Again", callback_data="reseller_search_user")]]),
            parse_mode=None)
        return

    # Display user profile with reseller options
    await _display_reseller_user_profile(context.bot, chat_id, user_info)


async def _display_reseller_user_profile(bot, chat_id, user_info, edit_message_id=None):
    """Helper to display user profile with reseller management options."""
    target_user_id = user_info['user_id']
    username = user_info['username'] or f"ID_{target_user_id}"
    is_reseller = user_info['is_reseller'] == 1
    balance = user_info['balance'] if user_info['balance'] else 0
    purchases = user_info['total_purchases'] if user_info['total_purchases'] else 0
    
    # Fetch current discounts
    discounts = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT product_type, discount_percentage 
            FROM reseller_discounts 
            WHERE reseller_user_id = ? 
            ORDER BY product_type
        """, (target_user_id,))
        discounts = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Error fetching discounts: {e}")
    finally:
        if conn: conn.close()
    
    # Build message
    status_icon = "✅" if is_reseller else "❌"
    msg = f"👤 **USER PROFILE**\n\n"
    msg += f"**Username:** @{username}\n"
    msg += f"**User ID:** `{target_user_id}`\n"
    msg += f"**Balance:** {format_currency(Decimal(str(balance)))} EUR\n"
    msg += f"**Purchases:** {purchases}\n\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"**Reseller Status:** {status_icon} {'ACTIVE' if is_reseller else 'INACTIVE'}\n"
    
    if is_reseller and discounts:
        msg += f"\n**Current Discounts:**\n"
        for d in discounts:
            emoji = PRODUCT_TYPES.get(d['product_type'], DEFAULT_PRODUCT_EMOJI)
            msg += f"  • {emoji} {d['product_type']}: **{d['discount_percentage']:.1f}%**\n"
    elif is_reseller:
        msg += f"\n⚠️ No discounts configured yet!\n"
    
    msg += f"\n━━━━━━━━━━━━━━━━━━━━"
    
    # Build keyboard
    keyboard = []
    
    if is_reseller:
        keyboard.append([InlineKeyboardButton("🚫 Disable Reseller", callback_data=f"reseller_toggle_status|{target_user_id}|0")])
        # Two discount options: individual per-type OR quick global
        keyboard.append([InlineKeyboardButton("🏷️ Per-Type Discounts", callback_data=f"reseller_manage_specific|{target_user_id}")])
        keyboard.append([InlineKeyboardButton("⚡ Set ALL Types Same %", callback_data=f"reseller_quick_discount|{target_user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("✅ Enable as Reseller", callback_data=f"reseller_toggle_status|{target_user_id}|0")])
        keyboard.append([InlineKeyboardButton("⚡ Enable + Set Discount", callback_data=f"reseller_quick_enable|{target_user_id}")])
    
    keyboard.append([InlineKeyboardButton("🔍 Search Another", callback_data="reseller_search_user")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="manage_resellers_menu")])
    
    await send_message_with_retry(bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def handle_reseller_view_user(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """View a specific user's reseller profile (from search results)."""
    query = update.callback_query
    if not is_primary_admin(query.from_user.id): return await query.answer("Access Denied.", show_alert=True)
    
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid user ID.", show_alert=True)
        return
    
    target_user_id = int(params[0])
    
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username, is_reseller, balance, total_purchases FROM users WHERE user_id = ?", (target_user_id,))
        user_info = c.fetchone()
    except sqlite3.Error as e:
        logger.error(f"Error fetching user: {e}")
        await query.answer("Database error.", show_alert=True)
        return
    finally:
        if conn: conn.close()
    
    if not user_info:
        await query.answer("User not found.", show_alert=True)
        return
    
    await query.message.delete()
    await _display_reseller_user_profile(context.bot, query.message.chat_id, user_info)
    await query.answer()


async def handle_reseller_quick_enable(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Quick enable reseller + show discount preset options."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True)
        return
    
    target_user_id = int(params[0])
    
    # Enable reseller status first
    conn = None
    username = f"ID_{target_user_id}"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username, is_reseller FROM users WHERE user_id = ?", (target_user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.answer("User not found.", show_alert=True)
            return
        
        username = user_data['username'] or username
        
        if user_data['is_reseller'] != 1:
            c.execute("UPDATE users SET is_reseller = 1 WHERE user_id = ?", (target_user_id,))
            conn.commit()
            log_admin_action(admin_id, ACTION_RESELLER_ENABLED, target_user_id=target_user_id, old_value=0, new_value=1)
    except sqlite3.Error as e:
        logger.error(f"Error enabling reseller: {e}")
        await query.answer("Database error.", show_alert=True)
        return
    finally:
        if conn: conn.close()
    
    # Show quick discount options
    msg = f"✅ **@{username}** is now a Reseller!\n\n"
    msg += "⚡ **Quick Setup:** Select a discount to apply to ALL product types:\n"
    
    keyboard = []
    for preset in QUICK_DISCOUNT_PRESETS:
        keyboard.append([InlineKeyboardButton(f"🏷️ {preset}% off ALL types", callback_data=f"reseller_apply_global|{target_user_id}|{preset}")])
    
    keyboard.append([InlineKeyboardButton("✏️ Custom Discount", callback_data=f"reseller_custom_global|{target_user_id}")])
    keyboard.append([InlineKeyboardButton("⏭️ Skip (set later)", callback_data=f"reseller_view_user|{target_user_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="manage_resellers_menu")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer("Reseller enabled!")


async def handle_reseller_quick_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Show quick discount preset options for existing reseller."""
    query = update.callback_query
    if not is_primary_admin(query.from_user.id): return await query.answer("Access Denied.", show_alert=True)
    
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True)
        return
    
    target_user_id = int(params[0])
    
    # Get username
    conn = None
    username = f"ID_{target_user_id}"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username FROM users WHERE user_id = ?", (target_user_id,))
        user_data = c.fetchone()
        if user_data:
            username = user_data['username'] or username
    except:
        pass
    finally:
        if conn: conn.close()
    
    msg = f"⚡ **Quick Discount for @{username}**\n\n"
    msg += "Select a discount to apply to **ALL** product types:\n\n"
    msg += "💡 This will replace any existing discounts."
    
    keyboard = []
    for preset in QUICK_DISCOUNT_PRESETS:
        keyboard.append([InlineKeyboardButton(f"🏷️ {preset}% off ALL types", callback_data=f"reseller_apply_global|{target_user_id}|{preset}")])
    
    keyboard.append([InlineKeyboardButton("✏️ Custom Percentage", callback_data=f"reseller_custom_global|{target_user_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"reseller_view_user|{target_user_id}")])
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer()


async def handle_reseller_apply_global(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Apply a global discount to ALL product types for a reseller."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    
    if not params or len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True)
        return
    
    target_user_id = int(params[0])
    discount_percent = int(params[1])
    
    load_all_data()  # Ensure PRODUCT_TYPES is fresh
    
    if not PRODUCT_TYPES:
        await query.answer("No product types configured!", show_alert=True)
        return
    
    conn = None
    username = f"ID_{target_user_id}"
    applied_count = 0
    
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute("SELECT username FROM users WHERE user_id = ?", (target_user_id,))
        user_data = c.fetchone()
        if user_data:
            username = user_data['username'] or username
        
        c.execute("BEGIN")
        
        # Delete existing discounts
        c.execute("DELETE FROM reseller_discounts WHERE reseller_user_id = ?", (target_user_id,))
        
        # Insert new discounts for ALL product types
        for product_type in PRODUCT_TYPES.keys():
            c.execute("""
                INSERT INTO reseller_discounts (reseller_user_id, product_type, discount_percentage)
                VALUES (?, ?, ?)
            """, (target_user_id, product_type, float(discount_percent)))
            applied_count += 1
        
        conn.commit()
        
        # Log action
        log_admin_action(admin_id, ACTION_RESELLER_DISCOUNT_ADD, target_user_id=target_user_id,
                        reason=f"Global: {discount_percent}% on all {applied_count} types", new_value=discount_percent)
        
    except sqlite3.Error as e:
        logger.error(f"Error applying global discount: {e}")
        if conn: conn.rollback()
        await query.answer("Database error!", show_alert=True)
        return
    finally:
        if conn: conn.close()
    
    msg = f"✅ **Global Discount Applied!**\n\n"
    msg += f"**Reseller:** @{username}\n"
    msg += f"**Discount:** {discount_percent}% off\n"
    msg += f"**Applied to:** {applied_count} product types\n\n"
    msg += "The reseller will now see discounted prices for all products!"
    
    keyboard = [
        [InlineKeyboardButton("👤 View Profile", callback_data=f"reseller_view_user|{target_user_id}")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="manage_resellers_menu")]
    ]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer(f"Applied {discount_percent}% to all types!")


async def handle_reseller_custom_global(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Prompt for custom global discount percentage."""
    query = update.callback_query
    if not is_primary_admin(query.from_user.id): return await query.answer("Access Denied.", show_alert=True)
    
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True)
        return
    
    target_user_id = int(params[0])
    
    context.user_data['state'] = 'awaiting_reseller_global_percent'
    context.user_data['reseller_mgmt_target_id'] = target_user_id
    
    msg = "✏️ **Custom Global Discount**\n\n"
    msg += "Enter the discount percentage (0-100):\n"
    msg += "Example: `15` for 15% off all products"
    
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"reseller_view_user|{target_user_id}")]]
    
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer("Enter percentage in chat.")


async def handle_reseller_global_percent_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle custom global discount percentage input."""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_primary_admin(admin_id): return
    if context.user_data.get("state") != 'awaiting_reseller_global_percent': return
    if not update.message or not update.message.text: return
    
    percent_text = update.message.text.strip()
    target_user_id = context.user_data.get('reseller_mgmt_target_id')
    
    if target_user_id is None:
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost.", parse_mode=None)
        context.user_data.pop('state', None)
        return
    
    try:
        percentage = int(percent_text)
        if not (0 <= percentage <= 100):
            raise ValueError("Out of range")
        
        context.user_data.pop('state', None)
        
        # Apply global discount
        load_all_data()
        
        conn = None
        username = f"ID_{target_user_id}"
        applied_count = 0
        
        try:
            conn = get_db_connection()
            c = conn.cursor()
            
            c.execute("SELECT username FROM users WHERE user_id = ?", (target_user_id,))
            user_data = c.fetchone()
            if user_data:
                username = user_data['username'] or username
            
            c.execute("BEGIN")
            c.execute("DELETE FROM reseller_discounts WHERE reseller_user_id = ?", (target_user_id,))
            
            for product_type in PRODUCT_TYPES.keys():
                c.execute("""
                    INSERT INTO reseller_discounts (reseller_user_id, product_type, discount_percentage)
                    VALUES (?, ?, ?)
                """, (target_user_id, product_type, float(percentage)))
                applied_count += 1
            
            conn.commit()
            
            log_admin_action(admin_id, ACTION_RESELLER_DISCOUNT_ADD, target_user_id=target_user_id,
                            reason=f"Global: {percentage}% on all {applied_count} types", new_value=percentage)
            
        except sqlite3.Error as e:
            logger.error(f"Error applying global discount: {e}")
            if conn: conn.rollback()
            await send_message_with_retry(context.bot, chat_id, "❌ Database error!", parse_mode=None)
            return
        finally:
            if conn: conn.close()
        
        msg = f"✅ **Global Discount Applied!**\n\n"
        msg += f"**Reseller:** @{username}\n"
        msg += f"**Discount:** {percentage}% off\n"
        msg += f"**Applied to:** {applied_count} product types"
        
        keyboard = [
            [InlineKeyboardButton("👤 View Profile", callback_data=f"reseller_view_user|{target_user_id}")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="manage_resellers_menu")]
        ]
        
        await send_message_with_retry(context.bot, chat_id, msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        
    except ValueError:
        await send_message_with_retry(context.bot, chat_id, "❌ Invalid percentage. Enter a number between 0 and 100.", parse_mode=None)


async def handle_reseller_toggle_status(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Toggles the is_reseller flag for a user (called from user display)."""
    query = update.callback_query
    admin_id = query.from_user.id
    chat_id = query.message.chat_id

    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_user_id = int(params[0])
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username, is_reseller, balance, total_purchases FROM users WHERE user_id = ?", (target_user_id,))
        user_data = c.fetchone()
        if not user_data:
            await query.answer("User not found.", show_alert=True)
            return await handle_manage_resellers_menu(update, context)

        current_status = user_data['is_reseller']
        username = user_data['username'] or f"ID_{target_user_id}"
        new_status = 0 if current_status == 1 else 1
        c.execute("UPDATE users SET is_reseller = ? WHERE user_id = ?", (new_status, target_user_id))
        conn.commit()

        action_desc = ACTION_RESELLER_ENABLED if new_status == 1 else ACTION_RESELLER_DISABLED
        log_admin_action(admin_id, action_desc, target_user_id=target_user_id, old_value=current_status, new_value=new_status)

        status_text = "enabled" if new_status == 1 else "disabled"
        await query.answer(f"Reseller status {status_text}!")

        # Refresh user profile display
        user_info = {
            'user_id': target_user_id,
            'username': username,
            'is_reseller': new_status,
            'balance': user_data['balance'],
            'total_purchases': user_data['total_purchases']
        }
        
        await query.message.delete()
        await _display_reseller_user_profile(context.bot, chat_id, user_info)

    except sqlite3.Error as e:
        logger.error(f"DB error toggling reseller status {target_user_id}: {e}")
        await query.answer("DB Error.", show_alert=True)
    except Exception as e:
        logger.error(f"Error toggling reseller status {target_user_id}: {e}", exc_info=True)
        await query.answer("Error.", show_alert=True)
    finally:
        if conn: conn.close()


# ========================================
# --- Admin: Manage Reseller Discounts --- (Pagination kept)
# ========================================

async def handle_manage_reseller_discounts_select_reseller(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects which active reseller to manage discounts for (PAGINATED)."""
    query = update.callback_query
    if not is_primary_admin(query.from_user.id): return await query.answer("Access Denied.", show_alert=True)
    offset = 0
    if params and len(params) > 0 and params[0].isdigit(): offset = int(params[0])

    resellers = []
    total_resellers = 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as count FROM users WHERE is_reseller = 1")
        count_res = c.fetchone(); total_resellers = count_res['count'] if count_res else 0
        c.execute("""
            SELECT user_id, username FROM users
            WHERE is_reseller = 1 ORDER BY user_id DESC LIMIT ? OFFSET ?
        """, (USERS_PER_PAGE_DISCOUNT_SELECT, offset)) # Use specific constant
        resellers = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching active resellers: {e}")
        await query.edit_message_text("❌ DB Error fetching resellers.", parse_mode=None)
        return
    finally:
        if conn: conn.close()

    msg = "👤 Manage Reseller Discounts\n\nSelect an active reseller to set their discounts:\n"
    keyboard = []
    item_buttons = []

    if not resellers and offset == 0: msg += "\nNo active resellers found."
    elif not resellers: msg += "\nNo more resellers."
    else:
        for r in resellers:
            username = r['username'] or f"ID_{r['user_id']}"
            item_buttons.append([InlineKeyboardButton(f"👤 @{username}", callback_data=f"reseller_manage_specific|{r['user_id']}")])
        keyboard.extend(item_buttons)
        # Pagination
        total_pages = math.ceil(max(0, total_resellers) / USERS_PER_PAGE_DISCOUNT_SELECT)
        current_page = (offset // USERS_PER_PAGE_DISCOUNT_SELECT) + 1
        nav_buttons = []
        if current_page > 1: nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"manage_reseller_discounts_select_reseller|{max(0, offset - USERS_PER_PAGE_DISCOUNT_SELECT)}"))
        if current_page < total_pages: nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"manage_reseller_discounts_select_reseller|{offset + USERS_PER_PAGE_DISCOUNT_SELECT}"))
        if nav_buttons: keyboard.append(nav_buttons)
        msg += f"\nPage {current_page}/{total_pages}"

    keyboard.append([InlineKeyboardButton("⬅️ Back to Admin Menu", callback_data="admin_menu")])
    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing reseller selection list: {e}")
            await query.answer("Error updating list.", show_alert=True)
        else: await query.answer()
    except Exception as e:
        logger.error(f"Error display reseller selection list: {e}", exc_info=True)
        await query.edit_message_text("❌ Error displaying list.", parse_mode=None)


# --- Manage Specific Reseller Discounts ---

async def handle_manage_specific_reseller_discounts(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Displays current discounts for a specific reseller and allows adding/editing per product type."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid user ID.", show_alert=True); return

    target_reseller_id = int(params[0])
    discounts = []
    username = f"ID_{target_reseller_id}"
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT username FROM users WHERE user_id = ?", (target_reseller_id,))
        user_res = c.fetchone(); username = user_res['username'] if user_res and user_res['username'] else username
        c.execute("""
            SELECT product_type, discount_percentage FROM reseller_discounts
            WHERE reseller_user_id = ? ORDER BY product_type
        """, (target_reseller_id,))
        discounts = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"DB error fetching discounts for reseller {target_reseller_id}: {e}")
        await query.edit_message_text("❌ DB Error fetching discounts.", parse_mode=None)
        return
    finally:
        if conn: conn.close()

    load_all_data()  # Ensure PRODUCT_TYPES is fresh
    
    msg = f"🏷️ **Per-Type Discounts**\n"
    msg += f"**Reseller:** @{username}\n\n"
    
    keyboard = []
    
    # Create a dict for quick lookup
    discount_dict = {d['product_type']: d['discount_percentage'] for d in discounts}
    
    # Show ALL product types with their discount status
    msg += "**Product Types:**\n"
    for p_type, emoji in sorted(PRODUCT_TYPES.items()):
        if p_type in discount_dict:
            percentage = discount_dict[p_type]
            msg += f"  ✅ {emoji} {p_type}: **{percentage:.1f}%** off\n"
            keyboard.append([
                InlineKeyboardButton(f"✏️ {emoji} {p_type} ({percentage:.1f}%)", callback_data=f"reseller_edit_discount|{target_reseller_id}|{p_type}"),
                InlineKeyboardButton("🗑️", callback_data=f"reseller_delete_discount_confirm|{target_reseller_id}|{p_type}")
            ])
        else:
            msg += f"  ❌ {emoji} {p_type}: No discount\n"
            keyboard.append([
                InlineKeyboardButton(f"➕ Add {emoji} {p_type}", callback_data=f"reseller_add_discount_enter_percent_direct|{target_reseller_id}|{p_type}")
            ])
    
    msg += f"\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"💡 Tip: Use 'Set ALL Types' for same % on everything"

    keyboard.append([InlineKeyboardButton("⚡ Set ALL Types Same %", callback_data=f"reseller_quick_discount|{target_reseller_id}")])
    keyboard.append([InlineKeyboardButton("👤 Back to Profile", callback_data=f"reseller_view_user|{target_reseller_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="manage_resellers_menu")])

    try:
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)
    except telegram_error.BadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Error editing specific reseller discounts: {e}")
            await query.answer("Error updating view.", show_alert=True)
        else: await query.answer()
    except Exception as e:
        logger.error(f"Error display specific reseller discounts: {e}", exc_info=True)
        await query.edit_message_text("❌ Error displaying discounts.", parse_mode=None)


# <<< FIXED >>>
async def handle_reseller_add_discount_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin selects product type for a new reseller discount rule."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    if not params or not params[0].isdigit():
        await query.answer("Error: Invalid user ID.", show_alert=True); return

    target_reseller_id = int(params[0])
    # <<< STORE the target ID in context >>>
    context.user_data['reseller_mgmt_target_id'] = target_reseller_id

    load_all_data() # Ensure PRODUCT_TYPES is fresh

    if not PRODUCT_TYPES:
        await query.edit_message_text("❌ No product types configured. Please add types via 'Manage Product Types'.", parse_mode=None)
        return

    keyboard = []
    for type_name, emoji in sorted(PRODUCT_TYPES.items()):
        # <<< MODIFIED callback_data: Only command and type_name >>>
        callback_data_short = f"reseller_add_discount_enter_percent|{type_name}"
        # <<< ADDED length check >>>
        if len(callback_data_short.encode('utf-8')) > 64:
            logger.warning(f"Callback data for type '{type_name}' is too long ({len(callback_data_short.encode('utf-8'))} bytes) and will be skipped: {callback_data_short}")
            continue # Skip this button if the data is too long
        keyboard.append([InlineKeyboardButton(f"{emoji} {type_name}", callback_data=callback_data_short)])

    # Cancel button still needs the target_id to go back correctly
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"reseller_manage_specific|{target_reseller_id}")])
    await query.edit_message_text("Select Product Type for new discount rule:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


# <<< FIXED >>>
async def handle_reseller_add_discount_enter_percent(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin needs to enter the percentage for the new rule."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)

    # <<< RETRIEVE target ID from context >>>
    target_reseller_id = context.user_data.get('reseller_mgmt_target_id')

    # <<< Params now only contain the product_type >>>
    if not params or len(params) < 1 or target_reseller_id is None:
        logger.error("handle_reseller_add_discount_enter_percent missing context or params.")
        await query.answer("Error: Missing data.", show_alert=True); return

    product_type = params[0] # Get type from params
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)

    context.user_data['state'] = 'awaiting_reseller_discount_percent'
    # reseller_mgmt_target_id is already in context
    context.user_data['reseller_mgmt_product_type'] = product_type
    context.user_data['reseller_mgmt_mode'] = 'add'

    # Cancel button still needs the target_id
    cancel_callback = f"reseller_manage_specific|{target_reseller_id}"

    await query.edit_message_text(
        f"Enter discount percentage for {emoji} {product_type} (e.g., 10 or 15.5):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback)]]),
        parse_mode=None
    )
    await query.answer("Enter percentage in chat.")


async def handle_reseller_add_discount_enter_percent_direct(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Direct entry of discount percentage for a specific product type (from per-type view)."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_reseller_id = int(params[0])
    product_type = params[1]
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)

    context.user_data['state'] = 'awaiting_reseller_discount_percent'
    context.user_data['reseller_mgmt_target_id'] = target_reseller_id
    context.user_data['reseller_mgmt_product_type'] = product_type
    context.user_data['reseller_mgmt_mode'] = 'add'

    msg = f"🏷️ **Set Discount for {emoji} {product_type}**\n\n"
    msg += "Enter the discount percentage (0-100):\n"
    msg += "Example: `15` for 15% off"

    cancel_callback = f"reseller_manage_specific|{target_reseller_id}"
    
    # Show quick preset buttons too
    keyboard = []
    preset_row = []
    for preset in [5, 10, 15, 20, 25]:
        preset_row.append(InlineKeyboardButton(f"{preset}%", callback_data=f"reseller_set_type_preset|{target_reseller_id}|{product_type}|{preset}"))
    keyboard.append(preset_row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback)])

    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer("Enter percentage or tap a preset.")


async def handle_reseller_set_type_preset(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Set a preset discount for a specific product type."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 3 or not params[0].isdigit() or not params[2].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_reseller_id = int(params[0])
    product_type = params[1]
    discount_percent = int(params[2])
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)
    
    conn = None
    username = f"ID_{target_reseller_id}"
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        c.execute("SELECT username FROM users WHERE user_id = ?", (target_reseller_id,))
        user_data = c.fetchone()
        if user_data:
            username = user_data['username'] or username
        
        # Insert or replace the discount
        c.execute("""
            INSERT OR REPLACE INTO reseller_discounts (reseller_user_id, product_type, discount_percentage)
            VALUES (?, ?, ?)
        """, (target_reseller_id, product_type, float(discount_percent)))
        conn.commit()
        
        log_admin_action(admin_id, ACTION_RESELLER_DISCOUNT_ADD, target_user_id=target_reseller_id,
                        reason=f"Type: {product_type}", new_value=discount_percent)
        
    except sqlite3.Error as e:
        logger.error(f"Error setting type discount: {e}")
        await query.answer("Database error!", show_alert=True)
        return
    finally:
        if conn: conn.close()
    
    await query.answer(f"✅ Set {discount_percent}% for {product_type}!")
    
    # Return to per-type view
    # Re-call the handler to refresh the view
    await handle_manage_specific_reseller_discounts(update, context, [str(target_reseller_id)])


async def handle_reseller_edit_discount(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Admin wants to edit an existing discount percentage."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_reseller_id = int(params[0])
    product_type = params[1]
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)

    context.user_data['state'] = 'awaiting_reseller_discount_percent'
    context.user_data['reseller_mgmt_target_id'] = target_reseller_id
    context.user_data['reseller_mgmt_product_type'] = product_type
    context.user_data['reseller_mgmt_mode'] = 'edit'

    msg = f"✏️ **Edit Discount for {emoji} {product_type}**\n\n"
    msg += "Enter the new discount percentage (0-100):"

    cancel_callback = f"reseller_manage_specific|{target_reseller_id}"
    
    # Show quick preset buttons
    keyboard = []
    preset_row = []
    for preset in [5, 10, 15, 20, 25]:
        preset_row.append(InlineKeyboardButton(f"{preset}%", callback_data=f"reseller_set_type_preset|{target_reseller_id}|{product_type}|{preset}"))
    keyboard.append(preset_row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback)])

    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    await query.answer("Enter new percentage or tap a preset.")


async def handle_reseller_percent_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the admin entering the discount percentage via message."""
    admin_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_primary_admin(admin_id): return
    if context.user_data.get("state") != 'awaiting_reseller_discount_percent': return
    if not update.message or not update.message.text: return

    percent_text = update.message.text.strip()
    target_user_id = context.user_data.get('reseller_mgmt_target_id')
    product_type = context.user_data.get('reseller_mgmt_product_type')
    mode = context.user_data.get('reseller_mgmt_mode', 'add')

    if target_user_id is None or not product_type:
        logger.error("State awaiting_reseller_discount_percent missing context data.")
        await send_message_with_retry(context.bot, chat_id, "❌ Error: Context lost. Please start again.", parse_mode=None)
        context.user_data.pop('state', None)
        # Clean up other related context data as well
        context.user_data.pop('reseller_mgmt_target_id', None)
        context.user_data.pop('reseller_mgmt_product_type', None)
        context.user_data.pop('reseller_mgmt_mode', None)
        fallback_cb = "manage_reseller_discounts_select_reseller|0"
        await send_message_with_retry(context.bot, chat_id, "Returning...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=fallback_cb)]]), parse_mode=None)
        return

    back_callback = f"reseller_manage_specific|{target_user_id}"

    try:
        percentage = Decimal(percent_text)
        if not (Decimal('0.0') <= percentage <= Decimal('100.0')):
            raise ValueError("Percentage must be between 0 and 100.")

        conn = None
        old_value = None # For logging edits
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("BEGIN")

            if mode == 'edit':
                c.execute("SELECT discount_percentage FROM reseller_discounts WHERE reseller_user_id = ? AND product_type = ?", (target_user_id, product_type))
                old_res = c.fetchone()
                old_value = old_res['discount_percentage'] if old_res else None

            # Use INSERT OR REPLACE for both add and edit to simplify logic
            # If it's an 'edit' but the row doesn't exist, it becomes an 'add'
            sql = "INSERT OR REPLACE INTO reseller_discounts (reseller_user_id, product_type, discount_percentage) VALUES (?, ?, ?)"
            # Use quantize before converting to float for DB storage if needed, or store as TEXT
            # Storing as REAL (float) is generally fine for percentages if precision issues are acceptable,
            # but TEXT is safer if exact Decimal values are critical. Let's stick with REAL for now.
            params_sql = (target_user_id, product_type, float(percentage.quantize(Decimal("0.1")))) # Store with one decimal place

            # Determine action description based on whether old value existed
            action_desc = ACTION_RESELLER_DISCOUNT_ADD if old_value is None else ACTION_RESELLER_DISCOUNT_EDIT

            result = c.execute(sql, params_sql)
            conn.commit()

            # Log the action
            log_admin_action(
                admin_id=admin_id, action=action_desc, target_user_id=target_user_id,
                reason=f"Type: {product_type}", old_value=old_value, new_value=params_sql[2] # Log the value stored
            )

            action_verb = "set" if old_value is None else "updated"
            await send_message_with_retry(context.bot, chat_id, f"✅ Discount rule {action_verb} for {product_type}: {percentage:.1f}%",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=back_callback)]]), parse_mode=None)

            # Clean up context after successful operation
            context.user_data.pop('state', None); context.user_data.pop('reseller_mgmt_target_id', None)
            context.user_data.pop('reseller_mgmt_product_type', None); context.user_data.pop('reseller_mgmt_mode', None)

        except sqlite3.Error as e: # Catch potential DB errors like IntegrityError implicitly
            logger.error(f"DB error {mode} reseller discount: {e}", exc_info=True)
            if conn and conn.in_transaction: conn.rollback()
            await send_message_with_retry(context.bot, chat_id, "❌ DB Error saving discount rule.", parse_mode=None)
            context.user_data.pop('state', None) # Clear state on error
            # Clean up other related context data on error
            context.user_data.pop('reseller_mgmt_target_id', None)
            context.user_data.pop('reseller_mgmt_product_type', None)
            context.user_data.pop('reseller_mgmt_mode', None)
        finally:
            if conn: conn.close()

    except ValueError:
        await send_message_with_retry(context.bot, chat_id, "❌ Invalid percentage. Enter a number between 0 and 100 (e.g., 10 or 15.5).", parse_mode=None)
        # Keep state awaiting percentage


async def handle_reseller_delete_discount_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    """Handles 'Delete Discount' button press, shows confirmation."""
    query = update.callback_query
    admin_id = query.from_user.id
    if not is_primary_admin(admin_id): return await query.answer("Access Denied.", show_alert=True)
    if not params or len(params) < 2 or not params[0].isdigit():
        await query.answer("Error: Invalid data.", show_alert=True); return

    target_reseller_id = int(params[0])
    product_type = params[1]
    emoji = PRODUCT_TYPES.get(product_type, DEFAULT_PRODUCT_EMOJI)

    # Set confirm action for handle_confirm_yes
    context.user_data["confirm_action"] = f"confirm_delete_reseller_discount|{target_reseller_id}|{product_type}"

    msg = (f"⚠️ Confirm Deletion\n\n"
           f"Delete the discount rule for {emoji} {product_type} for user ID {target_reseller_id}?\n\n"
           f"🚨 This action is irreversible!")
    keyboard = [[InlineKeyboardButton("✅ Yes, Delete Rule", callback_data="confirm_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data=f"reseller_manage_specific|{target_reseller_id}")]]
    await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)


