# ClínicaSV — despliegue en Render

## GitHub
Sube el contenido de esta carpeta a un repositorio privado o público. No incluyas una base `clinica.db` real, la carpeta `LICENCIAS_LOCAL` ni `private_key.pem`.

## Render
Crear un Web Service conectado al repositorio.
Build Command:
`pip install -r requirements.txt`
Start Command:
`gunicorn app:app --bind 0.0.0.0:$PORT`
Health Check:
`/health`

Variables de entorno:
- `DATABASE_URL`: cadena de conexión PostgreSQL de Neon (o del PostgreSQL que decidas usar).
- `SECRET_KEY`: puede generarse desde Render con `generateValue` o configurarse manualmente.
- `LICENSE_PUBLIC_KEY`: contenido de `license_public_key.pem` si quieres que la verificación de licencia quede declarada explícitamente como variable de entorno. La aplicación también puede leer la clave pública que está en el repositorio.
- `CLINICASV_ONLINE`: `1` para activar el flujo online.
- `DEMO_DATABASE_URL`: cadena PostgreSQL de una base separada para la DEMO.
- `CLINICASV_DEMO_HOURS`: `72` para una demostración de 3 días.

## Base de datos
Al iniciar con `DATABASE_URL`, ClínicaSV crea/actualiza las tablas necesarias en PostgreSQL. La base local SQLite no se sube al repositorio.

Para migrar datos de una instalación local se recomienda usar el respaldo completo de la aplicación. Antes de hacer una migración real, revisar la versión que protege licencia/configuración para que un respaldo antiguo no haga retroceder esos datos.

## Nota sobre el plan Free
Render ofrece Web Services y Postgres gratuitos para pruebas, pero su Postgres Free tiene 1 GB y expira después de 30 días; no debe usarse como almacén de producción. Los Web Services Free se suspenden tras 15 minutos sin tráfico y se reactivan al recibir una nueva solicitud.


### Flujo online V6.7.3

La versión online abre con una pantalla pública con **Probar DEMO**, **Iniciar sesión** y **Activar licencia**.

**DEMO de 72 horas (3 días):** durante la demostración se habilitan las funciones completas de la plataforma para probar altas, ediciones, consultas, agenda, recetas, medicamentos, presupuestos, cuenta, documentos y configuración. La DEMO usa una base de datos PostgreSQL separada mediante `DEMO_DATABASE_URL`, para que sus cambios no se mezclen con la base real. No deben introducirse datos reales de pacientes.

Al finalizar las 72 horas, la DEMO queda bloqueada y se muestra **Activar licencia**. La activación real continúa con archivo `.clsv` o código y después permite crear el usuario administrador.

### Neon: base de datos DEMO

Dentro del proyecto de Neon de CLINICASV1 se puede crear una base PostgreSQL adicional, por ejemplo `clinicasv1_demo`, desde el SQL Editor con:

`CREATE DATABASE clinicasv1_demo;`

Después usa la cadena de conexión de esa base como `DEMO_DATABASE_URL` en Render. Neon documenta el uso de bases adicionales dentro del mismo proyecto PostgreSQL.
