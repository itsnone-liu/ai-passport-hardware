# Push local commits to GitHub through the Git Data API.
# git's smart-HTTP transport stalls on this network; the REST API does not.
$ErrorActionPreference = 'Stop'
$repo = 'D:\dsh-work\repo\ai-passport-hardware'
$tok = $env:GH_TOKEN   # 从环境变量读；**不要把 token 写进文件**，本仓库是公开的
$base = 'https://api.github.com/repos/itsnone-liu/ai-passport-hardware'
$h = @{ Authorization = "Bearer $tok"; 'User-Agent' = 'dsh'; Accept = 'application/vnd.github+json' }
$tmp = Join-Path $env:TEMP 'ghblob.bin'

Set-Location $repo

$branch = if ($env:GH_BRANCH) { $env:GH_BRANCH } else { 'main' }
$refPath = "$base/git/ref/heads/$branch"
$created = $false
try {
    $ref = Invoke-RestMethod -Uri $refPath -Headers $h
} catch {
    # 分支还不存在：以 main 为基点，提交建好后再创建 ref
    $ref = Invoke-RestMethod -Uri "$base/git/ref/heads/main" -Headers $h
    $created = $true
}
$parentSha = $ref.object.sha
"branch: $branch  base: $parentSha  new-branch: $created"

# Commits to replay, oldest first (pass shas as arguments, or use the defaults)
$commits = if ($args.Count -gt 0) { $args } else { @('f8e1200', '6d8b51d') }
$commits = $commits | Where-Object { $_ -notlike '-*' }

foreach ($c in $commits) {
    $fullSha = (git rev-parse $c).Trim()
    $msg = (git log -1 --format=%B $fullSha) -join "`n"
    $names = (git diff-tree --no-commit-id --name-status -r "$fullSha~1" $fullSha) | Where-Object { $_ }

    $parentTree = (Invoke-RestMethod -Uri "$base/git/commits/$parentSha" -Headers $h).tree.sha
    $entries = @()

    foreach ($line in $names) {
        $parts = $line -split "`t"
        $status = $parts[0]
        $path = $parts[1]
        if ($status -eq 'D') {
            $entries += @{ path = $path; mode = '100644'; type = 'blob'; sha = $null }
            continue
        }
        $spec = "$c`:$path"
        cmd /c "git show $spec > `"$tmp`""
        if ($LASTEXITCODE -ne 0) { throw "git show failed for $spec" }
        $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($tmp))
        $blob = Invoke-RestMethod -Method Post -Uri "$base/git/blobs" -Headers $h `
            -Body (@{ content = $b64; encoding = 'base64' } | ConvertTo-Json) -ContentType 'application/json'
        $entries += @{ path = $path; mode = '100644'; type = 'blob'; sha = $blob.sha }
        "  blob $path"
    }

    $treeBody = @{ base_tree = $parentTree; tree = $entries } | ConvertTo-Json -Depth 6
    $tree = Invoke-RestMethod -Method Post -Uri "$base/git/trees" -Headers $h -Body $treeBody -ContentType 'application/json'

    $commitBody = @{ message = $msg; tree = $tree.sha; parents = @($parentSha) } | ConvertTo-Json -Depth 4
    $newCommit = Invoke-RestMethod -Method Post -Uri "$base/git/commits" -Headers $h -Body $commitBody -ContentType 'application/json'

    if ($created) {
        $newRef = @{ ref = "refs/heads/$branch"; sha = $newCommit.sha } | ConvertTo-Json
        Invoke-RestMethod -Method Post -Uri "$base/git/refs" -Headers $h -Body $newRef -ContentType 'application/json' | Out-Null
        $created = $false
    } else {
        $refBody = @{ sha = $newCommit.sha; force = $false } | ConvertTo-Json
        Invoke-RestMethod -Method Patch -Uri "$base/git/refs/heads/$branch" -Headers $h -Body $refBody -ContentType 'application/json' | Out-Null
    }

    "pushed $($newCommit.sha.Substring(0,7))  ($($names.Count) files)"
    $parentSha = $newCommit.sha
}

"done, remote head = $parentSha"