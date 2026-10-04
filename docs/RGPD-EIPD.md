# Hoja de protección de datos (RGPD) y apoyo a la EIPD

Documento informativo para el cliente final y su **Delegado de Protección de Datos (DPO)**.
Describe qué datos trata VMS Multimarca y cómo, para que el responsable del tratamiento pueda
completar su registro de actividades y su **Evaluación de Impacto (EIPD)**. **No es asesoramiento
jurídico**: las bases legales y plazos indicados son propuestas que debe validar el DPO del
cliente.

## 1. Partes

| Papel | Quién | Notas |
|---|---|---|
| Responsable del tratamiento | la cadena de supermercados (cliente final) | decide fines y medios |
| Encargado del tratamiento | el integrador que instala y mantiene el sistema | contrato de encargo (art. 28 RGPD) |
| Subencargado (si aplica) | el desarrollador del software, solo si accede a los sistemas para soporte | autorización previa y por escrito del responsable |
| Subencargado (si se activa) | proveedor LLM configurable del informe semanal | solo recibe cifras agregadas anónimas (ver §4) |
| Subencargado (si se activa) | Telegram (avisos de cola) | solo texto con números, sin imágenes ni personas |

## 2. Tratamientos

### 2.1 Videovigilancia (vista en vivo, grabación, reproducción)

| Aspecto | Detalle |
|---|---|
| Finalidad | seguridad de personas, bienes e instalaciones |
| Datos | imagen de personas (clientes, trabajadores, proveedores) captada por las cámaras |
| Base legal sugerida | interés público / interés legítimo en la seguridad (art. 6.1.e/f RGPD y art. 22 LOPDGDD) |
| Plazo | **máximo 1 mes** (art. 22.3 LOPDGDD), salvo conservación para acreditar hechos graves ante autoridades. Configurable en *Ajustes → Retención* (por defecto 30 días) |
| Dónde | disco del PC de cada tienda (no sale de la tienda salvo acceso remoto por VPN) |
| Información | carteles de zona videovigilada con la identidad del responsable y cómo ejercer derechos (art. 22.4 LOPDGDD), más la información adicional disponible |
| Control laboral | si se usa para control de trabajadores, informar previamente y de forma expresa a la plantilla y sus representantes (art. 89 LOPDGDD). **El sistema no está diseñado ni configurado para ello** |

### 2.2 Analítica de afluencia (conteo de puerta y ocupación de cola)

| Aspecto | Detalle |
|---|---|
| Finalidad | dimensionar la apertura de cajas y conocer la afluencia por franja horaria |
| Qué se procesa | el subflujo de vídeo de la cámara de puerta y de cajas, **en memoria**, en el PC de la tienda |
| Qué se guarda | **solo números agregados**: entradas y salidas por minuto, personas en la zona de cola por minuto (media, máximo), alertas de cola (inicio, fin, pico) |
| Qué **no** se guarda | imágenes, vídeo, recortes, identificadores de personas, trayectorias, rasgos físicos, edad, género. Cada imagen se descarta en cuanto se cuenta |
| Reconocimiento facial | **no existe** en el sistema. El detector solo reconoce la clase «persona» |
| Trabajadores | las zonas se dibujan sobre el área de clientes (cola), **no** sobre los puestos de caja; no se mide productividad ni presencia de trabajadores |
| Seguimiento | identificadores numéricos temporales, solo en memoria, que desaparecen en segundos; necesarios para no contar dos veces a la misma persona |
| Base legal sugerida | interés legítimo (art. 6.1.f) en la gestión operativa; al no conservarse datos personales, el resultado (conteos) es información anónima. El DPO debe valorar la ponderación del procesamiento momentáneo de la imagen |
| Dónde | el procesamiento en el PC de la tienda; los conteos en la base de datos central por VPN |
| Plazo de los conteos | al ser anónimos no tienen límite legal; se propone 25 meses para comparar años |

Recomendaciones de instalación que reducen el impacto:
- Cámara de puerta **cenital** (mirando hacia abajo): no capta caras y cuenta mejor.
- Subflujo de baja resolución (640×360) para la analítica.
- Información en el cartel de videovigilancia de que se realiza un **conteo anónimo de afluencia**.

### 2.3 Panel central y latidos

| Aspecto | Detalle |
|---|---|
| Datos | estado técnico de cada tienda (versión, cámaras con vídeo, disco, temperatura), conteos agregados, informes semanales |
| Datos personales | usuarios del panel (nombre de usuario, hash de contraseña argon2id, último acceso) |
| Acceso | personal autorizado del integrador y del cliente, por VPN, con usuario y contraseña |

### 2.4 Informe semanal con proveedor LLM configurable

Las cifras se calculan en la base de datos; el proveedor LLM solo **redacta** el texto a partir
de ellas. Al proveedor viajan **solo cifras agregadas** de la tienda (entradas por día y hora,
número y duración de alertas), sin imágenes, sin nombres de personas ni datos de trabajadores. Se
puede desactivar (`VMS_LLM_PROVIDER=none`): el informe se genera solo con cifras. Si se activa,
revisar las condiciones del proveedor (ubicación del tratamiento, no uso de los datos para
entrenamiento) e incluirlo como subencargado.

## 3. Medidas de seguridad (art. 32 RGPD)

| Medida | Cómo |
|---|---|
| Minimización | la analítica no guarda imágenes; los conteos son anónimos |
| Cifrado en tránsito | VPN WireGuard (Headscale/Tailscale) entre tiendas y central; TLS en PostgreSQL |
| Aislamiento de red | cámaras en VLAN sin Internet; ningún puerto abierto en el router ([RED.md](RED.md)) |
| Control de acceso | usuarios con rol (administrador/operador), contraseñas argon2id, bloqueo tras 5 fallos, sesiones con caducidad, protección CSRF |
| Credenciales de cámaras | almacén del sistema o archivo cifrado (Fernet), nunca en texto plano ni en registros |
| Retención | borrado automático de grabaciones al cumplir el plazo; protección de disco lleno |
| Registros | sin contraseñas ni tokens; rotación automática |
| Integridad del software | binarios externos verificados por SHA-256; dependencias fijadas y revisadas |
| Disponibilidad | servicios con reinicio automático; grabación resistente a cortes de luz |

## 4. Derechos de los interesados

- **Acceso a imágenes**: el responsable localiza la grabación por fecha, hora y cámara
  (reproducción con línea de tiempo) y puede exportar el fragmento; antes de entregarlo debe
  proteger la imagen de terceros (pixelado con herramienta externa).
- **Supresión/oposición**: las grabaciones se borran solas al cumplir el plazo. Los conteos son
  anónimos y no permiten identificar a nadie, por lo que no hay datos personales que suprimir.

## 5. Guion para la EIPD

Puntos que la EIPD del responsable debería cubrir (la AEPD la exige para videovigilancia a gran
escala de zonas de acceso público):

1. Descripción sistemática: tiendas, número de cámaras, zonas, finalidades (§2).
2. Necesidad y proporcionalidad: por qué la cámara de puerta y la de cola; por qué conteo
   anónimo en lugar de otras técnicas; plazos.
3. Riesgos: acceso no autorizado a grabaciones; uso desviado para control laboral; filtración
   de credenciales de cámaras; transferencias internacionales si se activa el proveedor LLM.
4. Medidas: §3 de esta hoja, procedimiento de acceso a grabaciones, formación del personal,
   revisión periódica de usuarios.
5. Consulta a la representación legal de los trabajadores sobre la ubicación de las cámaras.
6. Revisión anual de la EIPD o cuando cambie el sistema (nuevas finalidades, nuevas cámaras).

## 6. Contrato de encargo (art. 28)

Debe incluir, como mínimo: objeto y duración; tipos de datos (imágenes y datos de usuarios del
sistema); instrucciones documentadas; confidencialidad del personal; medidas del §3;
subencargados autorizados (§1); asistencia en derechos y brechas (notificación en menos de 24 h al
responsable); devolución o borrado de datos al terminar; auditorías.
