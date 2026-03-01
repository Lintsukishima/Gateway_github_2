#requires -version 5.1
param(
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [string]$Model = "qwen/qwen3-235b-a22b-2507",
  [int]$Rounds = 4,
  [string]$MemoryId = "mem:nekoyue:core",
  [string]$AgentId = "companion:Lishuo-rui",
  [string]$Scope = "thread"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Console UTF-8 ---
chcp 65001 | Out-Null
[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

if ($Rounds -ne 4) {
  throw "本验收脚本固定为 4 轮触发验证，请传入 -Rounds 4（当前: $Rounds）"
}

Write-Host "[提示] 请先确认服务端已将 S60_EVERY_USER_TURNS 临时设置为 4（通过 .env 或启动参数），并已重启服务。"
Write-Host "[提示] 当前将以 memory_id='$MemoryId' + agent_id='$AgentId' 做 4 轮请求验证。"

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$threadA = "rk:th:s60-a-$ts"
$threadB = "rk:th:s60-b-$ts"
$threads = @($threadA, $threadB)

function Invoke-JsonUtf8 {
  param(
    [Parameter(Mandatory = $true)][string]$Uri,
    [Parameter(Mandatory = $true)][hashtable]$BodyObject
  )

  $json = $BodyObject | ConvertTo-Json -Depth 60 -Compress
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

  $params = @{
    Uri             = $Uri.Trim()
    Method          = 'Post'
    ContentType     = 'application/json; charset=utf-8'
    Headers         = @{ "Accept" = "application/json" }
    Body            = $bytes
    UseBasicParsing = $true
  }

  return Invoke-WebRequest @params
}

function Convert-BytesToHex {
  param(
    [Parameter(Mandatory = $true)][byte[]]$Bytes,
    [int]$MaxBytes = 120
  )

  if (-not $Bytes) { return "" }
  $slice = $Bytes | Select-Object -First $MaxBytes
  return (($slice | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-WebResponseBytes {
  param([Parameter(Mandatory = $true)]$Response)

  if ($null -ne $Response.RawContentStream) {
    $stream = $Response.RawContentStream
    if ($stream.CanSeek) { $stream.Position = 0 }
    $ms = New-Object System.IO.MemoryStream
    $stream.CopyTo($ms)
    if ($stream.CanSeek) { $stream.Position = 0 }
    return $ms.ToArray()
  }

  if ($null -ne $Response.Content) {
    return [System.Text.Encoding]::UTF8.GetBytes([string]$Response.Content)
  }

  return [byte[]]@()
}

function Get-JsonWithUtf8Decode {
  param(
    [Parameter(Mandatory = $true)][string]$Uri,
    [string]$Label = ""
  )

  $default = Invoke-RestMethod -Method Get -Uri $Uri
  $resp = Invoke-WebRequest -UseBasicParsing -Method Get -Uri $Uri
  $bytes = Get-WebResponseBytes -Response $resp
  $hex120 = Convert-BytesToHex -Bytes $bytes -MaxBytes 120
  $utf8Text = [System.Text.Encoding]::UTF8.GetString($bytes)
  $utf8Json = $utf8Text | ConvertFrom-Json

  if ($Label) {
    Write-Host ("`n[debug] $Label raw utf8 hex(120b): " + $hex120)
  } else {
    Write-Host ("`n[debug] raw utf8 hex(120b): " + $hex120)
  }
  Write-Host "[debug] utf8 text preview(200):"
  Write-Host ($utf8Text.Substring(0, [Math]::Min(200, $utf8Text.Length)))

  return [pscustomobject]@{
    Default  = $default
    Utf8Json = $utf8Json
  }
}

function Print-MemoryHeaders {
  param(
    [Parameter(Mandatory = $true)]$Headers,
    [Parameter(Mandatory = $true)][int]$Round,
    [Parameter(Mandatory = $true)][string]$ThreadId
  )

  Write-Host "[round $Round] thread_id=$ThreadId"
  Write-Host ("[round $Round] x-memory-id = " + $Headers["x-memory-id"])
  Write-Host ("[round $Round] x-agent-id  = " + $Headers["x-agent-id"])
}

Write-Host "[debug] thread candidates: $($threads -join ', ')"

$lastThread = $null
1..$Rounds | ForEach-Object {
  $round = $_
  $threadId = $threads[($round - 1) % $threads.Count]
  $lastThread = $threadId

  $body = @{
    model = $Model
    messages = @(@{ role = "user"; content = "S60 memory+agent 验收第${round}轮，thread=$threadId" })
    stream = $false
    metadata = @{
      memory_id = $MemoryId
      agent_id = $AgentId
      thread_id = $threadId
      s4_scope = $Scope
    }
  }

  $jsonRaw = $body | ConvertTo-Json -Depth 60 -Compress
  $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes($jsonRaw)
  Write-Host "`n[debug] round $round request body preview(200):"
  Write-Host ($jsonRaw.Substring(0, [Math]::Min(200, $jsonRaw.Length)))
  Write-Host ("[debug] round $round request utf8 hex(120b): " + (Convert-BytesToHex -Bytes $jsonBytes -MaxBytes 120))

  $resp = Invoke-JsonUtf8 -Uri "$BaseUrl/v1/chat/completions" -BodyObject $body
  Print-MemoryHeaders -Headers $resp.Headers -Round $round -ThreadId $threadId

  Write-Host "[debug] round $round response body preview(200):"
  Write-Host ($resp.Content.Substring(0, [Math]::Min(200, $resp.Content.Length)))
}

if (-not $lastThread) {
  throw "未生成有效 thread_id，无法继续查询 summaries。"
}

Write-Host "`n[debug] 使用第 4 轮 thread_id 查询 summaries: $lastThread"
$summariesResult = Get-JsonWithUtf8Decode -Uri "$BaseUrl/api/v1/sessions/$lastThread/summaries" -Label "summaries"
$summariesDefault = $summariesResult.Default
$summariesUtf8 = $summariesResult.Utf8Json

Write-Host "`n[debug] latest s60 summary (default path):"
if ($summariesDefault.s60 -and $summariesDefault.s60.Count -gt 0) {
  $summariesDefault.s60[0] | ConvertTo-Json -Depth 60
} else {
  Write-Host "no s60 summary found"
}

Write-Host "`n[debug] latest s60 summary (forced utf8 path):"
if ($summariesUtf8.s60 -and $summariesUtf8.s60.Count -gt 0) {
  $summariesUtf8.s60[0] | ConvertTo-Json -Depth 60
} else {
  Write-Host "no s60 summary found"
}

$dbgResult = Get-JsonWithUtf8Decode -Uri "$BaseUrl/api/v1/sessions/$lastThread/summaries/debug?limit=120" -Label "summaries/debug"
$dbgDefault = $dbgResult.Default
$dbgUtf8 = $dbgResult.Utf8Json

Write-Host "`n[debug] summaries/debug events (default path):"
$dbgDefault.events | ConvertTo-Json -Depth 60

Write-Host "`n[debug] summaries/debug events (forced utf8 path):"
$dbgUtf8.events | ConvertTo-Json -Depth 60

$hasS60 = ($summariesDefault.s60 -and $summariesDefault.s60.Count -gt 0) -or ($summariesUtf8.s60 -and $summariesUtf8.s60.Count -gt 0)
$events = @()
if ($dbgDefault.events) { $events += $dbgDefault.events }
if ($dbgUtf8.events) { $events += $dbgUtf8.events }

$hasRunS60 = $false
foreach ($ev in $events) {
  $evText = ($ev | ConvertTo-Json -Depth 20 -Compress)
  if ($evText -match 'run_s60' -or $evText -match 'to_turn') {
    $hasRunS60 = $true
    break
  }
}

if ($hasS60 -or $hasRunS60) {
  Write-Host "`nPASS: 已发现 s60 记录或 run_s60 相关 debug 事件。"
  exit 0
}

Write-Host "`nFAIL: 达到阈值后未发现 s60，且 debug 中无 run_s60 相关事件。"
exit 2
