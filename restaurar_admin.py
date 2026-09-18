"""
Restaura (o crea) un usuario ADMINISTRADOR COMPLETO en la base de datos
local de ClinicaSV, sin necesitar iniciar sesión. Útil si un usuario quedó
con permisos restringidos por error, o si necesitas recuperar el acceso.

CÓMO USARLO:
1. Cierra ClinicaSV por completo (que no quede ningún proceso corriendo).
2. Guarda este archivo en la MISMA carpeta donde está tu app.py y tu
   archivo de base de datos (normalmente algo como clinica.db o similar).
3. Abre PowerShell en esa carpeta y corre:
       python restaurar_admin.py
4. Sigue las instrucciones en pantalla.
"""
import sqlite3
import sys
import uuid
from datetime import datetime

try:
    from werkzeug.security import generate_password_hash
except ImportError:
    print("Falta instalar 'werkzeug'. Corre primero: pip install werkzeug")
    sys.exit(1)

# Si tu archivo de base de datos tiene otro nombre, cámbialo aquí:
DB_CANDIDATES = ["clinica.db", "clinicasv.db", "database.db", "app.db"]

import os
db_path = None
for name in DB_CANDIDATES:
    if os.path.exists(name):
        db_path = name
        break

if not db_path:
    db_path = input("No encontré el archivo .db automáticamente. Escribe su nombre exacto: ").strip()
    if not os.path.exists(db_path):
        print(f"No se encontró el archivo '{db_path}' en esta carpeta.")
        sys.exit(1)

print(f"Usando base de datos: {db_path}\n")

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

users = cur.execute("SELECT id, username, role, permissions FROM users").fetchall()
print("Usuarios actuales en el sistema:")
for u in users:
    print(f"  - {u['username']}  (rol: {u['role']}, permisos: {u['permissions']})")

print("\n¿Qué quieres hacer?")
print("1) Restaurar TODOS los permisos de un usuario existente (convertirlo en admin completo)")
print("2) Crear un usuario NUEVO como administrador completo")
opcion = input("Escribe 1 o 2: ").strip()

if opcion == "1":
    username = input("Nombre del usuario a restaurar: ").strip()
    row = cur.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if not row:
        print(f"No existe un usuario llamado '{username}'.")
        sys.exit(1)
    cur.execute(
        "UPDATE users SET role = 'admin', permissions = 'all' WHERE id = ?",
        (row["id"],)
    )
    conn.commit()
    print(f"\n✅ Listo. El usuario '{username}' ahora es administrador completo (todos los permisos).")

elif opcion == "2":
    username = input("Nombre de usuario nuevo: ").strip()
    existing = cur.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        print("Ya existe un usuario con ese nombre. Usa la opción 1 para restaurarlo en vez de crear otro.")
        sys.exit(1)
    password = input("Contraseña para el nuevo usuario: ").strip()
    if len(password) < 6:
        print("La contraseña es muy corta, usa al menos 6 caracteres.")
        sys.exit(1)
    cur.execute(
        "INSERT INTO users (id, username, password_hash, role, permissions, created_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, username, generate_password_hash(password), "admin", "all",
         datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    )
    conn.commit()
    print(f"\n✅ Listo. Se creó el usuario '{username}' como administrador completo.")

else:
    print("Opción no válida.")
    sys.exit(1)

conn.close()
print("\nYa puedes abrir ClinicaSV normalmente e iniciar sesión.")
