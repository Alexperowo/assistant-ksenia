function Resolve-KseniaPython {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [string]$ExplicitPath = '',
        [switch]$AllowSystemPython
    )

    $resolvedProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
    $candidates = [Collections.Generic.List[string]]::new()
    if ($ExplicitPath) { $candidates.Add($ExplicitPath) }
    if ($env:KSENIA_PYTHON) { $candidates.Add([string]$env:KSENIA_PYTHON) }

    $userConfigPath = Join-Path $resolvedProjectRoot 'config\user.json'
    if (Test-Path -LiteralPath $userConfigPath -PathType Leaf) {
        try {
            $userConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $userConfigPath |
                ConvertFrom-Json
            if ($userConfig.voice.python) {
                $candidates.Add([string]$userConfig.voice.python)
            }
        }
        catch {}
    }

    if ($env:VIRTUAL_ENV) {
        $candidates.Add((Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'))
    }
    $candidates.Add((Join-Path $resolvedProjectRoot '.venv\Scripts\python.exe'))
    $candidates.Add((Join-Path $resolvedProjectRoot 'venv\Scripts\python.exe'))
    $projectDrive = Split-Path $resolvedProjectRoot -Qualifier
    if ($projectDrive) {
        $candidates.Add((Join-Path $projectDrive 'AI\Butler\venv\Scripts\python.exe'))
    }
    if ($env:SystemDrive) {
        $candidates.Add((Join-Path $env:SystemDrive 'AI\Butler\venv\Scripts\python.exe'))
    }
    if ($env:LocalAppData) {
        $candidates.Add(
            (Join-Path $env:LocalAppData 'Ksenia\Butler\venv\Scripts\python.exe')
        )
    }
    if ($AllowSystemPython) {
        $systemPython = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($systemPython) { $candidates.Add([string]$systemPython.Source) }
    }

    $seen = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($candidate in $candidates) {
        if (-not $candidate) { continue }
        try { $resolved = [IO.Path]::GetFullPath($candidate) } catch {
            Write-Verbose "Пропущен некорректный путь Python: $candidate"
            continue
        }
        if (-not $seen.Add($resolved)) { continue }
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            Write-Verbose "Python отсутствует: $resolved"
            continue
        }
        try {
            $version = & $resolved -c 'import sys; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))' 2>$null |
                Select-Object -Last 1
            if ($LASTEXITCODE -eq 0 -and ([string]$version).Trim() -eq '3.12') {
                return $resolved
            }
            Write-Verbose "Отклонён Python ${resolved}: версия $version, код $LASTEXITCODE"
        }
        catch {
            Write-Verbose "Не удалось проверить Python ${resolved}: $($_.Exception.Message)"
            continue
        }
    }
    return $null
}
