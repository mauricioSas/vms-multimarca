; VMS Multimarca: installer texts in English (optional language, /LANG=en). Owner: B3.
; Same keys as lang\es.isl; tests/windows/test_installer_script.py checks that both files have them all.

[Messages]
WizardSelectComponents=Workstation type
SelectComponentsDesc=What role does this computer have?
SelectComponentsLabel2=Choose the workstation type. The list shows what each one installs. Click Next to continue.
WizardSelectTasks=Options for this computer
ReadyMemoType=Workstation type:
ReadyMemoComponents=Installs:
ReadyMemoTasks=Options:

[CustomMessages]
TypeControl=Control room (recording and video walls)
TypeStore=Store with analytics
TypeCentral=Central panel
TypeViewer=Viewer only (watches cameras of other computers)
CompVideo=Recording and live video (VMSEngine and VMSBackend services)
CompAnalytics=Analytics and heartbeat to the central panel (VMSAnalytics and VMSHeartbeat)
CompCentral=Multi-site central panel (VMSCentral)
CompViewer=Desktop viewer (Start menu)
CompUpdater=Automatic updates (VMSUpdater)
TaskStoreViewer=Also install the desktop viewer on this computer
TaskWalls=Open the video walls full screen at sign-in (wall account)
ShortcutComment=Opens the VMS Multimarca viewer
RunViewer=Open VMS Multimarca
OperatorsGroupComment=Users who can open the VMS Multimarca video walls without a password

RecCaption=Recordings
RecDescription=Where are recordings stored?
RecSubCaption=Choose a folder on a disk with free space. Ideally a disk used only for recordings, not the Windows disk.
RecCameras=&Number of cameras:
RecMbps=&Mbit/s per camera (approx.):
RecFreeSpace=Free space on %1: %2 GB of %3 GB.
RecSystemDrive=Careful: this is the Windows disk. If it fills up, the computer may slow down or stop recording; a separate disk is better.
RecDays=With %2 cameras at %3 Mbit/s there is room for about %1 days of recordings.
RecDaysLow=That is less than the default retention (30 days): older recordings will be deleted sooner.
RecTooSmall=At least %1 GB free on that disk are needed to record.
RecNoDisk=The disk of that folder cannot be read. Check the drive letter or the network path.
ErrRecPath=Type a full path for the recordings, for example D:\Recordings.
ErrRecRoot=Choose a folder inside the disk (for example D:\Recordings), not the root of the disk.
ErrRecInsideProgram=Recordings cannot go inside the program folder or the Windows folder.
ErrRecCameras=Type how many cameras will be recorded (1 or more).
ErrRecMbps=Type the Mbit/s per camera, for example 4 or 2.5.

SiteCaption=Site
SiteDescription=Which store or site is this computer?
SiteSubCaption=This identifies the computer in the central panel. The identifier is suggested automatically; change it if your company uses another one.
SiteName=Site &name:
SiteCode=Store &code (optional):
SiteId=&Identifier (lowercase letters, digits and hyphens):
SiteCentralUrl=Central panel &address (optional):
SiteToken=Site &token (optional):
ErrSiteName=Type the site name (up to 80 characters).
ErrSiteCode=The store code allows up to 32 characters.
ErrSiteId=The identifier must have 3 to 40 characters: lowercase letters without accents, digits and hyphens (for example store-centre).
ErrCentralUrl=The central panel address must start with https:// (or http:// on a private network).
ErrTokenWithoutUrl=You typed the site token but not the central panel address.
ErrControlChars=One of the fields contains an invalid character (line break or similar) or the sequence «${», which is not allowed.

CentralCaption=Central panel
CentralDescription=Connection to the central panel database
CentralSubCaption=The central panel stores the data of all sites in PostgreSQL. Type the connection string given by the database administrator.
CentralDsn=PostgreSQL &connection string:
ErrPgDsnEmpty=Type the PostgreSQL connection string.
ErrPgDsnFormat=The connection string must start with postgresql:// (or look like host=… dbname=…).

SecCaption=Security
SecDescription=Administrator password
SecSubCaption=The «admin» user is created with this password (at least 8 characters). Keep it safe: it is needed to add cameras and users.
SecPassword=&Password:
SecPassword2=&Repeat the password:
ErrPasswordShort=The password must have at least 8 characters.
ErrPasswordLong=The password can have at most 128 characters.
ErrPasswordMismatch=The two passwords do not match.

NetCaption=Network
NetDescription=Access from other computers on the network
NetHttps=Use &HTTPS on the local network (recommended: encrypts connections from other computers)
NetDomain=Also open on &domain networks (besides private ones; never on public networks)
NetHttpPort=Web port (HTTP, this computer only):
NetHttpsPort=Secure web port (HTTPS):
NetPublicWarning=Careful: these networks are marked as Public: %1. On a public network the firewall does not let other computers of the store in.
NetNoPublic=The network of this computer is not marked as Public.
NetSetPrivate=Change those networks to &Private (only if it is the store network or the VPN)
ErrPort=Ports must be numbers between 1024 and 65535.
ErrPortSame=The HTTP and HTTPS ports must be different.
ErrPortInUse=A port is used by another program: %1%n%nChange the port on this page or close the other program.
ErrPortsCheck=The ports could not be checked: %1

ReadyVersion=Version:
ReadyUpgradeFrom=(version %1 is installed now)
ReadyV1=Upgrade from version 1:
ReadyV1Detail=settings, users and recordings are kept; the old services are replaced.
ReadyRecordings=Recordings:
ReadyFree=%1 GB free
ReadySystemDrive=on the Windows disk (a separate disk is better)
ReadyKeepConfig=the current settings are kept
ReadySite=Site:
ReadyCentral=central panel: %1
ReadyAdmin=Administrator:
ReadyAdminDetail=the «admin» user is created with the given password
ReadyNetwork=Network:
ReadyHttps=HTTPS on port %1
ReadyHttp=no HTTPS (web on port %1)
ReadyProfilesPrivate=firewall open on private networks only
ReadyProfilesDomain=firewall open on private and domain networks
ReadySetPrivate=public networks are changed to private
ReadyPublicWarning=warning: public networks left unchanged (%1)

StatusSettings=Saving the settings of this computer...
StatusMigrate=Converting the version 1 installation...
StatusPointer=Activating the new version...
StatusServices=Registering the Windows services...
StatusAcl=Protecting the data folders...
StatusKiosk=Preparing the video wall sign-in...
StatusFirewall=Configuring the firewall...
StatusTls=Creating the HTTPS certificate...
StatusStart=Starting the services...
StatusHealth=Checking that everything responds (up to 2 minutes)...
StatusUninstServices=Stopping and removing the services...
ErrCreateDir=The folder %1 could not be created.
ErrWriteFile=The file %1 could not be written.
ErrVmsctlMissing=The configuration tool is missing (%1). Download the installer again.
ErrVmsctlStart=%1 could not be run: %2
ErrVmsctlCode=The configuration tool ended with code %1.
ErrStopServices=The services of the installed version could not be stopped: %1
ErrUpdaterBusy=An update is in progress. Wait a few minutes for it to finish and run the installer again.
ErrUpdateSource=UpdateSource must start with https://, http:// or file:///.
ErrDowngrade=Version %1 is already installed, newer than this installer (%2).%n%nThe installer cannot go back to an older version. If technical support told you to, run it with /ALLOWDOWNGRADE.
ErrSecretsMissing=The secrets file %1 does not exist (/SECRETS parameter).
ErrSecretsRead=The secrets file %1 could not be read.
ErrSecretsFormat=The secrets file %1 is not valid JSON.
WarnWin10=This computer runs Windows 10, which Microsoft stopped supporting on 14 October 2025. VMS Multimarca is only supported on Windows 10 with the paid Extended Security Updates (ESU), and new features may not be guaranteed. Windows 11 is recommended.%n%nClick OK to install anyway or Cancel to exit.

ResultCaption=Final check
ResultDescription=Service status after installing
ResultOk=All set: the services are running and responding.
ResultFailed=The installation did not finish correctly at step «%1»:%n%n%2%n%nSave the diagnostic report and send it to technical support.
ResultHealthFailed=The program is installed, but the system did not respond correctly within 2 minutes:%n%n%1%n%nSometimes it just takes longer to start. If it stays like this, save the diagnostic report and send it to technical support.
ResultLog=Installation log: %1
ResultSave=&Save report...
ResultSavePrompt=Save the diagnostic report
ResultSaveFilter=Zip file (*.zip)|*.zip
ResultSaved=Report saved to %1. It contains no passwords.
ResultSaveFailed=The report could not be saved: %1

UninstKeepData=Do you want to keep the recordings and the settings?%n%nYes (recommended): the program is removed and the recordings, users and settings in %1 are kept, to reinstall without losing anything.%nNo: EVERYTHING is deleted, recordings included. This cannot be undone.
