import csv
import io
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("LEADS_DB_PATH", "team_leads.db").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("lead-manager")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def uk_time_text():
    return datetime.now(ZoneInfo("Europe/London")).strftime("%d/%m/%Y %H:%M")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS workers (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_at TEXT NOT NULL,
                approved_at TEXT,
                approved_by INTEGER
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by INTEGER,
                current_round INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                value TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'available',

                assigned_to INTEGER,
                assigned_at TEXT,

                attempts INTEGER NOT NULL DEFAULT 0,
                last_round INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,

                notes TEXT,
                card_bin6 TEXT,

                caller_user_id INTEGER,
                caller_username TEXT,
                caller_name TEXT,

                ready_by INTEGER,
                ready_at TEXT,

                ran_by INTEGER,
                ran_at TEXT,

                taken_by INTEGER,
                taken_at TEXT,

                FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
            );

            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                lead_id INTEGER,
                worker_user_id INTEGER,
                created_at TEXT NOT NULL,
                detail TEXT
            );

            CREATE TABLE IF NOT EXISTS call_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                campaign_id INTEGER NOT NULL,
                worker_user_id INTEGER NOT NULL,
                worker_username TEXT,
                worker_name TEXT,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_call_events_campaign_worker
            ON call_events(campaign_id, worker_user_id, outcome);

            CREATE INDEX IF NOT EXISTS idx_leads_queue
            ON leads(campaign_id, status, last_round, position);

            CREATE INDEX IF NOT EXISTS idx_leads_assigned
            ON leads(campaign_id, assigned_to, status);
            """
        )

        # Safe migration for existing Railway databases.
        lead_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(leads)").fetchall()
        }
        if "card_bin6" not in lead_columns:
            conn.execute("ALTER TABLE leads ADD COLUMN card_bin6 TEXT")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_leads_card_bin6 "
            "ON leads(campaign_id, card_bin6, status, last_round, position)"
        )


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key,value)
            VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(value)),
        )


def get_setting(key):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def get_group_id():
    raw = get_setting("team_group_id")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def active_campaign():
    with db() as conn:
        return conn.execute(
            "SELECT * FROM campaigns WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()


def worker_status(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM workers WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row["status"] if row else None


def upsert_worker_request(user):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM workers WHERE user_id=?",
            (user.id,),
        ).fetchone()

        if not row:
            conn.execute(
                """
                INSERT INTO workers(
                    user_id,username,first_name,status,requested_at
                ) VALUES(?,?,?,?,?)
                """,
                (
                    user.id,
                    user.username or "",
                    user.first_name or "",
                    "pending",
                    now_iso(),
                ),
            )
            return "new"

        conn.execute(
            """
            UPDATE workers
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (user.username or "", user.first_name or "", user.id),
        )

        if row["status"] == "declined":
            conn.execute(
                """
                UPDATE workers
                SET status='pending', requested_at=?
                WHERE user_id=?
                """,
                (now_iso(), user.id),
            )
            return "new"

        return row["status"]


def parse_leads(filename, raw):
    """
    Automatically support both upload formats.

    Personal Information mode:
      Any occurrence matching variations such as:
        Personal Information
        PERSONAL INFORMATION
        Personal   Information
        Personal Information:
        Personal-Information
      starts a NEW lead.

    Fallback mode:
      If no Personal Information marker is found, every non-empty line
      is treated as one lead.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    # Flexible marker detection. This also handles non-standard whitespace.
    marker_re = re.compile(
        r"(?im)^[ \t]*personal(?:[\s_\-:]+)information\b[^\n]*"
    )

    matches = list(marker_re.finditer(normalized))

    if matches:
        leads = []

        for i, match in enumerate(matches):
            start_pos = match.start()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(normalized)

            value = normalized[start_pos:end_pos].strip()
            if value:
                leads.append(value)

        return leads

    # No recognised Personal Information marker: one non-empty line = one lead.
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def detect_upload_mode(raw):
    """
    Return (mode, marker_count) so the bot can report exactly how it parsed
    the uploaded file.
    """
    text = raw.decode("utf-8-sig", errors="replace")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    marker_re = re.compile(
        r"(?im)^[ \t]*personal(?:[\s_\-:]+)information\b[^\n]*"
    )

    marker_count = len(list(marker_re.finditer(normalized)))

    if marker_count:
        return "PERSONAL_INFORMATION", marker_count

    return "LINE_PER_LEAD", 0



def extract_bin6(lead_text):
    """
    Extract only the first 6 digits from a labelled Card Number field.

    Example:
      Card Number: 1234567890123456 -> 123456

    The full card number is never returned by this helper.
    """
    if not lead_text:
        return None

    match = re.search(
        r"(?im)^\s*(?:card\s*number|card\s*no|card)\s*[:\-]\s*([0-9][0-9\s\-]{5,})",
        lead_text,
    )
    if not match:
        return None

    digits = re.sub(r"\D", "", match.group(1))
    return digits[:6] if len(digits) >= 6 else None


def bin_keyboard(bin_values):
    rows = []
    current = []

    for bin6 in bin_values:
        current.append(
            InlineKeyboardButton(
                bin6,
                callback_data=f"bin:{bin6}",
            )
        )
        if len(current) == 2:
            rows.append(current)
            current = []

    if current:
        rows.append(current)

    rows.append([
        InlineKeyboardButton("ALL BINS", callback_data="bin:ALL")
    ])

    return InlineKeyboardMarkup(rows)


def get_campaign_bins(campaign_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT card_bin6, COUNT(*) AS c
            FROM leads
            WHERE campaign_id=?
              AND card_bin6 IS NOT NULL
            GROUP BY card_bin6
            ORDER BY c DESC, card_bin6 ASC
            """,
            (campaign_id,),
        ).fetchall()

    return [(row["card_bin6"], row["c"]) for row in rows]

def redact_phone_numbers(text):
    """
    Hide phone-number-like content before READY FOR WA notes are posted
    to the review group. Full notes stay unchanged in SQLite and are sent
    privately to the admin who presses TAKE FURTHER.
    """
    if not text:
        return ""

    redacted = text
    redacted = re.sub(
        r"(?im)^([ \t]*(?:phone|mobile|telephone|tel|contact[ \t]*number|number)[ \t]*[:\-][ \t]*).+$",
        r"\1[PHONE HIDDEN]",
        redacted,
    )

    def replace_number(match):
        raw = match.group(0)
        digits = sum(ch.isdigit() for ch in raw)
        return "[PHONE HIDDEN]" if 10 <= digits <= 15 else raw

    redacted = re.sub(
        r"(?<!\w)(?:\+?\d[\d\s().-]{8,20}\d)(?!\w)",
        replace_number,
        redacted,
    )
    return redacted

def lead_keyboard(lead_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("PICKED ✅", callback_data=f"call:pickup:{lead_id}"),
            InlineKeyboardButton("NO ANSWER 📵", callback_data=f"call:noanswer:{lead_id}"),
        ],
        [
            InlineKeyboardButton("RAN ❌", callback_data=f"call:ran:{lead_id}"),
            InlineKeyboardButton("INVALID 🚫", callback_data=f"call:invalid:{lead_id}"),
        ],
    ])


def new_lead_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("NEW LEAD ➡️", callback_data="newlead")
    ]])


def notes_keyboard(lead_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("READY FOR WA ✅", callback_data=f"notes:ready:{lead_id}"),
        InlineKeyboardButton("RAN ❌", callback_data=f"notes:ran:{lead_id}"),
    ]])


def approval_keyboard(worker_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"worker:approve:{worker_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"worker:decline:{worker_id}"),
    ]])


def take_keyboard(lead_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("TAKE FURTHER ➡️", callback_data=f"take:{lead_id}")
    ]])


async def is_admin(context, user_id, chat_id=None):
    gid = chat_id if chat_id is not None else get_group_id()
    if not gid:
        return False

    try:
        member = await context.bot.get_chat_member(gid, user_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError:
        return False


async def require_configured_admin(update, context):
    gid = get_group_id()
    if not gid:
        if update.effective_message:
            await update.effective_message.reply_text(
                "No review group is configured yet.\n\n"
                "Add me to your group and have a group admin send /setgroup there."
            )
        return False

    if not await is_admin(context, update.effective_user.id, gid):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Group admins only.")
        return False

    return True


async def require_worker(update):
    status = worker_status(update.effective_user.id)

    if status == "approved":
        return True

    if status == "pending":
        await update.effective_message.reply_text(
            "⏳ Your access request is waiting for admin approval."
        )
    elif status == "declined":
        await update.effective_message.reply_text(
            "⛔ Your access request was declined."
        )
    else:
        await update.effective_message.reply_text(
            "Send /start privately first to request access."
        )

    return False


def assign_next_lead(user):
    """
    SQLite-safe atomic assignment.

    A BEGIN IMMEDIATE transaction ensures two callers cannot receive
    the same lead at the same time.
    """
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        camp = conn.execute(
            """
            SELECT * FROM campaigns
            WHERE active=1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if not camp:
            conn.rollback()
            return "no_campaign", None, None

        existing = conn.execute(
            """
            SELECT * FROM leads
            WHERE campaign_id=?
              AND assigned_to=?
              AND status IN ('assigned','awaiting_notes')
            ORDER BY assigned_at DESC
            LIMIT 1
            """,
            (camp["id"], user.id),
        ).fetchone()

        if existing:
            conn.commit()
            return "existing", existing, camp["current_round"]

        round_no = camp["current_round"]

        bin_row = conn.execute(
            "SELECT value FROM settings WHERE key='active_card_bin6'"
        ).fetchone()
        active_bin = bin_row["value"] if bin_row else "ALL"

        if active_bin == "ALL":
            if active_bin == "ALL":
                lead = conn.execute(
                    """
                    SELECT * FROM leads
                    WHERE campaign_id=?
                      AND status IN ('available','no_answer')
                      AND assigned_to IS NULL
                      AND last_round < ?
                    ORDER BY position ASC
                    LIMIT 1
                    """,
                    (camp["id"], round_no),
                ).fetchone()
            else:
                lead = conn.execute(
                    """
                    SELECT * FROM leads
                    WHERE campaign_id=?
                      AND card_bin6=?
                      AND status IN ('available','no_answer')
                      AND assigned_to IS NULL
                      AND last_round < ?
                    ORDER BY position ASC
                    LIMIT 1
                    """,
                    (camp["id"], active_bin, round_no),
                ).fetchone()
        else:
            lead = conn.execute(
                """
                SELECT * FROM leads
                WHERE campaign_id=?
                  AND card_bin6=?
                  AND status IN ('available','no_answer')
                  AND assigned_to IS NULL
                  AND last_round < ?
                ORDER BY position ASC
                LIMIT 1
                """,
                (camp["id"], active_bin, round_no),
            ).fetchone()

        if not lead:
            if active_bin == "ALL":
                currently_busy = conn.execute(
                    """
                    SELECT COUNT(*) c
                    FROM leads
                    WHERE campaign_id=?
                      AND status IN ('assigned','awaiting_notes')
                    """,
                    (camp["id"],),
                ).fetchone()["c"]
            else:
                currently_busy = conn.execute(
                    """
                    SELECT COUNT(*) c
                    FROM leads
                    WHERE campaign_id=?
                      AND card_bin6=?
                      AND status IN ('assigned','awaiting_notes')
                    """,
                    (camp["id"], active_bin),
                ).fetchone()["c"]

            if active_bin == "ALL":
                retryable = conn.execute(
                    """
                    SELECT COUNT(*) c
                    FROM leads
                    WHERE campaign_id=?
                      AND status IN ('available','no_answer')
                    """,
                    (camp["id"],),
                ).fetchone()["c"]
            else:
                retryable = conn.execute(
                    """
                    SELECT COUNT(*) c
                    FROM leads
                    WHERE campaign_id=?
                      AND card_bin6=?
                      AND status IN ('available','no_answer')
                    """,
                    (camp["id"], active_bin),
                ).fetchone()["c"]

            if retryable == 0:
                conn.commit()
                return "finished", None, round_no

            if currently_busy > 0:
                conn.commit()
                return "waiting", None, round_no

            round_no += 1

            conn.execute(
                "UPDATE campaigns SET current_round=? WHERE id=?",
                (round_no, camp["id"]),
            )

            lead = conn.execute(
                """
                SELECT * FROM leads
                WHERE campaign_id=?
                  AND status IN ('available','no_answer')
                  AND assigned_to IS NULL
                  AND last_round < ?
                ORDER BY position ASC
                LIMIT 1
                """,
                (camp["id"], round_no),
            ).fetchone()

        if not lead:
            conn.commit()
            return "waiting", None, round_no

        conn.execute(
            """
            UPDATE leads
            SET status='assigned',
                assigned_to=?,
                assigned_at=?,
                caller_user_id=?,
                caller_username=?,
                caller_name=?
            WHERE id=?
            """,
            (
                user.id,
                now_iso(),
                user.id,
                user.username or "",
                user.full_name or "",
                lead["id"],
            ),
        )

        fresh = conn.execute(
            "SELECT * FROM leads WHERE id=?",
            (lead["id"],),
        ).fetchone()

        conn.commit()
        return "assigned", fresh, round_no

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Please open me privately and send /start."
        )
        return

    user = update.effective_user
    gid = get_group_id()

    if gid and await is_admin(context, user.id, gid):
        # Group admins are automatically approved as workers too.
        upsert_worker_request(user)

        with db() as conn:
            conn.execute(
                """
                UPDATE workers
                SET status='approved',
                    approved_at=?,
                    approved_by=?
                WHERE user_id=?
                """,
                (now_iso(), user.id, user.id),
            )

        await update.message.reply_text(
            "👑 Admin access ready.\n\n"
            "You can upload leads privately, approve workers in the group, "
            "use /stats, /workers, /export and /lead."
        )
        return

    state = upsert_worker_request(user)

    if state == "approved":
        await update.message.reply_text(
            "✅ You're approved. Send /lead when you're ready."
        )
        return

    if state == "pending":
        await update.message.reply_text(
            "⏳ Your access request is pending. I've sent/re-sent it to the admin group."
        )
    else:
        await update.message.reply_text(
            "✅ Access requested. A group admin can approve you."
        )

    if gid:
        try:
            await context.bot.send_message(
                gid,
                (
                    "👤 <b>Worker Access Request</b>\n\n"
                    f"Name: {escape(user.full_name or '')}\n"
                    f"Username: {escape('@'+user.username if user.username else 'None')}\n"
                    f"Telegram ID: <code>{user.id}</code>"
                ),
                parse_mode="HTML",
                reply_markup=approval_keyboard(user.id),
            )
        except TelegramError:
            log.exception("Could not send approval request to group")
            await update.message.reply_text(
                "⚠️ I couldn't post the approval request into the group. "
                "Ask an admin to make sure /setgroup was run and that I can send messages in the group."
            )
    else:
        await update.message.reply_text(
            "⚠️ No review group is configured yet. An admin must add me to the group and send /setgroup."
        )


async def setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.effective_message.reply_text(
            "Run /setgroup inside the Telegram group you want to use."
        )
        return

    # Bootstrap-safe: verify admin status directly in THIS group.
    if not await is_admin(context, update.effective_user.id, chat.id):
        await update.effective_message.reply_text(
            "⛔ Only an admin of this group can configure it."
        )
        return

    set_setting("team_group_id", str(chat.id))

    await update.effective_message.reply_text(
        "✅ This group is now the Ready-for-WA review group."
    )


async def worker_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    gid = get_group_id()

    if not gid or q.message.chat_id != gid:
        await q.answer(
            "This button is not from the configured review group.",
            show_alert=True,
        )
        return

    if not await is_admin(context, update.effective_user.id, gid):
        await q.answer(
            "Only current group admins can approve workers.",
            show_alert=True,
        )
        return

    _, action, worker_id_raw = q.data.split(":")
    worker_id = int(worker_id_raw)
    approved = action == "approve"

    with db() as conn:
        worker = conn.execute(
            "SELECT * FROM workers WHERE user_id=?",
            (worker_id,),
        ).fetchone()

        if not worker:
            await q.answer("Worker not found.", show_alert=True)
            return

        conn.execute(
            """
            UPDATE workers
            SET status=?,
                approved_at=?,
                approved_by=?
            WHERE user_id=?
            """,
            (
                "approved" if approved else "declined",
                now_iso() if approved else None,
                update.effective_user.id if approved else None,
                worker_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO admin_actions(
                admin_user_id,action,worker_user_id,created_at
            ) VALUES(?,?,?,?)
            """,
            (
                update.effective_user.id,
                "approve_worker" if approved else "decline_worker",
                worker_id,
                now_iso(),
            ),
        )

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    await q.message.reply_text(
        f"{'✅ Approved' if approved else '❌ Declined'} worker {worker_id}."
    )

    try:
        await context.bot.send_message(
            worker_id,
            (
                "✅ You've been approved. Send /lead when you're ready."
                if approved
                else "❌ Your access request was declined."
            ),
        )
    except TelegramError:
        pass


async def upload_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    if not await require_configured_admin(update, context):
        return

    doc = update.message.document
    filename = doc.file_name or "leads.txt"

    if not filename.lower().endswith((".txt", ".csv")):
        await update.message.reply_text("Please upload a .txt or .csv file.")
        return

    tg_file = await doc.get_file()
    raw = bytes(await tg_file.download_as_bytearray())

    detection_mode, marker_count = detect_upload_mode(raw)
    values = parse_leads(filename, raw)

    if not values:
        await update.message.reply_text("No leads were found.")
        return

    # Hold the parsed leads temporarily until the admin provides a file name.
    context.user_data["pending_lead_upload"] = {
        "original_filename": filename,
        "values": values,
        "detection_mode": detection_mode,
        "marker_count": marker_count,
    }

    if detection_mode == "PERSONAL_INFORMATION":
        await update.message.reply_text(
            f"✅ Personal Information format detected\n"
            f"Markers found: {marker_count:,}\n"
            f"Leads detected: {len(values):,}\n\n"
            "FILE NAME"
        )
    else:
        await update.message.reply_text(
            f"✅ Line-per-lead format detected\n"
            f"Leads detected: {len(values):,}\n\n"
            "FILE NAME"
        )


async def save_named_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, file_name: str):
    pending = context.user_data.get("pending_lead_upload")
    if not pending:
        return False

    values = pending["values"]
    display_name = file_name.strip()

    if not display_name:
        await update.message.reply_text("FILE NAME")
        return True

    # Keep names neat in group messages.
    if len(display_name) > 80:
        await update.message.reply_text("File name is too long. Send a shorter FILE NAME.")
        return True

    with db() as conn:
        conn.execute(
            "UPDATE campaigns SET active=0 WHERE active=1"
        )

        cur = conn.execute(
            """
            INSERT INTO campaigns(
                filename,created_at,created_by,current_round,active
            ) VALUES(?,?,?,1,1)
            """,
            (
                display_name,
                now_iso(),
                update.effective_user.id,
            ),
        )

        campaign_id = cur.lastrowid

        prepared = [
            (campaign_id, i, value, extract_bin6(value))
            for i, value in enumerate(values, start=1)
        ]

        conn.executemany(
            """
            INSERT INTO leads(
                campaign_id,position,value,card_bin6,status,last_round
            ) VALUES(?,?,?,?, 'available',0)
            """,
            prepared,
        )

        conn.execute(
            """
            INSERT INTO admin_actions(
                admin_user_id,action,created_at,detail
            ) VALUES(?,?,?,?)
            """,
            (
                update.effective_user.id,
                "upload_campaign",
                now_iso(),
                display_name,
            ),
        )

    context.user_data.pop("pending_lead_upload", None)

    bins = get_campaign_bins(campaign_id)

    # Default to ALL so existing behaviour remains unchanged until an admin
    # explicitly selects a BIN.
    set_setting("active_card_bin6", "ALL")

    if bins:
        lines = [
            f"✅ <b>{escape(display_name)}</b>",
            f"{len(values):,} leads loaded",
            "",
            "<b>Card BIN groups</b>",
        ]
        for bin6, count in bins:
            lines.append(f"{escape(bin6)}: <b>{count:,}</b>")

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=bin_keyboard([b for b, _ in bins]),
        )
    else:
        await update.message.reply_text(
            f"✅ <b>{escape(display_name)}</b>\n"
            f"{len(values):,} leads loaded.",
            parse_mode="HTML",
        )

    return True

async def send_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Use /lead in a private chat with me."
        )
        return

    if not await require_worker(update):
        return

    state, lead, round_no = assign_next_lead(update.effective_user)

    if state == "no_campaign":
        await update.effective_message.reply_text("No active campaign yet.")
        return

    if state == "finished":
        await update.effective_message.reply_text(
            "🎉 There are no unresolved leads left."
        )
        return

    if state == "waiting":
        await update.effective_message.reply_text(
            "⏳ All currently eligible leads are being handled by other callers. "
            "Try /lead again shortly."
        )
        return

    warning = (
        "⚠️ Finish this lead before requesting another.\n\n"
        if state == "existing"
        else ""
    )

    await update.effective_message.reply_text(
        (
            f"{warning}"
            f"📞 <b>Your Lead</b>\n\n"
            f"<code>{escape(lead['value'])}</code>\n\n"
            f"Attempt: <b>{lead['attempts'] + 1}</b> · "
            f"Round: <b>{round_no}</b>"
        ),
        parse_mode="HTML",
        reply_markup=lead_keyboard(lead["id"]),
    )


async def call_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await require_worker(update):
        return

    _, action, lead_id_raw = q.data.split(":")
    lead_id = int(lead_id_raw)
    uid = update.effective_user.id

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        camp = conn.execute(
            """
            SELECT * FROM campaigns
            WHERE active=1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if not camp:
            conn.rollback()
            await q.answer("No active campaign.", show_alert=True)
            return

        lead = conn.execute(
            """
            SELECT * FROM leads
            WHERE id=? AND campaign_id=?
            """,
            (lead_id, camp["id"]),
        ).fetchone()

        if (
            not lead
            or lead["status"] != "assigned"
            or lead["assigned_to"] != uid
        ):
            conn.rollback()
            await q.answer(
                "That lead is no longer assigned to you.",
                show_alert=True,
            )
            return

        event_time = now_iso()

        # -------------------------
        # NO ANSWER -> retry pool
        # -------------------------
        if action == "noanswer":
            conn.execute(
                """
                UPDATE leads
                SET status='no_answer',
                    assigned_to=NULL,
                    assigned_at=NULL,
                    attempts=attempts+1,
                    last_round=?,
                    last_attempt_at=?
                WHERE id=?
                """,
                (
                    camp["current_round"],
                    event_time,
                    lead_id,
                ),
            )

            conn.execute(
                """
                INSERT INTO call_events(
                    lead_id,campaign_id,worker_user_id,
                    worker_username,worker_name,outcome,created_at
                ) VALUES(?,?,?,?,?,'no_answer',?)
                """,
                (
                    lead_id,
                    camp["id"],
                    uid,
                    update.effective_user.username or "",
                    update.effective_user.full_name or "",
                    event_time,
                ),
            )

            conn.commit()

            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass

            await q.message.reply_text(
                "📵 NO ANSWER",
                reply_markup=new_lead_keyboard(),
            )
            return

        # -------------------------
        # RAN -> permanently close
        # -------------------------
        if action == "ran":
            conn.execute(
                """
                UPDATE leads
                SET status='ran',
                    assigned_to=NULL,
                    assigned_at=NULL,
                    attempts=attempts+1,
                    last_round=?,
                    last_attempt_at=?,
                    ran_by=?,
                    ran_at=?
                WHERE id=?
                """,
                (
                    camp["current_round"],
                    event_time,
                    uid,
                    event_time,
                    lead_id,
                ),
            )

            conn.execute(
                """
                INSERT INTO call_events(
                    lead_id,campaign_id,worker_user_id,
                    worker_username,worker_name,outcome,created_at
                ) VALUES(?,?,?,?,?,'ran',?)
                """,
                (
                    lead_id,
                    camp["id"],
                    uid,
                    update.effective_user.username or "",
                    update.effective_user.full_name or "",
                    event_time,
                ),
            )

            conn.commit()

            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass

            await q.message.reply_text(
                "❌ RAN",
                reply_markup=new_lead_keyboard(),
            )
            return

        # -------------------------
        # INVALID -> permanently close
        # -------------------------
        if action == "invalid":
            conn.execute(
                """
                UPDATE leads
                SET status='invalid',
                    assigned_to=NULL,
                    assigned_at=NULL,
                    attempts=attempts+1,
                    last_round=?,
                    last_attempt_at=?
                WHERE id=?
                """,
                (
                    camp["current_round"],
                    event_time,
                    lead_id,
                ),
            )

            conn.execute(
                """
                INSERT INTO call_events(
                    lead_id,campaign_id,worker_user_id,
                    worker_username,worker_name,outcome,created_at
                ) VALUES(?,?,?,?,?,'invalid',?)
                """,
                (
                    lead_id,
                    camp["id"],
                    uid,
                    update.effective_user.username or "",
                    update.effective_user.full_name or "",
                    event_time,
                ),
            )

            conn.commit()

            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except TelegramError:
                pass

            await q.message.reply_text(
                "🚫 INVALID",
                reply_markup=new_lead_keyboard(),
            )
            return

        # -------------------------
        # PICKED -> notes workflow
        # -------------------------
        if action != "pickup":
            conn.rollback()
            await q.answer("Unknown option.", show_alert=True)
            return

        conn.execute(
            """
            UPDATE leads
            SET status='awaiting_notes',
                attempts=attempts+1,
                last_round=?,
                last_attempt_at=?
            WHERE id=?
            """,
            (
                camp["current_round"],
                event_time,
                lead_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO call_events(
                lead_id,campaign_id,worker_user_id,
                worker_username,worker_name,outcome,created_at
            ) VALUES(?,?,?,?,?,'picked_up',?)
            """,
            (
                lead_id,
                camp["id"],
                uid,
                update.effective_user.username or "",
                update.effective_user.full_name or "",
                event_time,
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    gid = get_group_id()
    if gid:
        worker_label = (
            f"@{update.effective_user.username}"
            if update.effective_user.username
            else (update.effective_user.full_name or str(uid))
        )
        try:
            campaign_name = camp["filename"] or "Unnamed"
            await context.bot.send_message(
                gid,
                (
                    "✅ <b>PICKED</b>\n"
                    f"👤 {escape(worker_label)}\n"
                    f"🏦 <b>{escape(campaign_name)}</b>\n"
                    f"⏰ {escape(uk_time_text())}"
                ),
                parse_mode="HTML",
            )
        except TelegramError:
            log.exception("Could not post Call Picked notification")

    await q.message.reply_text("Upload Notes 📝")


async def new_lead_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await require_worker(update):
        return

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    # send_lead now uses update.effective_message, so it works correctly
    # from this callback as well as from the /lead command.
    await send_lead(update, context)

async def notes_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    # If an admin just uploaded a lead file, their next text message is the
    # custom file/campaign name rather than lead notes.
    if context.user_data.get("pending_lead_upload"):
        if await require_configured_admin(update, context):
            text = (update.message.text or "").strip()
            if text and not text.startswith("/"):
                await save_named_upload(update, context, text)
        return

    if not await require_worker(update):
        return

    text = (update.message.text or "").strip()

    if not text or text.startswith("/"):
        return

    camp = active_campaign()

    if not camp:
        return

    with db() as conn:
        lead = conn.execute(
            """
            SELECT * FROM leads
            WHERE campaign_id=?
              AND assigned_to=?
              AND status='awaiting_notes'
            ORDER BY assigned_at DESC
            LIMIT 1
            """,
            (
                camp["id"],
                update.effective_user.id,
            ),
        ).fetchone()

        if not lead:
            return

        conn.execute(
            "UPDATE leads SET notes=? WHERE id=?",
            (text, lead["id"]),
        )

    await update.message.reply_text(
        (
            "📝 <b>Notes saved</b>\n\n"
            f"{escape(text)}\n\n"
            "Choose what happens next:"
        ),
        parse_mode="HTML",
        reply_markup=notes_keyboard(lead["id"]),
    )


async def notes_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await require_worker(update):
        return

    _, action, lead_id_raw = q.data.split(":")
    lead_id = int(lead_id_raw)
    uid = update.effective_user.id

    if action == "ran":
        conn = db()

        try:
            conn.execute("BEGIN IMMEDIATE")

            lead = conn.execute(
                "SELECT * FROM leads WHERE id=?",
                (lead_id,),
            ).fetchone()

            if (
                not lead
                or lead["status"] != "awaiting_notes"
                or lead["assigned_to"] != uid
            ):
                conn.rollback()
                await q.answer(
                    "That lead is no longer yours.",
                    show_alert=True,
                )
                return

            conn.execute(
                """
                UPDATE leads
                SET status='ran',
                    assigned_to=NULL,
                    assigned_at=NULL,
                    ran_by=?,
                    ran_at=?
                WHERE id=?
                """,
                (
                    uid,
                    now_iso(),
                    lead_id,
                ),
            )

            conn.commit()

        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass

        await q.message.reply_text(
            "❌ RAN",
            reply_markup=new_lead_keyboard(),
        )
        return

    gid = get_group_id()

    if not gid:
        await q.answer(
            "No review group is configured.",
            show_alert=True,
        )
        return

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        lead = conn.execute(
            "SELECT * FROM leads WHERE id=?",
            (lead_id,),
        ).fetchone()

        if (
            not lead
            or lead["status"] != "awaiting_notes"
            or lead["assigned_to"] != uid
            or not (lead["notes"] or "").strip()
        ):
            conn.rollback()
            await q.answer(
                "This lead isn't ready.",
                show_alert=True,
            )
            return

        conn.execute(
            """
            UPDATE leads
            SET status='ready_wa',
                ready_by=?,
                ready_at=?,
                assigned_to=NULL,
                assigned_at=NULL
            WHERE id=?
            """,
            (
                uid,
                now_iso(),
                lead_id,
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    caller = (
        f"@{lead['caller_username']}"
        if lead["caller_username"]
        else (lead["caller_name"] or str(uid))
    )

    # IMPORTANT:
    # No phone number / original lead line is included here.
    safe_notes = redact_phone_numbers(lead["notes"] or "")

    with db() as conn:
        campaign_row = conn.execute(
            "SELECT filename FROM campaigns WHERE id=?",
            (lead["campaign_id"],),
        ).fetchone()

    campaign_name = (
        campaign_row["filename"]
        if campaign_row and campaign_row["filename"]
        else "Unnamed"
    )

    group_text = (
        "📲 <b>READY FOR WA</b>\n"
        f"🏦 <b>{escape(campaign_name)}</b>\n"
        f"📝 <b>Notes:</b>\n"
        f"{escape(safe_notes)}\n\n\n\n"
        f"⏰ {escape(uk_time_text())}\n"
        f"👤 {escape(caller)}"
    )

    try:
        await context.bot.send_message(
            gid,
            group_text,
            parse_mode="HTML",
            reply_markup=take_keyboard(lead_id),
        )
    except TelegramError:
        log.exception("Failed to post Ready for WA")

        with db() as conn:
            conn.execute(
                """
                UPDATE leads
                SET status='awaiting_notes',
                    assigned_to=?,
                    assigned_at=?
                WHERE id=?
                """,
                (
                    uid,
                    now_iso(),
                    lead_id,
                ),
            )

        await q.answer(
            "Couldn't post to the group. Ask an admin to check /setgroup.",
            show_alert=True,
        )
        return

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    await q.message.reply_text(
        "✅ READY FOR WA",
        reply_markup=new_lead_keyboard(),
    )


async def take_further(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    gid = get_group_id()

    if not gid or q.message.chat_id != gid:
        await q.answer(
            "This is not the configured review group.",
            show_alert=True,
        )
        return

    admin = update.effective_user

    if not await is_admin(context, admin.id, gid):
        await q.answer(
            "Only current group admins can Take Further.",
            show_alert=True,
        )
        return

    lead_id = int(q.data.split(":")[1])

    conn = db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        lead = conn.execute(
            "SELECT * FROM leads WHERE id=?",
            (lead_id,),
        ).fetchone()

        if not lead or lead["status"] != "ready_wa":
            conn.rollback()
            await q.answer(
                "This lead has already been taken or is no longer available.",
                show_alert=True,
            )
            return

        # Temporary claim prevents two admins from taking the same lead at once.
        conn.execute(
            """
            UPDATE leads
            SET status='taking',
                taken_by=?
            WHERE id=?
            """,
            (
                admin.id,
                lead_id,
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    caller = (
        f"@{lead['caller_username']}"
        if lead["caller_username"]
        else (
            lead["caller_name"]
            or str(lead["caller_user_id"] or "")
        )
    )

    private_text = (
        "🔥 <b>LEAD TO TAKE FURTHER</b>\n\n"
        f"<b>Full Lead</b>\n"
        f"<code>{escape(lead['value'])}</code>\n\n"
        f"<b>Notes</b>\n"
        f"{escape(lead['notes'] or '')}\n\n"
        f"<b>Caller</b>: {escape(caller)}\n"
        f"<b>Attempts</b>: {lead['attempts']}\n"
        f"<b>Last call</b>: {escape(lead['last_attempt_at'] or 'Unknown')}\n"
        f"<b>Ready for WA</b>: {escape(lead['ready_at'] or 'Unknown')}"
    )

    try:
        await context.bot.send_message(
            admin.id,
            private_text,
            parse_mode="HTML",
        )

    except Forbidden:
        with db() as conn:
            conn.execute(
                """
                UPDATE leads
                SET status='ready_wa',
                    taken_by=NULL
                WHERE id=?
                  AND status='taking'
                  AND taken_by=?
                """,
                (
                    lead_id,
                    admin.id,
                ),
            )

        await q.answer(
            "Open the bot privately and send /start first, "
            "then press Take Further again.",
            show_alert=True,
        )
        return

    except TelegramError:
        with db() as conn:
            conn.execute(
                """
                UPDATE leads
                SET status='ready_wa',
                    taken_by=NULL
                WHERE id=?
                  AND status='taking'
                  AND taken_by=?
                """,
                (
                    lead_id,
                    admin.id,
                ),
            )

        await q.answer(
            "Couldn't send you the private message. Try again.",
            show_alert=True,
        )
        return

    with db() as conn:
        conn.execute(
            """
            UPDATE leads
            SET status='taken',
                taken_at=?
            WHERE id=?
              AND status='taking'
              AND taken_by=?
            """,
            (
                now_iso(),
                lead_id,
                admin.id,
            ),
        )

        conn.execute(
            """
            INSERT INTO admin_actions(
                admin_user_id,action,lead_id,created_at
            ) VALUES(?,?,?,?)
            """,
            (
                admin.id,
                "take_further",
                lead_id,
                now_iso(),
            ),
        )

    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    who = (
        f"@{admin.username}"
        if admin.username
        else admin.full_name
    )

    await q.message.reply_text(
        f"✅ Taken further privately by {escape(who)}.",
        parse_mode="HTML",
    )



async def bins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_configured_admin(update, context):
        return

    camp = active_campaign()
    if not camp:
        await update.effective_message.reply_text("No active campaign.")
        return

    bins = get_campaign_bins(camp["id"])
    if not bins:
        await update.effective_message.reply_text(
            "No card BIN groups were detected in this file."
        )
        return

    current = get_setting("active_card_bin6") or "ALL"

    await update.effective_message.reply_text(
        f"Current BIN: <b>{escape(current)}</b>",
        parse_mode="HTML",
        reply_markup=bin_keyboard([b for b, _ in bins]),
    )


async def bin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not await require_configured_admin(update, context):
        return

    selected = q.data.split(":", 1)[1]

    if selected != "ALL":
        camp = active_campaign()
        if not camp:
            await q.answer("No active campaign.", show_alert=True)
            return

        valid_bins = {b for b, _ in get_campaign_bins(camp["id"])}
        if selected not in valid_bins:
            await q.answer("That BIN is not in the active file.", show_alert=True)
            return

    set_setting("active_card_bin6", selected)

    try:
        await q.edit_message_text(
            f"✅ Active BIN: <b>{escape(selected)}</b>",
            parse_mode="HTML",
        )
    except TelegramError:
        await q.message.reply_text(
            f"✅ Active BIN: <b>{escape(selected)}</b>",
            parse_mode="HTML",
        )

async def remove_worker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_configured_admin(update, context):
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /remove @username")
        return

    username = context.args[0].strip()
    if username.startswith("@"):
        username = username[1:]

    if not username:
        await update.effective_message.reply_text("Usage: /remove @username")
        return

    with db() as conn:
        worker = conn.execute(
            """
            SELECT * FROM workers
            WHERE LOWER(username)=LOWER(?)
            LIMIT 1
            """,
            (username,),
        ).fetchone()

        if not worker:
            await update.effective_message.reply_text(
                f"Couldn't find @{escape(username)} in the worker list.",
                parse_mode="HTML",
            )
            return

        worker_id = worker["user_id"]

        conn.execute(
            """
            UPDATE leads
            SET status='available',
                assigned_to=NULL,
                assigned_at=NULL
            WHERE assigned_to=?
              AND status IN ('assigned','awaiting_notes')
            """,
            (worker_id,),
        )

        conn.execute(
            "DELETE FROM workers WHERE user_id=?",
            (worker_id,),
        )

        conn.execute(
            """
            INSERT INTO admin_actions(
                admin_user_id,action,worker_user_id,created_at,detail
            ) VALUES(?,?,?,?,?)
            """,
            (
                update.effective_user.id,
                "remove_worker",
                worker_id,
                now_iso(),
                username,
            ),
        )

    await update.effective_message.reply_text(
        f"✅ Removed @{escape(username)} from the worker list.",
        parse_mode="HTML",
    )

    try:
        await context.bot.send_message(
            worker_id,
            "Your worker access has been removed by an admin."
        )
    except TelegramError:
        pass


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_configured_admin(update, context):
        return

    camp = active_campaign()

    if not camp:
        await update.effective_message.reply_text("No active campaign.")
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) c
            FROM leads
            WHERE campaign_id=?
            GROUP BY status
            """,
            (camp["id"],),
        ).fetchall()

        attempts = conn.execute(
            """
            SELECT COALESCE(SUM(attempts),0) total
            FROM leads
            WHERE campaign_id=?
            """,
            (camp["id"],),
        ).fetchone()["total"]

        total = conn.execute(
            """
            SELECT COUNT(*) c
            FROM leads
            WHERE campaign_id=?
            """,
            (camp["id"],),
        ).fetchone()["c"]

    counts = {row["status"]: row["c"] for row in rows}

    with db() as conn:
        caller_rows = conn.execute(
            """
            SELECT
                worker_user_id,
                MAX(worker_username) AS username,
                MAX(worker_name) AS worker_name,
                SUM(CASE WHEN outcome='picked_up' THEN 1 ELSE 0 END) AS picked,
                SUM(CASE WHEN outcome='no_answer' THEN 1 ELSE 0 END) AS no_answer
            FROM call_events
            WHERE campaign_id=?
            GROUP BY worker_user_id
            ORDER BY (picked + no_answer) DESC, picked DESC
            """,
            (camp["id"],),
        ).fetchall()

    caller_lines = []
    for row in caller_rows:
        if row["username"]:
            label = f"@{row['username']}"
        else:
            label = row["worker_name"] or str(row["worker_user_id"])

        caller_lines.append(
            f"{escape(label)}  ✅ {row['picked'] or 0}  ❌ {row['no_answer'] or 0}"
        )

    callers_text = (
        "\n\n<b>Caller Stats</b>\n" + "\n".join(caller_lines)
        if caller_lines
        else "\n\n<b>Caller Stats</b>\nNo calls recorded yet."
    )

    await update.effective_message.reply_text(
        (
            "📊 <b>Campaign Stats</b>\n\n"
            f"Total uploaded: <b>{total:,}</b>\n"
            f"Untouched: <b>{counts.get('available', 0):,}</b>\n"
            f"Currently assigned: <b>{counts.get('assigned', 0):,}</b>\n"
            f"No Answer / retry pool: <b>{counts.get('no_answer', 0):,}</b>\n"
            f"Picked Up / awaiting notes: <b>{counts.get('awaiting_notes', 0):,}</b>\n"
            f"Ready for WA: <b>{counts.get('ready_wa', 0):,}</b>\n"
            f"Ran: <b>{counts.get('ran', 0):,}</b>\n"
            f"Invalid: <b>{counts.get('invalid', 0):,}</b>\n"
            f"Taken Further: <b>{counts.get('taken', 0):,}</b>\n"
            f"Current retry round: <b>{camp['current_round']}</b>\n"
            f"Total call attempts: <b>{attempts:,}</b>"
            f"{callers_text}"
        ),
        parse_mode="HTML",
    )


async def workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_configured_admin(update, context):
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM workers
            ORDER BY requested_at DESC
            LIMIT 100
            """
        ).fetchall()

    if not rows:
        await update.effective_message.reply_text(
            "No workers have registered yet."
        )
        return

    lines = []

    for row in rows:
        icon = {
            "approved": "✅",
            "pending": "⏳",
            "declined": "❌",
        }.get(row["status"], "•")

        name = row["first_name"] or str(row["user_id"])
        username = (
            f" @{row['username']}"
            if row["username"]
            else ""
        )

        lines.append(
            f"{icon} {name}{username} — {row['status']}"
        )

    await update.effective_message.reply_text(
        "\n".join(lines)
    )


async def export_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Use /export privately so full lead details aren't exposed in the group."
        )
        return

    if not await require_configured_admin(update, context):
        return

    camp = active_campaign()

    if not camp:
        await update.message.reply_text("No active campaign.")
        return

    with db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM leads
            WHERE campaign_id=?
            ORDER BY position
            """,
            (camp["id"],),
        ).fetchall()

    out = io.StringIO()
    writer = csv.writer(out)

    writer.writerow([
        "position",
        "lead",
        "status",
        "attempts",
        "last_round",
        "last_attempt_at",
        "notes",
        "caller_user_id",
        "caller_username",
        "caller_name",
        "ready_at",
        "ran_by",
        "ran_at",
        "taken_by",
        "taken_at",
    ])

    for row in rows:
        writer.writerow([
            row["position"],
            row["value"],
            row["status"],
            row["attempts"],
            row["last_round"],
            row["last_attempt_at"] or "",
            row["notes"] or "",
            row["caller_user_id"] or "",
            row["caller_username"] or "",
            row["caller_name"] or "",
            row["ready_at"] or "",
            row["ran_by"] or "",
            row["ran_at"] or "",
            row["taken_by"] or "",
            row["taken_at"] or "",
        ])

    data = io.BytesIO(
        out.getvalue().encode("utf-8-sig")
    )

    data.name = "lead_results.csv"

    await update.message.reply_document(
        document=data,
        filename=data.name,
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_configured_admin(update, context):
        return

    with db() as conn:
        camp = conn.execute(
            """
            SELECT * FROM campaigns
            WHERE active=1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if not camp:
            await update.effective_message.reply_text(
                "No active campaign."
            )
            return

        conn.execute(
            "UPDATE campaigns SET active=0 WHERE id=?",
            (camp["id"],),
        )

        conn.execute(
            """
            INSERT INTO admin_actions(
                admin_user_id,action,created_at
            ) VALUES(?,?,?)
            """,
            (
                update.effective_user.id,
                "reset_campaign",
                now_iso(),
            ),
        )

    await update.effective_message.reply_text(
        "✅ Current campaign archived. Upload a new lead file privately when ready."
    )


async def error_handler(update, context):
    log.exception(
        "Unhandled bot error",
        exc_info=context.error,
    )


def main():
    if not TOKEN:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lead", send_lead))
    app.add_handler(CommandHandler("setgroup", setgroup))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("bins", bins_command))
    app.add_handler(CommandHandler("workers", workers))
    app.add_handler(CommandHandler("remove", remove_worker))
    app.add_handler(CommandHandler("export", export_results))
    app.add_handler(CommandHandler("reset", reset))

    app.add_handler(
        CallbackQueryHandler(
            worker_approval,
            pattern=r"^worker:(approve|decline):\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            call_result,
            pattern=r"^call:(pickup|noanswer|ran|invalid):\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            notes_result,
            pattern=r"^notes:(ready|ran):\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            new_lead_callback,
            pattern=r"^newlead$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            bin_callback,
            pattern=r"^bin:(ALL|\d{6})$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            take_further,
            pattern=r"^take:\d+$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            upload_leads,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            notes_message,
        )
    )

    app.add_error_handler(error_handler)

    log.info("Team Lead Manager Admin v4 robust Personal Information detection is running")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
