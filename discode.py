import os
import socket
import imaplib
import email
from email.header import decode_header
import re
import pandas as pd
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
# 1. Configuración de color en logs
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


# =============================================================================
# 2. Carpeta de logs por usuario
# =============================================================================

LOGS_FOLDER = "logs"
if not os.path.exists(LOGS_FOLDER):
    os.makedirs(LOGS_FOLDER)

def user_log(user_id: int, message: str):
    """
    Registra un mensaje en logs/<user_id>.txt
    """
    log_file = os.path.join(LOGS_FOLDER, f"{user_id}.txt")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(message + "\n")


# =============================================================================
# 3. Cargar datos y configuraciones
# =============================================================================

# Lee el ID del admin desde un archivo
with open('admin_id.txt', 'r') as f:
    ADMIN_ID = int(f.read().strip())

# Carga el archivo con credenciales IMAP (col: "Correo", "IMAP", "Pass")
df = pd.read_excel('correos.xlsx')

# Archivo para controlar accesos de usuarios
USERS_FILE = 'usuarios.xlsx'
def load_users():
    try:
        df_users = pd.read_excel(USERS_FILE)
        if not {'UserID', 'Emails'}.issubset(df_users.columns):
            raise ValueError(f"El archivo {USERS_FILE} no contiene las columnas necesarias.")
        return df_users
    except FileNotFoundError:
        df_users = pd.DataFrame(columns=['UserID', 'Emails'])
        df_users.to_excel(USERS_FILE, index=False)
        return df_users

def save_users(df_users):
    df_users.to_excel(USERS_FILE, index=False)

# Lee el token del bot
with open('token.txt', 'r') as token_file:
    TELEGRAM_BOT_TOKEN = token_file.read().strip()


# =============================================================================
# 4. Funciones auxiliares
# =============================================================================

def autodetect_imap_server(email_address: str) -> str:
    """
    Intenta deducir el servidor IMAP según el dominio del email.
    Ajusta estas reglas a tus necesidades.
    """
    domain = email_address.split("@")[-1].lower()

    # Regla para PrivateEmail (Namecheap)
    if "privateemail.com" in domain:
        return "mail.privateemail.com"

    # Fallback genérico: "mail.<dominio>"
    return f"mail.{domain}"


# =============================================================================
# 5. Lógica para credenciales e IMAP
# =============================================================================

def user_has_access(user_id: int, email_address: str) -> bool:
    """
    Retorna True si es ADMIN o si user_id tiene permiso para email_address.
    """
    if user_id == ADMIN_ID:
        return True

    df_users = load_users()
    row = df_users.loc[df_users['UserID'] == user_id]

    if row.empty:
        return False

    emails_str = row.iloc[0]['Emails']
    if not emails_str or pd.isna(emails_str):
        return False

    allowed_emails = set(email.strip().lower() for email in emails_str.split(';') if email.strip())
    return email_address.lower() in allowed_emails


def get_credentials(email_address: str):
    """
    Retorna una tupla (imap_server, password) para el correo dado,
    o (None, None) si no existe en correos.xlsx y tampoco se puede deducir.
    """
    user_data = df[df['Correo'].str.lower() == email_address.lower()]

    # Si el correo aparece en el DataFrame
    if not user_data.empty:
        imap_server = user_data['IMAP'].values[0] if 'IMAP' in user_data.columns else None
        app_password = user_data['Pass'].values[0] if 'Pass' in user_data.columns else None
        
        # Si no hay IMAP en la fila, intentamos autodetectar
        if pd.isna(imap_server) or not imap_server:
            imap_server = autodetect_imap_server(email_address)

        return imap_server, app_password

    else:
        # Si no existe en correos.xlsx, intentamos deducir
        imap_server = autodetect_imap_server(email_address)
        # Sin una contraseña conocida, devolvemos None
        app_password = None
        return imap_server, app_password


def get_verification_code(email_address: str, imap_server: str, app_password: str):
    """
    Retorna (code, minutes) si encuentra un código de 6 dígitos en un correo Disney+;
    De lo contrario (None, None).
    """
    socket.setdefaulttimeout(10)  # time-out de 10 segundos

    # Si no hay contraseña, salimos
    if not app_password:
        logging.error(f"No se proporcionó contraseña para {email_address}.")
        return None, None

    try:
        server = imaplib.IMAP4_SSL(imap_server)
        server.login(email_address, app_password)
        server.select("inbox")

        status, messages = server.search(
            None,
            '(OR FROM "disneyplus@mail.disneyplus.com" FROM "disneyplus@mail2.disneyplus.com")'
        )
        if not messages or not messages[0]:
            return None, None

        email_ids = messages[0].split()
        latest_email_id = email_ids[-1]
        status, msg_data = server.fetch(latest_email_id, "(RFC822)")

        code = None
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg_obj = email.message_from_bytes(response_part[1])
                date_header = msg_obj["Date"]
                parsed_date = email.utils.parsedate_to_datetime(date_header).astimezone(timezone.utc)
                now = datetime.now(timezone.utc)
                time_diff = now - parsed_date
                total_minutes = int(time_diff.total_seconds() // 60)

                if msg_obj.is_multipart():
                    for part in msg_obj.walk():
                        content_type = part.get_content_type()
                        if content_type == "text/html":
                            html_content = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                            soup = BeautifulSoup(html_content, "html.parser")
                            match = re.search(r'\b\d{6}\b', soup.get_text())
                            if match:
                                code = match.group(0)
                                break
                        elif content_type == "text/plain":
                            plain_content = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                            match = re.search(r'\b\d{6}\b', plain_content)
                            if match:
                                code = match.group(0)
                                break
                else:
                    content_type = msg_obj.get_content_type()
                    payload = msg_obj.get_payload(decode=True).decode("utf-8", errors="ignore")
                    if content_type == "text/html":
                        soup = BeautifulSoup(payload, "html.parser")
                        match = re.search(r'\b\d{6}\b', soup.get_text())
                        if match:
                            code = match.group(0)
                    elif content_type == "text/plain":
                        match = re.search(r'\b\d{6}\b', payload)
                        if match:
                            code = match.group(0)

                if code:
                    return code, total_minutes

        return None, None

    except socket.timeout:
        logging.error("Time-Out: El servidor de correo tardó demasiado en responder.")
        return None, None
    except Exception as e:
        logging.error(f"Error al obtener el código de verificación: {e}")
        return None, None
    finally:
        if 'server' in locals():
            server.logout()


# =============================================================================
# 6. Handlers de comandos y mensajes
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
    Edita el mismo mensaje en lugar de crear uno nuevo.
    """
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()  # Contesta al CallbackQuery para remover el 'loading...'

    if query.data == "obtener_codigo":
        user_log(user_id, "Usuario seleccionó Obtener Código.")
        keyboard = [
            [InlineKeyboardButton("Cancelar", callback_data="cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text="Por favor, ingresa tu dirección de correo electrónico:",
            reply_markup=reply_markup
        )
        context.user_data['awaiting_email'] = True

    elif query.data == "help":
        user_log(user_id, "Usuario solicitó Ayuda.")
        keyboard = [
            [InlineKeyboardButton("Volver", callback_data="volver_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text=(
                "Este bot te ayuda a obtener códigos de verificación enviados a tu correo.\n\n"
                "1. **Escribe tu correo electrónico** cuando se te pida.\n"
                "2. **El bot buscará el correo más reciente** y te proporcionará el código de 6 dígitos.\n\n"
                "Si necesitas ayuda adicional, contáctanos."
            ),
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
        keyboard = [
            [InlineKeyboardButton("Cancelar", callback_data="cancel")]
        ]
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
        email_address = update.message.text.strip()
        user_id = update.effective_user.id

        user_log(user_id, f"Usuario solicitó código para el correo: {email_address}")

        # Verifica si el usuario tiene permiso
        if not user_has_access(user_id, email_address):
            user_log(user_id, "Acceso denegado (sin permisos).")
            await update.message.reply_text(
                "❌ **No tienes permiso para consultar este correo.**\n"
                "Pídele al administrador que te autorice.",
                parse_mode="Markdown"
            )
            context.user_data['awaiting_email'] = False
            return

        # Obtén servidor IMAP y contraseña
        imap_server, app_password = get_credentials(email_address)
        if not imap_server or not app_password:
            user_log(user_id, f"Correo '{email_address}' no encontrado o sin credenciales completas.")
            keyboard = [
                [
                    InlineKeyboardButton("Reintentar", callback_data="cambiar_correo"),
                    InlineKeyboardButton("Volver al Menú Principal", callback_data="volver_menu")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                "⚠️ **Correo no encontrado o credenciales incompletas en el sistema.**",
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            context.user_data['awaiting_email'] = False
            return

        await update.message.reply_text(
            "🔄 **Buscando tu código, por favor espera...**",
            parse_mode="Markdown"
        )

        code, minutes = get_verification_code(email_address, imap_server, app_password)
        if code:
            user_log(user_id, f"Código obtenido: {code} (hace {minutes} minutos).")
            await update.message.reply_text(
                f"✉️ **Tu código de verificación es:** `{code}`\n"
                f"⌛ **Recibido hace** {minutes} **minutos.**",
                parse_mode="Markdown"
            )
        else:
            user_log(user_id, "No se encontró código reciente o error en la conexión.")
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
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"Admin ejecutó /add_access con args {context.args}")

    if admin_user_id != ADMIN_ID:
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uso: /add_access <user_id> <correo1> [<correo2> ...]")
        return

    target_user_id = int(args[0])
    new_emails = args[1:]

    df_users = load_users()
    row_index = df_users.index[df_users['UserID'] == target_user_id].tolist()

    if not row_index:
        new_row = {
            'UserID': target_user_id,
            'Emails': ';'.join(new_emails)
        }
        df_users = pd.concat([df_users, pd.DataFrame([new_row])], ignore_index=True)
        save_users(df_users)
        await update.message.reply_text(
            f"✅ Se ha creado el usuario {target_user_id} con acceso a:\n" +
            "\n".join(new_emails)
        )
    else:
        idx = row_index[0]
        current_emails_str = df_users.at[idx, 'Emails']
        current_emails = set(e.strip().lower() for e in current_emails_str.split(';') if e.strip()) \
                         if isinstance(current_emails_str, str) else set()

        for mail in new_emails:
            current_emails.add(mail.lower())

        df_users.at[idx, 'Emails'] = ';'.join(sorted(current_emails))
        save_users(df_users)
        await update.message.reply_text(
            f"✅ Se ha actualizado el usuario {target_user_id}.\n"
            f"Accesos actuales: {df_users.at[idx, 'Emails']}"
        )


async def remove_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /remove_access <user_id> <correo1> [<correo2> ...]
    """
    admin_user_id = update.effective_user.id
    user_log(admin_user_id, f"Admin ejecutó /remove_access con args {context.args}")

    if admin_user_id != ADMIN_ID:
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Uso: /remove_access <user_id> <correo1> [<correo2> ...]")
        return

    target_user_id = int(args[0])
    emails_to_remove = args[1:]

    df_users = load_users()
    row_index = df_users.index[df_users['UserID'] == target_user_id].tolist()

    if not row_index:
        await update.message.reply_text(
            f"⚠️ El usuario {target_user_id} no existe en la base de datos."
        )
        return

    idx = row_index[0]
    current_emails_str = df_users.at[idx, 'Emails']
    if not current_emails_str or pd.isna(current_emails_str):
        await update.message.reply_text(
            f"El usuario {target_user_id} no tiene correos asignados actualmente."
        )
        return

    current_emails = set(e.strip().lower() for e in current_emails_str.split(';') if e.strip())
    removed = []
    for mail in emails_to_remove:
        mail_lower = mail.lower()
        if mail_lower in current_emails:
            current_emails.remove(mail_lower)
            removed.append(mail_lower)

    df_users.at[idx, 'Emails'] = ';'.join(sorted(current_emails)) if current_emails else ""
    save_users(df_users)

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

    if admin_user_id != ADMIN_ID:
        await update.message.reply_text("❌ No tienes permisos de administrador.")
        return

    df_users = load_users()
    if df_users.empty:
        await update.message.reply_text("No hay usuarios en la base de datos.")
        return

    msg = ["**Lista de Usuarios Autorizados**\n"]
    for _, row in df_users.iterrows():
        uid = row['UserID']
        emails = row['Emails'] if isinstance(row['Emails'], str) else ""
        msg.append(f"- **UserID**: `{uid}` | **Emails**: `{emails}`")

    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")


# =============================================================================
# 8. Main / Ejecución del bot
# =============================================================================

if __name__ == "__main__":
    # Inicializa colorama (importante en Windows para ANSI, etc.)
    colorama.init(autoreset=True)

    # Creamos un StreamHandler para la consola
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)

    # Definimos el formato de log (fecha, logger, nivel, mensaje)
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    # Asignamos nuestro formatter con colores
    console_handler.setFormatter(ColorfulFormatter(log_format))

    # Obtenemos el logger raíz
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # Construimos la aplicación
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Handlers básicos
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_buttons))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, email_input))
    application.add_handler(CommandHandler("cancel", cancel))

    # Handlers de administración
    application.add_handler(CommandHandler("add_access", add_access))
    application.add_handler(CommandHandler("remove_access", remove_access))
    application.add_handler(CommandHandler("list_users", list_users))

    # Handler de utilidad
    application.add_handler(CommandHandler("mi_id", mi_id))

    # Ejecutamos el bot
    application.run_polling()
