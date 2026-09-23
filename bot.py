import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# SETTINGS
# =========================

TZ = ZoneInfo("Asia/Karachi")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
REPORT_CHAT_ID = os.environ.get("REPORT_CHAT_ID")

DB_URL = os.environ.get("DATABASE_URL")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# =========================
# DATABASE
# =========================

def db():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(DB_URL)


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                approved BOOLEAN DEFAULT FALSE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                shift_date DATE NOT NULL,
                start_time TIMESTAMPTZ,
                late_seconds INTEGER DEFAULT 0,
                off_time TIMESTAMPTZ,
                total_shift_seconds INTEGER DEFAULT 0,
                net_work_seconds INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                UNIQUE(user_id, shift_date)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS breaks (
                id SERIAL PRIMARY KEY,
                shift_id INTEGER NOT NULL,
                break_type TEXT NOT NULL,
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ,
                duration_seconds INTEGER DEFAULT 0,
                message_id BIGINT
            )
        """)

        conn.commit()


# =========================
# TIME HELPERS
# =========================

def now():
    return datetime.now(TZ)


def current_shift_date():
    """
    A shift belongs to the date on which it starts.
    Shift: 9:30 PM -> next day 9:30 AM
    """
    t = now()

    if t.hour > 21 or (t.hour == 21 and t.minute >= 30):
        return t.date()

    return (t - timedelta(days=1)).date()


def shift_start_datetime(shift_date):
    return datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        21,
        30,
        tzinfo=TZ,
    )


# =========================
# KEYBOARD
# =========================

def employee_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["🟢 Start Work", "🔴 Off Work"],
            ["🚬 Smoke", "🚻 WC"],
            ["🍽️ Break Time", "💺 Back in Seat"],
        ],
        resize_keyboard=True,
    )


# =========================
# USER / ACCESS
# =========================

def register_user(user):
    with db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name
        """, (
            user.id,
            user.username,
            user.first_name,
        ))
        conn.commit()


def is_approved(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT approved FROM users WHERE user_id = %s",
            (user_id,),
        ).fetchone()

    return bool(row and row[0])


def get_shift(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT id, start_time, late_seconds, off_time,
                   total_shift_seconds, net_work_seconds, status
            FROM shifts
            WHERE user_id = %s AND shift_date = %s
        """, (user_id, current_shift_date())).fetchone()


# =========================
# BREAK HELPERS
# =========================

BREAK_LIMITS = {
    "Smoke": 10 * 60,
    "WC": 10 * 60,
    "Meal": 60 * 60,
}


def active_break(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT b.id, b.break_type, b.start_time, b.message_id
            FROM breaks b
            JOIN shifts s ON s.id = b.shift_id
            WHERE s.user_id = %s
              AND s.shift_date = %s
              AND b.end_time IS NULL
            ORDER BY b.id DESC
            LIMIT 1
        """, (user_id, current_shift_date())).fetchone()


# =========================
# /START
# =========================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user)

    if not is_approved(user.id):
        await update.message.reply_text(
            "🔒 Access Restricted\n\n"
            "Your account has not been approved yet.\n"
            "Please contact the administrator to request access."
        )
        return

    await update.message.reply_text(
        "👋 Welcome to WorkShiftPunch!\n\n"
        "Use the buttons below to record your attendance.",
        reply_markup=employee_keyboard(),
    )


# =========================
# ADMIN COMMANDS
# =========================

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /approve @username"
        )
        return

    username = context.args[0].lstrip("@")

    with db() as conn:
        row = conn.execute("""
            SELECT user_id, first_name
            FROM users
            WHERE LOWER(username) = LOWER(%s)
        """, (username,)).fetchone()

        if not row:
            await update.message.reply_text(
                "User not found.\n\n"
                "Ask the employee to open the bot and press Start first."
            )
            return

        conn.execute(
            "UPDATE users SET approved = TRUE WHERE user_id = %s",
            (row[0],),
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ @{username} has been approved."
    )


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: /remove @username"
        )
        return

    username = context.args[0].lstrip("@")

    with db() as conn:
        row = conn.execute("""
            SELECT user_id
            FROM users
            WHERE LOWER(username) = LOWER(%s)
        """, (username,)).fetchone()

        if not row:
            await update.message.reply_text("User not found.")
            return

        conn.execute(
            "UPDATE users SET approved = FALSE WHERE user_id = %s",
            (row[0],),
        )
        conn.commit()

    await update.message.reply_text(
        f"🚫 @{username} has been removed from approved users."
    )


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    with db() as conn:
        rows = conn.execute("""
            SELECT username, first_name, user_id
            FROM users
            WHERE approved = FALSE
            ORDER BY first_name
        """).fetchall()

    if not rows:
        await update.message.reply_text(
            "✅ There are no pending users."
        )
        return

    text = "⏳ Pending Users\n\n"

    for username, first_name, user_id in rows:
        name = first_name or "Unknown"
        username_text = f"@{username}" if username else "No username"

        text += (
            f"👤 {name}\n"
            f"Username: {username_text}\n"
            f"ID: {user_id}\n\n"
        )

    await update.message.reply_text(text)


# =========================
# START WORK
# =========================

async def start_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_approved(user.id):
        await update.message.reply_text(
            "🔒 Your account has not been approved yet."
        )
        return

    existing = get_shift(user.id)

    if existing and existing[1]:
        await update.message.reply_text(
            "🟢 You have already started your shift."
        )
        return

    t = now()
    shift_date = current_shift_date()
    scheduled = shift_start_datetime(shift_date)

    # If Start Work is pressed before the scheduled shift start,
    # late time is zero.
    late_seconds = max(0, int((t - scheduled).total_seconds()))

    with db() as conn:
        conn.execute("""
            INSERT INTO shifts
            (user_id, shift_date, start_time, late_seconds, status)
            VALUES (%s, %s, %s, %s, 'active')
            ON CONFLICT (user_id, shift_date)
            DO UPDATE SET
                start_time = EXCLUDED.start_time,
                late_seconds = EXCLUDED.late_seconds,
                status = 'active'
        """, (
            user.id,
            shift_date,
            t,
            late_seconds,
        ))
        conn.commit()

    if late_seconds == 0:
        message = (
            "🟢 Work Started\n\n"
            f"Start time: {t.strftime('%I:%M:%S %p')}\n"
            "Status: On time."
        )
    else:
        late_minutes = late_seconds // 60
        late_secs = late_seconds % 60

        message = (
            "🟢 Work Started\n\n"
            f"Start time: {t.strftime('%I:%M:%S %p')}\n"
            f"Late by: {late_minutes} minutes {late_secs} seconds."
        )

    await update.message.reply_text(
        message,
        reply_markup=employee_keyboard(),
    )


# =========================
# BREAK START
# =========================

async def start_break(update: Update, break_type):
    user = update.effective_user

    if not is_approved(user.id):
        await update.message.reply_text(
            "🔒 Your account has not been approved yet."
        )
        return

    shift = get_shift(user.id)

    if not shift or not shift[1]:
        await update.message.reply_text(
            "⚠️ Please press 🟢 Start Work first."
        )
        return

    if shift[6] == "completed":
        await update.message.reply_text(
            "⚠️ Your shift has already ended."
        )
        return

    current = active_break(user.id)

    if current:
        await update.message.reply_text(
            f"⚠️ You are currently on {current[1]} break.\n"
            "Please press 💺 Back in Seat first."
        )
        return

    t = now()

    with db() as conn:
        cur = conn.execute("""
            INSERT INTO breaks
            (shift_id, break_type, start_time)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (shift[0], break_type, t))

        break_id = cur.fetchone()[0]
        conn.commit()

    limit = BREAK_LIMITS[break_type]

    hours = limit // 3600
    minutes = (limit % 3600) // 60

    if hours:
        allowed = f"{hours} hour"
    else:
        allowed = f"{minutes} minutes"

    message = await update.message.reply_text(
        f"{'🚬' if break_type == 'Smoke' else '🚻' if break_type == 'WC' else '🍽️'} "
        f"{break_type} Break Started\n\n"
        f"Start time: {t.strftime('%I:%M:%S %p')}\n"
        f"Allowed time: {allowed}\n\n"
        "Press 💺 Back in Seat when you return."
    )

    with db() as conn:
        conn.execute(
            "UPDATE breaks SET message_id = %s WHERE id = %s",
            (message.message_id, break_id),
        )
        conn.commit()


# =========================
# BACK IN SEAT
# =========================

async def back_in_seat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_approved(user.id):
        await update.message.reply_text(
            "🔒 Your account has not been approved yet."
        )
        return

    current = active_break(user.id)

    if not current:
        await update.message.reply_text(
            "ℹ️ You are not currently on a break."
        )
        return

    break_id, break_type, start_time, original_message_id = current
    end_time = now()

    duration = max(
        0,
        int((end_time - start_time).total_seconds())
    )

    limit = BREAK_LIMITS[break_type]

    with db() as conn:
        conn.execute("""
            UPDATE breaks
            SET end_time = %s,
                duration_seconds = %s
            WHERE id = %s
        """, (end_time, duration, break_id))
        conn.commit()

    minutes = duration // 60
    seconds = duration % 60

    if duration > limit:
        overtime = duration - limit
        overtime_minutes = overtime // 60
        overtime_seconds = overtime % 60

        result = (
            f"💺 Back in Seat\n\n"
            f"You returned from your {break_type} break.\n"
            f"Break duration: {minutes} minutes {seconds} seconds.\n"
            f"Allowed time: {limit // 60} minutes"
            f"{' / 1 hour' if break_type == 'Meal' else ''}.\n"
            f"⚠️ Overtime: {overtime_minutes} minutes "
            f"{overtime_seconds} seconds."
        )
    else:
        result = (
            f"💺 Back in Seat\n\n"
            f"You returned from your {break_type} break.\n"
            f"Break duration: {minutes} minutes {seconds} seconds.\n"
            f"Allowed time: "
            f"{'1 hour' if break_type == 'Meal' else '10 minutes'}.\n"
            "Status: Within allowed time."
        )

    # Reply to the original break message when possible.
    try:
        await update.message.reply_text(
            result,
            reply_to_message_id=original_message_id
            if original_message_id
            else None,
        )
    except Exception:
        await update.message.reply_text(result)


# =========================
# OFF WORK
# =========================

async def off_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_approved(user.id):
        await update.message.reply_text(
            "🔒 Your account has not been approved yet."
        )
        return

    shift = get_shift(user.id)

    if not shift or not shift[1]:
        await update.message.reply_text(
            "⚠️ Please press 🟢 Start Work first."
        )
        return

    if shift[6] == "completed":
        await update.message.reply_text(
            "🔴 Your shift has already been ended."
        )
        return

    current = active_break(user.id)

    if current:
        await update.message.reply_text(
            "⚠️ You are currently on a break.\n"
            "Please press 💺 Back in Seat before pressing 🔴 Off Work."
        )
        return

    t = now()

    start_time = shift[1]

    total_shift = max(
        0,
        int((t - start_time).total_seconds())
    )

    with db() as conn:
        break_total = conn.execute("""
            SELECT COALESCE(SUM(duration_seconds), 0)
            FROM breaks
            WHERE shift_id = %s
        """, (shift[0],)).fetchone()[0]

        net_work = max(0, total_shift - break_total)

        conn.execute("""
            UPDATE shifts
            SET off_time = %s,
                total_shift_seconds = %s,
                net_work_seconds = %s,
                status = 'completed'
            WHERE id = %s
        """, (
            t,
            total_shift,
            net_work,
            shift[0],
        ))

        conn.commit()

    def fmt(seconds):
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours}h {minutes}m {secs}s"

    await update.message.reply_text(
        "🔴 Work Ended\n\n"
        f"Off time: {t.strftime('%I:%M:%S %p')}\n"
        f"Total shift: {fmt(total_shift)}\n"
        f"Net work time: {fmt(net_work)}\n\n"
        "Have a good rest! 👋",
        reply_markup=employee_keyboard(),
    )


# =========================
# REPORT
# =========================

def make_report(shift_date):
    with db() as conn:
        rows = conn.execute("""
            SELECT
                u.user_id,
                u.username,
                u.first_name,
                s.start_time,
                s.late_seconds,
                s.off_time,
                s.total_shift_seconds,
                s.net_work_seconds,
                s.status,
                COALESCE(
                    SUM(CASE WHEN b.break_type = 'Smoke'
                        THEN 1 ELSE 0 END), 0
                ) AS smoke_count,
                COALESCE(
                    SUM(CASE WHEN b.break_type = 'Smoke'
                        THEN b.duration_seconds ELSE 0 END), 0
                ) AS smoke_total,
                COALESCE(
                    SUM(CASE WHEN b.break_type = 'WC'
                        THEN 1 ELSE 0 END), 0
                ) AS wc_count,
                COALESCE(
                    SUM(CASE WHEN b.break_type = 'WC'
                        THEN b.duration_seconds ELSE 0 END), 0
                ) AS wc_total,
                COALESCE(
                    SUM(CASE WHEN b.break_type = 'Meal'
                        THEN 1 ELSE 0 END), 0
                ) AS meal_count,
                COALESCE(
                    SUM(CASE WHEN b.break_type = 'Meal'
                        THEN b.duration_seconds ELSE 0 END), 0
                ) AS meal_total
            FROM users u
            LEFT JOIN shifts s
                ON s.user_id = u.user_id
                AND s.shift_date = %s
            LEFT JOIN breaks b
                ON b.shift_id = s.id
            WHERE u.approved = TRUE
            GROUP BY
                u.user_id, u.username, u.first_name,
                s.start_time, s.late_seconds,
                s.off_time, s.total_shift_seconds,
                s.net_work_seconds, s.status
            ORDER BY u.first_name
        """, (shift_date,)).fetchall()

    if not rows:
        return "📊 Previous Shift Report\n\nNo approved employees found."

    def fmt(seconds):
        if seconds is None:
            return "—"

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        return f"{hours}h {minutes}m {secs}s"

    text = (
        "📊 PREVIOUS SHIFT REPORT\n"
        f"Shift date: {shift_date.strftime('%d %b %Y')}\n"
        "Shift: 9:30 PM – 9:30 AM\n"
        "Timezone: Pakistan\n\n"
    )

    for i, row in enumerate(rows, start=1):
        (
            user_id,
            username,
            first_name,
            start_time,
            late_seconds,
            off_time,
            total_shift_seconds,
            net_work_seconds,
            status,
            smoke_count,
            smoke_total,
            wc_count,
            wc_total,
            meal_count,
            meal_total,
        ) = row

        name = first_name or username or str(user_id)

        text += f"━━━━━━━━━━━━━━\n{i}. {name}\n"

        if username:
            text += f"Username: @{username}\n"

        if start_time:
            text += (
                f"Start: {start_time.astimezone(TZ).strftime('%I:%M:%S %p')}\n"
            )

            if late_seconds and late_seconds > 0:
                text += f"Late by: {fmt(late_seconds)}\n"
            else:
                text += "Late by: On time\n"
        else:
            text += "Start: Not started\n"

        text += (
            f"🚬 Smoke: {smoke_count} / {fmt(smoke_total)}\n"
            f"🚻 WC: {wc_count} / {fmt(wc_total)}\n"
            f"🍽️ Meal: {meal_count} / {fmt(meal_total)}\n"
        )

        if off_time:
            text += (
                f"Off: {off_time.astimezone(TZ).strftime('%I:%M:%S %p')}\n"
                f"Total shift: {fmt(total_shift_seconds)}\n"
                f"Net work: {fmt(net_work_seconds)}\n"
                "Status: Completed\n"
            )
        else:
            text += "Off: Not recorded\nStatus: Not completed\n"

        text += "\n"

    return text


async def send_previous_shift_report(context: ContextTypes.DEFAULT_TYPE):
    if not REPORT_CHAT_ID:
        logging.warning("REPORT_CHAT_ID is not configured.")
        return

    yesterday = (now() - timedelta(days=1)).date()

    report = make_report(yesterday)

    try:
        await context.bot.send_message(
            chat_id=REPORT_CHAT_ID,
            text=report,
        )
    except Exception:
        logging.exception("Could not send shift report.")


# =========================
# TEXT ROUTER
# =========================

async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🟢 Start Work":
        await start_work(update, context)

    elif text == "🔴 Off Work":
        await off_work(update, context)

    elif text == "🚬 Smoke":
        await start_break(update, "Smoke")

    elif text == "🚻 WC":
        await start_break(update, "WC")

    elif text == "🍽️ Break Time":
        await start_break(update, "Meal")

    elif text == "💺 Back in Seat":
        await back_in_seat(update, context)


# =========================
# MAIN
# =========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")

    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("pending", pending_command))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_router,
        )
    )

    # 9:20 PM Pakistan time every day
    app.job_queue.run_daily(
        send_previous_shift_report,
        time=datetime.strptime("21:20", "%H:%M").time().replace(
            tzinfo=TZ
        ),
    )

    print("WorkShiftPunch is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
