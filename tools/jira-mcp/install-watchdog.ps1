<#
  Регистрирует задачу Планировщика Windows, которая раз в N минут дёргает
  watchdog.sh. Резидентного процесса не появляется: Планировщик — уже
  работающая системная служба, каждый тик живёт ~50 мс и умирает.

  Задача запускается через wscript.exe + скрытый VBS-шим, иначе Git Bash
  раз в 5 минут мигал бы консольным окном.

  Принципал — текущий пользователь, LogonType Interactive: это нужно, чтобы
  при истёкшем сертификате tsh мог открыть браузер в вашей сессии.
  Права администратора не требуются.

  Установка:  powershell -ExecutionPolicy Bypass -File install-watchdog.ps1
  Удаление:   powershell -ExecutionPolicy Bypass -File install-watchdog.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [int]$IntervalMinutes = 5,
    [string]$TaskName = 'JiraMcpTunnelWatchdog'
)

$ErrorActionPreference = 'Stop'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[ok] задача '$TaskName' удалена"
    } else {
        Write-Host "[skip] задачи '$TaskName' нет"
    }
    return
}

# --- bash.exe ---------------------------------------------------------------
$bash = @(
    'C:\Program Files\Git\bin\bash.exe',
    'C:\Program Files (x86)\Git\bin\bash.exe'
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $bash) {
    $cmd = Get-Command bash.exe -ErrorAction SilentlyContinue |
           Where-Object { $_.Source -notmatch 'System32|WindowsApps' } |
           Select-Object -First 1
    if ($cmd) { $bash = $cmd.Source }
}
if (-not $bash) { throw 'bash.exe от Git for Windows не найден — укажи путь вручную в этом скрипте' }

# --- пути -------------------------------------------------------------------
$here     = $PSScriptRoot
$watchdog = Join-Path $here 'watchdog.sh'
if (-not (Test-Path $watchdog)) { throw "не найден $watchdog" }

# C:\path\to -> /c/path/to (Git Bash понимает такой путь как аргумент-скрипт)
$posix = '/' + $watchdog.Substring(0,1).ToLower() + $watchdog.Substring(2).Replace('\','/')

# --- VBS-шим (скрытый запуск, без мигания консоли) --------------------------
$vbsPath = Join-Path $here 'watchdog-hidden.vbs'
$vbs = @"
' Генерируется install-watchdog.ps1 — правки затрутся при переустановке.
' Запускает watchdog.sh скрыто (0 = без окна), не дожидаясь завершения.
Set sh = CreateObject("WScript.Shell")
sh.Run """$bash"" ""$posix""", 0, False
"@
Set-Content -Path $vbsPath -Value $vbs -Encoding ASCII
Write-Host "[ok] шим: $vbsPath"

# --- задача -----------------------------------------------------------------
$action = New-ScheduledTaskAction -Execute 'wscript.exe' `
                                  -Argument ('"{0}"' -f $vbsPath) `
                                  -WorkingDirectory $here

$interval = New-TimeSpan -Minutes $IntervalMinutes

# Once-триггер «от сейчас» с бесконечным повтором + AtLogOn на случай перелогина.
# Бесконечность задаётся НЕ через [TimeSpan]::MaxValue — Планировщик отвергает
# такой XML («значение за пределами допустимого диапазона»). Рабочие варианты:
# Duration = PT0S, а если и он не принят — элемент <Duration> вовсе отсутствует.
$tOnce = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                                  -RepetitionInterval $interval `
                                  -RepetitionDuration ([TimeSpan]::Zero)
$tLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet -Hidden `
                                         -StartWhenAvailable `
                                         -MultipleInstances IgnoreNew `
                                         -AllowStartIfOnBatteries `
                                         -DontStopIfGoingOnBatteries `
                                         -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive `
                                        -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[ok] старая задача снята"
}

$desc = 'Лечит туннель Teleport к Jira для MCP-сервера jira. Сам туннель не поднимает — только если он был поднят и умер.'

function Register-Watchdog {
    param($Triggers)
    Register-ScheduledTask -TaskName $TaskName `
                           -Action $action `
                           -Trigger $Triggers `
                           -Settings $settings `
                           -Principal $principal `
                           -Description $desc `
                           -ErrorAction Stop | Out-Null
}

try {
    Register-Watchdog -Triggers @($tOnce, $tLogon)
} catch {
    Write-Host "[warn] Duration=PT0S не принят: $($_.Exception.Message)"
    Write-Host "[info] пробую вариант без элемента <Duration>"
    $tOnce.Repetition.Duration = $null
    Register-Watchdog -Triggers @($tOnce, $tLogon)
}

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "задача '$TaskName' не зарегистрировалась"
}

Write-Host "[ok] задача '$TaskName' зарегистрирована, интервал $IntervalMinutes мин"
Write-Host "     проверка: Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "     лог:      $env:USERPROFILE\.local\state\jira-mcp\tunnel.log"
