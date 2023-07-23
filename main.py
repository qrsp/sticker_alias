import logging
import os
import re
import sqlite3
from datetime import datetime, time, timedelta
from typing import Dict, Optional

from dotenv import dotenv_values
from pytz import timezone
from telegram import (CallbackQuery, InlineKeyboardButton,
                      InlineKeyboardMarkup, InlineQueryResultCachedSticker,
                      Sticker, Update)
from telegram.ext import (Application, CallbackContext, CallbackQueryHandler,
                          ChosenInlineResultHandler, CommandHandler,
                          ContextTypes, InlineQueryHandler, MessageHandler,
                          filters)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger: logging.Logger = logging.getLogger(__name__)


config: Dict[str, Optional[str]] = dotenv_values(".env")
if not config["DB_FILE"] or not config["API_TOKEN"]:
    logger.error("Setup DB_FILE and API_TOKEN in .env file.")
    exit(1)

API_TOKEN: str = config["API_TOKEN"]
DB_FILE: str = config["DB_FILE"]


def get_connection(context: ContextTypes.DEFAULT_TYPE) -> sqlite3.Connection:
    connection = context.bot_data.get("connection")
    if not connection:
        connection = sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES)
        # Enabling Foreign Key Support.
        connection.execute("PRAGMA foreign_keys = ON")
        context.bot_data["connection"] = connection
    return connection


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""
    await update.message.reply_html(
        """
Send me stciker to set alias.
Use @stickers_alias_bot inline mode to search stickers on the fly.

Example:
@stickers_alias_bot [query] - Search stickers by alias.
@stickers_alias_bot [1 - 9] - Show stickers in the favorite [1 - 9].
@stickers_alias_bot % - Show trending stickers.
@stickers_alias_bot [1 - 9] i - Show stickers in the favorite [1 - 9]. Results are returned from bot not from server by cache. Useful when edit favorite, show correct result.
@stickers_alias_bot ,[query] - Search stickers by set alias.
@stickers_alias_bot ,[query] i - Search stickers by set alias. Results are returned from bot not from server by cache.

Command:
/help - Show help message.
/favorite add - Add stickers to favorite.
/favorite delete - Delete stickers from favorite.
/alias - Show all alias.
/bulk - Update sticker set alias.
            """,
        disable_web_page_preview=True,
    )


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorize(update.message.from_user.id, context):
        return

    await update.message.reply_document(open("sticker.db", "rb"))


async def favorite_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorize(update.message.from_user.id, context):
        return

    if not context.args or len(context.args) == 0:
        await update.message.reply_text(
            "/favorite add - Add stickers to favorite.\n/favorite delete - Delete stickers from favorite."
        )
        return

    if context.args[0] == "add":
        context.chat_data["status"] = "Add favorite"
    elif context.args[0] == "delete":
        context.chat_data["status"] = "Delete favorite"
    else:
        return

    keyboard = []
    for i in range(3):
        a: int = i * 3 + 1
        b: int = a + 1
        c: int = b + 1
        keyboard.append(
            [
                InlineKeyboardButton(
                    str(a),
                    callback_data=f"favorite_group {a} {update.message.from_user.id}",
                ),
                InlineKeyboardButton(
                    str(b),
                    callback_data=f"favorite_group {b} {update.message.from_user.id}",
                ),
                InlineKeyboardButton(
                    str(c),
                    callback_data=f"favorite_group {c} {update.message.from_user.id}",
                ),
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Which favorite:", reply_markup=reply_markup)


async def alias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorize(update.message.from_user.id, context):
        return

    cursor = get_connection(context).cursor()

    results = search_all_alias(cursor)
    reply = "alias:\n"
    for alias in results:
        reply += f"{alias[0]}\n"

    await update.message.reply_text(reply)

    results = search_all_set_alias(cursor)
    reply = "set_alias:\n"
    for set_alias in results:
        reply += f"{set_alias[0]}\n"

    await update.message.reply_text(reply)


async def bulk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorize(update.message.from_user.id, context):
        return

    context.chat_data["status"] = "Bulk update alias"
    await update.message.reply_text("Send a sticker to me.")


def calculate_score(age, gravity=1.8):
    return 1 / pow((age + 2), gravity)


async def callback_update_trending(context: ContextTypes.DEFAULT_TYPE):
    connection = sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES)
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()

    if not context.bot_data.get("admin"):
        context.bot_data["admin"] = select_admin_id(cursor)[0]

    await context.bot.send_message(
        context.bot_data.get("admin"), "System: Update trending..."
    )

    create_trending(cursor, True)
    now = datetime.now()
    oldest_time = now - timedelta(days=90)

    for result in select_all_user_id(cursor):
        user_id = result[0]
        recorder = search_chosen_recent(cursor, user_id, oldest_time)

        score_dict: Dict[int, int] = {}
        for file_unique_id, _, chosen_time in recorder:
            age = now - chosen_time
            day_age = round(age.total_seconds() / 86400)
            try:
                score_dict[file_unique_id] += calculate_score(day_age)
            except KeyError:
                score_dict[file_unique_id] = 0
                score_dict[file_unique_id] += calculate_score(day_age)

        # If two stickers have the same weight, the recently used one wins.
        score_list = sorted(score_dict, key=score_dict.get)
        for idx, file_unique_id in enumerate(score_list):
            insert_trending_tmp(cursor, file_unique_id, user_id, idx)

    drop_trending(cursor)
    alter_tmp_to_trending(cursor)
    connection.commit()
    connection.close()

    await context.bot.send_message(
        context.bot_data.get("admin"), "System: Trending updated."
    )


async def favorite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cursor: sqlite3.Cursor = get_connection(context).cursor()

    query: CallbackQuery = update.callback_query
    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()
    await query.message.delete()

    _, group_no, user_id_str = query.data.split()
    context.chat_data["group_no"] = int(group_no)
    user_id: int = int(user_id_str)

    result = count_favorite_sticker(cursor, user_id, group_no)
    number_of_stickers = int(result[0])
    context.chat_data["number_of_stickers"] = number_of_stickers

    if context.chat_data["status"] == "Add favorite":
        await query.message.reply_text(f"Add to favorite {group_no} ...")
    elif context.chat_data["status"] == "Delete favorite":
        await query.message.reply_text(f"Delete from favorite {group_no} ...")

    keyboard = []
    keyboard.append([InlineKeyboardButton("Finish", callback_data="finish")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    context.chat_data["finish_message"] = await query.message.reply_text(
        f"Favorite {group_no}: {number_of_stickers}", reply_markup=reply_markup
    )

    await context.chat_data["finish_message"].pin()


async def update_favorite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cursor: sqlite3.Cursor = get_connection(context).cursor()

    sticker: Sticker = update.message.sticker
    user_id: int = update.message.from_user.id
    data: dict = context.chat_data
    rowcount: int = 0

    if context.chat_data["status"] == "Add favorite":
        conflict_group_no = None
        try:
            rowcount = insert_favorite(
                cursor, user_id, sticker.file_unique_id, context.chat_data["group_no"]
            )
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY constraint failed" in e.args:
                insert_or_update_sticker(
                    cursor, sticker.file_unique_id, sticker.file_id, user_id, None, None
                )

                rowcount = insert_favorite(
                    cursor,
                    user_id,
                    sticker.file_unique_id,
                    context.chat_data["group_no"],
                )
            elif "UNIQUE constraint failed" in str(e):
                conflict_group_no = search_favorite_group_no(
                    cursor, user_id, sticker.file_unique_id
                )[0]
                rowcount = -1

        if rowcount == 1:
            data["number_of_stickers"] = data["number_of_stickers"] + 1
        elif rowcount == 0:
            await update.message.reply_text("The sticker has already in this favorite.")
        elif rowcount == -1:
            await update.message.reply_text(
                f"The sticker has already in favorite {conflict_group_no}."
            )

    elif context.chat_data["status"] == "Delete favorite":
        rowcount = delete_favoirte(
            cursor, user_id, sticker.file_unique_id, context.chat_data["group_no"]
        )

        if rowcount == 1:
            data["number_of_stickers"] = data["number_of_stickers"] - 1
        elif rowcount == 0:
            await update.message.reply_text("The sticker is not in this favorite.")

    if rowcount == 1:
        await data["finish_message"].edit_text(
            f"Favorite {data['group_no']}: {data['number_of_stickers']}",
            reply_markup=data["finish_message"].reply_markup,
        )


async def finish_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query: CallbackQuery = update.callback_query
    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()
    await query.message.delete()

    conn: sqlite3.Connection = get_connection(context)
    conn.commit()

    data: dict = context.chat_data
    await query.message.reply_text(
        f"Succeeded. Now Favorite {data['group_no']} has {data['number_of_stickers']} stickers."
    )
    data.clear()


def flag_parser(query: str):
    flags = []
    alias = query
    if query.startswith("，") or query.startswith(","):
        flags.append("set alias")
        alias = alias[1:]
    if query.endswith(" i"):
        flags.append("fresh")
        alias = alias[:-2]
    return flags, alias


async def inlinequery(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.inline_query.from_user.id
    if not authorize(user_id, context):
        return

    """Handle the inline query."""
    query = update.inline_query.query

    # logger.info(f"query: {query}")
    flags, alias = flag_parser(query)
    cursor = get_connection(context).cursor()

    if alias == "":
        # Fix first response would not display correctly.
        last_sticker = context.user_data.get("last_results")
        if last_sticker:
            await update.inline_query.answer(last_sticker, auto_pagination=True)
        else:
            stickers = search_trending_sticker(cursor, user_id)
            if stickers:
                file_unique_id, file_id = stickers[0]
                result = [
                    InlineQueryResultCachedSticker(
                        id=file_unique_id,
                        sticker_file_id=file_id,
                    )
                ]
                await update.inline_query.answer(result)
        return

    if re.match(r"^(\d)$", alias):
        stickers = search_sticker_by_favortie(cursor, user_id, int(alias))
    elif alias == "%":
        stickers = search_trending_sticker(cursor, user_id)
    else:
        if "set alias" in flags:
            stickers = search_sticker_by_set_alias(cursor, alias)
        else:
            stickers = search_sticker_by_alias(cursor, user_id, alias)

    results = []
    if stickers:
        for file_unique_id, file_id in stickers:
            results.append(
                InlineQueryResultCachedSticker(
                    id=file_unique_id,
                    sticker_file_id=file_id,
                )
            )
    else:
        return

    # logger.info(f"results: {results}")
    context.user_data["last_results"] = results
    if "fresh" in flags:
        await update.inline_query.answer(results, cache_time=0, auto_pagination=True)
    else:
        await update.inline_query.answer(results, auto_pagination=True)


async def chosen_inline_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_connection(context)
    cursor = conn.cursor()
    insert_chosen(
        cursor,
        update.chosen_inline_result.result_id,
        update.chosen_inline_result.from_user.id,
        datetime.now(),
    )
    conn.commit()


async def text_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorize(update.message.from_user.id, context):
        return

    if "sticker" in context.chat_data or "stickers" in context.chat_data:
        await update_alias_2(update, context)
    else:
        await help_command(update, context)


async def sticker_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorize(update.message.from_user.id, context):
        return

    status = context.chat_data.get("status")
    if status == "Add favorite" or status == "Delete favorite":
        await update_favorite(update, context)
    else:
        await update_alias_1(update, context)


async def update_alias_1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cursor = get_connection(context).cursor()

    logger.debug(f"file_unique_id: {update.message.sticker.file_unique_id}")
    result = search_sticker_by_unique_id(cursor, update.message.sticker.file_unique_id)

    if context.chat_data.get("status") == "Bulk update alias":
        # Bulk
        set_name = update.message.sticker.set_name
        sticker_set = await context.bot.get_sticker_set(set_name)
        context.chat_data["stickers"] = sticker_set.stickers

        await update.message.reply_text(f"Sticker set title: {sticker_set.title}")

        if result and result[-1]:
            await update.message.reply_text(f"Set alias is: {result[-1]}")

        context.chat_data["update_alias_placeholder"] = await update.message.reply_text(
            "New set alias is?"
        )
    else:
        # Single
        context.chat_data["sticker"] = update.message.sticker

        if result and result[-2]:
            await update.message.reply_text(f"Alias is: {result[-2]}")

        context.chat_data["update_alias_placeholder"] = await update.message.reply_text(
            "New alias is?"
        )

    # cancel button
    keyboard = []
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    context.chat_data["cancel_message"] = await update.message.reply_text(
        "Cancel this action?", reply_markup=reply_markup
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()
    await query.message.delete()

    await context.chat_data["update_alias_placeholder"].delete()
    context.chat_data.clear()


async def update_alias_2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_connection(context)
    cursor = conn.cursor()

    if context.chat_data.get("status") == "Bulk update alias":
        # Bulk
        stickers = context.chat_data["stickers"]
        set_alias = update.message.text
        for sticker in stickers:
            insert_or_update_sticker(
                cursor,
                sticker.file_unique_id,
                sticker.file_id,
                update.message.from_user.id,
                None,
                set_alias,
            )
    else:
        # Single
        sticker = context.chat_data["sticker"]
        alias = update.message.text
        insert_or_update_sticker(
            cursor,
            sticker.file_unique_id,
            sticker.file_id,
            update.message.from_user.id,
            alias,
            None,
        )
    conn.commit()

    await context.chat_data["cancel_message"].delete()

    context.chat_data.clear()
    await update.message.reply_text("Succeeded.")


def authorize(user_id, context: CallbackContext):
    if not context.bot_data.get("user_id"):
        cursor = get_connection(context).cursor()

        reuslts = select_all_user_id(cursor)
        context.bot_data["user_id"] = [x[0] for x in reuslts]

    if user_id in context.bot_data["user_id"]:
        return True
    else:
        logger.info(f"Unknow user: {user_id}")
        return False


def main() -> None:
    """Run the bot."""
    # Create the Updater and pass it your bot's token.
    updater = Application.builder().token(API_TOKEN).build()

    job_queue = updater.job_queue
    if config["UPDATE_TIME"]:
        update_time = time.fromisoformat(config["UPDATE_TIME"])
    else:
        update_time = time.fromisoformat("00:00:00")
    if config["TIME_ZONE"]:
        tz = timezone(config["TIME_ZONE"])
        update_time = update_time.replace(tzinfo=tz)
    job_queue.run_daily(callback_update_trending, update_time)

    # Get the application to register handlers
    # application = updater.application
    application = updater

    application.add_handler(CommandHandler("start", help_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("favorite", favorite_command))
    application.add_handler(CommandHandler("alias", alias_command))
    application.add_handler(CommandHandler("bulk", bulk_command))

    application.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel$"))
    application.add_handler(
        CallbackQueryHandler(favorite_callback, pattern=r"^favorite_group \d+ \d+$")
    )
    application.add_handler(CallbackQueryHandler(finish_callback, pattern=r"^finish$"))

    application.add_handler(InlineQueryHandler(inlinequery))
    application.add_handler(ChosenInlineResultHandler(chosen_inline_result))

    # noncommand
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_decision)
    )
    application.add_handler(MessageHandler(filters.Sticker.ALL, sticker_decision))

    # Start the Bot
    application.run_polling()

    # Block until the user presses Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT.
    updater.idle()


def initial_DB(cursor):
    cursor.execute(
        """CREATE TABLE user (
                user_id INTEGER NOT NULL PRIMARY KEY,
                nickname TEXT,
                admin INTEGER
                )"""
    )

    cursor.execute(
        """CREATE TABLE sticker (
                file_unique_id TEXT NOT NULL PRIMARY KEY,
                file_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                alias TEXT,
                set_alias TEXT,
                FOREIGN KEY (user_id)
                    REFERENCES user (user_id)
                        ON UPDATE CASCADE
                )"""
    )

    cursor.execute(
        """CREATE TABLE favorite (
                user_id INTEGER NOT NULL,
                file_unique_id TEXT NOT NULL,
                group_no INTEGER NOT NULL,
                PRIMARY KEY (user_id, file_unique_id)
                FOREIGN KEY (user_id)
                    REFERENCES user (user_id)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE,
                FOREIGN KEY (file_unique_id)
                    REFERENCES sticker (file_unique_id)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )"""
    )

    cursor.execute(
        """CREATE TABLE chosen (
                file_unique_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                chosen_time TIMESTAMP NOT NULL,
                PRIMARY KEY (file_unique_id, user_id, chosen_time)
                FOREIGN KEY (user_id)
                    REFERENCES user (user_id)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE,
                FOREIGN KEY (file_unique_id)
                    REFERENCES sticker (file_unique_id)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )"""
    )

    create_trending(cursor)


def create_trending(cursor, is_temp=False):
    table_name = "trending_tmp" if is_temp else "trending"
    cmd = f"""CREATE TABLE {table_name} (
                file_unique_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                score INTEGER NOT NULL,
                PRIMARY KEY (file_unique_id, user_id)
                FOREIGN KEY (user_id)
                    REFERENCES user (user_id)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE,
                FOREIGN KEY (file_unique_id)
                    REFERENCES sticker (file_unique_id)
                        ON DELETE CASCADE
                        ON UPDATE CASCADE
                )
    """
    cursor.execute(cmd)


# TABLE user
def select_admin_id(cursor):
    cursor.execute("SELECT user_id FROM user WHERE admin=1")
    return cursor.fetchone()


def select_all_user_id(cursor):
    cursor.execute("SELECT user_id FROM user")
    return cursor.fetchall()


# TABLE sticker
def insert_or_update_sticker(
    cursor, file_unique_id, file_id, user_id, alias, set_alias
):
    query = "INSERT INTO sticker VALUES(?,?,?,?,?) ON CONFLICT(file_unique_id) DO UPDATE SET user_id=?"
    data = (file_unique_id, file_id, user_id, alias, set_alias, user_id)
    if alias is not None:
        query += ", alias=?"
        data += (alias,)
    elif set_alias is not None:
        query += ", set_alias=?"
        data += (set_alias,)

    cursor.execute(query, data)


def search_sticker_by_unique_id(cursor, file_unique_id):
    cursor.execute("SELECT * FROM sticker WHERE file_unique_id=?", (file_unique_id,))
    return cursor.fetchone()


def search_sticker_by_alias(cursor, user_id, alias):
    cursor.execute(
        """
            SELECT sticker.file_unique_id, sticker.file_id FROM sticker
            LEFT OUTER JOIN trending
            ON trending.user_id=? AND sticker.file_unique_id = trending.file_unique_id
            WHERE alias like ?
            ORDER BY score DESC
            """,
        (
            user_id,
            f"%{alias}%",
        ),
    )
    return cursor.fetchall()


def search_sticker_by_set_alias(cursor, set_alias):
    cursor.execute(
        """
            SELECT sticker.file_unique_id, sticker.file_id FROM sticker
            WHERE set_alias like ?
            """,
        (f"%{set_alias}%",),
    )
    return cursor.fetchall()


def search_trending_sticker(cursor, user_id):
    cursor.execute(
        """
            SELECT sticker.file_unique_id, sticker.file_id FROM sticker
            LEFT OUTER JOIN trending
            ON trending.user_id=? AND sticker.file_unique_id = trending.file_unique_id
            WHERE score IS NOT NULL
            ORDER BY score DESC
            """,
        (user_id,),
    )
    return cursor.fetchall()


def search_all_alias(cursor):
    cursor.execute(
        "SELECT DISTINCT(alias) FROM sticker WHERE alias IS NOT NULL ORDER BY alias"
    )
    return cursor.fetchall()


def search_all_set_alias(cursor):
    cursor.execute(
        "SELECT DISTINCT(set_alias) FROM sticker WHERE set_alias IS NOT NULL ORDER BY set_alias"
    )
    return cursor.fetchall()


# TABLE favorite
def insert_favorite(cursor, user_id, file_unique_id, group_no):
    cursor.execute(
        " INSERT INTO favorite VALUES(?,?,?)", (user_id, file_unique_id, group_no)
    )
    return cursor.rowcount


def delete_favoirte(cursor, user_id, file_unique_id, group_no):
    cursor.execute(
        "DELETE FROM favorite WHERE user_id=? AND file_unique_id=? AND group_no=?",
        (user_id, file_unique_id, group_no),
    )
    return cursor.rowcount


def count_favorite_sticker(cursor, user_id, group_no):
    cursor.execute(
        "SELECT COUNT(*) FROM favorite WHERE user_id=? AND group_no=?",
        (user_id, group_no),
    )
    return cursor.fetchone()


def search_favorite_group_no(cursor, user_id, file_unique_id):
    cursor.execute(
        "SELECT group_no FROM favorite WHERE user_id=? AND file_unique_id=?",
        (user_id, file_unique_id),
    )
    return cursor.fetchone()


def search_sticker_by_favortie(cursor, user_id, group_no):
    cursor.execute(
        """
            SELECT sticker.file_unique_id, sticker.file_id
            FROM sticker
            INNER JOIN favorite ON sticker.file_unique_id = favorite.file_unique_id
            WHERE favorite.user_id=? and favorite.group_no=?""",
        (user_id, group_no),
    )
    return cursor.fetchall()


# TABLE chosen
def insert_chosen(cursor, file_unique_id, user_id, chosen_time):
    cursor.execute(
        "INSERT INTO chosen VALUES(?,?,?)", (file_unique_id, user_id, chosen_time)
    )


def search_chosen_recent(cursor, user_id, chosen_time):
    cursor.execute(
        "SELECT * FROM chosen WHERE user_id=? AND chosen_time>=? ORDER BY chosen_time",
        (user_id, chosen_time),
    )
    return cursor.fetchall()


# TABLE trending
def drop_trending(cursor):
    cursor.execute("DROP TABLE trending")


def alter_tmp_to_trending(cursor):
    cursor.execute("ALTER TABLE trending_tmp RENAME TO trending")


def insert_trending_tmp(cursor, file_unique_id, user_id, score):
    cursor.execute(
        "INSERT INTO trending_tmp VALUES(?,?,?)", (file_unique_id, user_id, score)
    )


if __name__ == "__main__":
    if not os.path.isfile(config["DB_FILE"]):
        conn = sqlite3.connect(config["DB_FILE"], detect_types=sqlite3.PARSE_DECLTYPES)
        cursor = conn.cursor()
        initial_DB(cursor)
        conn.commit()
        conn.close()

    main()
