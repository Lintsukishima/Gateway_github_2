#requires -version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Console UTF-8 ---
chcp 65001 | Out-Null
[Console]::InputEncoding  = [System.Text.UTF8Encoding]::new()
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$OutputEncoding = [System.Text.UTF8Encoding]::new()

$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$th = "rk:th:s4-thread-fresh-$ts"
$mem = "mem:nekoyue:core"
$agent = "companion:Lishuo-rui"
$base = "http://127.0.0.1:8000"

Write-Host "[debug] 请确保服务端已设置环境变量 OPENAI_PROXY_DEBUG_ECHO=1，并重启服务"

function Invoke-JsonUtf8 {
  param(
    [Parameter(Mandatory = $true)][string]$Uri,
    [Parameter(Mandatory = $true)][hashtable]$BodyObject
  )

  $json = $BodyObject | ConvertTo-Json -Depth 50 -Compress
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)

  # 使用 -UseBasicParsing 避免 WinPS 5.1 的安全提示
  return Invoke-WebRequest \
    -UseBasicParsing \
    -Method Post \
    -Uri $Uri \
    -ContentType "application/json; charset=utf-8" \
    -Headers @{ "Accept" = "application/json" } \
    -Body $bytes
}

function Print-DebugHeaders {
  param([Parameter(Mandatory = $true)]$Headers)

  $kwHex = $Headers["x-debug-keyword-hex"]
  $kwB64 = $Headers["x-debug-keyword-b64"]
  $txtHex = $Headers["x-debug-user-text-hex"]
  $txtB64 = $Headers["x-debug-user-text-b64"]

  $kwDecoded = ""
  $txtDecoded = ""
  if ($kwB64) {
    $kwDecoded = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($kwB64.Replace('-', '+').Replace('_', '/')))
  }
  if ($txtB64) {
    $txtDecoded = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($txtB64.Replace('-', '+').Replace('_', '/')))
  }

  Write-Host ("X-Session-Id             = " + $Headers["x-session-id"])
  Write-Host ("X-Thread-Id              = " + $Headers["x-thread-id"])
  Write-Host ("X-Debug-Keyword-Hex      = " + $kwHex)
  Write-Host ("X-Debug-Keyword-B64      = " + $kwB64)
  Write-Host ("X-Debug-Keyword-Decoded  = " + $kwDecoded)
  Write-Host ("X-Debug-User-Text-Hex    = " + $txtHex)
  Write-Host ("X-Debug-User-Text-B64    = " + $txtB64)
  Write-Host ("X-Debug-User-Text-Decoded= " + $txtDecoded)
}

Write-Host "[debug] thread_id/session_id = $th"

# 第 1 轮：保留响应头用于确认 session 对齐
$body1 = @{
  model = "qwen/qwen3-235b-a22b-2507"
  messages = @(@{ role = "user"; content = "S4 thread 新ID验收 第1轮：中文测试-猫咪-哥哥" })
  stream = $false
  metadata = @{ thread_id = $th; memory_id = $mem; agent_id = $agent; s4_scope = "thread" }
}
$resp1 = Invoke-JsonUtf8 -Uri "$base/v1/chat/completions" -BodyObject $body1
Print-DebugHeaders -Headers $resp1.Headers
Write-Host "[debug] round1 response body preview(200):"
$resp1.Content.Substring(0, [Math]::Min(200, $resp1.Content.Length))

# 第 2~4 轮：触发 S4
2..4 | ForEach-Object {
  $round = $_
  $body = @{
    model = "qwen/qwen3-235b-a22b-2507"
    messages = @(@{ role = "user"; content = "S4 thread 新ID验收 第${round}轮：中文测试-猫咪-哥哥" })
    stream = $false
    metadata = @{ thread_id = $th; memory_id = $mem; agent_id = $agent; s4_scope = "thread" }
  }
  $jsonRaw = $body | ConvertTo-Json -Depth 50 -Compress
  $jsonBytes = [System.Text.Encoding]::UTF8.GetBytes($jsonRaw)
  $jsonHexBytes = $jsonBytes | Select-Object -First 120
  Write-Host "`n[debug] round $round request body preview(200):"
  $jsonRaw.Substring(0, [Math]::Min(200, $jsonRaw.Length))
  Write-Host ("[debug] round $round request utf8 hex(120b): " + (($jsonHexBytes | ForEach-Object { $_.ToString('x2') }) -join ''))

  $resp = Invoke-JsonUtf8 -Uri "$base/v1/chat/completions" -BodyObject $body
  Print-DebugHeaders -Headers $resp.Headers
  Write-Host "[debug] round $round response body preview(200):"
  $resp.Content.Substring(0, [Math]::Min(200, $resp.Content.Length))
}

# 拉 summaries
$summaries = Invoke-RestMethod -Method Get -Uri "$base/api/v1/sessions/$th/summaries"

Write-Host "`n[debug] latest s4 summary:"
if ($summaries.s4 -and $summaries.s4.Count -gt 0) {
  $summaries.s4[0] | ConvertTo-Json -Depth 50
} else {
  Write-Host "no s4 summary found"
}

Write-Host "`n[debug] summarizer debug events:"
$dbg = Invoke-RestMethod -Method Get -Uri "$base/api/v1/sessions/$th/summaries/debug?limit=80"
$dbg.events | ConvertTo-Json -Depth 50
