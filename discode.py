# =============================================================================
# Nombre del Proyecto: Telegram Verification Code Bot (Múltiples Admins)
# Autor: Don Marcial
# Fecha de Creación: 2024/Diciembre
# Última Actualización: 2025/Enero
# Versión: 4.2 (Soporte Múltiples Admins + /help + WhatsApp en la ayuda + DB en TXT)
# =============================================================================

import os
import socket
import imaplib
import email
import re
from bs4 import BeautifulSoup
from datetime import datetime, timezone

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
# 1. Configuración global
# =============================================================================

# --- Credenciales de la cuenta principal IMAP (admin@dmarcial.com) ---
ADMIN_IMAP_SERVER = "mail.privateemail.com"
ADMIN_EMAIL = "admin@dmarcial.com"

# Lee la contraseña (app password) desde un archivo
with open('admin_imap_pass.txt', 'r', encoding='utf-8') as f:
    ADMIN_EMAIL_PASSWORD = f.read().strip()

# Lee la lista de administradores desde admin_ids.txt
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
    """
    Retorna True si el user_id está en la lista de administradores.
    """
    return user_id in ADMIN_IDS

# Usaremos un archivo de texto para la "base de datos" de usuarios y correos
USERS_DB_FILE = 'users_db.txt'

# Carpeta de logs
LOGS_FOLDER = "logs"
if not os.path.exists(LOGS_FOLDER):
    os.makedirs(LOGS_FOLDER)

# Lee el token del bot
with open('token.txt', 'r', encoding='utf-8') as token_file:
    TELEGRAM_BOT_TOKEN = token_file.read().strip()

# Texto de ayuda (para botón de Ayuda y comando /help)
HELP_TEXT = (
    "Este bot te ayuda a obtener códigos de verificación enviados a tu correo.\n\n"
    "1. **Escribe el correo** cuando se te pida.\n"
    "2. **El bot buscará el correo más reciente** enviado a esa dirección.\n\n"
    "Si necesitas ayuda adicional, contáctanos por WhatsApp: +34624090880."
)

# =============================================================================
# 2. Log con colores
# =============================================================================

class ColorfulFormatter(logging.Formatter):
    """
    Formatter que añade colores a los mensajes de log según su nivel.
    """
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
    Registra un mensaje en logs/<user_id>.txt
    """
    log_file = os.path.join(LOGS_FOLDER, f"{user_id}.txt")
    with open(log_file, "a", encoding='utf-8') as f:
        f.write(message + "\n")


# =============================================================================
# 3. Carga y guardado de usuarios (ahora en archivo TXT)
# =============================================================================

def load_users():
    """
    Carga los usuarios y sus correos desde USERS_DB_FILE.
    Formato de cada línea: <UserID> <email1> <email2> ...
    Retorna un diccionario: { user_id: {email1, email2, ...}, ... }
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
            if len(parts) < 2:
                # Puede haber un user_id sin correos
                try:
                    user_id = int(parts[0])
                    users_dict[user_id] = set()
                except ValueError:
                    pass
                continue

            # Primer elemento es el user_id, el resto son correos
            try:
                user_id = int(parts[0])
            except ValueError:
                # Si no podemos convertir el user_id, ignoramos la línea
                continue

            emails = set(parts[1:])  # resto de la línea son correos
            users_dict[user_id] = emails

    return users_dict

def save_users(users_dict):
    """
    Guarda el diccionario de usuarios en USERS_DB_FILE.
    Cada línea: <UserID> <email1> <email2> ...
    """
    with open(USERS_DB_FILE, 'w', encoding='utf-8') as f:
        for user_id, emails_set in users_dict.items():
            if emails_set:
                emails_str = " ".join(sorted(emails_set))
                f.write(f"{user_id} {emails_str}\n")
            else:
                # Si no tiene correos, solo escribimos el user_id
                f.write(f"{user_id}\n")


# =============================================================================
# 4. Lógica de permisos y obtención del código
# =============================================================================

def user_has_access(user_id: int, email_address: str) -> bool:
    """
    Verifica si user_id (o cualquier admin) tiene permiso para pedir
    el código del 'email_address' dado.
    """
    # Si es admin, tiene acceso a cualquier correo
    if is_admin(user_id):
        return True

    # Si no es admin, revisamos la lista de usuarios
    users_dict = load_users()
    if user_id not in users_dict:
        return False

    # Comparamos en minúsculas
    return email_address.lower() in [e.lower() for e in users_dict[user_id]]


def get_verification_code(requested_email: str):
    """
    Obtiene el código de verificación (6 dígitos) del último correo de Disney+
    enviado a 'requested_email'. Para ello:
      1. Inicia sesión IMAP usando la cuenta ADMIN_EMAIL / ADMIN_EMAIL_PASSWORD.
      2. Busca correos de Disney+ (2 posibles remitentes).
      3. Recorre correos recientes, verificando si el destino es 'requested_email'.
      4. Extrae y devuelve (code, minutes). Retorna (None, None) si no lo encuentra.
    """
    socket.setdefaulttimeout(10)  # time-out de 10 segundos

    try:
        server = imaplib.IMAP4_SSL(ADMIN_IMAP_SERVER)
        server.login(ADMIN_EMAIL, ADMIN_EMAIL_PASSWORD)
        server.select("inbox")

        # Buscar correos con FROM Disney
        status, messages = server.search(
            None,
            '(OR FROM "disneyplus@mail.disneyplus.com" FROM "disneyplus@mail2.disneyplus.com")'
        )

        if status != "OK" or not messages or not messages[0]:
            server.logout()
            return None, None

        email_ids = messages[0].split()
        # Recorremos del más reciente al más antiguo
        for email_id in reversed(email_ids):
            status_msg, msg_data = server.fetch(email_id, "(RFC822)")
            if status_msg != "OK":
                continue

            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg_obj = email.message_from_bytes(response_part[1])
                    # Fecha del correo
                    date_header = msg_obj["Date"]
                    parsed_date = email.utils.parsedate_to_datetime(date_header).astimezone(timezone.utc)
                    now = datetime.now(timezone.utc)
                    time_diff = now - parsed_date
                    total_minutes = int(time_diff.total_seconds() // 60)

                    # Verificar destinatarios en varios headers
                    recipients = []
                    for header_key, header_value in msg_obj.items():
                        if header_key.lower() in ["to", "cc", "bcc", "delivered-to", "x-original-to"]:
                            if header_value:
                                recipients.append(header_value.lower())

                    recipients_str = "\n".join(recipients)
                    if requested_email.lower() not in recipients_str:
                        # No coincide con el email buscado
                        continue

                    # Extraer el código de 6 dígitos
                    code = extract_6_digit_code(msg_obj)
                    if code:
                        server.logout()
                        return code, total_minutes

        server.logout()
        return None, None

    except socket.timeout:
        logging.error("Time-Out: El servidor de correo tardó demasiado en responder.")
        return None, None
    except Exception as e:
        logging.error(f"Error al obtener el código de verificación: {e}")
        return None, None


def extract_6_digit_code(msg_obj) -> str:
    """
    Dado un objeto EmailMessage, busca y retorna un código de 6 dígitos
    en su contenido. Retorna None si no encuentra.
    """
    if msg_obj.is_multipart():
        for part in msg_obj.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                html_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                text = BeautifulSoup(html_content, "html.parser").get_text()
                code_match = re.search(r'\b\d{6}\b', text)
                if code_match:
                    return code_match.group(0)
            elif content_type == "text/plain":
                plain_content = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                code_match = re.search(r'\b\d{6}\b', plain_content)
                if code_match:
                    return code_match.group(0)
    else:
        # No es multipart
        content_type = msg_obj.get_content_type()
        payload = msg_obj.get_payload(decode=True).decode("utf-8", errors="ignore")
        if content_type == "text/html":
            text = BeautifulSoup(payload, "html.parser").get_text()
            code_match = re.search(r'\b\d{6}\b', text)
            if code_match:
                return code_match.group(0)
        elif content_type == "text/plain":
            code_match = re.search(r'\b\d{6}\b', payload)
            if code_match:
                return code_match.group(0)

    return None


# =============================================================================
# 5. Handlers de comandos y mensajes
# =============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando /start. Muestra el menú principal en un mensaje nuevo.
    """
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name or "Usuario"
    user_log(user_id, "Usuario ejecutó /start.")

    keyboard = [
        [
            InlineKeyboardButton("Obtener Código", callback_data="obtener_codigo"),
            InlineKeyboardButton("Ayuda", callback_data="help")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"¡Hola, {user_name}! Bienvenido al bot de códigos de verificación.\n"
        "Selecciona una opción:",
        reply_markup=reply_markup
    )


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Maneja los botones interactivos (CallbackQuery).
    """
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()  # Quita el "loading..."

    if query.data == "obtener_codigo":
        user_log(user_id, "Usuario seleccionó Obtener Código.")
        keyboard = [[InlineKeyboardButton("Cancelar", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text="Por favor, ingresa tu dirección de correo electrónico:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email'] = True

    elif query.data == "help":
        user_log(user_id, "Usuario solicitó Ayuda.")
        keyboard = [[InlineKeyboardButton("Volver", callback_data="volver_menu")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Usa el HELP_TEXT definido
        await query.edit_message_text(
            text=HELP_TEXT,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email'] = False

    elif query.data == "cancel":
        user_log(user_id, "Usuario seleccionó Cancelar desde un botón.")
        keyboard = [
            [
                InlineKeyboardButton("Obtener Código", callback_data="obtener_codigo"),
                InlineKeyboardButton("Ayuda", callback_data="help")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text=(
                "**Operación cancelada.**\n\n"
                "Menú principal: Selecciona una opción:"
            ),
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email'] = False

    elif query.data == "cambiar_correo":
        user_log(user_id, "Usuario seleccionó Cambiar Correo (Reintentar).")
        keyboard = [[InlineKeyboardButton("Cancelar", callback_data="cancel")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text="Ingresa un nuevo correo para buscar el código:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email'] = True

    elif query.data == "volver_menu":
        user_log(user_id, "Usuario seleccionó Volver al Menú Principal.")
        keyboard = [
            [
                InlineKeyboardButton("Obtener Código", callback_data="obtener_codigo"),
                InlineKeyboardButton("Ayuda", callback_data="help")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text="¡Menú principal! Selecciona una opción:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email'] = False


async def email_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Procesa la dirección de correo ingresada por el usuario (mientras awaiting_email=True).
    """
    if context.user_data.get('awaiting_email', False):
        requested_email = update.message.text.strip()
        user_id = update.effective_user.id

        user_log(user_id, f"Usuario solicitó código para el correo: {requested_email}")

        # Verifica si el usuario tiene permiso
        if not user_has_access(user_id, requested_email):
            user_log(user_id, "Acceso denegado (sin permisos).")
            await update.message.reply_text(
                "❌ **No tienes permiso para consultar este correo.**\n"
                "Pídele al administrador que te autorice.",
                parse_mode="Markdown"
            )
            context.user_data['awaiting_email'] = False
            return

        # Mensaje de espera
        await update.message.reply_text(
            "🔄 **Buscando tu código, por favor espera...**",
            parse_mode="Markdown"
        )

        # Obtiene el código usando la cuenta principal
        code, minutes = get_verification_code(requested_email)
        if code:
            user_log(user_id, f"Código obtenido: {code} (hace {minutes} minutos).")
            await update.message.reply_text(
                f"✉️ **Tu código de verificación es:** `{code}`\n"
                f"⌛ **Recibido hace** {minutes} **minutos.**",
                parse_mode="Markdown"
            )
        else:
            user_log(user_id, "No se encontró código o error en la conexión.")
            await update.message.reply_text(
                "⚠️ **No se encontró ningún código reciente** o no se pudo obtener.",
                parse_mode="Markdown"
            )

        context.user_data['awaiting_email'] = False


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando /cancel: si el usuario estaba esperando un correo, lo cancela.
    De lo contrario, responde que no hay operación activa.
    """
    user_id = update.effective_user.id
    if context.user_data.get('awaiting_email', False):
        user_log(user_id, "Usuario canceló la operación con /cancel.")
        keyboard = [
            [
                InlineKeyboardButton("Obtener Código", callback_data="obtener_codigo"),
                InlineKeyboardButton("Ayuda", callback_data="help")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "**Operación cancelada.**\n\n"
            "Menú principal: Selecciona una opción:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email'] = False
    else:
        user_log(user_id, "Usuario ejecutó /cancel sin operación activa.")
        await update.message.reply_text("No hay ninguna operación activa que cancelar.")


# =============================================================================
# 6. Comando /help (opcional, además del botón Ayuda)
# =============================================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Muestra el mismo texto que el botón "Ayuda" en el menú principal.
    """
    user_id = update.effective_user.id
    user_log(user_id, "Usuario ejecutó /help.")

    keyboard = [[InlineKeyboardButton("Volver", callback_data="volver_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text=HELP_TEXT,
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    context.user_data['awaiting_email'] = False


# =============================================================================
# 7. Comandos de administración y utilidad
# =============================================================================

async def mi_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra el ID de Telegram del usuario."""
    user_id = update.effective_user.id
    user_log(user_id, "Usuario solicitó /mi_id.")
    await update.message.reply_text(
        f"Tu ID de Telegram es: **{user_id}**",
        parse_mode="Markdown"
    )


async def add_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add_access <user_id> <correo1> [<correo2> ...]
    Agrega uno o varios correos a la lista de permitidos para un user_id.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"Admin ejecutó /add_access con args {context.args}")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uso: /add_access <user_id> <correo1> [<correo2> ...]")
        return

    try:
        target_user_id = int(args[0])
    except ValueError:
        await update.message.reply_text("El primer argumento debe ser un número (user_id).")
        return

    new_emails = args[1:]

    users_dict = load_users()
    if target_user_id not in users_dict:
        # Si el user no existe, lo creamos
        users_dict[target_user_id] = set(new_emails)
    else:
        # Si existe, actualizamos
        for mail in new_emails:
            users_dict[target_user_id].add(mail.lower())

    save_users(users_dict)

    await update.message.reply_text(
        f"✅ Se ha agregado acceso al usuario {target_user_id} para:\n" +
        "\n".join(new_emails)
    )


async def remove_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /remove_access <user_id> <correo1> [<correo2> ...]
    Elimina uno o varios correos de la lista de permitidos para un user_id.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"Admin ejecutó /remove_access con args {context.args}")

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
            current_emails.remove(mail_lower)
            removed.append(mail_lower)

    # Actualizamos el set de ese usuario
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
    /list_users: muestra los usuarios y correos autorizados.
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, "Admin ejecutó /list_users")

    if not is_admin(admin_user_id):
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    users_dict = load_users()
    if not users_dict:
        await update.message.reply_text("No hay usuarios en la base de datos.")
        return

    msg = ["**Lista de Usuarios Autorizados**\n"]
    for uid, emails_set in users_dict.items():
        emails_str = ", ".join(sorted(emails_set)) if emails_set else "(sin correos)"
        msg.append(f"- **UserID**: `{uid}` | **Emails**: `{emails_str}`")

    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")


# =============================================================================
# 8. Main / Ejecución del bot
# =============================================================================

if __name__ == "__main__":
    # Inicializa colorama para consola con colores
    colorama.init(autoreset=True)

    # Logger con colores
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    console_handler.setFormatter(ColorfulFormatter(log_format))

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # Construimos la aplicación
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers básicos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))  # /help adicional
    application.add_handler(CallbackQueryHandler(handle_buttons))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, email_input))
    application.add_handler(CommandHandler("cancel", cancel))

    # Handlers de administración
    application.add_handler(CommandHandler("add_access", add_access))
    application.add_handler(CommandHandler("remove_access", remove_access))
    application.add_handler(CommandHandler("list_users", list_users))

    # Handler de utilidad
    application.add_handler(CommandHandler("mi_id", mi_id))

    # Ejecutamos el bot (polling)
    application.run_polling()
