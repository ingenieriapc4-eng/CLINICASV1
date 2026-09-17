"""
Resetea la contraseña de un usuario en la base de datos LOCAL (SQLite)
de la Clínica, sin necesitar iniciar sesión en la app.

CÓMO USARLO:
1. Copia este archivo (reset_password_local.py) dentro de tu carpeta de
   la app local, por ejemplo:
   C:\\Clinica local\\reset_password_local.py
2. Asegúrate de que la app NO esté corriendo en ese momento (cierra la
   ventana negra / el proceso de Python), para evitar que ambos escriban
   al mismo tiempo.
3. Abre PowerShell en esa carpeta y corre:
       python reset_password_local.py
4. Sigue las instrucciones en pantalla.
"""
import sqlite3
import sys

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    print("Falta instalar 'werkzeug'. Corre primero: pip install werkzeug")
    sys.exit(1)

DB_PATH = "clinica.db"

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

users = cur.execute("SELECT username, role FROM users").fetchall()
if not users:
    print("No se encontró ningún usuario en la base de datos local.")
    sys.exit(1)

print("\nUsuarios encontrados en esta base de datos local:")
for u in users:
    print(f"  - {u['username']}  (rol: {u['role']})")

username = input("\nEscribe el nombre de usuario al que quieres cambiarle la contraseña: ").strip()
row = cur.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
if not row:
    print(f"No existe un usuario llamado '{username}'. Revisa mayúsculas/minúsculas y vuelve a intentar.")
    sys.exit(1)

new_password = input("Escribe la nueva contraseña que quieres usar: ").strip()
if len(new_password) < 4:
    print("La contraseña es muy corta, usa al menos 4 caracteres.")
    sys.exit(1)

cur.execute(
    "UPDATE users SET password_hash = ? WHERE id = ?",
    (generate_password_hash(new_password), row["id"]),
)
conn.commit()
conn.close()

print(f"\n✅ Listo. La contraseña del usuario '{username}' se cambió correctamente.")
print("Ya puedes abrir la app local normalmente e iniciar sesión con la nueva contraseña.")
