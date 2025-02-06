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

# Leemos múltiples cuentas desde admin_imap_pass.txt (una por línea, "correo|contraseña")
def load_email_accounts(filename='admin_imap_pass.txt'):
    """
    Cada línea: 'correo@dominio.com|password'
    Devuelve una lista de tuplas [(correo, contraseña), ...].
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
                # Si la línea no contiene '|', la ignoramos o podrías lanzar un error.
                continue
            email_str, password_str = line.split("|", 1)
            accounts.append((email_str.strip(), password_str.strip()))
    return accounts

EMAIL_ACCOUNTS = load_email_accounts()

def load_admin_ids(filename='admin_ids.txt'):
    """
    Carga IDs de administradores (uno por línea).
    """
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
    """
    Verifica si user_id está en la lista de administradores.
    """
    return user_id in ADMIN_IDS

USERS_DB_FILE = 'users_db.txt'
LOGS_FOLDER = "logs"
if not os.path.exists(LOGS_FOLDER):
    os.makedirs(LOGS_FOLDER)

# Este archivo contendrá el token del bot (solo una línea con el token)
with open('token.txt', 'r', encoding='utf-8') as token_file:
    TELEGRAM_BOT_TOKEN = token_file.read().strip()

HELP_TEXT = (
    "ℹ️ *Ayuda del Bot*\n\n"
    "Este bot te permite obtener códigos de *Disney+* o *Netflix* "
    "si tienes permiso sobre el correo.\n"
    "1. Pulsa un botón en el menú principal.\n"
    "2. Ingresa tu correo.\n"
    "3. Te enviaremos el código o link si lo encontramos.\n\n"
    "Si necesitas más ayuda, contáctanos por WhatsApp: +34624090880. 💬"
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
    """
    Registra en logs/<user_id>.txt las acciones del usuario.
    """
    log_file = os.path.join(LOGS_FOLDER, f"{user_id}.txt")
    with open(log_file, "a", encoding='utf-8') as f:
        f.write(f"{datetime.now()}: {message}\n")

# =============================================================================
# 3. BASE DE DATOS DE USUARIOS (CON FECHAS DE EXPIRACIÓN)
# =============================================================================

"""
En users_db.txt guardamos:
<user_id> email1:YYYY-MM-DD email2:YYYY-MM-DD ...
Si no hay fecha, se asume acceso indefinido (None).
"""

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
            # primer elemento => user_id
            try:
                uid = int(parts[0])
            except ValueError:
                continue

            users_dict[uid] = {}
            # resto => email:fecha
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
                    # sin fecha => None
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
    """
    - Si user_id es administrador => acceso total a cualquier correo.
    - Si no es admin, verifica si está en la DB y si la fecha no ha expirado.
    """
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
        return True  # acceso indefinido

    today = datetime.now().date()
    return today <= exp_date

# =============================================================================
# 4. FUNCIONES PARA DISNEY+ Y NETFLIX
# =============================================================================

# ----------------- DISNEY+ ------------------

def get_disney_code(requested_email: str):
    """
    Busca un correo de Disney+ en las cuentas definidas en EMAIL_ACCOUNTS.
    Retorna (code, minutes) o (None, None).
    """
    socket.setdefaulttimeout(10)
    for (acc_email, acc_password) in EMAIL_ACCOUNTS:
        try:
            server = imaplib.IMAP4_SSL(IMAP_HOST)
            server.login(acc_email, acc_password)
            server.select("INBOX")

            # Filtra correos de Disney+
            status, messages = server.search(
                None,
                '(OR FROM "disneyplus@mail.disneyplus.com" FROM "disneyplus@mail2.disneyplus.com")'
            )
            if status != "OK" or not messages or not messages[0]:
                server.logout()
                continue

            email_ids = messages[0].split()
            # Del más reciente al más antiguo
            for email_id in reversed(email_ids):
                status_msg, msg_data = server.fetch(email_id, "(RFC822)")
                if status_msg != "OK":
                    continue

                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg_obj = email.message_from_bytes(response_part[1])
                        # Verifica destinatario
                        recipients = []
                        for header_key, header_value in msg_obj.items():
                            if header_key.lower() in ["to", "cc", "bcc", "delivered-to", "x-original-to"]:
                                if header_value:
                                    recipients.append(header_value.lower())

                        if requested_email.lower() not in "\n".join(recipients):
                            continue

                        # Minutos desde el envío
                        date_header = msg_obj["Date"]
                        parsed_date = email.utils.parsedate_to_datetime(date_header).astimezone(timezone.utc)
                        now = datetime.now(timezone.utc)
                        time_diff = now - parsed_date
                        total_minutes = int(time_diff.total_seconds() // 60)

                        # Extraer código 6 dígitos
                        code = extract_6_digit_code(msg_obj)
                        if code:
                            server.logout()
                            return code, total_minutes

            server.logout()
        except Exception as e:
            logging.error(f"Error con la cuenta {acc_email} al buscar Disney+: {e}")
            # Intenta la siguiente cuenta

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

# ----------------- NETFLIX ------------------

def get_netflix_reset_link(requested_email: str):
    """
    Busca un correo de Netflix y extrae el link de restablecimiento.
    Retorna (link, minutes) o (None, None).
    """
    return _search_netflix_email(requested_email, _parse_netflix_link)

def get_netflix_access_code(requested_email: str):
    """
    Busca un correo de Netflix y extrae un código de EXACTAMENTE 4 dígitos.
    Retorna (code, minutes).
    """
    return _search_netflix_email(requested_email, _parse_netflix_code)

def get_netflix_country_info(requested_email: str):
    """
    Busca un correo de Netflix para extraer (lang, country) con la etiqueta 'SRC:'.
    Retorna ((lang, country), minutes).
    """
    return _search_netflix_email(requested_email, _parse_netflix_country)

def get_netflix_temporary_access_link(requested_email: str):
    """
    Busca en los correos de Netflix el link específico para "account/travel/verify".
    Retorna (link, minutes) o (None, None).
    """
    return _search_netflix_email(requested_email, _parse_netflix_temporary_link)

# (NUEVO) --> Función para "Código Actualiza Hogar"
def get_netflix_update_household_link(requested_email: str):
    """
    Busca en los correos de Netflix el link específico para "account/update-primary-location".
    Retorna (link, minutes) o (None, None).
    """
    return _search_netflix_email(requested_email, _parse_netflix_update_household_link)

def _search_netflix_email(requested_email: str, parse_function):
    """
    Función genérica para buscar en correos de Netflix.
    parse_function es la función que extrae el dato que necesitamos (link, código, etc.)
    """
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
                        time_diff = now - parsed_date
                        total_minutes = int(time_diff.total_seconds() // 60)

                        extracted_value = parse_function(msg_obj)
                        if extracted_value:
                            server.logout()
                            return extracted_value, total_minutes

            server.logout()
        except Exception as e:
            logging.error(f"Error con la cuenta {acc_email} al buscar Netflix: {e}")
            # Pasamos a la siguiente cuenta

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
        # 1) <a> con texto "Restablecer contraseña"
        link_tag = soup.find("a", string=re.compile(r"restablecer contraseña", re.IGNORECASE))
        if link_tag and link_tag.get("href"):
            return link_tag["href"]
        # 2) <a> con "password?" en href
        for a in soup.find_all("a", href=True):
            if "password?" in a["href"]:
                return a["href"]
    else:
        # texto plano
        match = re.search(r'(https?://[^\s]+password\?[^"\s]*)', content)
        if match:
            return match.group(1)
    return None

def _parse_netflix_code(msg_obj):
    # 4 dígitos exactos
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
    """
    Busca un enlace que contenga:
    https://www.netflix.com/account/travel/verify?nftoken=...
    """
    travel_link_regex = r'(https?://[^"\s]+/account/travel/verify\?nftoken=[^"\s]+)'
    return _search_link_by_regex(msg_obj, travel_link_regex)


def _parse_netflix_update_household_link(msg_obj):
    """
    Busca un enlace que contenga:
    https://www.netflix.com/account/update-primary-location?nftoken=...
    """
    update_household_regex = r'(https?://[^"\s]+/account/update-primary-location\?nftoken=[^"\s]+)'
    return _search_link_by_regex(msg_obj, update_household_regex)

def _search_link_by_regex(msg_obj, regex_pattern):
    """
    Función auxiliar para buscar en el cuerpo del correo (HTML / texto)
    la expresión regular que capture el enlace deseado.
    """
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
# 5. HANDLERS DE TELEGRAM
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
            text="Ingresa el correo para buscar el código de acceso:",
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

    # (NUEVO) Callback para “Código Actualiza Hogar”
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
    """
    Handler que se activa cuando el usuario ingresa texto (y no es comando).
    Verifica qué correo pidieron y para qué servicio.
    """
    user_id = update.effective_user.id
    requested_email = update.message.text.strip()
    awaiting = context.user_data.get('awaiting_email_for', None)

    if not awaiting:
        return  # no estaba esperando nada

    user_log(user_id, f"Ingresó correo '{requested_email}' para {awaiting}")
    context.user_data['awaiting_email_for'] = None

    # Verificamos si tiene acceso
    if not user_has_valid_access(user_id, requested_email):
        user_log(user_id, "Acceso denegado o expirado")
        await update.message.reply_text(
            "❌ No tienes permiso (o expiró tu acceso) para ese correo."
        )
        return

    await update.message.reply_text("🔄 Buscando, por favor espera...")

    if awaiting == "disney":
        code, minutes = get_disney_code(requested_email)
        if code:
            user_log(user_id, f"Código Disney: {code}")
            await update.message.reply_text(
                f"✅ Tu código Disney+ es: {code}\n"
                f"⌛ Recibido hace {minutes} minutos."
            )
        else:
            await update.message.reply_text("⚠️ No se encontró un código reciente de Disney+")

    elif awaiting == "netflix_reset_link":
        link, minutes = get_netflix_reset_link(requested_email)
        if link:
            user_log(user_id, f"Link Netflix: {link}")
            await update.message.reply_text(
                f"🔗 Link de restablecimiento:\n{link}\n\n"
                f"⌛ Recibido hace {minutes} minutos."
            )
        else:
            await update.message.reply_text("⚠️ No se encontró un link reciente de Netflix")

    elif awaiting == "netflix_access_code":
        code, minutes = get_netflix_access_code(requested_email)
        if code:
            user_log(user_id, f"Código Netflix: {code}")
            await update.message.reply_text(
                f"✅ Código de acceso (4 díg.): {code}\n"
                f"⌛ Recibido hace {minutes} minutos."
            )
        else:
            await update.message.reply_text("⚠️ No se encontró ningún código reciente de Netflix")

    elif awaiting == "netflix_country_info":
        info, minutes = get_netflix_country_info(requested_email)
        if info:
            lang, country = info
            user_log(user_id, f"País/Idioma Netflix: {lang}, {country}")
            await update.message.reply_text(
                f"🌎 País: {country}\n"
                f"💬 Idioma: {lang}\n"
                
            )
        else:
            await update.message.reply_text("⚠️ No se encontró país/idioma en el correo de Netflix.")

    elif awaiting == "netflix_temporary_access":
        link, minutes = get_netflix_temporary_access_link(requested_email)
        if link:
            user_log(user_id, f"Link Netflix (Acceso Temporal): {link}")
            await update.message.reply_text(
                f"🔗 Aquí tienes tu enlace de acceso temporal:\n{link}\n\n"
                f"⌛ Recibido hace {minutes} minutos."
            )
        else:
            await update.message.reply_text(
                "⚠️ No se encontró ningún enlace de acceso temporal en tu correo de Netflix."
            )

    # (NUEVO) - Manejo de “Código Actualiza Hogar”
    elif awaiting == "netflix_update_household":
        link, minutes = get_netflix_update_household_link(requested_email)
        if link:
            user_log(user_id, f"Link Netflix (Actualizar Hogar): {link}")
            await update.message.reply_text(
                f"🔗 Aquí tienes tu enlace de 'Actualizar Hogar':\n{link}\n\n"
                f"⌛ Recibido hace {minutes} minutos."
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
# 6. COMANDOS: /help, /mi_perfil, etc.
# =============================================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_log(user_id, "/help")
    keyboard = [[InlineKeyboardButton("Volver ↩️", callback_data="volver_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        text=HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    context.user_data['awaiting_email_for'] = None

async def mi_perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra el ID de Telegram y los correos (con expiración) que tiene el usuario.
    Si es admin => acceso total, pero igual mostramos si tiene correos propios en la DB.
    """
    user_id = update.effective_user.id
    user_log(user_id, "/mi_perfil")
    users_dict = load_users()

    info = f"Tu ID de Telegram: **{user_id}**\n\n"
    if user_id not in users_dict or not users_dict[user_id]:
        info += "No tienes correos asignados en la base de datos."
    else:
        info += "Accesos asignados:\n"
        for mail, exp_date in users_dict[user_id].items():
            if exp_date is None:
                info += f" - {mail}: acceso *ilimitado*\n"
            else:
                delta = (exp_date - datetime.now().date()).days
                if delta < 0:
                    info += f" - {mail}: **Expirado** (expiró el {exp_date.isoformat()})\n"
                else:
                    info += f" - {mail}: {delta} día(s) restante(s) (expira el {exp_date.isoformat()})\n"

    if is_admin(user_id):
        info += "\nEres *administrador*, por lo que tienes acceso total a cualquier correo."
    await update.message.reply_text(info, parse_mode="Markdown")

# =============================================================================
# 7. COMANDOS DE ADMINISTRACIÓN
# =============================================================================

async def add_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_access <user_id> <correo> <dias>
    Añade o extiende el acceso a <correo> para el user_id especificado.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/add_access con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Uso: /add_access <user_id> <correo> <días>")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("El primer argumento debe ser un número (user_id).")
        return

    email_arg = args[1].lower()
    try:
        days = int(args[2])
    except ValueError:
        await update.message.reply_text("El tercer argumento debe ser un número entero (días).")
        return

    users_dict = load_users()
    if target_user_id not in users_dict:
        users_dict[target_user_id] = {}

    today = datetime.now().date()
    current_exp = users_dict[target_user_id].get(email_arg)
    if current_exp is None:
        base_date = today
    else:
        base_date = max(today, current_exp)

    new_exp = base_date + timedelta(days=days)
    users_dict[target_user_id][email_arg] = new_exp
    save_users(users_dict)

    await update.message.reply_text(
        f"✅ Se ha asignado/extendido acceso a *{email_arg}* "
        f"para el usuario {target_user_id}.\n"
        f"Expira el {new_exp.isoformat()}."
    )

async def remove_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /remove_access <user_id> <correo1> [<correo2> ...]
    Elimina correos de la lista de un user_id.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"/remove_access con args: {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uso: /remove_access <user_id> <correo1> [<correo2> ...]")
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
        await update.message.reply_text(
            f"Se han eliminado los siguientes correos de {target_user_id}:\n" +
            "\n".join(removed)
        )
    else:
        await update.message.reply_text(
            f"⚠️ Ninguno de los correos proporcionados estaba asignado al usuario {target_user_id}."
        )

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lista todos los user_id y los correos que tienen, con fecha de expiración.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, "/list_users")

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
            if exp_date is None:
                detalles.append(f"{mail} (ilimitado)")
            else:
                delta = (exp_date - datetime.now().date()).days
                if delta < 0:
                    detalles.append(f"{mail} (expirado {exp_date.isoformat()})")
                else:
                    detalles.append(f"{mail} (expira {exp_date.isoformat()} / faltan {delta} días)")
        detalles_str = "; ".join(detalles)
        msg.append(f"- **UserID**: `{uid}` | {detalles_str}")

    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")

# =============================================================================
# 8. MAIN
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

    # Admin
    application.add_handler(CommandHandler("add_access", add_access))
    application.add_handler(CommandHandler("remove_access", remove_access))
    application.add_handler(CommandHandler("list_users", list_users))

    # Ejecuta el bot
    application.run_polling()
