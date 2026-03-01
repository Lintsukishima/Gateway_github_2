#requires -version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Console UTF-8 ---
chcp 65001 | Out-Null
[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$threadA = "rk:th:s4m-A-$ts"
$threadB = "rk:th:s4m-B-$ts"
$mem = "mem:nekoyue:core"
$agent = "companion:Lishuo-rui"
$scope = "memory"
$base = "http://127.0.0.1:8000"
$model = "qwen/qwen3-235b-a22b-2507"
$runTag = "s4-memory-run-$ts"
$maxWaitSec = 90
$pollIntervalSec = 3

Write-Host "[debug] validating metadata.s4_scope=memory"
Write-Host "[debug] threadA=$threadA"
Write-Host "[debug] threadB=$threadB"
Write-Host "[debug] memory_id=$mem agent_id=$agent scope=$scope"
Write-Host "[debug] runTag=$runTag"

function ConvertTo-Array {
  param([Parameter(Mandatory = $false)]$Value)

  if ($null -eq $Value) {
    return @()
  }

  # Always return a true array so .Count is safe under StrictMode.
  return @($Value)
}

function Get-ObjectKeys {
  param([Parameter(Mandatory = $false)]$Obj)

  if ($null -eq $Obj -or -not $Obj.PSObject) {
    return "<none>"
  }

  $keys = @($Obj.PSObject.Properties.Name)
  if ($keys.Count -eq 0) {
    return "<none>"
  }

  return ($keys -join ",")
}

function Invoke-ChatCompletionUtf8 {
  param(
    [Parameter(Mandatory = $true)][string]$ThreadId,
    [Parameter(Mandatory = $true)][int]$Round
  )

  $body = @{
    model = $model
    messages = @(@{ role = "user"; content = "S4 memory scope check round-${Round}; run=${runTag}; thread=${ThreadId}; keep this info" })
    stream = $false
    metadata = @{
      thread_id = $ThreadId
      memory_id = $mem
      agent_id = $agent
      s4_scope = $scope
    }
  }

  $json = $body | ConvertTo-Json -Depth 50 -Compress
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

  return Invoke-WebRequest -Uri "$base/v1/chat/completions" `
    -Method Post `
    -ContentType "application/json; charset=utf-8" `
    -Headers @{ "Accept" = "application/json" } `
    -Body $bytes `
    -UseBasicParsing
}

function Print-CoreHeaders {
  param(
    [Parameter(Mandatory = $true)]$Headers,
    [Parameter(Mandatory = $true)][int]$Round,
    [Parameter(Mandatory = $true)][string]$ThreadId
  )

  Write-Host "`n[round $Round][$ThreadId] headers:"
  Write-Host ("x-thread-id = " + $Headers["x-thread-id"])
  Write-Host ("x-memory-id = " + $Headers["x-memory-id"])
  Write-Host ("x-agent-id  = " + $Headers["x-agent-id"])
  Write-Host ("x-s4-scope  = " + $Headers["x-s4-scope"])
}

function Get-Summaries {
  param([Parameter(Mandatory = $true)][string]$ThreadId)

  return Invoke-RestMethod -Method Get -Uri "$base/api/v1/sessions/$ThreadId/summaries" -UseBasicParsing
}

function Extract-S4Entries {
  param([Parameter(Mandatory = $true)]$Summaries)

  if ($Summaries.PSObject.Properties.Name -contains "s4") {
    return (ConvertTo-Array -Value $Summaries.s4)
  }

  if (($Summaries.PSObject.Properties.Name -contains "data") -and $Summaries.data) {
    if ($Summaries.data.PSObject.Properties.Name -contains "s4") {
      return (ConvertTo-Array -Value $Summaries.data.s4)
    }
  }

  if (($Summaries.PSObject.Properties.Name -contains "summaries") -and $Summaries.summaries) {
    if ($Summaries.summaries.PSObject.Properties.Name -contains "s4") {
      return (ConvertTo-Array -Value $Summaries.summaries.s4)
    }
  }

  return @()
}

function Get-LatestS4Text {
  param([Parameter(Mandatory = $false)][AllowNull()]$S4Entries)

  $arr = @(ConvertTo-Array -Value $S4Entries)
  if ($arr.Count -eq 0) {
    return ""
  }

  $latest = $arr[0]
  foreach ($key in @("summary", "content", "text", "message")) {
    if ($latest.PSObject.Properties.Name -contains $key -and $latest.$key) {
      return [string]$latest.$key
    }
  }

  return ($latest | ConvertTo-Json -Depth 50 -Compress)
}

$sequence = @(
  @{ Round = 1; Thread = $threadA },
  @{ Round = 2; Thread = $threadB },
  @{ Round = 3; Thread = $threadA },
  @{ Round = 4; Thread = $threadB }
)

$allScopesMemory = $true

foreach ($step in $sequence) {
  $resp = Invoke-ChatCompletionUtf8 -ThreadId $step.Thread -Round $step.Round
  Print-CoreHeaders -Headers $resp.Headers -Round $step.Round -ThreadId $step.Thread

  if ($resp.Headers["x-s4-scope"] -ne "memory") {
    $allScopesMemory = $false
  }
}

Write-Host "`n[debug] poll summaries after round 4 (maxWaitSec=$maxWaitSec, pollIntervalSec=$pollIntervalSec)"
$sumA = $null
$sumB = $null
$s4A = @()
$s4B = @()
$hasS4 = $false
$startPoll = Get-Date

while ($true) {
  $sumA = Get-Summaries -ThreadId $threadA
  $sumB = Get-Summaries -ThreadId $threadB

  $s4A = Extract-S4Entries -Summaries $sumA
  $s4B = Extract-S4Entries -Summaries $sumB

  $s4ACount = @(ConvertTo-Array -Value $s4A).Count
  $s4BCount = @(ConvertTo-Array -Value $s4B).Count
  if ($s4ACount -gt 0 -or $s4BCount -gt 0) {
    $hasS4 = $true
    break
  }

  $elapsedSec = [int]((Get-Date) - $startPoll).TotalSeconds
  if ($elapsedSec -ge $maxWaitSec) {
    break
  }

  Start-Sleep -Seconds $pollIntervalSec
}

$waitedSec = [int]((Get-Date) - $startPoll).TotalSeconds

$s4ACount = @(ConvertTo-Array -Value $s4A).Count
$s4BCount = @(ConvertTo-Array -Value $s4B).Count
Write-Host "[debug] threadA summaries.s4 (count=$s4ACount, keys=$(Get-ObjectKeys -Obj $sumA)):"
$s4A | ConvertTo-Json -Depth 50

Write-Host "`n[debug] threadB summaries.s4 (count=$s4BCount, keys=$(Get-ObjectKeys -Obj $sumB)):"
$s4B | ConvertTo-Json -Depth 50

$s4TextA = Get-LatestS4Text -S4Entries (ConvertTo-Array -Value $s4A)
$s4TextB = Get-LatestS4Text -S4Entries (ConvertTo-Array -Value $s4B)

$combinedText = ($s4TextA + "`n" + $s4TextB)
$relatedToThisRun = ($combinedText -match [Regex]::Escape($runTag)) -or ($combinedText -match "s4m-A") -or ($combinedText -match "s4m-B") -or ($combinedText -match "memory scope check") -or ($combinedText -match "round-")

if ($hasS4 -and $allScopesMemory) {
  if ($relatedToThisRun) {
    Write-Host "`nPASS: round4后检测到s4，且x-s4-scope=memory，摘要与本轮相关。"
  } else {
    Write-Host "`nPASS_WITH_WARN: 检测到s4且scope正确，但摘要未命中runTag（可能被模型抽象化）。"
    Write-Host "[debug] hasS4=$hasS4 allScopesMemory=$allScopesMemory relatedToThisRun=$relatedToThisRun waitedSec=$waitedSec"
  }
} else {
  Write-Host "`nFAIL: round4后未满足s4或scope条件。"
  Write-Host "[debug] hasS4=$hasS4 allScopesMemory=$allScopesMemory relatedToThisRun=$relatedToThisRun waitedSec=$waitedSec"
  Write-Host "[debug] last threadA summaries raw:"
  $sumA | ConvertTo-Json -Depth 50
  Write-Host "`n[debug] last threadB summaries raw:"
  $sumB | ConvertTo-Json -Depth 50
  exit 1
}
