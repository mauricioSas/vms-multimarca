#Requires -Version 5.1
<#
.SYNOPSIS
  Prueba S1 (PLAN-V2 §4.7): ¿decodifica WebView2 por hardware 4 ventanas × 16 flujos H.264 640x360 a 15 fps?

.DESCRIPTION
  Lo ejecuta el usuario en el PC Windows del laboratorio (el de los 4 monitores y la GPU). No instala nada:
  todo va a una carpeta de trabajo que se puede borrar al terminar.

    1. Descarga MediaMTX v1.21.1 (comprobando su SHA-256 con la publicación oficial) y un ffmpeg de pruebas
       (gyan.dev, comprobando su SHA-256). ffmpeg solo genera los vídeos de prueba en este PC; no forma
       parte del producto.
    2. Genera dos clips (H.264 y H.265, 640x360, 15 fps) y los publica en bucle sin recomprimir.
    3. Fase A ($Minutes min): el visor Tauri (s1-visor.exe) abre $Windows ventanas × $Cells flujos WebRTC.
       Mide CPU total, CPU de WebView2, uso del motor de decodificación de vídeo de la GPU y, en cada
       flujo, «powerEfficientDecoder», el decodificador real y los fotogramas perdidos.
    4. Fase B (2 min): 4 flujos H.265 por WebRTC.
    5. Fase C (40 s): un fMP4 H.265 en <video> servido por el /get de MediaMTX (la vía de la reproducción).
    6. Escribe resultado\resultado-s1.json y resultado\resumen-s1.txt con el veredicto.

  Criterio de aprobado (PLAN-V2 §4.7): CPU media < 60 %, decodificación por hardware en ≥ 90 % de los
  flujos y < 1 % de fotogramas perdidos.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\s1.ps1
#>
[CmdletBinding()]
param(
    [int] $Minutes = 30,
    [int] $Windows = 4,
    [int] $Cells = 16,
    [string] $Work = (Join-Path $env:LOCALAPPDATA 'vms-s1'),
    [switch] $SkipHevc
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Viewer = Join-Path $Here 's1-visor.exe'
$Out = Join-Path $Here 'resultado'
$MediaMtxVersion = 'v1.21.1'
$FfmpegZipUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
$WarmupSeconds = 60

function Say([string] $text) { Write-Host "[$((Get-Date).ToString('HH:mm:ss'))] $text" }
function Sha256([string] $path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() }

if (-not (Test-Path $Viewer)) { throw "No encuentro s1-visor.exe junto a este script ($Here). Descomprime el kit completo." }
Get-ChildItem -LiteralPath $Here -Recurse | Unblock-File -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Work, $Out | Out-Null
$procs = New-Object System.Collections.ArrayList

try {
    # ------------------------------------------------------------------ 0. datos del equipo
    Say 'Leyendo datos del equipo…'
    $os = Get-CimInstance Win32_OperatingSystem
    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    $gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { "$($_.Name) (driver $($_.DriverVersion))" })
    $wv2 = $null
    foreach ($k in 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
                   'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}') {
        if (Test-Path $k) { $wv2 = (Get-ItemProperty $k).pv; break }
    }
    Add-Type -AssemblyName System.Windows.Forms
    $machine = [ordered]@{
        os = "$($os.Caption) $($os.Version)"; cpu = $cpu.Name; logical_cpus = [Environment]::ProcessorCount
        ram_gb = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1); gpus = $gpus; webview2 = $wv2
        monitors = [System.Windows.Forms.Screen]::AllScreens.Count
    }
    Say "Equipo: $($machine.cpu) · $($gpus -join ' / ') · WebView2 $wv2 · $($machine.monitors) monitores"

    # ------------------------------------------------------------------ 1. herramientas
    $mtxDir = Join-Path $Work "mediamtx-$MediaMtxVersion"
    $mtxExe = Join-Path $mtxDir 'mediamtx.exe'
    if (-not (Test-Path $mtxExe)) {
        Say "Descargando MediaMTX $MediaMtxVersion…"
        $zipName = "mediamtx_${MediaMtxVersion}_windows_amd64.zip"
        $base = "https://github.com/bluenviron/mediamtx/releases/download/$MediaMtxVersion"
        $zip = Join-Path $Work $zipName
        Invoke-WebRequest "$base/$zipName" -OutFile $zip -UseBasicParsing
        $sums = (Invoke-WebRequest "$base/checksums.sha256" -UseBasicParsing).Content -split "`n"
        $expected = ($sums | Where-Object { $_ -match [regex]::Escape($zipName) } | ForEach-Object { ($_ -split '\s+')[0] }) | Select-Object -First 1
        if (-not $expected -or (Sha256 $zip) -ne $expected.ToLowerInvariant()) { throw 'El SHA-256 de MediaMTX no coincide con la publicación oficial' }
        Expand-Archive -LiteralPath $zip -DestinationPath $mtxDir -Force
    }
    $ffDir = Join-Path $Work 'ffmpeg'
    $ffmpeg = Get-ChildItem -Path $ffDir -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
    if (-not $ffmpeg) {
        Say 'Descargando ffmpeg de pruebas (gyan.dev)…'
        $zip = Join-Path $Work 'ffmpeg.zip'
        Invoke-WebRequest $FfmpegZipUrl -OutFile $zip -UseBasicParsing
        $expected = ((Invoke-WebRequest "$FfmpegZipUrl.sha256" -UseBasicParsing).Content.Trim() -split '\s+')[0]
        if ((Sha256 $zip) -ne $expected.ToLowerInvariant()) { throw 'El SHA-256 de ffmpeg no coincide con el publicado' }
        Expand-Archive -LiteralPath $zip -DestinationPath $ffDir -Force
        $ffmpeg = Get-ChildItem -Path $ffDir -Recurse -Filter ffmpeg.exe | Select-Object -First 1 -ExpandProperty FullName
    }

    # ------------------------------------------------------------------ 2. clips de prueba
    $h264 = Join-Path $Work 'clip-h264.mp4'
    $h265 = Join-Path $Work 'clip-h265.mp4'
    if (-not (Test-Path $h264)) {
        Say 'Generando el clip H.264 640x360 15 fps…'
        & $ffmpeg -hide_banner -loglevel error -y -f lavfi -i 'testsrc2=size=640x360:rate=15' -t 60 `
            -c:v libx264 -profile:v main -bf 0 -g 30 -pix_fmt yuv420p $h264
    }
    if (-not $SkipHevc -and -not (Test-Path $h265)) {
        Say 'Generando el clip H.265…'
        & $ffmpeg -hide_banner -loglevel error -y -f lavfi -i 'testsrc2=size=640x360:rate=15' -t 60 `
            -c:v libx265 -x265-params 'bframes=0:keyint=30:log-level=error' -tag:v hvc1 -pix_fmt yuv420p $h265
    }

    # ------------------------------------------------------------------ 3. MediaMTX
    $rec = (Join-Path $Work 'rec') -replace '\\', '/'
    $yml = @"
logLevel: warn
authMethod: internal
authInternalUsers:
  - user: any
    pass:
    ips: ['127.0.0.1/32', '::1/128']
    permissions: [{action: publish}, {action: read}, {action: playback}, {action: api}]
api: yes
apiAddress: 127.0.0.1:9997
playback: yes
playbackAddress: 127.0.0.1:9996
playbackAllowOrigins: ['*']
rtsp: yes
rtspAddress: 127.0.0.1:8554
rtspTransports: [tcp]
rtmp: no
hls: no
srt: no
moq: no
webrtc: yes
webrtcAddress: 127.0.0.1:8889
webrtcAllowOrigins: ['*']
webrtcLocalUDPAddress: :8189
webrtcIPsFromInterfaces: yes
pathDefaults:
  recordPath: $rec/%path/%Y-%m-%d_%H-%M-%S-%f
  recordFormat: fmp4
  recordPartDuration: 1s
paths:
  h265-rec:
    record: yes
  all_others:
"@
    $ymlPath = Join-Path $Work 'mediamtx.yml'
    Set-Content -LiteralPath $ymlPath -Value $yml -Encoding ascii
    Say 'Arrancando MediaMTX…'
    $p = Start-Process -FilePath $mtxExe -ArgumentList "`"$ymlPath`"" -WorkingDirectory $Work -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $Out 'mediamtx.log') -RedirectStandardError (Join-Path $Out 'mediamtx.err.log')
    [void] $procs.Add($p)
    $end = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $end) { try { Invoke-RestMethod 'http://127.0.0.1:9997/v3/paths/list' | Out-Null; break } catch { Start-Sleep -Milliseconds 300 } }

    function Publish([string] $clip, [string] $path) {
        $a = "-hide_banner -loglevel error -re -stream_loop -1 -i `"$clip`" -c copy -an -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/$path"
        $p = Start-Process -FilePath $ffmpeg -ArgumentList $a -WindowStyle Hidden -PassThru
        [void] $script:procs.Add($p)
    }
    Say "Publicando $Cells flujos H.264 sin recomprimir…"
    1..$Cells | ForEach-Object { Publish $h264 ('h264-{0:D2}' -f $_) }
    if (-not $SkipHevc) { 1..4 | ForEach-Object { Publish $h265 ('h265-{0:D2}' -f $_) }; Publish $h265 'h265-rec' }
    Start-Sleep -Seconds 5

    # ------------------------------------------------------------------ medidas del sistema
    $samples = New-Object System.Collections.ArrayList
    function Sample([string] $phase) {
        $cpuT = (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'").PercentProcessorTime
        $engines = @(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine -ErrorAction SilentlyContinue)
        $dec = @($engines | Where-Object { $_.Name -like '*engtype_VideoDecode*' } | ForEach-Object { [double]$_.UtilizationPercentage })
        $g3d = @($engines | Where-Object { $_.Name -like '*engtype_3D*' } | ForEach-Object { [double]$_.UtilizationPercentage })
        $pp = @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process |
                Where-Object { $_.Name -like 'msedgewebview2*' -or $_.Name -like 's1-visor*' -or $_.Name -like 'ffmpeg*' -or $_.Name -like 'mediamtx*' })
        $n = [Environment]::ProcessorCount
        $sum = { param($pat) ([double](($pp | Where-Object { $_.Name -like $pat } | Measure-Object PercentProcessorTime -Sum).Sum)) / $n }
        [void] $script:samples.Add([pscustomobject]@{
            t = (Get-Date).ToString('o'); phase = $phase; cpu_total = [double]$cpuT
            cpu_webview2 = [math]::Round((& $sum 'msedgewebview2*') + (& $sum 's1-visor*'), 1)
            cpu_ffmpeg_mediamtx = [math]::Round((& $sum 'ffmpeg*') + (& $sum 'mediamtx*'), 1)
            gpu_videodecode_sum = [math]::Round(($dec | Measure-Object -Sum).Sum, 1)
            gpu_videodecode_max = [math]::Round(($dec | Measure-Object -Maximum).Maximum, 1)
            gpu_3d_max = [math]::Round(($g3d | Measure-Object -Maximum).Maximum, 1)
        })
    }
    function Run-Viewer([string] $phase, [string[]] $viewerArgs, [int] $timeoutSeconds) {
        Say "Fase ${phase}: abriendo el visor…"
        $v = Start-Process -FilePath $Viewer -ArgumentList $viewerArgs -PassThru
        [void] $script:procs.Add($v)
        $deadline = (Get-Date).AddSeconds($timeoutSeconds)
        while (-not $v.HasExited -and (Get-Date) -lt $deadline) {
            Sample $phase
            Start-Sleep -Seconds 5
        }
        if (-not $v.HasExited) { Stop-Process -Id $v.Id -Force -ErrorAction SilentlyContinue }
    }

    # ------------------------------------------------------------------ 4. fase A
    $statsA = Join-Path $Out 'stats-h264.jsonl'; Remove-Item $statsA -ErrorAction SilentlyContinue
    Say "Fase A: $Windows ventanas × $Cells flujos H.264 durante $Minutes min. Puedes mirar, pero no cierres las ventanas."
    Run-Viewer 'A' @('--windows', $Windows, '--cells', $Cells, '--prefix', 'h264-', '--minutes', $Minutes, '--out', "`"$statsA`"") ($Minutes * 60 + 120)

    # ------------------------------------------------------------------ 5-6. HEVC
    $statsB = Join-Path $Out 'stats-h265.jsonl'; Remove-Item $statsB -ErrorAction SilentlyContinue
    $statsC = Join-Path $Out 'stats-hevc-file.jsonl'; Remove-Item $statsC -ErrorAction SilentlyContinue
    if (-not $SkipHevc) {
        Run-Viewer 'B' @('--windows', 1, '--cells', 4, '--prefix', 'h265-', '--minutes', 2, '--out', "`"$statsB`"") 200
        $list = Invoke-RestMethod 'http://127.0.0.1:9996/list?path=h265-rec'
        $start = ($list | Select-Object -First 1).start
        $src = "http://127.0.0.1:9996/get?path=h265-rec&start=$([uri]::EscapeDataString($start))&duration=20&format=fmp4"
        $query = 'src=' + [uri]::EscapeDataString($src) + '&seconds=40'
        Run-Viewer 'C' @('--windows', 1, '--page', 's1-file.html', '--query', "`"$query`"", '--out', "`"$statsC`"") 90
    }

    # ------------------------------------------------------------------ 7. veredicto
    function Read-Jsonl([string] $path) { if (Test-Path $path) { Get-Content -LiteralPath $path -Encoding UTF8 | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json } } else { @() } }
    $a = @(Read-Jsonl $statsA)
    $t0 = if ($a.Count) { [datetime]$a[0].t } else { Get-Date }
    $steady = @($a | Where-Object { $_.kind -eq 'webrtc' -and ([datetime]$_.t - $t0).TotalSeconds -ge $WarmupSeconds })
    $lastPerWindow = @($a | Where-Object { $_.kind -eq 'webrtc' } | Group-Object w | ForEach-Object { $_.Group | Select-Object -Last 1 })
    $received = ($lastPerWindow | Measure-Object received -Sum).Sum
    $dropped = ($lastPerWindow | Measure-Object dropped -Sum).Sum
    $rTotal = ($lastPerWindow | Measure-Object rendered_total -Sum).Sum
    $rDropped = ($lastPerWindow | Measure-Object rendered_dropped -Sum).Sum
    $hwShare = if ($steady.Count) { ($steady | ForEach-Object { $_.power_efficient / [double]$_.cells } | Measure-Object -Average).Average } else { 0 }
    $playShare = if ($steady.Count) { ($steady | ForEach-Object { $_.playing / [double]$_.cells } | Measure-Object -Average).Average } else { 0 }
    $fps = if ($steady.Count) { ($steady | Measure-Object fps_avg -Average).Average } else { 0 }
    $decoders = @{}
    foreach ($s in $steady) { foreach ($p in $s.decoders.PSObject.Properties) { $decoders[$p.Name] = [int]($decoders[$p.Name]) + [int]$p.Value } }
    $sysA = @($samples | Where-Object { $_.phase -eq 'A' -and ([datetime]$_.t - $t0).TotalSeconds -ge $WarmupSeconds })
    $cpuAvg = if ($sysA.Count) { ($sysA | Measure-Object cpu_total -Average).Average } else { $null }
    $dropRatio = if ($received) { $dropped / [double]$received } else { $null }
    $renderDropRatio = if ($rTotal) { $rDropped / [double]$rTotal } else { $null }
    $b = @(Read-Jsonl $statsB | Where-Object { $_.kind -eq 'webrtc' })
    $c = @(Read-Jsonl $statsC)
    $hevcWebrtc = [bool]($b | Where-Object { $_.playing -gt 0 })
    $lastC = $c | Select-Object -Last 1
    $hevcFile = [bool]($lastC -and $lastC.advanced_samples -ge 3 -and $lastC.width -gt 0 -and -not $lastC.error)

    $checks = [ordered]@{
        cpu_media_menor_60 = ($null -ne $cpuAvg -and $cpuAvg -lt 60)
        hardware_90_por_ciento = ($hwShare -ge 0.9)
        perdidos_menor_1_por_ciento = ($null -ne $dropRatio -and $dropRatio -lt 0.01 -and ($null -eq $renderDropRatio -or $renderDropRatio -lt 0.01))
        flujos_con_video_98_por_ciento = ($playShare -ge 0.98)
    }
    $approved = -not ($checks.Values -contains $false)
    $result = [ordered]@{
        prueba = 'S1'; fecha = (Get-Date).ToString('o'); aprobado = $approved; comprobaciones = $checks
        equipo = $machine; parametros = [ordered]@{ minutos = $Minutes; ventanas = $Windows; flujos_por_ventana = $Cells; resolucion = '640x360'; fps = 15 }
        medidas = [ordered]@{
            cpu_total_media = if ($null -ne $cpuAvg) { [math]::Round($cpuAvg, 1) } else { $null }
            cpu_total_max = ($sysA | Measure-Object cpu_total -Maximum).Maximum
            cpu_webview2_media = ($sysA | Measure-Object cpu_webview2 -Average).Average
            cpu_ffmpeg_mediamtx_media = ($sysA | Measure-Object cpu_ffmpeg_mediamtx -Average).Average
            gpu_decodificacion_media = ($sysA | Measure-Object gpu_videodecode_max -Average).Average
            flujos_con_hardware = [math]::Round($hwShare, 3); flujos_con_video = [math]::Round($playShare, 3)
            fps_medios = [math]::Round($fps, 2)
            perdidos_webrtc = $dropRatio; perdidos_al_pintar = $renderDropRatio
            decodificadores = $decoders
        }
        hevc = [ordered]@{ webrtc = $hevcWebrtc; video_fmp4 = $hevcFile; detalle_video = $lastC }
    }
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Out 'resultado-s1.json') -Encoding utf8
    $samples | ConvertTo-Csv -NoTypeInformation | Set-Content -LiteralPath (Join-Path $Out 'sistema.csv') -Encoding utf8
    $lines = @(
        "Prueba S1 · $($result.fecha)",
        "Veredicto: $(if ($approved) { 'APROBADA: seguimos con Tauri (WebView2)' } else { 'NO APROBADA: revisar plan B (Electron) o bajar la resolución de los subflujos' })",
        "Equipo: $($machine.cpu) · $($gpus -join ' / ') · WebView2 $wv2 · $($machine.monitors) monitores",
        "CPU media: $($result.medidas.cpu_total_media) % (máx. $($result.medidas.cpu_total_max) %) · WebView2: $([math]::Round([double]$result.medidas.cpu_webview2_media,1)) %",
        "Flujos con decodificación por hardware: $([math]::Round($hwShare*100)) % · con vídeo: $([math]::Round($playShare*100)) % · $([math]::Round($fps,1)) fps",
        "Fotogramas perdidos: WebRTC $(if ($null -ne $dropRatio) { [math]::Round($dropRatio*100,3) } else { '?' }) % · al pintar $(if ($null -ne $renderDropRatio) { [math]::Round($renderDropRatio*100,3) } else { '?' }) %",
        "Decodificadores vistos: $(($decoders.GetEnumerator() | ForEach-Object { "$($_.Key) ($($_.Value))" }) -join ', ')",
        "HEVC por WebRTC: $(if ($hevcWebrtc) { 'sí' } else { 'no' }) · HEVC fMP4 en <video>: $(if ($hevcFile) { 'sí' } else { 'no' })"
    )
    $lines | Set-Content -LiteralPath (Join-Path $Out 'resumen-s1.txt') -Encoding utf8
    Write-Host ''
    $lines | ForEach-Object { Write-Host $_ }
    Write-Host ''
    Say "Listo. Envía la carpeta $Out (sobre todo resumen-s1.txt y resultado-s1.json)."
}
finally {
    foreach ($p in $procs) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
}
