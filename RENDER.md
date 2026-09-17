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

## Base de datos
Al iniciar con `DATABASE_URL`, ClínicaSV crea/actualiza las tablas necesarias en PostgreSQL. La base local SQLite no se sube al repositorio.

Para migrar datos de una instalación local se recomienda usar el respaldo completo de la aplicación. Antes de hacer una migración real, revisar la versión que protege licencia/configuración para que un respaldo antiguo no haga retroceder esos datos.

## Nota sobre el plan Free
Render ofrece Web Services y Postgres gratuitos para pruebas, pero su Postgres Free tiene 1 GB y expira después de 30 días; no debe usarse como almacén de producción. Los Web Services Free se suspenden tras 15 minutos sin tráfico y se reactivan al recibir una nueva solicitud.
