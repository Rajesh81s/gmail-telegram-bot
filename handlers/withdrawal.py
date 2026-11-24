"""
EarnX Gmail Bot — Withdrawal & Payment Handlers
Withdrawal flow, UPI/USDT setup, amount entry.
"""

import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from config import (
    ADMIN_ID, WITHDRAWAL_FEE_PERCENT, WITHDRAWAL_FEE_MIN,
    WITHDRAWAL_FEE_MIN_USDT, WITHDRAWAL_FEE_MIN_INR,
    DEFAULT_MIN_WITHDRAW_USDT, DEFAULT_MIN_WITHDRAW_UPI_USDT,
    MAX_WITHDRAWALS_PER_DAY, MAX_PENDING_WITHDRAWALS,
    WITHDRAW_AMT, WITHDRAW_CONFIRM, UPI_ID, USDT_ADDRESS,
)
from database import get_db
from utils import (
    validate_upi, validate_usdt_address, can_withdraw_today,
    calculate_withdrawal_fee_usdt, round_decimal, round_usdt,
    get_usdt_to_inr_rate, inr_to_usdt, usdt_to_inr,
    format_dual_currency, safe_edit_or_reply,
    is_blocked, notify_user, get_max_withdrawal_amount,
)

logger = logging.getLogger(__name__)


# ==================== WITHDRAW MENU ====================

async def handle_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Withdraw main menu."""
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT balance, balance_usdt, usdt_address, upi_id FROM users WHERE user_id=%s", (uid,))
        result = c.fetchone()

        c.execute("SELECT COUNT(*) FROM withdrawals WHERE user_id=%s AND status='pending'", (uid,))
        pending_count = list(c.fetchone().values())[0]

    can_withdraw, remaining = can_withdraw_today(uid)
    rate = get_usdt_to_inr_rate()

    if result:
        bal_usdt = Decimal(str(result.get('balance_usdt') or 0))
        if bal_usdt == 0 and result.get('balance'):
            bal_usdt = inr_to_usdt(Decimal(str(result['balance'])), rate=rate)

        usdt = result['usdt_address']
        upi = result['upi_id']

        if not can_withdraw:
            text = (
                f"💸 <b>Withdraw Funds</b>\n\n"
                f"💰 Balance: <b>{format_dual_currency(bal_usdt, rate=rate)}</b>\n\n"
                f"⚠️ Daily withdrawal limit reached.\n"
                f"Max {MAX_WITHDRAWALS_PER_DAY}/day. Try again tomorrow."
            )
            kb = [[InlineKeyboardButton("🔙 Back", callback_data="menu")]]
        elif pending_count >= MAX_PENDING_WITHDRAWALS:
            text = (
                f"💸 <b>Withdraw Funds</b>\n\n"
                f"💰 Balance: <b>{format_dual_currency(bal_usdt, rate=rate)}</b>\n\n"
                f"⚠️ {pending_count} pending requests. Please wait for processing."
            )
            kb = [[InlineKeyboardButton("🔙 Back", callback_data="menu")]]
        elif bal_usdt < DEFAULT_MIN_WITHDRAW_UPI_USDT:
            text = (
                f"💸 <b>Withdraw Funds</b>\n\n"
                f"💰 Balance: <b>{format_dual_currency(bal_usdt, rate=rate)}</b>\n\n"
                f"⚠️ Minimum withdrawal: <b>${float(DEFAULT_MIN_WITHDRAW_UPI_USDT):.2f} USDT</b> (via UPI) or <b>${float(DEFAULT_MIN_WITHDRAW_USDT):.2f} USDT</b> (via Crypto)\n"
                f"Keep submitting tasks to reach the threshold!"
            )
            kb = [[InlineKeyboardButton("🔙 Back", callback_data="menu")]]
        else:
            text = (
                f"💸 <b>Withdraw Funds</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Balance: <b>{format_dual_currency(bal_usdt)}</b>\n"
                f"📊 Today: <b>{remaining}/{MAX_WITHDRAWALS_PER_DAY}</b> remaining\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"💡 <b>Available Methods:</b>\n"
                f"📱 <b>UPI:</b> Min ${float(DEFAULT_MIN_WITHDRAW_UPI_USDT):.2f} USDT (Fee: 5%, min ${float(WITHDRAWAL_FEE_MIN_USDT):.2f} USDT)\n"
                f"💎 <b>USDT (BEP20):</b> Min ${float(DEFAULT_MIN_WITHDRAW_USDT):.2f} USDT (Fee: 5%, min ${float(WITHDRAWAL_FEE_MIN_USDT):.2f} USDT)\n\n"
                f"Choose withdrawal method:"
            )
            kb = [
                [InlineKeyboardButton(f"📱 UPI{'  ✅' if upi else ''}", callback_data="withdraw_upi")],
                [InlineKeyboardButton(f"💎 USDT (BEP20){'  ✅' if usdt else ''}", callback_data="withdraw_usdt")],
                [InlineKeyboardButton("💳 Setup Payment", callback_data="setup_payment")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu")],
            ]

        await safe_edit_or_reply(q, text, InlineKeyboardMarkup(kb))
    else:
        await safe_edit_or_reply(
            q, "⚠️ Error occurred",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu")]])
        )


# ==================== WITHDRAWAL METHOD SELECTION ====================

async def handle_withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle UPI / USDT withdrawal method selection."""
    q = update.callback_query
    await q.answer()
    d = q.data
    rate = get_usdt_to_inr_rate()

    if d == "withdraw_upi":
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT upi_id FROM users WHERE user_id=%s", (q.from_user.id,))
            result = c.fetchone()
        if not result or not result['upi_id']:
            await q.answer("📱 Please setup UPI first", show_alert=True)
            return
        context.user_data['withdraw_method'] = 'upi'
        await safe_edit_or_reply(
            q,
            f"💸 <b>Withdraw via UPI</b>\n\n"
            f"Enter amount in <b>USDT</b> (Minimum ${float(DEFAULT_MIN_WITHDRAW_UPI_USDT):.2f} USDT):\n\n"
            f"/cancel to abort",
        )
        return WITHDRAW_AMT

    elif d == "withdraw_usdt":
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT usdt_address FROM users WHERE user_id=%s", (q.from_user.id,))
            result = c.fetchone()
        if not result or not result['usdt_address']:
            await q.answer("💎 Please setup USDT address first", show_alert=True)
            return
        context.user_data['withdraw_method'] = 'usdt'
        await safe_edit_or_reply(
            q,
            f"💸 <b>Withdraw via USDT (BEP20)</b>\n\n"
            f"Enter amount in <b>USDT</b> (Minimum ${float(DEFAULT_MIN_WITHDRAW_USDT):.2f} USDT):\n\n"
            f"/cancel to abort",
        )
        return WITHDRAW_AMT


# ==================== PAYMENT SETUP ====================

async def handle_setup_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Payment method setup menu."""
    q = update.callback_query
    await q.answer()

    kb = [
        [InlineKeyboardButton("📱 Setup UPI", callback_data="set_upi")],
        [InlineKeyboardButton("💎 Setup USDT (BEP20)", callback_data="set_usdt")],
        [InlineKeyboardButton("🔙 Back", callback_data="withdraw")],
    ]
    await safe_edit_or_reply(
        q,
        "💳 <b>Setup Payment Method</b>\n\nChoose your preferred method:",
        InlineKeyboardMarkup(kb)
    )


# ==================== TEXT-BASED ENTRY POINTS (for keyboard buttons) ====================

async def handle_set_upi_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text-based entry for '📱 Setup UPI' keyboard button."""
    try:
        await update.message.delete()
    except Exception:
        pass
    # Delete previous bot message
    prev_msg_id = context.user_data.get('last_bot_msg')
    if prev_msg_id:
        try:
            await context.bot.delete_message(update.effective_chat.id, prev_msg_id)
        except Exception:
            pass
    context.user_data.pop('last_bot_msg', None)

    from handlers.user import get_main_reply_keyboard
    await context.bot.send_message(
        update.effective_chat.id,
        "📱 <b>Setup UPI</b>\n\nSend your UPI ID (e.g., <code>name@paytm</code>)\n\n/cancel to abort",
        reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
    )
    return UPI_ID


async def handle_set_usdt_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text-based entry for '💎 Setup USDT' keyboard button."""
    try:
        await update.message.delete()
    except Exception:
        pass
    prev_msg_id = context.user_data.get('last_bot_msg')
    if prev_msg_id:
        try:
            await context.bot.delete_message(update.effective_chat.id, prev_msg_id)
        except Exception:
            pass
    context.user_data.pop('last_bot_msg', None)

    from handlers.user import get_main_reply_keyboard
    await context.bot.send_message(
        update.effective_chat.id,
        "💎 <b>Setup USDT</b>\n\nSend your BEP20 (BSC) address\n"
        "<i>Must be 42 characters, starting with 0x</i>\n\n/cancel to abort",
        reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
    )
    return USDT_ADDRESS


async def handle_withdraw_upi_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text-based entry for '📱 Withdraw UPI' keyboard button."""
    try:
        await update.message.delete()
    except Exception:
        pass
    prev_msg_id = context.user_data.get('last_bot_msg')
    if prev_msg_id:
        try:
            await context.bot.delete_message(update.effective_chat.id, prev_msg_id)
        except Exception:
            pass
    context.user_data.pop('last_bot_msg', None)
    uid = update.effective_user.id

    from handlers.user import get_main_reply_keyboard
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT upi_id FROM users WHERE user_id=%s", (uid,))
        result = c.fetchone()
    if not result or not result['upi_id']:
        await context.bot.send_message(
            update.effective_chat.id,
            "⚠️ Please setup UPI first via Profile → Payment Methods",
            reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
        )
        return ConversationHandler.END
    context.user_data['withdraw_method'] = 'upi'
    await context.bot.send_message(
        update.effective_chat.id,
        f"💸 <b>Withdraw via UPI</b>\n\n"
        f"Enter amount in <b>USDT</b> (Minimum ${float(DEFAULT_MIN_WITHDRAW_UPI_USDT):.2f} USDT):\n\n"
        f"/cancel to abort",
        reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
    )
    return WITHDRAW_AMT


async def handle_withdraw_usdt_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text-based entry for '💎 Withdraw USDT' keyboard button."""
    try:
        await update.message.delete()
    except Exception:
        pass
    prev_msg_id = context.user_data.get('last_bot_msg')
    if prev_msg_id:
        try:
            await context.bot.delete_message(update.effective_chat.id, prev_msg_id)
        except Exception:
            pass
    context.user_data.pop('last_bot_msg', None)
    uid = update.effective_user.id

    from handlers.user import get_main_reply_keyboard
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT usdt_address FROM users WHERE user_id=%s", (uid,))
        result = c.fetchone()
    if not result or not result['usdt_address']:
        await context.bot.send_message(
            update.effective_chat.id,
            "⚠️ Please setup USDT first via Profile → Payment Methods",
            reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
        )
        return ConversationHandler.END
    context.user_data['withdraw_method'] = 'usdt'
    await context.bot.send_message(
        update.effective_chat.id,
        f"💸 <b>Withdraw via USDT (BEP20)</b>\n\n"
        f"Enter amount in <b>USDT</b> (Minimum ${float(DEFAULT_MIN_WITHDRAW_USDT):.2f} USDT):\n\n"
        f"/cancel to abort",
        reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
    )
    return WITHDRAW_AMT


async def handle_set_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start UPI setup."""
    q = update.callback_query
    await q.answer()
    await safe_edit_or_reply(
        q,
        "📱 <b>Setup UPI</b>\n\nSend your UPI ID (e.g., <code>name@paytm</code>)\n\n/cancel to abort",
    )
    return UPI_ID


async def handle_set_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start USDT setup."""
    q = update.callback_query
    await q.answer()
    await safe_edit_or_reply(
        q,
        "💎 <b>Setup USDT</b>\n\nSend your BEP20 (BSC) address\n"
        "<i>Must be 42 characters, starting with 0x</i>\n\n/cancel to abort",
    )
    return USDT_ADDRESS


# ==================== MESSAGE HANDLERS ====================

async def receive_upi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and save UPI ID."""
    upi_id = update.message.text.strip()

    if not validate_upi(upi_id):
        await update.message.reply_text(
            "❌ <b>Invalid UPI ID</b>\n\nFormat: <code>name@bank</code>\n\n/cancel to abort",
            parse_mode="HTML"
        )
        return UPI_ID

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET upi_id=%s WHERE user_id=%s", (upi_id, update.effective_user.id))

        from handlers.user import get_main_reply_keyboard
        await update.message.reply_text(
            f"✅ <b>UPI ID Saved</b>\n\nUPI: <code>{upi_id}</code>",
            reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in receive_upi: {e}")
        await update.message.reply_text("⚠️ Error occurred. Please try again.", parse_mode="HTML")
        return ConversationHandler.END


async def receive_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive and save USDT address."""
    addr = update.message.text.strip()

    if not validate_usdt_address(addr):
        await update.message.reply_text(
            "❌ <b>Invalid USDT Address</b>\n\n"
            "Must be 42 characters, starting with <code>0x</code>\n\n/cancel to abort",
            parse_mode="HTML"
        )
        return USDT_ADDRESS

    try:
        with get_db() as conn:
            c = conn.cursor()
            c.execute("UPDATE users SET usdt_address=%s WHERE user_id=%s", (addr, update.effective_user.id))

        from handlers.user import get_main_reply_keyboard
        await update.message.reply_text(
            f"✅ <b>USDT Address Saved</b>\n\nAddress: <code>{addr[:10]}...{addr[-10:]}</code>",
            reply_markup=get_main_reply_keyboard(), parse_mode="HTML"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in receive_usdt: {e}")
        await update.message.reply_text("⚠️ Error occurred. Please try again.", parse_mode="HTML")
        return ConversationHandler.END


async def receive_withdraw_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive withdrawal amount in USDT and show confirmation before processing."""
    try:
        amount = Decimal(update.message.text.strip())
        method = context.user_data.get('withdraw_method', 'upi')
        rate = get_usdt_to_inr_rate()

        min_req = DEFAULT_MIN_WITHDRAW_UPI_USDT if method == 'upi' else DEFAULT_MIN_WITHDRAW_USDT
        if amount < min_req:
            await update.message.reply_text(
                f"⚠️ <b>Minimum withdrawal for {method.upper()}: ${float(min_req):.2f} USDT</b>\n\n"
                f"Please enter a valid amount or /cancel",
                parse_mode="HTML"
            )
            return WITHDRAW_AMT

        # Check max withdrawal limit (admin-controlled in USDT)
        max_withdrawal = get_max_withdrawal_amount()
        if amount > max_withdrawal:
            await update.message.reply_text(
                f"⚠️ <b>Maximum withdrawal: ${float(max_withdrawal):.2f} USDT</b> per request\n\n"
                f"Enter a smaller amount or /cancel",
                parse_mode="HTML"
            )
            return WITHDRAW_AMT

        can_withdraw, remaining = can_withdraw_today(update.effective_user.id)
        if not can_withdraw:
            await update.message.reply_text(
                f"⚠️ Daily limit reached ({MAX_WITHDRAWALS_PER_DAY}/day). Try tomorrow.",
                parse_mode="HTML"
            )
            return ConversationHandler.END

        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT balance, balance_usdt, usdt_address, upi_id FROM users WHERE user_id=%s",
                      (update.effective_user.id,))
            result = c.fetchone()

        if not result:
            await update.message.reply_text("⚠️ Error occurred", parse_mode="HTML")
            return ConversationHandler.END

        bal_usdt = Decimal(str(result.get('balance_usdt') or 0))
        if bal_usdt == 0 and result.get('balance'):
            bal_usdt = inr_to_usdt(Decimal(str(result['balance'])), rate=rate)

        if amount > bal_usdt:
            await update.message.reply_text(
                f"⚠️ <b>Insufficient Balance</b>\n\n"
                f"Available: <b>{format_dual_currency(bal_usdt, rate=rate)}</b>\n"
                f"Requested: <b>${float(amount):.2f} USDT</b>",
                parse_mode="HTML"
            )
            return WITHDRAW_AMT

        payment_info = result['upi_id'] if method == 'upi' else result['usdt_address']
        method_name = "UPI" if method == 'upi' else "USDT (BEP20)"

        fee_usdt, final_usdt, fee_inr, final_inr = calculate_withdrawal_fee_usdt(amount, method=method, rate=rate)

        # Store details for confirmation step
        context.user_data['withdraw_amount_usdt'] = str(amount)
        context.user_data['withdraw_rate'] = str(rate)
        context.user_data['withdraw_payment_info'] = payment_info
        context.user_data['withdraw_method_name'] = method_name
        context.user_data['withdraw_fee'] = str(fee_usdt)
        context.user_data['withdraw_final'] = str(final_usdt)
        context.user_data['withdraw_payout_amount'] = str(final_usdt)

        kb = [
            [InlineKeyboardButton("✅ Confirm Withdrawal", callback_data="wdraw_yes")],
            [InlineKeyboardButton("❌ Cancel", callback_data="wdraw_no")],
        ]

        if method == 'upi':
            text = (
                f"⚠️ <b>Confirm UPI Withdrawal</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Requested: <b>${float(amount):.2f} USDT</b>\n"
                f"📱 UPI ID: <code>{payment_info}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 Platform Fee (5%): ${float(fee_usdt):.2f} USDT\n"
                f"🎉 <b>You will receive: ${float(final_usdt):.2f} USDT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚠️ <i>This cannot be undone. Please verify your UPI ID.</i>"
            )
        else:
            text = (
                f"⚠️ <b>Confirm USDT Crypto Withdrawal</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Requested: <b>${float(amount):.2f} USDT</b>\n"
                f"💎 USDT (BEP20): <code>{payment_info}</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 Platform Fee (5%): ${float(fee_usdt):.2f} USDT\n"
                f"🎉 <b>You will receive: ${float(final_usdt):.2f} USDT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚠️ <i>This cannot be undone. Please verify your BEP20 address.</i>"
            )

        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return WITHDRAW_CONFIRM

    except (ValueError, InvalidOperation):
        await update.message.reply_text(
            "❌ <b>Invalid amount</b>\n\nEnter a valid number (e.g. 5.00) or /cancel",
            parse_mode="HTML"
        )
        return WITHDRAW_AMT
    except Exception as e:
        logger.error(f"Error in receive_withdraw_amt: {e}")
        await update.message.reply_text("⚠️ Error. Please try again later.", parse_mode="HTML")
        return ConversationHandler.END


async def confirm_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process confirmed withdrawal with row-level locking (SELECT FOR UPDATE)."""
    q = update.callback_query
    await q.answer()

    uid = q.from_user.id
    amount_usdt = Decimal(context.user_data.get('withdraw_amount_usdt', '0'))
    rate = Decimal(context.user_data.get('withdraw_rate', '90.00'))
    fee = Decimal(context.user_data.get('withdraw_fee', '0'))
    final_amount = Decimal(context.user_data.get('withdraw_final', '0'))
    payout_amount = Decimal(context.user_data.get('withdraw_payout_amount', '0'))
    method = context.user_data.get('withdraw_method')
    payment_info = context.user_data.get('withdraw_payment_info')
    method_name = context.user_data.get('withdraw_method_name')

    if not amount_usdt or not method or not payment_info:
        await q.edit_message_text("⚠️ Session expired. Please start over.", parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    try:
        with get_db() as conn:
            c = conn.cursor()

            c.execute("SELECT balance, balance_usdt FROM users WHERE user_id=%s FOR UPDATE", (uid,))
            result = c.fetchone()

            if not result:
                await q.edit_message_text("⚠️ Error occurred.", parse_mode="HTML")
                context.user_data.clear()
                return ConversationHandler.END

            bal_usdt = Decimal(str(result.get('balance_usdt') or 0))
            if bal_usdt == 0 and result.get('balance'):
                bal_usdt = inr_to_usdt(Decimal(str(result['balance'])), rate=rate)

            if amount_usdt > bal_usdt:
                await q.edit_message_text(
                    f"⚠️ <b>Insufficient Balance</b>\n\n"
                    f"Balance: {format_dual_currency(bal_usdt, rate=rate)}\nRequested: ${float(amount_usdt):.2f} USDT\n\n"
                    f"Your balance may have changed. Please try again.",
                    parse_mode="HTML"
                )
                context.user_data.clear()
                return ConversationHandler.END

            # Deduct from balance_usdt and keep legacy balance in sync
            inr_deduction = round_decimal(amount_usdt * rate)
            c.execute("""
                UPDATE users
                SET balance_usdt = balance_usdt - %s,
                    balance = GREATEST(0, balance - %s)
                WHERE user_id=%s AND (balance_usdt >= %s OR balance >= %s)
                RETURNING balance_usdt
            """, (amount_usdt, inr_deduction, uid, amount_usdt, inr_deduction))

            updated = c.fetchone()
            if not updated:
                await q.edit_message_text(
                    "⚠️ Balance changed. Please try again.", parse_mode="HTML"
                )
                context.user_data.clear()
                return ConversationHandler.END

            # Create withdrawal record with complete USDT and INR tracking
            c.execute("""
                INSERT INTO withdrawals (
                    user_id, amount, fee, final_amount, method, payment_info,
                    status, request_date, amount_usdt, inr_exchange_rate, inr_payout_amount
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                uid, payout_amount, fee, final_amount, method, payment_info,
                "pending", datetime.now().isoformat(), amount_usdt, rate,
                payout_amount if method == 'upi' else None
            ))
            wid = c.fetchone()['id']

    except Exception as e:
        logger.error(f"Error in confirm_withdrawal: {e}")
        await q.edit_message_text("⚠️ Error. Please try again later.", parse_mode="HTML")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data.clear()

    # Success message
    payout_str = f"${float(final_amount):.2f} USDT"
    await q.edit_message_text(
        f"✅ <b>Withdrawal Requested!</b>\n\n"
        f"Request ID: <b>#{wid}</b>\n"
        f"Method: <b>{method_name}</b>\n"
        f"Amount Deducted: <b>${float(amount_usdt):.2f} USDT</b>\n"
        f"Payout Amount: <b>{payout_str}</b>\n"
        f"Destination: <code>{payment_info}</code>\n\n"
        f"⏳ Processing time: <b>1-24 hours</b>\n"
        f"You will receive a notification once processed.",
        parse_mode="HTML"
    )

    # Notify admin
    try:
        admin_text = (
            f"💸 <b>New Withdrawal Request #{wid}</b>\n\n"
            f"User ID: <code>{uid}</code>\n"
            f"Method: <b>{method_name}</b>\n"
            f"Requested: <b>${float(amount_usdt):.2f} USDT</b>\n"
            f"Payout: <b>{payout_str}</b>\n"
            f"Destination: <code>{payment_info}</code>"
        )
        await context.bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to notify admin of withdrawal #{wid}: {e}")

    return ConversationHandler.END


async def cancel_withdrawal_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel withdrawal at confirmation step. No money deducted."""
    q = update.callback_query
    await q.answer("Withdrawal cancelled", show_alert=True)

    context.user_data.clear()

    await q.edit_message_text(
        "❌ <b>Withdrawal Cancelled</b>\n\nNo amount was deducted.",
        parse_mode="HTML"
    )
    return ConversationHandler.END
