import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import schedule
import telebot
import uvicorn
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from telebot import types
from telegram_bot_calendar import LSTEP, DetailedTelegramCalendar

load_dotenv()
bot = telebot.TeleBot(os.getenv("TELEGRAM_API_TOKEN"))
user_schedules = {}
values = None
value_new = None
flag = False
ind = None


def create_user_reminders_table(user_id):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f'''CREATE TABLE IF NOT EXISTS user_{user_id}
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  description TEXT,
                  date TEXT,
                  attachment_folder INTEGER DEFAULT 0,
                  done INTEGER DEFAULT 0,
                  period INTEGER DEFAULT 0,
                  periodic_time TEXT DEFAULT '0 0 0')''')
    conn.commit()
    conn.close()


def add_to_database(user_id, description, date, attachment_folder, period):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"INSERT INTO user_{user_id} (description, date, attachment_folder, period) VALUES (?, ?, ?, ?)",
              (description, date, attachment_folder, period))
    conn.commit()
    conn.close()


def get_user_reminders(user_id, done=False):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"SELECT * FROM user_{user_id} WHERE done = ?", (1 if done else 0,))
    reminders = c.fetchall()
    conn.close()
    return reminders


def update_attachment_folder(user_id, attachment_folder):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"UPDATE user_{user_id} SET attachment_folder = ? WHERE id = (SELECT MAX(id) FROM user_{user_id})",
              (attachment_folder,))
    conn.commit()
    conn.close()


def send_main_menu(message):
    keyboard = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    current_button = types.KeyboardButton('Current tasks')
    completed_button = types.KeyboardButton('Completed tasks')
    keyboard.add(current_button, completed_button)
    bot.send_message(message.chat.id, "What should be done?", reply_markup=keyboard)


@bot.message_handler(func=lambda message: message.text == 'Current tasks')
def show_current_reminders(message):
    user_id = message.from_user.id
    reminders = get_user_reminders(user_id, done=False)
    if reminders:
        for reminder in reminders:
            keyboard = types.InlineKeyboardMarkup()
            keyboard.row(
                types.InlineKeyboardButton("Change description", callback_data=f"edit_description_{reminder[0]}"),
                types.InlineKeyboardButton("Change date", callback_data=f"edit_date_{reminder[0]}"),
            )
            keyboard.row(
                types.InlineKeyboardButton("Change files", callback_data=f"edit_files_{reminder[0]}"),
                types.InlineKeyboardButton("Delete", callback_data=f"delete_{reminder[0]}"),
                types.InlineKeyboardButton("Done", callback_data=f"complete_{reminder[0]}")
            )
            if reminder[5] and reminder[6] != '0 0 0':
                keyboard.row(
                    types.InlineKeyboardButton("Change frequency", callback_data=f"edit_period_{reminder[0]}"))
                bot.send_message(message.chat.id,
                                 f"Periodic reminder. Description: {reminder[1]}, Date: {reminder[2]}. Period: {reminder[6]}",
                                 reply_markup=keyboard)
            elif reminder[5]:
                bot.send_message(message.chat.id,
                                 f"Current periodic reminder. Description: {reminder[1]}, Date: {reminder[2]}.",
                                 reply_markup=keyboard)
            else:
                bot.send_message(message.chat.id, f"Description: {reminder[1]}, Date: {reminder[2]}",
                                 reply_markup=keyboard)
    else:
        bot.send_message(message.chat.id, "No current tasks.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_period_"))
def handle_edit_period_query(query):
    user_id = query.from_user.id
    reminder_id = int(query.data.split("_")[2])
    msg = bot.send_message(query.message.chat.id,
                           "Specify the new reminder frequency in the format [days hours minutes]:")
    bot.register_next_step_handler(msg, lambda m: ask_periodic_interval(m, reminder_id, True))


@bot.callback_query_handler(func=lambda call: call.data.startswith('edit_files'))
def edit_files_handler(call):
    user_id = call.from_user.id
    reminder_id = call.data.split('_')[2]
    chat_id = call.message.chat.id

    attachment_table_name = f"attachments_{user_id}_{reminder_id}"
    try:
        files_info = get_all_files_info_from_database(attachment_table_name)
    except Exception:
        bot.send_message(chat_id, "No files to edit.")
        return

    if not files_info:
        bot.send_message(chat_id, "No files to edit.")
        return
    keyboard = types.InlineKeyboardMarkup()
    for file_id, file_path in files_info:
        keyboard.row(
            types.InlineKeyboardButton(f"Delete {file_path}", callback_data=f"file_delete_{file_id}_{reminder_id}"),
        )
    keyboard.row(types.InlineKeyboardButton("Add attachment", callback_data=f"add_attachment_{reminder_id}"))

    bot.send_message(chat_id, "Select a file to edit:", reply_markup=keyboard)


def get_all_files_info_from_database(table_name):
    conn = sqlite3.connect("reminders.db")
    c = conn.cursor()
    c.execute(f'''
        SELECT file_path, file_name FROM {table_name}
    ''')
    file_info = c.fetchall()
    conn.close()

    return file_info


@bot.callback_query_handler(func=lambda call: call.data.startswith('file_delete'))
def delete_file_handler(call):
    user_id = call.from_user.id
    reminder_id = call.data.split('_')[-1]
    file_id = call.data[12:-len(reminder_id) - 1]
    if delete_file_from_database(user_id, file_id, reminder_id):
        bot.send_message(call.message.chat.id, f"File with ID {file_id} successfully deleted.")
    else:
        bot.send_message(call.message.chat.id, f"Error when deleting file with ID {file_id}.")
    delete_file_from_drive(file_id)


def delete_file_from_database(user_id, file_id, reminder_id):
    try:
        conn = sqlite3.connect('reminders.db')
        c = conn.cursor()
        c.execute(f'''
            DELETE FROM attachments_{user_id}_{reminder_id} WHERE file_path = ?
        ''', (file_id,))
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as e:
        print("Error when deleting a file from the database:", e)
        return False


@bot.callback_query_handler(func=lambda call: call.data.startswith('add_attachment'))
def add_attachment_handler(call):
    global flag
    global ind
    flag = True
    ind = call.data.split('_')[2]
    bot.send_message(call.message.chat.id, "Attach a new file, then enter 'end'")


@bot.callback_query_handler(lambda query: query.data.startswith("complete_"))
def handle_complete_query(query):
    user_id = query.from_user.id
    reminder_id = int(query.data.split("_")[1])
    mark_as(user_id, reminder_id)
    bot.send_message(query.message.chat.id, "The reminder is marked as completed.")


def mark_as(user_id, reminder_id, value=1):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"UPDATE user_{user_id} SET done = ? WHERE id = ?", (value, reminder_id))
    conn.commit()
    conn.close()


@bot.callback_query_handler(lambda query: query.data.startswith("delete_"))
def handle_delete_query(query):
    user_id = query.from_user.id
    reminder_id = int(query.data.split("_")[1])
    delete_reminder(user_id, reminder_id)
    bot.send_message(query.message.chat.id, "Reminder deleted.")


def delete_reminder(user_id, reminder_id):
    try:
        conn = sqlite3.connect('reminders.db')
        c = conn.cursor()

        attachment_table_name = f"attachments_{user_id}_{reminder_id}"

        c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (attachment_table_name,))
        table_exists = c.fetchone()

        if table_exists:
            c.execute(f"SELECT file_path FROM {attachment_table_name}")
            file_ids = c.fetchall()

            for file_id in file_ids:
                delete_file_from_drive(file_id[0])

            c.execute(f"DROP TABLE {attachment_table_name}")

        c.execute(f"DELETE FROM user_{user_id} WHERE id = ?", (reminder_id,))

        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as e:
        print("Error when deleting a reminder from the database:", e)
        return False


@bot.callback_query_handler(lambda query: query.data.startswith("edit_description_"))
def handle_edit_description_query(query):
    user_id = query.from_user.id
    reminder_id = int(query.data.split("_")[2])
    msg = bot.send_message(query.message.chat.id, "Enter a new description:")
    bot.register_next_step_handler(msg, lambda m: process_edit_description(m, user_id, reminder_id))


def process_edit_description(message, user_id, reminder_id):
    new_description = message.text
    update_description(user_id, reminder_id, new_description)
    bot.send_message(message.chat.id, "Description successfully updated.")


def update_description(user_id, reminder_id, new_description):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"UPDATE user_{user_id} SET description = ? WHERE id = ?", (new_description, reminder_id))
    conn.commit()
    conn.close()


@bot.callback_query_handler(lambda query: query.data.startswith("edit_date_"))
def handle_edit_date_query(query):
    user_id = query.from_user.id
    reminder_id = int(query.data.split("_")[2])
    calendar, step = DetailedTelegramCalendar().build()
    msg = bot.send_message(query.message.chat.id, "Select a new date:", reply_markup=calendar)
    process_edit_date(msg, user_id, reminder_id)


def process_edit_date(message, user_id, reminder_id):
    global value_new
    msg = bot.send_message(message.chat.id, "Now enter the new time (in HH:MM format):")
    bot.register_next_step_handler(msg, lambda m: process_edit_date1(m, user_id, reminder_id, value_new))


def process_edit_date1(message, user_id, reminder_id, value_new):
    if not validate_time_format(message.text):
        msg = bot.send_message(message.chat.id, "Invalid time format. Please enter the time in HH:MM format.")
        bot.register_next_step_handler(msg, process_edit_date1, user_id, reminder_id, value_new)
    else:
        process_edit_time(message, user_id, reminder_id, value_new)


def process_edit_time(message, user_id, reminder_id, new_date):
    new_time = message.text
    new_datetime = f"{new_date} {new_time}"
    update_date(user_id, reminder_id, new_datetime)
    msg = bot.send_message(message.chat.id, "The date and time have been successfully updated.")
    process_return(msg)


def update_date(user_id, reminder_id, new_date):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"UPDATE user_{user_id} SET date = ? WHERE id = ?", (new_date, reminder_id))
    conn.commit()
    conn.close()


@bot.message_handler(func=lambda message: message.text == 'Completed tasks')
def show_completed_reminders(message):
    user_id = message.from_user.id
    reminders = get_user_reminders(user_id, done=True)
    if reminders:
        reminders_sorted = sorted(reminders, key=lambda x: datetime.strptime(x[2], '%Y-%m-%d %H:%M'), reverse=True)
        for reminder in reminders_sorted:
            keyboard = types.InlineKeyboardMarkup()
            keyboard.row(
                types.InlineKeyboardButton("Return with date change", callback_data=f"return_{reminder[0]}")
            )
            if reminder[5] and reminder[6] != '0 00:00':
                bot.send_message(message.chat.id,
                                 f"Periodic reminder. Description: {reminder[1]}, Date: {reminder[2]}. Period: {reminder[6]}",
                                 reply_markup=keyboard)
            elif reminder[5]:
                bot.send_message(message.chat.id,
                                 f"current periodic reminder. Description: {reminder[1]}, Date: {reminder[2]}.",
                                 reply_markup=keyboard)
            else:
                bot.send_message(message.chat.id, f"Description: {reminder[1]}, Date: {reminder[2]}",
                                 reply_markup=keyboard)
    else:
        bot.send_message(message.chat.id, "You have no completed tasks yet.")


@bot.callback_query_handler(lambda query: query.data.startswith("return_"))
def handle_return_query(query):
    user_id = query.from_user.id
    reminder_id = int(query.data.split("_")[1])
    calendar, step = DetailedTelegramCalendar().build()
    msg = bot.send_message(query.message.chat.id, "Select a new date:", reply_markup=calendar)
    mark_as(user_id, reminder_id, 0)
    process_edit_date(msg, user_id, reminder_id)


def process_return(message):
    bot.send_message(message.chat.id, "The reminder was successfully returned with a new date.")


@bot.message_handler(commands=['start'])
def start(message):
    user = message.from_user
    create_user_reminders_table(user.id)
    welcome_message = (
        f"testHello, {user.first_name}!\n"
        "I'm test2ReminderBot. I will help you not to forget the most important things and remind you of upcoming matters.\n"
        "Message me /create to create a reminder.\n"
    )
    add_user_schedule(user.id, 1)
    bot.send_message(message.chat.id, welcome_message)
    send_main_menu(message)


@bot.callback_query_handler(func=DetailedTelegramCalendar.func())
def cal(c):
    global values
    global value_new
    if c.message.text.startswith('When'):
        values = c.message.text[5:-1]
    result, key, step = DetailedTelegramCalendar().process(c.data)
    if not result and key:
        bot.edit_message_text(f"Select {LSTEP[step]}",
                              c.message.chat.id,
                              c.message.message_id,
                              reply_markup=key)
    elif result:
        bot.edit_message_text(f"You selected {result}",
                              c.message.chat.id,
                              c.message.message_id)
        if values is not None:
            msg = bot.send_message(c.message.chat.id, f"Now select the time in HH:MM format")
            bot.register_next_step_handler(msg, set_time, result, values)
        else:
            value_new = result


def validate_time_format(time_str):
    try:
        datetime.strptime(time_str, "%H:%M")
        return True
    except ValueError:
        return False


def set_time(message, chosen_date, text):
    try:
        chat_id = message.chat.id
        time_chosen = message.text
        if not validate_time_format(time_chosen):
            msg = bot.send_message(chat_id, "Unknown time. Enter in HH:MM format.")
            bot.register_next_step_handler(msg, set_time, chosen_date, text)
            return

        reminder_time = f"{chosen_date} {time_chosen}"
        msg = bot.send_message(chat_id,
                               f"Time selected {time_chosen}. A reminder will be sent in {reminder_time}")
        set_date(msg, text, reminder_time)
    except Exception:
        bot.send_message(message.chat.id, 'Timing error. Try again.')


@bot.message_handler(commands=['create'])
def add_reminder(message):
    global values
    values = None
    msg = bot.send_message(message.chat.id, "What needs to be reminded?")
    bot.register_next_step_handler(msg, set_description)


def set_description(message):
    description = message.text
    chat_id = message.chat.id
    calendar, step = DetailedTelegramCalendar().build()

    bot.send_message(chat_id, f"When {description}:", reply_markup=calendar)


def set_date(message, description, result):
    global values
    values = None
    if description is not None:
        try:
            chat_id = message.chat.id
            markup = telebot.types.InlineKeyboardMarkup()
            markup.row(telebot.types.InlineKeyboardButton("Yes", callback_data="periodic_yes"),
                       telebot.types.InlineKeyboardButton("No", callback_data="periodic_no"))
            bot.send_message(chat_id, f"Reminder '{description}' set to {result}."
                                      "Does it need to be repeated?", reply_markup=markup)
            add_to_database(message.chat.id, description, result, 0, 0)
        except Exception as e:
            bot.send_message(message.chat.id, 'Date selection error. Try again.')


@bot.callback_query_handler(func=lambda call: call.data == 'periodic_yes')
def handle_periodic_yes(call):
    chat_id = call.message.chat.id
    bot.send_message(chat_id, "Specify how often to remind (in the format [days hours minutes]).")
    bot.register_next_step_handler(call.message, ask_periodic_interval)


def ask_periodic_interval(message, id=None, only_edit=False):
    chat_id = message.chat.id
    try:
        pattern = r'^\d+ \d+ \d+$'
        if not re.match(pattern, message.text):
            raise ValueError
        days, hour, minute = map(int, message.text.split())
        if all(map(lambda x: x == 0, [days, hour, minute])):
            raise ValueError
        period = timedelta(hours=hour, minutes=minute, days=days)
        bot.send_message(chat_id, f"Reminders will come at intervals {period}.")
        if id is None:
            reminder_id = get_latest_reminder_id(chat_id)
        else:
            reminder_id = id
        update_periodic_info(chat_id, reminder_id, message.text, 1)
        if not only_edit:
            ask_attachment(message, True)
    except ValueError as e:
        msg = bot.send_message(chat_id, "Unknown period. Enter in the format [days hours minutes].")
        bot.register_next_step_handler(msg, ask_periodic_interval)


def update_periodic_info(user_id, reminder_id, periodic_time, period):
    try:
        conn = sqlite3.connect('reminders.db')
        c = conn.cursor()
        c.execute(f"UPDATE user_{user_id} SET period = ?, periodic_time = ? WHERE id = ?",
                  (period, periodic_time, reminder_id))
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as e:
        print("Error when updating periodic information in the database:", e)
        return False


@bot.callback_query_handler(func=lambda call: call.data == 'periodic_no')
def handle_periodic_no(call):
    chat_id = call.message.chat.id
    bot.send_message(chat_id, "The reminder will be one-time.")
    ask_attachment(call.message)


@bot.message_handler(func=lambda message: message.text.lower() == 'end', content_types=['text'])
def handle_upload(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Files are attached")


def ask_attachment(message, period=False):
    chat_id = message.chat.id
    markup = telebot.types.InlineKeyboardMarkup()
    if period:
        markup.row(telebot.types.InlineKeyboardButton("Yes", callback_data="attach_yes_period"),
                   telebot.types.InlineKeyboardButton("No", callback_data="attach_no_period"))
    else:
        markup.row(telebot.types.InlineKeyboardButton("Yes", callback_data="attach_yes"),
                   telebot.types.InlineKeyboardButton("No", callback_data="attach_no"))
    bot.send_message(chat_id, "Do I need to attach files to a reminder?", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith('attach'))
def handle_attachment(call):
    global flag
    chat_id = call.message.chat.id
    if call.data.startswith('attach_yes'):
        reminder_id = get_latest_reminder_id(chat_id)
        create_attachments_table(chat_id, reminder_id)
        flag = True
        msg = bot.send_message(chat_id, "Attach the required files, then enter 'end'")
        update_attachment_folder(chat_id, 1)
        if msg.text == 'upload':
            flag = False
    elif call.data.startswith('attach_no'):
        reminder = get_latest_reminder_id(chat_id)
        if reminder:
            reminder_info = get_reminder_info(chat_id, reminder)
            if reminder_info:
                if reminder_info[5]:
                    new_date = reminder_info[2]
                    add_to_database(chat_id, reminder_info[1], new_date, reminder_info[3], reminder_info[5])
        bot.send_message(chat_id, "Reminder created successfully!")

    bot.edit_message_reply_markup(chat_id=chat_id, message_id=call.message.message_id, reply_markup=None)


def get_last_reminder_id(chat_id):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f'''
        SELECT id FROM user_{chat_id} ORDER BY id DESC LIMIT 1
    ''')
    last_reminder_id = c.fetchone()[0]
    conn.close()
    return last_reminder_id


def create_attachments_table(user_id, reminder_id):
    table_name = f"attachments_{user_id}_{reminder_id}"
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f'''
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/drive.file"]


def connect_to_drive():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    service = build("drive", "v3", credentials=creds)
    return service


def upload_file_to_drive(service, file_path):
    file_metadata = {"name": os.path.basename(file_path)}
    media = MediaFileUpload(file_path, resumable=True)
    file = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )
    return file.get("id")


def save_file_info_to_database(user_id, reminder_id, file_path, file_name):
    table_name = f"attachments_{user_id}_{reminder_id}"
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f'''
        INSERT INTO {table_name} (file_path, file_name) VALUES (?, ?)
    ''', (file_path, file_name))
    conn.commit()
    conn.close()


'''@bot.message_handler(content_types=['audio', 'video', 'document'])
def handle_document(message):
    global flag
    global ind
    if flag:
        user_id = message.from_user.id
        if ind is not None:
            reminder_id = ind
        else:
            reminder_id = get_latest_reminder_id(user_id)
        service = connect_to_drive()
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        file_path = f"{user_id}_{message.document.file_name}"
        with open(file_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        file_id = upload_file_to_drive(service, file_path)
        save_file_info_to_database(user_id, reminder_id, file_id, f"{message.document.file_name}")
        os.remove(file_path)'''


@bot.message_handler(content_types=['audio', 'video', 'document', 'photo'])
def handle_document(message):
    global flag
    global ind
    if flag:
        user_id = message.from_user.id
        if ind is not None:
            reminder_id = ind
        else:
            reminder_id = get_latest_reminder_id(user_id)
        service = connect_to_drive()

        if message.document:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            file_path = f"{user_id}_{message.document.file_name}"
            with open(file_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            file_id = upload_file_to_drive(service, file_path)
            save_file_info_to_database(user_id, reminder_id, file_id, message.document.file_name)
            os.remove(file_path)

        elif message.photo:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            file_path = f"{user_id}_{message.photo[-1].file_id}.jpg"
            with open(file_path, 'wb') as new_file:
                new_file.write(downloaded_file)
            file_id = upload_file_to_drive(service, file_path)
            save_file_info_to_database(user_id, reminder_id, file_id, f"photo_{message.photo[-1].file_id}.jpg")
            os.remove(file_path)


def get_latest_reminder_id(user_id):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute("SELECT id FROM user_{} ORDER BY id DESC LIMIT 1".format(user_id))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    else:
        return None


def download_file_from_drive(service, file_id, save_path):
    request = service.files().get_media(fileId=file_id)
    fh = open(save_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    fh.close()


def delete_file_from_drive(file_id):
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    service = build('drive', 'v3', credentials=creds)

    try:
        service.files().delete(fileId=file_id).execute()
        print("The file was successfully deleted from Google Drive.")
    except Exception as e:
        print("Error when deleting a file from Google Drive:", e)


@bot.message_handler(func=lambda message: message.text.lower() == 'end')
def end_command_handler(message):
    global flag
    global ind
    ind = None
    flag = False
    user_id = message.chat.id
    reminder = get_latest_reminder_id(user_id)
    if reminder:
        reminder_info = get_reminder_info(user_id, reminder)
        if reminder_info:
            if reminder_info[5]:
                new_date = reminder_info[2]
                add_to_database(user_id, reminder_info[1], new_date, reminder_info[3], reminder_info[5])
                if reminder_info[3] == 1:
                    last_attachment_table_name = f"attachments_{user_id}_{reminder}"
                    new_attachment_table_name = f"attachments_{user_id}_{reminder + 1}"
                    copy_attachments(user_id, last_attachment_table_name, new_attachment_table_name)


def copy_attachments(user_id, last_attachment_table_name, new_attachment_table_name):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"CREATE TABLE IF NOT EXISTS {new_attachment_table_name} AS SELECT * FROM {last_attachment_table_name}")
    conn.commit()
    conn.close()


def get_reminder_info(user_id, reminder_id):
    conn = sqlite3.connect('reminders.db')
    c = conn.cursor()
    c.execute(f"SELECT * FROM user_{user_id} WHERE id = ?", (reminder_id,))
    reminder_info = c.fetchone()
    conn.close()
    return reminder_info


def check_reminders(user_id):
    reminders = get_user_reminders(user_id)
    current_time = datetime.now()
    for reminder in reminders:
        reminder_time = datetime.strptime(reminder[2], "%Y-%m-%d %H:%M")
        if current_time >= reminder_time:
            message = f"Reminder: {reminder[1]}"
            if reminder[5] and reminder[6] != '0 0 0':
                days, hour, minute = map(int, reminder[6].split())
                period = timedelta(hours=hour, minutes=minute, days=days)
                time = (datetime.strptime(reminder[2], "%Y-%m-%d %H:%M") + period).strftime("%Y-%m-%d %H:%M")
                update_date(user_id, reminder[0], time)
                add_to_database(user_id, reminder[1], time, reminder[3], reminder[5])
                if reminder[3] == 1:
                    last_attachment_table_name = f"attachments_{user_id}_{reminder[0]}"
                    new_attachment_table_name = f"attachments_{user_id}_{reminder[0] + 1}"
                    copy_attachments(user_id, last_attachment_table_name, new_attachment_table_name)
                continue
            attachment_table_name = f"attachments_{user_id}_{reminder[0]}"
            if reminder[3]:
                files_info = get_all_files_info_from_database(attachment_table_name)
                files = []
                if files_info:

                    message += "\nAttachments:"
                    for file_info in files_info:
                        file_id, save_path = file_info
                        files.append([file_id, save_path])
                        message += f"\n{save_path}"

                bot.send_message(user_id, message)
                for el in files:
                    service = connect_to_drive()

                    download_file_from_drive(service, el[0], el[1])
                    with open(el[1], "rb") as file:
                        bot.send_document(user_id, file)

                    os.remove(el[1])
            else:
                bot.send_message(user_id, message)
            mark_as(user_id, reminder[0])


def add_user_schedule(user_id, interval_minutes):
    user_schedules[user_id] = schedule.every(interval_minutes).minutes.do(check_reminders, user_id)


# def start_check_reminders():
#     while True:
#         schedule.run_pending()
#         time.sleep(30)


# def main():
#     reminder_thread = threading.Thread(target=start_check_reminders)
#     reminder_thread.start()
#     bot.polling()

def start_bot_polling():
    bot.polling()


if __name__ == '__main__':
    bot_thread = threading.Thread(target=start_bot_polling)
    bot_thread.start()

    uvicorn.run('server:app', host='0.0.0.0', port=5000, reload=True)

