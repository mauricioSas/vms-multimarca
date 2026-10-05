{ pages.pas: páginas propias del asistente (PLAN-V2 §1.4): grabaciones, sede, panel central, seguridad, red
  y resultado. Los valores iniciales salen de /LOADINF y /SECRETS, así la instalación silenciosa recorre las
  mismas páginas y la misma validación que la interactiva. Dueño: B3. }

const
  MIN_FREE_GB_DEFAULT = 50;
  DEFAULT_HTTP_PORT = '8600';
  DEFAULT_HTTPS_PORT = '8643';

var
  RecPage: TInputDirWizardPage;
  RecCamerasEdit, RecMbpsEdit: TNewEdit;
  RecInfoLabel: TNewStaticText;
  SitePage: TInputQueryWizardPage;
  CentralPage: TInputQueryWizardPage;
  SecPage: TInputQueryWizardPage;
  NetPage: TWizardPage;
  NetHttpsCheck, NetDomainCheck, NetPrivateCheck: TNewCheckBox;
  NetHttpEdit, NetHttpsEdit: TNewEdit;
  NetHttpLabel, NetHttpsLabel, NetPublicLabel: TNewStaticText;
  ResultPage: TOutputMsgMemoWizardPage;
  ResultSaveButton: TNewButton;
  gSiteIdTouched, gSuggesting: Boolean;
  gEnvHasPgDsn: Boolean;
  gHasCentralUsers: Boolean;
  gMinFreeGB: Integer;

{ ------------------------------------------------------------------ rol }

function Role: String;
begin
  Result := WizardSetupType(False);
end;

function IsVideoRole: Boolean;
begin
  Result := (Role = 'control') or (Role = 'store');
end;

function IsCentralRole: Boolean;
begin
  Result := Role = 'central';
end;

function IsViewerRole: Boolean;
begin
  Result := Role = 'viewer';
end;

function ViewerSelected: Boolean;
begin
  Result := WizardIsComponentSelected('viewer') or WizardIsTaskSelected('storeviewer');
end;

function ShowsRecPage: Boolean;
begin
  Result := IsVideoRole and not gHasConfig;
end;

function ShowsSitePage: Boolean;
begin
  Result := IsVideoRole and not gHasConfig;
end;

function ShowsCentralPage: Boolean;
begin
  Result := IsCentralRole and not gEnvHasPgDsn;
end;

function NeedsAdminPassword: Boolean;
begin
  Result := (IsVideoRole and not gHasUsers) or (IsCentralRole and not gHasCentralUsers);
end;

function ShowsNetPage: Boolean;
begin
  Result := not IsViewerRole;
end;

{ ------------------------------------------------------------------ grabaciones }

{ «4», «4,5» o «4.5» → décimas (45). 0 si no es un número válido. }
function ParseTenths(const S: String): Integer;
var
  T, IntPart, Frac: String;
  P: Integer;
begin
  Result := 0;
  T := Trim(S);
  StringChangeEx(T, ',', '.', True);
  P := Pos('.', T);
  if P = 0 then
  begin
    if IsAllDigits(T) then
      Result := StrToIntDef(T, 0) * 10;
    Exit;
  end;
  IntPart := Copy(T, 1, P - 1);
  Frac := Copy(T, P + 1, Length(T));
  if IntPart = '' then
    IntPart := '0';
  if IsAllDigits(IntPart) and IsAllDigits(Frac) then
    Result := StrToIntDef(IntPart, 0) * 10 + StrToIntDef(Copy(Frac, 1, 1), 0);
end;

function RecordingsDir: String;
begin
  Result := '';
  if ShowsRecPage then
    Result := RemoveBackslash(Trim(RecPage.Values[0]));
end;

function RecDrive(const Dir: String): String;
begin
  Result := ExtractFileDrive(Dir);
  if (Length(Result) = 2) and (Result[2] = ':') then
    Result := Result + '\'
  else
    Result := AddBackslash(Result);
end;

function DiskFreeBytes(const Dir: String; var Free, Total: Int64): Boolean;
begin
  Free := 0;
  Total := 0;
  Result := (Dir <> '') and GetSpaceOnDisk64(RecDrive(Dir), Free, Total);
end;

function IsSystemDrive(const Dir: String): Boolean;
begin
  Result := SameText(ExtractFileDrive(Dir), ExtractFileDrive(ExpandConstant('{win}')));
end;

{ Días de grabación con el espacio libre: bytes/día = cámaras × Mbit/s × 10⁶/8 × 86 400. }
function EstimatedDays(const Free: Int64; const Cameras, MbpsTenths: Integer): Int64;
var
  PerDay: Int64;
begin
  Result := -1;
  if (Cameras <= 0) or (MbpsTenths <= 0) then
    Exit;
  PerDay := Cameras;
  PerDay := PerDay * MbpsTenths;
  PerDay := PerDay * 1080000000;
  Result := Free div PerDay;
end;

function RecInfoText: String;
var
  Dir: String;
  Free, Total, Days: Int64;
  Cams, Tenths: Integer;
begin
  Dir := RemoveBackslash(Trim(RecPage.Values[0]));
  Result := '';
  if not DiskFreeBytes(Dir, Free, Total) then
  begin
    Result := CustomMessage('RecNoDisk');
    Exit;
  end;
  Result := FmtMessage(CustomMessage('RecFreeSpace'), [ExtractFileDrive(Dir), IntToStr(GiB(Free)),
    IntToStr(GiB(Total))]);
  if IsSystemDrive(Dir) then
    Result := Result + #13#10 + CustomMessage('RecSystemDrive');
  Cams := StrToIntDef(Trim(RecCamerasEdit.Text), 0);
  Tenths := ParseTenths(RecMbpsEdit.Text);
  Days := EstimatedDays(Free, Cams, Tenths);
  if Days = 0 then
    Result := Result + #13#10 + FmtMessage(CustomMessage('RecDaysLess1'), [IntToStr(Cams), Trim(RecMbpsEdit.Text)])
  else if Days > 0 then
  begin
    Result := Result + #13#10 + FmtMessage(CustomMessage('RecDays'), [IntToStr(Days), IntToStr(Cams),
      Trim(RecMbpsEdit.Text)]);
    if Days < 30 then
      Result := Result + ' ' + CustomMessage('RecDaysLow');
  end;
  if GiB(Free) < gMinFreeGB then
    Result := Result + #13#10 + FmtMessage(CustomMessage('RecTooSmall'), [IntToStr(gMinFreeGB)]);
end;

procedure RecOnChange(Sender: TObject);
begin
  RecInfoLabel.Caption := RecInfoText;
end;

function ValidateRecPage: String;
var
  Dir: String;
  Free, Total: Int64;
begin
  Result := '';
  if not ShowsRecPage then
    Exit;
  Dir := RemoveBackslash(Trim(RecPage.Values[0]));
  if (Dir = '') or not IsAbsolutePath(Dir) or HasControlChars(Dir) then
    Result := CustomMessage('ErrRecPath')
  else if IsRootPath(Dir) then
    Result := CustomMessage('ErrRecRoot')
  else if PathIsInside(Dir, ProgramDir) or PathIsInside(Dir, ExpandConstant('{win}')) then
    Result := CustomMessage('ErrRecInsideProgram')
  else if not DiskFreeBytes(Dir, Free, Total) then
    Result := CustomMessage('RecNoDisk')
  else if GiB(Free) < gMinFreeGB then
    Result := FmtMessage(CustomMessage('RecTooSmall'), [IntToStr(gMinFreeGB)])
  else if StrToIntDef(Trim(RecCamerasEdit.Text), -1) < 1 then
    Result := CustomMessage('ErrRecCameras')
  else if ParseTenths(RecMbpsEdit.Text) <= 0 then
    Result := CustomMessage('ErrRecMbps');
end;

{ ------------------------------------------------------------------ sede }

procedure SiteSuggest(Sender: TObject);
var
  Source: String;
begin
  if gSiteIdTouched then
    Exit;
  Source := Trim(SitePage.Values[1]);
  if Source = '' then
    Source := Trim(SitePage.Values[0]);
  gSuggesting := True;
  SitePage.Values[2] := SuggestSiteId(Source);
  gSuggesting := False;
end;

procedure SiteIdChanged(Sender: TObject);
begin
  if not gSuggesting then
    gSiteIdTouched := True;
end;

function SiteName: String;
begin
  Result := Trim(SitePage.Values[0]);
end;

function SiteCode: String;
begin
  Result := Trim(SitePage.Values[1]);
end;

function SiteId: String;
begin
  Result := Trim(SitePage.Values[2]);
end;

function CentralUrl: String;
begin
  Result := Trim(SitePage.Values[3]);
end;

function SiteToken: String;
begin
  Result := Trim(SitePage.Values[4]);
end;

function ValidateSitePage: String;
begin
  Result := '';
  if not ShowsSitePage then
    Exit;
  if (SiteName = '') or (Length(SiteName) > 80) or HasControlChars(SiteName) then
    Result := CustomMessage('ErrSiteName')
  else if (Length(SiteCode) > 32) or HasControlChars(SiteCode) then
    Result := CustomMessage('ErrSiteCode')
  else if not IsValidSiteId(SiteId) then
    Result := CustomMessage('ErrSiteId')
  else if (CentralUrl <> '') and (Pos('https://', Lowercase(CentralUrl)) <> 1) and
    (Pos('http://', Lowercase(CentralUrl)) <> 1) then
    Result := CustomMessage('ErrCentralUrl')
  else if not IsEnvSafe(CentralUrl) or not IsEnvSafe(SiteToken) then
    Result := CustomMessage('ErrControlChars')
  else if (SiteToken <> '') and (CentralUrl = '') then
    Result := CustomMessage('ErrTokenWithoutUrl');
end;

{ ------------------------------------------------------------------ panel central }

function PgDsn: String;
begin
  Result := Trim(CentralPage.Values[0]);
end;

function ValidateCentralPage: String;
begin
  Result := '';
  if not ShowsCentralPage then
    Exit;
  if PgDsn = '' then
    Result := CustomMessage('ErrPgDsnEmpty')
  else if not IsEnvSafe(PgDsn) then
    Result := CustomMessage('ErrControlChars')
  else if (Pos('postgresql://', Lowercase(PgDsn)) <> 1) and (Pos('postgres://', Lowercase(PgDsn)) <> 1) and
    (Pos('host=', Lowercase(PgDsn)) = 0) then
    Result := CustomMessage('ErrPgDsnFormat');
end;

{ ------------------------------------------------------------------ seguridad }

function AdminPassword: String;
begin
  Result := SecPage.Values[0];
end;

function ValidateSecPage: String;
begin
  Result := '';
  if not NeedsAdminPassword then
    Exit;
  if Length(AdminPassword) < 8 then
    Result := CustomMessage('ErrPasswordShort')
  else if Length(AdminPassword) > 128 then
    Result := CustomMessage('ErrPasswordLong')
  else if not IsEnvSafe(AdminPassword) then
    Result := CustomMessage('ErrControlChars')
  else if AdminPassword <> SecPage.Values[1] then
    Result := CustomMessage('ErrPasswordMismatch');
end;

{ ------------------------------------------------------------------ red }

function HttpPort: String;
begin
  Result := Trim(NetHttpEdit.Text);
end;

function HttpsPort: String;
begin
  Result := Trim(NetHttpsEdit.Text);
end;

function HttpsEnabled: Boolean;
begin
  Result := ShowsNetPage and IsVideoRole and NetHttpsCheck.Checked;
end;

function FirewallProfiles: String;
begin
  Result := 'private';
  if NetDomainCheck.Checked then
    Result := 'private,domain';
end;

function ValidPort(const S: String): Boolean;
begin
  Result := IsAllDigits(S) and (StrToIntDef(S, 0) >= 1024) and (StrToIntDef(S, 0) <= 65535);
end;

function ValidateNetPage: String;
begin
  Result := '';
  if not (ShowsNetPage and IsVideoRole) then
    Exit;
  if not ValidPort(HttpPort) or not ValidPort(HttpsPort) then
    Result := CustomMessage('ErrPort')
  else if HttpPort = HttpsPort then
    Result := CustomMessage('ErrPortSame');
end;

{ ------------------------------------------------------------------ todo junto }

function ValidateAll: String;
begin
  Result := ValidateRecPage;
  if Result = '' then
    Result := ValidateSitePage;
  if Result = '' then
    Result := ValidateCentralPage;
  if Result = '' then
    Result := ValidateSecPage;
  if Result = '' then
    Result := ValidateNetPage;
end;

{ ------------------------------------------------------------------ creación }

function NewLabel(Page: TWizardPage; const Caption: String; Top: Integer): TNewStaticText;
begin
  Result := TNewStaticText.Create(Page);
  Result.Parent := Page.Surface;
  Result.Top := Top;
  Result.Left := 0;
  Result.Caption := Caption;
end;

function NewEdit(Page: TWizardPage; const Text: String; Left, Top, Width: Integer): TNewEdit;
begin
  Result := TNewEdit.Create(Page);
  Result.Parent := Page.Surface;
  Result.Left := Left;
  Result.Top := Top;
  Result.Width := Width;
  Result.Text := Text;
end;

procedure CreateRecPage;
var
  Top, LabelWidth: Integer;
  L: TNewStaticText;
begin
  RecPage := CreateInputDirPage(wpSelectTasks, CustomMessage('RecCaption'), CustomMessage('RecDescription'),
    CustomMessage('RecSubCaption'), False, '');
  RecPage.Add('');
  RecPage.Values[0] := InfGet('RecordingsDir', DefaultRecordingsDir);
  Top := RecPage.Edits[0].Top + RecPage.Edits[0].Height + ScaleY(16);
  LabelWidth := ScaleX(170);
  L := NewLabel(RecPage, CustomMessage('RecCameras'), Top + ScaleY(3));
  RecCamerasEdit := NewEdit(RecPage, InfGet('Cameras', '8'), LabelWidth, Top, ScaleX(60));
  L.FocusControl := RecCamerasEdit;
  Top := Top + RecCamerasEdit.Height + ScaleY(8);
  L := NewLabel(RecPage, CustomMessage('RecMbps'), Top + ScaleY(3));
  RecMbpsEdit := NewEdit(RecPage, InfGet('MbpsPerCamera', '4'), LabelWidth, Top, ScaleX(60));
  L.FocusControl := RecMbpsEdit;
  Top := Top + RecMbpsEdit.Height + ScaleY(14);
  RecInfoLabel := NewLabel(RecPage, '', Top);
  RecInfoLabel.AutoSize := False;
  RecInfoLabel.WordWrap := True;
  RecInfoLabel.Width := RecPage.SurfaceWidth;
  RecInfoLabel.Height := RecPage.SurfaceHeight - Top;
  RecPage.Edits[0].OnChange := @RecOnChange;
  RecCamerasEdit.OnChange := @RecOnChange;
  RecMbpsEdit.OnChange := @RecOnChange;
end;

procedure CreateSitePage(AfterID: Integer);
begin
  SitePage := CreateInputQueryPage(AfterID, CustomMessage('SiteCaption'), CustomMessage('SiteDescription'),
    CustomMessage('SiteSubCaption'));
  SitePage.Add(CustomMessage('SiteName'), False);
  SitePage.Add(CustomMessage('SiteCode'), False);
  SitePage.Add(CustomMessage('SiteId'), False);
  SitePage.Add(CustomMessage('SiteCentralUrl'), False);
  SitePage.Add(CustomMessage('SiteToken'), True);
  SitePage.Values[0] := InfGet('SiteName', '');
  SitePage.Values[1] := InfGet('SiteCode', '');
  SitePage.Values[2] := InfGet('SiteId', '');
  SitePage.Values[3] := InfGet('CentralUrl', '');
  SitePage.Values[4] := gSecretSiteToken;
  gSiteIdTouched := SitePage.Values[2] <> '';
  if not gSiteIdTouched then
    SiteSuggest(nil);
  SitePage.Edits[0].OnChange := @SiteSuggest;
  SitePage.Edits[1].OnChange := @SiteSuggest;
  SitePage.Edits[2].OnChange := @SiteIdChanged;
end;

procedure CreateCentralPage(AfterID: Integer);
begin
  CentralPage := CreateInputQueryPage(AfterID, CustomMessage('CentralCaption'),
    CustomMessage('CentralDescription'), CustomMessage('CentralSubCaption'));
  CentralPage.Add(CustomMessage('CentralDsn'), True);
  CentralPage.Values[0] := gSecretPgDsn;
end;

procedure CreateSecPage(AfterID: Integer);
begin
  SecPage := CreateInputQueryPage(AfterID, CustomMessage('SecCaption'), CustomMessage('SecDescription'),
    CustomMessage('SecSubCaption'));
  SecPage.Add(CustomMessage('SecPassword'), True);
  SecPage.Add(CustomMessage('SecPassword2'), True);
  SecPage.Values[0] := gSecretAdminPassword;
  SecPage.Values[1] := gSecretAdminPassword;
end;

procedure CreateNetPage(AfterID: Integer; var EnvLines: TArrayOfString);
var
  Top, LabelWidth: Integer;
  Port: String;
begin
  NetPage := CreateCustomPage(AfterID, CustomMessage('NetCaption'), CustomMessage('NetDescription'));
  Top := 0;
  NetHttpsCheck := TNewCheckBox.Create(NetPage);
  NetHttpsCheck.Parent := NetPage.Surface;
  NetHttpsCheck.Top := Top;
  NetHttpsCheck.Width := NetPage.SurfaceWidth;
  NetHttpsCheck.Height := ScaleY(17);
  NetHttpsCheck.Caption := CustomMessage('NetHttps');
  NetHttpsCheck.Checked := InfGetBool('Https', True);
  Top := Top + ScaleY(24);
  NetDomainCheck := TNewCheckBox.Create(NetPage);
  NetDomainCheck.Parent := NetPage.Surface;
  NetDomainCheck.Top := Top;
  NetDomainCheck.Width := NetPage.SurfaceWidth;
  NetDomainCheck.Height := ScaleY(17);
  NetDomainCheck.Caption := CustomMessage('NetDomain');
  NetDomainCheck.Checked := InfGetBool('DomainProfile', False);
  Top := Top + ScaleY(32);
  LabelWidth := ScaleX(220);
  NetHttpLabel := NewLabel(NetPage, CustomMessage('NetHttpPort'), Top + ScaleY(3));
  Port := EnvGet(EnvLines, 'VMS_HTTP_PORT');
  if Port = '' then
    Port := DEFAULT_HTTP_PORT;
  NetHttpEdit := NewEdit(NetPage, InfGet('HttpPort', Port), LabelWidth, Top, ScaleX(70));
  Top := Top + NetHttpEdit.Height + ScaleY(8);
  NetHttpsLabel := NewLabel(NetPage, CustomMessage('NetHttpsPort'), Top + ScaleY(3));
  Port := EnvGet(EnvLines, 'VMS_HTTPS_PORT');
  if Port = '' then
    Port := DEFAULT_HTTPS_PORT;
  NetHttpsEdit := NewEdit(NetPage, InfGet('HttpsPort', Port), LabelWidth, Top, ScaleX(70));
  Top := Top + NetHttpsEdit.Height + ScaleY(18);
  NetPublicLabel := NewLabel(NetPage, '', Top);
  NetPublicLabel.AutoSize := False;
  NetPublicLabel.WordWrap := True;
  NetPublicLabel.Width := NetPage.SurfaceWidth;
  NetPublicLabel.Height := ScaleY(48);
  Top := Top + ScaleY(52);
  NetPrivateCheck := TNewCheckBox.Create(NetPage);
  NetPrivateCheck.Parent := NetPage.Surface;
  NetPrivateCheck.Top := Top;
  NetPrivateCheck.Width := NetPage.SurfaceWidth;
  NetPrivateCheck.Height := ScaleY(17);
  NetPrivateCheck.Caption := CustomMessage('NetSetPrivate');
  NetPrivateCheck.Checked := InfGetBool('SetPrivateNetwork', False);
  gPublicNetworkCount := DetectPublicNetworks(gPublicNetworks);
  if gPublicNetworkCount > 0 then
  begin
    NetPublicLabel.Caption := FmtMessage(CustomMessage('NetPublicWarning'), [gPublicNetworks]);
    Log('Redes públicas activas: ' + gPublicNetworks);
  end
  else
  begin
    NetPublicLabel.Caption := CustomMessage('NetNoPublic');
    NetPrivateCheck.Visible := False;
  end;
end;

procedure ResultSaveClick(Sender: TObject);
var
  FileName: String;
begin
  FileName := 'vms-diagnostico-' + GetDateTimeString('yyyymmdd-hhnnss', #0, #0) + '.zip';
  if GetSaveFileName(CustomMessage('ResultSavePrompt'), FileName, ExpandConstant('{userdocs}'),
    CustomMessage('ResultSaveFilter'), 'zip') then
  begin
    if RunVmsctl(NewVmsctl, 'diag bundle --out ' + AddQuotes(FileName)) = VMSCTL_OK then
      MsgBox(FmtMessage(CustomMessage('ResultSaved'), [FileName]), mbInformation, MB_OK)
    else
      MsgBox(FmtMessage(CustomMessage('ResultSaveFailed'), [gLastVmsctlMessage]), mbError, MB_OK);
  end;
end;

procedure CreateResultPage;
begin
  ResultPage := CreateOutputMsgMemoPage(wpInstalling, CustomMessage('ResultCaption'),
    CustomMessage('ResultDescription'), '', '');
  ResultSaveButton := TNewButton.Create(ResultPage);
  ResultSaveButton.Parent := ResultPage.Surface;
  ResultSaveButton.Caption := CustomMessage('ResultSave');
  ResultSaveButton.Width := WizardForm.CalculateButtonWidth([ResultSaveButton.Caption]);
  ResultSaveButton.Height := ScaleY(23);
  ResultSaveButton.Top := ResultPage.SurfaceHeight - ResultSaveButton.Height;
  ResultSaveButton.OnClick := @ResultSaveClick;
  ResultPage.RichEditViewer.Height := ResultSaveButton.Top - ResultPage.RichEditViewer.Top - ScaleY(8);
end;

procedure CreateAllPages;
var
  EnvLines: TArrayOfString;
begin
  LoadEnv(EnvLines);
  gEnvHasPgDsn := (EnvGet(EnvLines, 'VMS_PG_DSN') <> '') or (EnvGet(EnvLines, 'VMS_CENTRAL_PG_DSN') <> '');
  CreateRecPage;
  CreateSitePage(RecPage.ID);
  CreateCentralPage(SitePage.ID);
  CreateSecPage(CentralPage.ID);
  CreateNetPage(SecPage.ID, EnvLines);
  CreateResultPage;
  RecInfoLabel.Caption := RecInfoText;
end;
