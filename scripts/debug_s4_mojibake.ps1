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

Write-Host "[debug] thread_id/session_id = $th"

# 第 1 轮：保留响应头用于确认 session 对齐
$body1 = @{
  model = "qwen/qwen3-235b-a22b-2507"
  messages = @(@{ role = "user"; content = "S4 thread 新ID验收 第1轮：中文测试-猫咪-哥哥" })
  stream = $false
  metadata = @{ thread_id = $th; memory_id = $mem; agent_id = $agent; s4_scope = "thread" }
}
$resp1 = Invoke-JsonUtf8 -Uri "$base/v1/chat/completions" -BodyObject $body1
Write-Host ("X-Session-Id = " + $resp1.Headers["x-session-id"])
Write-Host ("X-Thread-Id  = " + $resp1.Headers["x-thread-id"])

# 第 2~4 轮：触发 S4
2..4 | ForEach-Object {
  $round = $_
  $body = @{
    model = "qwen/qwen3-235b-a22b-2507"
    messages = @(@{ role = "user"; content = "S4 thread 新ID验收 第${round}轮：中文测试-猫咪-哥哥" })
    stream = $false
    metadata = @{ thread_id = $th; memory_id = $mem; agent_id = $agent; s4_scope = "thread" }
  }
  [void](Invoke-JsonUtf8 -Uri "$base/v1/chat/completions" -BodyObject $body)
}

# 拉 summaries
$summaries = Invoke-RestMethod -Method Get -Uri "$base/api/v1/sessions/$th/summaries"

Write-Host "`n[debug] latest s4 summary:"
if ($summaries.s4 -and $summaries.s4.Count -gt 0) {
  $summaries.s4[0] | ConvertTo-Json -Depth 50
} else {
  Write-Host "no s4 summary found"
}

Write-Host "`n[hint] If service logs still show garbled kw, capture backend log lines containing:"
Write-Host "  - LLM raw response diagnostics"
Write-Host "  - LLM response decode mismatch"
Write-Host "  - S4 summary preview before persist"
Write-Host "  - S4 summary changed after DB persist"
