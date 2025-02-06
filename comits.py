import os
import subprocess
from datetime import datetime

# Configuración
repo_path = "C:\\Users\\Administrator\\Documents\\GitHub\\Bot_Telegram_Disney"
  # Cambia esto a la ruta de tu repo
commit_message = f"Commit automático {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
branch_name = "main"  # Cambia esto si usas otro branch

def run_git_command(command):
    """Ejecuta un comando git en el directorio del repositorio."""
    result = subprocess.run(command, shell=True, cwd=repo_path, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)

# Pasos de Git
run_git_command("git add .")
run_git_command(f'git commit -m "{commit_message}"')
run_git_command(f"git push origin {branch_name}")
