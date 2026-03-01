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

Write-Host "[debug] validating metadata.s4_scope=memory"
Write-Host "[debug] threadA=$threadA"
Write-Host "[debug] threadB=$threadB"
Write-Host "[debug] memory_id=$mem agent_id=$agent scope=$scope"

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

function Get-LatestS4Text {
  param([Parameter(Mandatory = $true)]$Summaries)

  if (-not $Summaries.s4 -or $Summaries.s4.Count -eq 0) {
    return ""
  }

  $latest = $Summaries.s4[0]
  foreach ($key in @("summary", "content", "text")) {
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

Write-Host "`n[debug] query summaries after round 4"
$sumA = Get-Summaries -ThreadId $threadA
$sumB = Get-Summaries -ThreadId $threadB

Write-Host "[debug] threadA summaries.s4:"
$sumA.s4 | ConvertTo-Json -Depth 50

Write-Host "`n[debug] threadB summaries.s4:"
$sumB.s4 | ConvertTo-Json -Depth 50

$s4TextA = Get-LatestS4Text -Summaries $sumA
$s4TextB = Get-LatestS4Text -Summaries $sumB

$hasS4 = ($sumA.s4 -and $sumA.s4.Count -gt 0) -or ($sumB.s4 -and $sumB.s4.Count -gt 0)
$combinedText = ($s4TextA + "\n" + $s4TextB)
$relatedToThisRun = ($combinedText -match [Regex]::Escape($runTag)) -or ($combinedText -match "s4m-A") -or ($combinedText -match "s4m-B")

if ($hasS4 -and $allScopesMemory -and $relatedToThisRun) {
  Write-Host "`nPASS: round4后检测到s4，且x-s4-scope=memory。"
} else {
  Write-Host "`nFAIL: round4后未满足s4或scope条件。"
  Write-Host "[debug] hasS4=$hasS4 allScopesMemory=$allScopesMemory relatedToThisRun=$relatedToThisRun"
  exit 1
}
