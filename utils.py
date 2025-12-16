import sqlite3
import time
import os
import logging
import json
import shutil
import tempfile
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
from collections import Counter, defaultdict # Moved higher up

# --- Telegram Imports ---
from telegram import Update, Bot
from telegram.constants import ParseMode
import telegram.error as telegram_error
from telegram.ext import ContextTypes
from telegram import helpers
# -------------------------

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Render Disk Path Configuration ---
RENDER_DISK_MOUNT_PATH = '/mnt/data'
DATABASE_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'shop.db')
MEDIA_DIR = os.path.join(RENDER_DISK_MOUNT_PATH, 'media')
BOT_MEDIA_JSON_PATH = os.path.join(RENDER_DISK_MOUNT_PATH, 'bot_media.json')

# Ensure the base media directory exists on the disk when the script starts
try:
    os.makedirs(MEDIA_DIR, exist_ok=True)
    logger.info(f"Ensured media directory exists: {MEDIA_DIR}")
except OSError as e:
    logger.error(f"Could not create media directory {MEDIA_DIR}: {e}")

logger.info(f"Using Database Path: {DATABASE_PATH}")
logger.info(f"Using Media Directory: {MEDIA_DIR}")
logger.info(f"Using Bot Media Config Path: {BOT_MEDIA_JSON_PATH}")


# --- Configuration Loading (from Environment Variables) ---
# Multi-bot support: TOKENS can be comma-separated list of bot tokens
# Example: TOKENS=token1,token2,token3 or TOKEN=single_token (backward compatible)
TOKENS_STR = os.environ.get("TOKENS", "").strip()
TOKEN = os.environ.get("TOKEN", "").strip()  # Legacy single token support

# Parse multiple tokens
BOT_TOKENS = []
if TOKENS_STR:
    BOT_TOKENS = [t.strip() for t in TOKENS_STR.split(',') if t.strip()]
elif TOKEN:
    BOT_TOKENS = [TOKEN]

# For backward compatibility, keep TOKEN as first bot token
if BOT_TOKENS:
    TOKEN = BOT_TOKENS[0]

# Solana Configuration
SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
SOLANA_ADMIN_WALLET = os.environ.get("SOLANA_ADMIN_WALLET", "")  # Admin wallet for receiving funds
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "") # Base URL for Render app (e.g., https://app-name.onrender.com)
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", None)
PRIMARY_ADMIN_IDS_STR = os.environ.get("PRIMARY_ADMIN_IDS", "")
SECONDARY_ADMIN_IDS_STR = os.environ.get("SECONDARY_ADMIN_IDS", "")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "support")
BASKET_TIMEOUT_MINUTES_STR = os.environ.get("BASKET_TIMEOUT_MINUTES", "15")

# Legacy support for single ADMIN_ID
ADMIN_ID = None
if ADMIN_ID_RAW is not None:
    try: ADMIN_ID = int(ADMIN_ID_RAW)
    except (ValueError, TypeError): logger.error(f"Invalid format for ADMIN_ID: {ADMIN_ID_RAW}. Must be an integer.")

# New multi-primary admin support
PRIMARY_ADMIN_IDS = []
if PRIMARY_ADMIN_IDS_STR:
    try: PRIMARY_ADMIN_IDS = [int(uid.strip()) for uid in PRIMARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("PRIMARY_ADMIN_IDS contains non-integer values. Ignoring.")

# Add legacy ADMIN_ID to PRIMARY_ADMIN_IDS if it exists and isn't already included
if ADMIN_ID is not None and ADMIN_ID not in PRIMARY_ADMIN_IDS:
    PRIMARY_ADMIN_IDS.append(ADMIN_ID)

SECONDARY_ADMIN_IDS = []
if SECONDARY_ADMIN_IDS_STR:
    try: SECONDARY_ADMIN_IDS = [int(uid.strip()) for uid in SECONDARY_ADMIN_IDS_STR.split(',') if uid.strip()]
    except ValueError: logger.warning("SECONDARY_ADMIN_IDS contains non-integer values. Ignoring.")

BASKET_TIMEOUT = 15 * 60 # Default
try:
    BASKET_TIMEOUT = int(BASKET_TIMEOUT_MINUTES_STR) * 60
    if BASKET_TIMEOUT <= 0: logger.warning("BASKET_TIMEOUT_MINUTES non-positive, using default 15 min."); BASKET_TIMEOUT = 15 * 60
except ValueError: logger.warning("Invalid BASKET_TIMEOUT_MINUTES, using default 15 min."); BASKET_TIMEOUT = 15 * 60

# --- Validate essential config ---
if not BOT_TOKENS: 
    logger.critical("CRITICAL ERROR: No bot tokens configured. Set TOKEN or TOKENS environment variable."); 
    raise SystemExit("No bot tokens set.")

# Enhanced token validation for all bots
VALIDATED_BOT_TOKENS = []
for idx, bot_token in enumerate(BOT_TOKENS):
    if ':' not in bot_token:
        logger.critical(f"CRITICAL ERROR: Token {idx+1} format is invalid (missing colon). Token: {bot_token[:10]}...")
        raise SystemExit(f"Token {idx+1} format is invalid.")
    
    token_parts = bot_token.split(':')
    if len(token_parts) != 2 or not token_parts[0].isdigit() or len(token_parts[1]) < 30:
        logger.critical(f"CRITICAL ERROR: Token {idx+1} format is invalid. Expected format: 'bot_id:secret_key'")
        raise SystemExit(f"Token {idx+1} format is invalid.")
    
    VALIDATED_BOT_TOKENS.append({
        'token': bot_token,
        'bot_id': token_parts[0],
        'index': idx
    })
    logger.info(f"Token {idx+1} validation passed. Bot ID: {token_parts[0]}")

# Update BOT_TOKENS with validated list
BOT_TOKENS = VALIDATED_BOT_TOKENS
logger.info(f"🤖 Multi-bot mode: {len(BOT_TOKENS)} bot(s) configured")

if not WEBHOOK_URL: logger.critical("CRITICAL ERROR: WEBHOOK_URL environment variable is missing."); raise SystemExit("WEBHOOK_URL not set.")
if not PRIMARY_ADMIN_IDS: logger.warning("No primary admin IDs configured. Primary admin features disabled.")
if not SOLANA_ADMIN_WALLET: logger.warning("SOLANA_ADMIN_WALLET not set. Crypto payments will be disabled.")
else: logger.info(f"Solana admin wallet configured: {SOLANA_ADMIN_WALLET[:8]}...{SOLANA_ADMIN_WALLET[-4:]}")
logger.info(f"Loaded {len(PRIMARY_ADMIN_IDS)} primary admin ID(s): {PRIMARY_ADMIN_IDS}")
logger.info(f"Loaded {len(SECONDARY_ADMIN_IDS)} secondary admin ID(s): {SECONDARY_ADMIN_IDS}")
logger.info(f"Basket timeout set to {BASKET_TIMEOUT // 60} minutes.")
for bot_info in BOT_TOKENS:
    logger.info(f"Telegram webhook for Bot {bot_info['index']+1} (ID: {bot_info['bot_id']}): {WEBHOOK_URL}/telegram/{bot_info['token']}")


# --- Constants ---
THEMES = {
    "default": {"product": "💎", "basket": "🛒", "review": "📦"},
    "neon": {"product": "💎", "basket": "🛒", "review": "✅"},
    "stealth": {"product": "🌑", "basket": "🛒", "review": "🌟"},
    "nature": {"product": "🌐", "basket": "💥", "review": "🌐"}
}

# ==============================================================
# ===== V V V V V      LANGUAGE DICTIONARY     V V V V V ======
# ==============================================================
# Define LANGUAGES dictionary FIRST
LANGUAGES = {
    # --- English ---
    "en": {
        "native_name": "English",
        # --- General & Menu ---
        "welcome": "👋 Welcome, {username}!\n\n👤 Status: {status} {progress_bar}\n💰 Balance: {balance_str} EUR\n📦 Total Purchases: {purchases}\n🛒 Basket Items: {basket_count}\n\nStart shopping or explore your options below.\n\n⚠️ Note: No refunds.",
        "status_label": "Status",
        "balance_label": "Balance",
        "purchases_label": "Total Purchases",
        "basket_label": "Basket Items",
        "shopping_prompt": "Start shopping or explore your options below.",
        "refund_note": "Note: No refunds.",
        "shop_button": "Shop",
        "profile_button": "Profile",
        "top_up_button": "Top Up",
        "reviews_button": "Reviews",
        "price_list_button": "Price List",
        "language_button": "Language",
        "admin_button": "🔧 Admin Panel",
        "home_button": "Home",
        "back_button": "Back",
        "cancel_button": "Cancel",
        "error_occurred_answer": "An error occurred. Please try again.",
        "success_label": "Success!",
        "error_unexpected": "An unexpected error occurred",

        # --- Shopping Flow ---
        "choose_city_title": "Choose a City",
        "select_location_prompt": "Select your location:",
        "no_cities_available": "No cities available at the moment. Please check back later.",
        "error_city_not_found": "Error: City not found.",
        "choose_district_prompt": "Choose a district:",
        "no_districts_available": "No districts available yet for this city.",
        "back_cities_button": "Back to Cities",
        "error_district_city_not_found": "Error: District or city not found.",
        "select_type_prompt": "Select product type:",
        "no_types_available": "No product types currently available here.",
        "error_loading_types": "Error: Failed to Load Product Types",
        "back_districts_button": "Back to Districts",
        "available_options_prompt": "Available options:",
        "no_items_of_type": "No items of this type currently available here.",
        "error_loading_products": "Error: Failed to Load Products",
        "back_types_button": "Back to Types",
        "price_label": "Price",
        "available_label_long": "Available",
        "available_label_short": "Av",
        "add_to_basket_button": "Add to Basket",
        "error_location_mismatch": "Error: Location data mismatch.",
        "drop_unavailable": "Drop Unavailable! This option just sold out or was reserved by someone else.",
        "error_loading_details": "Error: Failed to Load Product Details",
        "back_options_button": "Back to Options",
        "no_products_in_city_districts": "No products currently available in any district of this city.",
        "error_loading_districts": "Error loading districts. Please try again.",

        # --- Basket & Payment ---
        "added_to_basket": "✅ Item Reserved!\n\n{item} is in your basket for {timeout} minutes! ⏳",
        "expires_label": "Expires in",
        "your_basket_title": "Your Basket",
        "basket_empty": "🛒 Your Basket is Empty!",
        "add_items_prompt": "Add items to start shopping!",
        "items_expired_note": "Items may have expired or were removed.",
        "subtotal_label": "Subtotal",
        "total_label": "Total",
        "pay_now_button": "Pay Now",
        "clear_all_button": "Clear All",
        "view_basket_button": "View Basket",
        "clear_basket_button": "Clear Basket",
        "remove_button_label": "Remove",
        "basket_already_empty": "Basket is already empty.",
        "basket_cleared": "🗑️ Basket Cleared!",
        "pay": "👤 Total to Pay: {amount} EUR",
        "insufficient_balance": "⚠️ Insufficient Balance!\n\nPlease top up to continue! 👤", # Keep generic one for /profile
        "insufficient_balance_pay_option": "⚠️ Insufficient Balance! ({balance} / {required} EUR)",
        "pay_crypto_button": "👤 Pay with Crypto",
        "apply_discount_pay_button": "💥️ Apply Discount Code",
        "skip_discount_button": "⏩ Skip Discount",
        "prompt_discount_or_pay": "Do you have a discount code to apply before paying with crypto?",
        "basket_pay_enter_discount": "Please enter discount code for this purchase:",
        "basket_pay_code_applied": "✅ Code '{code}' applied. New total: {total} EUR. Choose crypto:",
        "basket_pay_code_invalid": "❌ Code invalid: {reason}. Choose crypto to pay {total} EUR:",
        "choose_crypto_for_purchase": "Choose crypto to pay {amount} EUR for your basket:",
        "payment_summary": "👤 Payment Summary",
        "product_label": "Product",
        "price_label": "Price",
        "location_label": "Location",
        "crypto_purchase_success": "Payment Confirmed! Your purchase details are being sent.",
        "crypto_purchase_failed": "Payment Failed/Expired. Your items are no longer reserved.",
        "payment_timeout_notification": "⏰ Payment Timeout: Your payment for basket items has expired after 2 hours. Reserved items have been released.",
        "basket_pay_too_low": "Basket total {basket_total} EUR is below minimum for {currency}.",
        "balance_changed_error": "❌ Transaction failed: Your balance changed. Please check your balance and try again.",
        "order_failed_all_sold_out_balance": "❌ Order Failed: All items in your basket became unavailable during processing. Your balance was not charged.",
        "error_processing_purchase_contact_support": "❌ An error occurred while processing your purchase. Please contact support.",
        "purchase_success": "🎉 Purchase Complete!",
        "sold_out_note": "⚠️ Note: The following items became unavailable during processing and were not included: {items}. You were not charged for these.",
        "leave_review_now": "Leave Review Now",
        "back_basket_button": "Back to Basket",
        "error_adding_db": "Error: Database issue adding item to basket.",
        "error_adding_unexpected": "Error: An unexpected issue occurred.",
        "reseller_discount_label": "Reseller Discount",

        # --- Discounts ---
        "discount_no_items": "Your basket is empty. Add items first.",
        "enter_discount_code_prompt": "Please enter your discount code:",
        "enter_code_answer": "Enter code in chat.",
        "apply_discount_button": "Apply Discount Code",
        "no_code_provided": "No code provided.",
        "discount_code_not_found": "Discount code not found.",
        "discount_code_inactive": "This discount code is inactive.",
        "discount_code_expired": "This discount code has expired.",
        "invalid_code_expiry_data": "Invalid code expiry data.",
        "code_limit_reached": "Code reached usage limit.",
        "internal_error_discount_type": "Internal error processing discount type.",
        "db_error_validating_code": "Database error validating code.",
        "unexpected_error_validating_code": "An unexpected error occurred.",
        "discount_min_order_not_met": "Minimum order amount not met for this discount code.",
        "code_applied_message": "Code '{code}' ({value}) applied. Discount: -{amount} EUR",
        "discount_applied_label": "Discount Applied",
        "discount_value_label": "Value",
        "discount_removed_note": "Discount code {code} removed: {reason}",
        "discount_removed_invalid_basket": "Discount removed (basket changed).",
        "remove_discount_button": "Remove Discount",
        "discount_removed_answer": "Discount removed.",
        "no_discount_answer": "No discount applied.",
        "send_text_please": "Please send the discount code as text.",
        "error_calculating_total": "Error calculating total.",
        "returning_to_basket": "Returning to basket.",
        "basket_empty_no_discount": "Your basket is empty. Cannot apply discount code.",

        # --- Profile & History ---
        "profile_title": "Your Profile",
        "purchase_history_button": "Purchase History",
        "back_profile_button": "Back to Profile",
        "purchase_history_title": "Purchase History",
        "no_purchases_yet": "You haven't made any purchases yet.",
        "recent_purchases_title": "Your Recent Purchases",
        "error_loading_profile": "❌ Error: Unable to load profile data.",

        # --- Language ---
        "language_set_answer": "Language set to {lang}!",
        "error_saving_language": "Error saving language preference.",
        "invalid_language_answer": "Invalid language selected.",
        "language": "🌐 Language", # Also the menu title

        # --- Price List ---
        "no_cities_for_prices": "No cities available to view prices for.",
        "price_list_title": "Price List",
        "select_city_prices_prompt": "Select a city to view available products and prices:",
        # "error_city_not_found": "Error: City not found.", <-- Already exists above
        "price_list_title_city": "Price List: {city_name}",
        "no_products_in_city": "No products currently available in this city.",
        "back_city_list_button": "Back to City List",
        "message_truncated_note": "Message truncated due to length limit. Use 'Shop' for full details.",
        "error_loading_prices_db": "Error: Failed to Load Price List for {city_name}",
        "error_displaying_prices": "Error displaying price list.",
        "error_unexpected_prices": "Error: An unexpected issue occurred while generating the price list.",
        "available_label": "available", # Used in price list

        # --- Reviews ---
        "reviews": "📦 Reviews Menu",
        "view_reviews_button": "View Reviews",
        "leave_review_button": "Leave a Review",
        "enter_review_prompt": "Please type your review message and send it.",
        "enter_review_answer": "Enter your review in the chat.",
        "send_text_review_please": "Please send text only for your review.",
        "review_not_empty": "Review cannot be empty. Please try again or cancel.",
        "review_too_long": "Review is too long (max 1000 characters). Please shorten it.",
        "review_thanks": "Thank you for your review! Your feedback helps us improve.",
        "error_saving_review_db": "Error: Could not save your review due to a database issue.",
        "error_saving_review_unexpected": "Error: An unexpected issue occurred while saving your review.",
        "user_reviews_title": "User Reviews",
        "no_reviews_yet": "No reviews have been left yet.",
        "no_more_reviews": "No more reviews to display.",
        "prev_button": "Prev",
        "next_button": "Next",
        "back_review_menu_button": "Back to Reviews Menu",
        "unknown_date_label": "Unknown Date",
        "error_displaying_review": "Error displaying review",
        "error_updating_review_list": "Error updating review list.",

        # --- Refill / Crypto Payments ---
        "payment_amount_too_low_api": "❌ Payment Amount Too Low: The equivalent of {target_eur_amount} EUR in {currency} \\({crypto_amount}\\) is below the minimum required by the payment provider \\({min_amount} {currency}\\)\\. Please try a higher EUR amount\\.",
        "payment_amount_too_low_with_min_eur": "❌ Payment Amount Too Low: {target_eur_amount} EUR is below the minimum for {currency} payments \\(minimum: {min_eur_amount} EUR\\)\\. Please try a higher amount or select a different cryptocurrency\\.",
        "error_min_amount_fetch": "❌ Error: Could not retrieve minimum payment amount for {currency}\\. Please try again later or select a different currency\\.",
        "invoice_title_refill": "*Top\\-Up Invoice Created*",
        "invoice_title_purchase": "*Payment Invoice Created*",
        "invoice_important_notice": "⚠️ *Important:* Send the exact amount to this address.",
        "invoice_confirmation_notice": "✅ Auto-confirmed in ~1-2 min.",
        "invoice_valid_notice": "⏱️ *Valid for 30 minutes*",
        "min_amount_label": "*Minimum Amount:*",
        "payment_address_label": "*Payment Address:*",
        "amount_label": "*Amount:*",
        "expires_at_label": "*Expires At:*",
        "send_warning_template": "⚠️ *Important:* Send *exactly* this amount of {asset} to this address\\.",
        "overpayment_note": "ℹ️ _Sending more than this amount is okay\\! Your balance will be credited based on the amount received after network confirmation\\._",
        "confirmation_note": "✅ Confirmation is automatic via webhook after network confirmation\\.",
        "invoice_amount_label_text": "Amount",
        "invoice_send_following_amount": "Please send the following amount:",
        "invoice_payment_deadline": "Payment must be completed within 20 minutes of invoice creation.",
            "error_estimate_failed": "❌ Error: Could not estimate crypto amount. Please try again or select a different currency.",
    "error_estimate_currency_not_found": "❌ Error: Currency {currency} not supported for estimation. Please select a different currency.",
    "error_discount_invalid_payment": "❌ Your discount code is no longer valid: {reason}. Please return to your basket to continue without the discount.",
    "error_discount_mismatch_payment": "❌ Payment amount mismatch detected. Please return to your basket and try again.",
        "crypto_payment_disabled": "Top Up is currently disabled.",
        "top_up_title": "Top Up Balance",
        "enter_refill_amount_prompt": "Please reply with the amount in EUR you wish to add to your balance (e.g., 10 or 25.50).",
        "min_top_up_note": "Minimum top up: {amount} EUR",
        "enter_amount_answer": "Enter the top-up amount.",
        "send_amount_as_text": "Please send the amount as text (e.g., 10 or 25.50).",
        "amount_too_low_msg": "Amount too low. Minimum top up is {amount} EUR. Please enter a higher amount.",
        "amount_too_high_msg": "Amount too high. Please enter a lower amount.",
        "invalid_amount_format_msg": "Invalid amount format. Please enter a number (e.g., 10 or 25.50).",
        "unexpected_error_msg": "An unexpected error occurred. Please try again later.",
        "choose_crypto_prompt": "You want to top up {amount} EUR. Please choose the cryptocurrency you want to pay with:",
        "cancel_top_up_button": "Cancel Top Up",
        "preparing_invoice": "⏳ Preparing your payment invoice...",
        "failed_invoice_creation": "❌ Failed to create payment invoice. This could be a temporary issue with the payment provider or an API key problem. Please try again later or contact support.",
        "error_preparing_payment": "❌ An error occurred while preparing the payment details. Please try again later.",
        "top_up_success_title": "✅ Top Up Successful!",
        "amount_added_label": "Amount Added",
        "new_balance_label": "Your new balance",
        "error_nowpayments_api": "❌ Payment API Error: Could not create payment. Please try again later or contact support.",
        "error_invalid_nowpayments_response": "❌ Payment API Error: Invalid response received. Please contact support.",
        "error_nowpayments_api_key": "❌ Payment API Error: Invalid API key. Please contact support.",
        "payment_pending_db_error": "❌ Database Error: Could not record pending payment. Please contact support.",
        "payment_cancelled_or_expired": "Payment Status: Your payment ({payment_id}) was cancelled or expired.",
        "webhook_processing_error": "Webhook Error: Could not process payment update {payment_id}.",
        "webhook_db_update_failed": "Critical Error: Payment {payment_id} confirmed, but DB balance update failed for user {user_id}. Manual action required.",
        "webhook_pending_not_found": "Webhook Warning: Received update for payment ID {payment_id}, but no pending deposit found in DB.",
        "webhook_price_fetch_error": "Webhook Error: Could not fetch price for {currency} to confirm EUR value for payment {payment_id}.",
        "payment_cancelled_user": "Payment cancelled. Reserved items (if any) have been released.",
        "payment_cancel_error": "Could not cancel payment (already processed or context lost).",
        "cancel_payment_button": "Cancel Payment",
        "proceeding_to_payment_answer": "Proceeding to payment options...",
        "credit_overpayment_purchase": "✅ Your purchase was successful! Additionally, an overpayment of {amount} EUR has been credited to your balance. Your new balance is {new_balance} EUR.",
        "credit_underpayment_purchase": "ℹ️ Your purchase failed due to underpayment, but the received amount ({amount} EUR) has been credited to your balance. Your new balance is {new_balance} EUR.",
        "crypto_purchase_underpaid_credited": "⚠️ Purchase Failed: Underpayment detected. Amount needed was {needed_eur} EUR. Your balance has been credited with the received value ({paid_eur} EUR). Your items were not delivered.",
        "credit_refill": "✅ Your balance has been credited by {amount} EUR. Reason: {reason}. New balance: {new_balance} EUR.",


        # --- Admin ---
        "admin_menu": "🔧 Admin Panel\n\nManage the bot from here:",
        "admin_select_city": "🏳️ Select City to Edit\n\nChoose a city:",
        "admin_select_district": "🏘️ Select District in {city}\n\nPick a district:",
        "admin_select_type": "💎 Select Product Type\n\nChoose or create a type:",
        "admin_choose_action": "📦 Manage {type} in {city}, {district}\n\nWhat would you like to do?",
        "set_media_prompt_plain": "📦 Send a photo, video, or GIF to display above all messages:",
        "state_error": "❌ Error: Invalid State\n\nPlease start the 'Add New Product' process again from the Admin Panel.",
        "support": "📞 Need Help?\n\nContact {support} for assistance!",
        "file_download_error": "❌ Error: Failed to Download Media\n\nPlease try again or contact {support}. ",
        "admin_enter_type_emoji": "✅️ Please reply with a single emoji for the product type:",
        "admin_type_emoji_set": "Emoji set to {emoji}.",
        "admin_edit_type_emoji_button": "✅️ Change Emoji",
        "admin_invalid_emoji": "❌ Invalid input. Please send a single emoji.",
        "admin_type_emoji_updated": "✅ Emoji updated successfully for {type_name}!",
        "admin_edit_type_menu": "💥 Editing Type: {type_name}\n\nCurrent Emoji: {emoji}\nDescription: {description}\n\nWhat would you like to do?", # Added {description}
        "admin_edit_type_desc_button": "📦 Edit Description", #<<< NEW
        # --- Broadcast Translations ---
        "broadcast_select_target": "📦 Broadcast Message\n\nSelect the target audience:",
        "broadcast_target_all": "�Ÿ‘� All Users",
        "broadcast_target_city": "🏳️ By Last Purchased City",
        "broadcast_target_status": "�Ÿ‘‘ By User Status",
        "broadcast_target_inactive": "⏳ By Inactivity (Days)",
        "broadcast_select_city_target": "🏳️ Select City to Target\n\nUsers whose last purchase was in:",
        "broadcast_select_status_target": "�Ÿ‘‘ Select Status to Target:",
        "broadcast_status_vip": "VIP �Ÿ‘‘",
        "broadcast_status_regular": "Regular ⭐",
        "broadcast_status_new": "New 🌐",
        "broadcast_enter_inactive_days": "⏳ Enter Inactivity Period\n\nPlease reply with the number of days since the user's last purchase (or since registration if no purchases). Users inactive for this many days or more will receive the message.",
        "broadcast_invalid_days": "❌ Invalid number of days. Please enter a positive whole number.",
        "broadcast_days_too_large": "❌ Number of days is too large. Please enter a smaller number.",
        "broadcast_ask_message": "📦 Now send the message content (text, photo, video, or GIF with caption):",
        "broadcast_confirm_title": "📦 Confirm Broadcast",
        "broadcast_confirm_target_all": "Target: All Users",
        "broadcast_confirm_target_city": "Target: Last Purchase in {city}",
        "broadcast_confirm_target_status": "Target: Status - {status}",
        "broadcast_confirm_target_inactive": "Target: Inactive >= {days} days",
        "broadcast_confirm_preview": "Preview:",
        "broadcast_confirm_ask": "Send this message?",
        "broadcast_no_users_found_target": "⚠️ Broadcast Warning: No users found matching the target criteria.",
        # --- User Management Translations ---
        "manage_users_title": "�Ÿ‘� Manage Users",
        "manage_users_prompt": "Select a user to view details or manage:",
        "manage_users_no_users": "No users found.",
        "view_user_profile_title": "�Ÿ‘� User Profile: @{username} (ID: {user_id})",
        "user_profile_status": "Status",
        "user_profile_balance": "Balance",
        "user_profile_purchases": "Total Purchases",
        "user_profile_banned": "Banned Status",
        "user_profile_is_banned": "Yes ⚠",
        "user_profile_not_banned": "No ✅",
        "user_profile_button_adjust_balance": "👤 Adjust Balance",
        "user_profile_button_ban": "🚨 Ban User",
        "user_profile_button_unban": "✅ Unban User",
        "user_profile_button_back_list": "�✅️ Back to User List",
        "adjust_balance_prompt": "Reply with the amount to adjust balance for @{username} (ID: {user_id}).\nUse a positive number to add (e.g., 10.50) or a negative number to subtract (e.g., -5.00).",
        "adjust_balance_reason_prompt": "Please reply with a brief reason for this balance adjustment ({amount} EUR):",
        "adjust_balance_invalid_amount": "❌ Invalid amount. Please enter a non-zero number (e.g., 10.5 or -5).",
        "adjust_balance_reason_empty": "❌ Reason cannot be empty. Please provide a reason.",
        "adjust_balance_success": "✅ Balance adjusted successfully for @{username}. New balance: {new_balance} EUR.",
        "adjust_balance_db_error": "❌ Database error adjusting balance.",
        "ban_success": "🚨 User @{username} (ID: {user_id}) has been banned.",
        "unban_success": "✅ User @{username} (ID: {user_id}) has been unbanned.",
        "ban_db_error": "❌ Database error updating ban status.",
        "ban_cannot_ban_admin": "❌ Cannot ban the primary admin.",
       
        "manage_welcome_title": "⚙️ Manage Welcome Messages",
        "manage_welcome_prompt": "Select a template to manage or activate:",
        "welcome_template_active": " (Active ✅)",
        "welcome_template_inactive": "",
        "welcome_button_activate": "✅ Activate",
        "welcome_button_edit": "✅️ Edit",
        "welcome_button_delete": "�Ÿ—‘️ Delete",
        "welcome_button_add_new": "➕ Add New Template",
        "welcome_button_reset_default": "�Ÿ”„ Reset to Built-in Default",
        "welcome_button_edit_text": "Edit Text",
        "welcome_button_edit_desc": "Edit Description",
        "welcome_button_preview": "�Ÿ‘�️ Preview",
        "welcome_button_save": "👤 Save Template",
        "welcome_activate_success": "✅ Template '{name}' activated.",
        "welcome_activate_fail": "❌ Failed to activate template '{name}'.",
        "welcome_add_name_prompt": "Enter a unique short name for the new template (e.g., 'default', 'promo_weekend'):",
        "welcome_add_name_exists": "❌ Error: A template with the name '{name}' already exists.",
        "welcome_add_text_prompt": "Template Name: {name}\n\nPlease reply with the full welcome message text. Available placeholders:\n`{placeholders}`", # Escaped placeholders
        "welcome_add_description_prompt": "Optional: Enter a short description for this template (admin view only). Send '-' to skip.",
        "welcome_add_success": "✅ Welcome message template '{name}' added.",
        "welcome_add_fail": "❌ Failed to add welcome message template.",
        "welcome_edit_text_prompt": "Editing Text for '{name}'. Current text:\n\n{current_text}\n\nPlease reply with the new text. Available placeholders:\n`{placeholders}`", # Escaped placeholders
        "welcome_edit_description_prompt": "Editing description for '{name}'. Current: '{current_desc}'.\n\nEnter new description or send '-' to keep current.",
        "welcome_edit_success": "✅ Template '{name}' updated.",
        "welcome_edit_fail": "❌ Failed to update template '{name}'.",
        "welcome_delete_confirm_title": "⚠️ Confirm Deletion",
        "welcome_delete_confirm_text": "Are you sure you want to delete the welcome message template named '{name}'?",
        "welcome_delete_confirm_active": "\n\n🚨 WARNING: This is the currently active template! Deleting it will revert to the default built-in message.",
        "welcome_delete_confirm_last": "\n\n🚨 WARNING: This is the last template! Deleting it will revert to the default built-in message.",
        "welcome_delete_button_yes": "✅ Yes, Delete Template",
        "welcome_delete_success": "✅ Template '{name}' deleted.",
        "welcome_delete_fail": "❌ Failed to delete template '{name}'.",
        "welcome_delete_not_found": "❌ Template '{name}' not found for deletion.",
        "welcome_cannot_delete_active": "❌ Cannot delete the active template. Activate another first.",
        "welcome_reset_confirm_title": "⚠️ Confirm Reset",
        "welcome_reset_confirm_text": "Are you sure you want to reset the text of the 'default' template to the built-in version and activate it?",
        "welcome_reset_button_yes": "✅ Yes, Reset & Activate",
        "welcome_reset_success": "✅ 'default' template reset and activated.",
        "welcome_reset_fail": "❌ Failed to reset 'default' template.",
        "welcome_preview_title": "--- Welcome Message Preview ---",
        "welcome_preview_name": "Name",
        "welcome_preview_desc": "Desc",
        "welcome_preview_confirm": "Save this template?",
        "welcome_save_error_context": "❌ Error: Save data lost. Cannot save template.",
        "welcome_invalid_placeholder": "⚠️ Formatting Error! Missing placeholder: `{key}`\n\nRaw Text:\n{text}",
        "welcome_formatting_error": "⚠️ Unexpected Formatting Error!\n\nRaw Text:\n{text}",
    },
    # --- Lithuanian ---
    "lt": {
        "native_name": "Lietuvių",
        # --- General & Menu ---
        "welcome": "�Ÿ‘‹ Sveiki, {username}!\n\n�Ÿ‘� Būsena: {status} {progress_bar}\n👤 Balansas: {balance_str} EUR\n📦 Viso pirkimų: {purchases}\n🛒 Krepšelyje: {basket_count} prek�—(s)\n\nPrad�—kite apsipirkti arba naršykite parinktis žemiau.\n\n⚠️ Pastaba: Pinigai negr✅žinami.",
        "status_label": "Būsena",
        "balance_label": "Balansas",
        "purchases_label": "Viso pirkimų",
        "basket_label": "Krepšelyje",
        "shopping_prompt": "Prad�—kite apsipirkti arba naršykite parinktis žemiau.",
        "refund_note": "Pastaba: Pinigai negr✅žinami.",
        "shop_button": "Parduotuv�—", # <-- Example Translation
        "profile_button": "Profilis", # <-- Example Translation
        "top_up_button": "Papildyti", # <-- Example Translation
        "reviews_button": "Atsiliepimai", # <-- Example Translation
        "price_list_button": "Kainoraštis", # <-- Example Translation
        "language_button": "Kalba", # <-- Example Translation
        "admin_button": "🔧 Admino Panel�—",
        "home_button": "Pradžia", # <-- Example Translation
        "back_button": "Atgal", # <-- Example Translation
        "cancel_button": "Atšaukti", # <-- Example Translation
        "error_occurred_answer": "Įvyko klaida. Bandykite dar kart✅.",
        "success_label": "Pavyko!",
        "error_unexpected": "Įvyko netik�—ta klaida",

        # --- Shopping Flow ---
        "choose_city_title": "Pasirinkite miest✅",
        "select_location_prompt": "Pasirinkite savo viet✅:",
        "no_cities_available": "Šiuo metu n�—ra miestų. Patikrinkite v�—liau.",
        "error_city_not_found": "Klaida: Miestas nerastas.",
        "choose_district_prompt": "Pasirinkite rajon✅:",
        "no_districts_available": "Šiame mieste dar n�—ra rajonų.",
        "back_cities_button": "Atgal į miestus",
        "error_district_city_not_found": "Klaida: Rajonas ar miestas nerastas.",
        "select_type_prompt": "Pasirinkite produkto tip✅:",
        "no_types_available": "Šiuo metu čia n�—ra šio tipo produktų.",
        "error_loading_types": "Klaida: Nepavyko įkelti produktų tipų",
        "back_districts_button": "Atgal į rajonus",
        "available_options_prompt": "Galimos parinktys:",
        "no_items_of_type": "Šiuo metu čia n�—ra šio tipo prekių.",
        "error_loading_products": "Klaida: Nepavyko įkelti produktų",
        "back_types_button": "Atgal į tipus",
        "price_label": "Kaina",
        "available_label_long": "Yra",
        "available_label_short": "Yra",
        "add_to_basket_button": "Į krepšelį",
        "error_location_mismatch": "Klaida: Vietos duomenų neatitikimas.",
        "drop_unavailable": "Prek�— neprieinama! Ši parinktis k✅ tik buvo parduota ar rezervuota.",
        "error_loading_details": "Klaida: Nepavyko įkelti produkto detalių",
        "back_options_button": "Atgal į parinktis",
        "no_products_in_city_districts": "Šiuo metu n�—ra produktų jokiuose šio miesto rajonuose.",
        "error_loading_districts": "Klaida įkeliant rajonus. Bandykite dar kart✅.",

        # --- Basket & Payment ---
        "added_to_basket": "✅ Prek�— Rezervuota!\n\n{item} yra jūsų krepšelyje {timeout} minutes! ⏳",
        "expires_label": "Galioja iki",
        "your_basket_title": "Jūsų krepšelis",
        "basket_empty": "🛒 Jūsų krepšelis tuščias!",
        "add_items_prompt": "Prid�—kite prekių, kad prad�—tum�—te apsipirkti!",
        "items_expired_note": "Prek�—s gal�—jo baigtis arba buvo pašalintos.",
        "subtotal_label": "Tarpin�— suma",
        "total_label": "Viso",
        "pay_now_button": "Mok�—ti dabar",
        "clear_all_button": "Išvalyti visk✅",
        "view_basket_button": "Peržiūr�—ti krepšelį",
        "clear_basket_button": "Išvalyti krepšelį",
        "remove_button_label": "Pašalinti",
        "basket_already_empty": "Krepšelis jau tuščias.",
        "basket_cleared": "�Ÿ—‘️ Krepšelis išvalytas!",
        "pay": "👤 Mok�—ti viso: {amount} EUR",
        "insufficient_balance": "⚠️ Nepakankamas balansas!\n\nPrašome papildyti, kad t�™stum�—te! 👤",
        "insufficient_balance_pay_option": "⚠️ Nepakankamas balansas! ({balance} / {required} EUR)",
        "pay_crypto_button": "👤 Mok�—ti Crypto",
        "apply_discount_pay_button": "💥️ Panaudoti nuolaidos kod✅",
        "skip_discount_button": "⏩ Praleisti nuolaid✅",
        "prompt_discount_or_pay": "Ar turite nuolaidos kod✅, kurį norite panaudoti prieš mokant kriptovaliuta?",
        "basket_pay_enter_discount": "Įveskite nuolaidos kod✅ šiam pirkiniui:",
        "basket_pay_code_applied": "✅ Kodas '{code}' pritaikytas. Nauja suma: {total} EUR. Pasirinkite kriptovaliut✅:",
        "basket_pay_code_invalid": "❌ Kodas negalioja: {reason}. Pasirinkite kriptovaliut✅ mok�—ti {total} EUR:",
        "choose_crypto_for_purchase": "Pasirinkite kriptovaliut✅ mok�—ti {amount} EUR už jūsų krepšelį:",
        "payment_summary": "👤 Mok�—jimo suvestin�—",
        "product_label": "Prek�—",
        "price_label": "Kaina",
        "location_label": "Vieta",
        "crypto_purchase_success": "Mok�—jimas patvirtintas! Jūsų pirkimo detal�—s siunčiamos.",
        "crypto_purchase_failed": "Mok�—jimas nepavyko/baig�—si. Jūsų prek�—s neb�—ra rezervuotos.",
        "payment_timeout_notification": "⏰ Mok�—jimo Laikas Baig�—si: Jūsų mok�—jimas už krepšelio prekes pasibaig�— po 2 valandų. Rezervuotos prek�—s buvo atlaisvintos.",
        "basket_pay_too_low": "Krepšelio suma {basket_total} EUR yra mažesn�— nei minimali {currency}.",
        "balance_changed_error": "❌ Transakcija nepavyko: Jūsų balansas pasikeit�—. Patikrinkite balans✅ ir bandykite dar kart✅.",
        "order_failed_all_sold_out_balance": "❌ Užsakymas nepavyko: Visos prek�—s krepšelyje tapo neprieinamos apdorojimo metu. Jūsų balansas nebuvo apmokestintas.",
        "error_processing_purchase_contact_support": "❌ Apdorojant jūsų pirkim✅ įvyko klaida. Susisiekite su pagalba.",
        "purchase_success": "�ŸŽ‰ Pirkimas baigtas!",
        "sold_out_note": "⚠️ Pastaba: Šios prek�—s tapo neprieinamos apdorojimo metu ir nebuvo įtrauktos: {items}. Už jas nebuvote apmokestinti.",
        "leave_review_now": "Palikti atsiliepim✅ dabar",
        "back_basket_button": "Atgal į krepšelį",
        "error_adding_db": "Klaida: Duomenų baz�—s problema dedant prek�™ į krepšelį.",
        "error_adding_unexpected": "Klaida: Įvyko netik�—ta problema.",
        "reseller_discount_label": "Perpardav�—jo nuolaida",

        # --- Discounts ---
        "discount_no_items": "Jūsų krepšelis tuščias. Pirmiausia prid�—kite prekių.",
        "enter_discount_code_prompt": "Įveskite savo nuolaidos kod✅:",
        "enter_code_answer": "Įveskite kod✅ pokalbyje.",
        "apply_discount_button": "Pritaikyti nuolaidos kod✅",
        "no_code_provided": "Kodas neįvestas.",
        "discount_code_not_found": "Nuolaidos kodas nerastas.",
        "discount_code_inactive": "Šis nuolaidos kodas neaktyvus.",
        "discount_code_expired": "Šio nuolaidos kodo galiojimas baig�—si.",
        "invalid_code_expiry_data": "Neteisingi kodo galiojimo duomenys.",
        "code_limit_reached": "Kodas pasiek�— naudojimo limit✅.",
        "internal_error_discount_type": "Vidin�— klaida apdorojant nuolaidos tip✅.",
        "db_error_validating_code": "Duomenų baz�—s klaida tikrinant kod✅.",
        "unexpected_error_validating_code": "Įvyko netik�—ta klaida.",
        "discount_min_order_not_met": "Šiam nuolaidos kodui nepasiekta minimali užsakymo suma.",
        "code_applied_message": "Kodas '{code}' ({value}) pritaikytas. Nuolaida: -{amount} EUR",
        "discount_applied_label": "Pritaikyta nuolaida",
        "discount_value_label": "Vert�—",
        "discount_removed_note": "Nuolaidos kodas {code} pašalintas: {reason}",
        "discount_removed_invalid_basket": "Nuolaida pašalinta (krepšelis pasikeit�—).",
        "remove_discount_button": "Pašalinti nuolaid✅",
        "discount_removed_answer": "Nuolaida pašalinta.",
        "no_discount_answer": "Nuolaida nepritaikyta.",
        "send_text_please": "Siųskite nuolaidos kod✅ kaip tekst✅.",
        "error_calculating_total": "Klaida skaičiuojant sum✅.",
        "returning_to_basket": "Grįžtama į krepšelį.",
        "basket_empty_no_discount": "Krepšelis tuščias. Negalima pritaikyti nuolaidos kodo.",

        # --- Profile & History ---
        "profile_title": "Jūsų profilis",
        "purchase_history_button": "Pirkimų istorija",
        "back_profile_button": "Atgal į profilį",
        "purchase_history_title": "Pirkimų istorija",
        "no_purchases_yet": "Dar neatlikote jokių pirkimų.",
        "recent_purchases_title": "Jūsų paskutiniai pirkimai",
        "error_loading_profile": "❌ Klaida: Nepavyko įkelti profilio duomenų.",

        # --- Language ---
        "language_set_answer": "Kalba nustatyta į {lang}!",
        "error_saving_language": "Klaida išsaugant kalbos nustatym✅.",
        "invalid_language_answer": "Pasirinkta neteisinga kalba.",
        "language": "🌐 Kalba", # Menu title

        # --- Price List ---
        "no_cities_for_prices": "N�—ra miestų, kuriuose būtų galima peržiūr�—ti kainas.",
        "price_list_title": "Kainoraštis",
        "select_city_prices_prompt": "Pasirinkite miest✅, kad pamatytum�—te galimus produktus ir kainas:",
        "price_list_title_city": "Kainoraštis: {city_name}",
        "no_products_in_city": "Šiame mieste šiuo metu n�—ra produktų.",
        "back_city_list_button": "Atgal į miestų s✅raš✅",
        "message_truncated_note": "Žinut�— sutrumpinta d�—l ilgio limito. Naudokite 'Parduotuv�—' pilnai informacijai.",
        "error_loading_prices_db": "Klaida: Nepavyko įkelti kainoraščio {city_name}",
        "error_displaying_prices": "Klaida rodant kainoraštį.",
        "error_unexpected_prices": "Klaida: Įvyko netik�—ta problema generuojant kainoraštį.",
        "available_label": "yra", # Used in price list

        # --- Reviews ---
        "reviews": "📦 Atsiliepimų Meniu",
        "view_reviews_button": "Peržiūr�—ti atsiliepimus",
        "leave_review_button": "Palikti atsiliepim✅",
        "enter_review_prompt": "Įveskite savo atsiliepimo žinut�™ ir išsiųskite.",
        "enter_review_answer": "Įveskite savo atsiliepim✅ pokalbyje.",
        "send_text_review_please": "Siųskite tik tekst✅ savo atsiliepimui.",
        "review_not_empty": "Atsiliepimas negali būti tuščias. Bandykite dar kart✅ arba atšaukite.",
        "review_too_long": "Atsiliepimas per ilgas (maks. 1000 simbolių). Prašome sutrumpinti.",
        "review_thanks": "Ačiū už jūsų atsiliepim✅! Jūsų nuomon�— padeda mums tobul�—ti.",
        "error_saving_review_db": "Klaida: Nepavyko išsaugoti jūsų atsiliepimo d�—l duomenų baz�—s problemos.",
        "error_saving_review_unexpected": "Klaida: Įvyko netik�—ta problema saugant jūsų atsiliepim✅.",
        "user_reviews_title": "Vartotojų atsiliepimai",
        "no_reviews_yet": "Dar n�—ra paliktų atsiliepimų.",
        "no_more_reviews": "Neb�—ra daugiau atsiliepimų.",
        "prev_button": "Ankst.",
        "next_button": "Kitas",
        "back_review_menu_button": "Atgal į Atsiliepimų Meniu",
        "unknown_date_label": "Nežinoma data",
        "error_displaying_review": "Klaida rodant atsiliepim✅",
        "error_updating_review_list": "Klaida atnaujinant atsiliepimų s✅raš✅.",

        # --- Refill / Crypto Payments ---
        "payment_amount_too_low_api": "❌ Mok�—jimo Suma Per Maža: {target_eur_amount} EUR atitikmuo {currency} \\({crypto_amount}\\) yra mažesnis už minimalų reikalaujam✅ mok�—jimo teik�—jo \\({min_amount} {currency}\\)\\. Bandykite didesn�™ EUR sum✅\\.",
        "payment_amount_too_low_with_min_eur": "❌ Mok�—jimo Suma Per Maža: {target_eur_amount} EUR yra mažesn�— už minimalų {currency} mok�—jimų sum✅ \\(minimalus: {min_eur_amount} EUR\\)\\. Bandykite didesn�™ sum✅ arba pasirinkite kit✅ kriptovaliut✅\\.",
        "error_min_amount_fetch": "❌ Klaida: Nepavyko gauti minimalios mok�—jimo sumos {currency}\\. Bandykite v�—liau arba pasirinkite kit✅ valiut✅\\.",
        "invoice_title_refill": "*S✅skaita Papildymui Sukurta*",
        "invoice_title_purchase": "*S✅skaita Pirkimui Sukurta*",
        "invoice_important_notice": "⚠️ *Svarbu:* Siųskite tiksli✅ sum✅ šiuo adresu.",
        "invoice_confirmation_notice": "✅ Auto-patvirtinta per ~1-2 min.",
        "invoice_valid_notice": "⏱️ *Galioja 30 minučių*",
        "min_amount_label": "*Minimali Suma:*",
        "payment_address_label": "*Mok�—jimo Adresas:*",
        "amount_label": "*Suma:*",
        "expires_at_label": "*Galioja iki:*",
        "send_warning_template": "⚠️ *Svarbu:* Siųskite *tiksliai* ši✅ {asset} sum✅ šiuo adresu\\.",
        "overpayment_note": "ℹ️ _Siųsti daugiau nei nurodyta suma yra gerai\\! Jūsų balansas bus papildytas pagal gaut✅ sum✅ po tinklo patvirtinimo\\._",
        "confirmation_note": "✅ Patvirtinimas automatinis per webhook po tinklo patvirtinimo\\.",
        "invoice_amount_label_text": "Suma",
        "invoice_send_following_amount": "Prašome siųsti ši✅ sum✅:",
        "invoice_payment_deadline": "Mok�—jimas turi būti atliktas per 20 minučių nuo s✅skaitos sukūrimo.",
        "error_estimate_failed": "❌ Klaida: Nepavyko įvertinti kriptovaliutos sumos. Bandykite dar kart✅ arba pasirinkite kit✅ valiut✅.",
        "error_estimate_currency_not_found": "❌ Klaida: Valiuta {currency} nepalaikoma įvertinimui. Pasirinkite kit✅ valiut✅.",
        "error_discount_invalid_payment": "❌ Jūsų nuolaidos kodas nebegalioja: {reason}. Grįžkite į krepšelį, kad t�™stum�—te be nuolaidos.",
        "error_discount_mismatch_payment": "❌ Aptiktas mok�—jimo sumos neatitikimas. Grįžkite į krepšelį ir bandykite dar kart✅.",
        "crypto_payment_disabled": "Balanso papildymas šiuo metu išjungtas.",
        "top_up_title": "Papildyti balans✅",
        "enter_refill_amount_prompt": "Atsakykite su suma EUR, kuri✅ norite prid�—ti prie balanso (pvz., 10 arba 25.50).",
        "min_top_up_note": "Minimalus papildymas: {amount} EUR",
        "enter_amount_answer": "Įveskite papildymo sum✅.",
        "send_amount_as_text": "Siųskite sum✅ kaip tekst✅ (pvz., 10 arba 25.50).",
        "amount_too_low_msg": "Suma per maža. Minimalus papildymas yra {amount} EUR. Įveskite didesn�™ sum✅.",
        "amount_too_high_msg": "Suma per didel�—. Įveskite mažesn�™ sum✅.",
        "invalid_amount_format_msg": "Neteisingas sumos formatas. Įveskite skaičių (pvz., 10 arba 25.50).",
        "unexpected_error_msg": "Įvyko netik�—ta klaida. Bandykite v�—liau.",
        "choose_crypto_prompt": "Norite papildyti {amount} EUR. Pasirinkite kriptovaliut✅, kuria norite mok�—ti:",
        "cancel_top_up_button": "Atšaukti papildym✅",
        "preparing_invoice": "⏳ Ruošiama jūsų mok�—jimo s✅skaita...",
        "failed_invoice_creation": "❌ Nepavyko sukurti mok�—jimo s✅skaitos. Tai gali būti laikina problema su mok�—jimo teik�—ju arba API rakto problema. Bandykite v�—liau arba susisiekite su pagalba.",
        "error_preparing_payment": "❌ Ruošiant mok�—jimo detales įvyko klaida. Bandykite v�—liau.",
        "top_up_success_title": "✅ Papildymas S�—kmingas!",
        "amount_added_label": "Prid�—ta suma",
        "new_balance_label": "Jūsų naujas balansas",
        "error_nowpayments_api": "❌ Mok�—jimo API Klaida: Nepavyko sukurti mok�—jimo. Bandykite v�—liau arba susisiekite su pagalba.",
        "error_invalid_nowpayments_response": "❌ Mok�—jimo API Klaida: Gautas neteisingas atsakymas. Susisiekite su pagalba.",
        "error_nowpayments_api_key": "❌ Mok�—jimo API Klaida: Neteisingas API raktas. Susisiekite su pagalba.",
        "payment_pending_db_error": "❌ Duomenų Baz�—s Klaida: Nepavyko įrašyti laukiančio mok�—jimo. Susisiekite su pagalba.",
        "payment_cancelled_or_expired": "Mok�—jimo Būsena: Jūsų mok�—jimas ({payment_id}) buvo atšauktas arba baig�—si galiojimas.",
        "webhook_processing_error": "Webhook Klaida: Nepavyko apdoroti mok�—jimo atnaujinimo {payment_id}.",
        "webhook_db_update_failed": "Kritin�— Klaida: Mok�—jimas {payment_id} patvirtintas, bet DB balanso atnaujinimas vartotojui {user_id} nepavyko. Reikalingas rankinis veiksmas.",
        "webhook_pending_not_found": "Webhook Įsp�—jimas: Gautas mok�—jimo ID {payment_id} atnaujinimas, bet DB nerasta laukiančio įrašo.",
        "webhook_price_fetch_error": "Webhook Klaida: Nepavyko gauti {currency} kainos patvirtinti EUR vert�™ mok�—jimui {payment_id}.",
        "payment_cancelled_user": "Mok�—jimas atšauktas. Rezervuotos prek�—s (jei buvo) paleistos.",
        "payment_cancel_error": "Nepavyko atšaukti mok�—jimo (jau apdorotas arba prarastas kontekstas).",
        "cancel_payment_button": "Atšaukti mok�—jim✅",
        "proceeding_to_payment_answer": "Pereinama prie mok�—jimo parinkčių...",
        "credit_overpayment_purchase": "✅ Jūsų pirkimas buvo s�—kmingas! Papildomai, permoka {amount} EUR buvo įskaityta į jūsų balans✅. Jūsų naujas balansas: {new_balance} EUR.",
        "credit_underpayment_purchase": "ℹ️ Jūsų pirkimas nepavyko d�—l nepakankamo mok�—jimo, tačiau gauta suma ({amount} EUR) buvo įskaityta į jūsų balans✅. Jūsų naujas balansas: {new_balance} EUR.",
        "crypto_purchase_underpaid_credited": "⚠️ Pirkimas nepavyko: Aptiktas nepakankamas mok�—jimas. Reikalinga suma buvo {needed_eur} EUR. Jūsų balansas buvo papildytas gauta verte ({paid_eur} EUR). Jūsų prek�—s nebuvo pristatytos.",
        "credit_refill": "✅ Jūsų balansas buvo papildytas {amount} EUR. Priežastis: {reason}. Naujas balansas: {new_balance} EUR.",


        # --- Admin ---
        "admin_menu": "🔧 Admin Panel\n\nManage the bot from here:",
        "admin_select_city": "🏳️ Select City to Edit\n\nChoose a city:",
        "admin_select_district": "🏘️ Select District in {city}\n\nPick a district:",
        "admin_select_type": "💎 Select Product Type\n\nChoose or create a type:",
        "admin_choose_action": "📦 Manage {type} in {city}, {district}\n\nWhat would you like to do?",
        "set_media_prompt_plain": "📦 Send a photo, video, or GIF to display above all messages:",
        "state_error": "❌ Error: Invalid State\n\nPlease start the 'Add New Product' process again from the Admin Panel.",
        "support": "📞 Need Help?\n\nContact {support} for assistance!",
        "file_download_error": "❌ Error: Failed to Download Media\n\nPlease try again or contact {support}. ",
        "admin_enter_type_emoji": "✅️ Please reply with a single emoji for the product type:",
        "admin_type_emoji_set": "Emoji set to {emoji}.",
        "admin_edit_type_emoji_button": "✅️ Change Emoji",
        "admin_invalid_emoji": "❌ Invalid input. Please send a single emoji.",
        "admin_type_emoji_updated": "✅ Emoji updated successfully for {type_name}!",
        "admin_edit_type_menu": "💥 Editing Type: {type_name}\n\nCurrent Emoji: {emoji}\nDescription: {description}\n\nWhat would you like to do?", # Added {description}
        "admin_edit_type_desc_button": "📦 Edit Description", #<<< NEW
        # --- Broadcast Translations ---
        "broadcast_select_target": "📦 Broadcast Message\n\nSelect the target audience:",
        "broadcast_target_all": "�Ÿ‘� All Users",
        "broadcast_target_city": "🏳️ By Last Purchased City",
        "broadcast_target_status": "�Ÿ‘‘ By User Status",
        "broadcast_target_inactive": "⏳ By Inactivity (Days)",
        "broadcast_select_city_target": "🏳️ Select City to Target\n\nUsers whose last purchase was in:",
        "broadcast_select_status_target": "�Ÿ‘‘ Select Status to Target:",
        "broadcast_status_vip": "VIP �Ÿ‘‘",
        "broadcast_status_regular": "Regular ⭐",
        "broadcast_status_new": "New 🌐",
        "broadcast_enter_inactive_days": "⏳ Enter Inactivity Period\n\nPlease reply with the number of days since the user's last purchase (or since registration if no purchases). Users inactive for this many days or more will receive the message.",
        "broadcast_invalid_days": "❌ Invalid number of days. Please enter a positive whole number.",
        "broadcast_days_too_large": "❌ Number of days is too large. Please enter a smaller number.",
        "broadcast_ask_message": "📦 Now send the message content (text, photo, video, or GIF with caption):",
        "broadcast_confirm_title": "📦 Confirm Broadcast",
        "broadcast_confirm_target_all": "Target: All Users",
        "broadcast_confirm_target_city": "Target: Last Purchase in {city}",
        "broadcast_confirm_target_status": "Target: Status - {status}",
        "broadcast_confirm_target_inactive": "Target: Inactive >= {days} days",
        "broadcast_confirm_preview": "Preview:",
        "broadcast_confirm_ask": "Send this message?",
        "broadcast_no_users_found_target": "⚠️ Broadcast Warning: No users found matching the target criteria.",
        # --- User Management Translations ---
        "manage_users_title": "�Ÿ‘� Manage Users",
        "manage_users_prompt": "Select a user to view details or manage:",
        "manage_users_no_users": "No users found.",
        "view_user_profile_title": "�Ÿ‘� User Profile: @{username} (ID: {user_id})",
        "user_profile_status": "Status",
        "user_profile_balance": "Balance",
        "user_profile_purchases": "Total Purchases",
        "user_profile_banned": "Banned Status",
        "user_profile_is_banned": "Yes ⚠",
        "user_profile_not_banned": "No ✅",
        "user_profile_button_adjust_balance": "👤 Adjust Balance",
        "user_profile_button_ban": "🚨 Ban User",
        "user_profile_button_unban": "✅ Unban User",
        "user_profile_button_back_list": "�✅️ Back to User List",
        "adjust_balance_prompt": "Reply with the amount to adjust balance for @{username} (ID: {user_id}).\nUse a positive number to add (e.g., 10.50) or a negative number to subtract (e.g., -5.00).",
        "adjust_balance_reason_prompt": "Please reply with a brief reason for this balance adjustment ({amount} EUR):",
        "adjust_balance_invalid_amount": "❌ Invalid amount. Please enter a non-zero number (e.g., 10.5 or -5).",
        "adjust_balance_reason_empty": "❌ Reason cannot be empty. Please provide a reason.",
        "adjust_balance_success": "✅ Balance adjusted successfully for @{username}. New balance: {new_balance} EUR.",
        "adjust_balance_db_error": "❌ Database error adjusting balance.",
        "ban_success": "🚨 User @{username} (ID: {user_id}) has been banned.",
        "unban_success": "✅ User @{username} (ID: {user_id}) has been unbanned.",
        "ban_db_error": "❌ Database error updating ban status.",
        "ban_cannot_ban_admin": "❌ Cannot ban the primary admin.",
       
        "manage_welcome_title": "⚙️ Manage Welcome Messages",
        "manage_welcome_prompt": "Select a template to manage or activate:",
        "welcome_template_active": " (Active ✅)",
        "welcome_template_inactive": "",
        "welcome_button_activate": "✅ Activate",
        "welcome_button_edit": "✅️ Edit",
        "welcome_button_delete": "�Ÿ—‘️ Delete",
        "welcome_button_add_new": "➕ Add New Template",
        "welcome_button_reset_default": "�Ÿ”„ Reset to Built-in Default",
        "welcome_button_edit_text": "Edit Text",
        "welcome_button_edit_desc": "Edit Description",
        "welcome_button_preview": "�Ÿ‘�️ Preview",
        "welcome_button_save": "👤 Save Template",
        "welcome_activate_success": "✅ Template '{name}' activated.",
        "welcome_activate_fail": "❌ Failed to activate template '{name}'.",
        "welcome_add_name_prompt": "Enter a unique short name for the new template (e.g., 'default', 'promo_weekend'):",
        "welcome_add_name_exists": "❌ Error: A template with the name '{name}' already exists.",
        "welcome_add_text_prompt": "Template Name: {name}\n\nPlease reply with the full welcome message text. Available placeholders:\n`{placeholders}`", # Escaped placeholders
        "welcome_add_description_prompt": "Optional: Enter a short description for this template (admin view only). Send '-' to skip.",
        "welcome_add_success": "✅ Welcome message template '{name}' added.",
        "welcome_add_fail": "❌ Failed to add welcome message template.",
        "welcome_edit_text_prompt": "Editing Text for '{name}'. Current text:\n\n{current_text}\n\nPlease reply with the new text. Available placeholders:\n`{placeholders}`", # Escaped placeholders
        "welcome_edit_description_prompt": "Editing description for '{name}'. Current: '{current_desc}'.\n\nEnter new description or send '-' to keep current.",
        "welcome_edit_success": "✅ Template '{name}' updated.",
        "welcome_edit_fail": "❌ Failed to update template '{name}'.",
        "welcome_delete_confirm_title": "⚠️ Confirm Deletion",
        "welcome_delete_confirm_text": "Are you sure you want to delete the welcome message template named '{name}'?",
        "welcome_delete_confirm_active": "\n\n🚨 WARNING: This is the currently active template! Deleting it will revert to the default built-in message.",
        "welcome_delete_confirm_last": "\n\n🚨 WARNING: This is the last template! Deleting it will revert to the default built-in message.",
        "welcome_delete_button_yes": "✅ Yes, Delete Template",
        "welcome_delete_success": "✅ Template '{name}' deleted.",
        "welcome_delete_fail": "❌ Failed to delete template '{name}'.",
        "welcome_delete_not_found": "❌ Template '{name}' not found for deletion.",
        "welcome_cannot_delete_active": "❌ Cannot delete the active template. Activate another first.",
        "welcome_reset_confirm_title": "⚠️ Confirm Reset",
        "welcome_reset_confirm_text": "Are you sure you want to reset the text of the 'default' template to the built-in version and activate it?",
        "welcome_reset_button_yes": "✅ Yes, Reset & Activate",
        "welcome_reset_success": "✅ 'default' template reset and activated.",
        "welcome_reset_fail": "❌ Failed to reset 'default' template.",
        "welcome_preview_title": "--- Welcome Message Preview ---",
        "welcome_preview_name": "Name",
        "welcome_preview_desc": "Desc",
        "welcome_preview_confirm": "Save this template?",
        "welcome_save_error_context": "❌ Error: Save data lost. Cannot save template.",
        "welcome_invalid_placeholder": "⚠️ Formatting Error! Missing placeholder: `{key}`\n\nRaw Text:\n{text}",
        "welcome_formatting_error": "⚠️ Unexpected Formatting Error!\n\nRaw Text:\n{text}",
    },
    # --- Russian ---
    "ru": {
        "native_name": "Р�ƒсский",
        # --- General & Menu ---
        "welcome": "�Ÿ‘‹ �”об�€о пожалова�‚�Œ, {username}!\n\n�Ÿ‘� С�‚а�‚�ƒс: {status} {progress_bar}\n👤 �‘аланс: {balance_str} EUR\n📦 �’сего пок�ƒпок: {purchases}\n🛒 �’ ко�€зине: {basket_count} �‚ова�€(ов)\n\nНа�‡ни�‚е пок�ƒпки или из�ƒ�‡и�‚е оп�†ии ниже.\n\n⚠️ �Ÿ�€име�‡ание: �’озв�€а�‚ с�€едс�‚в невозможен.",
        "status_label": "С�‚а�‚�ƒс",
        "balance_label": "�‘аланс",
        "purchases_label": "�’сего пок�ƒпок",
        "basket_label": "�’ ко�€зине",
        "shopping_prompt": "На�‡ни�‚е пок�ƒпки или из�ƒ�‡и�‚е оп�†ии ниже.",
        "refund_note": "�Ÿ�€име�‡ание: �’озв�€а�‚ с�€едс�‚в невозможен.",
        "shop_button": "�œагазин", # <-- Example Translation
        "profile_button": "�Ÿ�€о�„ил�Œ", # <-- Example Translation
        "top_up_button": "�Ÿополни�‚�Œ", # <-- Example Translation
        "reviews_button": "�ž�‚з�‹в�‹", # <-- Example Translation
        "price_list_button": "�Ÿ�€айс-лис�‚", # <-- Example Translation
        "language_button": "Яз�‹к", # <-- Example Translation
        "admin_button": "🔧 �Ÿанел❌ Админа",
        "home_button": "�“лавная", # <-- Example Translation
        "back_button": "Назад", # <-- Example Translation
        "cancel_button": "�ž�‚мена", # <-- Example Translation
        "error_occurred_answer": "�Ÿ�€оизо�ˆла о�ˆибка. �Ÿожал�ƒйс�‚а, поп�€об�ƒй�‚е е�‰е �€аз.",
        "success_label": "Успе�ˆно!",
        "error_unexpected": "�Ÿ�€оизо�ˆла неп�€едвиденная о�ˆибка",

        # --- Shopping Flow ---
        "choose_city_title": "�’�‹бе�€и�‚е го�€од",
        "select_location_prompt": "�’�‹бе�€и�‚е ва�ˆе мес�‚оположение:",
        "no_cities_available": "На данн�‹й момен�‚ не�‚ дос�‚�ƒпн�‹✅ го�€одов. �Ÿожал�ƒйс�‚а, зайди�‚е позже.",
        "error_city_not_found": "�ž�ˆибка: �“о�€од не найден.",
        "choose_district_prompt": "�’�‹бе�€и�‚е �€айон:",
        "no_districts_available": "�’ э�‚ом го�€оде пока не�‚ дос�‚�ƒпн�‹✅ �€айонов.",
        "back_cities_button": "Назад к го�€одам",
        "error_district_city_not_found": "�ž�ˆибка: Район или го�€од не найден.",
        "select_type_prompt": "�’�‹бе�€и�‚е �‚ип п�€од�ƒк�‚а:",
        "no_types_available": "�’ данн�‹й момен�‚ здес❌ не�‚ �‚ова�€ов э�‚ого �‚ипа.",
        "error_loading_types": "�ž�ˆибка: Не �ƒдалос❌ заг�€�ƒзи�‚❌ �‚ип�‹ п�€од�ƒк�‚ов",
        "back_districts_button": "Назад к �€айонам",
        "available_options_prompt": "�”ос�‚�ƒпн�‹е ва�€иан�‚�‹:",
        "no_items_of_type": "�’ данн�‹й момен�‚ здес❌ не�‚ �‚ова�€ов э�‚ого �‚ипа.",
        "error_loading_products": "�ž�ˆибка: Не �ƒдалос❌ заг�€�ƒзи�‚❌ п�€од�ƒк�‚�‹",
        "back_types_button": "Назад к �‚ипам",
        "price_label": "Цена",
        "available_label_long": "�”ос�‚�ƒпно",
        "available_label_short": "�”ос�‚",
        "add_to_basket_button": "�’ ко�€зин�ƒ",
        "error_location_mismatch": "�ž�ˆибка: Несоо�‚ве�‚с�‚вие данн�‹✅ о мес�‚оположении.",
        "drop_unavailable": "Това�€ недос�‚�ƒпен! Э�‚о�‚ ва�€иан�‚ �‚ол�Œко �‡�‚о б�‹л �€асп�€одан или за�€езе�€ви�€ован кем-�‚о д�€�ƒгим.",
        "error_loading_details": "�ž�ˆибка: Не �ƒдалос❌ заг�€�ƒзи�‚❌ де�‚али п�€од�ƒк�‚а",
        "back_options_button": "Назад к ва�€иан�‚ам",
        "no_products_in_city_districts": "�’ нас�‚оя�‰ее в�€емя не�‚ дос�‚�ƒпн�‹✅ �‚ова�€ов ни в одном �€айоне э�‚ого го�€ода.",
        "error_loading_districts": "�ž�ˆибка заг�€�ƒзки �€айонов. �Ÿожал�ƒйс�‚а, поп�€об�ƒй�‚е е�‰е �€аз.",

        # --- Basket & Payment ---
        "added_to_basket": "✅ Това�€ за�€езе�€ви�€ован!\n\n{item} в ва�ˆей ко�€зине на {timeout} мин�ƒ�‚! ⏳",
        "expires_label": "�˜с�‚екае�‚ �‡е�€ез",
        "your_basket_title": "�’а�ˆа ко�€зина",
        "basket_empty": "🛒 �’а�ˆа ко�€зина п�ƒс�‚а!",
        "add_items_prompt": "�”обав�Œ�‚е �‚ова�€�‹, �‡�‚об�‹ на�‡а�‚❌ пок�ƒпки!",
        "items_expired_note": "С�€ок дейс�‚вия �‚ова�€ов мог ис�‚е�‡❌ или они б�‹ли �ƒдален�‹.",
        "subtotal_label": "�Ÿод�‹�‚ог",
        "total_label": "�˜�‚ого",
        "pay_now_button": "�žпла�‚и�‚❌ сей�‡ас",
        "clear_all_button": "�ž�‡ис�‚и�‚❌ все",
        "view_basket_button": "�Ÿосмо�‚�€е�‚❌ ко�€зин�ƒ",
        "clear_basket_button": "�ž�‡ис�‚и�‚❌ ко�€зин�ƒ",
        "remove_button_label": "Удали�‚�Œ",
        "basket_already_empty": "�šо�€зина �ƒже п�ƒс�‚а.",
        "basket_cleared": "�Ÿ—‘️ �šо�€зина о�‡и�‰ена!",
        "pay": "👤 �š опла�‚е: {amount} EUR",
        "insufficient_balance": "⚠️ Недос�‚а�‚о�‡но с�€едс�‚в!\n\n�Ÿожал�ƒйс�‚а, пополни�‚е баланс, �‡�‚об�‹ п�€одолжи�‚�Œ! 👤",
        "insufficient_balance_pay_option": "⚠️ Недос�‚а�‚о�‡но с�€едс�‚в! ({balance} / {required} EUR)",
        "pay_crypto_button": "👤 �žпла�‚и�‚❌ Crypto",
        "apply_discount_pay_button": "💥️ �Ÿ�€имени�‚❌ п�€омокод",
        "skip_discount_button": "⏩ �Ÿ�€оп�ƒс�‚и�‚❌ скидк�ƒ",
        "prompt_discount_or_pay": "У вас ес�‚❌ п�€омокод для п�€именения пе�€ед опла�‚ой к�€ип�‚овал�Ž�‚ой?",
        "basket_pay_enter_discount": "�’веди�‚е п�€омокод для э�‚ой пок�ƒпки:",
        "basket_pay_code_applied": "✅ �šод '{code}' п�€именен. Новая с�ƒмма: {total} EUR. �’�‹бе�€и�‚е к�€ип�‚овал�Ž�‚�ƒ:",
        "basket_pay_code_invalid": "❌ �šод недейс�‚ви�‚елен: {reason}. �’�‹бе�€и�‚е к�€ип�‚овал�Ž�‚�ƒ для опла�‚�‹ {total} EUR:",
        "choose_crypto_for_purchase": "�’�‹бе�€и�‚е к�€ип�‚овал�Ž�‚�ƒ для опла�‚�‹ {amount} EUR за ва�ˆ�ƒ ко�€зин�ƒ:",
        "payment_summary": "👤 Сводка пла�‚ежа",
        "product_label": "Това�€",
        "price_label": "Цена",
        "location_label": "�œес�‚оположение",
        "crypto_purchase_success": "�žпла�‚а под�‚ве�€ждена! �”е�‚али ва�ˆей пок�ƒпки о�‚п�€авля�Ž�‚ся.",
        "crypto_purchase_failed": "�žпла�‚а не �ƒдалас�Œ/ис�‚екла. �’а�ˆи �‚ова�€�‹ бол�Œ�ˆе не за�€езе�€ви�€ован�‹.",
        "payment_timeout_notification": "⏰ �’�€емя �žпла�‚�‹ �˜с�‚екло: �’а�ˆ пла�‚еж за �‚ова�€�‹ в ко�€зине ис�‚ек �‡е�€ез 2 �‡аса. �—а�€езе�€ви�€ованн�‹е �‚ова�€�‹ освобожден�‹.",
        "basket_pay_too_low": "С�ƒмма ко�€зин�‹ {basket_total} EUR ниже минимал�Œной для {currency}.",
        "balance_changed_error": "❌ Т�€анзак�†ия не �ƒдалас�Œ: �’а�ˆ баланс изменился. �Ÿожал�ƒйс�‚а, п�€ове�€�Œ�‚е баланс и поп�€об�ƒй�‚е снова.",
        "order_failed_all_sold_out_balance": "❌ �—аказ не �ƒдался: �’се �‚ова�€�‹ в ва�ˆей ко�€зине с�‚али недос�‚�ƒпн�‹ во в�€емя об�€або�‚ки. С�€едс�‚ва с ва�ˆего баланса не списан�‹.",
        "error_processing_purchase_contact_support": "❌ �Ÿ�€оизо�ˆла о�ˆибка п�€и об�€або�‚ке ва�ˆей пок�ƒпки. �žб�€а�‚и�‚ес❌ в сл�ƒжб�ƒ подде�€жки.",
        "purchase_success": "�ŸŽ‰ �Ÿок�ƒпка заве�€�ˆена!",
        "sold_out_note": "⚠️ �Ÿ�€име�‡ание: След�ƒ�Ž�‰ие �‚ова�€�‹ с�‚али недос�‚�ƒпн�‹ во в�€емя об�€або�‚ки и не б�‹ли вкл�Ž�‡ен�‹: {items}. С�€едс�‚ва за ни✅ не списан�‹.",
        "leave_review_now": "�žс�‚ави�‚❌ о�‚з�‹в сей�‡ас",
        "back_basket_button": "Назад в ко�€зин�ƒ",
        "error_adding_db": "�ž�ˆибка: �Ÿ�€облема с базой данн�‹✅ п�€и добавлении �‚ова�€а в ко�€зин�ƒ.",
        "error_adding_unexpected": "�ž�ˆибка: �Ÿ�€оизо�ˆла неп�€едвиденная п�€облема.",
        "reseller_discount_label": "Скидка �€еселле�€а",

        # --- Discounts ---
        "discount_no_items": "�’а�ˆа ко�€зина п�ƒс�‚а. Сна�‡ала добав�Œ�‚е �‚ова�€�‹.",
        "enter_discount_code_prompt": "�’веди�‚е ва�ˆ п�€омокод:",
        "enter_code_answer": "�’веди�‚е код в �‡а�‚.",
        "apply_discount_button": "�Ÿ�€имени�‚❌ п�€омокод",
        "no_code_provided": "�šод не п�€едос�‚авлен.",
        "discount_code_not_found": "�Ÿ�€омокод не найден.",
        "discount_code_inactive": "Э�‚о�‚ п�€омокод неак�‚ивен.",
        "discount_code_expired": "С�€ок дейс�‚вия э�‚ого п�€омокода ис�‚ек.",
        "invalid_code_expiry_data": "Неве�€н�‹е данн�‹е о с�€оке дейс�‚вия кода.",
        "code_limit_reached": "�”ос�‚игн�ƒ�‚ лими�‚ испол�Œзования кода.",
        "internal_error_discount_type": "�’н�ƒ�‚�€енняя о�ˆибка п�€и об�€або�‚ке �‚ипа скидки.",
        "db_error_validating_code": "�ž�ˆибка баз�‹ данн�‹✅ п�€и п�€ове�€ке кода.",
        "unexpected_error_validating_code": "�Ÿ�€оизо�ˆла неп�€едвиденная о�ˆибка.",
        "discount_min_order_not_met": "�œинимал�Œная с�ƒмма заказа для э�‚ого п�€омокода не дос�‚игн�ƒ�‚а.",
        "code_applied_message": "�šод '{code}' ({value}) п�€именен. Скидка: -{amount} EUR",
        "discount_applied_label": "�Ÿ�€именена скидка",
        "discount_value_label": "�—на�‡ение",
        "discount_removed_note": "�Ÿ�€омокод {code} �ƒдален: {reason}",
        "discount_removed_invalid_basket": "Скидка �ƒдалена (ко�€зина изменилас�Œ).",
        "remove_discount_button": "Удали�‚❌ скидк�ƒ",
        "discount_removed_answer": "Скидка �ƒдалена.",
        "no_discount_answer": "Скидка не п�€именена.",
        "send_text_please": "�Ÿожал�ƒйс�‚а, о�‚п�€ав�Œ�‚е п�€омокод �‚екс�‚ом.",
        "error_calculating_total": "�ž�ˆибка п�€и �€ас�‡е�‚е с�ƒмм�‹.",
        "returning_to_basket": "�’озв�€а�‰аемся в ко�€зин�ƒ.",
        "basket_empty_no_discount": "�šо�€зина п�ƒс�‚а. Невозможно п�€имени�‚❌ п�€омокод.",

        # --- Profile & History ---
        "profile_title": "�’а�ˆ п�€о�„ил�Œ",
        "purchase_history_button": "�˜с�‚о�€ия пок�ƒпок",
        "back_profile_button": "Назад в п�€о�„ил�Œ",
        "purchase_history_title": "�˜с�‚о�€ия пок�ƒпок",
        "no_purchases_yet": "�’�‹ е�‰е не сове�€�ˆали пок�ƒпок.",
        "recent_purchases_title": "�’а�ˆи недавние пок�ƒпки",
        "error_loading_profile": "❌ �ž�ˆибка: Не �ƒдалос❌ заг�€�ƒзи�‚❌ данн�‹е п�€о�„иля.",

        # --- Language ---
        "language_set_answer": "Яз�‹к �ƒс�‚ановлен на {lang}!",
        "error_saving_language": "�ž�ˆибка со✅�€анения нас�‚�€оек яз�‹ка.",
        "invalid_language_answer": "�’�‹б�€ан неве�€н�‹й яз�‹к.",
        "language": "🌐 Яз�‹к", # Menu title

        # --- Price List ---
        "no_cities_for_prices": "Не�‚ дос�‚�ƒпн�‹✅ го�€одов для п�€осмо�‚�€а �†ен.",
        "price_list_title": "�Ÿ�€айс-лис�‚",
        "select_city_prices_prompt": "�’�‹бе�€и�‚е го�€од для п�€осмо�‚�€а дос�‚�ƒпн�‹✅ �‚ова�€ов и �†ен:",
        "price_list_title_city": "�Ÿ�€айс-лис�‚: {city_name}",
        "no_products_in_city": "�’ э�‚ом го�€оде в нас�‚оя�‰ее в�€емя не�‚ дос�‚�ƒпн�‹✅ �‚ова�€ов.",
        "back_city_list_button": "Назад к списк�ƒ го�€одов",
        "message_truncated_note": "Сооб�‰ение �ƒсе�‡ено из-за ог�€ани�‡ения длин�‹. �˜спол�Œз�ƒй�‚е '�œагазин' для полной ин�„о�€ма�†ии.",
        "error_loading_prices_db": "�ž�ˆибка: Не �ƒдалос❌ заг�€�ƒзи�‚❌ п�€айс-лис�‚ для {city_name}",
        "error_displaying_prices": "�ž�ˆибка о�‚об�€ажения п�€айс-лис�‚а.",
        "error_unexpected_prices": "�ž�ˆибка: �Ÿ�€оизо�ˆла неп�€едвиденная п�€облема п�€и создании п�€айс-лис�‚а.",
        "available_label": "дос�‚�ƒпно", # Used in price list

        # --- Reviews ---
        "reviews": "📦 �œен�Ž о�‚з�‹вов",
        "view_reviews_button": "�Ÿосмо�‚�€е�‚❌ о�‚з�‹в�‹",
        "leave_review_button": "�žс�‚ави�‚❌ о�‚з�‹в",
        "enter_review_prompt": "�Ÿожал�ƒйс�‚а, введи�‚е �‚екс�‚ ва�ˆего о�‚з�‹ва и о�‚п�€ав�Œ�‚е его.",
        "enter_review_answer": "�’веди�‚е ва�ˆ о�‚з�‹в в �‡а�‚.",
        "send_text_review_please": "�Ÿожал�ƒйс�‚а, о�‚п�€ав�Œ�‚е о�‚з�‹в �‚ол�Œко �‚екс�‚ом.",
        "review_not_empty": "�ž�‚з�‹в не може�‚ б�‹�‚❌ п�ƒс�‚�‹м. �Ÿоп�€об�ƒй�‚е снова или о�‚мени�‚е.",
        "review_too_long": "�ž�‚з�‹в сли�ˆком длинн�‹й (макс. 1000 символов). �Ÿожал�ƒйс�‚а, сок�€а�‚и�‚е его.",
        "review_thanks": "Спасибо за ва�ˆ о�‚з�‹в! �’а�ˆе мнение помогае�‚ нам с�‚а�‚❌ л�ƒ�‡�ˆе.",
        "error_saving_review_db": "�ž�ˆибка: Не �ƒдалос❌ со✅�€ани�‚❌ ва�ˆ о�‚з�‹в из-за п�€облем�‹ с базой данн�‹✅.",
        "error_saving_review_unexpected": "�ž�ˆибка: �Ÿ�€оизо�ˆла неп�€едвиденная п�€облема п�€и со✅�€анении ва�ˆего о�‚з�‹ва.",
        "user_reviews_title": "�ž�‚з�‹в�‹ пол�Œзова�‚елей",
        "no_reviews_yet": "�ž�‚з�‹вов пока не�‚.",
        "no_more_reviews": "�‘ол�Œ�ˆе о�‚з�‹вов не�‚.",
        "prev_button": "�Ÿ�€ед.",
        "next_button": "След.",
        "back_review_menu_button": "Назад в �œен�Ž �ž�‚з�‹вов",
        "unknown_date_label": "Неизвес�‚ная да�‚а",
        "error_displaying_review": "�ž�ˆибка о�‚об�€ажения о�‚з�‹ва",
        "error_updating_review_list": "�ž�ˆибка обновления списка о�‚з�‹вов.",

        # --- Refill / Crypto Payments ---
        "payment_amount_too_low_api": "❌ С�ƒмма �Ÿла�‚ежа Сли�ˆком �œала: Эквивален�‚ {target_eur_amount} EUR в {currency} \\({crypto_amount}\\) ниже миним�ƒма, �‚�€еб�ƒемого пла�‚ежной сис�‚емой \\({min_amount} {currency}\\)\\. �Ÿоп�€об�ƒй�‚е бол�Œ�ˆ�ƒ�Ž с�ƒмм�ƒ EUR\\.",
        "payment_amount_too_low_with_min_eur": "❌ С�ƒмма �Ÿла�‚ежа Сли�ˆком �œала: {target_eur_amount} EUR ниже миним�ƒма для {currency} пла�‚ежей \\(миним�ƒм: {min_eur_amount} EUR\\)\\. �Ÿоп�€об�ƒй�‚е бол�Œ�ˆ�ƒ�Ž с�ƒмм�ƒ или в�‹бе�€и�‚е д�€�ƒг�ƒ�Ž к�€ип�‚овал�Ž�‚�ƒ\\.",
        "error_min_amount_fetch": "❌ �ž�ˆибка: Не �ƒдалос❌ пол�ƒ�‡и�‚❌ минимал�Œн�ƒ�Ž с�ƒмм�ƒ пла�‚ежа для {currency}\\. �Ÿоп�€об�ƒй�‚е позже или в�‹бе�€и�‚е д�€�ƒг�ƒ�Ž вал�Ž�‚�ƒ\\.",
        "invoice_title_refill": "*С�‡е�‚ на �Ÿополнение Создан*",
        "invoice_title_purchase": "*С�‡е�‚ на �žпла�‚�ƒ Создан*",
        "invoice_important_notice": "⚠️ *�’ажно:* �ž�‚п�€ав�Œ�‚е �‚о�‡н�ƒ�Ž с�ƒмм�ƒ на э�‚о�‚ ад�€ес.",
        "invoice_confirmation_notice": "✅ Ав�‚о-под�‚ве�€ждение за ~1-2 мин.",
        "invoice_valid_notice": "⏱️ *�”ейс�‚в�ƒе�‚ 30 мин�ƒ�‚*",
        "min_amount_label": "*�œинимал�Œная С�ƒмма:*",
        "payment_address_label": "*Ад�€ес для �žпла�‚�‹:*",
        "amount_label": "*С�ƒмма:*",
        "expires_at_label": "*�˜с�‚екае�‚ в:*",
        "send_warning_template": "⚠️ *�’ажно:* �ž�‚п�€ав�Œ�‚е *�‚о�‡но* э�‚�ƒ с�ƒмм�ƒ {asset} на э�‚о�‚ ад�€ес\\.",
        "overpayment_note": "ℹ️ _�ž�‚п�€авка бол�Œ�ˆей с�ƒмм�‹ доп�ƒс�‚има\\! �’а�ˆ баланс б�ƒде�‚ пополнен на основе пол�ƒ�‡енной с�ƒмм�‹ после под�‚ве�€ждения се�‚и\\._",
        "confirmation_note": "✅ �Ÿод�‚ве�€ждение ав�‚ома�‚и�‡еское �‡е�€ез веб✅�ƒк после под�‚ве�€ждения се�‚и\\.",
        "invoice_amount_label_text": "С�ƒмма",
        "invoice_send_following_amount": "�Ÿожал�ƒйс�‚а, о�‚п�€ав�Œ�‚е след�ƒ�Ž�‰�ƒ�Ž с�ƒмм�ƒ:",
        "invoice_payment_deadline": "�Ÿла�‚еж должен б�‹�‚❌ в�‹полнен в �‚е�‡ение 20 мин�ƒ�‚ с момен�‚а создания с�‡е�‚а.",
        "error_estimate_failed": "❌ �ž�ˆибка: Не �ƒдалос❌ о�†ени�‚❌ с�ƒмм�ƒ в к�€ип�‚овал�Ž�‚е. �Ÿоп�€об�ƒй�‚е снова или в�‹бе�€и�‚е д�€�ƒг�ƒ�Ž вал�Ž�‚�ƒ.",
        "error_estimate_currency_not_found": "❌ �ž�ˆибка: �’ал�Ž�‚а {currency} не подде�€живае�‚ся для о�†енки. �’�‹бе�€и�‚е д�€�ƒг�ƒ�Ž вал�Ž�‚�ƒ.",
        "error_discount_invalid_payment": "❌ �’а�ˆ п�€омокод бол�Œ�ˆе не дейс�‚ви�‚елен: {reason}. �’е�€ни�‚ес❌ в ко�€зин�ƒ, �‡�‚об�‹ п�€одолжи�‚❌ без скидки.",
        "error_discount_mismatch_payment": "❌ �žбна�€�ƒжено несоо�‚ве�‚с�‚вие с�ƒмм�‹ пла�‚ежа. �’е�€ни�‚ес❌ в ко�€зин�ƒ и поп�€об�ƒй�‚е снова.",
        "crypto_payment_disabled": "�Ÿополнение баланса в данн�‹й момен�‚ о�‚кл�Ž�‡ено.",
        "top_up_title": "�Ÿополни�‚❌ баланс",
        "enter_refill_amount_prompt": "�ž�‚ве�‚�Œ�‚е с�ƒммой в EUR, ко�‚о�€�ƒ�Ž в�‹ ✅о�‚и�‚е добави�‚❌ на баланс (нап�€име�€, 10 или 25.50).",
        "min_top_up_note": "�œинимал�Œное пополнение: {amount} EUR",
        "enter_amount_answer": "�’веди�‚е с�ƒмм�ƒ пополнения.",
        "send_amount_as_text": "�ž�‚п�€ав�Œ�‚е с�ƒмм�ƒ �‚екс�‚ом (нап�€име�€, 10 или 25.50).",
        "amount_too_low_msg": "С�ƒмма сли�ˆком мала. �œинимал�Œное пополнение {amount} EUR. �’веди�‚е бол�Œ�ˆ�ƒ�Ž с�ƒмм�ƒ.",
        "amount_too_high_msg": "С�ƒмма сли�ˆком велика. �’веди�‚е мен�Œ�ˆ�ƒ�Ž с�ƒмм�ƒ.",
        "invalid_amount_format_msg": "Неве�€н�‹й �„о�€ма�‚ с�ƒмм�‹. �’веди�‚е �‡исло (нап�€име�€, 10 или 25.50).",
        "unexpected_error_msg": "�Ÿ�€оизо�ˆла неп�€едвиденная о�ˆибка. �Ÿоп�€об�ƒй�‚е позже.",
        "choose_crypto_prompt": "�’�‹ ✅о�‚и�‚е пополни�‚❌ на {amount} EUR. �Ÿожал�ƒйс�‚а, в�‹бе�€и�‚е к�€ип�‚овал�Ž�‚�ƒ для опла�‚�‹:",
        "cancel_top_up_button": "�ž�‚мени�‚❌ пополнение",
        "preparing_invoice": "⏳ �Ÿодго�‚овка с�‡е�‚а на опла�‚�ƒ...",
        "failed_invoice_creation": "❌ Не �ƒдалос❌ созда�‚❌ с�‡е�‚ на опла�‚�ƒ. Э�‚о може�‚ б�‹�‚❌ в�€еменная п�€облема с пла�‚ежной сис�‚емой или п�€облема с кл�Ž�‡ом API. �Ÿоп�€об�ƒй�‚е позже или об�€а�‚и�‚ес❌ в подде�€жк�ƒ.",
        "error_preparing_payment": "❌ �Ÿ�€оизо�ˆла о�ˆибка п�€и подго�‚овке данн�‹✅ для опла�‚�‹. �Ÿоп�€об�ƒй�‚е позже.",
        "top_up_success_title": "✅ �‘аланс Успе�ˆно �Ÿополнен!",
        "amount_added_label": "�”обавлено",
        "new_balance_label": "�’а�ˆ нов�‹й баланс",
        "error_nowpayments_api": "❌ �ž�ˆибка API �Ÿла�‚ежей: Не �ƒдалос❌ созда�‚❌ пла�‚еж. �Ÿоп�€об�ƒй�‚е позже или об�€а�‚и�‚ес❌ в подде�€жк�ƒ.",
        "error_invalid_nowpayments_response": "❌ �ž�ˆибка API �Ÿла�‚ежей: �Ÿол�ƒ�‡ен неве�€н�‹й о�‚ве�‚. �žб�€а�‚и�‚ес❌ в подде�€жк�ƒ.",
        "error_nowpayments_api_key": "❌ �ž�ˆибка API �Ÿла�‚ежей: Неве�€н�‹й кл�Ž�‡ API. �žб�€а�‚и�‚ес❌ в подде�€жк�ƒ.",
        "payment_pending_db_error": "❌ �ž�ˆибка �‘аз�‹ �”анн�‹✅: Не �ƒдалос❌ записа�‚❌ ожида�Ž�‰ий пла�‚еж. �žб�€а�‚и�‚ес❌ в подде�€жк�ƒ.",
        "payment_cancelled_or_expired": "С�‚а�‚�ƒс �Ÿла�‚ежа: �’а�ˆ пла�‚еж ({payment_id}) б�‹л о�‚менен или ис�‚ек.",
        "webhook_processing_error": "�ž�ˆибка Webhook: Не �ƒдалос❌ об�€або�‚а�‚❌ обновление пла�‚ежа {payment_id}.",
        "webhook_db_update_failed": "⚠€и�‚и�‡еская �ž�ˆибка: �Ÿла�‚еж {payment_id} под�‚ве�€жден, но обновление баланса в �‘�” для пол�Œзова�‚еля {user_id} не �ƒдалос�Œ. Т�€еб�ƒе�‚ся �€�ƒ�‡ное вме�ˆа�‚ел�Œс�‚во.",
        "webhook_pending_not_found": "�Ÿ�€ед�ƒп�€еждение Webhook: �Ÿол�ƒ�‡ено обновление для ID пла�‚ежа {payment_id}, но в �‘�” не найден ожида�Ž�‰ий депози�‚.",
        "webhook_price_fetch_error": "�ž�ˆибка Webhook: Не �ƒдалос❌ пол�ƒ�‡и�‚❌ �†ен�ƒ {currency} для под�‚ве�€ждения зна�‡ения EUR для пла�‚ежа {payment_id}.",
        "payment_cancelled_user": "�Ÿла�‚еж о�‚менен. �—а�€езе�€ви�€ованн�‹е �‚ова�€�‹ (если б�‹ли) освобожден�‹.",
        "payment_cancel_error": "Не �ƒдалос❌ о�‚мени�‚❌ пла�‚еж (�ƒже об�€або�‚ан или по�‚е�€ян кон�‚екс�‚).",
        "cancel_payment_button": "�ž�‚мени�‚❌ пла�‚еж",
        "proceeding_to_payment_answer": "�Ÿе�€е✅од к ва�€иан�‚ам опла�‚�‹...",
        "credit_overpayment_purchase": "✅ �’а�ˆа пок�ƒпка б�‹ла �ƒспе�ˆной! �”ополни�‚ел�Œно, пе�€епла�‚а в �€азме�€е {amount} EUR за�‡ислена на ва�ˆ баланс. �’а�ˆ нов�‹й баланс: {new_balance} EUR.",
        "credit_underpayment_purchase": "ℹ️ �’а�ˆа пок�ƒпка не �ƒдалас❌ из-за недопла�‚�‹, но пол�ƒ�‡енная с�ƒмма ({amount} EUR) за�‡ислена на ва�ˆ баланс. �’а�ˆ нов�‹й баланс: {new_balance} EUR.",
        "crypto_purchase_underpaid_credited": "⚠️ �Ÿок�ƒпка не �ƒдалас�Œ: �žбна�€�ƒжена недопла�‚а. Т�€ебовалас❌ с�ƒмма {needed_eur} EUR. �’а�ˆ баланс пополнен на пол�ƒ�‡енн�ƒ�Ž с�ƒмм�ƒ ({paid_eur} EUR). �’а�ˆи �‚ова�€�‹ не б�‹ли дос�‚авлен�‹.",
        "credit_refill": "✅ �’а�ˆ баланс пополнен на {amount} EUR. �Ÿ�€и�‡ина: {reason}. Нов�‹й баланс: {new_balance} EUR.",
    }
}
# ==============================================================
# ===== ^ ^ ^ ^ ^      LANGUAGE DICTIONARY     ^ ^ ^ ^ ^ ======
# ==============================================================


DEFAULT_WELCOME_MESSAGE = LANGUAGES['en']['welcome']

MIN_DEPOSIT_EUR = Decimal('5.00') # Minimum deposit amount in EUR
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
FEE_ADJUSTMENT = Decimal('1.0')

# --- Global Data Variables ---
CITIES = {}
DISTRICTS = {}
PRODUCT_TYPES = {}
DEFAULT_PRODUCT_EMOJI = "💎" # Fallback emoji
SIZES = ["2g", "5g"]
BOT_MEDIA = {'type': None, 'path': None}
currency_price_cache = {}
min_amount_cache = {}
CACHE_EXPIRY_SECONDS = 900

# =========================================================================
# HIGH-CONCURRENCY DATABASE SYSTEM
# Designed to handle 200+ simultaneous users without issues
# =========================================================================

import threading
from functools import wraps
# Queue import removed - no longer using connection pool
import time as time_module

# Database settings - SIMPLE connection model (no pool needed with SQLite WAL)
_DB_BUSY_TIMEOUT = 60000  # 60 seconds busy timeout for SQLite
_db_dir_created = False

def _ensure_db_dir():
    """Ensure database directory exists."""
    global _db_dir_created
    if _db_dir_created:
        return
    
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not create DB dir {db_dir}: {e}")
    _db_dir_created = True

def get_db_connection():
    """
    Create a new database connection optimized for SQLite WAL mode.
    
    SQLite with WAL mode handles concurrent connections excellently - each connection
    can read while others write. Creating connections is fast, no pool needed.
    """
    _ensure_db_dir()
    
    conn = sqlite3.connect(
        DATABASE_PATH, 
        timeout=30,  # Wait up to 30 seconds for locks
        check_same_thread=False,  # Allow connection use from any thread
        isolation_level=None  # Autocommit mode
    )
    
    # WAL mode is CRITICAL for concurrent access - allows reads while writing
    conn.execute("PRAGMA journal_mode=WAL;")
    # Busy timeout - wait this long when database is locked  
    conn.execute(f"PRAGMA busy_timeout={_DB_BUSY_TIMEOUT};")
    # Synchronous NORMAL is faster but still safe with WAL
    conn.execute("PRAGMA synchronous=NORMAL;")
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys=ON;")
    # Increase cache for better performance
    conn.execute("PRAGMA cache_size=10000;")
    # Memory-mapped I/O for faster reads
    conn.execute("PRAGMA mmap_size=268435456;")  # 256MB
    conn.row_factory = sqlite3.Row
    return conn

def return_db_connection(conn):
    """Close a connection (compatibility function - just closes it)."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception as e:
        logger.debug(f"Error closing connection: {e}")

def db_retry(max_retries=5, base_delay=0.1, max_delay=5.0):
    """
    Decorator for database operations with exponential backoff retry.
    Handles 'database is locked' and other transient errors.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    error_str = str(e).lower()
                    if "database is locked" in error_str or "busy" in error_str:
                        last_error = e
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        jitter = delay * 0.1 * (0.5 - (time_module.time() % 1))
                        sleep_time = delay + jitter
                        if attempt < max_retries - 1:
                            logger.warning(f"⏳ DB locked (attempt {attempt+1}/{max_retries}), retrying in {sleep_time:.2f}s...")
                            time_module.sleep(sleep_time)
                        continue
                    raise
                except Exception:
                    raise
            raise last_error or sqlite3.OperationalError("Max retries exceeded")
        return wrapper
    return decorator

async def db_retry_async(max_retries=5, base_delay=0.1, max_delay=5.0):
    """Async version of db_retry decorator."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            import asyncio
            last_error = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    error_str = str(e).lower()
                    if "database is locked" in error_str or "busy" in error_str:
                        last_error = e
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        if attempt < max_retries - 1:
                            logger.warning(f"⏳ DB locked async (attempt {attempt+1}/{max_retries}), retrying in {delay:.2f}s...")
                            await asyncio.sleep(delay)
                        continue
                    raise
                except Exception:
                    raise
            raise last_error or sqlite3.OperationalError("Max retries exceeded")
        return wrapper
    return decorator

class DBTransaction:
    """
    Context manager for safe database transactions with automatic retry.
    Usage:
        with DBTransaction() as (conn, cursor):
            cursor.execute("...")
            # Auto-commits on success, auto-rollbacks on error
    """
    def __init__(self, max_retries=3):
        self.conn = None
        self.cursor = None
        self.max_retries = max_retries
        self._attempt = 0
    
    def __enter__(self):
        self.conn = get_db_connection()
        self.conn.execute("BEGIN IMMEDIATE")  # Lock immediately to prevent race conditions
        self.cursor = self.conn.cursor()
        return (self.conn, self.cursor)
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        except Exception as e:
            logger.error(f"Transaction cleanup error: {e}")
        finally:
            return_db_connection(self.conn)
        return False  # Don't suppress exceptions


# --- Database Initialization ---
def init_db():
    """Initializes the database schema."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # --- users table ---
            c.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0.0,
                total_purchases INTEGER DEFAULT 0, basket TEXT DEFAULT '',
                language TEXT DEFAULT 'en', theme TEXT DEFAULT 'default',
                is_banned INTEGER DEFAULT 0,
                is_reseller INTEGER DEFAULT 0, -- <<< ADDED is_reseller column
                last_active TEXT DEFAULT NULL, -- Track when user was last active/reachable
                broadcast_failed_count INTEGER DEFAULT 0 -- Track consecutive broadcast failures
            )''')
            # Add is_banned column if missing (safer check)
            try: c.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER DEFAULT 0")
            except sqlite3.OperationalError: pass # Ignore if already exists
           
            try:
                c.execute("ALTER TABLE users ADD COLUMN is_reseller INTEGER DEFAULT 0")
                logger.info("Added 'is_reseller' column to users table.")
            except sqlite3.OperationalError as alter_e:
                 if "duplicate column name: is_reseller" in str(alter_e): pass # Ignore if already exists
                 else: raise # Reraise other errors
           
            
            # Add broadcast tracking columns if missing
            try:
                c.execute("ALTER TABLE users ADD COLUMN last_active TEXT DEFAULT NULL")
                logger.info("Added 'last_active' column to users table.")
            except sqlite3.OperationalError as alter_e:
                if "duplicate column name: last_active" in str(alter_e): pass
                else: raise
            
            try:
                c.execute("ALTER TABLE users ADD COLUMN broadcast_failed_count INTEGER DEFAULT 0")
                logger.info("Added 'broadcast_failed_count' column to users table.")
            except sqlite3.OperationalError as alter_e:
                if "duplicate column name: broadcast_failed_count" in str(alter_e): pass
                else: raise

            # cities table
            c.execute('''CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL
            )''')
            # districts table
            c.execute('''CREATE TABLE IF NOT EXISTS districts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, city_id INTEGER NOT NULL, name TEXT NOT NULL,
                FOREIGN KEY(city_id) REFERENCES cities(id) ON DELETE CASCADE, UNIQUE (city_id, name)
            )''')
            # product_types table
            c.execute(f'''CREATE TABLE IF NOT EXISTS product_types (
                name TEXT PRIMARY KEY NOT NULL,
                emoji TEXT DEFAULT '{DEFAULT_PRODUCT_EMOJI}',
                description TEXT
            )''')
            # Add emoji column if missing
            try: c.execute(f"ALTER TABLE product_types ADD COLUMN emoji TEXT DEFAULT '{DEFAULT_PRODUCT_EMOJI}'")
            except sqlite3.OperationalError: pass # Ignore if already exists
            # Add description column if missing
            try: c.execute("ALTER TABLE product_types ADD COLUMN description TEXT")
            except sqlite3.OperationalError: pass # Ignore if already exists

            # products table
            c.execute('''CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, city TEXT NOT NULL, district TEXT NOT NULL,
                product_type TEXT NOT NULL, size TEXT NOT NULL, name TEXT NOT NULL, price REAL NOT NULL,
                available INTEGER DEFAULT 1, reserved INTEGER DEFAULT 0, original_text TEXT,
                added_by INTEGER, added_date TEXT
            )''')
            # product_media table (Fixed: No UNIQUE constraint on file_path to prevent insertion errors)
            c.execute('''CREATE TABLE IF NOT EXISTS product_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                media_type TEXT NOT NULL, file_path TEXT NOT NULL, telegram_file_id TEXT,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            )''')
            # purchases table
            c.execute('''CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER,
                product_name TEXT NOT NULL, product_type TEXT NOT NULL, product_size TEXT NOT NULL,
                price_paid REAL NOT NULL, city TEXT NOT NULL, district TEXT NOT NULL, purchase_date TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE SET NULL
            )''')
            # reviews table
            c.execute('''CREATE TABLE IF NOT EXISTS reviews (
                review_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                review_text TEXT NOT NULL, review_date TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            # discount_codes table
            c.execute('''CREATE TABLE IF NOT EXISTS discount_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
                discount_type TEXT NOT NULL CHECK(discount_type IN ('percentage', 'fixed')),
                value REAL NOT NULL, is_active INTEGER DEFAULT 1 CHECK(is_active IN (0, 1)),
                max_uses INTEGER DEFAULT NULL, uses_count INTEGER DEFAULT 0,
                created_date TEXT NOT NULL, expiry_date TEXT DEFAULT NULL,
                min_order_amount REAL DEFAULT NULL
            )''')
            
            # discount_code_usage table - Track individual user usage (allows reuse)
            c.execute('''CREATE TABLE IF NOT EXISTS discount_code_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                used_at TEXT NOT NULL,
                discount_amount REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            
            # YOLO MODE: Bulletproof migration for discount code reuse
            try:
                # Check if there are any existing unique constraints on this table
                indexes = c.execute("PRAGMA index_list(discount_code_usage)").fetchall()
                has_unique_constraint = False
                for index in indexes:
                    if index[2]:  # unique flag
                        index_info = c.execute("PRAGMA index_info(" + index[1] + ")").fetchall()
                        if len(index_info) == 2:  # Check if it's a composite index on user_id and code
                            columns = [col[2] for col in index_info]
                            if 'user_id' in columns and 'code' in columns:
                                has_unique_constraint = True
                                logger.info(f"Found unique constraint: {index[1]}")
                                break
                
                if has_unique_constraint:
                    logger.info("YOLO MODE: Migrating discount_code_usage table to allow code reuse...")
                    # Create new table without unique constraint
                    c.execute('''CREATE TABLE discount_code_usage_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        code TEXT NOT NULL,
                        used_at TEXT NOT NULL,
                        discount_amount REAL NOT NULL,
                        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                    )''')
                    
                    # Copy all data (duplicates will be preserved)
                    c.execute("INSERT INTO discount_code_usage_new SELECT * FROM discount_code_usage")
                    
                    # Drop old table and rename new one
                    c.execute("DROP TABLE discount_code_usage")
                    c.execute("ALTER TABLE discount_code_usage_new RENAME TO discount_code_usage")
                    logger.info("YOLO MODE: Migration completed - Users can now reuse discount codes")
                else:
                    logger.info("YOLO MODE: No unique constraint found, table is already in correct state")
                    
            except Exception as e:
                logger.error(f"YOLO MODE: Migration error (continuing anyway): {e}")
                # Continue execution even if migration fails
                pass
            # pending_deposits table
            c.execute('''CREATE TABLE IF NOT EXISTS pending_deposits (
                payment_id TEXT PRIMARY KEY NOT NULL, user_id INTEGER NOT NULL,
                currency TEXT NOT NULL, target_eur_amount REAL NOT NULL,
                expected_crypto_amount REAL NOT NULL, created_at TEXT NOT NULL,
                is_purchase INTEGER DEFAULT 0, basket_snapshot_json TEXT DEFAULT NULL,
                discount_code_used TEXT DEFAULT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            # Add columns to pending_deposits if missing
            pending_cols = [col[1] for col in c.execute("PRAGMA table_info(pending_deposits)").fetchall()]
            if 'is_purchase' not in pending_cols: c.execute("ALTER TABLE pending_deposits ADD COLUMN is_purchase INTEGER DEFAULT 0")
            if 'basket_snapshot_json' not in pending_cols: c.execute("ALTER TABLE pending_deposits ADD COLUMN basket_snapshot_json TEXT DEFAULT NULL")
            if 'discount_code_used' not in pending_cols: c.execute("ALTER TABLE pending_deposits ADD COLUMN discount_code_used TEXT DEFAULT NULL")

            # Admin Log table
            c.execute('''CREATE TABLE IF NOT EXISTS admin_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, admin_id INTEGER NOT NULL, target_user_id INTEGER,
                action TEXT NOT NULL, reason TEXT, amount_change REAL DEFAULT NULL,
                old_value TEXT, new_value TEXT
            )''')
            # Bot Settings table
            c.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY NOT NULL, setting_value TEXT
            )''')
            # Welcome Messages table
            c.execute('''CREATE TABLE IF NOT EXISTS welcome_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
                template_text TEXT NOT NULL, description TEXT
            )''')
            # Add description column if missing
            try: c.execute("ALTER TABLE welcome_messages ADD COLUMN description TEXT")
            except sqlite3.OperationalError: pass # Ignore if already exists

           
            c.execute('''CREATE TABLE IF NOT EXISTS reseller_discounts (
                reseller_user_id INTEGER NOT NULL,
                product_type TEXT NOT NULL,
                discount_percentage REAL NOT NULL CHECK (discount_percentage >= 0 AND discount_percentage <= 100),
                PRIMARY KEY (reseller_user_id, product_type),
                FOREIGN KEY (reseller_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (product_type) REFERENCES product_types(name) ON DELETE CASCADE
            )''')
           

            # Insert initial welcome messages (only if table was just created or empty - handled by INSERT OR IGNORE)
            initial_templates = [
                ("default", LANGUAGES['en']['welcome'], "Built-in default message (EN)"),
                ("clean", "�Ÿ‘‹ Hello, {username}!\n\n👤 Balance: {balance_str} EUR\n⭐ Status: {status}\n🛒 Basket: {basket_count} item(s)\n\nReady to shop or manage your profile? Explore the options below! �Ÿ‘‡\n\n⚠️ Note: No refunds.", "Clean and direct style"),
                ("enthusiastic", "✅ Welcome back, {username}! ✅\n\nReady for more? You've got **{balance_str} EUR** to spend! 👤\nYour basket ({basket_count} items) is waiting for you! 🛒\n\nYour current status: {status} {progress_bar}\nTotal Purchases: {purchases}\n\n�Ÿ‘‡ Dive back into the shop or check your profile! �Ÿ‘‡\n\n⚠️ Note: No refunds.", "Enthusiastic style with emojis"),
                ("status_focus", "�Ÿ‘‘ Welcome, {username}! ({status}) �Ÿ‘‘\n\nTrack your journey: {progress_bar}\nTotal Purchases: {purchases}\n\n👤 Balance: {balance_str} EUR\n🛒 Basket: {basket_count} item(s)\n\nManage your profile or explore the shop! �Ÿ‘‡\n\n⚠️ Note: No refunds.", "Focuses on status and progress"),
                ("minimalist", "Welcome, {username}.\n\nBalance: {balance_str} EUR\nBasket: {basket_count}\nStatus: {status}\n\nUse the menu below to navigate.\n\n⚠️ Note: No refunds.", "Simple, minimal text"),
                ("basket_focus", "Welcome back, {username}!\n\n🛒 You have **{basket_count} item(s)** in your basket! Don't forget about them!\n👤 Balance: {balance_str} EUR\n⭐ Status: {status} ({purchases} total purchases)\n\nCheck out your basket, keep shopping, or top up! �Ÿ‘‡\n\n⚠️ Note: No refunds.", "Reminds user about items in basket")
            ]
            inserted_count = 0
            changes_before = conn.total_changes # Get changes before loop
            for name, text, desc in initial_templates:
                try:
                    c.execute("INSERT OR IGNORE INTO welcome_messages (name, template_text, description) VALUES (?, ?, ?)", (name, text, desc))
                except sqlite3.Error as insert_e: logger.error(f"Error inserting template '{name}': {insert_e}")
            changes_after = conn.total_changes # Get changes after loop
            inserted_count = changes_after - changes_before # Calculate the difference

            if inserted_count > 0: logger.info(f"Checked/Inserted {inserted_count} initial welcome message templates.")
            else: logger.info("Initial welcome message templates already exist or failed to insert.")

            # Set default as active if setting doesn't exist
            c.execute("INSERT OR IGNORE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
                      ("active_welcome_message_name", "default"))
            logger.info("Ensured 'default' is set as active welcome message in settings if not already set.")

            # MIGRATION: Fix product_media table schema (remove UNIQUE constraint and add proper foreign key)
            try:
                # Check if the table exists and has the old schema
                c.execute("PRAGMA table_info(product_media)")
                columns = c.fetchall()
                
                # Check if file_path has UNIQUE constraint
                file_path_column = next((col for col in columns if col[1] == 'file_path'), None)
                has_unique_constraint = file_path_column and 'UNIQUE' in str(file_path_column)
                
                if has_unique_constraint:
                    logger.info("Migrating product_media table to remove UNIQUE constraint on file_path...")
                    # Create new table with proper schema
                    c.execute('''CREATE TABLE IF NOT EXISTS product_media_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                        media_type TEXT NOT NULL, file_path TEXT NOT NULL, telegram_file_id TEXT,
                        FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
                    )''')
                    # Copy data
                    c.execute("INSERT INTO product_media_new SELECT * FROM product_media")
                    # Drop old table and rename new one
                    c.execute("DROP TABLE product_media")
                    c.execute("ALTER TABLE product_media_new RENAME TO product_media")
                    logger.info("Successfully migrated product_media table to remove UNIQUE constraint")
                else:
                    logger.info("product_media table schema is already correct")
            except Exception as migration_e:
                logger.warning(f"Migration attempt failed, continuing with existing table: {migration_e}")

            # =========================================================================
            # SOLANA PAYMENT TABLES
            # =========================================================================
            c.execute('''CREATE TABLE IF NOT EXISTS solana_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_id TEXT UNIQUE NOT NULL,
                public_key TEXT NOT NULL,
                private_key TEXT NOT NULL,
                expected_amount REAL NOT NULL,
                amount_received REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')
            
            # Payment queue for 100% reliability under high load
            c.execute('''CREATE TABLE IF NOT EXISTS payment_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                max_attempts INTEGER DEFAULT 5,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )''')

            # Create Indices
            c.execute("CREATE INDEX IF NOT EXISTS idx_product_media_product_id ON product_media(product_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_districts_city_name ON districts(city_id, name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_products_location_type ON products(city, district, product_type)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_reviews_user ON reviews(user_id)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_discount_code_unique ON discount_codes(code)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_deposits_user_id ON pending_deposits(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_admin_log_timestamp ON admin_log(timestamp)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(is_banned)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pending_deposits_is_purchase ON pending_deposits(is_purchase)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_welcome_message_name ON welcome_messages(name)")
           
            c.execute("CREATE INDEX IF NOT EXISTS idx_users_is_reseller ON users(is_reseller)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_reseller_discounts_user_id ON reseller_discounts(reseller_user_id)")
           
            c.execute("CREATE INDEX IF NOT EXISTS idx_solana_wallets_status ON solana_wallets(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_solana_wallets_user_id ON solana_wallets(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_solana_wallets_created_at ON solana_wallets(created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_payment_queue_status ON payment_queue(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_payment_queue_user_id ON payment_queue(user_id)")
           

            conn.commit()
            
            # =========================================================================
            # ENABLE WAL MODE for HIGH CONCURRENCY
            # WAL allows concurrent reads while writing - critical for 200+ users
            # =========================================================================
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute(f"PRAGMA busy_timeout={_DB_BUSY_TIMEOUT};")
            c.execute("PRAGMA synchronous=NORMAL;")
            c.execute("PRAGMA cache_size=10000;")
            wal_mode = c.execute("PRAGMA journal_mode;").fetchone()[0]
            logger.info(f"✅ Database WAL mode: {wal_mode} (high-concurrency enabled)")
            
            logger.info(f"Database schema at {DATABASE_PATH} initialized/verified successfully.")
    except sqlite3.Error as e:
        logger.critical(f"CRITICAL ERROR: Database initialization failed for {DATABASE_PATH}: {e}", exc_info=True)
        raise SystemExit("Database initialization failed.")


# =========================================================================
# PAYMENT QUEUE - 100% RELIABILITY SYSTEM
# Ensures no payments are ever lost even under extreme load
# =========================================================================

def queue_payment_action(payment_id: str, user_id: int, action_type: str, payload: dict) -> bool:
    """
    Add a payment action to the queue for guaranteed processing.
    Actions: 'finalize_purchase', 'finalize_refill', 'credit_balance', 'send_notification'
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        c.execute("""
            INSERT OR REPLACE INTO payment_queue 
            (payment_id, user_id, action_type, payload, status, attempts, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', 0, datetime('now'), datetime('now'))
        """, (payment_id, user_id, action_type, json.dumps(payload)))
        conn.commit()
        conn.close()
        logger.info(f"📦 Queued payment action: {action_type} for user {user_id} (payment: {payment_id})")
        return True
    except Exception as e:
        logger.error(f"Failed to queue payment action: {e}")
        return False

def get_pending_queue_items(limit: int = 50) -> list:
    """Get pending items from the payment queue for processing."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT * FROM payment_queue 
            WHERE status = 'pending' AND attempts < max_attempts
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,))
        items = [dict(row) for row in c.fetchall()]
        conn.close()
        return items
    except Exception as e:
        logger.error(f"Failed to get queue items: {e}")
        return []

def mark_queue_item_processed(payment_id: str, success: bool, error_message: str = None):
    """Mark a queue item as processed (completed or failed)."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        if success:
            c.execute("""
                UPDATE payment_queue 
                SET status = 'completed', updated_at = datetime('now')
                WHERE payment_id = ?
            """, (payment_id,))
        else:
            c.execute("""
                UPDATE payment_queue 
                SET attempts = attempts + 1, 
                    error_message = ?,
                    updated_at = datetime('now'),
                    status = CASE WHEN attempts + 1 >= max_attempts THEN 'failed' ELSE 'pending' END
                WHERE payment_id = ?
            """, (error_message, payment_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update queue item: {e}")


# --- Pending Deposit DB Helpers (Synchronous - Modified) ---
def add_pending_deposit(payment_id: str, user_id: int, currency: str, target_eur_amount: float, expected_crypto_amount: float, is_purchase: bool = False, basket_snapshot: list | None = None, discount_code: str | None = None):
    basket_json = json.dumps(basket_snapshot) if basket_snapshot else None
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO pending_deposits (
                    payment_id, user_id, currency, target_eur_amount,
                    expected_crypto_amount, created_at, is_purchase,
                    basket_snapshot_json, discount_code_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                payment_id, user_id, currency.lower(), target_eur_amount,
                expected_crypto_amount, datetime.now(timezone.utc).isoformat(),
                1 if is_purchase else 0, basket_json, discount_code
                ))
            conn.commit()
            log_type = "direct purchase" if is_purchase else "refill"
            logger.info(f"Added pending {log_type} deposit {payment_id} for user {user_id} ({target_eur_amount:.2f} EUR / exp: {expected_crypto_amount} {currency}). Basket items: {len(basket_snapshot) if basket_snapshot else 0}.")
            return True
    except sqlite3.IntegrityError:
        logger.warning(f"Attempted to add duplicate pending deposit ID: {payment_id}")
        return False
    except sqlite3.Error as e:
        logger.error(f"DB error adding pending deposit {payment_id} for user {user_id}: {e}", exc_info=True)
        return False

def get_pending_deposit(payment_id: str):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # Fetch all needed columns, including the new ones
            c.execute("""
                SELECT user_id, currency, target_eur_amount, expected_crypto_amount,
                       is_purchase, basket_snapshot_json, discount_code_used
                FROM pending_deposits WHERE payment_id = ?
            """, (payment_id,))
            row = c.fetchone()
            if row:
                row_dict = dict(row)
                # Handle potential NULL for expected amount
                if row_dict.get('expected_crypto_amount') is None:
                    logger.warning(f"Pending deposit {payment_id} has NULL expected_crypto_amount. Using 0.0.")
                    row_dict['expected_crypto_amount'] = 0.0
                # Deserialize basket snapshot if present
                if row_dict.get('basket_snapshot_json'):
                    try:
                        row_dict['basket_snapshot'] = json.loads(row_dict['basket_snapshot_json'])
                    except json.JSONDecodeError:
                        logger.error(f"Failed to decode basket_snapshot_json for payment {payment_id}.")
                        row_dict['basket_snapshot'] = None # Indicate error or empty
                else:
                    row_dict['basket_snapshot'] = None
                return row_dict
            else:
                return None
    except sqlite3.Error as e:
        logger.error(f"DB error fetching pending deposit {payment_id}: {e}", exc_info=True)
        return None

# --- HELPER TO UNRESERVE ITEMS (Synchronous) ---
def _unreserve_basket_items(basket_snapshot: list | None):
    """Helper to decrement reserved counts for items in a snapshot."""
    if not basket_snapshot:
        return

    product_ids_to_release_counts = Counter(item['product_id'] for item in basket_snapshot if 'product_id' in item)
    if not product_ids_to_release_counts:
        return

    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        decrement_data = [(count, pid) for pid, count in product_ids_to_release_counts.items()]
        c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data)
        conn.commit()
        total_released = sum(product_ids_to_release_counts.values())
        logger.info(f"Un-reserved {total_released} items due to failed/expired/cancelled payment.") # General log message
    except sqlite3.Error as e:
        logger.error(f"DB error un-reserving items: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()

# --- REMOVE PENDING DEPOSIT (Modified Trigger Logic) ---
def remove_pending_deposit(payment_id: str, trigger: str = "unknown"): # Added trigger for logging
    pending_info = get_pending_deposit(payment_id) # Get info *before* deleting
    deleted = False
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        result = c.execute("DELETE FROM pending_deposits WHERE payment_id = ?", (payment_id,))
        conn.commit()
        deleted = result.rowcount > 0
        if deleted:
            logger.info(f"Removed pending deposit record for payment ID: {payment_id} (Trigger: {trigger})")
        else:
            # Reduce log level for "not found" as it can be normal (e.g., double webhook)
            logger.debug(f"No pending deposit record found to remove for payment ID: {payment_id} (Trigger: {trigger})")
    except sqlite3.Error as e:
        logger.error(f"DB error removing pending deposit {payment_id} (Trigger: {trigger}): {e}", exc_info=True)
        return False # Indicate failure

    # --- MODIFIED Condition for Un-reserving ---
    # Un-reserve if deletion was successful, it was a purchase, AND the trigger indicates non-success
    # IMPORTANT: Include ALL triggers that indicate successful payment completion
    successful_triggers = ['purchase_success', 'refill_success', 'crypto_payment_success', 'refill_payment_success', 'recovery_success']
    if deleted and pending_info and pending_info.get('is_purchase') == 1 and trigger not in successful_triggers:
        log_reason = f"payment {payment_id} failure/expiry/cancellation (Trigger: {trigger})"
        logger.info(f"Payment was a purchase that did not succeed or was cancelled. Attempting to un-reserve items from snapshot ({log_reason}).")
        _unreserve_basket_items(pending_info.get('basket_snapshot'))
    # --- END MODIFICATION ---

    return deleted


# --- Data Loading Functions (Synchronous) ---
def load_cities():
    cities_data = {}
    try:
        with get_db_connection() as conn: c = conn.cursor(); c.execute("SELECT id, name FROM cities ORDER BY name"); cities_data = {str(row['id']): row['name'] for row in c.fetchall()}
    except sqlite3.Error as e: logger.error(f"Failed to load cities: {e}")
    return cities_data

def load_districts():
    districts_data = {}
    try:
        with get_db_connection() as conn:
            c = conn.cursor(); c.execute("SELECT d.city_id, d.id, d.name FROM districts d ORDER BY d.city_id, d.name")
            for row in c.fetchall(): city_id_str = str(row['city_id']); districts_data.setdefault(city_id_str, {})[str(row['id'])] = row['name']
    except sqlite3.Error as e: logger.error(f"Failed to load districts: {e}")
    return districts_data

def load_product_types():
    product_types_dict = {}
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name, COALESCE(emoji, ?) as emoji FROM product_types ORDER BY name", (DEFAULT_PRODUCT_EMOJI,))
            product_types_dict = {row['name']: row['emoji'] for row in c.fetchall()}
    except sqlite3.Error as e:
        logger.error(f"Failed to load product types and emojis: {e}")
    return product_types_dict

def load_all_data():
    """Loads all dynamic data, modifying global variables IN PLACE."""
    global CITIES, DISTRICTS, PRODUCT_TYPES
    logger.info("Starting load_all_data (in-place update)...")
    try:
        cities_data = load_cities()
        districts_data = load_districts()
        product_types_dict = load_product_types()

        CITIES.clear(); CITIES.update(cities_data)
        DISTRICTS.clear(); DISTRICTS.update(districts_data)
        PRODUCT_TYPES.clear(); PRODUCT_TYPES.update(product_types_dict)

        logger.info(f"Loaded (in-place) {len(CITIES)} cities, {sum(len(d) for d in DISTRICTS.values())} districts, {len(PRODUCT_TYPES)} product types.")
    except Exception as e:
        logger.error(f"Error during load_all_data (in-place): {e}", exc_info=True)
        CITIES.clear(); DISTRICTS.clear(); PRODUCT_TYPES.clear()


# --- Bot Media Loading (from specified path on disk) ---
if os.path.exists(BOT_MEDIA_JSON_PATH):
    try:
        with open(BOT_MEDIA_JSON_PATH, 'r') as f: BOT_MEDIA = json.load(f)
        logger.info(f"Loaded BOT_MEDIA from {BOT_MEDIA_JSON_PATH}: {BOT_MEDIA}")
        if BOT_MEDIA.get("path"):
            filename = os.path.basename(BOT_MEDIA["path"]); correct_path = os.path.join(MEDIA_DIR, filename)
            if BOT_MEDIA["path"] != correct_path: logger.warning(f"Correcting BOT_MEDIA path from {BOT_MEDIA['path']} to {correct_path}"); BOT_MEDIA["path"] = correct_path
    except Exception as e: logger.warning(f"Could not load/parse {BOT_MEDIA_JSON_PATH}: {e}. Using default BOT_MEDIA.")
else: logger.info(f"{BOT_MEDIA_JSON_PATH} not found. Bot starting without default media.")


async def save_bot_media_config(media_type: str, media_path: str):
    """Save bot media configuration to file and update global BOT_MEDIA."""
    global BOT_MEDIA
    
    try:
        # Update global BOT_MEDIA
        BOT_MEDIA = {'type': media_type, 'path': media_path}
        
        # Save to file
        await asyncio.to_thread(_write_bot_media_config, BOT_MEDIA)
        
        logger.info(f"Bot media configuration saved: type={media_type}, path={media_path}")
        
    except Exception as e:
        logger.error(f"Error saving bot media configuration: {e}", exc_info=True)
        raise


def _write_bot_media_config(bot_media_data: dict):
    """Synchronous function to write bot media config to file."""
    import json
    with open(BOT_MEDIA_JSON_PATH, 'w') as f:
        json.dump(bot_media_data, f, indent=2)
    logger.debug(f"Bot media config written to {BOT_MEDIA_JSON_PATH}")


async def is_user_banned(user_id: int) -> bool:
    """Check if a user is banned. Returns True if banned, False otherwise.
    
    Args:
        user_id: The Telegram user ID to check
        
    Returns:
        bool: True if user is banned, False if not banned or if user doesn't exist
    """
    # Skip ban check for admins
    if user_id == ADMIN_ID or user_id in SECONDARY_ADMIN_IDS:
        return False
    
    conn = None
    max_retries = 3
    retry_delay = 0.1  # 100ms
    
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
            res = c.fetchone()
            return res and res['is_banned'] == 1
        except sqlite3.Error as e:
            if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                logger.warning(f"Database locked for ban check (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
                continue
            else:
                logger.error(f"DB error checking ban status for user {user_id}: {e}")
                return False  # Default to not banned if there's a DB error
        finally:
            if conn:
                conn.close()
                conn = None
    
    return False  # Default to not banned if all retries failed


# --- Utility Functions ---
def _get_lang_data(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, dict]:
    """Gets the current language code and corresponding language data dictionary.
    Safely handles None context or None user_data (from background jobs).
    """
    lang = "en"  # Default
    if context is not None and hasattr(context, 'user_data') and context.user_data is not None:
        lang = context.user_data.get("lang", "en")
    # Uses LANGUAGES dict defined above in this file
    lang_data = LANGUAGES.get(lang, LANGUAGES['en'])
    if lang not in LANGUAGES:
        logger.warning(f"_get_lang_data: Language '{lang}' not found in LANGUAGES dict. Falling back to 'en'.")
        lang = 'en' # Ensure lang variable reflects the fallback
    return lang, lang_data

def format_currency(value):
    try: return f"{Decimal(str(value)):.2f}"
    except (ValueError, TypeError): logger.warning(f"Could format currency {value}"); return "0.00"

def format_discount_value(dtype, value):
    try:
        if dtype == 'percentage': return f"{Decimal(str(value)):.1f}%"
        elif dtype == 'fixed': return f"{format_currency(value)} EUR"
        return str(value)
    except (ValueError, TypeError): logger.warning(f"Could not format discount {dtype} {value}"); return "N/A"

def get_progress_bar(purchases):
    try:
        p_int = int(purchases); thresholds = [0, 2, 5, 8, 10]
        filled = min(sum(1 for t in thresholds if p_int >= t), 5)
        return '[' + '�ŸŸ�' * filled + '��œ️' * (5 - filled) + ']'
    except (ValueError, TypeError): return '[��œ️��œ️��œ️��œ️��œ️]'


# ============================================================================
# TELEGRAM RATE LIMITING SYSTEM - 100% Delivery Guarantee
# ============================================================================

class TelegramRateLimiter:
    """
    Proactive rate limiter to prevent Telegram 429 errors.
    Ensures we stay within Telegram's limits:
    - Global: 30 msgs/sec (we use 25 for safety)
    - Per-chat: 20 msgs/sec (we use 16 for safety)
    """
    GLOBAL_MIN_INTERVAL = 0.04  # 25 msgs/sec (83% of 30 limit)
    CHAT_MIN_INTERVAL = 0.06     # 16 msgs/sec (80% of 20 limit)
    
    def __init__(self):
        self._global_lock = asyncio.Lock()
        self._chat_locks = {}
        self._last_global_send = 0.0
        self._last_chat_send = {}
    
    async def acquire(self, chat_id: int):
        """Acquire permission to send to chat_id. Waits if needed."""
        import time
        current_time = time.time()
        
        # Global rate limit
        async with self._global_lock:
            time_since_last = current_time - self._last_global_send
            if time_since_last < self.GLOBAL_MIN_INTERVAL:
                wait_time = self.GLOBAL_MIN_INTERVAL - time_since_last
                await asyncio.sleep(wait_time)
            self._last_global_send = time.time()
        
        # Per-chat rate limit
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        
        async with self._chat_locks[chat_id]:
            last_send = self._last_chat_send.get(chat_id, 0.0)
            time_since_last = time.time() - last_send
            if time_since_last < self.CHAT_MIN_INTERVAL:
                wait_time = self.CHAT_MIN_INTERVAL - time_since_last
                await asyncio.sleep(wait_time)
            self._last_chat_send[chat_id] = time.time()

# Global rate limiter instance
_telegram_rate_limiter = TelegramRateLimiter()


async def send_message_with_retry(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    max_retries=5,  # Increased from 3 to 5 for higher success rate
    parse_mode=None,
    disable_web_page_preview=False
):
    """
    Send message with automatic retry and rate limiting.
    - Rate limits BEFORE sending to prevent 429 errors
    - Handles RetryAfter exceptions automatically  
    - 5 retries with exponential backoff
    - Returns None only for permanent failures
    """
    for attempt in range(max_retries):
        try:
            # Rate limit BEFORE sending to prevent 429 errors
            await _telegram_rate_limiter.acquire(chat_id)
            
            result = await bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup,
                parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview
            )
            # Log success for debugging
            if attempt > 0:
                logger.info(f"✅ Message sent to {chat_id} after {attempt + 1} attempts")
            return result
        except telegram_error.BadRequest as e:
            error_lower = str(e).lower()
            logger.warning(f"BadRequest sending to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            # Unrecoverable errors - don't retry
            if any(phrase in error_lower for phrase in ["chat not found", "bot was blocked", "user is deactivated", "message is too long"]):
                logger.error(f"Unrecoverable BadRequest sending to {chat_id}: {e}. Aborting retries.")
                return None
            if attempt < max_retries - 1: 
                await asyncio.sleep(1 * (2 ** attempt))  # Exponential backoff: 1s, 2s, 4s, 8s, 16s
                continue
            else: 
                logger.error(f"Max retries reached for BadRequest sending to {chat_id}: {e}")
                break
        except telegram_error.RetryAfter as e:
            retry_seconds = e.retry_after + 2  # Add 2 second buffer
            logger.warning(f"⏳ Rate limit (429) for chat {chat_id}. Retrying after {retry_seconds}s")
            if retry_seconds > 120:  # Increased from 60 to 120 seconds
                logger.error(f"RetryAfter requested > 120s ({retry_seconds}s). Aborting for chat {chat_id}.")
                return None
            await asyncio.sleep(retry_seconds)
            continue  # Don't count as attempt
        except telegram_error.NetworkError as e:
            logger.warning(f"NetworkError sending to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1: 
                await asyncio.sleep(2 * (2 ** attempt))  # Exponential backoff: 2s, 4s, 8s, 16s, 32s
                continue
            else: 
                logger.error(f"Max retries reached for NetworkError sending to {chat_id}: {e}")
                break
        except telegram_error.Forbidden: 
            logger.warning(f"Forbidden error sending to {chat_id}. User may have blocked the bot. Aborting.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending message to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries - 1: 
                await asyncio.sleep(1 * (2 ** attempt))
                continue
            else: 
                logger.error(f"Max retries reached after unexpected error sending to {chat_id}: {e}")
                break
    logger.error(f"❌ Failed to send message to {chat_id} after {max_retries} attempts")
    return None


async def send_media_with_retry(
    bot: Bot,
    chat_id: int,
    media,  # File object, file_id, or path
    media_type='photo',  # 'photo', 'video', 'animation', 'document'
    caption=None,
    max_retries=5,
    parse_mode=None
):
    """
    Send media with automatic retry and rate limiting.
    Supports: photo, video, animation, document
    Returns: Message object on success, None on failure
    """
    for attempt in range(max_retries):
        try:
            # Rate limit BEFORE sending
            await _telegram_rate_limiter.acquire(chat_id)
            
            # Send based on media type
            if media_type == 'photo':
                return await bot.send_photo(chat_id=chat_id, photo=media, caption=caption, parse_mode=parse_mode)
            elif media_type == 'video':
                return await bot.send_video(chat_id=chat_id, video=media, caption=caption, parse_mode=parse_mode)
            elif media_type == 'animation':
                return await bot.send_animation(chat_id=chat_id, animation=media, caption=caption, parse_mode=parse_mode)
            elif media_type == 'document':
                return await bot.send_document(chat_id=chat_id, document=media, caption=caption, parse_mode=parse_mode)
            else:
                logger.error(f"Unsupported media_type: {media_type}")
                return None
                
        except telegram_error.BadRequest as e:
            error_lower = str(e).lower()
            logger.warning(f"BadRequest sending {media_type} to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            if any(phrase in error_lower for phrase in ["chat not found", "bot was blocked", "user is deactivated", "wrong file identifier"]):
                logger.error(f"Unrecoverable BadRequest sending {media_type} to {chat_id}: {e}")
                return None
            if attempt < max_retries - 1: 
                await asyncio.sleep(1 * (2 ** attempt))
                continue
            else: 
                logger.error(f"Max retries reached for BadRequest sending {media_type} to {chat_id}")
                break
        except telegram_error.RetryAfter as e:
            retry_seconds = e.retry_after + 2
            logger.warning(f"⏳ Rate limit (429) for chat {chat_id}. Retrying {media_type} after {retry_seconds}s")
            if retry_seconds > 120:
                logger.error(f"RetryAfter > 120s for {media_type} to {chat_id}. Aborting.")
                return None
            await asyncio.sleep(retry_seconds)
            continue
        except telegram_error.NetworkError as e:
            logger.warning(f"NetworkError sending {media_type} to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1: 
                await asyncio.sleep(2 * (2 ** attempt))
                continue
            else: 
                logger.error(f"Max retries reached for NetworkError sending {media_type} to {chat_id}")
                break
        except telegram_error.Forbidden: 
            logger.warning(f"Forbidden error sending {media_type} to {chat_id}. User blocked bot.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending {media_type} to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries - 1: 
                await asyncio.sleep(1 * (2 ** attempt))
                continue
            else: 
                logger.error(f"Max retries reached sending {media_type} to {chat_id}")
                break
    logger.error(f"❌ Failed to send {media_type} to {chat_id} after {max_retries} attempts")
    return None


async def send_media_group_with_retry(
    bot: Bot,
    chat_id: int,
    media,  # List of InputMedia objects
    max_retries=5,
    caption=None
):
    """
    Send media group with automatic retry and rate limiting.
    - Validates group size (max 10 items)
    - Same retry logic as individual media
    - Returns: List of Message objects on success, None on failure
    """
    # Validate media group size
    if not media or len(media) == 0:
        logger.error(f"Empty media group for chat {chat_id}")
        return None
    if len(media) > 10:
        logger.error(f"Media group too large ({len(media)} items) for chat {chat_id}. Max 10 items.")
        return None
    
    for attempt in range(max_retries):
        try:
            # Rate limit BEFORE sending
            await _telegram_rate_limiter.acquire(chat_id)
            
            return await bot.send_media_group(chat_id=chat_id, media=media)
                
        except telegram_error.BadRequest as e:
            error_lower = str(e).lower()
            logger.warning(f"BadRequest sending media group to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            if any(phrase in error_lower for phrase in ["chat not found", "bot was blocked", "user is deactivated", "wrong file identifier"]):
                logger.error(f"Unrecoverable BadRequest sending media group to {chat_id}: {e}")
                return None
            if attempt < max_retries - 1: 
                await asyncio.sleep(1 * (2 ** attempt))
                continue
            else: 
                logger.error(f"Max retries reached for BadRequest sending media group to {chat_id}")
                break
        except telegram_error.RetryAfter as e:
            retry_seconds = e.retry_after + 2
            logger.warning(f"⏳ Rate limit (429) for chat {chat_id}. Retrying media group after {retry_seconds}s")
            if retry_seconds > 120:
                logger.error(f"RetryAfter > 120s for media group to {chat_id}. Aborting.")
                return None
            await asyncio.sleep(retry_seconds)
            continue
        except telegram_error.NetworkError as e:
            logger.warning(f"NetworkError sending media group to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}")
            if attempt < max_retries - 1: 
                await asyncio.sleep(2 * (2 ** attempt))
                continue
            else: 
                logger.error(f"Max retries reached for NetworkError sending media group to {chat_id}")
                break
        except telegram_error.Forbidden: 
            logger.warning(f"Forbidden error sending media group to {chat_id}. User blocked bot.")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending media group to {chat_id} (Attempt {attempt+1}/{max_retries}): {e}", exc_info=True)
            if attempt < max_retries - 1: 
                await asyncio.sleep(1 * (2 ** attempt))
                continue
            else: 
                logger.error(f"Max retries reached sending media group to {chat_id}")
                break
    logger.error(f"❌ Failed to send media group ({len(media)} items) to {chat_id} after {max_retries} attempts")
    return None


def get_date_range(period_key):
    now = datetime.now(timezone.utc) # Use UTC now
    try:
        if period_key == 'today': start = now.replace(hour=0, minute=0, second=0, microsecond=0); end = now
        elif period_key == 'yesterday': yesterday = now - timedelta(days=1); start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0); end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'week': start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); end = now
        elif period_key == 'last_week': start_of_this_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); end_of_last_week = start_of_this_week - timedelta(microseconds=1); start = (end_of_last_week - timedelta(days=end_of_last_week.weekday())).replace(hour=0, minute=0, second=0, microsecond=0); end = end_of_last_week.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'month': start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0); end = now
        elif period_key == 'last_month': first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0); end_of_last_month = first_of_this_month - timedelta(microseconds=1); start = end_of_last_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0); end = end_of_last_month.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif period_key == 'year': start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0); end = now
        else: return None, None
        # Return ISO format strings (already in UTC)
        return start.isoformat(), end.isoformat()
    except Exception as e: logger.error(f"Error calculating date range for '{period_key}': {e}"); return None, None


def get_user_status(purchases):
    try:
        p_int = int(purchases)
        if p_int >= 10: return "VIP �Ÿ‘‘"
        elif p_int >= 5: return "Regular ⭐"
        else: return "New 🌐"
    except (ValueError, TypeError): return "New 🌐"

# --- Modified clear_expired_basket (Individual user focus) ---
def clear_expired_basket(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    if 'basket' not in context.user_data: context.user_data['basket'] = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("BEGIN")
        c.execute("SELECT basket FROM users WHERE user_id = ?", (user_id,))
        result = c.fetchone(); basket_str = result['basket'] if result else ''
        if not basket_str:
            # If DB basket is empty, ensure context basket is also empty
            if context.user_data.get('basket'): context.user_data['basket'] = []
            if context.user_data.get('applied_discount'): context.user_data.pop('applied_discount', None)
            c.execute("COMMIT"); # Commit potential state change from BEGIN
            return # Exit early if no basket string in DB

        items = basket_str.split(',')
        current_time = time.time(); valid_items_str_list = []; valid_items_userdata_list = []
        expired_product_ids_counts = Counter(); expired_items_found = False
        potential_prod_ids = []
        for item_part in items:
            if item_part and ':' in item_part:
                try: potential_prod_ids.append(int(item_part.split(':')[0]))
                except ValueError: logger.warning(f"Invalid product ID format in basket string '{item_part}' for user {user_id}")

        product_details = {}
        if potential_prod_ids:
             placeholders = ','.join('?' * len(potential_prod_ids))
             # Fetch product_type along with price
             c.execute(f"SELECT id, price, product_type FROM products WHERE id IN ({placeholders})", potential_prod_ids)
             product_details = {row['id']: {'price': Decimal(str(row['price'])), 'type': row['product_type']} for row in c.fetchall()}

        for item_str in items:
            if not item_str: continue
            try:
                prod_id_str, ts_str = item_str.split(':'); prod_id = int(prod_id_str); ts = float(ts_str)
                if current_time - ts <= BASKET_TIMEOUT:
                    valid_items_str_list.append(item_str)
                    details = product_details.get(prod_id)
                    if details:
                        # Add product_type to context item
                        valid_items_userdata_list.append({
                            "product_id": prod_id,
                            "price": details['price'], # Original price
                            "product_type": details['type'], # Store product type
                            "timestamp": ts
                        })
                    else: logger.warning(f"P{prod_id} details not found during basket validation (user {user_id}).")
                else:
                    expired_product_ids_counts[prod_id] += 1
                    expired_items_found = True
            except (ValueError, IndexError) as e: logger.warning(f"Malformed item '{item_str}' in basket for user {user_id}: {e}")

        if expired_items_found:
            new_basket_str = ','.join(valid_items_str_list)
            c.execute("UPDATE users SET basket = ? WHERE user_id = ?", (new_basket_str, user_id))
            if expired_product_ids_counts:
                decrement_data = [(count, pid) for pid, count in expired_product_ids_counts.items()]
                c.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data)
                logger.info(f"Released {sum(expired_product_ids_counts.values())} reservations for user {user_id} due to expiry.")

        c.execute("COMMIT") # Commit transaction
        context.user_data['basket'] = valid_items_userdata_list
        if not valid_items_userdata_list and context.user_data.get('applied_discount'):
            context.user_data.pop('applied_discount', None); logger.info(f"Cleared discount for user {user_id} as basket became empty.")

    except sqlite3.Error as e:
        logger.error(f"SQLite error clearing basket user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
    except Exception as e:
        logger.error(f"Unexpected error clearing basket user {user_id}: {e}", exc_info=True)
        if conn and conn.in_transaction: conn.rollback()
    finally:
        if conn: conn.close()

# --- MODIFIED clear_all_expired_baskets (Individual user processing) ---
def clear_all_expired_baskets():
    logger.info("Running scheduled job: clear_all_expired_baskets (Improved)")
    all_expired_product_counts = Counter()
    processed_user_count = 0
    failed_user_count = 0
    conn_outer = None
    users_to_process = []

    # 1. Fetch all users with baskets first
    try:
        conn_outer = get_db_connection()
        c_outer = conn_outer.cursor()
        c_outer.execute("SELECT user_id, basket FROM users WHERE basket IS NOT NULL AND basket != ''")
        users_to_process = c_outer.fetchall() # Fetch all relevant users
    except sqlite3.Error as e:
        logger.error(f"Failed to fetch users for basket clearing job: {e}", exc_info=True)
        return # Cannot proceed if user fetch fails
    finally:
        if conn_outer: conn_outer.close()

    if not users_to_process:
        logger.info("Scheduled clear: No users with active baskets found.")
        return

    logger.info(f"Scheduled clear: Found {len(users_to_process)} users with baskets to check.")
    current_time = time.time()
    user_basket_updates = [] # Batch updates for user basket strings

    # 2. Process each user individually for basket string updates and count expired items
    for user_row in users_to_process:
        user_id = user_row['user_id']
        basket_str = user_row['basket']
        items = basket_str.split(',')
        valid_items_str_list = []
        user_had_expired = False
        user_error = False

        for item_str in items:
            if not item_str: continue
            try:
                prod_id_str, ts_str = item_str.split(':')
                prod_id = int(prod_id_str)
                ts = float(ts_str)
                if current_time - ts <= BASKET_TIMEOUT:
                    valid_items_str_list.append(item_str)
                else:
                    all_expired_product_counts[prod_id] += 1
                    user_had_expired = True
            except (ValueError, IndexError) as e:
                logger.warning(f"Malformed item '{item_str}' user {user_id} in global clear: {e}")
                user_error = True # Mark user had an error, but continue processing others
                continue # Skip this malformed item

        if user_error:
            failed_user_count += 1

        # Only add to batch update if expired items were found for this user
        if user_had_expired:
            new_basket_str = ','.join(valid_items_str_list)
            user_basket_updates.append((new_basket_str, user_id))

        processed_user_count += 1
        # Optional: Add a small sleep if processing many users to avoid bursts
        # time.sleep(0.01) # Using time.sleep in sync function is fine

    # 3. Perform batch updates outside the user loop
    conn_update = None
    try:
        conn_update = get_db_connection()
        c_update = conn_update.cursor()
        c_update.execute("BEGIN") # Start transaction for batch updates

        # Update user basket strings
        if user_basket_updates:
            c_update.executemany("UPDATE users SET basket = ? WHERE user_id = ?", user_basket_updates)
            logger.info(f"Scheduled clear: Updated basket strings for {len(user_basket_updates)} users.")

        # Decrement reservations
        if all_expired_product_counts:
            decrement_data = [(count, pid) for pid, count in all_expired_product_counts.items()]
            if decrement_data:
                c_update.executemany("UPDATE products SET reserved = MAX(0, reserved - ?) WHERE id = ?", decrement_data)
                total_released = sum(all_expired_product_counts.values())
                logger.info(f"Scheduled clear: Released {total_released} expired product reservations.")

        conn_update.commit() # Commit all updates together

    except sqlite3.Error as e:
        logger.error(f"SQLite error during batch updates in clear_all_expired_baskets: {e}", exc_info=True)
        if conn_update and conn_update.in_transaction: conn_update.rollback()
    except Exception as e:
        logger.error(f"Unexpected error during batch updates in clear_all_expired_baskets: {e}", exc_info=True)
        if conn_update and conn_update.in_transaction: conn_update.rollback()
    finally:
        if conn_update: conn_update.close()

    logger.info(f"Scheduled job clear_all_expired_baskets finished. Processed: {processed_user_count}, Users with errors: {failed_user_count}, Total items un-reserved: {sum(all_expired_product_counts.values())}")


def fetch_last_purchases(user_id, limit=10):
    try:
        with get_db_connection() as conn:
            c = conn.cursor(); c.execute("SELECT purchase_date, product_name, product_type, product_size, price_paid FROM purchases WHERE user_id = ? ORDER BY purchase_date DESC LIMIT ?", (user_id, limit))
            return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"DB error fetching purchase history user {user_id}: {e}", exc_info=True); return []

def fetch_reviews(offset=0, limit=5):
    try:
        with get_db_connection() as conn:
            c = conn.cursor(); c.execute("SELECT r.review_id, r.user_id, r.review_text, r.review_date, COALESCE(u.username, 'anonymous') as username FROM reviews r LEFT JOIN users u ON r.user_id = u.user_id ORDER BY r.review_date DESC LIMIT ? OFFSET ?", (limit, offset))
            return [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e: logger.error(f"Failed to fetch reviews (offset={offset}, limit={limit}): {e}", exc_info=True); return []


# --- API Helpers ---
def get_crypto_price_eur(currency_code: str) -> Decimal | None:
    """
    Gets the current price of a cryptocurrency in EUR using CoinGecko API.
    Returns None if the price cannot be fetched.
    """
    currency_code_lower = currency_code.lower()
    now = time.time()
    
    # Check cache first
    if currency_code_lower in currency_price_cache:
        price, timestamp = currency_price_cache[currency_code_lower]
        if now - timestamp < CACHE_EXPIRY_SECONDS:
            logger.debug(f"Cache hit for {currency_code_lower} price: {price} EUR")
            return price
    
    # Map currency codes to CoinGecko IDs
    currency_mapping = {
        'btc': 'bitcoin',
        'eth': 'ethereum',
        'ltc': 'litecoin',
        'sol': 'solana',
        'ton': 'the-open-network',
        'usdttrc20': 'tether',
        'usdterc20': 'tether',
        'usdtbsc': 'tether',
        'usdtsol': 'tether',
        'usdctrc20': 'usd-coin',
        'usdcerc20': 'usd-coin',
        'usdcsol': 'usd-coin',
    }
    
    coingecko_id = currency_mapping.get(currency_code_lower)
    if not coingecko_id:
        logger.warning(f"No CoinGecko mapping found for currency {currency_code_lower}")
        return None
    
    try:
        url = f"{COINGECKO_API_URL}/simple/price"
        params = {
            'ids': coingecko_id,
            'vs_currencies': 'eur'
        }
        
        logger.debug(f"Fetching price for {currency_code_lower} from CoinGecko: {url}")
        response = requests.get(url, params=params, timeout=10)
        logger.debug(f"CoinGecko price response status: {response.status_code}, content: {response.text[:200]}")
        response.raise_for_status()
        
        data = response.json()
        if coingecko_id in data and 'eur' in data[coingecko_id]:
            price = Decimal(str(data[coingecko_id]['eur']))
            currency_price_cache[currency_code_lower] = (price, now)
            logger.info(f"Fetched price for {currency_code_lower}: {price} EUR from CoinGecko.")
            return price
        else:
            logger.warning(f"Price data not found for {coingecko_id} in CoinGecko response: {data}")
            return None
            
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching price for {currency_code_lower} from CoinGecko.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching price for {currency_code_lower} from CoinGecko: {e}")
        if e.response is not None:
            logger.error(f"CoinGecko price error response ({e.response.status_code}): {e.response.text}")
        return None
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.error(f"Error parsing CoinGecko price response for {currency_code_lower}: {e}")
        return None

def format_expiration_time(expiration_date_str: str | None) -> str:
    if not expiration_date_str: return "N/A"
    try:
        # Import pytz for timezone conversion
        import pytz
        
        # Ensure the string ends with timezone info for fromisoformat
        if not expiration_date_str.endswith('Z') and '+' not in expiration_date_str and '-' not in expiration_date_str[10:]:
            expiration_date_str += 'Z' # Assume UTC if no timezone
        dt_obj = datetime.fromisoformat(expiration_date_str.replace('Z', '+00:00'))
        
        # Convert to Lithuanian timezone (Europe/Vilnius)
        lithuanian_tz = pytz.timezone('Europe/Vilnius')
        if dt_obj.tzinfo:
            # Convert UTC to Lithuanian time
            lithuanian_time = dt_obj.astimezone(lithuanian_tz)
            return lithuanian_time.strftime("%H:%M:%S LT")  # LT = Local Time (Lithuanian)
        else:
            # If no timezone info, assume UTC and convert
            utc_time = dt_obj.replace(tzinfo=pytz.UTC)
            lithuanian_time = utc_time.astimezone(lithuanian_tz)
            return lithuanian_time.strftime("%H:%M:%S LT")
    except ImportError:
        # Fallback if pytz is not available - use manual offset
        try:
            if not expiration_date_str.endswith('Z') and '+' not in expiration_date_str and '-' not in expiration_date_str[10:]:
                expiration_date_str += 'Z'
            dt_obj = datetime.fromisoformat(expiration_date_str.replace('Z', '+00:00'))
            # Lithuania is UTC+2 (UTC+3 during DST)
            # For simplicity, add 2 hours (this is a fallback)
            from datetime import timedelta
            lithuanian_time = dt_obj + timedelta(hours=2)
            return lithuanian_time.strftime("%H:%M:%S LT")
        except Exception as fallback_e:
            logger.warning(f"Fallback timezone conversion failed for '{expiration_date_str}': {fallback_e}")
            return "Invalid Date"
    except (ValueError, TypeError) as e: 
        logger.warning(f"Could not parse expiration date string '{expiration_date_str}': {e}"); 
        return "Invalid Date"


# --- Placeholder Handler ---
async def handle_coming_soon(update: Update, context: ContextTypes.DEFAULT_TYPE, params=None):
    query = update.callback_query
    if query:
        try: await query.answer("This feature is coming soon!", show_alert=True); logger.info(f"User {query.from_user.id} clicked coming soon (data: {query.data})")
        except Exception as e: logger.error(f"Error answering 'coming soon' callback: {e}")


# --- Fetch User IDs for Broadcast (Synchronous) ---
def fetch_user_ids_for_broadcast(target_type: str, target_value: str | int | None = None) -> list[int]:
    """Fetches user IDs based on broadcast target criteria."""
    user_ids = []
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if target_type == 'all':
            # Send to ALL users who have ever pressed /start (exist in users table) except banned ones
            # TEMPORARILY REMOVED broadcast_failed_count filtering to ensure ALL users get messages
            c.execute("SELECT user_id FROM users WHERE is_banned = 0 ORDER BY total_purchases DESC")
            user_ids = [row['user_id'] for row in c.fetchall()]
            logger.info(f"Broadcast target 'all': Found {len(user_ids)} users (excluding only banned users).")

        elif target_type == 'status' and target_value:
            status = str(target_value).lower()
            min_purchases, max_purchases = -1, -1
            # Use the status string including emoji for matching (rely on English definition)
            if status == LANGUAGES['en'].get("broadcast_status_vip", "VIP �Ÿ‘‘").lower(): min_purchases = 10; max_purchases = float('inf')
            elif status == LANGUAGES['en'].get("broadcast_status_regular", "Regular ⭐").lower(): min_purchases = 5; max_purchases = 9
            elif status == LANGUAGES['en'].get("broadcast_status_new", "New 🌐").lower(): min_purchases = 0; max_purchases = 4

            if min_purchases != -1:
                 if max_purchases == float('inf'):
                     c.execute("SELECT user_id FROM users WHERE total_purchases >= ? AND is_banned=0", (min_purchases,)) # Exclude banned
                 else:
                     c.execute("SELECT user_id FROM users WHERE total_purchases BETWEEN ? AND ? AND is_banned=0", (min_purchases, max_purchases)) # Exclude banned
                 user_ids = [row['user_id'] for row in c.fetchall()]
                 logger.info(f"Broadcast target status '{target_value}': Found {len(user_ids)} non-banned users.")
            else: logger.warning(f"Invalid status value for broadcast: {target_value}")

        elif target_type == 'city' and target_value:
            city_name = str(target_value)
            # Find non-banned users whose *most recent* purchase was in this city
            c.execute("""
                SELECT p1.user_id
                FROM purchases p1
                JOIN users u ON p1.user_id = u.user_id
                WHERE p1.city = ? AND u.is_banned = 0 AND p1.purchase_date = (
                    SELECT MAX(purchase_date)
                    FROM purchases p2
                    WHERE p1.user_id = p2.user_id
                )
            """, (city_name,))
            user_ids = [row['user_id'] for row in c.fetchall()]
            logger.info(f"Broadcast target city '{city_name}': Found {len(user_ids)} non-banned users based on last purchase.")

        elif target_type == 'inactive' and target_value:
            try:
                days_inactive = int(target_value)
                if days_inactive <= 0: raise ValueError("Days must be positive")
                cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_inactive)
                cutoff_iso = cutoff_date.isoformat()

                # Find non-banned users whose last purchase date is older than the cutoff date OR have no purchases
                # 1. Get users with last purchase older than cutoff
                c.execute("""
                    SELECT p1.user_id
                    FROM purchases p1
                    JOIN users u ON p1.user_id = u.user_id
                    WHERE u.is_banned = 0 AND p1.purchase_date = (
                        SELECT MAX(purchase_date)
                        FROM purchases p2
                        WHERE p1.user_id = p2.user_id
                    ) AND p1.purchase_date < ?
                """, (cutoff_iso,))
                inactive_users = {row['user_id'] for row in c.fetchall()}

                # 2. Get users with zero purchases (who implicitly meet the inactive criteria)
                c.execute("SELECT user_id FROM users WHERE total_purchases = 0 AND is_banned = 0") # Exclude banned
                zero_purchase_users = {row['user_id'] for row in c.fetchall()}

                # Combine the sets
                user_ids_set = inactive_users.union(zero_purchase_users)
                user_ids = list(user_ids_set)
                logger.info(f"Broadcast target inactive >= {days_inactive} days: Found {len(user_ids)} non-banned users.")

            except (ValueError, TypeError):
                logger.error(f"Invalid number of days for inactive broadcast: {target_value}")

        else:
            logger.error(f"Unknown broadcast target type or missing value: type={target_type}, value={target_value}")

    except sqlite3.Error as e:
        logger.error(f"DB error fetching users for broadcast ({target_type}, {target_value}): {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error fetching users for broadcast: {e}", exc_info=True)
    finally:
        if conn: conn.close()

    # IMPROVED: Limit broadcast size to prevent overwhelming the system
    max_broadcast_users = 10000  # Reasonable limit
    if len(user_ids) > max_broadcast_users:
        logger.warning(f"Broadcast target too large ({len(user_ids)} users), limiting to {max_broadcast_users}")
        user_ids = user_ids[:max_broadcast_users]

    return user_ids


# --- User Broadcast Status Tracking (Synchronous) ---
def update_user_broadcast_status(user_id: int, success: bool):
    """Update user's broadcast status based on success/failure."""
    conn = None
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            c = conn.cursor()
            
            if success:
                # Reset failure count and update last active time
                current_time = datetime.now(timezone.utc).isoformat()
                c.execute("""
                    UPDATE users 
                    SET broadcast_failed_count = 0, last_active = ?
                    WHERE user_id = ?
                """, (current_time, user_id))
                logger.debug(f"Reset broadcast failure count for user {user_id}")
            else:
                # Increment failure count
                c.execute("""
                    UPDATE users 
                    SET broadcast_failed_count = COALESCE(broadcast_failed_count, 0) + 1
                    WHERE user_id = ?
                """, (user_id,))
                
                # Check new failure count
                c.execute("SELECT broadcast_failed_count FROM users WHERE user_id = ?", (user_id,))
                result = c.fetchone()
                if result and result['broadcast_failed_count'] >= 5:
                    logger.info(f"User {user_id} marked as unreachable after {result['broadcast_failed_count']} consecutive failures")
            
            conn.commit()
            return  # Success, exit the retry loop
            
        except sqlite3.Error as e:
            logger.error(f"DB error updating broadcast status for user {user_id} (attempt {attempt+1}/{max_retries}): {e}")
            if conn and conn.in_transaction:
                try:
                    conn.rollback()
                except:
                    pass
            if attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))  # Brief delay before retry
                continue
            else:
                logger.error(f"Failed to update broadcast status for user {user_id} after {max_retries} attempts")
        except Exception as e:
            logger.error(f"Unexpected error updating broadcast status for user {user_id} (attempt {attempt+1}/{max_retries}): {e}")
            if conn and conn.in_transaction:
                try:
                    conn.rollback()
                except:
                    pass
            if attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))  # Brief delay before retry
                continue
            else:
                logger.error(f"Failed to update broadcast status for user {user_id} after {max_retries} attempts")
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass
                conn = None


# --- Admin Action Logging (Synchronous) ---

ACTION_RESELLER_ENABLED = "RESELLER_ENABLED"
ACTION_RESELLER_DISABLED = "RESELLER_DISABLED"
ACTION_RESELLER_DISCOUNT_ADD = "RESELLER_DISCOUNT_ADD"
ACTION_RESELLER_DISCOUNT_EDIT = "RESELLER_DISCOUNT_EDIT"
ACTION_RESELLER_DISCOUNT_DELETE = "RESELLER_DISCOUNT_DELETE"

ACTION_PRODUCT_TYPE_REASSIGN = "PRODUCT_TYPE_REASSIGN"
ACTION_BULK_PRICE_UPDATE = "BULK_PRICE_UPDATE"


def log_admin_action(admin_id: int, action: str, target_user_id: int | None = None, reason: str | None = None, amount_change: float | None = None, old_value=None, new_value=None):
    """Logs an administrative action to the admin_log table."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO admin_log (timestamp, admin_id, target_user_id, action, reason, amount_change, old_value, new_value)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                admin_id,
                target_user_id,
                action, # Ensure action string is passed correctly
                reason,
                amount_change,
                str(old_value) if old_value is not None else None,
                str(new_value) if new_value is not None else None
            ))
            conn.commit()
            logger.info(f"Admin Action Logged: Admin={admin_id}, Action='{action}', Target={target_user_id}, Reason='{reason}', Amount={amount_change}, Old='{old_value}', New='{new_value}'")
    except sqlite3.Error as e:
        logger.error(f"Failed to log admin action: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error logging admin action: {e}", exc_info=True)

# --- Admin Authorization Helpers ---
def is_primary_admin(user_id: int) -> bool:
    """Check if a user ID is a primary admin."""
    return user_id in PRIMARY_ADMIN_IDS

def is_secondary_admin(user_id: int) -> bool:
    """Check if a user ID is a secondary admin."""
    return user_id in SECONDARY_ADMIN_IDS

def is_any_admin(user_id: int) -> bool:
    """Check if a user ID is either a primary or secondary admin."""
    return is_primary_admin(user_id) or is_secondary_admin(user_id)

def get_first_primary_admin_id() -> int | None:
    """Get the first primary admin ID for legacy compatibility, or None if none configured."""
    return PRIMARY_ADMIN_IDS[0] if PRIMARY_ADMIN_IDS else None

# --- Welcome Message Helpers (Synchronous) ---
def load_active_welcome_message() -> str:
    """Loads the currently active welcome message template from the database."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = ?", ("active_welcome_message_name",))
        setting_row = c.fetchone()
        active_name = setting_row['setting_value'] if setting_row else "default"

        c.execute("SELECT template_text FROM welcome_messages WHERE name = ?", (active_name,))
        template_row = c.fetchone()
        if template_row:
            logger.info(f"Loaded active welcome message template: '{active_name}'")
            return template_row['template_text']
        else:
            # If active template name points to a non-existent template, try fallback
            logger.warning(f"Active welcome message template '{active_name}' not found. Trying 'default'.")
            c.execute("SELECT template_text FROM welcome_messages WHERE name = ?", ("default",))
            template_row = c.fetchone()
            if template_row:
                logger.info("Loaded fallback 'default' welcome message template.")
                # Optionally update setting to default?
                # c.execute("UPDATE bot_settings SET setting_value = ? WHERE setting_key = ?", ("default", "active_welcome_message_name"))
                # conn.commit()
                return template_row['template_text']
            else:
                # If even default is missing
                logger.error("FATAL: Default welcome message template 'default' not found in DB! Using hardcoded default.")
                return DEFAULT_WELCOME_MESSAGE

    except sqlite3.Error as e:
        logger.error(f"DB error loading active welcome message: {e}", exc_info=True)
        return DEFAULT_WELCOME_MESSAGE
    except Exception as e:
        logger.error(f"Unexpected error loading welcome message: {e}", exc_info=True)
        return DEFAULT_WELCOME_MESSAGE
    finally:
        if conn: conn.close()


def get_welcome_message_templates(limit: int | None = None, offset: int = 0) -> list[dict]:
    """Fetches welcome message templates (name, text, description), optionally paginated."""
    templates = []
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            query = "SELECT name, template_text, description FROM welcome_messages ORDER BY name"
            params = []
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params.extend([limit, offset])
            c.execute(query, params)
            templates = [dict(row) for row in c.fetchall()]
    except sqlite3.Error as e:
        logger.error(f"DB error fetching welcome message templates: {e}", exc_info=True)
    return templates


def get_welcome_message_template_count() -> int:
    """Gets the total number of welcome message templates."""
    count = 0
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM welcome_messages")
            result = c.fetchone()
            if result: count = result[0]
    except sqlite3.Error as e:
        logger.error(f"DB error counting welcome message templates: {e}", exc_info=True)
    return count


def add_welcome_message_template(name: str, template_text: str, description: str | None = None) -> bool:
    """Adds a new welcome message template."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO welcome_messages (name, template_text, description) VALUES (?, ?, ?)",
                      (name, template_text, description))
            conn.commit()
            logger.info(f"Added welcome message template: '{name}'")
            return True
    except sqlite3.IntegrityError:
        logger.warning(f"Attempted to add duplicate welcome message template name: '{name}'")
        return False
    except sqlite3.Error as e:
        logger.error(f"DB error adding welcome message template '{name}': {e}", exc_info=True)
        return False


def update_welcome_message_template(name: str, new_template_text: str | None = None, new_description: str | None = None) -> bool:
    """Updates the text and/or description of an existing welcome message template."""
    if new_template_text is None and new_description is None:
        logger.warning("Update welcome template called without providing new text or description.")
        return False
    updates = []
    params = []
    if new_template_text is not None:
        updates.append("template_text = ?")
        params.append(new_template_text)
    if new_description is not None:
        # Handle empty string description as NULL
        desc_to_save = new_description if new_description else None
        updates.append("description = ?")
        params.append(desc_to_save)

    params.append(name)
    sql = f"UPDATE welcome_messages SET {', '.join(updates)} WHERE name = ?"

    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            result = c.execute(sql, params)
            conn.commit()
            if result.rowcount > 0:
                logger.info(f"Updated welcome message template: '{name}'")
                return True
            else:
                logger.warning(f"Welcome message template '{name}' not found for update.")
                return False
    except sqlite3.Error as e:
        logger.error(f"DB error updating welcome message template '{name}': {e}", exc_info=True)
        return False

def delete_welcome_message_template(name: str) -> bool:
    """Deletes a welcome message template."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # Check if it's the active one (handled better in admin logic now)
            result = c.execute("DELETE FROM welcome_messages WHERE name = ?", (name,))
            conn.commit()
            if result.rowcount > 0:
                logger.info(f"Deleted welcome message template: '{name}'")
                return True
            else:
                logger.warning(f"Welcome message template '{name}' not found for deletion.")
                return False
    except sqlite3.Error as e:
        logger.error(f"DB error deleting welcome message template '{name}': {e}", exc_info=True)
        return False

def set_active_welcome_message(name: str) -> bool:
    """Sets the active welcome message template name in bot_settings."""
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            # First check if the template name actually exists
            c.execute("SELECT 1 FROM welcome_messages WHERE name = ?", (name,))
            if not c.fetchone():
                logger.error(f"Attempted to activate non-existent welcome template: '{name}'")
                return False
            # Update or insert the setting
            c.execute("INSERT OR REPLACE INTO bot_settings (setting_key, setting_value) VALUES (?, ?)",
                      ("active_welcome_message_name", name))
            conn.commit()
            logger.info(f"Set active welcome message template to: '{name}'")
            return True
    except sqlite3.Error as e:
        logger.error(f"DB error setting active welcome message to '{name}': {e}", exc_info=True)
        return False

# --- PAYMENT RESERVATION TIMEOUT (2 hours for crypto payments) ---
PAYMENT_TIMEOUT_MINUTES_STR = os.environ.get("PAYMENT_TIMEOUT_MINUTES", "120")  # Increased from 30 to 120 minutes
try:
    PAYMENT_TIMEOUT_MINUTES = int(PAYMENT_TIMEOUT_MINUTES_STR)
    if PAYMENT_TIMEOUT_MINUTES <= 0:
        logger.warning("PAYMENT_TIMEOUT_MINUTES non-positive, using default 120 min.")
        PAYMENT_TIMEOUT_MINUTES = 120
except ValueError:
    logger.warning("Invalid PAYMENT_TIMEOUT_MINUTES, using default 120 min.")
    PAYMENT_TIMEOUT_MINUTES = 120

PAYMENT_TIMEOUT_SECONDS = PAYMENT_TIMEOUT_MINUTES * 60
logger.info(f"Payment timeout set to {PAYMENT_TIMEOUT_MINUTES} minutes ({PAYMENT_TIMEOUT_SECONDS} seconds).")

# --- ABANDONED RESERVATION TIMEOUT (30 minutes) ---
ABANDONED_RESERVATION_TIMEOUT_MINUTES = 30  # Timeout for items reserved but payment not started
ABANDONED_RESERVATION_TIMEOUT_SECONDS = ABANDONED_RESERVATION_TIMEOUT_MINUTES * 60
logger.info(f"Abandoned reservation timeout set to {ABANDONED_RESERVATION_TIMEOUT_MINUTES} minutes.")

# Global dictionary to track reservation timestamps
_reservation_timestamps = {}  # {user_id: {'timestamp': time.time(), 'snapshot': [...], 'type': 'single'/'basket'}}

def track_reservation(user_id: int, snapshot: list, reservation_type: str):
    """Track when a user reserves items so we can clean up abandoned reservations."""
    global _reservation_timestamps
    _reservation_timestamps[user_id] = {
        'timestamp': time.time(),
        'snapshot': snapshot,
        'type': reservation_type
    }
    logger.debug(f"Tracking {reservation_type} reservation for user {user_id}: {len(snapshot)} items")

def clear_reservation_tracking(user_id: int):
    """Clear reservation tracking when user proceeds to payment or cancels."""
    global _reservation_timestamps
    if user_id in _reservation_timestamps:
        logger.debug(f"Cleared reservation tracking for user {user_id}")
        del _reservation_timestamps[user_id]

def clean_abandoned_reservations():
    """Clean up items reserved by users who abandoned the payment flow without proceeding to invoice creation."""
    global _reservation_timestamps
    
    current_time = time.time()
    cutoff_time = current_time - ABANDONED_RESERVATION_TIMEOUT_SECONDS
    
    abandoned_users = []
    
    # Find users with abandoned reservations
    for user_id, reservation_data in _reservation_timestamps.items():
        if reservation_data['timestamp'] < cutoff_time:
            abandoned_users.append(user_id)
    
    if not abandoned_users:
        logger.debug("No abandoned reservations found.")
        return
    
    logger.info(f"Found {len(abandoned_users)} users with abandoned reservations to clean up.")
    
    # Process each abandoned reservation
    cleaned_count = 0
    for user_id in abandoned_users:
        try:
            reservation_data = _reservation_timestamps.get(user_id)
            if not reservation_data:
                continue
                
            snapshot = reservation_data['snapshot']
            reservation_type = reservation_data['type']
            
            # Unreserve the items
            _unreserve_basket_items(snapshot)
            
            # Remove from tracking
            del _reservation_timestamps[user_id]
            
            cleaned_count += 1
            logger.info(f"Cleaned up abandoned {reservation_type} reservation for user {user_id}: {len(snapshot)} items unreserved")
            
        except Exception as e:
            logger.error(f"Error cleaning up abandoned reservation for user {user_id}: {e}", exc_info=True)
    
    logger.info(f"Cleaned up {cleaned_count}/{len(abandoned_users)} abandoned reservations.")

# --- NEW: Clean up expired pending payments and unreserve items ---
def get_expired_payments_for_notification():
    """
    Gets information about expired pending payments for user notifications.
    Returns a list of user info for notifications before the records are cleaned up.
    """
    current_time = time.time()
    cutoff_timestamp = current_time - PAYMENT_TIMEOUT_SECONDS
    cutoff_datetime = datetime.fromtimestamp(cutoff_timestamp, tz=timezone.utc)
    
    user_notifications = []
    conn = None
    
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Find expired pending purchases and get user language info
        c.execute("""
            SELECT pd.user_id, u.language
            FROM pending_deposits pd
            JOIN users u ON pd.user_id = u.user_id
            WHERE pd.is_purchase = 1 
            AND pd.created_at < ? 
            ORDER BY pd.created_at
        """, (cutoff_datetime.isoformat(),))
        
        expired_records = c.fetchall()
        
        for record in expired_records:
            user_notifications.append({
                'user_id': record['user_id'],
                'language': record['language'] or 'en'
            })
            
    except sqlite3.Error as e:
        logger.error(f"DB error while getting expired payments for notification: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()
    
    return user_notifications


def clean_expired_pending_payments():
    """
    Checks for pending payments that have expired (older than PAYMENT_TIMEOUT_SECONDS)
    and automatically unreserves the items and removes the pending records.
    """
    logger.info("Running scheduled job: clean_expired_pending_payments")
    
    current_time = time.time()
    cutoff_timestamp = current_time - PAYMENT_TIMEOUT_SECONDS
    cutoff_datetime = datetime.fromtimestamp(cutoff_timestamp, tz=timezone.utc)
    
    expired_purchases = []
    conn = None
    
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Find expired pending purchases (not refills) older than cutoff time
        c.execute("""
            SELECT payment_id, user_id, basket_snapshot_json, created_at
            FROM pending_deposits 
            WHERE is_purchase = 1 
            AND created_at < ? 
            ORDER BY created_at
        """, (cutoff_datetime.isoformat(),))
        
        expired_records = c.fetchall()
        
        if not expired_records:
            logger.debug("No expired pending payments found.")
            return
            
        logger.info(f"Found {len(expired_records)} expired pending payments to clean up.")
        
        for record in expired_records:
            payment_id = record['payment_id']
            user_id = record['user_id']
            basket_snapshot_json = record['basket_snapshot_json']
            created_at = record['created_at']
            
            logger.info(f"Processing expired payment {payment_id} for user {user_id} (created: {created_at})")
            
            # Deserialize basket snapshot if present
            basket_snapshot = None
            if basket_snapshot_json:
                try:
                    basket_snapshot = json.loads(basket_snapshot_json)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode basket_snapshot_json for expired payment {payment_id}: {e}")
                    basket_snapshot = None
            
            # Collect info for later processing
            expired_purchases.append({
                'payment_id': payment_id,
                'user_id': user_id,
                'basket_snapshot': basket_snapshot
            })
            
    except sqlite3.Error as e:
        logger.error(f"DB error while checking expired pending payments: {e}", exc_info=True)
        return
    finally:
        if conn:
            conn.close()
    
    # Process each expired payment
    processed_count = 0
    for expired_payment in expired_purchases:
        payment_id = expired_payment['payment_id']
        user_id = expired_payment['user_id']
        basket_snapshot = expired_payment['basket_snapshot']
        
        try:
            # Remove the pending deposit record (this will trigger unreserving via remove_pending_deposit)
            success = remove_pending_deposit(payment_id, trigger="timeout_expiry")
            if success:
                processed_count += 1
                logger.info(f"Successfully cleaned up expired payment {payment_id} for user {user_id}")
            else:
                logger.warning(f"Failed to remove expired pending payment {payment_id} for user {user_id}")
                
        except Exception as e:
            logger.error(f"Error processing expired payment {payment_id} for user {user_id}: {e}", exc_info=True)
    
    logger.info(f"Cleaned up {processed_count}/{len(expired_purchases)} expired pending payments.")


# ============================================================================
# BULLETPROOF PAYMENT RECOVERY SYSTEM
# ============================================================================

def get_failed_payments_for_recovery():
    """Get all payments that failed during processing and need recovery.
    SAFETY: Only returns payments that haven't been processed in solana_wallets."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # FIXED: Use correct column name basket_snapshot_json
        # SAFETY: Exclude payments that are already marked as 'paid' in solana_wallets
        c.execute("""
            SELECT pd.payment_id, pd.user_id, pd.target_eur_amount, pd.currency, pd.expected_crypto_amount,
                   pd.basket_snapshot_json, pd.discount_code_used, pd.created_at
            FROM pending_deposits pd
            LEFT JOIN solana_wallets sw ON pd.payment_id = sw.order_id
            WHERE pd.created_at < datetime('now', '-10 minutes')
            AND pd.is_purchase = 1
            AND (sw.status IS NULL OR sw.status = 'pending')
            ORDER BY pd.created_at ASC
        """)
        
        failed_payments = []
        for row in c.fetchall():
            # Parse basket_snapshot_json back to list
            basket_snapshot = None
            if row[5]:  # basket_snapshot_json
                try:
                    basket_snapshot = json.loads(row[5])
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse basket_snapshot_json for payment {row[0]}")
                    basket_snapshot = None
            
            failed_payments.append({
                'payment_id': row[0],
                'user_id': row[1],
                'target_eur_amount': row[2],
                'currency': row[3],
                'expected_crypto_amount': row[4],
                'basket_snapshot': basket_snapshot,  # Now properly parsed
                'discount_code_used': row[6],
                'created_at': row[7]
            })
        
        conn.close()
        return failed_payments
    except Exception as e:
        logger.error(f"Error getting failed payments for recovery: {e}")
        return []


def recover_failed_payment(payment_id, user_id, basket_snapshot, discount_code_used, dummy_context):
    """Attempt to recover a failed payment by reprocessing it"""
    try:
        logger.info(f"�Ÿ”„ BULLETPROOF RECOVERY: Attempting to recover payment {payment_id} for user {user_id}")
        
        # Import here to avoid circular imports
        from payment import process_successful_crypto_purchase
        
        # Process the payment again
        success = process_successful_crypto_purchase(
            user_id, basket_snapshot, discount_code_used, payment_id, dummy_context
        )
        
        if success:
            logger.info(f"✅ BULLETPROOF RECOVERY: Successfully recovered payment {payment_id} for user {user_id}")
            # Remove from pending deposits
            remove_pending_deposit(payment_id, trigger="recovery_success")
            return True
        else:
            logger.warning(f"⚠️ BULLETPROOF RECOVERY: Failed to recover payment {payment_id} for user {user_id}")
            return False
            
    except Exception as e:
        logger.error(f"❌ BULLETPROOF RECOVERY: Error recovering payment {payment_id} for user {user_id}: {e}")
        return False


def run_payment_recovery_job():
    """Run the payment recovery job to process failed payments"""
    try:
        logger.info("�Ÿ”„ BULLETPROOF: Starting payment recovery job")
        
        failed_payments = get_failed_payments_for_recovery()
        if not failed_payments:
            logger.info("✅ BULLETPROOF: No failed payments found for recovery")
            return
        
        logger.info(f"�Ÿ”„ BULLETPROOF: Found {len(failed_payments)} failed payments for recovery")
        
        # Import here to avoid circular imports
        from main import telegram_app, get_first_primary_admin_id, send_message_with_retry
        
        if not telegram_app:
            logger.error("❌ BULLETPROOF: Telegram app not available for recovery")
            return
        
        recovered_count = 0
        for payment in failed_payments:
            try:
                # Create dummy context
                dummy_context = ContextTypes.DEFAULT_TYPE(
                    application=telegram_app, 
                    chat_id=payment['user_id'], 
                    user_id=payment['user_id']
                )
                
                # Attempt recovery
                if recover_failed_payment(
                    payment['payment_id'], 
                    payment['user_id'], 
                    payment['basket_snapshot'], 
                    payment['discount_code_used'], 
                    dummy_context
                ):
                    recovered_count += 1
                    
            except Exception as e:
                logger.error(f"❌ BULLETPROOF: Error processing recovery for payment {payment['payment_id']}: {e}")
        
        logger.info(f"✅ BULLETPROOF: Payment recovery completed. Recovered {recovered_count}/{len(failed_payments)} payments")
        
        # Notify admin about recovery results
        if get_first_primary_admin_id() and recovered_count > 0:
            try:
                asyncio.run_coroutine_threadsafe(
                    send_message_with_retry(
                        telegram_app.bot, 
                        get_first_primary_admin_id(), 
                        f"�Ÿ”„ BULLETPROOF RECOVERY: Recovered {recovered_count}/{len(failed_payments)} failed payments"
                    ),
                    asyncio.get_event_loop()
                )
            except Exception as e:
                logger.error(f"Error notifying admin about recovery: {e}")
                
    except Exception as e:
        logger.error(f"❌ BULLETPROOF: Error in payment recovery job: {e}")


def add_payment_recovery_scheduler(scheduler):
    """Add payment recovery job to the scheduler"""
    try:
        # Run recovery job every 5 minutes
        scheduler.add_job(
            run_payment_recovery_job,
            'interval',
            minutes=5,
            id='payment_recovery_job',
            replace_existing=True
        )
        logger.info("✅ BULLETPROOF: Payment recovery scheduler added (every 5 minutes)")
    except Exception as e:
        logger.error(f"❌ BULLETPROOF: Error adding payment recovery scheduler: {e}")


# ============================================================================
# BULLETPROOF MONITORING AND ALERTING
# ============================================================================

def check_payment_system_health():
    """Check the overall health of the payment system"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Check for stuck payments
        c.execute("""
            SELECT COUNT(*) FROM pending_deposits 
            WHERE created_at < datetime('now', '-30 minutes')
            AND is_purchase = 1
        """)
        stuck_payments = c.fetchone()[0]
        
        # Check for recent failures
        c.execute("""
            SELECT COUNT(*) FROM pending_deposits 
            WHERE created_at > datetime('now', '-1 hour')
            AND is_purchase = 1
        """)
        recent_payments = c.fetchone()[0]
        
        conn.close()
        
        health_status = {
            'stuck_payments': stuck_payments,
            'recent_payments': recent_payments,
            'is_healthy': stuck_payments < 5 and recent_payments > 0
        }
        
        logger.info(f"🔧 BULLETPROOF HEALTH CHECK: Stuck payments: {stuck_payments}, Recent payments: {recent_payments}")
        return health_status
        
    except Exception as e:
        logger.error(f"❌ BULLETPROOF: Error checking payment system health: {e}")
        return {'is_healthy': False, 'error': str(e)}


def send_health_alert(health_status):
    """Send health alert to admin if system is unhealthy"""
    try:
        from main import telegram_app, get_first_primary_admin_id, send_message_with_retry
        
        if not health_status.get('is_healthy', True) and get_first_primary_admin_id():
            message = f"🚨 BULLETPROOF ALERT: Payment system health issue detected!\n"
            message += f"Stuck payments: {health_status.get('stuck_payments', 0)}\n"
            message += f"Recent payments: {health_status.get('recent_payments', 0)}\n"
            message += f"Error: {health_status.get('error', 'Unknown')}"
            
            asyncio.run_coroutine_threadsafe(
                send_message_with_retry(
                    telegram_app.bot, 
                    get_first_primary_admin_id(), 
                    message
                ),
                asyncio.get_event_loop()
            )
    except Exception as e:
        logger.error(f"❌ BULLETPROOF: Error sending health alert: {e}")
