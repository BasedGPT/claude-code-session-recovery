# Worktree Inspector -- report-only, no destructive actions
#
# Classifies every git worktree into one of eight health buckets and writes
# a markdown report. Default report hides healthy stubs to keep the output
# focused on actionable items.
#
# Implements the maintainer's chosen lifecycle. Not required by Claude Code.
# Adopt only if this workflow matches what you want.
#
# Bucket definitions (evaluated in priority order):
#   shrink-in-progress       -- .shrink-in-progress.* marker present
#   broken-registration      -- git registered path doesn't exist, or on-disk dir
#                               not registered with git
#   healthy-stub             -- .git + .worktree-shrunk.txt sentinel only (shrink-toolkit output)
#   recovery-stub            -- .git only, no sentinel (worktrees created without full
#                               materialisation, e.g. via --no-checkout or git worktree add)
#   broken-stub              -- sentinel present but payload is incomplete
#   active-materialised      -- files present, FS-locked (live session likely)
#   dirty-materialised       -- uncommitted tracked changes
#   unknown-local-files      -- untracked files outside the allowlist
#   clean-inactive-materialised -- registered, no live session, eligible for shrink
#
# Usage:
#   worktree_inspector.ps1                              # report (hides healthy stubs)
#   worktree_inspector.ps1 -ShowHealthy                # include healthy stubs
#   worktree_inspector.ps1 -Queue                      # list .shrink-when-safe markers
#   worktree_inspector.ps1 -ProbeLocks                 # opt-in FS-rename lock probe
#   worktree_inspector.ps1 -CurrentWorktree <path>     # skip lock probe for live session

param(
    [string]$RepoRoot = '',
    [string]$OutDir   = '',
    [string]$CurrentWorktree = '',
    [switch]$ShowHealthy,
    [switch]$Queue,
    [switch]$ProbeLocks
)

# Resolve defaults at runtime (param block can't reference $PWD)
if (-not $RepoRoot) { $RepoRoot = $PWD.Path }
if (-not $OutDir)   { $OutDir   = $PWD.Path }

$ErrorActionPreference = 'Continue'
$ts = Get-Date -Format 'yyyy-MM-dd-HH-mm'
$outFile = Join-Path $OutDir ("worktree-inspector-" + $ts + ".md")

# Cross-language contract with worktree_lifecycle.py. Keep these literal
# values in sync; this read-only inspector intentionally does not invoke
# Python or load executable tool code.
$MarkerReady = '.shrink-when-safe'
$MarkerInProgressPrefix = '.shrink-in-progress.'
$SentinelFilename = '.worktree-shrunk.txt'
$SentinelRequiredFields = @('Operation ID:', 'Branch:', 'Quarantine:', 'Manifest:', 'Shrunk:')

# Allowlist mirrors worktree_shrink.py. Keep in sync if either changes.
$DisposableDirs = @(
    'node_modules','__pycache__','.pytest_cache','.next','dist','build',
    '.cache','.coverage','htmlcov','.mypy_cache','.ruff_cache','.tox'
)
$PreservedDirs = @(
    '.playwright-mcp','.tmp_audit','.transcript-index','.dxt-sources','.obsidian','.agents'
)

# -------- Queue mode (short-circuit) --------------------------------------
if ($Queue) {
    $wtRoot = Join-Path $RepoRoot '.claude\worktrees'
    Write-Host "Worktree shrink queue"
    Write-Host ("  Root: " + $wtRoot)
    Write-Host ""
    if (-not (Test-Path $wtRoot)) {
        Write-Host "  (no .claude/worktrees directory)"
        return
    }
    $hits = @()
    Get-ChildItem $wtRoot -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $ready = Join-Path $_.FullName $MarkerReady
        $inProg = Get-ChildItem $_.FullName -Filter ($MarkerInProgressPrefix + '*') -Force -ErrorAction SilentlyContinue | Select-Object -First 1
        if (Test-Path $ready) {
            $hits += [PSCustomObject]@{ Name=$_.Name; State='READY'; Marker=$MarkerReady }
        } elseif ($inProg) {
            $hits += [PSCustomObject]@{ Name=$_.Name; State='IN_PROGRESS'; Marker=$inProg.Name }
        }
    }
    if ($hits.Count -eq 0) {
        Write-Host "  No markers found."
    } else {
        $hits | Format-Table -AutoSize
        Write-Host ""
        Write-Host ("  " + ($hits | Where-Object State -eq 'READY').Count + " ready, " +
                            ($hits | Where-Object State -eq 'IN_PROGRESS').Count + " in progress.")
        Write-Host "  Process the queue with: py -3 .claude\tools\worktree_shrink.py --queue --apply"
    }
    return
}

Write-Host "Worktree Inspector"
Write-Host ("  Repo:    " + $RepoRoot)
Write-Host ("  Output:  " + $outFile)
Write-Host ""

# ---- 1. Enumerate worktrees ----
$wtListRaw = & git -C $RepoRoot worktree list --porcelain
$worktrees = @()
$cur = $null
foreach ($line in $wtListRaw) {
    if ($line -like 'worktree *') {
        if ($cur) { $worktrees += $cur }
        $cur = [PSCustomObject]@{ Path=$line.Substring(9); HEAD=''; Branch=''; Locked=$false }
    } elseif ($line -like 'HEAD *') { $cur.HEAD = $line.Substring(5) }
      elseif ($line -like 'branch *') { $cur.Branch = $line.Substring(7) -replace '^refs/heads/','' }
      elseif ($line -eq 'locked') { $cur.Locked = $true }
}
if ($cur) { $worktrees += $cur }

Write-Host ("  Found " + $worktrees.Count + " git-registered worktrees")

# Restore stale probe siblings from interrupted opt-in lock probes.
foreach ($w in $worktrees) {
    $probe = $w.Path + '.probetest'
    if ((-not (Test-Path $w.Path)) -and (Test-Path $probe -PathType Container)) {
        try {
            Rename-Item $probe $w.Path -ErrorAction Stop
            Write-Host ("  restored stale probe sibling: " + $probe)
        } catch {
            Write-Host ("  WARNING: could not restore stale probe sibling " + $probe + ": " + $_.Exception.Message)
        }
    }
}

# Also enumerate dirs in .claude\worktrees that may NOT be registered
$dirRoot = Join-Path $RepoRoot '.claude\worktrees'
$dirsOnDisk = @()
if (Test-Path $dirRoot) {
    $dirsOnDisk = Get-ChildItem $dirRoot -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne '_archive' -and $_.Name -ne '.shrink-quarantine' } |
        Select-Object -ExpandProperty FullName
}
$registeredPaths = $worktrees | ForEach-Object { $_.Path.Replace('/','\').TrimEnd('\').ToLower() }
$unregisteredDirs = $dirsOnDisk | Where-Object { $_.TrimEnd('\').ToLower() -notin $registeredPaths }

Write-Host ("  Found " + $unregisteredDirs.Count + " on-disk dirs not in git's worktree list")

# ---- 2. Per-worktree inspection ----
$rows = @()
$idx = 0
$total = $worktrees.Count + $unregisteredDirs.Count

$allTargets = @()
foreach ($w in $worktrees) {
    $allTargets += [PSCustomObject]@{
        Path=$w.Path; Branch=$w.Branch; HEAD=$w.HEAD; GitLocked=$w.Locked; Registered=$true
    }
}
foreach ($d in $unregisteredDirs) {
    $allTargets += [PSCustomObject]@{
        Path=$d; Branch='(unregistered)'; HEAD=''; GitLocked=$false; Registered=$false
    }
}

foreach ($t in $allTargets) {
    $idx++
    $name = Split-Path $t.Path -Leaf
    if ($idx % 10 -eq 0) { Write-Host ("  [" + $idx + "/" + $total + "] " + $name) }

    $row = [PSCustomObject]@{
        Name=$name; Path=$t.Path; Registered=$t.Registered; Branch=$t.Branch
        HEAD=$t.HEAD; GitLocked=$t.GitLocked
        Bucket=''; CommitsAhead=$null; StatusChanges=$null; StatusSample=''
        IgnoredCount=$null; UntrackedCount=$null; IgnoredSizeMB=$null
        DirEntryCount=$null; WorkingTreeSizeMB=$null; IsStub=$false; IsRecoveryStub=$false; IsEmpty=$false
        UnknownLocalFiles=@(); UnknownLocalCount=0
        FsLocked=$null; FsLockMessage=''
        ShrinkMarkerReady=$false; ShrinkMarkerInProgress=''; ShrinkManifestPath=''
        StubProblem=''
        Notes=@()
    }

    if (-not (Test-Path $t.Path)) {
        $row.Notes += 'PATH MISSING ON DISK'
    } else {
        $entries = @(Get-ChildItem $t.Path -Force -ErrorAction SilentlyContinue)
        $row.DirEntryCount = $entries.Count

        if ($entries.Count -eq 0) {
            $row.IsEmpty = $true
            $row.Notes += 'EMPTY DIR (no .git pointer)'
        } else {
            $entryNames = @($entries | ForEach-Object { $_.Name })
            $allowedStubNames = @('.git', $SentinelFilename)
            $extraStubEntries = @($entryNames | Where-Object { $_ -notin $allowedStubNames })
            if (($entryNames -contains '.git') -and ($entryNames -contains $SentinelFilename) -and $extraStubEntries.Count -eq 0) {
                $sentinel = Join-Path $t.Path $SentinelFilename
                try {
                    $sentinelText = Get-Content -LiteralPath $sentinel -Raw -ErrorAction Stop
                    $missing = @($SentinelRequiredFields | Where-Object { $sentinelText -notmatch [regex]::Escape($_) })
                    if ($missing.Count -eq 0) {
                        $row.IsStub = $true
                    } else {
                        $row.StubProblem = 'sentinel missing fields: ' + ($missing -join ', ')
                    }
                } catch {
                    $row.StubProblem = 'sentinel unreadable: ' + $_.Exception.Message
                }
            } elseif (($entryNames -contains '.git') -and $extraStubEntries.Count -eq 0) {
                # .git-only on disk with no sentinel: worktree created without full
                # materialisation (e.g. via --no-checkout or git worktree add with
                # sparse-checkout). Not a defect -- distinct from a broken-stub.
                $row.IsRecoveryStub = $true
            }
        }

        # Shrink markers
        if (Test-Path (Join-Path $t.Path $MarkerReady)) {
            $row.ShrinkMarkerReady = $true
        }
        $inProg = $entries | Where-Object { $_.Name -like ($MarkerInProgressPrefix + '*') } | Select-Object -First 1
        if ($inProg) {
            $row.ShrinkMarkerInProgress = $inProg.Name
            try {
                $payload = Get-Content -LiteralPath $inProg.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
                if ($payload.manifest_path) { $row.ShrinkManifestPath = $payload.manifest_path }
                else { $row.ShrinkManifestPath = '(marker payload has no manifest_path)' }
            } catch {
                $row.ShrinkManifestPath = '(marker payload unreadable: ' + $_.Exception.Message + ')'
            }
        }

        if (-not $row.IsStub -and -not $row.IsRecoveryStub -and -not $row.IsEmpty) {
            try {
                $sz = (Get-ChildItem $t.Path -Recurse -Force -File -ErrorAction SilentlyContinue |
                    Measure-Object -Property Length -Sum).Sum
                if ($sz) { $row.WorkingTreeSizeMB = [math]::Round($sz / 1MB, 1) }
            } catch {}
        }
    }

    if ($t.Registered -and (Test-Path $t.Path)) {
        try {
            $statusRaw = & git -C $t.Path status --porcelain=v1 --ignored -uall 2>$null
            $statusLines = @($statusRaw | Where-Object { $_ -ne $null -and $_ -ne '' })
            $tracked = $statusLines | Where-Object { $_ -notmatch '^\?\?' -and $_ -notmatch '^\!\!' }
            $untracked = $statusLines | Where-Object { $_ -match '^\?\?' }
            $ignored = $statusLines | Where-Object { $_ -match '^\!\!' }
            $row.StatusChanges = @($tracked).Count
            $row.UntrackedCount = @($untracked).Count
            $row.IgnoredCount = @($ignored).Count
            if (@($tracked).Count -gt 0) {
                $row.StatusSample = (@($tracked) | Select-Object -First 3) -join ' || '
            }

            $unknown = @()
            foreach ($l in @($untracked) + @($ignored)) {
                $rel = $l.Substring(3).Trim('"')
                $comps = ($rel -replace '\\','/').TrimStart('/').Split('/') | Where-Object { $_ }
                $matched = $false
                foreach ($c in $comps) {
                    if ($DisposableDirs -contains $c -or $PreservedDirs -contains $c) {
                        $matched = $true; break
                    }
                }
                if (-not $matched) { $unknown += $rel }
            }
            $row.UnknownLocalFiles = $unknown
            $row.UnknownLocalCount = $unknown.Count

            $extraSize = 0
            foreach ($l in @($untracked) + @($ignored)) {
                $relPath = $l.Substring(3).Trim('"')
                $abs = Join-Path $t.Path $relPath
                if (Test-Path $abs -PathType Leaf) {
                    try { $extraSize += (Get-Item $abs -Force).Length } catch {}
                } elseif (Test-Path $abs -PathType Container) {
                    try { $extraSize += (Get-ChildItem $abs -Recurse -Force -File -ErrorAction SilentlyContinue |
                        Measure-Object -Property Length -Sum).Sum } catch {}
                }
            }
            if ($extraSize) { $row.IgnoredSizeMB = [math]::Round($extraSize / 1MB, 2) }

            $aheadRaw = & git -C $t.Path rev-list --count master..HEAD 2>$null
            if ($LASTEXITCODE -eq 0 -and $aheadRaw) { $row.CommitsAhead = [int]$aheadRaw }
        } catch {
            $row.Notes += ("git status failed: " + $_.Exception.Message)
        }
    }

    # Lock probe -- skip current worktree and main repo
    $skipLockProbe = ($t.Path -eq $RepoRoot) -or
                     ($t.Path.TrimEnd('\').ToLower() -eq $CurrentWorktree.TrimEnd('\').ToLower())
    if (-not $ProbeLocks) {
        $row.FsLocked = $false
        $row.FsLockMessage = '(not probed; use -ProbeLocks)'
    } elseif ($skipLockProbe) {
        $row.FsLocked = $false
        $row.FsLockMessage = '(skipped: current/main)'
    } elseif (Test-Path $t.Path) {
        $probe = $t.Path + '.probetest'
        try {
            Rename-Item $t.Path $probe -ErrorAction Stop
            Rename-Item $probe $t.Path -ErrorAction Stop
            $row.FsLocked = $false
        } catch {
            $row.FsLocked = $true
            $row.FsLockMessage = $_.Exception.Message.Split([Environment]::NewLine)[0]
        } finally {
            if ((Test-Path $probe -PathType Container) -and (-not (Test-Path $t.Path))) {
                try { Rename-Item $probe $t.Path -ErrorAction Stop } catch {}
            }
        }
    }

    # ---- Bucket classification ----
    if ($row.ShrinkMarkerInProgress) {
        $row.Bucket = 'shrink-in-progress'
    } elseif ((-not $row.Registered) -or ($row.Notes -join '|' -match 'PATH MISSING|EMPTY DIR')) {
        $row.Bucket = 'broken-registration'
    } elseif ($row.IsStub) {
        $row.Bucket = 'healthy-stub'
    } elseif ($row.IsRecoveryStub) {
        $row.Bucket = 'recovery-stub'
    } elseif ($row.StubProblem) {
        $row.Bucket = 'broken-stub'
        $row.Notes += $row.StubProblem
    } elseif ($row.FsLocked -eq $true) {
        $row.Bucket = 'active-materialised'
    } elseif ($row.StatusChanges -ne $null -and $row.StatusChanges -gt 0) {
        $row.Bucket = 'dirty-materialised'
    } elseif ($row.UnknownLocalCount -gt 0) {
        $row.Bucket = 'unknown-local-files'
    } else {
        $row.Bucket = 'clean-inactive-materialised'
    }

    $rows += $row
}

Write-Host ""
Write-Host "Inspection complete. Writing report..."

# ---- 3. Generate markdown ----
$now = Get-Date -Format 'yyyy-MM-dd HH:mm'
$mdLines = New-Object System.Collections.ArrayList
[void]$mdLines.Add("# Worktree Inspector Report")
[void]$mdLines.Add("")
[void]$mdLines.Add("Generated: " + $now)
[void]$mdLines.Add("Repo root: ``" + $RepoRoot + "``")
[void]$mdLines.Add("")

$bucketCounts = @{
    'healthy-stub' = 0
    'recovery-stub' = 0
    'active-materialised' = 0
    'clean-inactive-materialised' = 0
    'dirty-materialised' = 0
    'unknown-local-files' = 0
    'broken-registration' = 0
    'broken-stub' = 0
    'shrink-in-progress' = 0
}
foreach ($r in $rows) { $bucketCounts[$r.Bucket]++ }

[void]$mdLines.Add("## Bucket summary")
[void]$mdLines.Add("")
[void]$mdLines.Add("| Bucket | Count | Meaning |")
[void]$mdLines.Add("|---|---:|---|")
[void]$mdLines.Add("| healthy-stub | " + $bucketCounts['healthy-stub'] + " | registered, only .git on disk, sentinel valid (shrink-toolkit output) |")
[void]$mdLines.Add("| recovery-stub | " + $bucketCounts['recovery-stub'] + " | registered, only .git on disk, no sentinel (sparse-checkout or --no-checkout worktree) |")
[void]$mdLines.Add("| active-materialised | " + $bucketCounts['active-materialised'] + " | registered, files present, FS-locked (live session likely) |")
[void]$mdLines.Add("| clean-inactive-materialised | " + $bucketCounts['clean-inactive-materialised'] + " | registered, no live session, eligible for shrink |")
[void]$mdLines.Add("| dirty-materialised | " + $bucketCounts['dirty-materialised'] + " | uncommitted tracked changes -- commit before wrap |")
[void]$mdLines.Add("| unknown-local-files | " + $bucketCounts['unknown-local-files'] + " | untracked files outside the allowlist -- review before shrink |")
[void]$mdLines.Add("| broken-registration | " + $bucketCounts['broken-registration'] + " | git list vs disk mismatch |")
[void]$mdLines.Add("| broken-stub | " + $bucketCounts['broken-stub'] + " | stub marker exists but sentinel payload is missing or invalid |")
[void]$mdLines.Add("| shrink-in-progress | " + $bucketCounts['shrink-in-progress'] + " | .shrink-in-progress.* marker present (running or crashed) |")
[void]$mdLines.Add("")

# Shrink-in-progress detail block
$inProgRows = @($rows | Where-Object Bucket -eq 'shrink-in-progress')
if ($inProgRows.Count -gt 0) {
    [void]$mdLines.Add("## Shrink in progress")
    [void]$mdLines.Add("")
    foreach ($r in $inProgRows) {
        $pidPart = ($r.ShrinkMarkerInProgress -split '\.')[2]
        [void]$mdLines.Add("- **" + $r.Name + "** -- marker ``" + $r.ShrinkMarkerInProgress + "`` (pid " + $pidPart + ")")
        [void]$mdLines.Add("    - manifest: ``" + $r.ShrinkManifestPath + "``")
    }
    [void]$mdLines.Add("")
}

# Filter rows displayed (default hides healthy stubs)
$display = if ($ShowHealthy) { $rows } else { $rows | Where-Object { $_.Bucket -ne 'healthy-stub' -and $_.Bucket -ne 'recovery-stub' } }

[void]$mdLines.Add("## Worktrees -- full table")
[void]$mdLines.Add("")
if (-not $ShowHealthy) {
    $hidden = $bucketCounts['healthy-stub'] + $bucketCounts['recovery-stub']
    [void]$mdLines.Add("_(" + $hidden + " healthy/recovery stubs hidden; re-run with -ShowHealthy to include.)_")
    [void]$mdLines.Add("")
}
[void]$mdLines.Add("Sorted by bucket priority, then name.")
[void]$mdLines.Add("")
[void]$mdLines.Add("| # | Name | Bucket | FS lock | Branch | Ahead | Tracked changes | Unknown local | Tree size | Notes |")
[void]$mdLines.Add("|---|---|---|---|---|---|---|---|---|---|")

$bucketOrder = @{
    'shrink-in-progress' = 0
    'broken-registration' = 1
    'unknown-local-files' = 2
    'broken-stub' = 3
    'dirty-materialised' = 4
    'active-materialised' = 5
    'clean-inactive-materialised' = 6
    'recovery-stub' = 8
    'healthy-stub' = 9
}
$sorted = $display | Sort-Object @{Expression={ $bucketOrder[$_.Bucket] }}, Name

$rowIdx = 0
foreach ($r in $sorted) {
    $rowIdx++
    $lock = if ($r.FsLocked -eq $true) { 'LOCKED' } elseif ($r.FsLocked -eq $false) { '.' } else { '?' }
    $branch = if ($r.Branch) { $r.Branch -replace '^claude/','' } else { '-' }
    $ahead = if ($r.CommitsAhead -ne $null) { [string]$r.CommitsAhead } else { '?' }
    $status = if ($r.StatusChanges -ne $null) { [string]$r.StatusChanges } else { '?' }
    $unk = [string]$r.UnknownLocalCount
    $tsz = if ($r.WorkingTreeSizeMB) { ([string]$r.WorkingTreeSizeMB + ' MB') } elseif ($r.IsStub -or $r.IsRecoveryStub) { '~0' } else { '?' }
    $notes = ($r.Notes -join '; ') -replace '\|','\\|'
    if ($notes.Length -gt 60) { $notes = $notes.Substring(0,60) + '...' }
    [void]$mdLines.Add("| " + $rowIdx + " | " + $r.Name + " | " + $r.Bucket + " | " + $lock + " | " + $branch + " | " + $ahead + " | " + $status + " | " + $unk + " | " + $tsz + " | " + $notes + " |")
}

# Bucket sections (only buckets we display)
[void]$mdLines.Add("")
[void]$mdLines.Add("## Per-bucket detail")
[void]$mdLines.Add("")

foreach ($bucket in 'shrink-in-progress','broken-registration','unknown-local-files','broken-stub','dirty-materialised','active-materialised','clean-inactive-materialised') {
    $br = @($display | Where-Object Bucket -eq $bucket)
    if ($br.Count -eq 0) { continue }
    [void]$mdLines.Add("### " + $bucket + " (" + $br.Count + ")")
    [void]$mdLines.Add("")
    foreach ($r in $br) {
        $headShort = if ($r.HEAD) { $r.HEAD.Substring(0,[Math]::Min(10,$r.HEAD.Length)) } else { '-' }
        [void]$mdLines.Add("- **" + $r.Name + "** -- ``" + $r.Branch + "`` at HEAD ``" + $headShort + "``")
        if ($r.WorkingTreeSizeMB) { [void]$mdLines.Add("    - size: " + $r.WorkingTreeSizeMB + " MB") }
        if ($r.StatusChanges -gt 0) { [void]$mdLines.Add("    - tracked changes: " + $r.StatusChanges + " (sample: ``" + $r.StatusSample + "``)") }
        if ($r.UnknownLocalCount -gt 0) {
            $sample = ($r.UnknownLocalFiles | Select-Object -First 5) -join ', '
            [void]$mdLines.Add("    - unknown local: " + $r.UnknownLocalCount + " (first 5: " + $sample + ")")
        }
        if ($r.FsLockMessage) { [void]$mdLines.Add("    - FS lock: " + $r.FsLockMessage) }
        if ($r.ShrinkMarkerReady) { [void]$mdLines.Add("    - shrink marker: .shrink-when-safe (queued)") }
        if ($r.Notes.Count -gt 0) { [void]$mdLines.Add("    - notes: " + ($r.Notes -join '; ')) }
    }
    [void]$mdLines.Add("")
}

$mdLines | Set-Content -Path $outFile -Encoding UTF8
Write-Host ("Report written: " + $outFile + " (" + $mdLines.Count + " lines)")
