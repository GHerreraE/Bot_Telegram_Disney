# **Bot de Telegram para Verificación de Códigos**

## **1. Instalación y Configuración en Windows**

### **Requisitos Previos**

- **Python 3.8 o superior** instalado. [Descargar Python](https://www.python.org/downloads/).
- Acceso al terminal de Windows (**CMD** o **PowerShell**).
- Bibliotecas necesarias:
  - `python-telegram-bot`
  - `pandas`
  - `openpyxl`
  - `colorama`
  - `beautifulsoup4`

### **Pasos de Instalación**

1. **Descargar el código:**

   - Guarda el script del bot en una carpeta, por ejemplo: `C:\TelegramBot`.

2. **Instalar Python y bibliotecas:**

   - Verifica que Python esté instalado ejecutando:
     ```bash
     python --version
     ```
   - Instala las bibliotecas necesarias ejecutando:
     ```bash
     pip install python-telegram-bot pandas openpyxl colorama beautifulsoup4
     ```

3. **Configurar los archivos necesarios:**

   - En la carpeta del bot, crea los siguientes archivos:
     - **`admin_id.txt`:**
       Incluye el ID de Telegram del administrador. Ejemplo:
       ```
       123456789
       ```
     - **`token.txt`:**
       Guarda el token del bot obtenido de **BotFather**. Ejemplo:
       ```
       123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
       ```
     - **`dis.xlsx`:**
       Archivo Excel con las columnas `Correo` y `IMAP` para vincular los correos con contraseñas IMAP. Ejemplo:
       | Correo | IMAP |
       |---------------------|----------------|
       | ejemplo@gmail.com | contraseña123 |
     - **`usuarios.xlsx`:**
       Archivo Excel vacío con las columnas `UserID` y `Emails`. Ejemplo:
       | UserID | Emails |
       |----------|---------------------|
       | 12345678 | correo1@gmail.com |

4. **Ejecutar el bot:**
   - Ve a la carpeta del bot y abre el terminal.
   - Ejecuta:
     ```bash
     python bot.py
     ```
   - El bot estará activo y listo para recibir comandos.

---

## **2. Instrucciones para el Administrador**

### **Comandos Disponibles para el Administrador**

1. **Agregar accesos:**

   - Comando:
     ```
     /add_access <user_id> <correo1> [<correo2> ...]
     ```
   - Ejemplo:
     ```
     /add_access 12345678 correo1@gmail.com correo2@gmail.com
     ```
   - Esto otorga acceso a los correos `correo1@gmail.com` y `correo2@gmail.com` al usuario con ID `12345678`.

2. **Eliminar accesos:**

   - Comando:
     ```
     /remove_access <user_id> <correo1> [<correo2> ...]
     ```
   - Ejemplo:
     ```
     /remove_access 12345678 correo1@gmail.com
     ```
   - Esto elimina el acceso al correo `correo1@gmail.com` para el usuario `12345678`.

3. **Listar usuarios y accesos:**

   - Comando:
     ```
     /list_users
     ```
   - Muestra todos los usuarios autorizados y sus correos vinculados.

4. **Conocer tu ID de Telegram:**
   - Comando:
     ```
     /mi_id
     ```
   - Devuelve tu ID de Telegram.

---

## **3. Instrucciones para los Usuarios**

### **Comandos Básicos**

1. **Iniciar el bot:**

   - Comando:
     ```
     /start
     ```
   - Muestra el menú principal con opciones para obtener códigos de verificación o solicitar ayuda.

2. **Cancelar operaciones activas:**

   - Comando:
     ```
     /cancel
     ```
   - Detiene cualquier operación activa, como ingresar correos.

3. **Conocer tu ID de Telegram:**
   - Comando:
     ```
     /mi_id
     ```
   - Devuelve tu ID de Telegram.

### **Obtener un Código de Verificación**

1. Ejecuta `/start` y selecciona la opción **Obtener Código**.
2. Ingresa tu correo electrónico autorizado.
3. El bot buscará en tu correo vinculado y enviará el código de verificación.

---

## **4. Ejecución del Bot**

Cada vez que quieras ejecutar el bot en Windows:

1. Abre el terminal.
2. Navega a la carpeta del bot:
   ```bash
   cd C:\TelegramBot
   ```
