import os
import shutil
import sqlite3
import sys
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, messagebox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'clinica.db')
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)

def integrity_ok(path):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute('PRAGMA integrity_check').fetchone()
        return bool(row and str(row[0]).lower() == 'ok')
    finally:
        conn.close()

root = tk.Tk()
root.withdraw()
try:
    source = filedialog.askopenfilename(
        title='Seleccionar base de datos de ClínicaSV',
        filetypes=[('Base de datos SQLite', '*.db'), ('Todos los archivos', '*.*')]
    )
    if not source:
        sys.exit(0)
    if os.path.abspath(source) == os.path.abspath(DB_PATH):
        messagebox.showinfo('ClínicaSV', 'Seleccionaste la base de datos que ya está en uso.')
        sys.exit(0)
    if not integrity_ok(source):
        messagebox.showerror('ClínicaSV', 'La base de datos seleccionada no superó la comprobación de integridad de SQLite.')
        sys.exit(1)

    if os.path.exists(DB_PATH):
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup = os.path.join(BACKUP_DIR, f'antes_importar_{stamp}.db')
        shutil.copy2(DB_PATH, backup)

    shutil.copy2(source, DB_PATH)
    messagebox.showinfo(
        'ClínicaSV',
        'Base de datos importada correctamente.\n\n'
        'La copia anterior, si existía, quedó guardada en la carpeta backups.\n\n'
        'Cierra y vuelve a abrir ClínicaSV para usar los datos importados.'
    )
except Exception as exc:
    messagebox.showerror('ClínicaSV', f'No se pudo importar la base de datos:\n\n{exc}')
    sys.exit(1)
finally:
    try:
        root.destroy()
    except Exception:
        pass
