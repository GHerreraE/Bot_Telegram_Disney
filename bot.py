import os
import socket
import imaplib
import email
import re
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

import colorama
from colorama import Fore, Style
import logging

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# =============================================================================
# 1. CONFIGURACIÓN INICIAL
# =============================================================================

IMAP_HOST = "mail.privateemail.com"

def load_email_accounts(filename='admin_imap_pass.txt'):
    """
    Cada línea: 'correo@dominio.com|password'
    """
    accounts = []
    if not os.path.exists(filename):
        raise FileNotFoundError(f"No se encontró el archivo {filename}")

    with open(filename, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if '|' not in line:
                continue
            email_str, password_str = line.split("|", 1)
            accounts.append((email_str.strip(), password_str.strip()))
    return accounts

EMAIL_ACCOUNTS = load_email_accounts()

def load_admin_ids(filename='admin_ids.txt'):
    admin_ids = []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.isdigit():
                    admin_ids.append(int(line))
    except FileNotFoundError:
        pass
    return admin_ids

ADMIN_IDS = load_admin_ids()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

USERS_DB_FILE = 'users_db.txt'
LOGS_FOLDER = "logs"
if not os.path.exists(LOGS_FOLDER):
    os.makedirs(LOGS_FOLDER)

with open('token.txt', 'r', encoding='utf-8') as token_file:
    TELEGRAM_BOT_TOKEN = token_file.read().strip()

PHONE_NUMBER = ""
if os.path.exists('help_phone.txt'):
    with open('help_phone.txt', 'r', encoding='utf-8') as phone_file:
        PHONE_NUMBER = phone_file.read().strip()

HELP_TEXT = (
    "ℹ️ *Ayuda del Bot*\n\n"
    "Este bot te permite obtener códigos de *Disney+* o *Netflix*, "
    "si tienes permiso sobre el correo. Y, para extraer códigos, "
    "debes contar con un permiso especial (o ser admin).\n\n"
    "1. Pulsa un botón en el menú principal.\n"
    "2. Ingresa tu correo.\n"
    "3. Te enviaremos el código o link si lo encontramos (y tienes permiso).\n\n"
    f"Si necesitas más ayuda, contáctanos por WhatsApp: {PHONE_NUMBER} 💬"
)

# =============================================================================
# 2. LOGS CON COLORES
# =============================================================================

class ColorfulFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.GREEN,
        logging.INFO: Fore.CYAN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }

    def format(self, record: logging.LogRecord) -> str:
        log_color = self.LEVEL_COLORS.get(record.levelno, Fore.WHITE)
        message = super().format(record)
        return f"{log_color}{message}{Style.RESET_ALL}"

def user_log(user_id: int, message: str):
    log_file = os.path.join(LOGS_FOLDER, f"{user_id}.txt")
    with open(log_file, "a", encoding='utf-8') as f:
        f.write(f"{datetime.now()}: {message}\n")

# =============================================================================
# 3. BASE DE DATOS DE USUARIOS (ACCESO A CORREOS)
# =============================================================================

def load_users():
    """
    Retorna un dict { user_id: { 'email': date or None, ...}, ...}
    """
    users_dict = {}
    if not os.path.exists(USERS_DB_FILE):
        return users_dict

    with open(USERS_DB_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 1:
                continue
            try:
                uid = int(parts[0])
            except ValueError:
                continue

            users_dict[uid] = {}
            for item in parts[1:]:
                if ':' in item:
                    mail_part, date_str = item.split(':', 1)
                    mail_part = mail_part.lower()
                    try:
                        exp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        exp_date = None
                    users_dict[uid][mail_part] = exp_date
                else:
                    users_dict[uid][item.lower()] = None
    return users_dict

def save_users(users_dict):
    with open(USERS_DB_FILE, 'w', encoding='utf-8') as f:
        for uid, emails_dict in users_dict.items():
            if not emails_dict:
                f.write(str(uid) + "\n")
                continue

            items = []
            for mail, exp_date in emails_dict.items():
                if exp_date is None:
                    items.append(mail)
                else:
                    items.append(f"{mail}:{exp_date.isoformat()}")
            line = f"{uid} {' '.join(items)}"
            f.write(line + "\n")

def user_has_valid_access(user_id: int, email_address: str) -> bool:
    if is_admin(user_id):
        return True

    users_dict = load_users()
    if user_id not in users_dict:
        return False

    mail = email_address.lower()
    if mail not in users_dict[user_id]:
        return False

    exp_date = users_dict[user_id][mail]
    if exp_date is None:
        return True

    today = datetime.now().date()
    return today <= exp_date

# =============================================================================
# 4. BASE DE DATOS DE PERMISO DE CÓDIGOS
# =============================================================================

CODE_ACCESS_FILE = "code_access_db.txt"

def load_code_access():
    code_dict = {}
    if not os.path.exists(CODE_ACCESS_FILE):
        return code_dict

    with open(CODE_ACCESS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                uid = int(parts[0])
            except ValueError:
                continue
            date_str = parts[1]
            if date_str.lower() == "none":
                code_dict[uid] = None
            else:
                try:
                    code_dict[uid] = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    code_dict[uid] = None
    return code_dict

def save_code_access(code_dict):
    with open(CODE_ACCESS_FILE, 'w', encoding='utf-8') as f:
        for uid, exp_date in code_dict.items():
            if exp_date is None:
                f.write(f"{uid} None\n")
            else:
                f.write(f"{uid} {exp_date.isoformat()}\n")

def user_has_code_permission(user_id: int) -> bool:
    if is_admin(user_id):
        return True

    code_dict = load_code_access()
    if user_id not in code_dict:
        return False

    exp_date = code_dict[user_id]
    if exp_date is None:
        return True
    today = datetime.now().date()
    return today <= exp_date

# =============================================================================
# 5. FUNCIONES PARA DISNEY+ Y NETFLIX
# =============================================================================

def get_disney_code(requested_email: str):
    socket.setdefaulttimeout(10)
    for (acc_email, acc_password) in EMAIL_ACCOUNTS:
        try:
            server = imaplib.IMAP4_SSL(IMAP_HOST)
            server.login(acc_email, acc_password)
            server.select("INBOX")

            status, messages = server.search(
                None,
                '(OR FROM "disneyplus@mail.disneyplus.com" FROM "disneyplus@mail2.disneyplus.com")'
            )
            if status != "OK" or not messages or not messages[0]:
                server.logout()
                continue

            email_ids = messages[0].split()
            for email_id in reversed(email_ids):
                status_msg, msg_data = server.fetch(email_id, "(RFC822)")
                if status_msg != "OK":
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg_obj = email.message_from_bytes(response_part[1])

                        recipients = []
                        for header_key, header_value in msg_obj.items():
                            if header_key.lower() in ["to", "cc", "bcc", "delivered-to", "x-original-to"]:
                                if header_value:
                                    recipients.append(header_value.lower())

                        if requested_email.lower() not in "\n".join(recipients):
                            continue

                        date_header = msg_obj["Date"]
                        parsed_date = email.utils.parsedate_to_datetime(date_header).astimezone(timezone.utc)
                        now = datetime.now(timezone.utc)
                        diff = now - parsed_date
                        total_minutes = int(diff.total_seconds() // 60)

                        code = extract_6_digit_code(msg_obj)
                        if code:
                            server.logout()
                            return code, total_minutes

            server.logout()
        except Exception as e:
            logging.error(f"Error con la cuenta {acc_email} al buscar Disney+: {e}")

    return None, None

def extract_6_digit_code(msg_obj):
    regex_6 = r'\b\d{6}\b'
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            ctype = part.get_content_type()
            if ctype == "text/html":
                html_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                text = BeautifulSoup(html_content, "html.parser").get_text()
                match = re.search(regex_6, text)
                if match:
                    return match.group(0)
            elif ctype == "text/plain":
                plain_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                match = re.search(regex_6, plain_content)
                if match:
                    return match.group(0)
    else:
        ctype = msg_obj.get_content_type()
        payload = msg_obj.get_payload(decode=True).decode('utf-8', errors='ignore')
        if ctype == "text/html":
            text = BeautifulSoup(payload, "html.parser").get_text()
            match = re.search(regex_6, text)
            if match:
                return match.group(0)
        elif ctype == "text/plain":
            match = re.search(regex_6, payload)
            if match:
                return match.group(0)
    return None

def get_netflix_reset_link(requested_email: str):
    return _search_netflix_email(requested_email, _parse_netflix_link)

def get_netflix_access_code(requested_email: str):
    return _search_netflix_email(requested_email, _parse_netflix_code)

def get_netflix_country_info(requested_email: str):
    return _search_netflix_email(requested_email, _parse_netflix_country)

def get_netflix_temporary_access_link(requested_email: str):
    return _search_netflix_email(requested_email, _parse_netflix_temporary_link)

def get_netflix_update_household_link(requested_email: str):
    return _search_netflix_email(requested_email, _parse_netflix_update_household_link)

def _search_netflix_email(requested_email: str, parse_function):
    socket.setdefaulttimeout(10)
    for (acc_email, acc_password) in EMAIL_ACCOUNTS:
        try:
            server = imaplib.IMAP4_SSL(IMAP_HOST)
            server.login(acc_email, acc_password)
            server.select("INBOX")

            status, messages = server.search(
                None,
                '(OR FROM "info@account.netflix.com" FROM "no-reply@netflix.com")'
            )
            if status != "OK" or not messages or not messages[0]:
                server.logout()
                continue

            email_ids = messages[0].split()
            for email_id in reversed(email_ids):
                status_msg, msg_data = server.fetch(email_id, "(RFC822)")
                if status_msg != "OK":
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg_obj = email.message_from_bytes(response_part[1])

                        recipients = []
                        for header_key, header_value in msg_obj.items():
                            if header_key.lower() in ["to", "cc", "bcc", "delivered-to", "x-original-to"]:
                                if header_value:
                                    recipients.append(header_value.lower())

                        if requested_email.lower() not in "\n".join(recipients):
                            continue

                        date_header = msg_obj["Date"]
                        parsed_date = email.utils.parsedate_to_datetime(date_header).astimezone(timezone.utc)
                        now = datetime.now(timezone.utc)
                        diff = now - parsed_date
                        total_minutes = int(diff.total_seconds() // 60)

                        extracted_value = parse_function(msg_obj)
                        if extracted_value:
                            server.logout()
                            return extracted_value, total_minutes

            server.logout()
        except Exception as e:
            logging.error(f"Error con la cuenta {acc_email} al buscar Netflix: {e}")

    return None, None

def _parse_netflix_link(msg_obj):
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            ctype = part.get_content_type()
            if ctype in ["text/html", "text/plain"]:
                text = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                link = _find_reset_link_in_text(text, ctype)
                if link:
                    return link
    else:
        ctype = msg_obj.get_content_type()
        text = msg_obj.get_payload(decode=True).decode('utf-8', errors='ignore')
        link = _find_reset_link_in_text(text, ctype)
        if link:
            return link
    return None

def _find_reset_link_in_text(content, ctype):
    if ctype == "text/html":
        soup = BeautifulSoup(content, "html.parser")
        link_tag = soup.find("a", string=re.compile(r"restablecer contraseña", re.IGNORECASE))
        if link_tag and link_tag.get("href"):
            return link_tag["href"]
        for a in soup.find_all("a", href=True):
            if "password?" in a["href"]:
                return a["href"]
    else:
        match = re.search(r'(https?://[^\s"\]\)]+password\?[^"\s\]\)]*)', content)
        if match:
            return match.group(1)
    return None

def _parse_netflix_code(msg_obj):
    regex_4 = r'\b\d{4}\b'
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            ctype = part.get_content_type()
            if ctype == "text/html":
                html_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                code = re.search(regex_4, BeautifulSoup(html_content, "html.parser").get_text())
                if code:
                    return code.group(0)
            elif ctype == "text/plain":
                text_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                code = re.search(regex_4, text_content)
                if code:
                    return code.group(0)
    else:
        ctype = msg_obj.get_content_type()
        text = msg_obj.get_payload(decode=True).decode('utf-8', errors='ignore')
        if ctype == "text/html":
            code = re.search(regex_4, BeautifulSoup(text, "html.parser").get_text())
            if code:
                return code.group(0)
        else:
            code = re.search(regex_4, text)
            if code:
                return code.group(0)
    return None

def _parse_netflix_country(msg_obj):
    full_text = _get_full_text(msg_obj)
    src_val = _extract_src_value(full_text)
    if not src_val:
        return None
    lang, country = _parse_language_country(src_val)
    if country:
        return (lang, country)
    return None

def _get_full_text(msg_obj):
    texts = []
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            ctype = part.get_content_type()
            if ctype in ["text/plain", "text/html"]:
                content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                texts.append(content)
    else:
        ctype = msg_obj.get_content_type()
        if ctype in ["text/plain", "text/html"]:
            content = msg_obj.get_payload(decode=True).decode('utf-8', errors='ignore')
            texts.append(content)
    return "\n".join(texts)

def _extract_src_value(full_text):
    match = re.search(r'^\s*SRC:\s+(.*)', full_text, flags=re.MULTILINE)
    if match:
        return match.group(1).strip()
    return None

def _parse_language_country(src_string):
    match = re.search(r'_([a-z]{2})_([A-Z]{2})_', src_string)
    if match:
        return match.group(1), match.group(2)
    return None, None

def _parse_netflix_temporary_link(msg_obj):
    travel_link_regex = r'(https?://[^"\s]+/account/travel/verify\?nftoken=[^"\s]+)'
    return _search_link_by_regex(msg_obj, travel_link_regex)

def _parse_netflix_update_household_link(msg_obj):
    update_household_regex = r'(https?://[^"\s]+/account/update-primary-location\?nftoken=[^"\s]+)'
    return _search_link_by_regex(msg_obj, update_household_regex)

def _search_link_by_regex(msg_obj, regex_pattern):
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            ctype = part.get_content_type()
            if ctype in ["text/html", "text/plain"]:
                content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                link_match = re.search(regex_pattern, content)
                if link_match:
                    return link_match.group(1)
    else:
        ctype = msg_obj.get_content_type()
        if ctype in ["text/html", "text/plain"]:
            content = msg_obj.get_payload(decode=True).decode('utf-8', errors='ignore')
            link_match = re.search(regex_pattern, content)
            if link_match:
                return link_match.group(1)
    return None

# =============================================================================
# 6. ESCAPAR TEXTO PARA MARKDOWN
# =============================================================================

def escape_markdown(text: str) -> str:
    """
    Escapa los caracteres que podrían causar problemas en Markdown (versión 1).
    """
    # Escapamos: '_', '*', '`', '['
    text = text.replace("_", "\\_")
    text = text.replace("*", "\\*")
    text = text.replace("`", "\\`")
    text = text.replace("[", "\\[")
    return text

# =============================================================================
# 7. HANDLERS DE TELEGRAM
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Usuario"
    user_log(user_id, "/start")

    keyboard = [
        [
            InlineKeyboardButton("Disney+ 🏰", callback_data="obtener_codigo_disney"),
            InlineKeyboardButton("Netflix 🎬", callback_data="submenu_netflix")
        ],
        [InlineKeyboardButton("Ayuda 💡", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"¡Hola, {user_name}! 🤖\nSelecciona un servicio:",
        reply_markup=reply_markup
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if query.data == "obtener_codigo_disney":
        user_log(user_id, "Seleccionó Disney+")
        keyboard = [[InlineKeyboardButton("Cancelar ❌", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Ingresa tu correo para Disney+:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = 'disney'

    elif query.data == "submenu_netflix":
        user_log(user_id, "Seleccionó Netflix (submenú)")
        keyboard = [
            [InlineKeyboardButton("🔗 Link Restablecimiento", callback_data="netflix_reset_link")],
            [InlineKeyboardButton("🔑 Código Único (4 díg.)", callback_data="netflix_access_code")],
            [InlineKeyboardButton("🌎 País/Idioma", callback_data="netflix_country_info")],
            [InlineKeyboardButton("🔑 Acceso Temporal", callback_data="netflix_temporary_access")],
            [InlineKeyboardButton("🏠 Actualiza Hogar", callback_data="netflix_update_household")],
            [InlineKeyboardButton("Cancelar ❌", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="¿Qué deseas de Netflix?",
            reply_markup=reply_markup
        )

    elif query.data == "netflix_reset_link":
        user_log(user_id, "Netflix => Link Restablecimiento")
        keyboard = [[InlineKeyboardButton("Cancelar ❌", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Ingresa el correo para buscar el link de restablecimiento:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = 'netflix_reset_link'

    elif query.data == "netflix_access_code":
        user_log(user_id, "Netflix => Código Único (4 díg.)")
        keyboard = [[InlineKeyboardButton("Cancelar ❌", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Ingresa el correo para buscar el código de acceso (4 díg):",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = 'netflix_access_code'

    elif query.data == "netflix_country_info":
        user_log(user_id, "Netflix => País/Idioma")
        keyboard = [[InlineKeyboardButton("Cancelar ❌", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Ingresa el correo para saber el país/idioma de la cuenta:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = 'netflix_country_info'

    elif query.data == "netflix_temporary_access":
        user_log(user_id, "Netflix => Código Acceso Temporal")
        keyboard = [[InlineKeyboardButton("Cancelar ❌", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Ingresa el correo para buscar el enlace de acceso temporal:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = 'netflix_temporary_access'

    elif query.data == "netflix_update_household":
        user_log(user_id, "Netflix => Código Actualiza Hogar")
        keyboard = [[InlineKeyboardButton("Cancelar ❌", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Ingresa el correo para buscar el enlace de 'Actualizar Hogar':",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = 'netflix_update_household'

    elif query.data == "help":
        user_log(user_id, "Ayuda")
        keyboard = [[InlineKeyboardButton("Volver ↩️", callback_data="volver_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=HELP_TEXT,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = None

    elif query.data == "cancel":
        user_log(user_id, "Operación cancelada")
        keyboard = [
            [
                InlineKeyboardButton("Disney+ 🏰", callback_data="obtener_codigo_disney"),
                InlineKeyboardButton("Netflix 🎬", callback_data="submenu_netflix")
            ],
            [InlineKeyboardButton("Ayuda 💡", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Operación cancelada. Menú principal:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = None

    elif query.data == "volver_menu":
        user_log(user_id, "Volvió al menú principal")
        keyboard = [
            [
                InlineKeyboardButton("Disney+ 🏰", callback_data="obtener_codigo_disney"),
                InlineKeyboardButton("Netflix 🎬", callback_data="submenu_netflix")
            ],
            [InlineKeyboardButton("Ayuda 💡", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text="Menú principal. Selecciona un servicio:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = None

async def email_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    requested_email = update.message.text.strip()
    awaiting = context.user_data.get('awaiting_email_for', None)

    if not awaiting:
        return

    user_log(user_id, f"Ingresó correo '{requested_email}' para {awaiting}")
    context.user_data['awaiting_email_for'] = None

    # Verificar acceso al correo
    if not user_has_valid_access(user_id, requested_email):
        user_log(user_id, "Acceso denegado o expirado al correo")
        await update.message.reply_text(
            "❌ No tienes permiso (o expiró tu acceso) para ese correo."
        )
        return

    await update.message.reply_text("🔄 Buscando, por favor espera...")

    # DISNEY (requiere code permission)
    if awaiting == "disney":
        if not user_has_code_permission(user_id):
            user_log(user_id, "Denegado. No tiene code access para Disney")
            await update.message.reply_text(
                "❌ No tienes permiso para extraer códigos. Contacta a un administrador."
            )
            return

        code, minutes = get_disney_code(requested_email)
        if code:
            user_log(user_id, f"Código Disney: {code}")
            code_esc = escape_markdown(code)
            await update.message.reply_text(
                f"✅ Tu código Disney+ es:\n`{code_esc}`\n\n"
                f"⌛ Recibido hace {minutes} minutos.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⚠️ No se encontró un código reciente de Disney+")

    # NETFLIX RESET LINK (no requiere code permission)
    elif awaiting == "netflix_reset_link":
        link, minutes = get_netflix_reset_link(requested_email)
        if link:
            link_esc = escape_markdown(link)
            user_log(user_id, f"Link Netflix: {link}")
            await update.message.reply_text(
                f"🔗 Link de restablecimiento:\n`{link_esc}`\n\n"
                f"⌛ Recibido hace {minutes} minutos.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⚠️ No se encontró un link reciente de Netflix")

    # NETFLIX CODE (requiere code permission)
    elif awaiting == "netflix_access_code":
        if not user_has_code_permission(user_id):
            user_log(user_id, "Denegado. No tiene code access para Netflix code (4 díg)")
            await update.message.reply_text(
                "❌ No tienes permiso para extraer códigos. Contacta a un administrador."
            )
            return

        code, minutes = get_netflix_access_code(requested_email)
        if code:
            user_log(user_id, f"Código Netflix 4 díg.: {code}")
            code_esc = escape_markdown(code)
            await update.message.reply_text(
                f"✅ Código de acceso (4 díg.):\n`{code_esc}`\n\n"
                f"⌛ Recibido hace {minutes} minutos.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⚠️ No se encontró ningún código reciente de Netflix")

    # NETFLIX COUNTRY INFO (no requiere code permission)
    elif awaiting == "netflix_country_info":
        info, minutes = get_netflix_country_info(requested_email)
        if info:
            lang, country = info
            lang_esc = escape_markdown(lang if lang else "")
            country_esc = escape_markdown(country if country else "")
            user_log(user_id, f"País/Idioma Netflix: {lang}, {country}")
            await update.message.reply_text(
                f"🌎 País: `{country_esc}`\n"
                f"💬 Idioma: `{lang_esc}`\n"
                f"⌛ Info extraída hace {minutes} minutos.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("⚠️ No se encontró país/idioma en el correo de Netflix.")

    # NETFLIX TEMPORARY ACCESS (no requiere code permission)
    elif awaiting == "netflix_temporary_access":
        link, minutes = get_netflix_temporary_access_link(requested_email)
        if link:
            link_esc = escape_markdown(link)
            user_log(user_id, f"Link Netflix (Acceso Temporal): {link}")
            await update.message.reply_text(
                f"🔗 Aquí tienes tu enlace de acceso temporal:\n`{link_esc}`\n\n"
                f"⌛ Recibido hace {minutes} minutos.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "⚠️ No se encontró ningún enlace de acceso temporal en tu correo de Netflix."
            )

    # NETFLIX UPDATE HOUSEHOLD (no requiere code permission)
    elif awaiting == "netflix_update_household":
        link, minutes = get_netflix_update_household_link(requested_email)
        if link:
            link_esc = escape_markdown(link)
            user_log(user_id, f"Link Netflix (Actualizar Hogar): {link}")
            await update.message.reply_text(
                f"🔗 Aquí tienes tu enlace de 'Actualizar Hogar':\n`{link_esc}`\n\n"
                f"⌛ Recibido hace {minutes} minutos.",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "⚠️ No se encontró ningún enlace de 'Actualizar Hogar' en tu correo de Netflix."
            )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get('awaiting_email_for'):
        user_log(user_id, "Cancel con /cancel")
        keyboard = [
            [
                InlineKeyboardButton("Disney+ 🏰", callback_data="obtener_codigo_disney"),
                InlineKeyboardButton("Netflix 🎬", callback_data="submenu_netflix")
            ],
            [InlineKeyboardButton("Ayuda 💡", callback_data="help")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "**Operación cancelada.**\n\nMenú principal:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email_for'] = None
    else:
        user_log(user_id, "No hay operación activa al hacer /cancel")
        await update.message.reply_text("No hay ninguna operación activa que cancelar.")

# =============================================================================
# 8. COMANDOS /help, /mi_perfil
# =============================================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra el mensaje de ayuda con formato Markdown.
    """
    user_id = update.effective_user.id
    user_log(user_id, "/help")

    keyboard = [[InlineKeyboardButton("Volver ↩️", callback_data="volver_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # No escapamos todo el HELP_TEXT, ya que ya está correctamente formateado en Markdown.
    await update.message.reply_text(
        text=HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

    context.user_data['awaiting_email_for'] = None


async def mi_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra la información del usuario: ID, correos y permisos de código.
    """
    user_id = update.effective_user.id
    user_log(user_id, "/mi_perfil")
    users_dict = load_users()

    # Escapar solo el user_id dinámico
    user_id_esc = escape_markdown(str(user_id))

    # Mensaje inicial con negritas
    info = f"**Tu ID de Telegram:** `{user_id_esc}`\n\n"

    # Verificar si tiene correos asignados
    if user_id not in users_dict or not users_dict[user_id]:
        info += "❌ No tienes correos asignados en la base de datos.\n"
    else:
        info += "**Accesos a correos:**\n"
        for mail, exp_date in users_dict[user_id].items():
            mail_esc = escape_markdown(mail)
            if exp_date is None:
                info += f" - `{mail_esc}`: acceso *ilimitado*\n"
            else:
                delta = (exp_date - datetime.now().date()).days
                if delta < 0:
                    info += f" - `{mail_esc}`: ❌ **Expirado** (expiró el {exp_date.isoformat()})\n"
                else:
                    info += f" - `{mail_esc}`: ⏳ {delta} día(s) (expira el {exp_date.isoformat()})\n"

    # Permiso de extracción de códigos
    if user_has_code_permission(user_id):
        code_dict = load_code_access()
        if user_id in code_dict:
            exp_date = code_dict[user_id]
            if exp_date is None:
                info += "\n✅ Tienes *permiso ilimitado* para extraer códigos."
            else:
                delta = (exp_date - datetime.now().date()).days
                if delta < 0:
                    info += "\n❌ Tu permiso para extraer códigos está **expirado**."
                else:
                    info += f"\n⏳ Tienes permiso para extraer códigos hasta {exp_date.isoformat()} (faltan {delta} días)."
        else:
            info += "\n✅ Tienes permiso para extraer códigos (sin fecha registrada)."
    else:
        info += "\n❌ No tienes permiso para extraer códigos."

    # Verificar si es administrador
    if is_admin(user_id):
        info += "\n\n👑 *Eres administrador*, con acceso total."

    # Enviar mensaje con Markdown correctamente formateado
    await update.message.reply_text(info, parse_mode="Markdown")


# =============================================================================
# 9. COMANDOS DE ADMINISTRACIÓN (renombrados, nuevos y listusers)
# =============================================================================

# Renombrado: /add_access -> /adduseremail
async def adduseremail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /adduseremail <user_id> <correo1> [<correo2> ... <correoN>] <días>
    Añade o extiende el acceso a los correos especificados para el user_id indicado.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/adduseremail con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    # Se requiere al menos un user_id, un correo y el número de días
    if len(args) < 3:
        await update.message.reply_text("Uso: /adduseremail <user_id> <correo1> [<correo2> ...] <días>")
        return

    # El primer argumento es el user_id
    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("El primer argumento debe ser un número (user_id).")
        return

    # El último argumento es la cantidad de días
    try:
        days = int(args[-1])
    except ValueError:
        await update.message.reply_text("El último argumento debe ser un número entero (días).")
        return

    # Los argumentos intermedios son los correos a asignar
    emails = [email_arg.lower().strip() for email_arg in args[1:-1]]
    if not emails:
        await update.message.reply_text("Debes especificar al menos un correo.")
        return

    users_dict = load_users()
    if target_user_id not in users_dict:
        users_dict[target_user_id] = {}

    today = datetime.now().date()

    results = []
    for email_arg in emails:
        current_exp = users_dict[target_user_id].get(email_arg)
        if current_exp is None:
            base_date = today
        else:
            base_date = max(today, current_exp)
        new_exp = base_date + timedelta(days=days)
        users_dict[target_user_id][email_arg] = new_exp
        results.append(f"{email_arg}: expira el {new_exp.isoformat()}")

    save_users(users_dict)

    result_text = "\n".join(results)
    await update.message.reply_text(
        f"✅ Se ha asignado/extendido acceso a los siguientes correos para el usuario {target_user_id}:\n{result_text}"
    )

# Renombrado: /remove_access -> /removeemail
async def removeemail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeemail <user_id> <correo1> [<correo2> ...]
    Elimina uno o varios correos asignados a un usuario (ID).
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/removeemail con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uso: /removeemail <user_id> <correo1> [<correo2> ...]")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("El primer argumento debe ser un número (user_id).")
        return

    emails_to_remove = args[1:]
    users_dict = load_users()

    if target_user_id not in users_dict:
        await update.message.reply_text(
            f"⚠️ El usuario {target_user_id} no existe en la base de datos."
        )
        return

    current_emails = users_dict[target_user_id]
    removed = []

    for mail in emails_to_remove:
        mail_lower = mail.lower()
        if mail_lower in current_emails:
            del current_emails[mail_lower]
            removed.append(mail_lower)

    users_dict[target_user_id] = current_emails
    save_users(users_dict)

    if removed:
        removed_str = "\n".join(removed)
        await update.message.reply_text(
            f"Se han eliminado los siguientes correos de {target_user_id}:\n{removed_str}"
        )
    else:
        await update.message.reply_text(
            f"⚠️ Ninguno de los correos proporcionados estaba asignado al usuario {target_user_id}."
        )

# Nuevo: /removeusertotal
async def removeusertotal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removeusertotal <user_id>
    Elimina completamente al usuario (y sus correos) de la base de datos.
    También elimina su permiso de extraer códigos (si lo tuviera).
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/removeusertotal con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /removeusertotal <user_id>")
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El argumento debe ser un número (user_id).")
        return

    users_dict = load_users()
    if target_user_id not in users_dict:
        await update.message.reply_text(
            f"El usuario {target_user_id} no existe en la base de datos."
        )
        return

    # Borramos de users_db
    del users_dict[target_user_id]
    save_users(users_dict)

    # Borramos también de code_access_db
    code_dict = load_code_access()
    if target_user_id in code_dict:
        del code_dict[target_user_id]
        save_code_access(code_dict)

    await update.message.reply_text(
        f"✅ Usuario {target_user_id} eliminado completamente."
    )

# Nuevo: /accesscode
async def accesscode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /accesscode <user_id> <días>
    Otorga permiso de EXTRAER CÓDIGOS a un usuario. 
    Si días <= 0 => acceso indefinido.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/accesscode con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Uso: /accesscode <user_id> <días>")
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El primer argumento debe ser un número (user_id).")
        return

    try:
        days = int(context.args[1])
    except ValueError:
        await update.message.reply_text("El segundo argumento debe ser un número entero (días).")
        return

    code_dict = load_code_access()
    if days <= 0:
        # acceso ilimitado
        code_dict[target_user_id] = None
        save_code_access(code_dict)
        await update.message.reply_text(
            f"✅ Se otorgó acceso para extraer códigos a {target_user_id} de forma *ilimitada*.",
            parse_mode="Markdown"
        )
    else:
        today = datetime.now().date()
        new_exp = today + timedelta(days=days)
        code_dict[target_user_id] = new_exp
        save_code_access(code_dict)
        await update.message.reply_text(
            f"✅ Se otorgó acceso de extracción de códigos a {target_user_id} hasta {new_exp.isoformat()}.",
            parse_mode="Markdown"
        )

# Nuevo: /removecode
async def removecode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /removecode <user_id>
    Revoca el permiso de extraer códigos a un usuario.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/removecode con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /removecode <user_id>")
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El argumento debe ser un número (user_id).")
        return

    code_dict = load_code_access()
    if target_user_id in code_dict:
        del code_dict[target_user_id]
        save_code_access(code_dict)
        await update.message.reply_text(
            f"✅ Se ha removido el permiso de extraer códigos de {target_user_id}."
        )
    else:
        await update.message.reply_text(
            f"⚠️ El usuario {target_user_id} no tenía permiso de extraer códigos."
        )

# Nuevo: /showuser
async def showuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /showuser <user_id>
    Muestra toda la información de un usuario: correos y permisos de código.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/showuser con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Uso: /showuser <user_id>")
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("El argumento debe ser un número (user_id).")
        return

    users_dict = load_users()
    code_dict = load_code_access()

    # Escapamos solo el ID dinámico
    target_user_id_esc = escape_markdown(str(target_user_id))

    msg = [f"**📋 Información de usuario:** `{target_user_id_esc}`\n"]

    # 📧 **Correos asignados**
    if target_user_id not in users_dict or not users_dict[target_user_id]:
        msg.append("❌ *No tiene correos asignados.*")
    else:
        msg.append("📧 **Correos asignados:**")
        for mail, exp_date in users_dict[target_user_id].items():
            mail_esc = escape_markdown(mail)
            if exp_date is None:
                msg.append(f"  - `{mail_esc}`: acceso *ilimitado* ✅")
            else:
                delta = (exp_date - datetime.now().date()).days
                if delta < 0:
                    msg.append(f"  - `{mail_esc}`: ❌ **Expirado** (expiró el {exp_date})")
                else:
                    msg.append(f"  - `{mail_esc}`: ⏳ {delta} día(s) (expira el {exp_date})")

    # 🔑 **Permiso para extraer códigos**
    if target_user_id in code_dict:
        exp_date = code_dict[target_user_id]
        if exp_date is None:
            msg.append("\n🔑 **Permiso de extraer códigos:** *ilimitado* ✅")
        else:
            delta = (exp_date - datetime.now().date()).days
            if delta < 0:
                msg.append(f"\n🔑 **Permiso de extraer códigos:** ❌ *Expirado* (expiró el {exp_date}).")
            else:
                msg.append(f"\n🔑 **Permiso de extraer códigos:** \n⏳ *Válido hasta {exp_date}* (faltan {delta} días).")
    else:
        msg.append("\n🔑 **Permiso de extraer códigos:** ❌ *No tiene acceso*.")

    # Unimos el mensaje correctamente sin escapar el formato Markdown
    final_text = "\n".join(msg)

    await update.message.reply_text(final_text, parse_mode="Markdown")

# /listusers
async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /listusers
    Lista todos los usuarios en la base de datos y sus correos.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, "/listusers")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    users_dict = load_users()
    if not users_dict:
        await update.message.reply_text("No hay usuarios en la base de datos.")
        return

    msg = ["**Lista de Usuarios Autorizados**\n"]
    for uid, emails_dict in users_dict.items():
        if not emails_dict:
            msg.append(f"- **UserID**: `{uid}` | (sin correos)")
            continue

        detalles = []
        for mail, exp_date in emails_dict.items():
            mail_esc = escape_markdown(mail)
            if exp_date is None:
                detalles.append(f"{mail_esc} (ilimitado)")
            else:
                delta = (exp_date - datetime.now().date()).days
                if delta < 0:
                    detalles.append(f"{mail_esc} (expirado {exp_date.isoformat()})")
                else:
                    detalles.append(f"{mail_esc} (expira {exp_date.isoformat()}, faltan {delta} días)")
        detalles_str = "; ".join(detalles)
        msg.append(f"- **UserID**: `{uid}` | {detalles_str}")

        final_text = "\n".join(msg)
    await update.message.reply_text(final_text, parse_mode="Markdown")


# =============================================================================
# 10. MAIN
# =============================================================================

if __name__ == "__main__":
    colorama.init(autoreset=True)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    console_handler.setFormatter(ColorfulFormatter(log_format))

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers principales
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(handle_buttons))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, email_input))
    application.add_handler(CommandHandler("cancel", cancel))

    # /mi_perfil
    application.add_handler(CommandHandler("mi_perfil", mi_perfil))

    # Admin commands
    application.add_handler(CommandHandler("adduseremail", adduseremail))
    application.add_handler(CommandHandler("removeemail", removeemail))
    application.add_handler(CommandHandler("removeusertotal", removeusertotal))
    application.add_handler(CommandHandler("accesscode", accesscode))
    application.add_handler(CommandHandler("removecode", removecode))
    application.add_handler(CommandHandler("showuser", showuser))
    application.add_handler(CommandHandler("listusers", listusers))

    # Ejecuta el bot
    application.run_polling()
