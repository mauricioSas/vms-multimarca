; VMS Multimarca: textos del instalador en español (latinoamericano neutro, con tuteo). Dueño: B3.
; Se carga DESPUÉS de compiler:Languages\Spanish.isl: aquí se sustituyen sus mensajes de «usted» por tuteo y se
; añaden los mensajes propios ([CustomMessages]). %1, %2…: datos; %n: salto de línea.
; tests/windows/test_installer_script.py comprueba que no falte ningún mensaje y que no haya «usted» ni voseo.

[Messages]
SetupLdrStartupMessage=Se instalará %1. ¿Quieres continuar?
SetupFileMissing=Falta el archivo %1 en la carpeta de instalación. Consigue una copia nueva del instalador.
SetupFileCorrupt=Los archivos de instalación están dañados. Consigue una copia nueva del instalador.
SetupFileCorruptOrWrongVer=Los archivos de instalación están dañados o no son de esta versión del instalador. Consigue una copia nueva.
WindowsVersionNotSupported=Este programa no es compatible con la versión de Windows de este equipo.
AdminPrivilegesRequired=Para instalar este programa tienes que iniciar sesión como administrador.
PowerUserPrivilegesRequired=Para instalar este programa tienes que iniciar sesión como administrador.
SetupAppRunningError=El instalador detectó que %1 está abierto.%n%nCiérralo y haz clic en Aceptar para continuar, o en Cancelar para salir.
UninstallAppRunningError=El desinstalador detectó que %1 está abierto.%n%nCiérralo y haz clic en Aceptar para continuar, o en Cancelar para salir.
PrivilegesRequiredOverrideInstruction=Elige el modo de instalación
WizardSelectProgramGroup=Carpeta del menú Inicio
PrivilegesRequiredOverrideText1=%1 se puede instalar para todos los usuarios (requiere permisos de administrador) o solo para ti.
PrivilegesRequiredOverrideText2=%1 se puede instalar solo para ti o para todos los usuarios (requiere permisos de administrador).
ExitSetupMessage=La instalación no terminó. Si sales ahora, el programa no quedará instalado.%n%nPuedes volver a ejecutar el instalador más tarde para completarla.%n%n¿Salir del instalador?
SelectLanguageTitle=Idioma del instalador
SelectLanguageLabel=Elige el idioma que se usará durante la instalación.
ClickNext=Haz clic en Siguiente para continuar o en Cancelar para salir del instalador.
BrowseDialogLabel=Elige una carpeta y haz clic en Aceptar.
WelcomeLabel1=Te damos la bienvenida al instalador de [name]
WelcomeLabel2=Se instalará [name/ver] en este equipo.%n%nEl asistente te pedirá el tipo de puesto, dónde guardar las grabaciones y los datos de la sede. Tarda unos minutos.
PasswordLabel3=Escribe la contraseña y haz clic en Siguiente. Se distinguen mayúsculas y minúsculas.
IncorrectPassword=La contraseña no es correcta. Vuelve a intentarlo.
WizardLicense=Condiciones de uso
LicenseLabel=Lee esta información antes de continuar.
LicenseLabel3=Lee las condiciones de uso. Para continuar con la instalación tienes que aceptarlas.
LicenseAccepted=&Acepto las condiciones
LicenseNotAccepted=&No acepto las condiciones
InfoBeforeLabel=Lee esta información antes de continuar.
InfoBeforeClickLabel=Cuando quieras continuar con la instalación, haz clic en Siguiente.
InfoAfterLabel=Lee esta información antes de continuar.
InfoAfterClickLabel=Cuando quieras continuar, haz clic en Siguiente.
UserInfoDesc=Escribe tus datos.
UserInfoNameRequired=Tienes que escribir un nombre.
WizardSelectDir=Carpeta de destino
SelectDirBrowseLabel=Para continuar, haz clic en Siguiente. Si quieres elegir otra carpeta, haz clic en Examinar.
InvalidPath=Escribe una ruta completa con la letra de la unidad, por ejemplo:%n%nC:\APP%n%no una ruta de red de esta forma:%n%n\\servidor\compartido
InvalidDrive=La unidad o la ruta de red que elegiste no existe o no está disponible. Elige otra.
DiskSpaceWarning=La instalación necesita al menos %1 KB libres, pero la unidad elegida solo tiene %2 KB.%n%n¿Quieres continuar de todos modos?
DirExists=La carpeta:%n%n%1%n%nya existe. ¿Quieres instalar en ella de todos modos?
DirDoesntExist=La carpeta:%n%n%1%n%nno existe. ¿Quieres crearla?
WizardSelectComponents=Tipo de puesto
SelectComponentsDesc=¿Qué papel tiene este equipo?
SelectComponentsLabel2=Elige el tipo de puesto. La lista muestra lo que se instala con cada uno. Haz clic en Siguiente para continuar.
NoUninstallWarning=El instalador detectó que estos componentes ya están instalados:%n%n%1%n%nDesmarcarlos no los desinstala.%n%n¿Quieres continuar de todos modos?
WizardSelectTasks=Opciones de este equipo
SelectTasksDesc=¿Qué más quieres que haga el instalador?
SelectTasksLabel2=Marca lo que quieras que se configure al instalar [name] y haz clic en Siguiente.
SelectStartMenuFolderBrowseLabel=Para continuar, haz clic en Siguiente. Si quieres elegir otra carpeta, haz clic en Examinar.
MustEnterGroupName=Tienes que escribir el nombre de una carpeta.
WizardReady=Todo listo para instalar
ReadyLabel1=El instalador ya puede instalar [name] en este equipo. Revisa el resumen:
ReadyLabel2a=Haz clic en Instalar para continuar, o en Atrás si quieres revisar o cambiar algo.
ReadyLabel2b=Haz clic en Instalar para continuar.
ReadyMemoType=Tipo de puesto:
ReadyMemoComponents=Se instala:
ReadyMemoTasks=Opciones:
StopDownload=¿Seguro que quieres detener la descarga?
StopExtraction=¿Seguro que quieres detener la extracción?
WizardPreparing=Preparando la instalación
PreparingDesc=El instalador está preparando la instalación de [name] en este equipo.
PreviousInstallNotCompleted=No terminó la instalación o desinstalación anterior de un programa. Reinicia el equipo para completarla.%n%nDespués, vuelve a ejecutar el instalador para completar la instalación de [name].
CannotContinue=El instalador no puede continuar. Haz clic en Cancelar para salir.
ApplicationsFound=Estas aplicaciones usan archivos que el instalador tiene que actualizar. Es mejor dejar que el instalador las cierre.
ApplicationsFound2=Estas aplicaciones usan archivos que el instalador tiene que actualizar. Es mejor dejar que el instalador las cierre. Al terminar, intentará volver a abrirlas.
ErrorCloseApplications=El instalador no pudo cerrar todas las aplicaciones. Antes de continuar, cierra las que usen archivos que hay que actualizar.
PrepareToInstallNeedsRestart=El instalador necesita reiniciar el equipo. Después del reinicio, vuelve a ejecutarlo para completar la instalación de [name].%n%n¿Quieres reiniciar ahora?
InstallingLabel=Espera mientras se instala [name] en este equipo.
FinishedHeadingLabel=Instalación de [name] terminada
FinishedLabelNoIcons=[name] quedó instalado en este equipo.
FinishedLabel=[name] quedó instalado en este equipo. Puedes abrirlo desde el menú Inicio.
ClickFinish=Haz clic en Finalizar para cerrar el instalador.
FinishedRestartLabel=Para completar la instalación de [name] hay que reiniciar el equipo. ¿Quieres reiniciarlo ahora?
FinishedRestartMessage=Para completar la instalación de [name] hay que reiniciar el equipo.%n%n¿Quieres reiniciarlo ahora?
ShowReadmeCheck=Sí, quiero ver el archivo LÉAME
YesRadio=&Sí, reiniciar el equipo ahora
NoRadio=&No, lo reiniciaré más tarde
SelectDiskLabel2=Inserta el disco %1 y haz clic en Aceptar.%n%nSi los archivos están en otra carpeta, escribe la ruta correcta o haz clic en Examinar.
FileNotInDir2=No se encontró el archivo «%1» en «%2». Inserta el disco correcto o elige otra carpeta.
SelectDirectoryLabel=Indica dónde está el siguiente disco.
SetupAborted=La instalación no terminó.%n%nCorrige el problema y vuelve a ejecutar el instalador.
AbortRetryIgnoreSelectAction=Elige una acción
RetryCancelSelectAction=Elige una acción
FileExistsSelectAction=Elige una acción
ExistingFileNewerSelectAction=Elige una acción
ExistingFileReadOnlyRetry=&Quitar el atributo de solo lectura y reintentar
ErrorRestartingComputer=El instalador no pudo reiniciar el equipo. Reinícialo a mano.
ConfirmUninstall=¿Seguro que quieres desinstalar %1 por completo?
UninstallStatusLabel=Espera mientras se desinstala %1 de este equipo.
UninstalledAll=%1 se desinstaló de este equipo.
UninstalledMost=Terminó la desinstalación de %1.%n%nAlgunos elementos no se pudieron eliminar; puedes borrarlos a mano.
UninstalledAndNeedsRestart=Para completar la desinstalación de %1 hay que reiniciar el equipo.%n%n¿Quieres reiniciarlo ahora?
ConfirmDeleteSharedFile2=El sistema indica que ningún otro programa usa este archivo compartido. ¿Quieres eliminarlo?%n%nSi otros programas lo usan, podrían dejar de funcionar. Si no estás seguro, elige No: dejarlo no causa ningún problema.

[CustomMessages]
; --- tipos y componentes
TypeControl=Puesto de control (grabación y muros)
TypeStore=Tienda con analítica
TypeCentral=Panel central
TypeViewer=Solo visor (mira las cámaras de otros equipos)
CompVideo=Grabación y vídeo en vivo (servicios VMSEngine y VMSBackend)
CompAnalytics=Analítica y latido a la central (VMSAnalytics y VMSHeartbeat)
CompCentral=Panel central multi-sede (VMSCentral)
CompViewer=Visor de escritorio (menú Inicio)
CompUpdater=Actualizaciones automáticas (VMSUpdater)
TaskStoreViewer=Instalar también el visor de escritorio en este equipo
TaskWalls=Abrir los muros a pantalla completa al iniciar sesión (cuenta de muros)
ShortcutComment=Abre el visor de VMS Multimarca
RunViewer=Abrir VMS Multimarca
OperatorsGroupComment=Usuarios que pueden abrir los muros de VMS Multimarca sin contraseña

; --- grabaciones
RecCaption=Grabaciones
RecDescription=¿Dónde se guardan las grabaciones?
RecSubCaption=Elige una carpeta en un disco con espacio. Lo ideal es un disco solo para grabaciones, distinto del de Windows.
RecCameras=&Número de cámaras:
RecMbps=&Mbit/s por cámara (aprox.):
RecFreeSpace=Espacio libre en el disco %1 %2 GB de %3 GB.
RecSystemDrive=Ojo: es el disco de Windows. Si se llena, el equipo puede ir lento o dejar de grabar; mejor un disco aparte.
RecDays=Con %2 cámaras a %3 Mbit/s caben unos %1 días de grabación.
RecDaysLess1=Con %1 cámaras a %2 Mbit/s no cabe ni un día de grabación.
RecDaysLow=Es menos que la retención por defecto (30 días): las grabaciones más antiguas se borrarán antes.
RecTooSmall=Hacen falta al menos %1 GB libres en ese disco para grabar.
RecNoDisk=No se puede leer el disco de esa carpeta. Revisa la letra de la unidad o la ruta de red.
ErrRecPath=Escribe una ruta completa para las grabaciones, por ejemplo D:\Grabaciones.
ErrRecRoot=Elige una carpeta dentro del disco (por ejemplo D:\Grabaciones), no la raíz del disco.
ErrRecInsideProgram=Las grabaciones no pueden ir dentro de la carpeta del programa ni de Windows.
ErrRecCameras=Escribe cuántas cámaras vas a grabar (1 o más).
ErrRecMbps=Escribe los Mbit/s por cámara, por ejemplo 4 o 2,5.

; --- sede
SiteCaption=Sede
SiteDescription=¿Qué tienda o sede es este equipo?
SiteSubCaption=Estos datos identifican el equipo en el panel central. El identificador se sugiere solo; cámbialo si tu empresa usa otro.
SiteName=&Nombre de la sede:
SiteCode=&Código de tienda (opcional):
SiteId=&Identificador (minúsculas, números y guiones):
SiteCentralUrl=&Dirección del panel central (opcional):
SiteToken=&Token de la sede (opcional):
ErrSiteName=Escribe el nombre de la sede (hasta 80 caracteres).
ErrSiteCode=El código de tienda admite hasta 32 caracteres.
ErrSiteId=El identificador tiene que tener entre 3 y 40 caracteres: minúsculas sin tildes, números y guiones (por ejemplo tienda-centro).
ErrCentralUrl=La dirección del panel central tiene que empezar por https:// (o http:// en una red privada).
ErrTokenWithoutUrl=Escribiste el token de la sede pero no la dirección del panel central.
ErrControlChars=Uno de los campos tiene un carácter no válido (salto de línea o similar) o la secuencia «${», que no se admite.

; --- panel central
CentralCaption=Panel central
CentralDescription=Conexión con la base de datos del panel central
CentralSubCaption=El panel central guarda los datos de todas las sedes en PostgreSQL. Escribe la cadena de conexión que te dio el administrador de la base de datos.
CentralDsn=&Cadena de conexión de PostgreSQL:
ErrPgDsnEmpty=Escribe la cadena de conexión de PostgreSQL.
ErrPgDsnFormat=La cadena de conexión tiene que empezar por postgresql:// (o tener la forma host=… dbname=…).

; --- seguridad
SecCaption=Seguridad
SecDescription=Contraseña del administrador
SecSubCaption=Se crea el usuario «admin» con esta contraseña (mínimo 8 caracteres). Guárdala en un lugar seguro: la necesitas para dar de alta cámaras y usuarios.
SecPassword=&Contraseña:
SecPassword2=&Repite la contraseña:
ErrPasswordShort=La contraseña tiene que tener al menos 8 caracteres.
ErrPasswordLong=La contraseña puede tener como mucho 128 caracteres.
ErrPasswordMismatch=Las dos contraseñas no coinciden.

; --- red
NetCaption=Red
NetDescription=Acceso desde otros equipos de la red
NetHttps=Usar &HTTPS en la red local (recomendado: cifra la conexión con otros equipos)
NetDomain=Abrir también en redes de &dominio (además de las privadas; nunca en redes públicas)
NetHttpPort=Puerto web (HTTP, solo este equipo):
NetHttpsPort=Puerto web seguro (HTTPS):
NetPublicWarning=Ojo: estas redes están marcadas como Públicas: %1. En una red pública el firewall no deja entrar a otros equipos de la tienda.
NetNoPublic=La red de este equipo no está marcada como Pública.
NetSetPrivate=Cambiar esas redes a &Privadas (hazlo solo si es la red de la tienda o la VPN)
ErrPort=Los puertos tienen que ser números entre 1024 y 65535.
ErrPortSame=Los puertos HTTP y HTTPS tienen que ser distintos.
ErrPortInUse=Hay un puerto ocupado por otro programa: %1%n%nCambia el puerto en esta página o cierra el otro programa.
ErrPortsCheck=No se pudieron comprobar los puertos: %1

; --- resumen
ReadyVersion=Versión:
ReadyUpgradeFrom=(ahora está instalada la %1)
ReadyV1=Actualización desde la versión 1:
ReadyV1Detail=se conservan la configuración, los usuarios y las grabaciones; los servicios antiguos se sustituyen.
ReadyRecordings=Grabaciones:
ReadyFree=%1 GB libres
ReadySystemDrive=en el disco de Windows (mejor un disco aparte)
ReadyKeepConfig=se conserva la configuración actual
ReadySite=Sede:
ReadyCentral=panel central: %1
ReadyAdmin=Administrador:
ReadyAdminDetail=se crea el usuario «admin» con la contraseña indicada
ReadyNetwork=Red:
ReadyHttps=HTTPS en el puerto %1
ReadyHttp=sin HTTPS (web en el puerto %1)
ReadyProfilesPrivate=firewall abierto solo en redes privadas
ReadyProfilesDomain=firewall abierto en redes privadas y de dominio
ReadySetPrivate=las redes públicas se cambian a privadas
ReadyPublicWarning=aviso: redes públicas sin cambiar (%1)

; --- instalación
StatusSettings=Guardando los ajustes del equipo...
StatusMigrate=Convirtiendo la instalación de la versión 1...
StatusServices=Registrando los servicios de Windows...
StatusAcl=Protegiendo las carpetas de datos...
StatusKiosk=Preparando la entrada de los muros...
StatusFirewall=Configurando el firewall...
StatusTls=Creando el certificado HTTPS...
StatusStart=Arrancando los servicios...
StatusHealth=Comprobando que todo responde (hasta 2 minutos)...
StatusUninstServices=Parando y quitando los servicios...
ErrCreateDir=No se pudo crear la carpeta %1.
ErrWriteFile=No se pudo escribir el archivo %1.
ErrVmsctlMissing=Falta la herramienta de configuración (%1). Vuelve a descargar el instalador.
ErrVmsctlStart=No se pudo ejecutar %1: %2
ErrVmsctlCode=La herramienta de configuración terminó con el código %1.
ErrStopServices=No se pudieron parar los servicios de la versión instalada: %1
ErrUpdaterBusy=Hay una actualización en curso. Espera unos minutos a que termine y vuelve a ejecutar el instalador.
ErrUpdateSource=UpdateSource tiene que empezar por https://, http:// o file:///.
ErrUpdateSourceNoRoot=Este instalador no trae el root de confianza de las actualizaciones en modo %1 (updater\trusted\%1\1.root.json): no puede usar UpdateSource. Usa un instalador publicado o quita UpdateSource.
ErrDowngrade=Ya está instalada la versión %1, más nueva que este instalador (%2).%n%nNo se puede volver a una versión anterior con el instalador. Si soporte técnico te lo indicó, ejecútalo con /ALLOWDOWNGRADE.
ErrSecretsMissing=No existe el archivo de secretos %1 (parámetro /SECRETS).
ErrSecretsRead=No se pudo leer el archivo de secretos %1.
ErrSecretsFormat=El archivo de secretos %1 no es un JSON válido.
WarnWin10=Este equipo tiene Windows 10, que Microsoft dejó de mantener el 14 de octubre de 2025. VMS Multimarca solo se admite en Windows 10 si el equipo tiene las actualizaciones de seguridad ampliadas (ESU) de pago, y las funciones nuevas pueden no estar garantizadas. Lo recomendable es Windows 11.%n%nHaz clic en Aceptar para instalar igualmente o en Cancelar para salir.

; --- resultado
ResultCaption=Comprobación final
ResultDescription=Estado de los servicios después de instalar
ResultOk=Todo listo: los servicios están en marcha y responden.
ResultFailed=La instalación no terminó bien en el paso «%1»:%n%n%2%n%nGuarda el informe de diagnóstico y envíalo a soporte técnico.
ResultHealthFailed=El programa quedó instalado, pero el sistema no respondió bien en 2 minutos:%n%n%1%n%nA veces solo tarda más en arrancar. Si sigue igual, guarda el informe de diagnóstico y envíalo a soporte técnico.
ResultLog=Registro de la instalación: %1
ResultSave=&Guardar informe...
ResultSavePrompt=Guardar el informe de diagnóstico
ResultSaveFilter=Archivo zip (*.zip)|*.zip
ResultSaved=Informe guardado en %1. No contiene contraseñas.
ResultSaveFailed=No se pudo guardar el informe: %1

; --- desinstalación
UninstKeepData=¿Quieres conservar las grabaciones y la configuración?%n%nSí (recomendado): se quita el programa y se conservan las grabaciones, los usuarios y los ajustes en %1, para reinstalar sin perder nada.%nNo: se borra TODO, también las grabaciones. No se puede deshacer.
