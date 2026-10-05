; VMS Multimarca: instalador de Windows (Inno Setup 7.1.0). Dueño: B3.
; Referencias: PLAN-V2 §1.4 (asistente, silenciosa, desinstalador, versión), §2.3 (accesos directos y muros),
; §2.4 (disposición en disco) y CONTRATO §13-§14 (vmshost, vmsctl, puntero, registro).
;
; No se compila a mano: python -m tools.build all --version X.Y.Z (ver docs/EMPAQUETADO.md), que pasa:
;   /DAppVersion=2.0.0  /DAppVersionNumeric=2.0.0.0  /DPayloadDir=<build\layout\payload>
;   /DOutputDir=<dist>  /DOutputBaseName=VMSMultimarca-Setup-2.0.0
;   [/DTestBuild=1]     build de prueba: parámetros de simulación para el e2e (nunca en una publicación)
;   [/DSignToolName=vmssign /Svmssign=...]   firma Authenticode del instalador y del desinstalador (decisión N1:
;                                            sin certificado todavía, el hueco queda preparado)
;
; Configuración del equipo: SIEMPRE con vmsctl.exe (sin PowerShell). El instalador solo copia archivos, escribe
; el .env y el config.json inicial y crea el grupo «VMS Operadores».

#ifndef AppVersion
  #error Falta /DAppVersion (usa python -m tools.build all --version X.Y.Z)
#endif
#ifndef AppVersionNumeric
  #error Falta /DAppVersionNumeric (cuatro números, p. ej. 2.0.0.0)
#endif
#ifndef PayloadDir
  #error Falta /DPayloadDir (carpeta payload de python -m distribution.layout)
#endif
#ifndef OutputDir
  #define OutputDir "..\..\dist"
#endif
#ifndef OutputBaseName
  #define OutputBaseName "VMSMultimarca-Setup-" + AppVersion
#endif

; AppId fijo para siempre: lo usan el actualizador (DisplayVersion, CONTRATO §13.7) y la detección de versiones.
#define AppIdPlain "{8D3F0C52-6A1B-4E7C-9B2D-5F4A3C2E1D07}"
#define AppName "VMS Multimarca"

[Setup]
AppId={{8D3F0C52-6A1B-4E7C-9B2D-5F4A3C2E1D07}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Unmanned Studio
AppCopyright=© Unmanned Studio
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductVersion={#AppVersionNumeric}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoCompany=Unmanned Studio
VersionInfoDescription=Instalador de {#AppName}
VersionInfoProductName={#AppName}
; Instalador de 64 bits (Inno 7): {commonpf} es Program Files nativo y el registro es la vista de 64 bits.
SetupArchitecture=x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
MinVersion=10.0.19045
DefaultDirName={commonpf64}\VMSMultimarca
DisableDirPage=yes
UsePreviousAppDir=no
DisableProgramGroupPage=yes
DisableWelcomePage=no
DisableReadyPage=no
AlwaysShowComponentsList=yes
UninstallFilesDir={app}\uninstall
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\vms.ico
SetupIconFile=assets\vms.ico
WizardStyle=modern
WizardImageFile=assets\wizard.bmp
WizardSmallImageFile=assets\wizard-small.bmp
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseName}
Compression=lzma2/max
SolidCompression=yes
; Los servicios los para vmsctl (PrepareToInstall), no el gestor de reinicio de Windows.
CloseApplications=no
RestartApplications=no
SetupLogging=yes
; Español por defecto aunque Windows esté en otro idioma (inglés con /LANG=en).
LanguageDetectionMethod=none
ShowLanguageDialog=no
#ifdef SignToolName
SignTool={#SignToolName}
SignedUninstaller=yes
#endif

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl,lang\es.isl"; LicenseFile: "lang\LICENCIA-es.txt"
Name: "en"; MessagesFile: "compiler:Default.isl,lang\en.isl"; LicenseFile: "lang\LICENCIA-en.txt"

[Types]
; Tipos de puesto (PLAN-V2 §1.4, punto 2). Silenciosa: /TYPE=control|store|central|viewer.
Name: "control"; Description: "{cm:TypeControl}"
Name: "store"; Description: "{cm:TypeStore}"
Name: "central"; Description: "{cm:TypeCentral}"
Name: "viewer"; Description: "{cm:TypeViewer}"

[Components]
; Fijos por tipo: describen qué servicios crea «vmsctl services install --role <tipo>». El visor de la tienda
; es opcional (tarea «storeviewer»).
Name: "video"; Description: "{cm:CompVideo}"; Types: control store; Flags: fixed
Name: "analytics"; Description: "{cm:CompAnalytics}"; Types: store; Flags: fixed
Name: "centralpanel"; Description: "{cm:CompCentral}"; Types: central; Flags: fixed
Name: "viewer"; Description: "{cm:CompViewer}"; Types: control viewer; Flags: fixed
Name: "updater"; Description: "{cm:CompUpdater}"; Types: control store central viewer; Flags: fixed

[Tasks]
Name: "storeviewer"; Description: "{cm:TaskStoreViewer}"; Components: analytics
Name: "walls"; Description: "{cm:TaskWalls}"; Components: video and not analytics; Flags: unchecked

[Files]
; Arrancador fijo (solo lo cambia el instalador completo, CONTRATO §13.3).
Source: "{#PayloadDir}\bin\vmshost.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
; La versión, inmutable (§13.1); la escribe el instalador y después solo el actualizador.
Source: "{#PayloadDir}\versions\{#AppVersion}\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Ranura A del actualizador (la B la crea el propio actualizador).
Source: "{#PayloadDir}\updater\slot-a\*"; DestDir: "{app}\updater\slot-a"; Flags: ignoreversion recursesubdirs createallsubdirs
; Copia temporal de vmsctl para comprobar puertos antes de instalar (no se instala aparte).
Source: "{#PayloadDir}\versions\{#AppVersion}\bin\vmsctl.exe"; DestDir: "{tmp}"; Flags: dontcopy
Source: "assets\vms.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "lang\LICENCIA-es.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; El acceso directo apunta al arrancador fijo, que abre el visor de la versión activa (PLAN-V2 §2.3).
Name: "{commonprograms}\{#AppName}"; Filename: "{app}\bin\vmshost.exe"; Parameters: "viewer"; IconFilename: "{app}\vms.ico"; Comment: "{cm:ShortcutComment}"; Check: ViewerSelected

[Registry]
; Muros al iniciar sesión en el puesto de control (PLAN-V2 §2.3). El resto del registro lo escribe el [Code].
Root: HKLM; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "VMSMultimarcaMuros"; ValueData: """{app}\bin\vmshost.exe"" viewer --walls"; Tasks: walls; Flags: uninsdeletevalue

[Run]
Filename: "{app}\bin\vmshost.exe"; Parameters: "viewer"; Description: "{cm:RunViewer}"; Flags: postinstall nowait skipifsilent runasoriginaluser; Check: ViewerSelected

[UninstallDelete]
Type: dirifempty; Name: "{app}\uninstall"
Type: dirifempty; Name: "{app}"

[Code]
#include "pascal\util.pas"
#include "pascal\state.pas"
#include "pascal\vmsctl.pas"
#include "pascal\files.pas"
#include "pascal\pages.pas"
#include "pascal\events.pas"
#include "pascal\uninstall.pas"
