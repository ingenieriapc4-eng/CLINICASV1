V6.7 — Ficha del paciente y perfil profesional

V6.4.5 FIX: generador local de licencias con lanzamiento directo y corrección del BAT para rutas con espacios.

============================================================
CLÍNICA — GUÍA COMPLETA DE INSTALACIÓN Y USO
============================================================
Versión: CORREGIDA v3
Aplicación: Clínica médica / medicina general / especialidades

IMPORTANTE
----------
Esta aplicación es una herramienta administrativa y de apoyo al registro
clínico. No sustituye el criterio, diagnóstico, prescripción ni decisiones
del profesional de salud. Verifique siempre los datos antes de entregar
un documento al paciente.

La aplicación incluye un disclaimer en los documentos impresos. Puede
modificarse desde Configuración.

============================================================
1. INSTALACIÓN EN WINDOWS
============================================================

PASO 1 — Extraer el ZIP
------------------------
1. Descargue el archivo Clinica_Medica_CORREGIDA_v3.zip.
2. Clic derecho sobre el ZIP.
3. Seleccione "Extraer todo...".
4. Extraiga la carpeta completa. No ejecute archivos directamente desde
   dentro del ZIP.

PASO 2 — Instalar dependencias
------------------------------
Dentro de la carpeta extraída, ejecute una sola vez:

    Instalar Clinica.bat

Este archivo crea/usa el entorno virtual de Python e instala las
bibliotecas necesarias, incluyendo Flask, Waitress, PyWebView y Pillow.

Si Windows pregunta con qué programa abrir un .bat, no es necesario elegir
un programa. Abra PowerShell en esa carpeta y ejecute:

    & ".\Instalar Clinica.bat"

PASO 3 — Abrir Clínica sin ventana negra
-----------------------------------------
Para el uso diario, haga doble clic en:

    Abrir Clinica Silencioso.vbs

Este archivo inicia Clínica sin dejar una ventana de PowerShell/CMD visible.

También existe:

    Iniciar Clinica Silencioso.bat

pero el VBS silencioso es el recomendado para el escritorio.

PASO 4 — Cerrar Clínica
-----------------------
Use el botón normal de cierre de la ventana de Clínica.
También está disponible:

    Cerrar Clinica.bat

============================================================
2. CREAR UN EXE REAL
============================================================

Si desea que el personal abra Clínica con un icono como cualquier otro
programa de Windows:

1. Ejecute:

    CREAR_EXE.bat

2. Espere a que termine.
3. El ejecutable se generará en:

    dist\Clinica.exe

4. Cree un acceso directo de ese EXE en el escritorio.

NOTA: La creación del EXE se realiza en Windows porque PyInstaller debe
construir el ejecutable para el sistema operativo correspondiente.

El archivo installer.iss es una plantilla para crear posteriormente un
instalador formal con Inno Setup.

============================================================
3. BASE DE DATOS Y DATOS EXISTENTES
============================================================

La base de datos local es:

    clinica.db

NO BORRE este archivo si contiene los datos reales de la clínica.

La aplicación crea automáticamente las tablas que necesita cuando inicia.

Para mayor seguridad:
- Haga respaldos periódicos.
- Mantenga una copia fuera del equipo principal.
- No comparta públicamente la base de datos.

En Configuración puede utilizar el sistema de respaldos de la aplicación.

============================================================
4. IMPRESIÓN
============================================================

La impresión está diseñada para mostrar primero una vista previa dentro
de Clínica. Desde esa vista puede seleccionar "Imprimir" o "Cerrar".

Se contemplan documentos como:
- Ficha / información del paciente.
- Comprobante de cita.
- Comprobante de tratamiento.
- Estado de cuenta.
- Resumen de Contabilidad.
- Nota de abono.
- Presupuestos.
- Historia clínica y evolución.
- Diagnóstico clínico.
- Receta / medicamentos.

IMPORTANTE SOBRE EL CIERRE DE LA IMPRESIÓN
-------------------------------------------
La aplicación fue ajustada para que la vista previa de impresión sea parte
de Clínica y para que el cierre de la ventana de impresión no deba cerrar
la aplicación principal.

Si Windows presenta una ventana de impresión del sistema, cierre o cancele
esa ventana con normalidad y vuelva a la vista de Clínica.

Si el equipo todavía cierra Clínica al cancelar la impresión, anote:
- qué documento estaba imprimiendo;
- si pulsó Imprimir, Cancelar o X;
- qué versión de Windows utiliza.

Con esos datos se puede revisar específicamente el motor WebView de ese PC.

============================================================
5. HISTORIA CLÍNICA, DIAGNÓSTICO Y RECETA
============================================================

La historia clínica permite registrar, según corresponda:
- especialidad;
- motivo de consulta;
- presión arterial;
- temperatura;
- pulso;
- peso;
- talla;
- saturación de oxígeno;
- examen físico / hallazgos;
- diagnóstico clínico;
- plan o tratamiento;
- medicamentos y dosis;
- indicaciones;
- seguimiento.

La impresión de Historia clínica y evolución reúne la información clínica
registrada para cada consulta, en lugar de mostrar solamente un concepto
breve.

La opción "Ver" muestra la consulta con mayor detalle y puede incluir los
datos generales relevantes del paciente, alergias y antecedentes.

============================================================
6. FOTOGRAFÍA DEL PACIENTE
============================================================

La aplicación permite cambiar la fotografía desde el perfil del paciente.

Formatos admitidos:
- JPG / JPEG
- PNG
- WEBP
- GIF
- BMP
- TIFF

La aplicación utiliza Pillow para procesar y optimizar las imágenes.

Recomendación: use una fotografía clara, frontal y de tamaño moderado.
No utilice fotografías innecesariamente grandes.

============================================================
7. CONTABILIDAD, TRATAMIENTOS Y PAGOS
============================================================

La sección anteriormente llamada "Facturación" se denomina:

    CONTABILIDAD

Permite trabajar con:
- tratamientos/cargos;
- monto total;
- monto pagado;
- saldo pendiente;
- presupuestos;
- pagos parciales de presupuestos;
- notas de abono;
- impresión de estados y comprobantes.

Cuando se registra una consulta con tratamiento y costo, puede registrarse
el cobro como pagado o generar un presupuesto pendiente, según la opción
seleccionada.

============================================================
8. TRABAJO EN RED — DOS O MÁS COMPUTADORAS
============================================================

La computadora principal actúa como servidor de Clínica.

PASO 1
------
En la PC principal abra Clínica con:

    Abrir Clinica Silencioso.vbs

PASO 2
------
Abra:

    CONFIGURAR_FIREWALL.bat

y permita el puerto TCP 5050 cuando Windows lo solicite.

PASO 3
------
En la PC principal obtenga su dirección IP local. Normalmente será algo
parecido a:

    192.168.1.100

PASO 4
------
En el otro computador, conectado a la misma red Wi-Fi o cable, abra Chrome,
Edge u otro navegador y escriba:

    http://192.168.1.100:5050

Sustituya 192.168.1.100 por la IP real de la computadora principal.

IMPORTANTE
----------
La PC principal debe permanecer encendida y Clínica debe estar ejecutándose
para que los demás equipos puedan conectarse.

============================================================
9. INSTALAR / USAR EN TABLETA O TELÉFONO ANDROID
============================================================

La forma recomendada para una instalación local es utilizar la aplicación
como una aplicación web desde la misma red.

REQUISITO
---------
La tableta y la computadora principal deben estar conectadas a la misma
red Wi-Fi si la Clínica está instalada solamente en la PC local.

PASO 1
------
En la PC principal inicie Clínica.

PASO 2
------
Averigüe la IP local de la PC, por ejemplo:

    192.168.1.100

PASO 3
------
En la tableta Android abra Google Chrome y escriba:

    http://192.168.1.100:5050

PASO 4
------
Inicie sesión normalmente.

PASO 5 — Crear acceso en la pantalla principal
-----------------------------------------------
En Chrome, abra el menú de tres puntos y busque una opción como:

    Instalar aplicación

ó

    Añadir a pantalla de inicio

El texto exacto puede variar según la versión de Android/Chrome.

Google documenta que las aplicaciones web pueden instalarse desde Chrome y
quedar accesibles desde la pantalla de inicio. Consulte también la ayuda
oficial de Chrome si el nombre del menú cambia en su dispositivo.

IMPORTANTE SOBRE LA TABLETA
---------------------------
Esto NO significa que el servidor esté instalado dentro de la tableta.
La PC principal sigue ejecutando Clínica y la tableta funciona como cliente.

Si desea que la tableta funcione sin depender de una PC encendida, habrá que
montar Clínica en un servidor/hosting accesible por Internet o en otro equipo
servidor. Eso es un proceso diferente.

============================================================
10. USO DEL LOGO Y DISCLAIMER
============================================================

En Configuración se puede modificar:
- nombre de la clínica;
- logo;
- tamaño del logo;
- texto del disclaimer;
- otros parámetros de la aplicación.

Texto base sugerido del disclaimer:

"La información registrada en este sistema es de uso administrativo y
clínico. Verifique los datos antes de emitir o entregar documentos. La
información clínica no sustituye el criterio profesional del médico."

Este texto es una sugerencia operativa y debe adaptarse a las necesidades
legales y profesionales de la clínica.

============================================================
11. SI WINDOWS VUELVE A PEDIR "¿CON QUÉ PROGRAMA QUIERES ABRIRLO?"
============================================================

No seleccione Word, Bloc de notas, Chrome ni otro programa para abrir un
archivo .bat o .vbs.

Abra PowerShell en la carpeta de Clínica y ejecute:

    & ".\Instalar Clinica.bat"

para instalar, o:

    & ".\Iniciar Clinica.bat"

para iniciar manualmente.

Para el uso normal, utilice:

    Abrir Clinica Silencioso.vbs

============================================================
12. SOLUCIÓN DE PROBLEMAS
============================================================

ERROR: ModuleNotFoundError: No module named 'webview'
---------------------------------------------------------
Ejecute:

    .\venv\Scripts\python.exe -m pip install "pywebview>=5.0,<6.0"

ERROR AL CAMBIAR FOTO
---------------------
Verifique que la instalación incluya Pillow. Ejecute:

    .\venv\Scripts\python.exe -m pip install "Pillow>=10.0,<13.0"

LA APP NO ABRE EN OTRO PC/TABLETA
---------------------------------
1. Verifique que ambos dispositivos estén en la misma red.
2. Verifique que Clínica esté abierta en la PC principal.
3. Ejecute CONFIGURAR_FIREWALL.bat.
4. Verifique la IP de la PC.
5. Pruebe http://IP-DE-LA-PC:5050.

NO APARECE LA FOTO
------------------
Verifique que la carpeta:

    uploads\photos

exista y que el usuario de Windows tenga permisos de escritura.

============================================================
13. RECOMENDACIÓN DE SEGURIDAD
============================================================

Los datos clínicos son sensibles. Recomendaciones:
- Proteja Windows con contraseña.
- Use cuentas individuales para el personal.
- No deje la sesión abierta sin supervisión.
- Haga respaldos.
- Mantenga Windows y el navegador actualizados.
- No exponga el puerto 5050 directamente a Internet sin una arquitectura
  de seguridad adecuada.
- Verifique cada documento antes de entregarlo al paciente.

============================================================
14. ARCHIVOS IMPORTANTES DE LA CARPETA
============================================================

app.py                         Servidor y lógica de la aplicación
clinica.db                     Base de datos local
requirements.txt               Dependencias Python
templates\index.html           Interfaz principal
Instalar Clinica.bat           Instalación inicial
Abrir Clinica Silencioso.vbs   Inicio diario sin consola
Iniciar Clinica.bat            Inicio manual con consola
Cerrar Clinica.bat             Cierre auxiliar
CREAR_EXE.bat                  Generación del EXE
CONFIGURAR_FIREWALL.bat       Configuración del puerto 5050
installer.iss                  Plantilla de instalador Windows
uploads\                      Fotos, documentos y branding
backups\                      Copias de seguridad locales

============================================================
FIN DE LA GUÍA
============================================================


==================== ACTUALIZACIÓN VISUAL v4 ====================

La versión v4 moderniza la interfaz sin cambiar la lógica clínica: tipografía más actual, paleta azul/turquesa, botones redondeados tipo píldora, tarjetas con sombras suaves, campos con mejor enfoque, navegación con degradado y mejor adaptación visual. Los colores configurables siguen disponibles en Configuración.

IMPRESIÓN: la vista previa integrada ahora tiene una X visible para cerrar solamente la vista previa. La confirmación de salida con '¿Desea salir?' se mantiene asociada al cierre de la ventana principal de la aplicación. No se debe usar la X de la vista previa para cerrar la aplicación completa.

Si al pulsar 'Imprimir' y luego cancelar/cerrar el diálogo nativo de impresión Windows todavía se cierra la aplicación completa, esto corresponde al comportamiento del motor de ventana nativa y debe corregirse separando la ventana de impresión del contenedor principal en una siguiente revisión.

============================================================
V5 - REORGANIZACION CLINICA
============================================================

Esta versión reorganiza la ficha del paciente para que las consultas no hagan
la página interminable. Las consultas aparecen como filas compactas y cada una
se abre con "Ver" para mostrar el detalle.

Nuevas funciones V5:
- Historial de consultas compacto y desplegable.
- Filtro por especialidad y búsqueda dentro del historial.
- Plantillas iniciales de campos clínicos por especialidad.
- Cardiología incluye síntomas cardiovasculares, examen cardiovascular y estudios.
- Plantillas preparadas para Pediatría, Ginecología, Dermatología, Neurología y Traumatología.
- Los datos específicos de especialidad se guardan junto con la consulta.
- Opciones de impresión: consulta actual, receta, historia clínica completa y resumen.
- La impresión se abre en una ventana independiente; la X de esa ventana cierra solo la impresión.
- Se agrega sección Vademécum y carpeta local `vademecum` como estructura de referencia.

IMPORTANTE SOBRE EL VADEMECUM
----------------------------
La carpeta `vademecum` contiene una plantilla, no una base farmacológica clínica
completa. Para producción se recomienda cargar una fuente farmacológica validada,
con versión y fecha de actualización. No se deben aceptar dosis automáticas sin
verificar la fuente y el contexto clínico.

La ventana principal de Clínica conserva su confirmación de salida. Cancelar o
cerrar una ventana secundaria de impresión no debe cerrar la aplicación principal.


V6 — PRIMERA ETAPA DE REDISEÑO VISUAL
- Interfaz más compacta y uniforme en Dashboard, Pacientes, Agenda y Configuración.
- Menor espacio vertical en tarjetas, formularios y estados vacíos.
- Agenda: el bloque sin citas ocupa mucho menos espacio para mantener visible el calendario mensual.
- Calendario mensual con celdas más compactas y legibles.
- Pacientes y estadísticas con tarjetas más bajas y consistentes.
- Configuración con mayor aprovechamiento horizontal.
- Se conserva la funcionalidad de V5; esta etapa prioriza la base visual antes de integrar los siguientes módulos de seguridad/licencia.


================ V6.1 — ARRANQUE Y ORGANIZACIÓN =================

1) ARRANQUE SIMPLE

Para el uso diario, el usuario debe abrir únicamente: Clinica.vbs
- Si existe Clinica.exe, abre directamente el EXE sin consola.
- Si todavía no existe el EXE, usa el arranque silencioso de desarrollo.
Los demás BAT/VBS son herramientas técnicas y no son necesarios para el uso diario.

En la versión distribuible final se recomienda entregar un acceso directo a Clinica.exe;
Clinica.vbs queda como alternativa de compatibilidad.

2) PERFIL PROFESIONAL

Configuración permite guardar la especialidad predeterminada. Las nuevas consultas la
cargan automáticamente. Las consultas antiguas conservan su especialidad histórica.

3) DISEÑO V6.1

Se compactaron encabezados, tarjetas, formularios, pacientes y agenda para reducir
espacios vacíos sin reducir la legibilidad. La Agenda prioriza que el calendario mensual
permanezca visible en una pantalla normal.

NOTA: Un EXE real de producción debe generarse en Windows mediante CREAR_EXE.bat.
No se incluye un EXE compilado desde este entorno.

============================================================
V6.2 — SEGURIDAD, BASE LEGAL Y CENTRO DE LICENCIA
============================================================

1) ACEPTACIÓN LEGAL POR VERSIÓN
Al iniciar sesión, el sistema verifica la versión vigente de los términos. Si el usuario todavía no aceptó la versión actual, deberá leerla y aceptarla antes de continuar.
La aceptación se registra por usuario con versión y fecha/hora.
Cuando cambie la versión de los términos, el sistema vuelve a solicitar la aceptación.
La negativa de términos no borra ni bloquea los datos clínicos.

IMPORTANTE: estos textos son una base operativa del software y no constituyen una certificación de cumplimiento legal. Para producción, revise el texto con asesoría jurídica de El Salvador y adapte el contenido a la normativa aplicable.

2) RECUPERACIÓN DE CUENTAS
Cada usuario puede configurar una pregunta de seguridad que funciona sin internet.
También puede generar un código local de recuperación de un solo uso. La aplicación solo almacena una representación protegida del código y lo muestra únicamente cuando se genera.
Generar otro código invalida el anterior.
Al usar el código para recuperar la cuenta, el código queda invalidado.
La recuperación por correo continúa como método de respaldo cuando existe configuración de correo.

3) CENTRO DE LICENCIA
Configuración → Licencia muestra estado, tipo, titular, vencimiento, ID de licencia e instalación.
La estructura V6.2 está preparada para tokens firmados Ed25519 y vinculación a instalación/equipo.
La clave privada de firma NO debe estar dentro del EXE ni del código fuente. La aplicación solo debe recibir la clave pública del proveedor mediante LICENSE_PUBLIC_KEY.

La licencia no debe eliminar ni bloquear los datos clínicos al vencer. El cliente mantiene el control y la responsabilidad sobre sus datos y respaldos.

4) RESPALDOS
Continúa disponible el respaldo manual, automático, descargable y de carpeta sincronizada. Mantenga respaldos fuera del equipo principal y compruebe periódicamente que pueden restaurarse.

5) CAMBIO IMPORTANTE DE ARRANQUE
Se dejó el import de pywebview para el arranque únicamente. Esto permite que el servidor Flask pueda iniciar en un entorno donde pywebview no esté instalado, por ejemplo durante pruebas o en un servidor web.
La ventana de escritorio sigue utilizando pywebview cuando está disponible.

6) EXE / INSTALADOR
El EXE definitivo y el instalador siguen pendientes deliberadamente. V6.2 no depende del empaquetado para funcionar o probar las funciones anteriores.

============================================================
V6.3 — CUSTODIA DE DATOS, INTEGRIDAD Y AUDITORÍA
============================================================

1) VERIFICACIÓN DE RESPALDOS
Los respaldos SQLite locales nuevos incluyen una referencia SHA-256. Desde Configuración → Respaldo puedes ejecutar “Verificar” para comprobar la integridad interna de SQLite y, cuando exista una referencia, comparar el hash. Los respaldos antiguos que no tenían hash pueden recibir una referencia en la primera verificación exitosa.

2) REGISTRO DE ACTIVIDAD
Se añade un registro local para acciones administrativas y de seguridad como creación, descarga, verificación y eliminación de respaldos, además de aceptación legal y activación de licencia cuando corresponda. No se guardan contraseñas, códigos de recuperación ni claves privadas.

3) CUSTODIA Y RECUPERACIÓN
La restauración de un respaldo sigue creando primero una copia del estado actual. Mantenga copias fuera del equipo principal y pruebe periódicamente que una copia puede recuperarse. La licencia del software no debe utilizarse para bloquear la posesión o recuperación de los datos clínicos.

4) ALCANCE LEGAL
La auditoría y los mecanismos de respaldo son herramientas operativas. No constituyen por sí mismos una certificación de cumplimiento legal, seguridad informática o normativa sanitaria. Revise las políticas y el texto legal con asesoría profesional en la jurisdicción donde opere la clínica.


V6.4 — Licencias locales y módulo Medicamentos
-----------------------------------------------
- La activación recomendada usa un archivo .clsv: Configuración → Licencia → Activar archivo.
- También se puede pegar el código firmado como alternativa.
- El generador está en LICENCIAS_LOCAL y es SOLO PARA USO INTERNO. No distribuir esa carpeta a clientes.
- license_public_key.pem es la clave pública que verifica las licencias.
- Más adelante se puede mover la emisión al servidor (Render + Neon) sin cambiar la interfaz de activación.
- El menú Vademécum ahora se presenta como “Medicamentos” y carga el catálogo local de vademecum/medicamentos.json.
- Términos y condiciones están disponibles mediante enlace en el pie y también en /terms.


V6.4.1 - Generador local de licencias
El archivo "LICENCIAS_LOCAL\Generar Licencia.bat" usa el entorno virtual de la aplicacion cuando existe. Si no existe, intenta usar Python instalado y preparar automaticamente el componente cryptography necesario para firmar licencias.


V6.4.2 — ACTIVACIÓN INICIAL SIMPLIFICADA
- En la primera configuración, la activación recomendada es importar un archivo .clsv.
- El código de activación se mantiene solo como compatibilidad.
- La licencia importada queda guardada automáticamente y no se vuelve a pedir al crear el administrador.
- El pie de la aplicación y la configuración inicial usan un enlace limpio a “Términos y condiciones”.
- CREAR_EXE.bat incluye la clave pública necesaria para verificar licencias locales dentro del ejecutable.


V6.4.4 FIX — licencia en EXE
- Corregida la lectura de license_public_key.pem cuando ClínicaSV se ejecuta como EXE one-file de PyInstaller.
- CREAR_EXE.bat ahora embebe la clave pública y deja una copia auxiliar junto a dist\Clinica.exe.
- La clave privada sigue siendo exclusiva de LICENCIAS_LOCAL y NO_DISTRIBUIR.

V6.5 — ESTABILIDAD, IMPRESIÓN Y DATOS
- La ventana de impresión ya no requiere sesión al abrir /print-window/<token>; usa un token aleatorio de vida corta generado por una acción autenticada. Esto evita que una ventana de impresión vuelva al login.
- Se eliminaron las referencias clínicas dentales visibles y los permisos antiguos de odontograma. La Historia Clínica médica usa el permiso de pacientes.
- Nuevo Importar Base de Datos.bat: abre un selector para traer una clinica.db existente. Antes de reemplazar la base actual crea automáticamente una copia en backups/ y valida PRAGMA integrity_check.
- Para actualizar una instalación, se recomienda cerrar ClínicaSV, conservar una copia de clinica.db y usar la herramienta de importación cuando la nueva carpeta tenga otra base inicial.
- El EXE utiliza la base de datos situada junto al ejecutable. Para una futura distribución comercial se deberá incluir un instalador/actualizador que conserve automáticamente los datos.

V6.6 — USUARIOS Y SEGURIDAD
- Los usuarios existentes se conservan; esta versión refuerza su administración sin crear un sistema paralelo.
- Una cuenta puede quedar activa o desactivada. Desactivar no borra su historial ni elimina datos clínicos.
- El administrador puede reactivar usuarios.
- El administrador puede marcar una cuenta para exigir cambio de contraseña en el próximo acceso.
- Si una contraseña se restablece desde la administración, el usuario debe cambiarla al entrar.
- Se registra el último acceso del usuario.
- Se registra actividad administrativa y de seguridad relacionada con accesos y gestión de cuentas.
- Se mantiene la recuperación mediante pregunta de seguridad y código local.
- Se mantiene el control de permisos por sección.
- La cuenta del administrador actual no puede desactivarse desde su propia sesión y siempre debe quedar al menos un administrador activo.
\n\n---\nV6.7 — Reorganización de ficha del paciente y documentos\n- Resumen compacto.\n- Consultas unifican historia clínica, diagnóstico y tratamiento.\n- Receta rápida y acceso directo a impresión.\n- Cuenta reúne presupuestos y movimientos económicos.\n- Perfil profesional para recetas e impresiones con nombre, especialidad, registro, contacto, dirección, encabezado y pie.\n

---
V6.7.1 FIX — Corrección de carga de ficha
- Al abrir un paciente, la última consulta ahora se carga desde su propia historia clínica, evitando mostrar temporalmente información del paciente anterior.
- Se mantiene la reorganización compacta de Resumen, Consultas y Cuenta.
