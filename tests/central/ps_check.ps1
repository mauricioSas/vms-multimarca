# Validación estática de los scripts de PowerShell (la usa tests/central/test_deploy.py).
# Uso: pwsh -NoProfile -File ps_check.ps1 [-AnalyzerPath <carpeta PSScriptAnalyzer>] script1.ps1 script2.ps1 ...
# Salida: una línea por problema («PARSE …» o «PSSA …»). Sin problemas: «ALL OK».
# PositionalBinding = $false: sin esto, el primer script de la lista se enlaza a -AnalyzerPath
# (posición 0) cuando no se pasa el analizador, y Import-Module acabaría EJECUTANDO ese script.
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$AnalyzerPath = '',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths
)
$problems = 0
foreach ($p in $Paths) {
    $tokens = $null; $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$tokens, [ref]$errors) | Out-Null
    foreach ($e in $errors) {
        $problems++
        Write-Output ('PARSE {0}:{1}: {2}' -f $p, $e.Extent.StartLineNumber, $e.Message)
    }
}
if ($AnalyzerPath) {
    Import-Module $AnalyzerPath -ErrorAction Stop
    # Write-Host es intencionado (instalador interactivo con colores); el BOM no hace falta en PS 5.1
    # porque los scripts solo usan caracteres ASCII en el código y UTF-8 en comentarios/cadenas.
    $exclude = @('PSAvoidUsingWriteHost', 'PSUseShouldProcessForStateChangingFunctions', 'PSUseBOMForUnicodeEncodedFile')
    foreach ($p in $Paths) {
        foreach ($x in @(Invoke-ScriptAnalyzer -Path $p -Severity Warning, Error -ExcludeRule $exclude)) {
            $problems++
            Write-Output ('PSSA {0}:{1} [{2}] {3}: {4}' -f $p, $x.Line, $x.Severity, $x.RuleName, $x.Message)
        }
    }
}
if ($problems -eq 0) { Write-Output 'ALL OK' }
exit $problems
