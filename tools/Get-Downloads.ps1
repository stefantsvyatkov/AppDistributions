<#
.SYNOPSIS
    Колко пъти са изтеглени файловете на релийсите в AppDistributions.
.DESCRIPTION
    Пита GitHub (без вход, публичните данни) и казва за всеки релийс колко пъти е изтеглен всеки негов файл, после
    общо за всяко приложение и накрая каталога на BST Radio. GitHub брои само изтеглянията на файла: не казва кой,
    кога, нито от коя държава. В броя влизат и обновяванията, които приложенията правят сами: BST Radio тегли новия
    инсталатор от същото място. Черновите (bst-radio-data) не се показват.
    Пускане с двоен клик: Downloads.cmd до този файл.
#>
[CmdletBinding()]
param([string]$Repository = "stefantsvyatkov/AppDistributions")

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# Началото на името на релийса и чие е.
$apps = [ordered]@{
    "bst-win-"     = "BST Radio за Windows"
    "bst-android-" = "BST Radio за Android"
    "crr-win-"     = "Clear RSS Reader за Windows"
    "crr-android-" = "Clear RSS Reader за Android"
    "vox-win-"     = "Vox TXT за Windows"
    "vox-android-" = "Vox TXT за Android"
}

function Kind([string]$file) {
    switch -Wildcard ($file) {
        "*Setup*.exe"    { return "инсталатор" }
        "*portable*.zip" { return "преносима версия" }
        "*.apk"          { return "APK" }
        default          { return $file }
    }
}

try {
    $releases = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repository/releases?per_page=100" `
        -Headers @{ Accept = "application/vnd.github+json" }
}
catch {
    Write-Output "GitHub не отговори: $($_.Exception.Message)"
    exit 1
}

Write-Output "Изтегляния от GitHub към $((Get-Date).ToString('dd.MM.yyyy HH:mm'))."
Write-Output ""

$totals = [ordered]@{}
foreach ($release in @($releases | Where-Object { -not $_.draft -and $_.tag_name -ne "bst-radio-catalog" } |
        Sort-Object { [datetime]$_.published_at } -Descending)) {
    $tag = $release.tag_name
    $prefix = $apps.Keys | Where-Object { $tag.StartsWith($_) } | Select-Object -First 1
    $app = if ($prefix) { $apps[$prefix] } else { $tag }
    $version = if ($prefix) { " " + $tag.Substring($prefix.Length) } else { "" }
    $sum = 0
    $parts = foreach ($asset in $release.assets) {
        $sum += $asset.download_count
        "$(Kind $asset.name) $($asset.download_count)"
    }
    $date = ([datetime]$release.published_at).ToLocalTime().ToString("dd.MM.yyyy")
    Write-Output "$app${version}, качена на ${date}: $($parts -join ', '). Общо $sum."
    $totals[$app] = [int]$totals[$app] + $sum
}

if ($totals.Count -gt 0) {
    Write-Output ""
    foreach ($app in $totals.Keys) {
        Write-Output "$app, всички версии: $($totals[$app])."
    }
}

# Пакетите на каталога се сменят с всеки нов каталог и броенето им почва отначало: броят е приблизително колко копия
# на BST Radio са си взели каталога, откакто е качен този пакет.
$catalog = $releases | Where-Object { $_.tag_name -eq "bst-radio-catalog" } | Select-Object -First 1
if ($catalog) {
    $windows = $catalog.assets | Where-Object { $_.name -like "*-windows.bstc" } | Select-Object -First 1
    $android = $catalog.assets | Where-Object { $_.name -like "*-android.bstc" } | Select-Object -First 1
    if ($windows -or $android) {
        $since = ([datetime]($windows, $android | Where-Object { $_ } | Select-Object -First 1).updated_at).ToLocalTime()
        Write-Output ""
        Write-Output ("Каталогът на BST Radio, качен на {0} в {1}: за Windows {2}, за Android {3}. Приблизително толкова копия на BST Radio са го взели досега." -f `
            $since.ToString("dd.MM.yyyy"), $since.ToString("HH:mm"), [int]$windows.download_count, [int]$android.download_count)
    }
}
